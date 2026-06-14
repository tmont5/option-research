"""ThetaData end-of-day ingestion pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionOpenInterest,
    OptionQuote,
    OptionType,
    UnderlyingPrice,
)
from options_quant.data.storage import DuckDBStorage


class ThetaDataEODProvider(Protocol):
    """Provider methods required by the EOD ingestion pipeline."""

    def retrieve_underlying_eod_prices(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[UnderlyingPrice]:
        """Return end-of-day underlying price rows."""

    def retrieve_option_chain(self, symbol: str, as_of_date: date) -> OptionChain:
        """Return a contract chain for filtering candidate options."""

    def retrieve_option_eod_quotes(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionQuote]:
        """Return end-of-day option quote/mark rows."""

    def retrieve_first_order_greeks(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionGreek]:
        """Return first-order Greek rows."""

    def retrieve_open_interest(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionOpenInterest]:
        """Return open-interest rows."""


class ThetaDataEODIngestionConfig(BaseModel):
    """Configuration for one historical ThetaData EOD ingestion pass."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(min_length=1, description="Underlying symbol to ingest.")
    start_date: date = Field(description="Inclusive first data date.")
    end_date: date = Field(description="Inclusive final data date.")
    chain_as_of_date: date | None = Field(
        default=None,
        description="Date used to discover the option chain. Defaults to start_date.",
    )
    min_dte: int = Field(default=30, ge=0, description="Minimum contract DTE to ingest.")
    max_dte: int = Field(default=45, ge=0, description="Maximum contract DTE to ingest.")
    option_type: OptionType = Field(
        default=OptionType.PUT,
        description="Option type to ingest.",
    )
    max_contracts: int | None = Field(
        default=None,
        gt=0,
        description="Optional cap for smoke tests or small local backfills.",
    )
    target_delta: Decimal | None = Field(
        default=None,
        ge=Decimal("-1"),
        le=Decimal("1"),
        description="Optional target delta used to narrow the contract universe.",
    )
    contracts_around_target: int = Field(
        default=5,
        gt=0,
        description="Number of closest-delta contracts to keep when target_delta is set.",
    )

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        """Validate calendar and DTE ranges."""
        if self.start_date > self.end_date:
            raise ValueError("start_date must be less than or equal to end_date")
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must be less than or equal to max_dte")
        return self

    @property
    def effective_chain_as_of_date(self) -> date:
        """Return the chain date used for candidate discovery."""
        return self.chain_as_of_date or self.start_date


@dataclass(frozen=True)
class ThetaDataEODIngestionResult:
    """Record counts written by one EOD ingestion pass."""

    underlying_prices: int
    option_chains: int
    contracts_selected: int
    option_quotes: int
    option_greeks: int


