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
from options_quant.pipelines import PortfolioValidationConfig, run_portfolio_validation_pipeline
from options_quant.pipelines.single_trade import (
    SingleTradePipelineConfig,
    SingleTradePipelineResult,
    TradeAudit,
)
from options_quant.strategies.selection import OptionSelectionCandidate


def test_portfolio_validation_skips_when_cash_secured_collateral_is_unavailable(
    tmp_path: Path,
) -> None:
    def runner(config: SingleTradePipelineConfig) -> SingleTradePipelineResult:
        return _single_trade_result(
            config,
            expiration=date(2025, 2, 14),
            strike=Decimal("550"),
            pnl=Decimal("200.70"),
        )

    result = run_portfolio_validation_pipeline(
        PortfolioValidationConfig(
            start_date=date(2025, 1, 3),
            trade_count=2,
            initial_cash=Decimal("100000"),
            report_path=tmp_path / "portfolio.md",
        ),
        trade_runner=runner,
    )

    assert result.metrics.accepted_trades == 1
    assert result.metrics.skipped_trades == 1
    assert result.metrics.failed_trades == 0
    assert result.skipped_trades[0].entry_date == date(2025, 1, 10)
    assert result.skipped_trades[0].required_collateral == Decimal("55000")
    assert result.skipped_trades[0].available_cash == Decimal("45199.35")
    assert result.metrics.final_equity == Decimal("100200.70")
    report = (tmp_path / "portfolio.md").read_text()
    assert '"cash_secured": true' in report
    assert '"skipped_trades": 1' in report


def test_portfolio_validation_reuses_collateral_after_expiration(tmp_path: Path) -> None:
    expirations = {
        date(2025, 1, 3): date(2025, 1, 10),
        date(2025, 1, 10): date(2025, 1, 17),
    }

    def runner(config: SingleTradePipelineConfig) -> SingleTradePipelineResult:
        return _single_trade_result(
            config,
            expiration=expirations[config.entry_date],
            strike=Decimal("550"),
            pnl=Decimal("200.70"),
        )

    result = run_portfolio_validation_pipeline(
        PortfolioValidationConfig(
            start_date=date(2025, 1, 3),
            trade_count=2,
            initial_cash=Decimal("100000"),
            report_path=tmp_path / "portfolio.md",
        ),
        trade_runner=runner,
    )

    assert result.metrics.accepted_trades == 2
    assert result.metrics.skipped_trades == 0
    assert result.metrics.total_realized_pnl == Decimal("401.40")
    assert result.metrics.final_equity == Decimal("100401.40")


def _single_trade_result(
    config: SingleTradePipelineConfig,
    *,
    expiration: date,
    strike: Decimal,
    pnl: Decimal,
) -> SingleTradePipelineResult:
    contract = OptionContract(
        underlying_symbol=config.symbol,
        expiration=expiration,
        strike=strike,
        option_type=OptionType.PUT,
    )
    candidate = OptionSelectionCandidate(
        contract=contract,
        as_of_date=config.entry_date,
        spot_price=Decimal("590"),
        dte=(expiration - config.entry_date).days,
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
        exit_date=expiration,
        entry_fill_price=Decimal("2.00"),
        exit_fill_price=Decimal("0.00"),
        realized_pnl=pnl,
        exit_reason=ExitReason.EXPIRATION,
    )
    entry_snapshot = BacktestAccountSnapshot(
        date=config.entry_date,
        cash_balance=config.initial_cash + Decimal("199.35"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("-0.65"),
        capital_utilization=Decimal("0.55"),
        equity=config.initial_cash - Decimal("0.65"),
        open_positions=(),
    )
    exit_snapshot = BacktestAccountSnapshot(
        date=expiration,
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
        expiration_candidates=(expiration,),
        chain_contracts=1,
        greek_rows=1,
        quote_rows=1,
        underlying_rows=1,
        selected_candidate=candidate,
        entry_quote=entry_quote,
        underlying_prices=(),
        backtest_result=BacktestResult(
            snapshots=(entry_snapshot, exit_snapshot),
            closed_positions=(closed,),
        ),
        audit=audit,
    )
