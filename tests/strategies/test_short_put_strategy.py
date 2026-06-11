from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from options_quant.backtest import BacktestOrderType, BacktestPosition
from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionQuote,
    OptionType,
    TradeSide,
    UnderlyingPrice,
)
from options_quant.strategies import (
    PortfolioState,
    ShortPutStrategy,
    ShortPutStrategyConfig,
    StrategyMarketData,
    StrategySignalType,
)

AS_OF = date(2026, 6, 10)
OBSERVED_AT = datetime(2026, 6, 10, 14, 30, tzinfo=UTC)


def make_contract(
    symbol: str = "SPY",
    expiration: date = date(2026, 7, 18),
    strike: Decimal = Decimal("500"),
    option_type: OptionType = OptionType.PUT,
) -> OptionContract:
    return OptionContract(
        underlying_symbol=symbol,
        expiration=expiration,
        strike=strike,
        option_type=option_type,
    )


SPY_30_DTE = make_contract(expiration=date(2026, 7, 10), strike=Decimal("505"))
SPY_38_DTE = make_contract(expiration=date(2026, 7, 18), strike=Decimal("500"))
SPY_45_DTE = make_contract(expiration=date(2026, 7, 25), strike=Decimal("495"))
SPY_60_DTE = make_contract(expiration=date(2026, 8, 9), strike=Decimal("480"))
SPY_CALL = make_contract(
    expiration=date(2026, 7, 18),
    strike=Decimal("530"),
    option_type=OptionType.CALL,
)
QQQ_PUT = make_contract(
    symbol="QQQ",
    expiration=date(2026, 7, 18),
    strike=Decimal("450"),
)