class ThetaDataEODIngestionPipeline:
    """Ingest ThetaData EOD rows into market-data storage."""

    def __init__(self, provider: ThetaDataEODProvider, storage: DuckDBStorage) -> None:
        self._provider = provider
        self._storage = storage

    def ingest(self, config: ThetaDataEODIngestionConfig) -> ThetaDataEODIngestionResult:
        """Fetch and store one symbol's EOD market data."""
        underlying_prices = self._provider.retrieve_underlying_eod_prices(
            config.symbol,
            config.start_date,
            config.end_date,
        )
        self._storage.underlying_prices.bulk_insert(underlying_prices)
        chain_as_of_date = _chain_as_of_date(config, underlying_prices)

        chain = self._provider.retrieve_option_chain(
            config.symbol,
            chain_as_of_date,
        )
        contracts = _candidate_contracts(chain, config, chain_as_of_date)
        if config.target_delta is not None:
            contracts = _closest_delta_contracts(
                contracts,
                config,
                self._provider,
                chain_as_of_date,
            )
        option_chains_inserted = 0
        if contracts:
            selected_chain = OptionChain(
                underlying_symbol=chain.underlying_symbol,
                timestamp=chain.timestamp,
                contracts=tuple(contracts),
            )
            self._storage.option_chains.insert(selected_chain)
            option_chains_inserted = 1

        option_quotes: list[OptionQuote] = []
        option_greeks: list[OptionGreek] = []
        for contract in contracts:
            open_interest_by_date = {
                observation.timestamp.date(): observation.open_interest
                for observation in self._provider.retrieve_open_interest(
                    contract,
                    config.start_date,
                    config.end_date,
                )
            }
            option_quotes.extend(
                _merge_open_interest(
                    self._provider.retrieve_option_eod_quotes(
                        contract,
                        config.start_date,
                        config.end_date,
                    ),
                    open_interest_by_date,
                )
            )
            option_greeks.extend(
                self._provider.retrieve_first_order_greeks(
                    contract,
                    config.start_date,
                    config.end_date,
                )
            )

        self._storage.option_quotes.bulk_insert(option_quotes)
        self._storage.option_greeks.bulk_insert(option_greeks)
        return ThetaDataEODIngestionResult(
            underlying_prices=len(underlying_prices),
            option_chains=option_chains_inserted,
            contracts_selected=len(contracts),
            option_quotes=len(option_quotes),
            option_greeks=len(option_greeks),
        )


def _candidate_contracts(
    chain: OptionChain,
    config: ThetaDataEODIngestionConfig,
    chain_date: date | None = None,
) -> list[OptionContract]:
    effective_chain_date = chain_date or config.effective_chain_as_of_date
    min_expiration = effective_chain_date + timedelta(days=config.min_dte)
    max_expiration = effective_chain_date + timedelta(days=config.max_dte)
    contracts = [
        contract
        for contract in chain.contracts
        if contract.option_type is config.option_type
        and min_expiration <= contract.expiration <= max_expiration
    ]
    contracts.sort(key=lambda contract: (contract.expiration, contract.strike))
    if config.max_contracts is not None:
        return contracts[: config.max_contracts]
    return contracts


def _closest_delta_contracts(
    contracts: list[OptionContract],
    config: ThetaDataEODIngestionConfig,
    provider: ThetaDataEODProvider,
    chain_date: date | None = None,
) -> list[OptionContract]:
    if config.target_delta is None:
        return contracts
    effective_chain_date = chain_date or config.effective_chain_as_of_date
    candidates: list[tuple[Decimal, Decimal, OptionContract]] = []
    for contract in contracts:
        greeks = provider.retrieve_first_order_greeks(
            contract,
            effective_chain_date,
            effective_chain_date,
        )
        deltas = [greek.delta for greek in greeks if greek.delta is not None]
        if not deltas:
            continue
        delta = deltas[-1]
        candidates.append((abs(delta - config.target_delta), contract.strike, contract))
    candidates.sort(key=lambda item: (item[0], item[1]))
    selected = [contract for _, _, contract in candidates[: config.contracts_around_target]]
    if config.max_contracts is not None:
        return selected[: config.max_contracts]
    return selected


def _chain_as_of_date(
    config: ThetaDataEODIngestionConfig,
    underlying_prices: list[UnderlyingPrice],
) -> date:
    if config.chain_as_of_date is not None:
        available_dates = sorted({price.timestamp.date() for price in underlying_prices})
        for available_date in available_dates:
            if available_date >= config.chain_as_of_date:
                return available_date
        return config.chain_as_of_date
    return config.effective_chain_as_of_date


def _merge_open_interest(
    quotes: list[OptionQuote],
    open_interest_by_date: dict[date, int],
) -> list[OptionQuote]:
    if not open_interest_by_date:
        return quotes
    return [
        quote.model_copy(
            update={
                "open_interest": quote.open_interest
                if quote.open_interest is not None
                else open_interest_by_date.get(quote.timestamp.date())
            }
        )
        for quote in quotes
    ]
