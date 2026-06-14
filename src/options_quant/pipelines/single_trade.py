"""Single-trade live-data pipeline for dollar-level inspection."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from options_quant.backtest import (
    BacktestConfig,
    BacktestEngine,
    BacktestMarketEvent,
    BacktestOrderEvent,
    BacktestOrderType,
    BacktestPosition,
    BacktestResult,
    EarlyExitRule,
    ExitReason,
)
from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionQuote,
    OptionType,
    TradeSide,
    UnderlyingPrice,
)
from options_quant.data.providers import ThetaDataProvider, ThetaDataPythonClient
from options_quant.data.providers.thetadata_options import RawRow, ThetaDataOptionEndpoints
from options_quant.strategies.selection import (
    ContractSelectionEngine,
    OptionSelectionCandidate,
    OptionSelectionQuery,
)

MARKET_CLOSE = time(16, 0)


class SingleTradePipelineConfig(BaseModel):
    """Configuration for one auditable live-data trade."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(default="SPY", min_length=1)
    entry_date: date = Field(default=date(2025, 1, 3))
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
    report_path: Path = Field(default=Path("runs/single_trade/report.md"))
    theta_mdds_host: str | None = Field(default=None, min_length=1)
    theta_mdds_port: str | None = Field(default=None, min_length=1)
    theta_mdds_type: str | None = Field(default=None, min_length=1)
    verbose: bool = Field(default=False)


class SingleTradeOptionEndpoints(Protocol):
    """ThetaData option endpoint methods used by the single-trade pipeline."""

    def list_expirations(self, *, symbol: str) -> list[RawRow]:
        """Return expiration rows for one option root."""

    def list_strikes(self, *, symbol: str, expiration: date | str) -> list[RawRow]:
        """Return strike rows for one option expiration."""

    def history_greeks_first_order(self, **params: Any) -> list[RawRow]:
        """Return first-order Greek rows."""


class SingleTradeMarketDataProvider(Protocol):
    """App-facing provider methods used for selected-contract EOD quotes."""

    def retrieve_option_eod_quotes(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionQuote]:
        """Return EOD option quotes for one contract."""


@dataclass(frozen=True)
class TradeAudit:
    """Dollar-level explanation of the selected trade."""

    entry_price: Decimal
    entry_fill_price: Decimal
    entry_gross_credit: Decimal
    entry_commission: Decimal
    entry_net_cash_flow: Decimal
    exit_price: Decimal | None
    exit_fill_price: Decimal | None
    exit_gross_debit: Decimal | None
    exit_commission: Decimal | None
    realized_pnl: Decimal | None
    final_equity: Decimal
    exit_reason: ExitReason | None


@dataclass(frozen=True)
class SingleTradePipelineResult:
    """Summary of one single-trade inspection run."""

    config: SingleTradePipelineConfig
    expiration_candidates: tuple[date, ...]
    chain_contracts: int
    greek_rows: int
    quote_rows: int
    underlying_rows: int
    selected_candidate: OptionSelectionCandidate
    entry_quote: OptionQuote
    underlying_prices: tuple[UnderlyingPrice, ...]
    backtest_result: BacktestResult
    audit: TradeAudit


