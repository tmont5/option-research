"""Configuration contract for a wheel options strategy."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from options_quant.strategies.base import StrategyModel

ZERO = Decimal("0")


class WheelAssignmentPolicy(StrEnum):
    """How the wheel handles short puts that expire in the money."""

    HOLD_SHARES = "hold_shares"


class WheelCoveredCallStrikePolicy(StrEnum):
    """How covered-call strikes are selected after assignment."""

    ABOVE_COST_BASIS_DELTA_TARGET = "above_cost_basis_delta_target"


class WheelStrategyConfig(StrategyModel):
    """Machine-readable definition of the intended wheel strategy."""

    underlying_symbol: str = Field(default="SPY", min_length=1)
    initial_cash: Decimal = Field(default=Decimal("100000"), gt=ZERO)
    contract_quantity: int = Field(default=1, gt=0)

    put_min_dte: int = Field(default=30, ge=0)
    put_max_dte: int = Field(default=60, ge=0)
    put_target_delta: Decimal = Field(default=Decimal("-0.10"), ge=Decimal("-1"), le=ZERO)

    call_min_dte: int = Field(default=30, ge=0)
    call_max_dte: int = Field(default=45, ge=0)
    call_target_delta: Decimal = Field(default=Decimal("0.20"), ge=ZERO, le=Decimal("1"))
    call_strike_policy: WheelCoveredCallStrikePolicy = Field(
        default=WheelCoveredCallStrikePolicy.ABOVE_COST_BASIS_DELTA_TARGET
    )

    assignment_policy: WheelAssignmentPolicy = Field(default=WheelAssignmentPolicy.HOLD_SHARES)
    sell_puts_only_when_flat: bool = Field(default=True)
    sell_calls_only_when_assigned: bool = Field(default=True)
    require_cash_secured_puts: bool = Field(default=True)
    require_covered_calls: bool = Field(default=True)
    allow_realized_stock_loss: bool = Field(default=False)

    take_profit_pct: Decimal | None = Field(default=None, gt=ZERO, le=Decimal("1"))
    stop_loss_pct: Decimal | None = Field(default=None, gt=ZERO)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        """Validate DTE ranges and core wheel constraints."""
        if self.put_min_dte > self.put_max_dte:
            raise ValueError("put_min_dte must be less than or equal to put_max_dte")
        if self.call_min_dte > self.call_max_dte:
            raise ValueError("call_min_dte must be less than or equal to call_max_dte")
        if not self.require_cash_secured_puts:
            raise ValueError("wheel v1 requires cash-secured puts")
        if not self.require_covered_calls:
            raise ValueError("wheel v1 requires covered calls")
        return self

    @property
    def share_lot_size(self) -> int:
        """Return the share count controlled by one wheel position."""
        return self.contract_quantity * 100
