"""DuckDB-backed repositories for options research market data."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionImpliedVolatility,
    OptionQuote,
    OptionStyle,
    OptionType,
    UnderlyingPrice,
)


def _date_bounds(start_date: date, end_date: date) -> tuple[date, date]:
    if start_date > end_date:
        raise ValueError("start_date must be less than or equal to end_date")
    return start_date, end_date


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value)


def _required_decimal(value: str) -> Decimal:
    return Decimal(value)


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _contract_values(contract: OptionContract) -> tuple[str, date, str, str, int, str]:
    return (
        contract.underlying_symbol,
        contract.expiration,
        str(contract.strike),
        contract.option_type.value,
        contract.multiplier,
        contract.style.value,
    )


def _contract_from_values(
    underlying_symbol: str,
    expiration: date,
    strike: str,
    option_type: str,
    multiplier: int,
    style: str,
) -> OptionContract:
    return OptionContract(
        underlying_symbol=underlying_symbol,
        expiration=expiration,
        strike=Decimal(strike),
        option_type=OptionType(option_type),
        multiplier=multiplier,
        style=OptionStyle(style),
    )


class DuckDBStorage:
    """Owns a DuckDB connection and repository instances."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.connection = duckdb.connect(str(database))
        self._initialize_schema()
        self.underlying_prices = DuckDBUnderlyingPricesRepository(self.connection)
        self.option_chains = DuckDBOptionChainsRepository(self.connection)
        self.option_quotes = DuckDBOptionQuotesRepository(self.connection)
        self.option_greeks = DuckDBOptionGreeksRepository(self.connection)
        self.option_iv = DuckDBOptionIVRepository(self.connection)

    def close(self) -> None:
        """Close the underlying DuckDB connection."""
        self.connection.close()

    def _initialize_schema(self) -> None:
        for statement in _SCHEMA:
            self.connection.execute(statement)


