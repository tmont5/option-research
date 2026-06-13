"""Small-batch trade validation before larger multi-month runs."""

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

MIN_SHARPE_TRADES = 30
WEEKLY_PERIODS_PER_YEAR = Decimal("52")
ZERO = Decimal("0")

SingleTradeRunner = Callable[[SingleTradePipelineConfig], SingleTradePipelineResult]


class BatchValidationConfig(BaseModel):
    """Configuration for a small weekly batch of auditable single trades."""

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
    report_path: Path = Field(default=Path("runs/batch_validation/report.md"))
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
class BatchTradeFailure:
    """One failed entry date in the batch validation run."""

    entry_date: date
    error: str


@dataclass(frozen=True)
class BatchValidationMetrics:
    """Aggregate metrics over completed batch trades."""

    completed_trades: int
    failed_trades: int
    total_realized_pnl: Decimal
    average_realized_pnl: Decimal | None
    win_rate: Decimal | None
    per_trade_sharpe: Decimal | None
    sharpe_note: str | None
    max_drawdown: Decimal
    final_equity: Decimal


@dataclass(frozen=True)
class BatchValidationResult:
    """Result of a small batch validation run."""

    config: BatchValidationConfig
    entry_dates: tuple[date, ...]
    trades: tuple[SingleTradePipelineResult, ...]
    failures: tuple[BatchTradeFailure, ...]
    metrics: BatchValidationMetrics


def run_batch_validation_pipeline(
    config: BatchValidationConfig,
    *,
    trade_runner: SingleTradeRunner | None = None,
) -> BatchValidationResult:
    """Run a small set of weekly single-trade validations."""
    entry_dates = _entry_dates(config)
    runner = trade_runner if trade_runner is not None else _live_trade_runner(config)
    trades: list[SingleTradePipelineResult] = []
    failures: list[BatchTradeFailure] = []
    for index, entry_date in enumerate(entry_dates, start=1):
        _log(config, f"[{index}/{len(entry_dates)}] running {config.symbol} entry {entry_date}")
        trade_config = _single_trade_config(config, entry_date, index)
        try:
            trades.append(runner(trade_config))
        except Exception as error:
            failures.append(BatchTradeFailure(entry_date=entry_date, error=str(error)))
            _log(config, f"  failed: {error}")
    result = BatchValidationResult(
        config=config,
        entry_dates=entry_dates,
        trades=tuple(trades),
        failures=tuple(failures),
        metrics=_metrics(config, trades, failures),
    )
    config.report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(config.report_path, result)
    _log(config, f"wrote report to {config.report_path}")
    return result


def _entry_dates(config: BatchValidationConfig) -> tuple[date, ...]:
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


def _live_trade_runner(config: BatchValidationConfig) -> SingleTradeRunner:
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
    config: BatchValidationConfig,
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
        report_path=config.report_path.parent / f"trade_{index:02d}_{entry_date.isoformat()}.md",
        theta_mdds_host=config.theta_mdds_host,
        theta_mdds_port=config.theta_mdds_port,
        theta_mdds_type=config.theta_mdds_type,
        verbose=config.verbose,
    )


def _metrics(
    config: BatchValidationConfig,
    trades: list[SingleTradePipelineResult],
    failures: list[BatchTradeFailure],
) -> BatchValidationMetrics:
    pnls = [trade.audit.realized_pnl for trade in trades if trade.audit.realized_pnl is not None]
    total_pnl = sum(pnls, ZERO)
    final_equity = config.initial_cash + total_pnl
    return BatchValidationMetrics(
        completed_trades=len(trades),
        failed_trades=len(failures),
        total_realized_pnl=total_pnl,
        average_realized_pnl=(total_pnl / Decimal(len(pnls)) if pnls else None),
        win_rate=_win_rate(pnls),
        per_trade_sharpe=_per_trade_sharpe(pnls, config.initial_cash),
        sharpe_note=_sharpe_note(pnls),
        max_drawdown=_max_drawdown(config.initial_cash, pnls),
        final_equity=final_equity,
    )


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
        f"insufficient sample: {len(pnls)} completed trades; "
        f"need at least {MIN_SHARPE_TRADES} for a rough Sharpe estimate"
    )


