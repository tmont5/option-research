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
class WheelValidationResult:
    """Initial wheel lifecycle validation output."""

    config: WheelValidationConfig
    entry_dates: tuple[date, ...]
    option_trades: tuple[SingleTradePipelineResult, ...]
    events: tuple[WheelEvent, ...]
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


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.01")))
