from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from options_quant.strategies.wheel import (
    WheelCoveredCallStrikePolicy,
    WheelStrategyConfig,
)


def test_wheel_strategy_defaults_match_v1_contract() -> None:
    config = WheelStrategyConfig()

    assert config.underlying_symbol == "SPY"
    assert config.initial_cash == Decimal("100000")
    assert config.contract_quantity == 1
    assert config.share_lot_size == 100
    assert config.put_min_dte == 30
    assert config.put_max_dte == 60
    assert config.put_target_delta == Decimal("-0.10")
    assert config.call_min_dte == 30
    assert config.call_max_dte == 45
    assert config.call_target_delta == Decimal("0.20")
    assert config.call_strike_policy is WheelCoveredCallStrikePolicy.ABOVE_COST_BASIS_DELTA_TARGET
    assert config.sell_puts_only_when_flat
    assert config.sell_calls_only_when_assigned
    assert config.require_cash_secured_puts
    assert config.require_covered_calls
    assert not config.allow_realized_stock_loss


def test_wheel_strategy_validates_dte_ranges() -> None:
    with pytest.raises(ValidationError, match="put_min_dte must be less than or equal"):
        WheelStrategyConfig(put_min_dte=61, put_max_dte=30)

    with pytest.raises(ValidationError, match="call_min_dte must be less than or equal"):
        WheelStrategyConfig(call_min_dte=46, call_max_dte=30)


def test_wheel_strategy_v1_rejects_unsecured_or_uncovered_variants() -> None:
    with pytest.raises(ValidationError, match="wheel v1 requires cash-secured puts"):
        WheelStrategyConfig(require_cash_secured_puts=False)

    with pytest.raises(ValidationError, match="wheel v1 requires covered calls"):
        WheelStrategyConfig(require_covered_calls=False)
