from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from options_quant.backtest import (
    BacktestAccountSnapshot,
    BacktestOrderEvent,
    BacktestOrderType,
    BacktestPosition,
)
from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionQuote,
    OptionType,
    TradeSide,
    UnderlyingPrice,
)
from options_quant.strategies import (
    OptionsStrategy,
    PortfolioState,
    StrategyMarketData,
    StrategySignal,
    StrategySignalType,
)


def make_contract() -> OptionContract:
    return OptionContract(
        underlying_symbol="AAPL",
        expiration=date(2026, 7, 17),
        strike=Decimal("100"),
        option_type=OptionType.PUT,
    )


def make_position(contract: OptionContract) -> BacktestPosition:
    return BacktestPosition(
        position_id=uuid4(),
        contract=contract,
        quantity=-1,
        entry_price=Decimal("2.00"),
        entry_fill_price=Decimal("2.00"),
        entry_date=date(2026, 6, 10),
        entry_cash_flow=Decimal("200"),
        entry_commission=Decimal("0"),
    )


def make_market_data(contract: OptionContract) -> StrategyMarketData:
    return StrategyMarketData(
        date=date(2026, 6, 10),
        underlying_prices={
            "AAPL": UnderlyingPrice(
                symbol="AAPL",
                timestamp="2026-06-10T14:30:00+00:00",
                price=Decimal("101"),
            )
        },
        option_chains={
            "AAPL": OptionChain(
                underlying_symbol="AAPL",
                timestamp="2026-06-10T14:30:00+00:00",
                contracts=(contract,),
            )
        },
        option_quotes={
            contract: OptionQuote(
                contract=contract,
                timestamp="2026-06-10T14:30:00+00:00",
                bid=Decimal("1.90"),
                ask=Decimal("2.10"),
                mark=Decimal("2.00"),
            )
        },
    )


def make_portfolio_state(position: BacktestPosition | None = None) -> PortfolioState:
    return PortfolioState(
        date=date(2026, 6, 10),
        cash_balance=Decimal("100000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        capital_utilization=Decimal("0"),
        equity=Decimal("100000"),
        open_positions=() if position is None else (position,),
    )


class ExampleStrategy(OptionsStrategy):
    def generate_signals(
        self,
        market_data: StrategyMarketData,
        portfolio_state: PortfolioState,
    ) -> tuple[StrategySignal, ...]:
        contract = market_data.option_chains["AAPL"].contracts[0]
        if portfolio_state.open_positions:
            return (
                StrategySignal(
                    signal_type=StrategySignalType.HOLD,
                    contract=contract,
                    reason="position already open",
                ),
            )
        return (
            StrategySignal(
                signal_type=StrategySignalType.ENTER_SHORT,
                contract=contract,
                target_quantity=1,
                confidence=Decimal("0.75"),
                reason="example entry",
            ),
        )

    def manage_positions(
        self,
        market_data: StrategyMarketData,
        portfolio_state: PortfolioState,
        signals: tuple[StrategySignal, ...],
    ) -> tuple[BacktestOrderEvent, ...]:
        del portfolio_state
        orders: list[BacktestOrderEvent] = []
        for signal in signals:
            if signal.signal_type is StrategySignalType.ENTER_SHORT and signal.contract is not None:
                quote = market_data.option_quotes[signal.contract]
                orders.append(
                    BacktestOrderEvent(
                        contract=signal.contract,
                        side=TradeSide.SELL,
                        quantity=signal.target_quantity or 1,
                        price=quote.mark or quote.bid,
                        event_type=BacktestOrderType.OPEN,
                    )
                )
        return tuple(orders)

    def exit_rules(
        self,
        market_data: StrategyMarketData,
        portfolio_state: PortfolioState,
    ) -> tuple[BacktestOrderEvent, ...]:
        del market_data
        orders: list[BacktestOrderEvent] = []
        for position in portfolio_state.open_positions:
            if portfolio_state.capital_utilization > Decimal("0.80"):
                orders.append(
                    BacktestOrderEvent(
                        contract=position.contract,
                        side=TradeSide.BUY,
                        quantity=position.absolute_quantity,
                        price=Decimal("1.00"),
                        event_type=BacktestOrderType.CLOSE,
                        position_id=position.position_id,
                    )
                )
        return tuple(orders)


def test_options_strategy_cannot_be_instantiated_without_required_methods() -> None:
    with pytest.raises(TypeError):
        OptionsStrategy()


def test_concrete_strategy_generates_signals_from_market_data_and_portfolio_state() -> None:
    contract = make_contract()
    strategy = ExampleStrategy()

    signals = strategy.generate_signals(make_market_data(contract), make_portfolio_state())

    assert len(signals) == 1
    assert signals[0].signal_type is StrategySignalType.ENTER_SHORT
    assert signals[0].contract == contract
    assert signals[0].target_quantity == 1
    assert signals[0].confidence == Decimal("0.75")


def test_concrete_strategy_manage_positions_returns_backtest_order_events() -> None:
    contract = make_contract()
    strategy = ExampleStrategy()
    market_data = make_market_data(contract)
    signals = strategy.generate_signals(market_data, make_portfolio_state())

    orders = strategy.manage_positions(market_data, make_portfolio_state(), signals)

    assert len(orders) == 1
    assert orders[0].contract == contract
    assert orders[0].side is TradeSide.SELL
    assert orders[0].price == Decimal("2.00")
    assert orders[0].event_type is BacktestOrderType.OPEN


def test_concrete_strategy_exit_rules_can_emit_close_orders() -> None:
    contract = make_contract()
    position = make_position(contract)
    strategy = ExampleStrategy()
    portfolio_state = make_portfolio_state(position).model_copy(
        update={"capital_utilization": Decimal("0.90")}
    )

    orders = strategy.exit_rules(make_market_data(contract), portfolio_state)

    assert len(orders) == 1
    assert orders[0].position_id == position.position_id
    assert orders[0].side is TradeSide.BUY
    assert orders[0].event_type is BacktestOrderType.CLOSE


def test_portfolio_state_can_be_created_from_backtest_account_snapshot() -> None:
    contract = make_contract()
    position = make_position(contract)
    snapshot = BacktestAccountSnapshot(
        date=date(2026, 6, 10),
        cash_balance=Decimal("100200"),
        realized_pnl=Decimal("50"),
        unrealized_pnl=Decimal("-25"),
        capital_utilization=Decimal("0.25"),
        equity=Decimal("100175"),
        open_positions=(position,),
    )

    portfolio_state = PortfolioState.from_account_snapshot(snapshot)

    assert portfolio_state.date == snapshot.date
    assert portfolio_state.cash_balance == snapshot.cash_balance
    assert portfolio_state.realized_pnl == snapshot.realized_pnl
    assert portfolio_state.unrealized_pnl == snapshot.unrealized_pnl
    assert portfolio_state.capital_utilization == snapshot.capital_utilization
    assert portfolio_state.equity == snapshot.equity
    assert portfolio_state.open_positions == (position,)


def test_strategy_signal_validates_confidence_range() -> None:
    with pytest.raises(ValidationError):
        StrategySignal(signal_type=StrategySignalType.HOLD, confidence=Decimal("1.5"))


def test_strategy_market_data_keeps_typed_market_context() -> None:
    contract = make_contract()
    market_data = make_market_data(contract)

    assert market_data.date == date(2026, 6, 10)
    assert market_data.underlying_prices["AAPL"].price == Decimal("101")
    assert market_data.option_chains["AAPL"].contracts == (contract,)
    assert market_data.option_quotes[contract].mark == Decimal("2.00")
