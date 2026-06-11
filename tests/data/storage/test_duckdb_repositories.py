from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionImpliedVolatility,
    OptionQuote,
    OptionType,
    UnderlyingPrice,
)
from options_quant.data.storage import DuckDBStorage

OBSERVED_AT = datetime(2026, 6, 10, 14, 30, tzinfo=UTC)
NEXT_DAY = datetime(2026, 6, 11, 14, 30, tzinfo=UTC)
OUTSIDE_RANGE = datetime(2026, 6, 13, 14, 30, tzinfo=UTC)


@pytest.fixture
def storage() -> DuckDBStorage:
    duckdb_storage = DuckDBStorage()
    try:
        yield duckdb_storage
    finally:
        duckdb_storage.close()


def make_contract(
    symbol: str = "AAPL",
    expiration: date = date(2026, 7, 17),
    strike: Decimal = Decimal("200"),
) -> OptionContract:
    return OptionContract(
        underlying_symbol=symbol,
        expiration=expiration,
        strike=strike,
        option_type=OptionType.PUT,
    )


def test_underlying_prices_repository_round_trips_single_and_bulk_records(
    storage: DuckDBStorage,
) -> None:
    first = UnderlyingPrice(
        symbol="AAPL",
        timestamp=OBSERVED_AT,
        price=Decimal("205.12"),
        bid=Decimal("205.10"),
        ask=Decimal("205.15"),
        volume=1_000_000,
    )
    second = UnderlyingPrice(symbol="MSFT", timestamp=NEXT_DAY, price=Decimal("410.50"))
    outside = UnderlyingPrice(symbol="GOOGL", timestamp=OUTSIDE_RANGE, price=Decimal("175.25"))

    storage.underlying_prices.insert(first)
    storage.underlying_prices.bulk_insert([second, outside])
    storage.underlying_prices.bulk_insert([])

    assert storage.underlying_prices.retrieve_by_date(OBSERVED_AT.date()) == [first]
    retrieved_range = storage.underlying_prices.retrieve_by_date_range(
        OBSERVED_AT.date(),
        NEXT_DAY.date(),
    )
    assert retrieved_range == [
        first,
        second,
    ]


def test_option_chains_repository_groups_contracts_by_snapshot(storage: DuckDBStorage) -> None:
    first_contract = make_contract(strike=Decimal("200"))
    second_contract = make_contract(strike=Decimal("195"))
    next_day_contract = make_contract("MSFT", date(2026, 8, 21), Decimal("400"))
    first_chain = OptionChain(
        underlying_symbol="AAPL",
        timestamp=OBSERVED_AT,
        contracts=(first_contract, second_contract),
    )
    sorted_first_chain = OptionChain(
        underlying_symbol="AAPL",
        timestamp=OBSERVED_AT,
        contracts=(second_contract, first_contract),
    )
    next_day_chain = OptionChain(
        underlying_symbol="MSFT",
        timestamp=NEXT_DAY,
        contracts=(next_day_contract,),
    )

    storage.option_chains.insert(first_chain)
    storage.option_chains.bulk_insert([next_day_chain])

    assert storage.option_chains.retrieve_by_date(OBSERVED_AT.date()) == [sorted_first_chain]
    assert storage.option_chains.retrieve_by_date_range(OBSERVED_AT.date(), NEXT_DAY.date()) == [
        sorted_first_chain,
        next_day_chain,
    ]


def test_option_quotes_repository_round_trips_quote_fields(storage: DuckDBStorage) -> None:
    first = OptionQuote(
        contract=make_contract(),
        timestamp=OBSERVED_AT,
        bid=Decimal("3.10"),
        ask=Decimal("3.25"),
        last=Decimal("3.20"),
        mark=Decimal("3.175"),
        volume=250,
        open_interest=1_500,
    )
    second = OptionQuote(
        contract=make_contract("MSFT", date(2026, 8, 21), Decimal("400")),
        timestamp=NEXT_DAY,
        bid=Decimal("4.10"),
        ask=Decimal("4.35"),
    )

    storage.option_quotes.insert(first)
    storage.option_quotes.bulk_insert([second])

    assert storage.option_quotes.retrieve_by_date(OBSERVED_AT.date()) == [first]
    assert storage.option_quotes.retrieve_by_date_range(OBSERVED_AT.date(), NEXT_DAY.date()) == [
        first,
        second,
    ]


def test_option_greeks_repository_round_trips_optional_metrics(storage: DuckDBStorage) -> None:
    first = OptionGreek(
        contract=make_contract(),
        timestamp=OBSERVED_AT,
        delta=Decimal("-0.32"),
        gamma=Decimal("0.012"),
        theta=Decimal("-0.04"),
        vega=Decimal("0.18"),
        rho=Decimal("-0.03"),
        implied_volatility=Decimal("0.42"),
    )
    second = OptionGreek(
        contract=make_contract("MSFT", date(2026, 8, 21), Decimal("400")),
        timestamp=NEXT_DAY,
        delta=Decimal("-0.25"),
    )

    storage.option_greeks.insert(first)
    storage.option_greeks.bulk_insert([second])

    assert storage.option_greeks.retrieve_by_date(OBSERVED_AT.date()) == [first]
    assert storage.option_greeks.retrieve_by_date_range(OBSERVED_AT.date(), NEXT_DAY.date()) == [
        first,
        second,
    ]


def test_option_iv_repository_round_trips_iv_observations(storage: DuckDBStorage) -> None:
    first = OptionImpliedVolatility(
        contract=make_contract(),
        timestamp=OBSERVED_AT,
        implied_volatility=Decimal("0.42"),
    )
    second = OptionImpliedVolatility(
        contract=make_contract("MSFT", date(2026, 8, 21), Decimal("400")),
        timestamp=NEXT_DAY,
        implied_volatility=Decimal("0.36"),
    )

    storage.option_iv.insert(first)
    storage.option_iv.bulk_insert([second])

    assert storage.option_iv.retrieve_by_date(OBSERVED_AT.date()) == [first]
    assert storage.option_iv.retrieve_by_date_range(OBSERVED_AT.date(), NEXT_DAY.date()) == [
        first,
        second,
    ]


def test_repositories_reject_inverted_date_ranges(storage: DuckDBStorage) -> None:
    with pytest.raises(ValueError, match="start_date must be less than or equal to end_date"):
        storage.option_quotes.retrieve_by_date_range(NEXT_DAY.date(), OBSERVED_AT.date())
