"""Focused autopsy report for a large losing short-option trade."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from options_quant.data.models import OptionContract, OptionQuote, OptionType, UnderlyingPrice
from options_quant.data.providers import ThetaDataProvider, ThetaDataPythonClient
from options_quant.data.providers.thetadata_options import ThetaDataOptionEndpoints
from options_quant.pipelines.single_trade import (
    SingleTradeOptionEndpoints,
    SingleTradePipelineConfig,
    SingleTradePipelineResult,
    _contract_greeks_range,
    _quote_mark,
    run_single_trade_pipeline,
)

ZERO = Decimal("0")


class LoserAutopsyConfig(BaseModel):
    """Configuration for a focused losing-trade inspection."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(default="SPY", min_length=1)
    entry_date: date = Field(default=date(2025, 2, 21))
    target_dte: int = Field(default=45, ge=0)
    target_delta: Decimal = Field(default=Decimal("-0.10"), ge=Decimal("-1"), le=Decimal("1"))
    option_type: OptionType = Field(default=OptionType.PUT)
    quantity: int = Field(default=1, gt=0)
    initial_cash: Decimal = Field(default=Decimal("100000"), gt=Decimal("0"))
    commission_per_contract: Decimal = Field(default=Decimal("0.65"), ge=Decimal("0"))
    slippage_per_contract: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    expiration_search_window_days: int = Field(default=14, ge=0)
    report_path: Path = Field(default=Path("runs/loser_autopsy/report.md"))
    theta_mdds_host: str | None = Field(default=None, min_length=1)
    theta_mdds_port: str | None = Field(default=None, min_length=1)
    theta_mdds_type: str | None = Field(default=None, min_length=1)
    verbose: bool = Field(default=False)


class LoserAutopsyProvider(Protocol):
    """Market data methods needed after the selected trade is known."""

    def retrieve_option_eod_quotes(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionQuote]:
        """Return EOD option quotes for one contract."""


@dataclass(frozen=True)
class DailyAutopsyRow:
    """One daily mark row in a loser autopsy."""

    observed_date: date
    option_mark: Decimal | None
    underlying_price: Decimal | None
    unrealized_pnl: Decimal | None
    multiple_of_credit: Decimal | None


@dataclass(frozen=True)
class StopTrigger:
    """First date a mark-based stop threshold would have triggered."""

    multiple: Decimal
    trigger_price: Decimal
    observed_date: date | None
    option_mark: Decimal | None
    unrealized_pnl: Decimal | None


@dataclass(frozen=True)
class LoserAutopsyResult:
    """Complete autopsy output for one selected losing trade."""

    config: LoserAutopsyConfig
    trade: SingleTradePipelineResult
    daily_rows: tuple[DailyAutopsyRow, ...]
    stop_triggers: tuple[StopTrigger, ...]
    max_mark_row: DailyAutopsyRow | None


def run_loser_autopsy_pipeline(
    config: LoserAutopsyConfig,
    *,
    endpoints: SingleTradeOptionEndpoints | None = None,
    provider: LoserAutopsyProvider | None = None,
) -> LoserAutopsyResult:
    """Run and explain one selected losing trade."""
    if endpoints is None and provider is None:
        theta_client = _build_thetadata_client(config)
        resolved_endpoints = cast(
            SingleTradeOptionEndpoints,
            ThetaDataOptionEndpoints(client=theta_client),
        )
        resolved_provider = cast(
            LoserAutopsyProvider,
            ThetaDataProvider(ThetaDataPythonClient(client=theta_client)),
        )
    else:
        resolved_endpoints = (
            endpoints
            if endpoints is not None
            else cast(
                SingleTradeOptionEndpoints,
                ThetaDataOptionEndpoints(
                    mdds_host=config.theta_mdds_host,
                    mdds_port=config.theta_mdds_port,
                    mdds_type=config.theta_mdds_type,
                ),
            )
        )
        resolved_provider = (
            provider
            if provider is not None
            else cast(
                LoserAutopsyProvider,
                ThetaDataProvider(
                    ThetaDataPythonClient(
                        mdds_host=config.theta_mdds_host,
                        mdds_port=config.theta_mdds_port,
                        mdds_type=config.theta_mdds_type,
                    )
                ),
            )
        )
    trade = run_single_trade_pipeline(
        _single_trade_config(config),
        endpoints=resolved_endpoints,
        provider=resolved_provider,
    )
    contract = trade.selected_candidate.contract
    quotes = resolved_provider.retrieve_option_eod_quotes(
        contract,
        config.entry_date,
        contract.expiration,
    )
    _, underlying_by_date = _contract_greeks_range(
        _single_trade_config(config),
        resolved_endpoints,
        contract,
        contract.expiration,
    )
    daily_rows = tuple(_daily_rows(config, trade, quotes, underlying_by_date))
    result = LoserAutopsyResult(
        config=config,
        trade=trade,
        daily_rows=daily_rows,
        stop_triggers=tuple(_stop_triggers(trade, daily_rows)),
        max_mark_row=_max_mark_row(daily_rows),
    )
    config.report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(config.report_path, result)
    _log(config, f"wrote report to {config.report_path}")
    return result


