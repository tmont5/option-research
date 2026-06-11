"""Backtest report generation."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from pydantic import BaseModel, ConfigDict, Field

from options_quant.analytics import PerformanceAnalyzer, PerformanceReport
from options_quant.backtest import BacktestResult, ClosedBacktestPosition

ONE = Decimal("1")


class ReportingModel(BaseModel):
    """Base configuration for immutable reporting objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SummaryStatistic(ReportingModel):
    """One row in a summary statistics table."""

    name: str = Field(description="Human-readable metric name.")
    value: str = Field(description="Formatted metric value.")


class ReportArtifacts(ReportingModel):
    """Files and summary table generated for a report."""

    summary_statistics: tuple[SummaryStatistic, ...] = Field(description="Summary table rows.")
    trade_log_csv: Path = Field(description="Trade log CSV path.")
    equity_curve_chart: Path = Field(description="Equity curve chart path.")
    drawdown_chart: Path = Field(description="Drawdown chart path.")


class BacktestReportGenerator:
    """Generate tabular and chart reports from backtest results."""

    def __init__(self, analyzer: PerformanceAnalyzer | None = None) -> None:
        self._analyzer = analyzer if analyzer is not None else PerformanceAnalyzer()

    def summary_statistics_table(
        self,
        result: BacktestResult,
        benchmark_returns: list[Decimal] | None = None,
    ) -> tuple[SummaryStatistic, ...]:
        """Return formatted summary statistics rows."""
        report = self._analyzer.analyze(result, benchmark_returns=benchmark_returns)
        return _summary_statistics_from_report(report)

    def export_trade_log_csv(self, result: BacktestResult, path: Path | str) -> Path:
        """Write closed trade records to CSV."""
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=_TRADE_LOG_FIELDNAMES)
            writer.writeheader()
            for position in result.closed_positions:
                writer.writerow(_trade_log_row(position))
        return output_path

    def save_equity_curve_chart(self, result: BacktestResult, path: Path | str) -> Path:
        """Save an equity curve chart as a PNG."""
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        labels = [snapshot.date.isoformat() for snapshot in result.snapshots]
        x_values = list(range(len(result.snapshots)))
        equity = [float(snapshot.equity) for snapshot in result.snapshots]
        figure, axis = plt.subplots(figsize=(10, 5))
        axis.plot(x_values, equity, color="#1f77b4", linewidth=2)
        axis.set_title("Equity Curve")
        axis.set_xlabel("Date")
        axis.set_ylabel("Equity")
        axis.set_xticks(x_values)
        axis.set_xticklabels(labels, rotation=30, ha="right")
        axis.grid(True, alpha=0.3)
        figure.tight_layout()
        figure.savefig(output_path, dpi=150)
        plt.close(figure)
        return output_path

    def save_drawdown_chart(self, result: BacktestResult, path: Path | str) -> Path:
        """Save a drawdown chart as a PNG."""
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        labels = [snapshot.date.isoformat() for snapshot in result.snapshots]
        x_values = list(range(len(result.snapshots)))
        drawdowns = [float(drawdown) for drawdown in _drawdown_series(result)]
        figure, axis = plt.subplots(figsize=(10, 5))
        axis.fill_between(x_values, drawdowns, 0, color="#d62728", alpha=0.35)
        axis.plot(x_values, drawdowns, color="#d62728", linewidth=1.5)
        axis.set_title("Drawdown")
        axis.set_xlabel("Date")
        axis.set_ylabel("Drawdown")
        axis.set_xticks(x_values)
        axis.set_xticklabels(labels, rotation=30, ha="right")
        axis.grid(True, alpha=0.3)
        figure.tight_layout()
        figure.savefig(output_path, dpi=150)
        plt.close(figure)
        return output_path

    def generate_report(
        self,
        result: BacktestResult,
        output_dir: Path | str,
        benchmark_returns: list[Decimal] | None = None,
    ) -> ReportArtifacts:
        """Generate all standard report artifacts into output_dir."""
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        summary_statistics = self.summary_statistics_table(
            result,
            benchmark_returns=benchmark_returns,
        )
        return ReportArtifacts(
            summary_statistics=summary_statistics,
            trade_log_csv=self.export_trade_log_csv(result, directory / "trade_log.csv"),
            equity_curve_chart=self.save_equity_curve_chart(
                result,
                directory / "equity_curve.png",
            ),
            drawdown_chart=self.save_drawdown_chart(result, directory / "drawdown.png"),
        )


def _summary_statistics_from_report(report: PerformanceReport) -> tuple[SummaryStatistic, ...]:
    metrics = [
        ("CAGR", _format_percent(report.cagr)),
        ("Annualized Return", _format_percent(report.annualized_return)),
        ("Annualized Volatility", _format_percent(report.annualized_volatility)),
        ("Sharpe Ratio", _format_decimal(report.sharpe_ratio)),
        ("Sortino Ratio", _format_decimal(report.sortino_ratio)),
        ("Maximum Drawdown", _format_percent(report.maximum_drawdown)),
        ("Calmar Ratio", _format_decimal(report.calmar_ratio)),
        ("Win Rate", _format_percent(report.win_rate)),
        ("Average Win", _format_currency(report.average_win)),
        ("Average Loss", _format_currency(report.average_loss)),
        ("Profit Factor", _format_decimal(report.profit_factor)),
        ("Expectancy", _format_currency(report.expectancy)),
        ("Benchmark Annualized Return", _format_percent(report.benchmark_annualized_return)),
        ("Alpha", _format_percent(report.alpha)),
        ("Beta", _format_decimal(report.beta)),
        ("Tracking Error", _format_percent(report.tracking_error)),
        ("Information Ratio", _format_decimal(report.information_ratio)),
    ]
    return tuple(SummaryStatistic(name=name, value=value) for name, value in metrics)


def _trade_log_row(position: ClosedBacktestPosition) -> dict[str, str]:
    return {
        "position_id": str(position.position_id),
        "underlying_symbol": position.contract.underlying_symbol,
        "expiration": position.contract.expiration.isoformat(),
        "strike": str(position.contract.strike),
        "option_type": position.contract.option_type.value,
        "quantity": str(position.quantity),
        "entry_date": position.entry_date.isoformat(),
        "exit_date": position.exit_date.isoformat(),
        "entry_fill_price": str(position.entry_fill_price),
        "exit_fill_price": str(position.exit_fill_price),
        "realized_pnl": str(position.realized_pnl),
        "exit_reason": position.exit_reason.value,
    }


def _drawdown_series(result: BacktestResult) -> list[Decimal]:
    peak = result.snapshots[0].equity
    drawdowns: list[Decimal] = []
    for snapshot in result.snapshots:
        peak = max(peak, snapshot.equity)
        drawdowns.append(snapshot.equity / peak - ONE)
    return drawdowns


def _format_percent(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * Decimal('100'):.2f}%"


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.4f}"


def _format_currency(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return "$" + f"{value:.2f}"


_TRADE_LOG_FIELDNAMES = [
    "position_id",
    "underlying_symbol",
    "expiration",
    "strike",
    "option_type",
    "quantity",
    "entry_date",
    "exit_date",
    "entry_fill_price",
    "exit_fill_price",
    "realized_pnl",
    "exit_reason",
]
