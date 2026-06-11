from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from options_quant.data.models import (
    OptionContract,
    OptionGreek,
    OptionQuote,
    OptionSnapshot,
    OptionType,
    Position,
    Trade,
    TradeSide,
    UnderlyingPrice,
)

OBSERVED_AT = datetime(2026, 6, 10, 14, 30, tzinfo=UTC)


def make_contract(symbol: str = "AAPL") -> OptionContract:
    return OptionContract(
        underlying_symbol=symbol,
        expiration=date(2026, 7, 17),
        strike=Decimal("200"),
        option_type=OptionType.PUT,
    )


def test_underlying_price_validates_market_shape() -> None:
    price = UnderlyingPrice(
        symbol="AAPL",
        timestamp=OBSERVED_AT,
        price=Decimal("205.12"),
        bid=Decimal("205.10"),
        ask=Decimal("205.15"),
        volume=1_000_000,
    )

    assert price.symbol == "AAPL"
    assert price.price == Decimal("205.12")


def test_underlying_price_rejects_crossed_market() -> None:
    with pytest.raises(ValidationError, match="bid must be less than or equal to ask"):
        UnderlyingPrice(
            symbol="AAPL",
            timestamp=OBSERVED_AT,
            price=Decimal("205.12"),
            bid=Decimal("205.20"),
            ask=Decimal("205.10"),
        )


def test_underlying_price_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValidationError):
        UnderlyingPrice(
            symbol="AAPL",
            timestamp=datetime(2026, 6, 10, 14, 30),
            price=Decimal("205.12"),
        )


def test_option_contract_validates_contract_identity() -> None:
    contract = make_contract()

    assert contract.underlying_symbol == "AAPL"
    assert contract.multiplier == 100
    assert contract.option_type is OptionType.PUT


def test_option_contract_rejects_invalid_strike() -> None:
    with pytest.raises(ValidationError):
        OptionContract(
            underlying_symbol="AAPL",
            expiration=date(2026, 7, 17),
            strike=Decimal("0"),
            option_type=OptionType.CALL,
        )


def test_option_quote_validates_bid_ask_ordering() -> None:
    contract = make_contract()

    quote = OptionQuote(
        contract=contract,
        timestamp=OBSERVED_AT,
        bid=Decimal("3.10"),
        ask=Decimal("3.25"),
        last=Decimal("3.20"),
        mark=Decimal("3.175"),
        volume=250,
        open_interest=1_500,
    )

    assert quote.contract == contract
    assert quote.open_interest == 1_500


def test_option_quote_rejects_crossed_market() -> None:
    with pytest.raises(ValidationError, match="bid must be less than or equal to ask"):
        OptionQuote(
            contract=make_contract(),
            timestamp=OBSERVED_AT,
            bid=Decimal("3.30"),
            ask=Decimal("3.25"),
        )


def test_option_greek_validates_ranges() -> None:
    greek = OptionGreek(
        contract=make_contract(),
        timestamp=OBSERVED_AT,
        delta=Decimal("-0.32"),
        gamma=Decimal("0.012"),
        theta=Decimal("-0.04"),
        vega=Decimal("0.18"),
        rho=Decimal("-0.03"),
        implied_volatility=Decimal("0.42"),
    )

    assert greek.delta == Decimal("-0.32")
    assert greek.implied_volatility == Decimal("0.42")


def test_option_greek_rejects_invalid_delta() -> None:
    with pytest.raises(ValidationError):
        OptionGreek(
            contract=make_contract(),
            timestamp=OBSERVED_AT,
            delta=Decimal("-1.25"),
        )


def test_option_snapshot_requires_consistent_contracts() -> None:
    contract = make_contract()
    snapshot = OptionSnapshot(
        underlying=UnderlyingPrice(
            symbol="AAPL",
            timestamp=OBSERVED_AT,
            price=Decimal("205.12"),
        ),
        contract=contract,
        quote=OptionQuote(
            contract=contract,
            timestamp=OBSERVED_AT,
            bid=Decimal("3.10"),
            ask=Decimal("3.25"),
        ),
        greek=OptionGreek(
            contract=contract,
            timestamp=OBSERVED_AT,
            delta=Decimal("-0.32"),
        ),
        timestamp=OBSERVED_AT,
    )

    assert snapshot.contract == contract
    assert snapshot.quote.contract == contract


def test_option_snapshot_rejects_mismatched_underlying() -> None:
    contract = make_contract("AAPL")

    with pytest.raises(ValidationError, match="underlying symbol must match"):
        OptionSnapshot(
            underlying=UnderlyingPrice(
                symbol="MSFT",
                timestamp=OBSERVED_AT,
                price=Decimal("410.00"),
            ),
            contract=contract,
            quote=OptionQuote(
                contract=contract,
                timestamp=OBSERVED_AT,
                bid=Decimal("3.10"),
                ask=Decimal("3.25"),
            ),
            timestamp=OBSERVED_AT,
        )


def test_trade_models_executed_order_data() -> None:
    trade = Trade(
        contract=make_contract(),
        side=TradeSide.SELL,
        quantity=2,
        price=Decimal("3.20"),
        timestamp=OBSERVED_AT,
        fees=Decimal("1.30"),
        notes="Opening trade",
    )

    assert trade.quantity == 2
    assert trade.side is TradeSide.SELL
    assert trade.trade_id


def test_position_validates_open_position_state() -> None:
    position = Position(
        contract=make_contract(),
        quantity=-2,
        average_price=Decimal("3.20"),
        opened_at=OBSERVED_AT,
        updated_at=OBSERVED_AT,
        realized_pnl=Decimal("0"),
    )

    assert position.quantity == -2
    assert position.average_price == Decimal("3.20")


def test_position_rejects_zero_quantity() -> None:
    with pytest.raises(ValidationError, match="quantity must be non-zero"):
        Position(
            contract=make_contract(),
            quantity=0,
            average_price=Decimal("3.20"),
            opened_at=OBSERVED_AT,
        )