class DuckDBUnderlyingPricesRepository:
    """DuckDB repository for underlying price observations."""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def insert(self, record: UnderlyingPrice) -> None:
        self.bulk_insert([record])

    def bulk_insert(self, records: list[UnderlyingPrice]) -> None:
        if not records:
            return
        self._connection.executemany(
            """
            INSERT INTO underlying_prices
              (symbol, timestamp, observed_date, price, bid, ask, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record.symbol,
                    record.timestamp.isoformat(),
                    record.timestamp.date(),
                    str(record.price),
                    _decimal_text(record.bid),
                    _decimal_text(record.ask),
                    record.volume,
                )
                for record in records
            ],
        )

    def retrieve_by_date(self, target_date: date) -> list[UnderlyingPrice]:
        return self.retrieve_by_date_range(target_date, target_date)

    def retrieve_by_date_range(self, start_date: date, end_date: date) -> list[UnderlyingPrice]:
        rows = self._connection.execute(
            """
            SELECT symbol, timestamp, price, bid, ask, volume
            FROM underlying_prices
            WHERE observed_date BETWEEN ? AND ?
            ORDER BY timestamp, symbol
            """,
            _date_bounds(start_date, end_date),
        ).fetchall()
        return [
            UnderlyingPrice(
                symbol=row[0],
                timestamp=_timestamp(row[1]),
                price=_required_decimal(row[2]),
                bid=_decimal(row[3]),
                ask=_decimal(row[4]),
                volume=row[5],
            )
            for row in rows
        ]


class DuckDBOptionChainsRepository:
    """DuckDB repository for option chain snapshots."""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def insert(self, record: OptionChain) -> None:
        self.bulk_insert([record])

    def bulk_insert(self, records: list[OptionChain]) -> None:
        if not records:
            return
        values: list[tuple[Any, ...]] = []
        for record in records:
            for contract in record.contracts:
                values.append(
                    (
                        record.underlying_symbol,
                        record.timestamp.isoformat(),
                        record.timestamp.date(),
                        *_contract_values(contract),
                    )
                )
        self._connection.executemany(
            """
            INSERT INTO option_chains
              (
                underlying_symbol,
                timestamp,
                observed_date,
                contract_underlying_symbol,
                expiration,
                strike,
                option_type,
                multiplier,
                style
              )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

    def retrieve_by_date(self, target_date: date) -> list[OptionChain]:
        return self.retrieve_by_date_range(target_date, target_date)

    def retrieve_by_date_range(self, start_date: date, end_date: date) -> list[OptionChain]:
        rows = self._connection.execute(
            """
            SELECT
              underlying_symbol,
              timestamp,
              contract_underlying_symbol,
              expiration,
              strike,
              option_type,
              multiplier,
              style
            FROM option_chains
            WHERE observed_date BETWEEN ? AND ?
            ORDER BY timestamp, underlying_symbol, expiration, strike, option_type
            """,
            _date_bounds(start_date, end_date),
        ).fetchall()
        grouped: dict[tuple[str, str], list[OptionContract]] = defaultdict(list)
        for row in rows:
            grouped[(row[0], row[1])].append(
                _contract_from_values(
                    underlying_symbol=row[2],
                    expiration=row[3],
                    strike=row[4],
                    option_type=row[5],
                    multiplier=row[6],
                    style=row[7],
                )
            )
        return [
            OptionChain(
                underlying_symbol=underlying_symbol,
                timestamp=_timestamp(timestamp),
                contracts=tuple(contracts),
            )
            for (underlying_symbol, timestamp), contracts in grouped.items()
        ]


class DuckDBOptionQuotesRepository:
    """DuckDB repository for option quote observations."""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def insert(self, record: OptionQuote) -> None:
        self.bulk_insert([record])

    def bulk_insert(self, records: list[OptionQuote]) -> None:
        if not records:
            return
        self._connection.executemany(
            """
            INSERT INTO option_quotes
              (
                timestamp,
                observed_date,
                underlying_symbol,
                expiration,
                strike,
                option_type,
                multiplier,
                style,
                bid,
                ask,
                last,
                mark,
                volume,
                open_interest
              )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record.timestamp.isoformat(),
                    record.timestamp.date(),
                    *_contract_values(record.contract),
                    str(record.bid),
                    str(record.ask),
                    _decimal_text(record.last),
                    _decimal_text(record.mark),
                    record.volume,
                    record.open_interest,
                )
                for record in records
            ],
        )

    def retrieve_by_date(self, target_date: date) -> list[OptionQuote]:
        return self.retrieve_by_date_range(target_date, target_date)

    def retrieve_by_date_range(self, start_date: date, end_date: date) -> list[OptionQuote]:
        rows = self._connection.execute(
            """
            SELECT
              timestamp,
              underlying_symbol,
              expiration,
              strike,
              option_type,
              multiplier,
              style,
              bid,
              ask,
              last,
              mark,
              volume,
              open_interest
            FROM option_quotes
            WHERE observed_date BETWEEN ? AND ?
            ORDER BY timestamp, underlying_symbol, expiration, strike, option_type
            """,
            _date_bounds(start_date, end_date),
        ).fetchall()
        return [
            OptionQuote(
                contract=_contract_from_values(row[1], row[2], row[3], row[4], row[5], row[6]),
                timestamp=_timestamp(row[0]),
                bid=_required_decimal(row[7]),
                ask=_required_decimal(row[8]),
                last=_decimal(row[9]),
                mark=_decimal(row[10]),
                volume=row[11],
                open_interest=row[12],
            )
            for row in rows
        ]


class DuckDBOptionGreeksRepository:
    """DuckDB repository for option Greek observations."""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def insert(self, record: OptionGreek) -> None:
        self.bulk_insert([record])

    def bulk_insert(self, records: list[OptionGreek]) -> None:
        if not records:
            return
        self._connection.executemany(
            """
            INSERT INTO option_greeks
              (
                timestamp,
                observed_date,
                underlying_symbol,
                expiration,
                strike,
                option_type,
                multiplier,
                style,
                delta,
                gamma,
                theta,
                vega,
                rho,
                implied_volatility
              )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record.timestamp.isoformat(),
                    record.timestamp.date(),
                    *_contract_values(record.contract),
                    _decimal_text(record.delta),
                    _decimal_text(record.gamma),
                    _decimal_text(record.theta),
                    _decimal_text(record.vega),
                    _decimal_text(record.rho),
                    _decimal_text(record.implied_volatility),
                )
                for record in records
            ],
        )

    def retrieve_by_date(self, target_date: date) -> list[OptionGreek]:
        return self.retrieve_by_date_range(target_date, target_date)

    def retrieve_by_date_range(self, start_date: date, end_date: date) -> list[OptionGreek]:
        rows = self._connection.execute(
            """
            SELECT
              timestamp,
              underlying_symbol,
              expiration,
              strike,
              option_type,
              multiplier,
              style,
              delta,
              gamma,
              theta,
              vega,
              rho,
              implied_volatility
            FROM option_greeks
            WHERE observed_date BETWEEN ? AND ?
            ORDER BY timestamp, underlying_symbol, expiration, strike, option_type
            """,
            _date_bounds(start_date, end_date),
        ).fetchall()
        return [
            OptionGreek(
                contract=_contract_from_values(row[1], row[2], row[3], row[4], row[5], row[6]),
                timestamp=_timestamp(row[0]),
                delta=_decimal(row[7]),
                gamma=_decimal(row[8]),
                theta=_decimal(row[9]),
                vega=_decimal(row[10]),
                rho=_decimal(row[11]),
                implied_volatility=_decimal(row[12]),
            )
            for row in rows
        ]


