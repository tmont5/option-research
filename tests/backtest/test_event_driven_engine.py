from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from options_quant.backtest import (
    BacktestConfig,
    BacktestEngine,
    BacktestMarketEvent,
    BacktestOrderEvent,
    BacktestOrderType,
    BacktestPosition,
    ExitReason,
)
from options_quant.data.models import OptionContract, OptionType, TradeSide

INITIAL_CASH = Decimal("100000")


def make_contract(
    *,
    expiration: date = date(2026, 7, 17),
    strike: Decimal = Decimal("100"),
    option_type: OptionType = OptionType.PUT,
) -> OptionContract:
    return OptionContract(
        underlying_symbol="AAPL",
        expiration=expiration,
        strike=strike,
        option_type=option_type,
    )


def open_order(
    contract: OptionContract,
    *,
    side: TradeSide,
    quantity: int,
    price: Decimal,
) -> BacktestOrderEvent:
    return BacktestOrderEvent(
        contract=contract,
        side=side,
        quantity=quantity,
        price=price,
        event_type=BacktestOrderType.OPEN,
    )


def close_order(
    position: BacktestPosition,
    *,
    price: Decimal,
) -> BacktestOrderEvent:
    return BacktestOrderEvent(
        contract=position.contract,
        side=TradeSide.SELL if position.quantity > 0 else TradeSide.BUY,
        quantity=position.absolute_quantity,
        price=price,
        event_type=BacktestOrderType.CLOSE,
        position_id=position.position_id,
    )


def test_long_option_open_mark_and_close_tracks_pnl_and_cash() -> None:
    contract = make_contract()
    engine = BacktestEngine(BacktestConfig(initial_cash=INITIAL_CASH))

    first = engine.run(
        [BacktestMarketEvent(date=date(2026, 6, 10), option_marks={contract: Decimal("2.50")})],
        {
            date(2026, 6, 10): [
                open_order(contract, side=TradeSide.BUY, quantity=1, price=Decimal("2.00"))
            ]
        },
    )
    position = first.snapshots[0].open_positions[0]

    result = engine.run(
        [BacktestMarketEvent(date=date(2026, 6, 11), option_marks={contract: Decimal("3.00")})],
        {date(2026, 6, 11): [close_order(position, price=Decimal("3.00"))]},
    )

    assert first.snapshots[0].cash_balance == Decimal("99800")
    assert first.snapshots[0].unrealized_pnl == Decimal("50.00")
    assert first.snapshots[0].equity == Decimal("100050.00")
    assert result.snapshots[0].cash_balance == Decimal("100100.00")
    assert result.snapshots[0].realized_pnl == Decimal("100.00")
    assert result.closed_positions[0].realized_pnl == Decimal("100.00")
    assert result.closed_positions[0].exit_reason is ExitReason.ORDER


def test_short_option_open_mark_and_close_tracks_pnl_and_cash() -> None:
    contract = make_contract(strike=Decimal("95"))
    engine = BacktestEngine(BacktestConfig(initial_cash=INITIAL_CASH))

    first = engine.run(
        [BacktestMarketEvent(date=date(2026, 6, 10), option_marks={contract: Decimal("1.50")})],
        {
            date(2026, 6, 10): [
                open_order(contract, side=TradeSide.SELL, quantity=2, price=Decimal("2.00"))
            ]
        },
    )
    position = first.snapshots[0].open_positions[0]

    result = engine.run(
        [BacktestMarketEvent(date=date(2026, 6, 11), option_marks={contract: Decimal("1.00")})],
        {date(2026, 6, 11): [close_order(position, price=Decimal("1.00"))]},
    )

    assert first.snapshots[0].cash_balance == Decimal("100400")
    assert first.snapshots[0].unrealized_pnl == Decimal("100.00")
    assert result.snapshots[0].cash_balance == Decimal("100200.00")
    assert result.snapshots[0].realized_pnl == Decimal("200.00")


