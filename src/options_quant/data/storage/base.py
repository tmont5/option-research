"""Provider-neutral repository interfaces for market data storage."""

from __future__ import annotations

from datetime import date
from typing import Protocol, TypeVar

TRecord = TypeVar("TRecord")


class MarketDataRepository(Protocol[TRecord]):
    """Common behavior for time-series market data repositories."""

    def insert(self, record: TRecord) -> None:
        """Insert a single market data record."""

    def bulk_insert(self, records: list[TRecord]) -> None:
        """Insert multiple market data records."""

    def retrieve_by_date(self, target_date: date) -> list[TRecord]:
        """Return all records observed on a single calendar date."""

    def retrieve_by_date_range(self, start_date: date, end_date: date) -> list[TRecord]:
        """Return all records observed within an inclusive calendar date range."""
