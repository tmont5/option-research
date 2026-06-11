"""Short put options strategy."""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from options_quant.backtest import (
    BacktestOrderEvent,
    BacktestOrderType,
    BacktestPosition,
)
from options_quant.data.models import OptionType, TradeSide
from options_quant.strategies.base import (
    OptionsStrategy,
    PortfolioState,
    StrategyMarketData,
    StrategyModel,
    StrategySignal,
    StrategySignalType,
)
from options_quant.strategies.selection import ContractSelectionEngine, OptionSelectionQuery


class ShortPutStrategyConfig(StrategyModel):
    """Configuration for a simple short put strategy."""

    underlying_symbol: str = Field(default="SPY", min_length=1, description="Underlying symbol.")
    min_dte: int = Field(default=30, ge=0, description="Minimum DTE, inclusive.")
    max_dte: int = Field(default=45, ge=0, description="Maximum DTE, inclusive.")
    target_delta: Decimal = Field(
        default=Decimal("-0.10"),
        ge=Decimal("-1"),
        le=Decimal("0"),
        description="Target short put delta.",
    )
    position_size: int = Field(default=1, gt=0, description="Contracts to sell per entry.")
    take_profit_pct: Decimal = Field(
        default=Decimal("0.50"),
        gt=Decimal("0"),
        le=Decimal("1"),
        description="Fraction of entry credit captured before taking profit.",
    )
    stop_loss_pct: Decimal = Field(
        default=Decimal("1.00"),
        gt=Decimal("0"),
        description="Fractional loss over entry credit that triggers stop loss.",
    )

    @model_validator(mode="after")
    def validate_dte_range(self) -> Self:
        """Ensure the configured DTE range is valid."""
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must be less than or equal to max_dte")
        return self


class ShortPutStrategy(OptionsStrategy):
    """Sell SPY puts around 30-45 DTE nearest a target delta."""

    def __init__(self, config: ShortPutStrategyConfig | None = None) -> None:
        self.config = config if config is not None else ShortPutStrategyConfig()

    def generate_signals(
        self,
        market_data: StrategyMarketData,
        portfolio_state: PortfolioState,
    ) -> tuple[StrategySignal, ...]:
        """Generate one short put entry signal when no SPY position is open."""
        if self._has_open_underlying_position(portfolio_state):
            return (
                StrategySignal(
                    signal_type=StrategySignalType.HOLD,
                    reason="existing SPY option position is open",
                ),
            )
        chain = market_data.option_chains.get(self.config.underlying_symbol)
        underlying = market_data.underlying_prices.get(self.config.underlying_symbol)
        if chain is None or underlying is None:
            return (
                StrategySignal(
                    signal_type=StrategySignalType.HOLD,
                    reason="missing SPY chain or underlying price",
                ),
            )

        greeks = [
            greek
            for greek in market_data.option_greeks.values()
            if greek.contract.underlying_symbol == self.config.underlying_symbol
        ]
        implied_volatilities = [
            iv
            for iv in market_data.implied_volatilities.values()
            if iv.contract.underlying_symbol == self.config.underlying_symbol
        ]
        engine = ContractSelectionEngine(
            chain,
            underlying.price,
            as_of_date=market_data.date,
            greeks=greeks,
            implied_volatilities=implied_volatilities,
        )
        candidate = engine.best(
            OptionSelectionQuery(
                option_type=OptionType.PUT,
                min_dte=self.config.min_dte,
                max_dte=self.config.max_dte,
                target_delta=self.config.target_delta,
            )
        )
        if candidate is None:
            return (
                StrategySignal(
                    signal_type=StrategySignalType.HOLD,
                    reason="no SPY put matched DTE and delta requirements",
                ),
            )

        return (
            StrategySignal(
                signal_type=StrategySignalType.ENTER_SHORT,
                contract=candidate.contract,
                target_quantity=self.config.position_size,
                reason=(
                    f"selected SPY put with {candidate.dte} DTE "
                    f"and delta {candidate.delta}"
                ),
            ),
        )

    def manage_positions(
        self,
        market_data: StrategyMarketData,
        portfolio_state: PortfolioState,
        signals: tuple[StrategySignal, ...],
    ) -> tuple[BacktestOrderEvent, ...]:
        """Convert short entry signals into sell-to-open order events."""
        del portfolio_state
        orders: list[BacktestOrderEvent] = []
        for signal in signals:
            if signal.signal_type is not StrategySignalType.ENTER_SHORT or signal.contract is None:
                continue
            quote = market_data.option_quotes[signal.contract]
            orders.append(
                BacktestOrderEvent(
                    contract=signal.contract,
                    side=TradeSide.SELL,
                    quantity=signal.target_quantity or self.config.position_size,
                    price=quote.mark if quote.mark is not None else quote.bid,
                    event_type=BacktestOrderType.OPEN,
                )
            )
        return tuple(orders)

    def exit_rules(
        self,
        market_data: StrategyMarketData,
        portfolio_state: PortfolioState,
    ) -> tuple[BacktestOrderEvent, ...]:
        """Close short puts at configured take-profit or stop-loss thresholds."""
        orders: list[BacktestOrderEvent] = []
        for position in portfolio_state.open_positions:
            if not self._is_strategy_position(position):
                continue
            mark = self._mark_for_position(position, market_data)
            take_profit_price = position.entry_fill_price * (
                Decimal("1") - self.config.take_profit_pct
            )
            stop_loss_price = position.entry_fill_price * (Decimal("1") + self.config.stop_loss_pct)
            if mark <= take_profit_price or mark >= stop_loss_price:
                orders.append(
                    BacktestOrderEvent(
                        contract=position.contract,
                        side=TradeSide.BUY,
                        quantity=position.absolute_quantity,
                        price=mark,
                        event_type=BacktestOrderType.CLOSE,
                        position_id=position.position_id,
                    )
                )
        return tuple(orders)

    def _has_open_underlying_position(self, portfolio_state: PortfolioState) -> bool:
        return any(
            self._is_strategy_position(position)
            for position in portfolio_state.open_positions
        )

    def _is_strategy_position(self, position: BacktestPosition) -> bool:
        return (
            position.contract.underlying_symbol == self.config.underlying_symbol
            and position.contract.option_type is OptionType.PUT
            and position.quantity < 0
        )

    @staticmethod
    def _mark_for_position(
        position: BacktestPosition,
        market_data: StrategyMarketData,
    ) -> Decimal:
        quote = market_data.option_quotes[position.contract]
        if quote.mark is not None:
            return quote.mark
        return (quote.bid + quote.ask) / Decimal("2")