def test_commissions_and_slippage_are_applied_to_open_close_and_realized_pnl() -> None:
    contract = make_contract()
    engine = BacktestEngine(
        BacktestConfig(
            initial_cash=INITIAL_CASH,
            commission_per_contract=Decimal("0.65"),
            slippage_per_contract=Decimal("0.05"),
        )
    )

    first = engine.run(
        [BacktestMarketEvent(date=date(2026, 6, 10), option_marks={contract: Decimal("2.50")})],
        {
            date(2026, 6, 10): [
                open_order(contract, side=TradeSide.BUY, quantity=1, price=Decimal("2.00"))
            ]
        },
    )
    position = first.snapshots[0].open_positions[0]
    result = engine.run(
        [BacktestMarketEvent(date=date(2026, 6, 11), option_marks={contract: Decimal("3.00")})],
        {date(2026, 6, 11): [close_order(position, price=Decimal("3.00"))]},
    )

    assert position.entry_fill_price == Decimal("2.05")
    assert first.snapshots[0].cash_balance == Decimal("99794.35")
    assert result.closed_positions[0].exit_fill_price == Decimal("2.95")
    assert result.snapshots[0].realized_pnl == Decimal("88.70")
    assert result.snapshots[0].cash_balance == Decimal("100088.70")


def test_multiple_simultaneous_positions_track_unrealized_pnl_and_utilization() -> None:
    long_contract = make_contract(strike=Decimal("100"), option_type=OptionType.CALL)
    short_contract = make_contract(strike=Decimal("90"), option_type=OptionType.PUT)
    engine = BacktestEngine(BacktestConfig(initial_cash=INITIAL_CASH))

    result = engine.run(
        [
            BacktestMarketEvent(
                date=date(2026, 6, 10),
                option_marks={
                    long_contract: Decimal("2.50"),
                    short_contract: Decimal("1.00"),
                },
            )
        ],
        {
            date(2026, 6, 10): [
                open_order(long_contract, side=TradeSide.BUY, quantity=1, price=Decimal("2.00")),
                open_order(short_contract, side=TradeSide.SELL, quantity=1, price=Decimal("1.50")),
            ]
        },
    )

    snapshot = result.snapshots[0]
    assert len(snapshot.open_positions) == 2
    assert snapshot.cash_balance == Decimal("99950.00")
    assert snapshot.unrealized_pnl == Decimal("100.00")
    assert snapshot.equity == Decimal("100100.00")
    assert snapshot.capital_utilization == Decimal("9250.00") / Decimal("100100.00")


def test_expiration_closes_long_call_at_intrinsic_value() -> None:
    contract = make_contract(
        expiration=date(2026, 6, 12),
        strike=Decimal("100"),
        option_type=OptionType.CALL,
    )
    engine = BacktestEngine(BacktestConfig(initial_cash=INITIAL_CASH))

    first = engine.run(
        [BacktestMarketEvent(date=date(2026, 6, 10), option_marks={contract: Decimal("2.00")})],
        {
            date(2026, 6, 10): [
                open_order(contract, side=TradeSide.BUY, quantity=1, price=Decimal("2.00"))
            ]
        },
    )

    result = engine.run(
        [
            BacktestMarketEvent(
                date=date(2026, 6, 12),
                underlying_prices={"AAPL": Decimal("105")},
            )
        ]
    )

    assert first.snapshots[0].open_positions
    assert result.snapshots[0].open_positions == ()
    assert result.closed_positions[0].exit_reason is ExitReason.EXPIRATION
    assert result.closed_positions[0].exit_fill_price == Decimal("5")
    assert result.snapshots[0].realized_pnl == Decimal("300.00")


