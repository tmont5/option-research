"""Configuration contract for scanner-style cash-secured put research."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from options_quant.strategies.base import StrategyModel

ZERO = Decimal("0")
ONE = Decimal("1")


class StockQualityTier(StrEnum):
    """Ownership-quality buckets used by the scanner-style strategy."""

    A = "A"
    B = "B"
    C = "C"


class ScannerUniverseEntry(StrategyModel):
    """One tradable underlying and its ownership-quality tier."""

    symbol: str = Field(min_length=1)
    tier: StockQualityTier


class TierRule(StrategyModel):
    """Tier-specific risk/return requirements."""

    min_put_monthly_yield: Decimal = Field(ge=ZERO)
    target_put_monthly_yield: Decimal = Field(ge=ZERO)
    max_delta: Decimal = Field(gt=ZERO, le=ONE)
    allow_yield_flex_for_excellent_structure: bool = Field(default=False)
    max_contracts_per_symbol: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def validate_yield_range(self) -> Self:
        """Ensure target yield is not below the minimum acceptable yield."""
        if self.target_put_monthly_yield < self.min_put_monthly_yield:
            raise ValueError("target_put_monthly_yield must be >= min_put_monthly_yield")
        return self


class PutLiquidityRules(StrategyModel):
    """Minimum tradability requirements for put candidates."""

    min_bid: Decimal = Field(default=Decimal("0.10"), ge=ZERO)
    min_open_interest: int = Field(default=50, ge=0)
    min_option_volume: int = Field(default=0, ge=0)
    max_bid_ask_spread_pct: Decimal = Field(default=Decimal("0.12"), gt=ZERO, le=ONE)
    reject_nonstandard_deliverables: bool = Field(default=True)


class PutTechnicalRules(StrategyModel):
    """Technical-structure requirements for put entries."""

    prefer_recent_drawdown: bool = Field(default=True)
    require_strike_at_or_below_support: bool = Field(default=True)
    require_pullback_not_breakdown: bool = Field(default=True)
    require_no_recent_support_failure: bool = Field(default=True)
    max_rsi: Decimal = Field(default=Decimal("68"), gt=ZERO)
    max_atr_pct: Decimal = Field(default=Decimal("0.06"), gt=ZERO)
    support_lookback_days: int = Field(default=40, gt=0)
    support_tolerance_pct: Decimal = Field(default=Decimal("0.03"), ge=ZERO)


class PutEntryRules(StrategyModel):
    """Option-contract and event-risk rules for cash-secured put entries."""

    min_dte: int = Field(default=20, ge=0)
    max_dte: int = Field(default=35, ge=0)
    min_delta: Decimal = Field(default=ZERO, ge=ZERO, le=ONE)
    max_delta: Decimal = Field(default=Decimal("0.28"), gt=ZERO, le=ONE)
    base_min_monthly_yield: Decimal = Field(default=Decimal("0.025"), ge=ZERO)
    avoid_earnings_before_expiration: bool = Field(default=True)
    liquidity: PutLiquidityRules = Field(default_factory=PutLiquidityRules)
    technicals: PutTechnicalRules = Field(default_factory=PutTechnicalRules)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        """Validate DTE and delta bounds."""
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must be less than or equal to max_dte")
        if self.min_delta > self.max_delta:
            raise ValueError("min_delta must be less than or equal to max_delta")
        return self


class CoveredCallRules(StrategyModel):
    """Covered-call requirements after assignment."""

    min_monthly_yield: Decimal = Field(default=Decimal("0.02"), ge=ZERO)
    min_strike_above_breakeven_pct: Decimal = Field(default=Decimal("0.05"), ge=ZERO)
    max_strike_above_breakeven_pct: Decimal = Field(default=Decimal("0.10"), ge=ZERO)
    require_strike_above_breakeven: bool = Field(default=True)
    avoid_earnings_before_expiration: bool = Field(default=True)

    @model_validator(mode="after")
    def validate_strike_range(self) -> Self:
        """Ensure covered-call strike window is internally consistent."""
        if self.min_strike_above_breakeven_pct > self.max_strike_above_breakeven_pct:
            raise ValueError(
                "min_strike_above_breakeven_pct must be <= max_strike_above_breakeven_pct"
            )
        return self


class ExitManagementRules(StrategyModel):
    """Profit-taking and assignment/roll research assumptions."""

    close_at_halfway_profit_capture: Decimal = Field(default=Decimal("0.50"), gt=ZERO, le=ONE)
    close_at_three_weeks_profit_capture: Decimal = Field(default=Decimal("0.75"), gt=ZERO, le=ONE)
    close_near_expiration_min_profit_capture: Decimal = Field(
        default=Decimal("0.90"), gt=ZERO, le=ONE
    )
    close_near_expiration_target_profit_capture: Decimal = Field(
        default=Decimal("0.95"), gt=ZERO, le=ONE
    )
    three_week_check_days_held: int = Field(default=21, gt=0)
    test_assignment_vs_roll: bool = Field(default=True)
    wheel_after_assignment: bool = Field(default=True)

    @model_validator(mode="after")
    def validate_profit_capture_order(self) -> Self:
        """Ensure later exits require at least as much profit capture as earlier exits."""
        if self.close_at_three_weeks_profit_capture < self.close_at_halfway_profit_capture:
            raise ValueError(
                "close_at_three_weeks_profit_capture must be >= "
                "close_at_halfway_profit_capture"
            )
        if self.close_near_expiration_min_profit_capture < self.close_at_three_weeks_profit_capture:
            raise ValueError(
                "close_near_expiration_min_profit_capture must be >= "
                "close_at_three_weeks_profit_capture"
            )
        if (
            self.close_near_expiration_target_profit_capture
            < self.close_near_expiration_min_profit_capture
        ):
            raise ValueError(
                "close_near_expiration_target_profit_capture must be >= "
                "close_near_expiration_min_profit_capture"
            )
        return self


class ScannerPortfolioRules(StrategyModel):
    """Portfolio-level concentration and sizing constraints."""

    max_universe_size: int = Field(default=40, gt=0)
    max_candidates_per_run: int = Field(default=8, gt=0)
    top_n_to_publish: int = Field(default=4, gt=0)
    max_ideas_per_ticker: int = Field(default=1, gt=0)
    max_per_sector_in_top_n: int = Field(default=2, gt=0)
    max_tier_c_open_positions: int = Field(default=1, ge=0)
    tier_c_one_contract_cap: bool = Field(default=True)
    use_vix_cash_reserve_guidance: bool = Field(default=True)

    @model_validator(mode="after")
    def validate_publish_count(self) -> Self:
        """Ensure published ideas fit inside the candidate cap."""
        if self.top_n_to_publish > self.max_candidates_per_run:
            raise ValueError("top_n_to_publish must be <= max_candidates_per_run")
        return self


class ScannerStylePutStrategyConfig(StrategyModel):
    """V1 contract for scanner-style cash-secured put research."""

    universe: tuple[ScannerUniverseEntry, ...] = Field(default_factory=lambda: DEFAULT_UNIVERSE)
    put_entry: PutEntryRules = Field(default_factory=PutEntryRules)
    covered_call: CoveredCallRules = Field(default_factory=CoveredCallRules)
    exits: ExitManagementRules = Field(default_factory=ExitManagementRules)
    portfolio: ScannerPortfolioRules = Field(default_factory=ScannerPortfolioRules)
    tier_rules: dict[StockQualityTier, TierRule] = Field(
        default_factory=lambda: dict(DEFAULT_TIER_RULES)
    )

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        """Validate universe uniqueness, size, and tier-rule coverage."""
        symbols = [entry.symbol for entry in self.universe]
        if len(symbols) != len(set(symbols)):
            raise ValueError("universe symbols must be unique")
        if len(symbols) > self.portfolio.max_universe_size:
            raise ValueError("universe exceeds max_universe_size")
        missing_tiers = {entry.tier for entry in self.universe} - set(self.tier_rules)
        if missing_tiers:
            raise ValueError(f"tier_rules missing tiers: {sorted(missing_tiers)}")
        return self

    def symbols_for_tier(self, tier: StockQualityTier) -> tuple[str, ...]:
        """Return configured symbols for one quality tier."""
        return tuple(entry.symbol for entry in self.universe if entry.tier is tier)


DEFAULT_UNIVERSE: tuple[ScannerUniverseEntry, ...] = (
    ScannerUniverseEntry(symbol="AAPL", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="MSFT", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="GOOGL", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="AMZN", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="META", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="NVDA", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="AVGO", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="JPM", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="V", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="MA", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="COST", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="LLY", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="UNH", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="XOM", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="BRK-B", tier=StockQualityTier.A),
    ScannerUniverseEntry(symbol="AMD", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="QCOM", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="CRM", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="ORCL", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="NFLX", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="TSM", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="WMT", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="HD", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="MCD", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="CAT", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="GE", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="RTX", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="GS", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="MS", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="CVX", tier=StockQualityTier.B),
    ScannerUniverseEntry(symbol="TSLA", tier=StockQualityTier.C),
    ScannerUniverseEntry(symbol="PLTR", tier=StockQualityTier.C),
    ScannerUniverseEntry(symbol="COIN", tier=StockQualityTier.C),
    ScannerUniverseEntry(symbol="MSTR", tier=StockQualityTier.C),
    ScannerUniverseEntry(symbol="SOFI", tier=StockQualityTier.C),
    ScannerUniverseEntry(symbol="HOOD", tier=StockQualityTier.C),
    ScannerUniverseEntry(symbol="SHOP", tier=StockQualityTier.C),
    ScannerUniverseEntry(symbol="UBER", tier=StockQualityTier.C),
    ScannerUniverseEntry(symbol="MU", tier=StockQualityTier.C),
    ScannerUniverseEntry(symbol="INTC", tier=StockQualityTier.C),
)

DEFAULT_TIER_RULES: dict[StockQualityTier, TierRule] = {
    StockQualityTier.A: TierRule(
        min_put_monthly_yield=Decimal("0.020"),
        target_put_monthly_yield=Decimal("0.025"),
        max_delta=Decimal("0.28"),
        allow_yield_flex_for_excellent_structure=True,
        max_contracts_per_symbol=1,
    ),
    StockQualityTier.B: TierRule(
        min_put_monthly_yield=Decimal("0.025"),
        target_put_monthly_yield=Decimal("0.025"),
        max_delta=Decimal("0.28"),
        max_contracts_per_symbol=1,
    ),
    StockQualityTier.C: TierRule(
        min_put_monthly_yield=Decimal("0.030"),
        target_put_monthly_yield=Decimal("0.040"),
        max_delta=Decimal("0.25"),
        max_contracts_per_symbol=1,
    ),
}
