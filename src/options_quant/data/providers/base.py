"""Provider interfaces for retrieving normalized market data."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionImpliedVolatility,
    OptionOpenInterest,
    UnderlyingPrice,
)


class MarketDataProvider(Protocol):
    """Provider-neutral interface for options research market data."""

    def retrieve_underlying_prices(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[UnderlyingPrice]:
        """Return normalized underlying price observations."""

    def retrieve_option_chain(self, symbol: str, as_of_date: date) -> OptionChain:
        """Return the option chain snapshot for an underlying."""

    def retrieve_implied_volatility(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionImpliedVolatility]:
        """Return normalized implied volatility observations."""

    def retrieve_greeks(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionGreek]:
        """Return normalized Greek observations."""

    def retrieve_open_interest(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionOpenInterest]:
        """Return normalized open interest observations."""