def make_portfolio_state(
    open_positions: tuple[BacktestPosition, ...] = (),
    *,
    capital_utilization: Decimal = Decimal("0"),
) -> PortfolioState:
    return PortfolioState(
        date=AS_OF,
        cash_balance=Decimal("100000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        capital_utilization=capital_utilization,
        equity=Decimal("100000"),
        open_positions=open_positions,
    )


def make_market_data(
    contracts: tuple[OptionContract, ...] = (SPY_30_DTE, SPY_38_DTE, SPY_45_DTE, SPY_60_DTE),
) -> StrategyMarketData:
    greeks = {
        SPY_30_DTE: OptionGreek(
            contract=SPY_30_DTE,
            timestamp=OBSERVED_AT,
            delta=Decimal("-0.18"),
        ),
        SPY_38_DTE: OptionGreek(
            contract=SPY_38_DTE,
            timestamp=OBSERVED_AT,
            delta=Decimal("-0.11"),
        ),
        SPY_45_DTE: OptionGreek(
            contract=SPY_45_DTE,
            timestamp=OBSERVED_AT,
            delta=Decimal("-0.09"),
        ),
        SPY_60_DTE: OptionGreek(
            contract=SPY_60_DTE,
            timestamp=OBSERVED_AT,
            delta=Decimal("-0.10"),
        ),
    }
    quotes = {
        contract: OptionQuote(
            contract=contract,
            timestamp=OBSERVED_AT,
            bid=Decimal("1.90"),
            ask=Decimal("2.10"),
            mark=Decimal("2.00"),
        )
        for contract in contracts
    }
    return StrategyMarketData(
        date=AS_OF,
        underlying_prices={
            "SPY": UnderlyingPrice(
                symbol="SPY",
                timestamp=OBSERVED_AT,
                price=Decimal("520"),
            )
        },
        option_chains={
            "SPY": OptionChain(
                underlying_symbol="SPY",
                timestamp=OBSERVED_AT,
                contracts=contracts,
            )
        },
        option_quotes=quotes,
        option_greeks={
            contract: greek for contract, greek in greeks.items() if contract in contracts
        },
    )


def make_position(
    contract: OptionContract = SPY_38_DTE,
    *,
    entry_fill_price: Decimal = Decimal("2.00"),
) -> BacktestPosition:
    return BacktestPosition(
        position_id=uuid4(),
        contract=contract,
        quantity=-1,
        entry_price=entry_fill_price,
        entry_fill_price=entry_fill_price,
        entry_date=AS_OF,
        entry_cash_flow=entry_fill_price * Decimal("100"),
        entry_commission=Decimal("0"),
    )


def test_generate_signals_selects_spy_put_between_30_and_45_dte_closest_to_target_delta() -> None:
    strategy = ShortPutStrategy()

    signals = strategy.generate_signals(make_market_data(), make_portfolio_state())

    assert len(signals) == 1
    assert signals[0].signal_type is StrategySignalType.ENTER_SHORT
    assert signals[0].contract == SPY_38_DTE
    assert signals[0].target_quantity == 1
    assert "38 DTE" in (signals[0].reason or "")
    assert "-0.11" in (signals[0].reason or "")


def test_generate_signals_uses_configurable_position_size() -> None:
    strategy = ShortPutStrategy(ShortPutStrategyConfig(position_size=3))

    signals = strategy.generate_signals(make_market_data(), make_portfolio_state())

    assert signals[0].target_quantity == 3


def test_generate_signals_returns_hold_when_existing_spy_short_put_is_open() -> None:
    strategy = ShortPutStrategy()
    position = make_position()

    signals = strategy.generate_signals(make_market_data(), make_portfolio_state((position,)))

    assert signals[0].signal_type is StrategySignalType.HOLD
    assert signals[0].contract is None


def test_generate_signals_ignores_non_spy_and_call_contracts() -> None:
    strategy = ShortPutStrategy()
    spy_market_data = make_market_data((SPY_CALL, SPY_45_DTE))
    market_data = spy_market_data.model_copy(
        update={
            "option_quotes": {
                **spy_market_data.option_quotes,
                QQQ_PUT: OptionQuote(
                    contract=QQQ_PUT,
                    timestamp=OBSERVED_AT,
                    bid=Decimal("1.90"),
                    ask=Decimal("2.10"),
                    mark=Decimal("2.00"),
                ),
            },
            "option_greeks": {
                **spy_market_data.option_greeks,
                QQQ_PUT: OptionGreek(
                    contract=QQQ_PUT,
                    timestamp=OBSERVED_AT,
                    delta=Decimal("-0.10"),
                ),
            },
        }
    )

    signals = strategy.generate_signals(market_data, make_portfolio_state())

    assert signals[0].signal_type is StrategySignalType.ENTER_SHORT
    assert signals[0].contract == SPY_45_DTE


def test_generate_signals_returns_hold_when_required_market_data_missing() -> None:
    strategy = ShortPutStrategy()
    market_data = StrategyMarketData(date=AS_OF)

    signals = strategy.generate_signals(market_data, make_portfolio_state())

    assert signals[0].signal_type is StrategySignalType.HOLD
    assert "missing SPY" in (signals[0].reason or "")


def test_generate_signals_returns_hold_when_no_contract_matches_dte_delta_requirements() -> None:
    strategy = ShortPutStrategy()
    market_data = make_market_data((SPY_60_DTE,))

    signals = strategy.generate_signals(market_data, make_portfolio_state())

    assert signals[0].signal_type is StrategySignalType.HOLD
    assert "no SPY put" in (signals[0].reason or "")


def test_manage_positions_converts_entry_signal_to_sell_order_with_configured_size() -> None:
    strategy = ShortPutStrategy(ShortPutStrategyConfig(position_size=2))
    market_data = make_market_data()
    signals = strategy.generate_signals(market_data, make_portfolio_state())

    orders = strategy.manage_positions(market_data, make_portfolio_state(), signals)

    assert len(orders) == 1
    assert orders[0].contract == SPY_38_DTE
    assert orders[0].side is TradeSide.SELL
    assert orders[0].quantity == 2
    assert orders[0].price == Decimal("2.00")
    assert orders[0].event_type is BacktestOrderType.OPEN


def test_exit_rules_emit_take_profit_close_order() -> None:
    strategy = ShortPutStrategy(ShortPutStrategyConfig(take_profit_pct=Decimal("0.50")))
    position = make_position(entry_fill_price=Decimal("2.00"))
    market_data = make_market_data((SPY_38_DTE,))
    market_data = market_data.model_copy(
        update={
            "option_quotes": {
                SPY_38_DTE: OptionQuote(
                    contract=SPY_38_DTE,
                    timestamp=OBSERVED_AT,
                    bid=Decimal("0.95"),
                    ask=Decimal("1.05"),
                    mark=Decimal("1.00"),
                )
            }
        }
    )

    orders = strategy.exit_rules(market_data, make_portfolio_state((position,)))

    assert len(orders) == 1
    assert orders[0].side is TradeSide.BUY
    assert orders[0].event_type is BacktestOrderType.CLOSE
    assert orders[0].position_id == position.position_id


def test_exit_rules_emit_stop_loss_close_order() -> None:
    strategy = ShortPutStrategy(ShortPutStrategyConfig(stop_loss_pct=Decimal("1.00")))
    position = make_position(entry_fill_price=Decimal("2.00"))
    market_data = make_market_data((SPY_38_DTE,))
    market_data = market_data.model_copy(
        update={
            "option_quotes": {
                SPY_38_DTE: OptionQuote(
                    contract=SPY_38_DTE,
                    timestamp=OBSERVED_AT,
                    bid=Decimal("3.90"),
                    ask=Decimal("4.10"),
                    mark=Decimal("4.00"),
                )
            }
        }
    )

    orders = strategy.exit_rules(market_data, make_portfolio_state((position,)))

    assert len(orders) == 1
    assert orders[0].side is TradeSide.BUY
    assert orders[0].price == Decimal("4.00")


def test_exit_rules_hold_when_profit_and_loss_thresholds_are_not_hit() -> None:
    strategy = ShortPutStrategy()
    position = make_position(entry_fill_price=Decimal("2.00"))

    orders = strategy.exit_rules(make_market_data((SPY_38_DTE,)), make_portfolio_state((position,)))

    assert orders == ()


def test_config_rejects_invalid_dte_range() -> None:
    with pytest.raises(ValidationError, match="min_dte must be less than or equal to max_dte"):
        ShortPutStrategyConfig(min_dte=45, max_dte=30)
