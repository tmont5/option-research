"""One-week live-data pipeline for manual end-to-end inspection."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from options_quant.backtest import (
    BacktestConfig,
    BacktestEngine,
    BacktestMarketEvent,
    BacktestOrderEvent,
    BacktestOrderType,
    BacktestResult,
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
from options_quant.data.storage import DuckDBStorage
from options_quant.strategies.selection import (
    ContractSelectionEngine,
    OptionSelectionCandidate,
    OptionSelectionQuery,
)

MARKET_CLOSE = time(16, 0)


class OneWeekPipelineConfig(BaseModel):
    """Configuration for a narrow live-data pipeline run."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(default="ANET", min_length=1)
    start_date: date = Field(default=date(2026, 6, 8))
    end_date: date = Field(default=date(2026, 6, 12))
    min_dte: int = Field(default=30, ge=0)
    max_dte: int = Field(default=45, ge=0)
    option_type: OptionType = Field(default=OptionType.PUT)
    target_delta: Decimal = Field(default=Decimal("-0.30"), ge=Decimal("-1"), le=Decimal("1"))
    quantity: int = Field(default=1, gt=0)
    initial_cash: Decimal = Field(default=Decimal("100000"), gt=Decimal("0"))
    commission_per_contract: Decimal = Field(default=Decimal("0.65"), ge=Decimal("0"))
    slippage_per_contract: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    database_path: Path = Field(default=Path("runs/one_week_pipeline/pipeline.duckdb"))
    report_path: Path = Field(default=Path("runs/one_week_pipeline/report.md"))
    max_contracts: int | None = Field(default=None, gt=0)
    min_strike: Decimal | None = Field(default=None, gt=Decimal("0"))
    max_strike: Decimal | None = Field(default=None, gt=Decimal("0"))
    verbose: bool = Field(default=False)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        """Validate the configured date and DTE ranges."""
        if self.start_date > self.end_date:
            raise ValueError("start_date must be less than or equal to end_date")
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must be less than or equal to max_dte")
        if (
            self.min_strike is not None
            and self.max_strike is not None
            and self.min_strike > self.max_strike
        ):
            raise ValueError("min_strike must be less than or equal to max_strike")
        return self


class OneWeekOptionEndpoints(Protocol):
    """ThetaData option endpoint methods used for discovery and close Greeks."""

    def list_expirations(self, *, symbol: str) -> list[RawRow]:
        """Return expiration rows for one option root."""

    def list_strikes(self, *, symbol: str, expiration: date | str) -> list[RawRow]:
        """Return strike rows for one option expiration."""

    def history_greeks_first_order(self, **params: Any) -> list[RawRow]:
        """Return first-order Greek rows."""


class OneWeekMarketDataProvider(Protocol):
    """App-facing provider methods used for EOD option quotes."""

    def retrieve_option_eod_quotes(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionQuote]:
        """Return EOD option quotes for one contract."""


@dataclass(frozen=True)
class OneWeekPipelineResult:
    """Summary of one live-data pipeline run."""

    config: OneWeekPipelineConfig
    chain_contracts: int
    quote_rows: int
    greek_rows: int
    underlying_rows: int
    selected_candidate: OptionSelectionCandidate
    backtest_result: BacktestResult


