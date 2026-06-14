from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from options_quant.data.ingestion import (
    ThetaDataEODIngestionConfig,
    ThetaDataEODIngestionPipeline,
)
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

OBSERVED_AT = datetime(2026, 6, 10, tzinfo=UTC)
EXPIRATION = date(2026, 7, 17)


class MockEODProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.selected_contract = OptionContract(
            underlying_symbol="SPY",
            expiration=EXPIRATION,
            strike=Decimal("520"),
            option_type=OptionType.PUT,
        )
        self.unselected_call = OptionContract(
            underlying_symbol="SPY",
            expiration=EXPIRATION,
            strike=Decimal("520"),
            option_type=OptionType.CALL,
        )
        self.too_far_put = OptionContract(
            underlying_symbol="SPY",
            expiration=date(2026, 8, 21),
            strike=Decimal("500"),
            option_type=OptionType.PUT,
        )

    def retrieve_underlying_eod_prices(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[UnderlyingPrice]:
        self.calls.append(("underlying", (symbol, start_date, end_date)))
        return [
            UnderlyingPrice(
                symbol=symbol,
                timestamp=OBSERVED_AT,
                price=Decimal("525.00"),
                volume=1_000_000,
            )
        ]

    def retrieve_option_chain(self, symbol: str, as_of_date: date) -> OptionChain:
        self.calls.append(("chain", (symbol, as_of_date)))
        return OptionChain(
            underlying_symbol=symbol,
            timestamp=datetime.combine(as_of_date, datetime.min.time(), tzinfo=UTC),
            contracts=(self.unselected_call, self.too_far_put, self.selected_contract),
        )

    def retrieve_option_eod_quotes(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionQuote]:
        self.calls.append(("quotes", contract))
        return [
            OptionQuote(
                contract=contract,
                timestamp=OBSERVED_AT,
                bid=Decimal("3.10"),
                ask=Decimal("3.30"),
                last=Decimal("3.20"),
                mark=Decimal("3.20"),
                volume=250,
            )
        ]

    def retrieve_first_order_greeks(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionGreek]:
        self.calls.append(("greeks", contract))
        return [
            OptionGreek(
                contract=contract,
                timestamp=OBSERVED_AT,
                delta=Decimal("-0.12"),
                theta=Decimal("-0.04"),
                vega=Decimal("0.18"),
                rho=Decimal("-0.03"),
            )
        ]

    def retrieve_open_interest(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionOpenInterest]:
        self.calls.append(("open_interest", contract))
        return [
            OptionOpenInterest(
                contract=contract,
                timestamp=OBSERVED_AT,
                open_interest=1_500,
            )
        ]


@pytest.fixture
def storage() -> Generator[DuckDBStorage]:
    duckdb_storage = DuckDBStorage()
    try:
        yield duckdb_storage
    finally:
        duckdb_storage.close()


def test_pipeline_ingests_filtered_eod_rows_into_storage(storage: DuckDBStorage) -> None:
    provider = MockEODProvider()
    pipeline = ThetaDataEODIngestionPipeline(provider, storage)

    result = pipeline.ingest(
        ThetaDataEODIngestionConfig(
            symbol="SPY",
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 10),
            min_dte=30,
            max_dte=45,
        )
    )

    assert result.underlying_prices == 1
    assert result.option_chains == 1
    assert result.contracts_selected == 1
    assert result.option_quotes == 1
    assert result.option_greeks == 1
    assert storage.underlying_prices.retrieve_by_date(date(2026, 6, 10))[0].price == Decimal(
        "525.00"
    )
    assert storage.option_chains.retrieve_by_date(date(2026, 6, 10))[0].contracts == (
        provider.selected_contract,
    )
    quote = storage.option_quotes.retrieve_by_date(date(2026, 6, 10))[0]
    assert quote.contract == provider.selected_contract
    assert quote.open_interest == 1_500
    greek = storage.option_greeks.retrieve_by_date(date(2026, 6, 10))[0]
    assert greek.contract == provider.selected_contract
    assert greek.delta == Decimal("-0.12")
    assert ("quotes", provider.unselected_call) not in provider.calls
    assert ("quotes", provider.too_far_put) not in provider.calls


def test_pipeline_allows_empty_candidate_set(storage: DuckDBStorage) -> None:
    class NoPutProvider(MockEODProvider):
        def retrieve_option_chain(self, symbol: str, as_of_date: date) -> OptionChain:
            self.calls.append(("chain", (symbol, as_of_date)))
            return OptionChain(
                underlying_symbol=symbol,
                timestamp=datetime.combine(as_of_date, datetime.min.time(), tzinfo=UTC),
                contracts=(self.unselected_call,),
            )

    provider = NoPutProvider()
    pipeline = ThetaDataEODIngestionPipeline(provider, storage)

    result = pipeline.ingest(
        ThetaDataEODIngestionConfig(
            symbol="SPY",
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 10),
            min_dte=1,
            max_dte=5,
        )
    )

    assert result.contracts_selected == 0
    assert result.option_chains == 0
    assert storage.option_quotes.retrieve_by_date(date(2026, 6, 10)) == []
    assert storage.option_greeks.retrieve_by_date(date(2026, 6, 10)) == []


def test_pipeline_uses_nearest_expiration_when_dte_window_is_empty(
    storage: DuckDBStorage,
) -> None:
    provider = MockEODProvider()
    pipeline = ThetaDataEODIngestionPipeline(provider, storage)

    result = pipeline.ingest(
        ThetaDataEODIngestionConfig(
            symbol="SPY",
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 10),
            min_dte=1,
            max_dte=5,
        )
    )

    assert result.contracts_selected == 1
    assert storage.option_quotes.retrieve_by_date(date(2026, 6, 10))[0].contract == (
        provider.selected_contract
    )


def test_pipeline_uses_first_market_date_for_chain_discovery(storage: DuckDBStorage) -> None:
    provider = MockEODProvider()
    pipeline = ThetaDataEODIngestionPipeline(provider, storage)

    result = pipeline.ingest(
        ThetaDataEODIngestionConfig(
            symbol="SPY",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 10),
            chain_as_of_date=date(2026, 6, 8),
            min_dte=30,
            max_dte=45,
        )
    )

    assert result.contracts_selected == 1
    assert ("chain", ("SPY", date(2026, 6, 10))) in provider.calls


def test_ingestion_config_validates_ranges() -> None:
    with pytest.raises(ValidationError, match="start_date must be less than or equal to end_date"):
        ThetaDataEODIngestionConfig(
            symbol="SPY",
            start_date=date(2026, 6, 11),
            end_date=date(2026, 6, 10),
        )

    with pytest.raises(ValidationError, match="min_dte must be less than or equal to max_dte"):
        ThetaDataEODIngestionConfig(
            symbol="SPY",
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 10),
            min_dte=45,
            max_dte=30,
        )