def run_single_trade_pipeline(
    config: SingleTradePipelineConfig,
    *,
    endpoints: SingleTradeOptionEndpoints | None = None,
    provider: SingleTradeMarketDataProvider | None = None,
) -> SingleTradePipelineResult:
    """Select and run one short-option trade through expiration."""
    if endpoints is None and provider is None:
        theta_client = _build_thetadata_client(config)
        resolved_endpoints: SingleTradeOptionEndpoints = cast(
            SingleTradeOptionEndpoints,
            ThetaDataOptionEndpoints(client=theta_client),
        )
        resolved_provider: SingleTradeMarketDataProvider = ThetaDataProvider(
            ThetaDataPythonClient(client=theta_client)
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
            else ThetaDataProvider(
                ThetaDataPythonClient(
                    mdds_host=config.theta_mdds_host,
                    mdds_port=config.theta_mdds_port,
                    mdds_type=config.theta_mdds_type,
                )
            )
        )

    expiration_candidates = _expiration_candidates(config, resolved_endpoints)
    expiration: date | None = None
    contracts: list[OptionContract] = []
    greeks: list[OptionGreek] = []
    underlying_by_date: dict[date, UnderlyingPrice] = {}
    for candidate_expiration in expiration_candidates:
        candidate_contracts = _contracts_for_expiration(
            config,
            resolved_endpoints,
            candidate_expiration,
        )
        candidate_greeks, candidate_underlying = _entry_greeks(
            config,
            resolved_endpoints,
            candidate_contracts,
        )
        if candidate_greeks:
            expiration = candidate_expiration
            contracts = candidate_contracts
            greeks = candidate_greeks
            underlying_by_date = candidate_underlying
            break
        _log(config, f"skipping {candidate_expiration}: no entry Greeks returned")
    if expiration is None:
        raise ValueError("no candidate expiration returned entry Greeks")
    selected = _select_contract(config, expiration, contracts, greeks, underlying_by_date)
    end_date = selected.contract.expiration
    quotes = resolved_provider.retrieve_option_eod_quotes(
        selected.contract,
        config.entry_date,
        end_date,
    )
    selected_greeks, selected_underlying = _contract_greeks_range(
        config,
        resolved_endpoints,
        selected.contract,
        end_date,
    )
    underlying_by_date.update(selected_underlying)
    entry_quote = _quote_for_date(quotes, config.entry_date)
    backtest_result = _run_backtest(config, selected.contract, quotes, underlying_by_date)
    result = SingleTradePipelineResult(
        config=config,
        expiration_candidates=tuple(expiration_candidates),
        chain_contracts=len(contracts),
        greek_rows=len(greeks) + len(selected_greeks),
        quote_rows=len(quotes),
        underlying_rows=len(underlying_by_date),
        selected_candidate=selected,
        entry_quote=entry_quote,
        underlying_prices=tuple(
            sorted(underlying_by_date.values(), key=lambda underlying: underlying.timestamp)
        ),
        backtest_result=backtest_result,
        audit=_audit_trade(config, selected.contract, entry_quote, backtest_result),
    )
    config.report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(config.report_path, result)
    _log(config, f"wrote report to {config.report_path}")
    return result


def _build_thetadata_client(config: SingleTradePipelineConfig) -> Any:
    theta_module = cast(Any, import_module("thetadata"))
    client_kwargs: dict[str, str] = {"dataframe_type": "pandas"}
    if config.theta_mdds_host is not None:
        client_kwargs["mdds_host"] = config.theta_mdds_host
    if config.theta_mdds_port is not None:
        client_kwargs["mdds_port"] = config.theta_mdds_port
    if config.theta_mdds_type is not None:
        client_kwargs["mdds_type"] = config.theta_mdds_type
    return theta_module.ThetaClient(**client_kwargs)


def _expiration_candidates(
    config: SingleTradePipelineConfig,
    endpoints: SingleTradeOptionEndpoints,
) -> list[date]:
    target_expiration = config.entry_date + timedelta(days=config.target_dte)
    min_expiration = target_expiration - timedelta(days=config.expiration_search_window_days)
    max_expiration = target_expiration + timedelta(days=config.expiration_search_window_days)
    _log(config, f"listing expirations for {config.symbol}")
    expirations = sorted(
        {
            _row_date(row, "expiration", "exp", "expiration_date")
            for row in endpoints.list_expirations(symbol=config.symbol)
        }
    )
    candidates = [
        expiration for expiration in expirations if min_expiration <= expiration <= max_expiration
    ]
    if not candidates:
        raise ValueError("no expirations found near target DTE")
    return sorted(
        candidates, key=lambda expiration: (abs((expiration - target_expiration).days), expiration)
    )


def _contracts_for_expiration(
    config: SingleTradePipelineConfig,
    endpoints: SingleTradeOptionEndpoints,
    expiration: date,
) -> list[OptionContract]:
    _log(config, f"listing strikes for {config.symbol} {expiration}")
    contracts = [
        OptionContract(
            underlying_symbol=config.symbol,
            expiration=expiration,
            strike=_row_decimal(row, "strike"),
            option_type=config.option_type,
        )
        for row in endpoints.list_strikes(symbol=config.symbol, expiration=expiration)
    ]
    contracts.sort(key=lambda contract: contract.strike)
    if not contracts:
        raise ValueError("no strikes found for selected expiration")
    return contracts


