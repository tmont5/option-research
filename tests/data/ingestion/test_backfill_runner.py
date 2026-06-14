from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from options_quant.data.ingestion.backfill_runner import (
    BackfillRunnerConfig,
    run_backfill_runner,
)
from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionOpenInterest,
    OptionQuote,
    OptionType,
    UnderlyingPrice,
)
from options_quant.data.storage import DuckDBStorage


class MockBackfillProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.delta_by_strike = {
            Decimal("480"): Decimal("-0.19"),
            Decimal("470"): Decimal("-0.32"),
            Decimal("460"): Decimal("-0.08"),
        }

    def retrieve_underlying_eod_prices(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[UnderlyingPrice]:
        self.calls.append(("underlying", (symbol, start_date, end_date)))
        return [
            UnderlyingPrice(
                symbol=symbol,
                timestamp=datetime.combine(start_date, datetime.min.time(), tzinfo=UTC),
                price=Decimal("500"),
            )
        ]

    def retrieve_option_chain(self, symbol: str, as_of_date: date) -> OptionChain:
        self.calls.append(("chain", as_of_date))
        return OptionChain(
            underlying_symbol=symbol,
            timestamp=datetime.combine(as_of_date, datetime.min.time(), tzinfo=UTC),
            contracts=(
                OptionContract(
                    underlying_symbol=symbol,
                    expiration=as_of_date + timedelta(days=35),
                    strike=Decimal("480"),
                    option_type=OptionType.PUT,
                ),
                OptionContract(
                    underlying_symbol=symbol,
                    expiration=as_of_date + timedelta(days=40),
                    strike=Decimal("470"),
                    option_type=OptionType.PUT,
                ),
                OptionContract(
                    underlying_symbol=symbol,
                    expiration=as_of_date + timedelta(days=35),
                    strike=Decimal("460"),
                    option_type=OptionType.PUT,
                ),
            ),
        )

    def retrieve_option_eod_quotes(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionQuote]:
        self.calls.append(("quotes", contract))
        return [
            OptionQuote(
                contract=contract,
                timestamp=datetime.combine(start_date, datetime.min.time(), tzinfo=UTC),
                bid=Decimal("1.00"),
                ask=Decimal("1.10"),
                mark=Decimal("1.05"),
            )
        ]

    def retrieve_first_order_greeks(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionGreek]:
        self.calls.append(("greeks", contract))
        return [
            OptionGreek(
                contract=contract,
                timestamp=datetime.combine(start_date, datetime.min.time(), tzinfo=UTC),
                delta=self.delta_by_strike[contract.strike],
                implied_volatility=Decimal("0.20"),
            )
        ]

    def retrieve_open_interest(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionOpenInterest]:
        self.calls.append(("open_interest", contract))
        return [
            OptionOpenInterest(
                contract=contract,
                timestamp=datetime.combine(start_date, datetime.min.time(), tzinfo=UTC),
                open_interest=100,
            )
        ]


def test_backfill_runner_executes_bounded_chunks_and_writes_reports(tmp_path: Path) -> None:
    provider = MockBackfillProvider()
    database_path = tmp_path / "market.duckdb"

    result = run_backfill_runner(
        BackfillRunnerConfig(
            symbol="SPY",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 2, 15),
            chunk_days=20,
            max_chunks=2,
            max_contracts=1,
            database_path=database_path,
            manifest_path=tmp_path / "manifest.json",
            report_path=tmp_path / "report.md",
            reset_database=True,
        ),
        provider=provider,
    )

    assert result.chunks_planned == 3
    assert result.chunks_completed == 2
    assert result.underlying_prices == 2
    assert result.contracts_selected == 2
    assert result.option_quotes == 2
    assert result.option_greeks == 2
    assert (tmp_path / "manifest.json").exists()
    assert "chunks_completed" in (tmp_path / "report.md").read_text()

    storage = DuckDBStorage(database_path)
    try:
        assert (
            len(
                storage.underlying_prices.retrieve_by_date_range(
                    date(2025, 1, 1),
                    date(2025, 2, 15),
                )
            )
            == 2
        )
        assert (
            len(storage.option_quotes.retrieve_by_date_range(date(2025, 1, 1), date(2025, 2, 15)))
            == 2
        )
    finally:
        storage.close()


def test_backfill_runner_reset_database_removes_prior_rows(tmp_path: Path) -> None:
    provider = MockBackfillProvider()
    database_path = tmp_path / "market.duckdb"
    config = BackfillRunnerConfig(
        symbol="SPY",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 20),
        max_chunks=1,
        max_contracts=1,
        database_path=database_path,
        manifest_path=tmp_path / "manifest.json",
        report_path=tmp_path / "report.md",
        reset_database=True,
    )

    run_backfill_runner(config, provider=provider)
    run_backfill_runner(config, provider=provider)

    storage = DuckDBStorage(database_path)
    try:
        assert (
            len(storage.option_quotes.retrieve_by_date_range(date(2025, 1, 1), date(2025, 1, 20)))
            == 1
        )
    finally:
        storage.close()


def test_backfill_runner_can_select_contracts_by_target_delta(tmp_path: Path) -> None:
    provider = MockBackfillProvider()
    database_path = tmp_path / "target_delta.duckdb"

    result = run_backfill_runner(
        BackfillRunnerConfig(
            symbol="SPY",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 20),
            min_dte=30,
            max_dte=35,
            target_delta=Decimal("-0.20"),
            contracts_around_target=1,
            database_path=database_path,
            manifest_path=tmp_path / "manifest.json",
            report_path=tmp_path / "report.md",
            reset_database=True,
        ),
        provider=provider,
    )

    assert result.contracts_selected == 1
    assert result.option_quotes == 1
    storage = DuckDBStorage(database_path)
    try:
        quote = storage.option_quotes.retrieve_by_date(date(2025, 1, 1))[0]
        assert quote.contract.strike == Decimal("480")
    finally:
        storage.close()
