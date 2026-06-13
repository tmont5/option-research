from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from options_quant.data.models import OptionContract, OptionQuote
from options_quant.pipelines import OneWeekPipelineConfig, run_one_week_pipeline


class FakeEndpoints:
    def list_expirations(self, *, symbol: str) -> list[dict[str, object]]:
        assert symbol == "ANET"
        return [{"symbol": symbol, "expiration": date(2026, 7, 17)}]

    def list_strikes(self, *, symbol: str, expiration: date | str) -> list[dict[str, object]]:
        assert symbol == "ANET"
        assert expiration == date(2026, 7, 17)
        return [{"symbol": symbol, "strike": Decimal("145")}, {"symbol": symbol, "strike": 150.0}]

    def history_greeks_first_order(self, **params: object) -> list[dict[str, object]]:
        strike = Decimal(str(params["strike"]))
        base_delta = Decimal("-0.20") if strike == Decimal("145") else Decimal("-0.31")
        return [
            {
                "timestamp": datetime(2026, 6, 8, 20, tzinfo=UTC),
                "delta": base_delta,
                "theta": Decimal("-0.10"),
                "vega": Decimal("12.3"),
                "rho": Decimal("-4.1"),
                "implied_vol": Decimal("0.55"),
                "underlying_price": Decimal("156.39"),
            },
            {
                "timestamp": datetime(2026, 6, 9, 20, tzinfo=UTC),
                "delta": base_delta - Decimal("0.02"),
                "theta": Decimal("-0.11"),
                "vega": Decimal("12.9"),
                "rho": Decimal("-4.5"),
                "implied_vol": Decimal("0.56"),
                "underlying_price": Decimal("152.16"),
            },
        ]


class FakeProvider:
    def retrieve_option_eod_quotes(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionQuote]:
        assert start_date == date(2026, 6, 8)
        assert end_date == date(2026, 6, 9)
        mark = Decimal("6.00") if contract.strike == Decimal("150") else Decimal("3.00")
        return [
            OptionQuote(
                contract=contract,
                timestamp=datetime(2026, 6, 8, 21, tzinfo=UTC),
                bid=mark - Decimal("0.10"),
                ask=mark + Decimal("0.10"),
                last=mark,
                mark=mark,
                volume=10,
            ),
            OptionQuote(
                contract=contract,
                timestamp=datetime(2026, 6, 9, 21, tzinfo=UTC),
                bid=mark - Decimal("1.10"),
                ask=mark - Decimal("0.90"),
                last=mark - Decimal("1.00"),
                mark=mark - Decimal("1.00"),
                volume=12,
            ),
        ]


def test_one_week_pipeline_runs_through_storage_selection_and_backtest(tmp_path: Path) -> None:
    config = OneWeekPipelineConfig(
        symbol="ANET",
        start_date=date(2026, 6, 8),
        end_date=date(2026, 6, 9),
        target_delta=Decimal("-0.30"),
        database_path=tmp_path / "pipeline.duckdb",
        report_path=tmp_path / "report.md",
    )
    result = run_one_week_pipeline(config, endpoints=FakeEndpoints(), provider=FakeProvider())

    assert result.chain_contracts == 2
    assert result.quote_rows == 4
    assert result.greek_rows == 4
    assert result.underlying_rows == 2
    assert result.selected_candidate.contract.strike == Decimal("150")
    assert result.selected_candidate.delta == Decimal("-0.31")
    assert result.backtest_result.snapshots[-1].unrealized_pnl == Decimal("99.35")
    assert (tmp_path / "pipeline.duckdb").exists()
    assert "One-Week Pipeline Inspection" in (tmp_path / "report.md").read_text()

    rerun_result = run_one_week_pipeline(config, endpoints=FakeEndpoints(), provider=FakeProvider())

    assert rerun_result.underlying_rows == 2
    assert rerun_result.selected_candidate.contract.strike == Decimal("150")
