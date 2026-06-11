"""Option contract selection helpers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionImpliedVolatility,
    OptionType,
)


class OptionSelectionModel(BaseModel):
    """Base configuration for contract selection objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class OptionSelectionCandidate(OptionSelectionModel):
    """A contract plus normalized selection metadata."""

    contract: OptionContract = Field(description="Selected option contract.")
    as_of_date: date = Field(description="Date used to calculate DTE.")
    spot_price: Decimal = Field(gt=Decimal("0"), description="Underlying spot reference price.")
    dte: int = Field(ge=0, description="Days to expiration from as_of_date.")
    strike_distance: Decimal = Field(description="Contract strike minus spot price.")
    strike_distance_pct: Decimal = Field(description="Strike distance divided by spot price.")
    delta: Decimal | None = Field(default=None, description="Option delta when available.")
    implied_volatility: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        description="Annualized implied volatility when available.",
    )


class OptionSelectionQuery(OptionSelectionModel):
    """Filters and ranking hints for option contract selection."""

    option_type: OptionType | None = Field(default=None, description="Optional call/put filter.")
    min_dte: int | None = Field(default=None, ge=0, description="Minimum DTE, inclusive.")
    max_dte: int | None = Field(default=None, ge=0, description="Maximum DTE, inclusive.")
    target_dte: int | None = Field(default=None, ge=0, description="Preferred DTE for ranking.")
    target_delta: Decimal | None = Field(
        default=None,
        ge=Decimal("-1"),
        le=Decimal("1"),
        description="Preferred delta for ranking.",
    )
    target_strike: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        description="Preferred strike for ranking.",
    )
    min_strike_distance: Decimal | None = Field(
        default=None,
        description="Minimum strike minus spot, inclusive.",
    )
    max_strike_distance: Decimal | None = Field(
        default=None,
        description="Maximum strike minus spot, inclusive.",
    )
    min_implied_volatility: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        description="Minimum IV, inclusive.",
    )
    max_implied_volatility: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        description="Maximum IV, inclusive.",
    )

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        """Ensure range filters are internally consistent."""
        if self.min_dte is not None and self.max_dte is not None and self.min_dte > self.max_dte:
            raise ValueError("min_dte must be less than or equal to max_dte")
        if (
            self.min_strike_distance is not None
            and self.max_strike_distance is not None
            and self.min_strike_distance > self.max_strike_distance
        ):
            raise ValueError(
                "min_strike_distance must be less than or equal to max_strike_distance"
            )
        if (
            self.min_implied_volatility is not None
            and self.max_implied_volatility is not None
            and self.min_implied_volatility > self.max_implied_volatility
        ):
            raise ValueError(
                "min_implied_volatility must be less than or equal to max_implied_volatility"
            )
        return self


class ContractSelectionEngine:
    """Select option contracts from a chain plus optional analytics data."""

    def __init__(
        self,
        chain: OptionChain,
        spot_price: Decimal,
        *,
        as_of_date: date | None = None,
        greeks: list[OptionGreek] | None = None,
        implied_volatilities: list[OptionImpliedVolatility] | None = None,
    ) -> None:
        self._chain = chain
        self._spot_price = spot_price
        self._as_of_date = as_of_date if as_of_date is not None else chain.timestamp.date()
        self._greeks_by_contract = {
            greek.contract: greek for greek in greeks or []
        }
        self._ivs_by_contract = {
            iv.contract: iv for iv in implied_volatilities or []
        }

    def candidates(self) -> list[OptionSelectionCandidate]:
        """Return all contracts as typed selection candidates."""
        return [self._candidate(contract) for contract in self._chain.contracts]

    def select(self, query: OptionSelectionQuery) -> list[OptionSelectionCandidate]:
        """Filter and rank contracts according to a selection query."""
        candidates = [candidate for candidate in self.candidates() if _matches(candidate, query)]
        return sorted(candidates, key=lambda candidate: _rank_key(candidate, query))

    def best(self, query: OptionSelectionQuery) -> OptionSelectionCandidate | None:
        """Return the top-ranked candidate for a query, if any."""
        selected = self.select(query)
        if not selected:
            return None
        return selected[0]

    def find_nearest_dte_with_delta(
        self,
        *,
        target_dte: int,
        target_delta: Decimal,
        option_type: OptionType | None = None,
    ) -> OptionSelectionCandidate | None:
        """Return the contract nearest target DTE, then closest to target delta."""
        return self.best(
            OptionSelectionQuery(
                option_type=option_type,
                target_dte=target_dte,
                target_delta=target_delta,
            )
        )

    def find_contracts_between_dte(
        self,
        min_dte: int,
        max_dte: int,
        *,
        option_type: OptionType | None = None,
    ) -> list[OptionSelectionCandidate]:
        """Return contracts within an inclusive DTE range."""
        return self.select(
            OptionSelectionQuery(
                option_type=option_type,
                min_dte=min_dte,
                max_dte=max_dte,
            )
        )

    def find_closest_to_strike(
        self,
        target_strike: Decimal,
        *,
        option_type: OptionType | None = None,
    ) -> OptionSelectionCandidate | None:
        """Return the contract closest to a target strike."""
        return self.best(
            OptionSelectionQuery(option_type=option_type, target_strike=target_strike)
        )

    def _candidate(self, contract: OptionContract) -> OptionSelectionCandidate:
        greek = self._greeks_by_contract.get(contract)
        iv = self._ivs_by_contract.get(contract)
        implied_volatility = None
        if iv is not None:
            implied_volatility = iv.implied_volatility
        elif greek is not None:
            implied_volatility = greek.implied_volatility
        strike_distance = contract.strike - self._spot_price
        return OptionSelectionCandidate(
            contract=contract,
            as_of_date=self._as_of_date,
            spot_price=self._spot_price,
            dte=(contract.expiration - self._as_of_date).days,
            strike_distance=strike_distance,
            strike_distance_pct=strike_distance / self._spot_price,
            delta=greek.delta if greek is not None else None,
            implied_volatility=implied_volatility,
        )


def _matches(candidate: OptionSelectionCandidate, query: OptionSelectionQuery) -> bool:
    if query.option_type is not None and candidate.contract.option_type is not query.option_type:
        return False
    if query.min_dte is not None and candidate.dte < query.min_dte:
        return False
    if query.max_dte is not None and candidate.dte > query.max_dte:
        return False
    if query.min_strike_distance is not None and (
        candidate.strike_distance < query.min_strike_distance
    ):
        return False
    if query.max_strike_distance is not None and (
        candidate.strike_distance > query.max_strike_distance
    ):
        return False
    if query.min_implied_volatility is not None and (
        candidate.implied_volatility is None
        or candidate.implied_volatility < query.min_implied_volatility
    ):
        return False
    if query.max_implied_volatility is not None and (
        candidate.implied_volatility is None
        or candidate.implied_volatility > query.max_implied_volatility
    ):
        return False
    if query.target_delta is not None and candidate.delta is None:
        return False
    return True


def _rank_key(
    candidate: OptionSelectionCandidate,
    query: OptionSelectionQuery,
) -> tuple[Decimal | int, ...]:
    key: list[Decimal | int] = []
    if query.target_dte is not None:
        key.append(abs(candidate.dte - query.target_dte))
    if query.target_delta is not None and candidate.delta is not None:
        key.append(abs(candidate.delta - query.target_delta))
    if query.target_strike is not None:
        key.append(abs(candidate.contract.strike - query.target_strike))
    key.extend(
        [
            candidate.dte,
            abs(candidate.strike_distance),
            candidate.contract.strike,
        ]
    )
    return tuple(key)
