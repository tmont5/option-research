"""Cash-secured portfolio validation over selected single-trade candidates."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module
from math import sqrt
from pathlib import Path
from typing import Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from options_quant.backtest import BacktestAccountSnapshot, ClosedBacktestPosition
from options_quant.data.models import OptionContract, OptionType
from options_quant.data.providers import ThetaDataProvider, ThetaDataPythonClient
from options_quant.data.providers.thetadata_options import ThetaDataOptionEndpoints
from options_quant.pipelines.single_trade import (
    SingleTradeMarketDataProvider,
    SingleTradeOptionEndpoints,
    SingleTradePipelineConfig,
    SingleTradePipelineResult,
    run_single_trade_pipeline,
)

ZERO = Decimal("0")
WEEKLY_PERIODS_PER_YEAR = Decimal("52")
MIN_SHARPE_TRADES = 30

SingleTradeRunner = Callable[[SingleTradePipelineConfig], SingleTradePipelineResult]


class PortfolioValidationConfig(BaseModel):
    """Configuration for one-account cash-secured portfolio validation."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(default="SPY", min_length=1)
    start_date: date = Field(default=date(2025, 1, 3))
    trade_count: int = Field(default=5, gt=0)
    end_date: date | None = Field(default=None)
    spacing_days: int = Field(default=7, gt=0)
    target_dte: int = Field(default=45, ge=0)
    target_delta: Decimal = Field(default=Decimal("-0.10"), ge=Decimal("-1"), le=Decimal("1"))
    option_type: OptionType = Field(default=OptionType.PUT)
    quantity: int = Field(default=1, gt=0)
    initial_cash: Decimal = Field(default=Decimal("100000"), gt=Decimal("0"))
    commission_per_contract: Decimal = Field(default=Decimal("0.65"), ge=Decimal("0"))
    slippage_per_contract: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    take_profit_pct: Decimal | None = Field(default=None, gt=Decimal("0"), le=Decimal("1"))
    stop_loss_pct: Decimal | None = Field(default=None, gt=Decimal("0"))
    expiration_search_window_days: int = Field(default=14, ge=0)
    report_path: Path = Field(default=Path("runs/portfolio_validation/report.md"))
    theta_mdds_host: str | None = Field(default=None, min_length=1)
    theta_mdds_port: str | None = Field(default=None, min_length=1)
    theta_mdds_type: str | None = Field(default=None, min_length=1)
    verbose: bool = Field(default=False)

    @model_validator(mode="after")
    def validate_date_range(self) -> Self:
        """Validate optional date-range generation settings."""
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must be greater than or equal to start_date")
        return self


@dataclass(frozen=True)
class PortfolioTradeFailure:
    """One failed candidate entry date."""

    entry_date: date
    error: str


@dataclass(frozen=True)
class PortfolioTradeSkip:
    """One candidate skipped because the account lacked cash-secured capacity."""

    entry_date: date
    contract: OptionContract
    required_collateral: Decimal
    available_cash: Decimal


@dataclass(frozen=True)
class PortfolioAcceptedTrade:
    """One selected trade accepted into the portfolio."""

    trade: SingleTradePipelineResult
    required_collateral: Decimal


@dataclass(frozen=True)
class PortfolioSnapshot:
    """One daily portfolio account snapshot."""

    observed_date: date
    cash_balance: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    reserved_collateral: Decimal
    available_cash: Decimal
    capital_utilization: Decimal
    open_positions: int


@dataclass(frozen=True)
class PortfolioValidationMetrics:
    """Aggregate metrics for a cash-secured portfolio run."""

    accepted_trades: int
    skipped_trades: int
    failed_trades: int
    total_realized_pnl: Decimal
    win_rate: Decimal | None
    per_trade_sharpe: Decimal | None
    sharpe_note: str | None
    max_drawdown: Decimal
    max_capital_utilization: Decimal
    final_equity: Decimal


@dataclass(frozen=True)
class PortfolioValidationResult:
    """Complete portfolio validation output."""

    config: PortfolioValidationConfig
    entry_dates: tuple[date, ...]
    accepted_trades: tuple[PortfolioAcceptedTrade, ...]
    skipped_trades: tuple[PortfolioTradeSkip, ...]
    failures: tuple[PortfolioTradeFailure, ...]
    snapshots: tuple[PortfolioSnapshot, ...]
    metrics: PortfolioValidationMetrics


