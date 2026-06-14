"""Initial wheel lifecycle validation using single-trade option audits."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from options_quant.backtest import ClosedBacktestPosition
from options_quant.data.models import OptionType
from options_quant.data.providers import ThetaDataProvider, ThetaDataPythonClient
from options_quant.data.providers.thetadata_options import ThetaDataOptionEndpoints
from options_quant.pipelines.single_trade import (
    SingleTradeMarketDataProvider,
    SingleTradeOptionEndpoints,
    SingleTradePipelineConfig,
    SingleTradePipelineResult,
    run_single_trade_pipeline,
)
from options_quant.strategies.wheel import WheelStrategyConfig

ZERO = Decimal("0")
CONTRACT_MULTIPLIER = Decimal("100")

SingleTradeRunner = Callable[[SingleTradePipelineConfig], SingleTradePipelineResult]


class WheelValidationConfig(BaseModel):
    """Configuration for an initial wheel lifecycle validation run."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    strategy: WheelStrategyConfig = Field(default_factory=WheelStrategyConfig)
    start_date: date = Field(default=date(2025, 1, 3))
    trade_count: int = Field(default=5, gt=0)
    end_date: date | None = Field(default=None)
    spacing_days: int = Field(default=7, gt=0)
    expiration_search_window_days: int = Field(default=14, ge=0)
    commission_per_contract: Decimal = Field(default=Decimal("0.65"), ge=ZERO)
    slippage_per_contract: Decimal = Field(default=Decimal("0.00"), ge=ZERO)
    report_path: Path = Field(default=Path("runs/wheel_validation/report.md"))
    theta_mdds_host: str | None = Field(default=None, min_length=1)
    theta_mdds_port: str | None = Field(default=None, min_length=1)
    theta_mdds_type: str | None = Field(default=None, min_length=1)
    verbose: bool = Field(default=False)

    @model_validator(mode="after")
    def validate_date_range(self) -> Self:
        """Validate optional date-range settings."""
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must be greater than or equal to start_date")
        return self


@dataclass(frozen=True)
class WheelEvent:
    """One wheel lifecycle event."""

    event_date: date
    event_type: str
    description: str
    cash_balance: Decimal
    realized_pnl: Decimal
    share_quantity: int
    share_cost_basis: Decimal | None


@dataclass(frozen=True)
class WheelDailySnapshot:
    """One daily mark-to-market wheel portfolio snapshot."""

    observed_date: date
    cash_balance: Decimal
    stock_value: Decimal
    option_value: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    drawdown: Decimal
    share_quantity: int
    share_cost_basis: Decimal | None
    underlying_price: Decimal | None
    open_options: int


@dataclass(frozen=True)
class WheelValidationResult:
    """Initial wheel lifecycle validation output."""

    config: WheelValidationConfig
    entry_dates: tuple[date, ...]
    option_trades: tuple[SingleTradePipelineResult, ...]
    events: tuple[WheelEvent, ...]
    snapshots: tuple[WheelDailySnapshot, ...]
    failed_entries: tuple[tuple[date, str], ...]
    skipped_entries: tuple[tuple[date, str], ...]
    cash_balance: Decimal
    realized_pnl: Decimal
    share_quantity: int
    share_cost_basis: Decimal | None


