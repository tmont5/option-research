from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from options_quant.backtest import (
    BacktestAccountSnapshot,
    BacktestResult,
    ClosedBacktestPosition,
    ExitReason,
)
from options_quant.data.models import OptionContract, OptionQuote, OptionType
from options_quant.pipelines import BatchValidationConfig, run_batch_validation_pipeline
from options_quant.pipelines.single_trade import (
    SingleTradePipelineConfig,
    SingleTradePipelineResult,
    TradeAudit,
)
from options_quant.strategies.selection import OptionSelectionCandidate


def test_batch_validation_reports_completed_trades_and_failures(tmp_path: Path) -> None:
    pnls = [Decimal("100"), Decimal("-50")]

    def runner(config: SingleTradePipelineConfig) -> SingleTradePipelineResult:
        if config.entry_date == date(2025, 1, 17):
            raise ValueError("holiday gap")
        pnl = pnls.pop(0)
        return _single_trade_result(config, pnl)

    result = run_batch_validation_pipeline(
        BatchValidationConfig(
            start_date=date(2025, 1, 3),
            trade_count=3,
            report_path=tmp_path / "report.md",
        ),
        trade_runner=runner,
    )

    assert result.entry_dates == (
        date(2025, 1, 3),
        date(2025, 1, 10),
        date(2025, 1, 17),
    )
    assert result.metrics.completed_trades == 2
    assert result.metrics.failed_trades == 1
    assert result.metrics.total_realized_pnl == Decimal("50")
    assert result.metrics.average_realized_pnl == Decimal("25")
    assert result.metrics.win_rate == Decimal("0.5")
    assert result.metrics.per_trade_sharpe is None
    assert result.metrics.sharpe_note is not None
    assert result.metrics.max_drawdown < Decimal("0")
    report = (tmp_path / "report.md").read_text()
    assert "holiday gap" in report
    assert "total_realized_pnl" in report
    assert "insufficient sample" in report


def test_batch_validation_generates_entries_through_end_date(tmp_path: Path) -> None:
    seen_dates: list[date] = []

    def runner(config: SingleTradePipelineConfig) -> SingleTradePipelineResult:
        seen_dates.append(config.entry_date)
        return _single_trade_result(config, Decimal("10"))

    result = run_batch_validation_pipeline(
        BatchValidationConfig(
            start_date=date(2025, 1, 3),
            end_date=date(2025, 1, 24),
            trade_count=99,
            report_path=tmp_path / "range_report.md",
        ),
        trade_runner=runner,
    )

    assert result.entry_dates == (
        date(2025, 1, 3),
        date(2025, 1, 10),
        date(2025, 1, 17),
        date(2025, 1, 24),
    )
    assert tuple(seen_dates) == result.entry_dates
    assert result.metrics.completed_trades == 4


def test_batch_validation_passes_risk_exit_settings(tmp_path: Path) -> None:
    seen_configs: list[SingleTradePipelineConfig] = []

    def runner(config: SingleTradePipelineConfig) -> SingleTradePipelineResult:
        seen_configs.append(config)
        return _single_trade_result(config, Decimal("10"))

    run_batch_validation_pipeline(
        BatchValidationConfig(
            start_date=date(2025, 1, 3),
            trade_count=1,
            take_profit_pct=Decimal("0.50"),
            stop_loss_pct=Decimal("1.00"),
            report_path=tmp_path / "risk_report.md",
        ),
        trade_runner=runner,
    )

    assert seen_configs[0].take_profit_pct == Decimal("0.50")
    assert seen_configs[0].stop_loss_pct == Decimal("1.00")
    report = (tmp_path / "risk_report.md").read_text()
    assert '"take_profit_pct": "0.50"' in report
    assert '"stop_loss_pct": "1.00"' in report


def _single_trade_result(
    config: SingleTradePipelineConfig,
    pnl: Decimal,
) -> SingleTradePipelineResult:
    contract = OptionContract(
        underlying_symbol=config.symbol,
        expiration=date(2025, 2, 14),
        strike=Decimal("550"),
        option_type=OptionType.PUT,
    )
    candidate = OptionSelectionCandidate(
        contract=contract,
        as_of_date=config.entry_date,
        spot_price=Decimal("590"),
        dte=42,
        strike_distance=Decimal("-40"),
        strike_distance_pct=Decimal("-0.0678"),
        delta=Decimal("-0.10"),
        implied_volatility=Decimal("0.20"),
    )
    entry_quote = OptionQuote(
        contract=contract,
        timestamp=datetime.combine(config.entry_date, datetime.min.time(), tzinfo=UTC),
        bid=Decimal("1.99"),
        ask=Decimal("2.01"),
        last=Decimal("2.00"),
        mark=Decimal("2.00"),
    )
    closed = ClosedBacktestPosition(
        position_id=uuid4(),
        contract=contract,
        quantity=-1,
        entry_date=config.entry_date,
        exit_date=date(2025, 2, 14),
        entry_fill_price=Decimal("2.00"),
        exit_fill_price=Decimal("0.00"),
        realized_pnl=pnl,
        exit_reason=ExitReason.EXPIRATION,
    )
    snapshot = BacktestAccountSnapshot(
        date=date(2025, 2, 14),
        cash_balance=config.initial_cash + pnl,
        realized_pnl=pnl,
        unrealized_pnl=Decimal("0"),
        capital_utilization=Decimal("0"),
        equity=config.initial_cash + pnl,
        open_positions=(),
    )
    audit = TradeAudit(
        entry_price=Decimal("2.00"),
        entry_fill_price=Decimal("2.00"),
        entry_gross_credit=Decimal("200"),
        entry_commission=Decimal("0.65"),
        entry_net_cash_flow=Decimal("199.35"),
        exit_price=Decimal("0"),
        exit_fill_price=Decimal("0"),
        exit_gross_debit=Decimal("0"),
        exit_commission=Decimal("0.65"),
        realized_pnl=pnl,
        final_equity=config.initial_cash + pnl,
        exit_reason=ExitReason.EXPIRATION,
    )
    return SingleTradePipelineResult(
        config=config,
        expiration_candidates=(date(2025, 2, 14),),
        chain_contracts=1,
        greek_rows=1,
        quote_rows=1,
        underlying_rows=1,
        selected_candidate=candidate,
        entry_quote=entry_quote,
        backtest_result=BacktestResult(snapshots=(snapshot,), closed_positions=(closed,)),
        audit=audit,
    )