def run_portfolio_validation_pipeline(
    config: PortfolioValidationConfig,
    *,
    trade_runner: SingleTradeRunner | None = None,
) -> PortfolioValidationResult:
    """Run one-account cash-secured validation over candidate weekly trades."""
    entry_dates = _entry_dates(config)
    runner = trade_runner if trade_runner is not None else _live_trade_runner(config)
    accepted: list[PortfolioAcceptedTrade] = []
    skipped: list[PortfolioTradeSkip] = []
    failures: list[PortfolioTradeFailure] = []
    cash_balance = config.initial_cash
    closed_cash_applied: set[int] = set()

    for index, entry_date in enumerate(entry_dates, start=1):
        _log(config, f"[{index}/{len(entry_dates)}] selecting {config.symbol} entry {entry_date}")
        cash_balance = _apply_closed_cash(cash_balance, accepted, closed_cash_applied, entry_date)
        reserved_collateral = _reserved_collateral(accepted, closed_cash_applied)
        available_cash = cash_balance - reserved_collateral
        trade_config = _single_trade_config(config, entry_date, index)
        try:
            trade = runner(trade_config)
        except Exception as error:
            failures.append(PortfolioTradeFailure(entry_date=entry_date, error=str(error)))
            _log(config, f"  failed: {error}")
            continue

        collateral = _cash_secured_collateral(
            trade.selected_candidate.contract,
            config.quantity,
        )
        if collateral > available_cash:
            skipped.append(
                PortfolioTradeSkip(
                    entry_date=entry_date,
                    contract=trade.selected_candidate.contract,
                    required_collateral=collateral,
                    available_cash=available_cash,
                )
            )
            _log(
                config,
                f"  skipped: collateral {collateral} exceeds available cash {available_cash}",
            )
            continue

        accepted_trade = PortfolioAcceptedTrade(trade=trade, required_collateral=collateral)
        accepted.append(accepted_trade)
        cash_balance += trade.audit.entry_net_cash_flow
        _log(config, f"  accepted: collateral {collateral}")

    snapshots = tuple(_portfolio_snapshots(config, accepted))
    result = PortfolioValidationResult(
        config=config,
        entry_dates=entry_dates,
        accepted_trades=tuple(accepted),
        skipped_trades=tuple(skipped),
        failures=tuple(failures),
        snapshots=snapshots,
        metrics=_metrics(config, accepted, skipped, failures, snapshots),
    )
    config.report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(config.report_path, result)
    _log(config, f"wrote report to {config.report_path}")
    return result


def _apply_closed_cash(
    cash_balance: Decimal,
    accepted: list[PortfolioAcceptedTrade],
    closed_cash_applied: set[int],
    entry_date: date,
) -> Decimal:
    for index, accepted_trade in enumerate(accepted):
        if index in closed_cash_applied:
            continue
        trade = accepted_trade.trade
        closed = _closed_position(trade)
        if closed is None or closed.exit_date > entry_date:
            continue
        exit_gross_debit = trade.audit.exit_gross_debit
        exit_commission = trade.audit.exit_commission
        if exit_gross_debit is None or exit_commission is None:
            continue
        cash_balance -= exit_gross_debit + exit_commission
        closed_cash_applied.add(index)
    return cash_balance


def _reserved_collateral(
    accepted: list[PortfolioAcceptedTrade],
    closed_cash_applied: set[int],
) -> Decimal:
    return sum(
        (
            accepted_trade.required_collateral
            for index, accepted_trade in enumerate(accepted)
            if index not in closed_cash_applied
        ),
        ZERO,
    )