def run_wheel_validation_pipeline(
    config: WheelValidationConfig,
    *,
    trade_runner: SingleTradeRunner | None = None,
) -> WheelValidationResult:
    """Run an initial assignment-aware wheel lifecycle test."""
    runner = trade_runner if trade_runner is not None else _live_trade_runner(config)
    entry_dates = _entry_dates(config)
    option_trades: list[SingleTradePipelineResult] = []
    market_observations: list[SingleTradePipelineResult] = []
    events: list[WheelEvent] = []
    failures: list[tuple[date, str]] = []
    skips: list[tuple[date, str]] = []

    cash_balance = config.strategy.initial_cash
    realized_pnl = ZERO
    share_quantity = 0
    share_cost_basis: Decimal | None = None
    put_active_until: date | None = None
    call_active_until: date | None = None

    for index, entry_date in enumerate(entry_dates, start=1):
        if put_active_until is not None and entry_date < put_active_until:
            skips.append((entry_date, f"short put active until {put_active_until}"))
            continue
        if put_active_until is not None and entry_date >= put_active_until:
            put_active_until = None
        if call_active_until is not None and entry_date < call_active_until:
            skips.append((entry_date, f"covered call active until {call_active_until}"))
            continue
        if call_active_until is not None and entry_date >= call_active_until:
            call_active_until = None

        if share_quantity == 0:
            trade_config = _single_trade_config(config, entry_date, index, OptionType.PUT)
            try:
                trade = runner(trade_config)
            except Exception as error:
                failures.append((entry_date, str(error)))
                continue
            option_trades.append(trade)
            market_observations.append(trade)
            put_active_until = None
            cash_balance += trade.audit.entry_net_cash_flow
            closed = _closed(trade)
            if closed is None:
                skips.append((entry_date, "put did not close in single-trade audit"))
                continue
            put_active_until = closed.exit_date
            if trade.audit.exit_price is not None and trade.audit.exit_price > ZERO:
                assignment_cash = trade.selected_candidate.contract.strike * CONTRACT_MULTIPLIER
                cash_balance -= assignment_cash
                share_quantity = config.strategy.share_lot_size
                net_premium_per_share = trade.audit.entry_net_cash_flow / CONTRACT_MULTIPLIER
                share_cost_basis = trade.selected_candidate.contract.strike - net_premium_per_share
                events.append(
                    WheelEvent(
                        event_date=closed.exit_date,
                        event_type="put_assigned",
                        description=(
                            f"assigned {share_quantity} shares at "
                            f"{trade.selected_candidate.contract.strike}"
                        ),
                        cash_balance=cash_balance,
                        realized_pnl=realized_pnl,
                        share_quantity=share_quantity,
                        share_cost_basis=share_cost_basis,
                    )
                )
            else:
                realized_pnl += trade.audit.entry_net_cash_flow
                events.append(
                    WheelEvent(
                        event_date=closed.exit_date,
                        event_type="put_expired_otm",
                        description="short put expired out of the money",
                        cash_balance=cash_balance,
                        realized_pnl=realized_pnl,
                        share_quantity=share_quantity,
                        share_cost_basis=share_cost_basis,
                    )
                )
            continue

        if share_cost_basis is None:
            skips.append((entry_date, "assigned shares missing cost basis"))
            continue
        trade_config = _single_trade_config(config, entry_date, index, OptionType.CALL)
        try:
            trade = runner(trade_config)
        except Exception as error:
            failures.append((entry_date, str(error)))
            continue
        market_observations.append(trade)
        if trade.selected_candidate.contract.strike < share_cost_basis:
            skips.append(
                (
                    entry_date,
                    (
                        f"covered call strike {trade.selected_candidate.contract.strike} "
                        f"below cost basis {share_cost_basis}"
                    ),
                )
            )
            continue
        option_trades.append(trade)
        call_active_until = None
        cash_balance += trade.audit.entry_net_cash_flow
        realized_pnl += trade.audit.entry_net_cash_flow
        closed = _closed(trade)
        if closed is None:
            skips.append((entry_date, "covered call did not close in single-trade audit"))
            continue
        call_active_until = closed.exit_date
        if trade.audit.exit_price is not None and trade.audit.exit_price > ZERO:
            stock_sale_cash = trade.selected_candidate.contract.strike * CONTRACT_MULTIPLIER
            stock_pnl = (
                trade.selected_candidate.contract.strike - share_cost_basis
            ) * CONTRACT_MULTIPLIER
            cash_balance += stock_sale_cash
            realized_pnl += stock_pnl
            events.append(
                WheelEvent(
                    event_date=closed.exit_date,
                    event_type="shares_called_away",
                    description=(
                        f"sold {share_quantity} shares at "
                        f"{trade.selected_candidate.contract.strike}"
                    ),
                    cash_balance=cash_balance,
                    realized_pnl=realized_pnl,
                    share_quantity=0,
                    share_cost_basis=None,
                )
            )
            share_quantity = 0
            share_cost_basis = None
        else:
            events.append(
                WheelEvent(
                    event_date=closed.exit_date,
                    event_type="call_expired_otm",
                    description="covered call expired out of the money",
                    cash_balance=cash_balance,
                    realized_pnl=realized_pnl,
                    share_quantity=share_quantity,
                    share_cost_basis=share_cost_basis,
                )
            )

    result = WheelValidationResult(
        config=config,
        entry_dates=entry_dates,
        option_trades=tuple(option_trades),
        events=tuple(events),
        snapshots=tuple(_daily_snapshots(config, option_trades, market_observations, events)),
        failed_entries=tuple(failures),
        skipped_entries=tuple(skips),
        cash_balance=cash_balance,
        realized_pnl=realized_pnl,
        share_quantity=share_quantity,
        share_cost_basis=share_cost_basis,
    )
    config.report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(config.report_path, result)
    return result