def test_expiration_closes_short_put_at_intrinsic_value() -> None:
    contract = make_contract(expiration=date(2026, 6, 12), strike=Decimal("100"))
    engine = BacktestEngine(BacktestConfig(initial_cash=INITIAL_CASH))

    first = engine.run(
        [BacktestMarketEvent(date=date(2026, 6, 10), option_marks={contract: Decimal("2.00")})],
        {
            date(2026, 6, 10): [
                open_order(contract, side=TradeSide.SELL, quantity=1, price=Decimal("2.00"))
            ]
        },
    )

    result = engine.run(
        [
            BacktestMarketEvent(
                date=date(2026, 6, 12),
                underlying_prices={"AAPL": Decimal("97")},
            )
        ]
    )

    assert first.snapshots[0].open_positions
    assert result.closed_positions[0].exit_fill_price == Decimal("3")
    assert result.snapshots[0].realized_pnl == Decimal("-100.00")
    assert result.snapshots[0].cash_balance == Decimal("99900.00")


def test_early_exit_rule_can_close_position_before_expiration() -> None:
    contract = make_contract()

    def exit_at_double(
        position: BacktestPosition,
        market_event: BacktestMarketEvent,
    ) -> BacktestOrderEvent | None:
        mark = market_event.option_marks[position.contract]
        if mark >= position.entry_fill_price * Decimal("2"):
            return close_order(position, price=mark)
        return None

    engine = BacktestEngine(
        BacktestConfig(initial_cash=INITIAL_CASH),
        early_exit_rules=[exit_at_double],
    )

    result = engine.run(
        [
            BacktestMarketEvent(date=date(2026, 6, 10), option_marks={contract: Decimal("2.00")}),
            BacktestMarketEvent(date=date(2026, 6, 11), option_marks={contract: Decimal("4.00")}),
        ],
        {
            date(2026, 6, 10): [
                open_order(contract, side=TradeSide.BUY, quantity=1, price=Decimal("2.00"))
            ]
        },
    )

    assert result.snapshots[1].open_positions == ()
    assert result.closed_positions[0].exit_reason is ExitReason.EARLY_EXIT
    assert result.snapshots[1].realized_pnl == Decimal("200.00")


def test_close_event_requires_position_id() -> None:
    with pytest.raises(ValidationError, match="close events require position_id"):
        BacktestOrderEvent(
            contract=make_contract(),
            side=TradeSide.SELL,
            quantity=1,
            price=Decimal("2.00"),
            event_type=BacktestOrderType.CLOSE,
        )


def test_close_order_must_match_position_contract() -> None:
    opened = make_contract(strike=Decimal("100"))
    other = make_contract(strike=Decimal("105"))
    engine = BacktestEngine(BacktestConfig(initial_cash=INITIAL_CASH))
    first = engine.run(
        [BacktestMarketEvent(date=date(2026, 6, 10), option_marks={opened: Decimal("2.00")})],
        {
            date(2026, 6, 10): [
                open_order(opened, side=TradeSide.BUY, quantity=1, price=Decimal("2.00"))
            ]
        },
    )
    position = first.snapshots[0].open_positions[0]

    with pytest.raises(ValueError, match="close order contract must match position contract"):
        engine.run(
            [BacktestMarketEvent(date=date(2026, 6, 11), option_marks={opened: Decimal("2.00")})],
            {
                date(2026, 6, 11): [
                    BacktestOrderEvent(
                        contract=other,
                        side=TradeSide.SELL,
                        quantity=1,
                        price=Decimal("2.00"),
                        event_type=BacktestOrderType.CLOSE,
                        position_id=position.position_id,
                    )
                ]
            },
        )


def test_missing_mark_for_open_non_expired_position_raises() -> None:
    contract = make_contract()
    engine = BacktestEngine(BacktestConfig(initial_cash=INITIAL_CASH))

    with pytest.raises(ValueError, match="missing option mark for open position"):
        engine.run(
            [BacktestMarketEvent(date=date(2026, 6, 10))],
            {
                date(2026, 6, 10): [
                    open_order(contract, side=TradeSide.BUY, quantity=1, price=Decimal("2.00"))
                ]
            },
        )