def _portfolio_snapshots(
    config: PortfolioValidationConfig,
    accepted: list[PortfolioAcceptedTrade],
) -> list[PortfolioSnapshot]:
    dates = sorted(
        {
            snapshot.date
            for accepted_trade in accepted
            for snapshot in accepted_trade.trade.backtest_result.snapshots
        }
    )
    cash_events: dict[date, Decimal] = {}
    realized_events: dict[date, Decimal] = {}
    for accepted_trade in accepted:
        trade = accepted_trade.trade
        _add_event(cash_events, trade.config.entry_date, trade.audit.entry_net_cash_flow)
        closed = _closed_position(trade)
        if closed is None:
            continue
        if trade.audit.exit_gross_debit is not None and trade.audit.exit_commission is not None:
            _add_event(
                cash_events,
                closed.exit_date,
                -(trade.audit.exit_gross_debit + trade.audit.exit_commission),
            )
        if trade.audit.realized_pnl is not None:
            _add_event(realized_events, closed.exit_date, trade.audit.realized_pnl)

    snapshots: list[PortfolioSnapshot] = []
    cash_balance = config.initial_cash
    realized_pnl = ZERO
    for observed_date in dates:
        cash_balance += cash_events.get(observed_date, ZERO)
        realized_pnl += realized_events.get(observed_date, ZERO)
        unrealized_pnl = _portfolio_unrealized_pnl(accepted, observed_date)
        equity = config.initial_cash + realized_pnl + unrealized_pnl
        reserved_collateral = _portfolio_reserved_collateral(accepted, observed_date)
        available_cash = cash_balance - reserved_collateral
        capital_utilization = ZERO if equity == ZERO else reserved_collateral / equity
        snapshots.append(
            PortfolioSnapshot(
                observed_date=observed_date,
                cash_balance=cash_balance,
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                equity=equity,
                reserved_collateral=reserved_collateral,
                available_cash=available_cash,
                capital_utilization=capital_utilization,
                open_positions=_open_positions(accepted, observed_date),
            )
        )
    return snapshots


def _portfolio_unrealized_pnl(
    accepted: list[PortfolioAcceptedTrade],
    observed_date: date,
) -> Decimal:
    total = ZERO
    for accepted_trade in accepted:
        snapshot = _snapshot_for_date(accepted_trade.trade, observed_date)
        if snapshot is not None:
            total += snapshot.unrealized_pnl
    return total


def _portfolio_reserved_collateral(
    accepted: list[PortfolioAcceptedTrade],
    observed_date: date,
) -> Decimal:
    return sum(
        (
            accepted_trade.required_collateral
            for accepted_trade in accepted
            if _is_open_on(accepted_trade.trade, observed_date)
        ),
        ZERO,
    )


def _open_positions(accepted: list[PortfolioAcceptedTrade], observed_date: date) -> int:
    return sum(1 for accepted_trade in accepted if _is_open_on(accepted_trade.trade, observed_date))


def _is_open_on(trade: SingleTradePipelineResult, observed_date: date) -> bool:
    closed = _closed_position(trade)
    if closed is None:
        return trade.config.entry_date <= observed_date
    return bool(trade.config.entry_date <= observed_date < closed.exit_date)


def _snapshot_for_date(
    trade: SingleTradePipelineResult,
    observed_date: date,
) -> BacktestAccountSnapshot | None:
    for snapshot in trade.backtest_result.snapshots:
        if snapshot.date == observed_date:
            return snapshot
    return None


def _add_event(events: dict[date, Decimal], event_date: date, amount: Decimal) -> None:
    events[event_date] = events.get(event_date, ZERO) + amount


def _metrics(
    config: PortfolioValidationConfig,
    accepted: list[PortfolioAcceptedTrade],
    skipped: list[PortfolioTradeSkip],
    failures: list[PortfolioTradeFailure],
    snapshots: tuple[PortfolioSnapshot, ...],
) -> PortfolioValidationMetrics:
    pnls = [
        accepted_trade.trade.audit.realized_pnl
        for accepted_trade in accepted
        if accepted_trade.trade.audit.realized_pnl is not None
    ]
    total_pnl = sum(pnls, ZERO)
    final_equity = snapshots[-1].equity if snapshots else config.initial_cash
    return PortfolioValidationMetrics(
        accepted_trades=len(accepted),
        skipped_trades=len(skipped),
        failed_trades=len(failures),
        total_realized_pnl=total_pnl,
        win_rate=_win_rate(pnls),
        per_trade_sharpe=_per_trade_sharpe(pnls, config.initial_cash),
        sharpe_note=_sharpe_note(pnls),
        max_drawdown=_max_drawdown(snapshots),
        max_capital_utilization=max(
            (snapshot.capital_utilization for snapshot in snapshots),
            default=ZERO,
        ),
        final_equity=final_equity,
    )