def _entry_greeks(
    config: SingleTradePipelineConfig,
    endpoints: SingleTradeOptionEndpoints,
    contracts: list[OptionContract],
) -> tuple[list[OptionGreek], dict[date, UnderlyingPrice]]:
    if not contracts:
        return [], {}
    expiration = contracts[0].expiration
    rows = _raw_entry_greek_rows_for_expiration(config, endpoints, expiration)
    contract_by_key = {
        _contract_key(contract.expiration, contract.strike, contract.option_type): contract
        for contract in contracts
    }
    greeks: list[OptionGreek] = []
    underlying_by_date: dict[date, UnderlyingPrice] = {}
    for row in rows:
        strike = _optional_decimal(row, "strike")
        if strike is None:
            continue
        row_expiration = (
            _optional_row_date(row, "expiration", "exp", "expiration_date") or expiration
        )
        option_type = _optional_option_type(row) or config.option_type
        contract = contract_by_key.get(_contract_key(row_expiration, strike, option_type))
        if contract is None:
            continue
        contract_greeks, contract_underlying = _greek_from_row(config, contract, row)
        greeks.append(contract_greeks)
        underlying_by_date.update(contract_underlying)
    return greeks, underlying_by_date


def _raw_entry_greek_rows_for_expiration(
    config: SingleTradePipelineConfig,
    endpoints: SingleTradeOptionEndpoints,
    expiration: date,
) -> list[RawRow]:
    _log(config, f"fetching entry Greeks for {config.symbol} {expiration}")
    try:
        return endpoints.history_greeks_first_order(
            symbol=config.symbol,
            expiration=expiration,
            strike="*",
            right=_thetadata_right(config.option_type),
            interval="1m",
            start_date=config.entry_date,
            end_date=config.entry_date,
            start_time=MARKET_CLOSE.isoformat(),
            end_time=MARKET_CLOSE.isoformat(),
        )
    except Exception as error:
        if _is_no_data_error(error):
            return []
        raise


def _contract_greeks_range(
    config: SingleTradePipelineConfig,
    endpoints: SingleTradeOptionEndpoints,
    contract: OptionContract,
    end_date: date,
) -> tuple[list[OptionGreek], dict[date, UnderlyingPrice]]:
    return _greeks_for_contract(config, endpoints, contract, config.entry_date, end_date)


def _greeks_for_contract(
    config: SingleTradePipelineConfig,
    endpoints: SingleTradeOptionEndpoints,
    contract: OptionContract,
    start_date: date,
    end_date: date,
) -> tuple[list[OptionGreek], dict[date, UnderlyingPrice]]:
    rows: list[RawRow] = []
    for chunk_start, chunk_end in _date_chunks(start_date, end_date, days=30):
        rows.extend(
            _raw_greek_rows_for_contract(
                contract,
                endpoints,
                chunk_start,
                chunk_end,
            )
        )
    greeks: list[OptionGreek] = []
    underlying_by_date: dict[date, UnderlyingPrice] = {}
    for row in rows:
        greek, contract_underlying = _greek_from_row(config, contract, row)
        greeks.append(greek)
        underlying_by_date.update(contract_underlying)
    return greeks, underlying_by_date


def _greek_from_row(
    config: SingleTradePipelineConfig,
    contract: OptionContract,
    row: RawRow,
) -> tuple[OptionGreek, dict[date, UnderlyingPrice]]:
    timestamp = _row_timestamp(row, config.entry_date)
    greek = OptionGreek(
        contract=contract,
        timestamp=timestamp,
        delta=_optional_decimal(row, "delta"),
        gamma=None,
        theta=_optional_decimal(row, "theta"),
        vega=_optional_decimal(row, "vega"),
        rho=_optional_decimal(row, "rho"),
        implied_volatility=_positive_optional_decimal(
            row,
            "implied_volatility",
            "implied_vol",
            "iv",
        ),
    )
    underlying: dict[date, UnderlyingPrice] = {}
    underlying_price = _optional_decimal(row, "underlying_price")
    if underlying_price is not None:
        underlying[timestamp.date()] = UnderlyingPrice(
            symbol=config.symbol,
            timestamp=timestamp,
            price=underlying_price,
        )
    return greek, underlying