def _daily_snapshots(
    config: WheelValidationConfig,
    option_trades: list[SingleTradePipelineResult],
    market_observations: list[SingleTradePipelineResult],
    events: list[WheelEvent],
) -> list[WheelDailySnapshot]:
    cash_events = _cash_events(option_trades)
    dates = sorted(
        {
            snapshot.date
            for trade in market_observations
            for snapshot in trade.backtest_result.snapshots
        }
        | {
            underlying.timestamp.date()
            for trade in market_observations
            for underlying in trade.underlying_prices
        }
        | {event.event_date for event in events}
        | set(cash_events)
    )
    if not dates:
        return []

    underlying_by_date = _underlying_by_date(market_observations)
    cash_balance = config.strategy.initial_cash
    peak = config.strategy.initial_cash
    snapshots: list[WheelDailySnapshot] = []
    for observed_date in dates:
        cash_balance += cash_events.get(observed_date, ZERO)
        share_quantity, share_cost_basis = _share_state(events, observed_date)
        underlying_price = underlying_by_date.get(observed_date)
        stock_value = (
            underlying_price * Decimal(share_quantity)
            if underlying_price is not None and share_quantity > 0
            else ZERO
        )
        option_value = _open_option_value(option_trades, observed_date)
        equity = cash_balance + stock_value + option_value
        stock_unrealized = (
            (underlying_price - share_cost_basis) * Decimal(share_quantity)
            if underlying_price is not None and share_cost_basis is not None and share_quantity > 0
            else ZERO
        )
        option_unrealized = _open_option_unrealized(option_trades, observed_date)
        unrealized_pnl = stock_unrealized + option_unrealized
        realized_pnl = equity - config.strategy.initial_cash - unrealized_pnl
        peak = max(peak, equity)
        drawdown = equity / peak - Decimal("1") if peak != ZERO else ZERO
        snapshots.append(
            WheelDailySnapshot(
                observed_date=observed_date,
                cash_balance=cash_balance,
                stock_value=stock_value,
                option_value=option_value,
                equity=equity,
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                drawdown=drawdown,
                share_quantity=share_quantity,
                share_cost_basis=share_cost_basis,
                underlying_price=underlying_price,
                open_options=_open_option_count(option_trades, observed_date),
            )
        )
    return snapshots


def _cash_events(option_trades: list[SingleTradePipelineResult]) -> dict[date, Decimal]:
    events: dict[date, Decimal] = {}
    for trade in option_trades:
        _add_cash_event(events, trade.config.entry_date, trade.audit.entry_net_cash_flow)
        closed = _closed(trade)
        if closed is None or trade.audit.exit_price is None or trade.audit.exit_price <= ZERO:
            continue
        if trade.selected_candidate.contract.option_type is OptionType.PUT:
            assignment_cash = trade.selected_candidate.contract.strike * CONTRACT_MULTIPLIER
            _add_cash_event(events, closed.exit_date, -assignment_cash)
        else:
            stock_sale_cash = trade.selected_candidate.contract.strike * CONTRACT_MULTIPLIER
            _add_cash_event(events, closed.exit_date, stock_sale_cash)
    return events


def _add_cash_event(events: dict[date, Decimal], event_date: date, amount: Decimal) -> None:
    events[event_date] = events.get(event_date, ZERO) + amount


def _underlying_by_date(option_trades: list[SingleTradePipelineResult]) -> dict[date, Decimal]:
    prices: dict[date, Decimal] = {}
    for trade in option_trades:
        for underlying in trade.underlying_prices:
            prices[underlying.timestamp.date()] = underlying.price
    return prices


