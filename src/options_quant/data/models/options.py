"""Pydantic models for options research data.

The models in this module are intentionally limited to validation and data
shape. They do not price options, calculate Greeks, or manage portfolio state.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class OptionsQuantModel(BaseModel):
    """Base model configuration shared by research data models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class OptionType(StrEnum):
    """Supported listed option contract types."""

    CALL = "call"
    PUT = "put"


class OptionStyle(StrEnum):
    """Supported option exercise styles."""

    AMERICAN = "american"
    EUROPEAN = "european"


class TradeSide(StrEnum):
    """Direction of an executed option trade."""

    BUY = "buy"
    SELL = "sell"


class UnderlyingPrice(OptionsQuantModel):
    """Point-in-time market data for an underlying instrument."""

    symbol: str = Field(description="Ticker or provider symbol for the underlying.")
    timestamp: AwareDatetime = Field(description="Timezone-aware observation timestamp.")
    price: Decimal = Field(gt=Decimal("0"), description="Last or reference underlying price.")
    bid: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        description="Best bid for the underlying, when available.",
    )
    ask: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        description="Best ask for the underlying, when available.",
    )
    volume: int | None = Field(default=None, ge=0, description="Underlying session volume.")

    @model_validator(mode="after")
    def validate_market(self) -> Self:
        """Ensure bid/ask fields are internally consistent when both are present."""
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid must be less than or equal to ask")
        return self


class OptionContract(OptionsQuantModel):
    """Listed option contract identity."""

    underlying_symbol: str = Field(description="Ticker or provider symbol for the underlying.")
    expiration: date = Field(description="Contract expiration date.")
    strike: Decimal = Field(gt=Decimal("0"), description="Contract strike price.")
    option_type: OptionType = Field(description="Call or put contract type.")
    multiplier: int = Field(default=100, gt=0, description="Contract share multiplier.")
    style: OptionStyle = Field(
        default=OptionStyle.AMERICAN,
        description="Contract exercise style.",
    )


class OptionChain(OptionsQuantModel):
    """Point-in-time list of option contracts available for an underlying."""

    underlying_symbol: str = Field(description="Ticker or provider symbol for the underlying.")
    timestamp: AwareDatetime = Field(description="Timezone-aware chain observation timestamp.")
    contracts: tuple[OptionContract, ...] = Field(
        min_length=1,
        description="Option contracts available in this chain snapshot.",
    )

    @model_validator(mode="after")
    def validate_contract_symbols(self) -> Self:
        """Ensure chain contracts all belong to the chain underlying."""
        for contract in self.contracts:
            if contract.underlying_symbol != self.underlying_symbol:
                raise ValueError(
                    "chain contract underlying_symbol must match chain underlying_symbol"
                )
        return self


class OptionQuote(OptionsQuantModel):
    """Point-in-time quote and liquidity data for an option contract."""

    contract: OptionContract = Field(description="Quoted option contract.")
    timestamp: AwareDatetime = Field(description="Timezone-aware quote timestamp.")
    bid: Decimal = Field(ge=Decimal("0"), description="Best option bid.")
    ask: Decimal = Field(ge=Decimal("0"), description="Best option ask.")
    last: Decimal | None = Field(default=None, ge=Decimal("0"), description="Last traded price.")
    mark: Decimal | None = Field(default=None, ge=Decimal("0"), description="Provider mark price.")
    volume: int | None = Field(default=None, ge=0, description="Option session volume.")
    open_interest: int | None = Field(default=None, ge=0, description="Open interest.")

    @model_validator(mode="after")
    def validate_quote(self) -> Self:
        """Ensure bid/ask fields are internally consistent."""
        if self.bid > self.ask:
            raise ValueError("bid must be less than or equal to ask")
        return self