def _max_drawdown(initial_cash: Decimal, pnls: list[Decimal]) -> Decimal:
    equity = initial_cash
    peak = initial_cash
    max_drawdown = ZERO
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = equity / peak - Decimal("1")
        max_drawdown = min(max_drawdown, drawdown)
    return max_drawdown


def _write_report(path: Path, result: BatchValidationResult) -> None:
    metrics = result.metrics
    payload = {
        "symbol": result.config.symbol,
        "start_date": result.config.start_date.isoformat(),
        "end_date": result.config.end_date.isoformat()
        if result.config.end_date is not None
        else None,
        "entry_generation": "date_range" if result.config.end_date is not None else "count",
        "requested_trade_count": result.config.trade_count,
        "generated_entry_dates": len(result.entry_dates),
        "spacing_days": result.config.spacing_days,
        "target_dte": result.config.target_dte,
        "target_delta": str(result.config.target_delta),
        "take_profit_pct": _decimal_text(result.config.take_profit_pct),
        "stop_loss_pct": _decimal_text(result.config.stop_loss_pct),
        "completed_trades": metrics.completed_trades,
        "failed_trades": metrics.failed_trades,
        "total_realized_pnl": _money_text(metrics.total_realized_pnl),
        "average_realized_pnl": _money_text(metrics.average_realized_pnl),
        "win_rate": _decimal_text(metrics.win_rate),
        "per_trade_sharpe": _ratio_text(metrics.per_trade_sharpe),
        "sharpe_note": metrics.sharpe_note,
        "max_drawdown": _ratio_text(metrics.max_drawdown),
        "final_equity": _money_text(metrics.final_equity),
        "trades": [_trade_payload(trade) for trade in result.trades],
        "failures": [
            {"entry_date": failure.entry_date.isoformat(), "error": failure.error}
            for failure in result.failures
        ],
    }
    lines = [
        "# Batch Trade Validation",
        "",
        "## JSON Summary",
        "",
        json.dumps(payload, indent=2),
        "",
        "## Trades",
        "",
    ]
    if not result.trades:
        lines.append("- No completed trades")
    for trade in result.trades:
        selected = trade.selected_candidate
        exit_reason = trade.audit.exit_reason.value if trade.audit.exit_reason is not None else None
        lines.append(
            "- "
            f"{trade.config.entry_date}: {selected.contract.expiration} "
            f"{selected.contract.strike}{selected.contract.option_type.value[0].upper()} "
            f"DTE={selected.dte} delta={_ratio_text(selected.delta)} "
            f"entry={_money_text(trade.audit.entry_price)} "
            f"exit={_money_text(trade.audit.exit_price)} "
            f"reason={exit_reason} "
            f"PnL={_money_text(trade.audit.realized_pnl)}"
        )
    if result.failures:
        lines.extend(["", "## Failures", ""])
        for failure in result.failures:
            lines.append(f"- {failure.entry_date}: {failure.error}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _trade_payload(trade: SingleTradePipelineResult) -> dict[str, str | int | None]:
    selected = trade.selected_candidate
    closed = (
        trade.backtest_result.closed_positions[-1]
        if trade.backtest_result.closed_positions
        else None
    )
    return {
        "entry_date": trade.config.entry_date.isoformat(),
        "expiration": selected.contract.expiration.isoformat(),
        "strike": str(selected.contract.strike),
        "option_type": selected.contract.option_type.value,
        "dte": selected.dte,
        "delta": _ratio_text(selected.delta),
        "iv": _ratio_text(selected.implied_volatility),
        "entry_price": _money_text(trade.audit.entry_price),
        "exit_date": closed.exit_date.isoformat() if closed is not None else None,
        "exit_reason": trade.audit.exit_reason.value
        if trade.audit.exit_reason is not None
        else None,
        "exit_price": _money_text(trade.audit.exit_price),
        "realized_pnl": _money_text(trade.audit.realized_pnl),
        "final_equity": _money_text(trade.audit.final_equity),
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


def _log(config: BatchValidationConfig, message: str) -> None:
    if config.verbose:
        print(message, flush=True)