def _share_state(events: list[WheelEvent], observed_date: date) -> tuple[int, Decimal | None]:
    share_quantity = 0
    share_cost_basis: Decimal | None = None
    for event in sorted(events, key=lambda item: item.event_date):
        if event.event_date > observed_date:
            break
        share_quantity = event.share_quantity
        share_cost_basis = event.share_cost_basis
    return share_quantity, share_cost_basis


def _open_option_value(
    option_trades: list[SingleTradePipelineResult],
    observed_date: date,
) -> Decimal:
    return sum(
        (
            snapshot.equity - snapshot.cash_balance
            for trade in option_trades
            if _option_is_open(trade, observed_date)
            for snapshot in trade.backtest_result.snapshots
            if snapshot.date == observed_date
        ),
        ZERO,
    )


def _open_option_unrealized(
    option_trades: list[SingleTradePipelineResult],
    observed_date: date,
) -> Decimal:
    return sum(
        (
            snapshot.unrealized_pnl
            for trade in option_trades
            if _option_is_open(trade, observed_date)
            for snapshot in trade.backtest_result.snapshots
            if snapshot.date == observed_date
        ),
        ZERO,
    )


def _open_option_count(option_trades: list[SingleTradePipelineResult], observed_date: date) -> int:
    return sum(1 for trade in option_trades if _option_is_open(trade, observed_date))


def _option_is_open(trade: SingleTradePipelineResult, observed_date: date) -> bool:
    closed = _closed(trade)
    if closed is None:
        return trade.config.entry_date <= observed_date
    return bool(trade.config.entry_date <= observed_date < closed.exit_date)


def _entry_dates(config: WheelValidationConfig) -> tuple[date, ...]:
    if config.end_date is None:
        return tuple(
            config.start_date + timedelta(days=config.spacing_days * index)
            for index in range(config.trade_count)
        )
    dates: list[date] = []
    current = config.start_date
    while current <= config.end_date:
        dates.append(current)
        current += timedelta(days=config.spacing_days)
    return tuple(dates)


def _live_trade_runner(config: WheelValidationConfig) -> SingleTradeRunner:
    theta_module = cast(Any, import_module("thetadata"))
    client_kwargs: dict[str, str] = {"dataframe_type": "pandas"}
    if config.theta_mdds_host is not None:
        client_kwargs["mdds_host"] = config.theta_mdds_host
    if config.theta_mdds_port is not None:
        client_kwargs["mdds_port"] = config.theta_mdds_port
    if config.theta_mdds_type is not None:
        client_kwargs["mdds_type"] = config.theta_mdds_type
    theta_client = theta_module.ThetaClient(**client_kwargs)
    endpoints = cast(SingleTradeOptionEndpoints, ThetaDataOptionEndpoints(client=theta_client))
    provider = cast(
        SingleTradeMarketDataProvider,
        ThetaDataProvider(ThetaDataPythonClient(client=theta_client)),
    )

    def run(config: SingleTradePipelineConfig) -> SingleTradePipelineResult:
        return run_single_trade_pipeline(config, endpoints=endpoints, provider=provider)

    return run


def _single_trade_config(
    config: WheelValidationConfig,
    entry_date: date,
    index: int,
    option_type: OptionType,
) -> SingleTradePipelineConfig:
    strategy = config.strategy
    target_dte = (
        (strategy.put_min_dte + strategy.put_max_dte) // 2
        if option_type is OptionType.PUT
        else (strategy.call_min_dte + strategy.call_max_dte) // 2
    )
    target_delta = (
        strategy.put_target_delta if option_type is OptionType.PUT else strategy.call_target_delta
    )
    return SingleTradePipelineConfig(
        symbol=strategy.underlying_symbol,
        entry_date=entry_date,
        target_dte=target_dte,
        target_delta=target_delta,
        option_type=option_type,
        quantity=strategy.contract_quantity,
        initial_cash=strategy.initial_cash,
        commission_per_contract=config.commission_per_contract,
        slippage_per_contract=config.slippage_per_contract,
        take_profit_pct=strategy.take_profit_pct,
        stop_loss_pct=strategy.stop_loss_pct,
        expiration_search_window_days=config.expiration_search_window_days,
        report_path=config.report_path.parent
        / f"wheel_{index:02d}_{entry_date.isoformat()}_{option_type.value}.md",
        theta_mdds_host=config.theta_mdds_host,
        theta_mdds_port=config.theta_mdds_port,
        theta_mdds_type=config.theta_mdds_type,
        verbose=config.verbose,
    )


