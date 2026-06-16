from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from options_quant.strategies.scanner_put import (
    ScannerPortfolioRules,
    ScannerStylePutStrategyConfig,
    ScannerUniverseEntry,
    StockQualityTier,
    TierRule,
)


def test_scanner_style_put_defaults_match_v1_contract() -> None:
    config = ScannerStylePutStrategyConfig()

    assert len(config.universe) == 40
    assert config.symbols_for_tier(StockQualityTier.A) == (
        "AAPL",
        "MSFT",
        "GOOGL",
        "AMZN",
        "META",
        "NVDA",
        "AVGO",
        "JPM",
        "V",
        "MA",
        "COST",
        "LLY",
        "UNH",
        "XOM",
        "BRK-B",
    )
    assert config.symbols_for_tier(StockQualityTier.B) == (
        "AMD",
        "QCOM",
        "CRM",
        "ORCL",
        "NFLX",
        "TSM",
        "WMT",
        "HD",
        "MCD",
        "CAT",
        "GE",
        "RTX",
        "GS",
        "MS",
        "CVX",
    )
    assert config.symbols_for_tier(StockQualityTier.C) == (
        "TSLA",
        "PLTR",
        "COIN",
        "MSTR",
        "SOFI",
        "HOOD",
        "SHOP",
        "UBER",
        "MU",
        "INTC",
    )

    assert config.put_entry.min_dte == 20
    assert config.put_entry.max_dte == 35
    assert config.put_entry.max_delta == Decimal("0.28")
    assert config.put_entry.base_min_monthly_yield == Decimal("0.025")
    assert config.put_entry.avoid_earnings_before_expiration
    assert config.put_entry.technicals.require_strike_at_or_below_support
    assert config.put_entry.technicals.require_pullback_not_breakdown
    assert config.put_entry.liquidity.reject_nonstandard_deliverables

    assert config.covered_call.min_monthly_yield == Decimal("0.02")
    assert config.covered_call.min_strike_above_breakeven_pct == Decimal("0.05")
    assert config.covered_call.max_strike_above_breakeven_pct == Decimal("0.10")

    assert config.exits.close_at_halfway_profit_capture == Decimal("0.50")
    assert config.exits.close_at_three_weeks_profit_capture == Decimal("0.75")
    assert config.exits.close_near_expiration_min_profit_capture == Decimal("0.90")
    assert config.exits.close_near_expiration_target_profit_capture == Decimal("0.95")
    assert config.exits.wheel_after_assignment
    assert config.exits.test_assignment_vs_roll


def test_scanner_style_tier_rules_capture_risk_return_tradeoffs() -> None:
    config = ScannerStylePutStrategyConfig()

    tier_a = config.tier_rules[StockQualityTier.A]
    tier_b = config.tier_rules[StockQualityTier.B]
    tier_c = config.tier_rules[StockQualityTier.C]

    assert tier_a.min_put_monthly_yield == Decimal("0.020")
    assert tier_a.target_put_monthly_yield == Decimal("0.025")
    assert tier_a.allow_yield_flex_for_excellent_structure
    assert tier_b.min_put_monthly_yield == Decimal("0.025")
    assert tier_b.target_put_monthly_yield == Decimal("0.025")
    assert tier_c.min_put_monthly_yield == Decimal("0.030")
    assert tier_c.target_put_monthly_yield == Decimal("0.040")
    assert tier_c.max_delta == Decimal("0.25")
    assert config.portfolio.max_tier_c_open_positions == 1
    assert config.portfolio.tier_c_one_contract_cap


def test_scanner_style_config_rejects_duplicate_symbols() -> None:
    with pytest.raises(ValidationError, match="universe symbols must be unique"):
        ScannerStylePutStrategyConfig(
            universe=(
                ScannerUniverseEntry(symbol="AAPL", tier=StockQualityTier.A),
                ScannerUniverseEntry(symbol="AAPL", tier=StockQualityTier.B),
            )
        )


def test_scanner_style_config_rejects_oversized_universe() -> None:
    universe = tuple(
        ScannerUniverseEntry(symbol=f"TICKER{index}", tier=StockQualityTier.A)
        for index in range(41)
    )

    with pytest.raises(ValidationError, match="universe exceeds max_universe_size"):
        ScannerStylePutStrategyConfig(universe=universe)


def test_scanner_style_config_requires_rules_for_used_tiers() -> None:
    with pytest.raises(ValidationError, match="tier_rules missing tiers"):
        ScannerStylePutStrategyConfig(
            universe=(ScannerUniverseEntry(symbol="TSLA", tier=StockQualityTier.C),),
            tier_rules={
                StockQualityTier.A: TierRule(
                    min_put_monthly_yield=Decimal("0.02"),
                    target_put_monthly_yield=Decimal("0.025"),
                    max_delta=Decimal("0.28"),
                )
            },
        )


def test_scanner_style_rules_validate_ranges() -> None:
    with pytest.raises(ValidationError, match="target_put_monthly_yield"):
        TierRule(
            min_put_monthly_yield=Decimal("0.03"),
            target_put_monthly_yield=Decimal("0.02"),
            max_delta=Decimal("0.28"),
        )

    with pytest.raises(ValidationError, match="top_n_to_publish"):
        ScannerPortfolioRules(max_candidates_per_run=4, top_n_to_publish=5)