def run_one_week_pipeline(
    config: OneWeekPipelineConfig,
    *,
    endpoints: OneWeekOptionEndpoints | None = None,
    provider: OneWeekMarketDataProvider | None = None,
) -> OneWeekPipelineResult:
    """Run ThetaData through storage, contract selection, and the backtest engine."""
    resolved_endpoints: OneWeekOptionEndpoints = (
        endpoints
        if endpoints is not None
        else cast(OneWeekOptionEndpoints, ThetaDataOptionEndpoints())
    )
    resolved_provider: OneWeekMarketDataProvider = (
        provider if provider is not None else ThetaDataProvider(ThetaDataPythonClient())
    )

    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.report_path.parent.mkdir(parents=True, exist_ok=True)

    contracts = _discover_contracts(config, resolved_endpoints)
    _log(config, f"discovered {len(contracts)} candidate contracts")
    chain = OptionChain(
        underlying_symbol=config.symbol,
        timestamp=datetime.combine(config.start_date, time.min, tzinfo=UTC),
        contracts=tuple(contracts),
    )

    quotes: list[OptionQuote] = []
    greeks: list[OptionGreek] = []
    underlying_prices_by_date: dict[date, UnderlyingPrice] = {}
    for index, contract in enumerate(contracts, start=1):
        _log(
            config,
            (
                f"[{index}/{len(contracts)}] fetching {contract.underlying_symbol} "
                f"{contract.expiration} {contract.strike} {contract.option_type.value}"
            ),
        )
        quotes.extend(
            resolved_provider.retrieve_option_eod_quotes(
                contract,
                config.start_date,
                config.end_date,
            )
        )
        contract_greeks, contract_underlying = _retrieve_close_greeks(
            config,
            resolved_endpoints,
            contract,
        )
        greeks.extend(contract_greeks)
        underlying_prices_by_date.update(contract_underlying)
        _log(
            config,
            f"  rows: quotes={len(quotes)} greeks={len(greeks)}",
        )

    storage = DuckDBStorage(config.database_path)
    try:
        storage.option_chains.insert(chain)
        storage.option_quotes.bulk_insert(quotes)
        storage.option_greeks.bulk_insert(greeks)
        storage.underlying_prices.bulk_insert(
            [
                underlying_prices_by_date[observed_date]
                for observed_date in sorted(underlying_prices_by_date)
            ]
        )

        selected = _select_contract(config, storage)
        backtest_result = _run_backtest(config, storage, selected.contract)
    finally:
        storage.close()

    result = OneWeekPipelineResult(
        config=config,
        chain_contracts=len(contracts),
        quote_rows=len(quotes),
        greek_rows=len(greeks),
        underlying_rows=len(underlying_prices_by_date),
        selected_candidate=selected,
        backtest_result=backtest_result,
    )
    _write_report(config.report_path, result)
    _log(config, f"wrote report to {config.report_path}")
    return result


def _discover_contracts(
    config: OneWeekPipelineConfig,
    endpoints: OneWeekOptionEndpoints,
) -> list[OptionContract]:
    min_expiration = config.start_date + timedelta(days=config.min_dte)
    max_expiration = config.start_date + timedelta(days=config.max_dte)
    _log(config, f"listing expirations for {config.symbol}")
    expirations = [
        _row_date(row, "expiration", "exp", "expiration_date")
        for row in endpoints.list_expirations(symbol=config.symbol)
    ]
    _log(config, f"received {len(expirations)} expirations")
    contracts: list[OptionContract] = []
    for expiration in sorted(set(expirations)):
        if not min_expiration <= expiration <= max_expiration:
            continue
        _log(config, f"listing strikes for {config.symbol} {expiration}")
        for strike_row in endpoints.list_strikes(symbol=config.symbol, expiration=expiration):
            strike = _row_decimal(strike_row, "strike")
            if config.min_strike is not None and strike < config.min_strike:
                continue
            if config.max_strike is not None and strike > config.max_strike:
                continue
            contracts.append(
                OptionContract(
                    underlying_symbol=config.symbol,
                    expiration=expiration,
                    strike=strike,
                    option_type=config.option_type,
                )
            )
    contracts.sort(key=lambda contract: (contract.expiration, contract.strike))
    if config.max_contracts is not None:
        return contracts[: config.max_contracts]
    return contracts


def _retrieve_close_greeks(
    config: OneWeekPipelineConfig,
    endpoints: OneWeekOptionEndpoints,
    contract: OptionContract,
) -> tuple[list[OptionGreek], dict[date, UnderlyingPrice]]:
    rows = endpoints.history_greeks_first_order(
        symbol=contract.underlying_symbol,
        expiration=contract.expiration,
        strike=_strike_text(contract.strike),
        right=_thetadata_right(contract.option_type),
        interval="1m",
        start_date=config.start_date,
        end_date=config.end_date,
        start_time=MARKET_CLOSE.isoformat(),
        end_time=MARKET_CLOSE.isoformat(),
    )
    greeks: list[OptionGreek] = []
    underlying_by_date: dict[date, UnderlyingPrice] = {}
    for row in rows:
        timestamp = _row_timestamp(row, config.start_date)
        greeks.append(
            OptionGreek(
                contract=contract,
                timestamp=timestamp,
                delta=_optional_decimal(row, "delta"),
                gamma=None,
                theta=_optional_decimal(row, "theta"),
                vega=_optional_decimal(row, "vega"),
                rho=_optional_decimal(row, "rho"),
                implied_volatility=_optional_decimal(
                    row,
                    "implied_volatility",
                    "implied_vol",
                    "iv",
                ),
            )
        )
        underlying_price = _optional_decimal(row, "underlying_price")
        if underlying_price is not None:
            underlying_by_date[timestamp.date()] = UnderlyingPrice(
                symbol=config.symbol,
                timestamp=timestamp,
                price=underlying_price,
            )
    return greeks, underlying_by_date