def _build_thetadata_client(config: LoserAutopsyConfig) -> Any:
    theta_module = cast(Any, import_module("thetadata"))
    client_kwargs: dict[str, str] = {"dataframe_type": "pandas"}
    if config.theta_mdds_host is not None:
        client_kwargs["mdds_host"] = config.theta_mdds_host
    if config.theta_mdds_port is not None:
        client_kwargs["mdds_port"] = config.theta_mdds_port
    if config.theta_mdds_type is not None:
        client_kwargs["mdds_type"] = config.theta_mdds_type
    return theta_module.ThetaClient(**client_kwargs)


def _single_trade_config(config: LoserAutopsyConfig) -> SingleTradePipelineConfig:
    return SingleTradePipelineConfig(
        symbol=config.symbol,
        entry_date=config.entry_date,
        target_dte=config.target_dte,
        target_delta=config.target_delta,
        option_type=config.option_type,
        quantity=config.quantity,
        initial_cash=config.initial_cash,
        commission_per_contract=config.commission_per_contract,
        slippage_per_contract=config.slippage_per_contract,
        expiration_search_window_days=config.expiration_search_window_days,
        report_path=config.report_path.parent / "selected_trade.md",
        theta_mdds_host=config.theta_mdds_host,
        theta_mdds_port=config.theta_mdds_port,
        theta_mdds_type=config.theta_mdds_type,
        verbose=config.verbose,
    )


def _daily_rows(
    config: LoserAutopsyConfig,
    trade: SingleTradePipelineResult,
    quotes: list[OptionQuote],
    underlying_by_date: dict[date, UnderlyingPrice],
) -> list[DailyAutopsyRow]:
    quote_by_date = {quote.timestamp.date(): quote for quote in quotes}
    dates = sorted(set(quote_by_date) | set(underlying_by_date))
    rows: list[DailyAutopsyRow] = []
    for observed_date in dates:
        quote = quote_by_date.get(observed_date)
        mark = _quote_mark(quote) if quote is not None else None
        underlying = underlying_by_date.get(observed_date)
        rows.append(
            DailyAutopsyRow(
                observed_date=observed_date,
                option_mark=mark,
                underlying_price=underlying.price if underlying is not None else None,
                unrealized_pnl=_unrealized_pnl(config, trade, mark),
                multiple_of_credit=(
                    mark / trade.audit.entry_fill_price
                    if mark is not None and trade.audit.entry_fill_price > ZERO
                    else None
                ),
            )
        )
    return rows


def _unrealized_pnl(
    config: LoserAutopsyConfig,
    trade: SingleTradePipelineResult,
    option_mark: Decimal | None,
) -> Decimal | None:
    if option_mark is None:
        return None
    mark_debit = (
        option_mark
        * Decimal(config.quantity)
        * Decimal(trade.selected_candidate.contract.multiplier)
    )
    return trade.audit.entry_net_cash_flow - mark_debit


