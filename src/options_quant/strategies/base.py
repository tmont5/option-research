"""Base interfaces for extensible options strategies."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from options_quant.backtest import BacktestAccountSnapshot, BacktestOrderEvent, BacktestPosition
from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionImpliedVolatility,
    OptionQuote,
    UnderlyingPrice,
)

ZERO = Decimal("0")


class StrategyModel(BaseModel):
    """Base configuration for immutable strategy value objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategySignalType(StrEnum):
    """High-level signal intents emitted by a strategy."""

    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"
    HOLD = "hold"


class StrategyMarketData(StrategyModel):
    """Market data snapshot provided to a strategy."""

    date: dt.date = Field(description="Strategy evaluation date.")
    underlying_prices: dict[str, UnderlyingPrice] = Field(
        default_factory=dict,
        description="Underlying observations keyed by symbol.",
    )
    option_chains: dict[str, OptionChain] = Field(
        default_factory=dict,
        description="Option chains keyed by underlying symbol.",
    )
    option_quotes: dict[OptionContract, OptionQuote] = Field(
        default_factory=dict,
        description="Option quotes keyed by contract.",
    )
    option_greeks: dict[OptionContract, OptionGreek] = Field(
        default_factory=dict,
        description="Option Greeks keyed by contract.",
    )
    implied_volatilities: dict[OptionContract, OptionImpliedVolatility] = Field(
        default_factory=dict,
        description="Implied volatility observations keyed by contract.",
    )


class PortfolioState(StrategyModel):
    """Portfolio state provided to a strategy."""

    date: dt.date = Field(description="Portfolio snapshot date.")
    cash_balance: Decimal = Field(description="Cash balance.")
    realized_pnl: Decimal = Field(description="Cumulative realized PnL.")
    unrealized_pnl: Decimal = Field(description="Current unrealized PnL.")
    capital_utilization: Decimal = Field(ge=ZERO, description="Capital utilization.")
    equity: Decimal = Field(description="Cash plus marked value of open positions.")
    open_positions: tuple[BacktestPosition, ...] = Field(description="Open option positions.")

    @classmethod
    def from_account_snapshot(cls, snapshot: BacktestAccountSnapshot) -> PortfolioState:
        """Create strategy portfolio state from a backtest account snapshot."""
        return cls(
            date=snapshot.date,
            cash_balance=snapshot.cash_balance,
            realized_pnl=snapshot.realized_pnl,
            unrealized_pnl=snapshot.unrealized_pnl,
            capital_utilization=snapshot.capital_utilization,
            equity=snapshot.equity,
            open_positions=snapshot.open_positions,
        )


class StrategySignal(StrategyModel):
    """Typed signal emitted by an options strategy."""

    signal_id: UUID = Field(default_factory=uuid4, description="Unique signal identifier.")
    signal_type: StrategySignalType = Field(description="Signal intent.")
    contract: OptionContract | None = Field(
        default=None,
        description="Contract associated with the signal, when applicable.",
    )
    target_quantity: int | None = Field(
        default=None,
        gt=0,
        description="Target contract quantity for actionable signals.",
    )
    confidence: Decimal | None = Field(
        default=None,
        ge=ZERO,
        le=Decimal("1"),
        description="Optional normalized confidence score.",
    )
    reason: str | None = Field(default=None, description="Human-readable signal rationale.")


class OptionsStrategy(ABC):
    """Abstract base class for options strategy implementations."""

    @abstractmethod
    def generate_signals(
        self,
        market_data: StrategyMarketData,
        portfolio_state: PortfolioState,
    ) -> tuple[StrategySignal, ...]:
        """Generate strategy signals from market data and current portfolio state."""

    @abstractmethod
    def manage_positions(
        self,
        market_data: StrategyMarketData,
        portfolio_state: PortfolioState,
        signals: tuple[StrategySignal, ...],
    ) -> tuple[BacktestOrderEvent, ...]:
        """Convert signals and current positions into order events."""

    @abstractmethod
    def exit_rules(
        self,
        market_data: StrategyMarketData,
        portfolio_state: PortfolioState,
    ) -> tuple[BacktestOrderEvent, ...]:
        """Return exit order events required by the strategy's risk rules."""