def _select_contract(
    config: OneWeekPipelineConfig,
    storage: DuckDBStorage,
) -> OptionSelectionCandidate:
    chains = storage.option_chains.retrieve_by_date(config.start_date)
    if not chains:
        raise ValueError("no option chain was stored for the start date")
    underlying = _single(
        storage.underlying_prices.retrieve_by_date(config.start_date),
        "underlying price",
    )
    greeks = storage.option_greeks.retrieve_by_date(config.start_date)
    selector = ContractSelectionEngine(
        chains[0],
        underlying.price,
        as_of_date=config.start_date,
        greeks=greeks,
    )
    selected = selector.best(
        OptionSelectionQuery(
            option_type=config.option_type,
            min_dte=config.min_dte,
            max_dte=config.max_dte,
            target_delta=config.target_delta,
        )
    )
    if selected is None:
        raise ValueError("contract selector found no candidate")
    return selected


def _run_backtest(
    config: OneWeekPipelineConfig,
    storage: DuckDBStorage,
    selected_contract: OptionContract,
) -> BacktestResult:
    quotes = storage.option_quotes.retrieve_by_date_range(config.start_date, config.end_date)
    underlying_prices = storage.underlying_prices.retrieve_by_date_range(
        config.start_date,
        config.end_date,
    )
    quotes_by_date = {
        quote.timestamp.date(): quote
        for quote in quotes
        if quote.contract == selected_contract
    }
    underlying_by_date = {price.timestamp.date(): price for price in underlying_prices}
    market_events: list[BacktestMarketEvent] = []
    for observed_date in sorted(quotes_by_date):
        if observed_date not in underlying_by_date:
            continue
        quote = quotes_by_date[observed_date]
        market_events.append(
            BacktestMarketEvent(
                date=observed_date,
                option_marks={selected_contract: _quote_mark(quote)},
                underlying_prices={config.symbol: underlying_by_date[observed_date].price},
            )
        )
    if not market_events:
        raise ValueError("no market events available for selected contract")
    entry_quote = quotes_by_date[market_events[0].date]
    orders_by_date = {
        market_events[0].date: [
            BacktestOrderEvent(
                contract=selected_contract,
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
        )
    )
    return engine.run(market_events, orders_by_date)


def _write_report(path: Path, result: OneWeekPipelineResult) -> None:
    selected = result.selected_candidate
    snapshots = result.backtest_result.snapshots
    final_snapshot = snapshots[-1]
    payload = {
        "symbol": result.config.symbol,
        "start_date": result.config.start_date.isoformat(),
        "end_date": result.config.end_date.isoformat(),
        "chain_contracts": result.chain_contracts,
        "quote_rows": result.quote_rows,
        "greek_rows": result.greek_rows,
        "underlying_rows": result.underlying_rows,
        "selected_contract": _contract_payload(selected.contract),
        "selected_dte": selected.dte,
        "selected_delta": _decimal_text(selected.delta),
        "selected_iv": _decimal_text(selected.implied_volatility),
        "snapshots": [
            {
                "date": snapshot.date.isoformat(),
                "cash_balance": str(snapshot.cash_balance),
                "unrealized_pnl": str(snapshot.unrealized_pnl),
                "realized_pnl": str(snapshot.realized_pnl),
                "equity": str(snapshot.equity),
                "open_positions": len(snapshot.open_positions),
            }
            for snapshot in snapshots
        ],
    }
    lines = [
        "# One-Week Pipeline Inspection",
        "",
        "## JSON Summary",
        "",
        json.dumps(payload, indent=2),
        "",
        "## Final Snapshot",
        "",
        f"- Date: {final_snapshot.date.isoformat()}",
        f"- Equity: {final_snapshot.equity}",
        f"- Unrealized PnL: {final_snapshot.unrealized_pnl}",
        f"- Realized PnL: {final_snapshot.realized_pnl}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _quote_mark(quote: OptionQuote) -> Decimal:
    if quote.mark is not None:
        return quote.mark
    return (quote.bid + quote.ask) / Decimal("2")


def _single(values: list[UnderlyingPrice], label: str) -> UnderlyingPrice:
    if not values:
        raise ValueError(f"missing {label}")
    if len(values) > 1:
        raise ValueError(f"expected one {label}, found {len(values)}")
    return values[0]


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
    return datetime.combine(fallback_date, MARKET_CLOSE, tzinfo=UTC)


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


def _log(config: OneWeekPipelineConfig, message: str) -> None:
    if config.verbose:
        print(message, flush=True)