def _entry_dates(config: PortfolioValidationConfig) -> tuple[date, ...]:
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


def _live_trade_runner(config: PortfolioValidationConfig) -> SingleTradeRunner:
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
    config: PortfolioValidationConfig,
    entry_date: date,
    index: int,
) -> SingleTradePipelineConfig:
    return SingleTradePipelineConfig(
        symbol=config.symbol,
        entry_date=entry_date,
        target_dte=config.target_dte,
        target_delta=config.target_delta,
        option_type=config.option_type,
        quantity=config.quantity,
        initial_cash=config.initial_cash,
        commission_per_contract=config.commission_per_contract,
        slippage_per_contract=config.slippage_per_contract,
        take_profit_pct=config.take_profit_pct,
        stop_loss_pct=config.stop_loss_pct,
        expiration_search_window_days=config.expiration_search_window_days,
        report_path=config.report_path.parent
        / f"candidate_{index:02d}_{entry_date.isoformat()}.md",
        theta_mdds_host=config.theta_mdds_host,
        theta_mdds_port=config.theta_mdds_port,
        theta_mdds_type=config.theta_mdds_type,
        verbose=config.verbose,
    )


def _cash_secured_collateral(contract: OptionContract, quantity: int) -> Decimal:
    return contract.strike * Decimal(quantity) * Decimal(contract.multiplier)


def _closed_position(trade: SingleTradePipelineResult) -> ClosedBacktestPosition | None:
    if not trade.backtest_result.closed_positions:
        return None
    return trade.backtest_result.closed_positions[-1]


def _win_rate(pnls: list[Decimal]) -> Decimal | None:
    if not pnls:
        return None
    wins = sum(1 for pnl in pnls if pnl > ZERO)
    return Decimal(wins) / Decimal(len(pnls))


def _per_trade_sharpe(pnls: list[Decimal], initial_cash: Decimal) -> Decimal | None:
    if len(pnls) < MIN_SHARPE_TRADES:
        return None
    returns = [pnl / initial_cash for pnl in pnls]
    average_return = sum(returns, ZERO) / Decimal(len(returns))
    variance = sum((value - average_return) ** 2 for value in returns) / Decimal(len(returns))
    volatility = Decimal(str(sqrt(float(variance)))) if variance > ZERO else ZERO
    if volatility == ZERO:
        return None
    return (average_return / volatility) * Decimal(str(sqrt(float(WEEKLY_PERIODS_PER_YEAR))))


def _sharpe_note(pnls: list[Decimal]) -> str | None:
    if len(pnls) >= MIN_SHARPE_TRADES:
        return None
    return (
        f"insufficient sample: {len(pnls)} accepted trades; "
        f"need at least {MIN_SHARPE_TRADES} for a rough Sharpe estimate"
    )


def _max_drawdown(snapshots: tuple[PortfolioSnapshot, ...]) -> Decimal:
    if not snapshots:
        return ZERO
    peak = snapshots[0].equity
    max_drawdown = ZERO
    for snapshot in snapshots:
        peak = max(peak, snapshot.equity)
        drawdown = snapshot.equity / peak - Decimal("1")
        max_drawdown = min(max_drawdown, drawdown)
    return max_drawdown