def _stop_triggers(
    trade: SingleTradePipelineResult,
    daily_rows: tuple[DailyAutopsyRow, ...],
) -> list[StopTrigger]:
    triggers: list[StopTrigger] = []
    for multiple in (Decimal("2"), Decimal("3"), Decimal("5")):
        trigger_price = trade.audit.entry_fill_price * multiple
        hit = next(
            (
                row
                for row in daily_rows
                if row.option_mark is not None and row.option_mark >= trigger_price
            ),
            None,
        )
        triggers.append(
            StopTrigger(
                multiple=multiple,
                trigger_price=trigger_price,
                observed_date=hit.observed_date if hit is not None else None,
                option_mark=hit.option_mark if hit is not None else None,
                unrealized_pnl=hit.unrealized_pnl if hit is not None else None,
            )
        )
    return triggers


def _max_mark_row(daily_rows: tuple[DailyAutopsyRow, ...]) -> DailyAutopsyRow | None:
    rows_with_marks = [row for row in daily_rows if row.option_mark is not None]
    if not rows_with_marks:
        return None
    return max(rows_with_marks, key=lambda row: row.option_mark or ZERO)


def _write_report(path: Path, result: LoserAutopsyResult) -> None:
    trade = result.trade
    selected = trade.selected_candidate
    max_row = result.max_mark_row
    payload = {
        "entry_date": result.config.entry_date.isoformat(),
        "selected_contract": {
            "symbol": selected.contract.underlying_symbol,
            "expiration": selected.contract.expiration.isoformat(),
            "strike": str(selected.contract.strike),
            "option_type": selected.contract.option_type.value,
        },
        "selected_dte": selected.dte,
        "selected_delta": _ratio_text(selected.delta),
        "entry_price": _money_text(trade.audit.entry_price),
        "entry_net_cash_flow": _money_text(trade.audit.entry_net_cash_flow),
        "expiration_exit_price": _money_text(trade.audit.exit_price),
        "realized_pnl": _money_text(trade.audit.realized_pnl),
        "max_mark": _row_payload(max_row) if max_row is not None else None,
        "stop_triggers": [_stop_payload(trigger) for trigger in result.stop_triggers],
        "daily_rows": [_row_payload(row) for row in result.daily_rows],
    }
    lines = [
        "# Loser Autopsy",
        "",
        "## JSON Summary",
        "",
        json.dumps(payload, indent=2),
        "",
        "## Stop Checks",
        "",
    ]
    for trigger in result.stop_triggers:
        if trigger.observed_date is None:
            lines.append(
                f"- {trigger.multiple}x credit stop ({_money_text(trigger.trigger_price)}): not hit"
            )
        else:
            lines.append(
                f"- {trigger.multiple}x credit stop ({_money_text(trigger.trigger_price)}): "
                f"hit {trigger.observed_date} at mark {_money_text(trigger.option_mark)}, "
                f"unrealized PnL {_money_text(trigger.unrealized_pnl)}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _row_payload(row: DailyAutopsyRow | None) -> dict[str, str | None] | None:
    if row is None:
        return None
    return {
        "date": row.observed_date.isoformat(),
        "option_mark": _money_text(row.option_mark),
        "underlying_price": _money_text(row.underlying_price),
        "unrealized_pnl": _money_text(row.unrealized_pnl),
        "multiple_of_credit": _ratio_text(row.multiple_of_credit),
    }


def _stop_payload(trigger: StopTrigger) -> dict[str, str | None]:
    return {
        "multiple": str(trigger.multiple),
        "trigger_price": _money_text(trigger.trigger_price),
        "observed_date": trigger.observed_date.isoformat()
        if trigger.observed_date is not None
        else None,
        "option_mark": _money_text(trigger.option_mark),
        "unrealized_pnl": _money_text(trigger.unrealized_pnl),
    }


def _money_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.01")))


def _ratio_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.0001")))


def _log(config: LoserAutopsyConfig, message: str) -> None:
    if config.verbose:
        print(message, flush=True)