def _closed(trade: SingleTradePipelineResult) -> ClosedBacktestPosition | None:
    if not trade.backtest_result.closed_positions:
        return None
    return trade.backtest_result.closed_positions[-1]


def _write_report(path: Path, result: WheelValidationResult) -> None:
    payload = {
        "symbol": result.config.strategy.underlying_symbol,
        "start_date": result.config.start_date.isoformat(),
        "end_date": result.config.end_date.isoformat()
        if result.config.end_date is not None
        else None,
        "entry_dates": len(result.entry_dates),
        "option_trades": len(result.option_trades),
        "events": [_event_payload(event) for event in result.events],
        "failed_entries": [
            {"entry_date": entry_date.isoformat(), "error": error}
            for entry_date, error in result.failed_entries
        ],
        "skipped_entries": [
            {"entry_date": entry_date.isoformat(), "reason": reason}
            for entry_date, reason in result.skipped_entries
        ],
        "cash_balance": _money(result.cash_balance),
        "realized_pnl": _money(result.realized_pnl),
        "share_quantity": result.share_quantity,
        "share_cost_basis": _money(result.share_cost_basis),
        "final_equity": _money(result.snapshots[-1].equity if result.snapshots else None),
        "max_drawdown": _ratio(
            min((snapshot.drawdown for snapshot in result.snapshots), default=ZERO)
        ),
        "min_equity": _money(
            min((snapshot.equity for snapshot in result.snapshots), default=result.cash_balance)
        ),
        "snapshots": [_snapshot_payload(snapshot) for snapshot in result.snapshots],
    }
    lines = [
        "# Wheel Validation",
        "",
        "## JSON Summary",
        "",
        json.dumps(payload, indent=2),
        "",
        "## Events",
        "",
    ]
    if not result.events:
        lines.append("- No wheel events")
    for event in result.events:
        lines.append(
            "- "
            f"{event.event_date}: {event.event_type} - {event.description}; "
            f"cash={_money(event.cash_balance)} shares={event.share_quantity} "
            f"basis={_money(event.share_cost_basis)}"
        )
    if result.skipped_entries:
        lines.extend(["", "## Skipped", ""])
        for entry_date, reason in result.skipped_entries:
            lines.append(f"- {entry_date}: {reason}")
    if result.failed_entries:
        lines.extend(["", "## Failures", ""])
        for entry_date, error in result.failed_entries:
            lines.append(f"- {entry_date}: {error}")
    lines.extend(["", "## Daily Mark To Market", ""])
    if not result.snapshots:
        lines.append("- No daily snapshots")
    for snapshot in result.snapshots:
        lines.append(
            "- "
            f"{snapshot.observed_date}: equity={_money(snapshot.equity)} "
            f"cash={_money(snapshot.cash_balance)} "
            f"stock={_money(snapshot.stock_value)} "
            f"option={_money(snapshot.option_value)} "
            f"drawdown={_ratio(snapshot.drawdown)} "
            f"shares={snapshot.share_quantity} open_options={snapshot.open_options}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _event_payload(event: WheelEvent) -> dict[str, str | int | None]:
    return {
        "event_date": event.event_date.isoformat(),
        "event_type": event.event_type,
        "description": event.description,
        "cash_balance": _money(event.cash_balance),
        "realized_pnl": _money(event.realized_pnl),
        "share_quantity": event.share_quantity,
        "share_cost_basis": _money(event.share_cost_basis),
    }


def _snapshot_payload(snapshot: WheelDailySnapshot) -> dict[str, str | int | None]:
    return {
        "date": snapshot.observed_date.isoformat(),
        "cash_balance": _money(snapshot.cash_balance),
        "stock_value": _money(snapshot.stock_value),
        "option_value": _money(snapshot.option_value),
        "equity": _money(snapshot.equity),
        "realized_pnl": _money(snapshot.realized_pnl),
        "unrealized_pnl": _money(snapshot.unrealized_pnl),
        "drawdown": _ratio(snapshot.drawdown),
        "share_quantity": snapshot.share_quantity,
        "share_cost_basis": _money(snapshot.share_cost_basis),
        "underlying_price": _money(snapshot.underlying_price),
        "open_options": snapshot.open_options,
    }


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.01")))


def _ratio(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.0001")))