def _raw_greek_rows_for_contract(
    contract: OptionContract,
    endpoints: SingleTradeOptionEndpoints,
    start_date: date,
    end_date: date,
) -> list[RawRow]:
    try:
        return endpoints.history_greeks_first_order(
            symbol=contract.underlying_symbol,
            expiration=contract.expiration,
            strike=_strike_text(contract.strike),
            right=_thetadata_right(contract.option_type),
            interval="1m",
            start_date=start_date,
            end_date=end_date,
            start_time=MARKET_CLOSE.isoformat(),
            end_time=MARKET_CLOSE.isoformat(),
        )
    except Exception as error:
        if _is_no_data_error(error):
            return []
        raise


def _date_chunks(start_date: date, end_date: date, *, days: int) -> Iterable[tuple[date, date]]:
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=days - 1), end_date)
        yield chunk_start, chunk_end
        chunk_start = chunk_end + timedelta(days=1)


def _select_contract(
    config: SingleTradePipelineConfig,
    expiration: date,
    contracts: list[OptionContract],
    greeks: list[OptionGreek],
    underlying_by_date: dict[date, UnderlyingPrice],
) -> OptionSelectionCandidate:
    underlying = underlying_by_date.get(config.entry_date)
    if underlying is None:
        raise ValueError("missing entry-date underlying price")
    chain = OptionChain(
        underlying_symbol=config.symbol,
        timestamp=datetime.combine(config.entry_date, MARKET_CLOSE, tzinfo=UTC),
        contracts=tuple(contracts),
    )
    selector = ContractSelectionEngine(
        chain,
        underlying.price,
        as_of_date=config.entry_date,
        greeks=greeks,
    )
    selected = selector.best(
        OptionSelectionQuery(
            option_type=config.option_type,
            target_dte=(expiration - config.entry_date).days,
            target_delta=config.target_delta,
        )
    )
    if selected is None:
        raise ValueError("contract selector found no candidate")
    return selected


def _run_backtest(
    config: SingleTradePipelineConfig,
    contract: OptionContract,
    quotes: list[OptionQuote],
    underlying_by_date: dict[date, UnderlyingPrice],
) -> BacktestResult:
    quotes_by_date = {quote.timestamp.date(): quote for quote in quotes}
    market_events: list[BacktestMarketEvent] = []
    for observed_date in sorted(underlying_by_date):
        if observed_date < config.entry_date or observed_date > contract.expiration:
            continue
        option_marks = {}
        if observed_date < contract.expiration:
            quote = quotes_by_date.get(observed_date)
            if quote is None:
                continue
            option_marks[contract] = _quote_mark(quote)
        market_events.append(
            BacktestMarketEvent(
                date=observed_date,
                option_marks=option_marks,
                underlying_prices={config.symbol: underlying_by_date[observed_date].price},
            )
        )
    if not market_events or market_events[0].date != config.entry_date:
        raise ValueError("entry-date market event is unavailable")
    if market_events[-1].date != contract.expiration:
        raise ValueError("expiration-date market event is unavailable")
    entry_quote = _quote_for_date(quotes, config.entry_date)
    orders_by_date = {
        config.entry_date: [
            BacktestOrderEvent(
                contract=contract,
                side=TradeSide.SELL,
                quantity=config.quantity,
                price=_quote_mark(entry_quote),
                event_type=BacktestOrderType.OPEN,
            )
        ]
    }
    engine = BacktestEngine(
        BacktestConfig(
            initial_cash=config.initial_cash,
            commission_per_contract=config.commission_per_contract,
            slippage_per_contract=config.slippage_per_contract,
        ),
        early_exit_rules=_early_exit_rules(config),
    )
    return engine.run(market_events, orders_by_date)


def _early_exit_rules(config: SingleTradePipelineConfig) -> list[EarlyExitRule]:
    if config.take_profit_pct is None and config.stop_loss_pct is None:
        return []

    def risk_exit(
        position: BacktestPosition,
        market_event: BacktestMarketEvent,
    ) -> BacktestOrderEvent | None:
        mark = market_event.option_marks.get(position.contract)
        if mark is None:
            return None
        take_profit_price = (
            position.entry_fill_price * (Decimal("1") - config.take_profit_pct)
            if config.take_profit_pct is not None
            else None
        )
        stop_loss_price = (
            position.entry_fill_price * (Decimal("1") + config.stop_loss_pct)
            if config.stop_loss_pct is not None
            else None
        )
        if (
            take_profit_price is not None
            and mark <= take_profit_price
            or stop_loss_price is not None
            and mark >= stop_loss_price
        ):
            return BacktestOrderEvent(
                contract=position.contract,
                side=TradeSide.BUY,
                quantity=position.absolute_quantity,
                price=mark,
                event_type=BacktestOrderType.CLOSE,
                position_id=position.position_id,
            )
        return None

    return [risk_exit]