def _write_report(path: Path, result: PortfolioValidationResult) -> None:
    metrics = result.metrics
    payload = {
        "symbol": result.config.symbol,
        "start_date": result.config.start_date.isoformat(),
        "end_date": result.config.end_date.isoformat()
        if result.config.end_date is not None
        else None,
        "initial_cash": _money_text(result.config.initial_cash),
        "cash_secured": True,
        "collateral_model": "strike * quantity * multiplier",
        "generated_entry_dates": len(result.entry_dates),
        "target_dte": result.config.target_dte,
        "target_delta": str(result.config.target_delta),
        "take_profit_pct": _decimal_text(result.config.take_profit_pct),
        "stop_loss_pct": _decimal_text(result.config.stop_loss_pct),
        "accepted_trades": metrics.accepted_trades,
        "skipped_trades": metrics.skipped_trades,
        "failed_trades": metrics.failed_trades,
        "total_realized_pnl": _money_text(metrics.total_realized_pnl),
        "win_rate": _decimal_text(metrics.win_rate),
        "per_trade_sharpe": _ratio_text(metrics.per_trade_sharpe),
        "sharpe_note": metrics.sharpe_note,
        "max_drawdown": _ratio_text(metrics.max_drawdown),
        "max_capital_utilization": _ratio_text(metrics.max_capital_utilization),
        "final_equity": _money_text(metrics.final_equity),
        "accepted": [_accepted_payload(accepted) for accepted in result.accepted_trades],
        "skipped": [_skip_payload(skip) for skip in result.skipped_trades],
        "failures": [
            {"entry_date": failure.entry_date.isoformat(), "error": failure.error}
            for failure in result.failures
        ],
    }
    lines = [
        "# Cash-Secured Portfolio Validation",
        "",
        "## JSON Summary",
        "",
        json.dumps(payload, indent=2),
        "",
        "## Accepted Trades",
        "",
    ]
    if not result.accepted_trades:
        lines.append("- No accepted trades")
    for accepted in result.accepted_trades:
        trade = accepted.trade
        selected = trade.selected_candidate
        closed = _closed_position(trade)
        exit_reason = trade.audit.exit_reason.value if trade.audit.exit_reason is not None else None
        lines.append(
            "- "
            f"{trade.config.entry_date}: {selected.contract.expiration} "
            f"{selected.contract.strike}{selected.contract.option_type.value[0].upper()} "
            f"collateral={_money_text(accepted.required_collateral)} "
            f"exit={closed.exit_date if closed is not None else None} "
            f"reason={exit_reason} "
            f"PnL={_money_text(trade.audit.realized_pnl)}"
        )
    if result.skipped_trades:
        lines.extend(["", "## Skipped For Collateral", ""])
        for skip in result.skipped_trades:
            lines.append(
                "- "
                f"{skip.entry_date}: {skip.contract.expiration} "
                f"{skip.contract.strike}{skip.contract.option_type.value[0].upper()} "
                f"required={_money_text(skip.required_collateral)} "
                f"available={_money_text(skip.available_cash)}"
            )
    if result.failures:
        lines.extend(["", "## Failures", ""])
        for failure in result.failures:
            lines.append(f"- {failure.entry_date}: {failure.error}")
    lines.extend(["", "## Portfolio Snapshots", ""])
    for snapshot in result.snapshots:
        lines.append(
            "- "
            f"{snapshot.observed_date}: equity={_money_text(snapshot.equity)} "
            f"cash={_money_text(snapshot.cash_balance)} "
            f"reserved={_money_text(snapshot.reserved_collateral)} "
            f"open={snapshot.open_positions}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _accepted_payload(accepted: PortfolioAcceptedTrade) -> dict[str, str | int | None]:
    trade = accepted.trade
    selected = trade.selected_candidate
    closed = _closed_position(trade)
    return {
        "entry_date": trade.config.entry_date.isoformat(),
        "expiration": selected.contract.expiration.isoformat(),
        "strike": str(selected.contract.strike),
        "option_type": selected.contract.option_type.value,
        "dte": selected.dte,
        "delta": _ratio_text(selected.delta),
        "entry_price": _money_text(trade.audit.entry_price),
        "required_collateral": _money_text(accepted.required_collateral),
        "exit_date": closed.exit_date.isoformat() if closed is not None else None,
        "exit_reason": trade.audit.exit_reason.value
        if trade.audit.exit_reason is not None
        else None,
        "exit_price": _money_text(trade.audit.exit_price),
        "realized_pnl": _money_text(trade.audit.realized_pnl),
    }


def _skip_payload(skip: PortfolioTradeSkip) -> dict[str, str]:
    return {
        "entry_date": skip.entry_date.isoformat(),
        "expiration": skip.contract.expiration.isoformat(),
        "strike": str(skip.contract.strike),
        "option_type": skip.contract.option_type.value,
        "required_collateral": str(skip.required_collateral.quantize(Decimal("0.01"))),
        "available_cash": str(skip.available_cash.quantize(Decimal("0.01"))),
    }


def _money_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.01")))


def _ratio_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.0001")))


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _log(config: PortfolioValidationConfig, message: str) -> None:
    if config.verbose:
        print(message, flush=True)
