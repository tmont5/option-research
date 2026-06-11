import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from options_quant.backtest import (
    BacktestAccountSnapshot,
    BacktestResult,
    ClosedBacktestPosition,
    ExitReason,
)
from options_quant.data.models import OptionContract, OptionType
from options_quant.reporting import BacktestReportGenerator, ReportArtifacts, SummaryStatistic


def make_contract() -> OptionContract:
    return OptionContract(
        underlying_symbol="SPY",
        expiration=date(2026, 7, 17),
        strike=Decimal("500"),
        option_type=OptionType.PUT,
    )


def make_snapshot(index: int, equity: Decimal) -> BacktestAccountSnapshot:
    return BacktestAccountSnapshot(
        date=date(2026, 1, 1 + index),
        cash_balance=equity,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        capital_utilization=Decimal("0"),
        equity=equity,
        open_positions=(),
    )


def make_closed_position(realized_pnl: Decimal) -> ClosedBacktestPosition:
    return ClosedBacktestPosition(
        position_id=uuid4(),
        contract=make_contract(),
        quantity=-1,
        entry_date=date(2026, 1, 1),
        exit_date=date(2026, 1, 2),
        entry_fill_price=Decimal("2.00"),
        exit_fill_price=Decimal("1.00"),
        realized_pnl=realized_pnl,
        exit_reason=ExitReason.ORDER,
    )


def make_result() -> BacktestResult:
    return BacktestResult(
        snapshots=(
            make_snapshot(0, Decimal("100000")),
            make_snapshot(1, Decimal("101000")),
            make_snapshot(2, Decimal("99500")),
            make_snapshot(3, Decimal("102000")),
        ),
        closed_positions=(
            make_closed_position(Decimal("100")),
            make_closed_position(Decimal("-50")),
        ),
    )


def test_summary_statistics_table_returns_formatted_rows() -> None:
    rows = BacktestReportGenerator().summary_statistics_table(make_result())

    assert all(isinstance(row, SummaryStatistic) for row in rows)
    table = {row.name: row.value for row in rows}
    assert table["CAGR"].endswith("%")
    assert table["Annualized Return"].endswith("%")
    assert table["Sharpe Ratio"] != "N/A"
    assert table["Win Rate"] == "50.00%"
    assert table["Average Win"] == "$100.00"
    assert table["Average Loss"] == "$-50.00"
    assert table["Profit Factor"] == "2.0000"
    assert table["Expectancy"] == "$25.00"
    assert table["Alpha"] == "N/A"


def test_summary_statistics_table_includes_benchmark_relative_rows() -> None:
    rows = BacktestReportGenerator().summary_statistics_table(
        make_result(),
        benchmark_returns=[Decimal("0.005"), Decimal("-0.010"), Decimal("0.015")],
    )
    table = {row.name: row.value for row in rows}

    assert table["Benchmark Annualized Return"].endswith("%")
    assert table["Alpha"].endswith("%")
    assert table["Beta"] != "N/A"
    assert table["Tracking Error"].endswith("%")
    assert table["Information Ratio"] != "N/A"


def test_export_trade_log_csv_writes_closed_positions(tmp_path: Path) -> None:
    output_path = BacktestReportGenerator().export_trade_log_csv(
        make_result(),
        tmp_path / "trade_log.csv",
    )

    with output_path.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))

    assert len(rows) == 2
    assert rows[0]["underlying_symbol"] == "SPY"
    assert rows[0]["expiration"] == "2026-07-17"
    assert rows[0]["strike"] == "500"
    assert rows[0]["option_type"] == "put"
    assert rows[0]["quantity"] == "-1"
    assert rows[0]["entry_fill_price"] == "2.00"
    assert rows[0]["exit_fill_price"] == "1.00"
    assert rows[0]["realized_pnl"] == "100"
    assert rows[0]["exit_reason"] == "order"


def test_chart_methods_write_png_files(tmp_path: Path) -> None:
    generator = BacktestReportGenerator()
    equity_path = generator.save_equity_curve_chart(make_result(), tmp_path / "equity.png")
    drawdown_path = generator.save_drawdown_chart(make_result(), tmp_path / "drawdown.png")

    assert equity_path.exists()
    assert equity_path.stat().st_size > 0
    assert drawdown_path.exists()
    assert drawdown_path.stat().st_size > 0


def test_generate_report_writes_all_artifacts(tmp_path: Path) -> None:
    artifacts = BacktestReportGenerator().generate_report(make_result(), tmp_path)

    assert isinstance(artifacts, ReportArtifacts)
    assert artifacts.trade_log_csv == tmp_path / "trade_log.csv"
    assert artifacts.trade_log_csv.exists()
    assert artifacts.equity_curve_chart == tmp_path / "equity_curve.png"
    assert artifacts.equity_curve_chart.exists()
    assert artifacts.drawdown_chart == tmp_path / "drawdown.png"
    assert artifacts.drawdown_chart.exists()
    assert len(artifacts.summary_statistics) > 0