def _audit_trade(
    config: SingleTradePipelineConfig,
    contract: OptionContract,
    entry_quote: OptionQuote,
    backtest_result: BacktestResult,
) -> TradeAudit:
    entry_price = _quote_mark(entry_quote)
    entry_fill_price = max(Decimal("0"), entry_price - config.slippage_per_contract)
    entry_gross_credit = entry_fill_price * Decimal(config.quantity) * Decimal(contract.multiplier)
    entry_commission = config.commission_per_contract * Decimal(config.quantity)
    entry_net_cash_flow = entry_gross_credit - entry_commission
    final_snapshot = backtest_result.snapshots[-1]
    if not backtest_result.closed_positions:
        return TradeAudit(
            entry_price=entry_price,
            entry_fill_price=entry_fill_price,
            entry_gross_credit=entry_gross_credit,
            entry_commission=entry_commission,
            entry_net_cash_flow=entry_net_cash_flow,
            exit_price=None,
            exit_fill_price=None,
            exit_gross_debit=None,
            exit_commission=None,
            realized_pnl=None,
            final_equity=final_snapshot.equity,
            exit_reason=None,
        )
    closed = backtest_result.closed_positions[-1]
    exit_gross_debit = (
        closed.exit_fill_price * Decimal(config.quantity) * Decimal(contract.multiplier)
    )
    exit_commission = config.commission_per_contract * Decimal(config.quantity)
    return TradeAudit(
        entry_price=entry_price,
        entry_fill_price=entry_fill_price,
        entry_gross_credit=entry_gross_credit,
        entry_commission=entry_commission,
        entry_net_cash_flow=entry_net_cash_flow,
        exit_price=closed.exit_fill_price,
        exit_fill_price=closed.exit_fill_price,
        exit_gross_debit=exit_gross_debit,
        exit_commission=exit_commission,
        realized_pnl=closed.realized_pnl,
        final_equity=final_snapshot.equity,
        exit_reason=closed.exit_reason,
    )