class OptionGreek(OptionsQuantModel):
    """Point-in-time Greek and implied volatility data for an option contract."""

    contract: OptionContract = Field(description="Option contract associated with the Greeks.")
    timestamp: AwareDatetime = Field(description="Timezone-aware Greek observation timestamp.")
    delta: Decimal | None = Field(
        default=None,
        ge=Decimal("-1"),
        le=Decimal("1"),
        description="Option delta.",
    )
    gamma: Decimal | None = Field(default=None, ge=Decimal("0"), description="Option gamma.")
    theta: Decimal | None = Field(default=None, description="Option theta.")
    vega: Decimal | None = Field(default=None, ge=Decimal("0"), description="Option vega.")
    rho: Decimal | None = Field(default=None, description="Option rho.")
    implied_volatility: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        description="Annualized implied volatility as a decimal.",
    )


class OptionImpliedVolatility(OptionsQuantModel):
    """Point-in-time implied volatility observation for an option contract."""

    contract: OptionContract = Field(description="Option contract associated with the IV.")
    timestamp: AwareDatetime = Field(description="Timezone-aware IV observation timestamp.")
    implied_volatility: Decimal = Field(
        gt=Decimal("0"),
        description="Annualized implied volatility as a decimal.",
    )


class OptionOpenInterest(OptionsQuantModel):
    """Point-in-time open interest observation for an option contract."""

    contract: OptionContract = Field(description="Option contract associated with open interest.")
    timestamp: AwareDatetime = Field(
        description="Timezone-aware open interest observation timestamp."
    )
    open_interest: int = Field(ge=0, description="Open interest.")


class OptionSnapshot(OptionsQuantModel):
    """Consistent snapshot joining underlying, option quote, and optional Greeks."""

    underlying: UnderlyingPrice = Field(description="Underlying market data.")
    contract: OptionContract = Field(description="Option contract identity.")
    quote: OptionQuote = Field(description="Option quote for the contract.")
    greek: OptionGreek | None = Field(default=None, description="Optional Greek data.")
    timestamp: AwareDatetime = Field(description="Timezone-aware snapshot timestamp.")

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        """Ensure all nested option data references the same contract and underlying."""
        if self.underlying.symbol != self.contract.underlying_symbol:
            raise ValueError("underlying symbol must match contract underlying_symbol")
        if self.quote.contract != self.contract:
            raise ValueError("quote contract must match snapshot contract")
        if self.greek is not None and self.greek.contract != self.contract:
            raise ValueError("greek contract must match snapshot contract")
        return self


class Trade(OptionsQuantModel):
    """Executed option trade."""

    trade_id: UUID = Field(default_factory=uuid4, description="Unique trade identifier.")
    contract: OptionContract = Field(description="Traded option contract.")
    side: TradeSide = Field(description="Buy or sell direction.")
    quantity: int = Field(gt=0, description="Number of contracts traded.")
    price: Decimal = Field(ge=Decimal("0"), description="Execution price per contract.")
    timestamp: AwareDatetime = Field(description="Timezone-aware execution timestamp.")
    fees: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), description="Total fees paid.")
    notes: str | None = Field(default=None, description="Optional trade notes.")


class Position(OptionsQuantModel):
    """Current option position state."""

    contract: OptionContract = Field(description="Position option contract.")
    quantity: int = Field(description="Signed contract quantity; positive long, negative short.")
    average_price: Decimal = Field(
        ge=Decimal("0"),
        description="Average entry price per contract.",
    )
    opened_at: AwareDatetime = Field(description="Timezone-aware position open timestamp.")
    updated_at: AwareDatetime | None = Field(
        default=None,
        description="Timezone-aware last update timestamp.",
    )
    realized_pnl: Decimal = Field(
        default=Decimal("0"),
        description="Realized profit and loss for closed lots.",
    )

    @model_validator(mode="after")
    def validate_position(self) -> Self:
        """Ensure position quantity and timestamps are internally consistent."""
        if self.quantity == 0:
            raise ValueError("quantity must be non-zero for an open position")
        if self.updated_at is not None and self.updated_at < self.opened_at:
            raise ValueError("updated_at must be greater than or equal to opened_at")
        return self