class DuckDBOptionIVRepository:
    """DuckDB repository for implied volatility observations."""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection

    def insert(self, record: OptionImpliedVolatility) -> None:
        self.bulk_insert([record])

    def bulk_insert(self, records: list[OptionImpliedVolatility]) -> None:
        if not records:
            return
        self._connection.executemany(
            """
            INSERT INTO option_iv
              (
                timestamp,
                observed_date,
                underlying_symbol,
                expiration,
                strike,
                option_type,
                multiplier,
                style,
                implied_volatility
              )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record.timestamp.isoformat(),
                    record.timestamp.date(),
                    *_contract_values(record.contract),
                    str(record.implied_volatility),
                )
                for record in records
            ],
        )

    def retrieve_by_date(self, target_date: date) -> list[OptionImpliedVolatility]:
        return self.retrieve_by_date_range(target_date, target_date)

    def retrieve_by_date_range(
        self,
        start_date: date,
        end_date: date,
    ) -> list[OptionImpliedVolatility]:
        rows = self._connection.execute(
            """
            SELECT
              timestamp,
              underlying_symbol,
              expiration,
              strike,
              option_type,
              multiplier,
              style,
              implied_volatility
            FROM option_iv
            WHERE observed_date BETWEEN ? AND ?
            ORDER BY timestamp, underlying_symbol, expiration, strike, option_type
            """,
            _date_bounds(start_date, end_date),
        ).fetchall()
        return [
            OptionImpliedVolatility(
                contract=_contract_from_values(row[1], row[2], row[3], row[4], row[5], row[6]),
                timestamp=_timestamp(row[0]),
                implied_volatility=_required_decimal(row[7]),
            )
            for row in rows
        ]


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS underlying_prices (
      symbol TEXT NOT NULL,
      timestamp TEXT NOT NULL,
      observed_date DATE NOT NULL,
      price TEXT NOT NULL,
      bid TEXT,
      ask TEXT,
      volume BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS option_chains (
      underlying_symbol TEXT NOT NULL,
      timestamp TEXT NOT NULL,
      observed_date DATE NOT NULL,
      contract_underlying_symbol TEXT NOT NULL,
      expiration DATE NOT NULL,
      strike TEXT NOT NULL,
      option_type TEXT NOT NULL,
      multiplier INTEGER NOT NULL,
      style TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS option_quotes (
      timestamp TEXT NOT NULL,
      observed_date DATE NOT NULL,
      underlying_symbol TEXT NOT NULL,
      expiration DATE NOT NULL,
      strike TEXT NOT NULL,
      option_type TEXT NOT NULL,
      multiplier INTEGER NOT NULL,
      style TEXT NOT NULL,
      bid TEXT NOT NULL,
      ask TEXT NOT NULL,
      last TEXT,
      mark TEXT,
      volume BIGINT,
      open_interest BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS option_greeks (
      timestamp TEXT NOT NULL,
      observed_date DATE NOT NULL,
      underlying_symbol TEXT NOT NULL,
      expiration DATE NOT NULL,
      strike TEXT NOT NULL,
      option_type TEXT NOT NULL,
      multiplier INTEGER NOT NULL,
      style TEXT NOT NULL,
      delta TEXT,
      gamma TEXT,
      theta TEXT,
      vega TEXT,
      rho TEXT,
      implied_volatility TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS option_iv (
      timestamp TEXT NOT NULL,
      observed_date DATE NOT NULL,
      underlying_symbol TEXT NOT NULL,
      expiration DATE NOT NULL,
      strike TEXT NOT NULL,
      option_type TEXT NOT NULL,
      multiplier INTEGER NOT NULL,
      style TEXT NOT NULL,
      implied_volatility TEXT NOT NULL
    )
    """,
]