def _write_report(path: Path, result: SingleTradePipelineResult) -> None:
    selected = result.selected_candidate
    audit = result.audit
    closed = (
        result.backtest_result.closed_positions[-1]
        if result.backtest_result.closed_positions
        else None
    )
    payload = {
        "symbol": result.config.symbol,
        "entry_date": result.config.entry_date.isoformat(),
        "target_dte": result.config.target_dte,
        "target_delta": str(result.config.target_delta),
        "take_profit_pct": _decimal_text(result.config.take_profit_pct),
        "stop_loss_pct": _decimal_text(result.config.stop_loss_pct),
        "expiration_candidates": [
            expiration.isoformat() for expiration in result.expiration_candidates
        ],
        "selected_contract": _contract_payload(selected.contract),
        "selected_dte": selected.dte,
        "selected_delta": _decimal_text(selected.delta),
        "selected_iv": _decimal_text(selected.implied_volatility),
        "entry_quote": {
            "bid": str(result.entry_quote.bid),
            "ask": str(result.entry_quote.ask),
            "mark": _decimal_text(result.entry_quote.mark),
            "used_price": str(audit.entry_price),
            "volume": result.entry_quote.volume,
            "open_interest": result.entry_quote.open_interest,
        },
        "exit": {
            "reason": audit.exit_reason.value if audit.exit_reason is not None else None,
            "date": closed.exit_date.isoformat() if closed is not None else None,
            "price": _decimal_text(audit.exit_price),
        },
        "dollar_audit": {
            "entry_gross_credit": str(audit.entry_gross_credit),
            "entry_commission": str(audit.entry_commission),
            "entry_net_cash_flow": str(audit.entry_net_cash_flow),
            "exit_gross_debit": _decimal_text(audit.exit_gross_debit),
            "exit_commission": _decimal_text(audit.exit_commission),
            "realized_pnl": _decimal_text(audit.realized_pnl),
            "final_equity": str(audit.final_equity),
        },
    }
    formula = "PnL = entry gross credit - entry commission - exit gross debit - exit commission"
    lines = [
        "# Single-Trade Inspection",
        "",
        "## JSON Summary",
        "",
        json.dumps(payload, indent=2),
        "",
        "## Dollar Check",
        "",
        f"- Formula: {formula}",
        (
            f"- Entry: {audit.entry_gross_credit} - {audit.entry_commission} = "
            f"{audit.entry_net_cash_flow}"
        ),
    ]
    if audit.exit_gross_debit is not None and audit.exit_commission is not None:
        lines.append(
            "- Exit/PnL: "
            f"{audit.entry_gross_credit} - {audit.entry_commission} - "
            f"{audit.exit_gross_debit} - {audit.exit_commission} = {audit.realized_pnl}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _quote_for_date(quotes: list[OptionQuote], target_date: date) -> OptionQuote:
    matches = [quote for quote in quotes if quote.timestamp.date() == target_date]
    if not matches:
        raise ValueError(f"missing option quote for {target_date}")
    return sorted(matches, key=lambda quote: quote.timestamp)[-1]


def _is_no_data_error(error: Exception) -> bool:
    return error.__class__.__name__ == "NoDataFoundError" or "No data found" in str(error)


def _quote_mark(quote: OptionQuote) -> Decimal:
    if quote.mark is not None:
        return quote.mark
    return (quote.bid + quote.ask) / Decimal("2")


def _row_date(row: RawRow, *keys: str) -> date:
    value = _first_present(row, keys)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if value is None:
        raise ValueError(f"missing date field: {keys[0]}")
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text)


def _optional_row_date(row: RawRow, *keys: str) -> date | None:
    value = _first_present(row, keys)
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text)


def _row_timestamp(row: RawRow, fallback_date: date) -> datetime:
    timestamp = _first_present(row, ("timestamp", "datetime"))
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=UTC)
        return timestamp
    if timestamp is not None:
        parsed = datetime.fromisoformat(str(timestamp))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    row_date = (
        _row_date(row, "date") if _first_present(row, ("date",)) is not None else fallback_date
    )
    return datetime.combine(row_date, MARKET_CLOSE, tzinfo=UTC)


def _row_decimal(row: RawRow, key: str) -> Decimal:
    value = row.get(key)
    if value is None:
        raise ValueError(f"missing decimal field: {key}")
    return Decimal(str(value))


def _optional_decimal(row: RawRow, *keys: str) -> Decimal | None:
    value = _first_present(row, keys)
    if value is None:
        return None
    return Decimal(str(value))


def _positive_optional_decimal(row: RawRow, *keys: str) -> Decimal | None:
    value = _optional_decimal(row, *keys)
    if value is None or value <= Decimal("0"):
        return None
    return value


def _optional_option_type(row: RawRow) -> OptionType | None:
    value = _first_present(row, ("option_type", "right", "cp"))
    if value is None:
        return None
    normalized = str(value).lower()
    if normalized in {"c", "call"}:
        return OptionType.CALL
    if normalized in {"p", "put"}:
        return OptionType.PUT
    raise ValueError(f"unsupported option type: {value}")


def _first_present(row: RawRow, keys: Iterable[str]) -> Any | None:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _thetadata_right(option_type: OptionType) -> str:
    if option_type is OptionType.CALL:
        return "C"
    return "P"


def _strike_text(strike: Decimal) -> str:
    return format(strike.normalize(), "f")


def _contract_key(
    expiration: date,
    strike: Decimal,
    option_type: OptionType,
) -> tuple[date, Decimal, OptionType]:
    return expiration, strike, option_type


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _contract_payload(contract: OptionContract) -> dict[str, str | int]:
    return {
        "underlying_symbol": contract.underlying_symbol,
        "expiration": contract.expiration.isoformat(),
        "strike": str(contract.strike),
        "option_type": contract.option_type.value,
        "multiplier": contract.multiplier,
    }


def _log(config: SingleTradePipelineConfig, message: str) -> None:
    if config.verbose:
        print(message, flush=True)
