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
from options_quant.data.models import OptionContract, OptionQuote, OptionType, UnderlyingPrice
from options_quant.pipelines.single_trade import (
    SingleTradePipelineConfig,
    SingleTradePipelineResult,
    TradeAudit,
)
from options_quant.pipelines.wheel_validation import (
    WheelValidationConfig,
    run_wheel_validation_pipeline,
)
from options_quant.strategies.selection import OptionSelectionCandidate
from options_quant.strategies.wheel import WheelStrategyConfig


def test_wheel_validation_assigns_put_then_sells_covered_call(tmp_path: Path) -> None:
    def runner(config: SingleTradePipelineConfig) -> SingleTradePipelineResult:
        if config.option_type is OptionType.PUT:
            return _trade(
                config,
                expiration=date(2025, 1, 10),
                strike=Decimal("100"),
                entry=Decimal("2"),
                exit_price=Decimal("5"),
            )
        return _trade(
            config,
            expiration=date(2025, 1, 17),
            strike=Decimal("105"),
            entry=Decimal("1"),
            exit_price=Decimal("6"),
        )

    result = run_wheel_validation_pipeline(
        WheelValidationConfig(
            strategy=WheelStrategyConfig(initial_cash=Decimal("10000")),
            start_date=date(2025, 1, 3),
            trade_count=2,
            report_path=tmp_path / "wheel.md",
        ),
        trade_runner=runner,
    )

    assert [event.event_type for event in result.events] == ["put_assigned", "shares_called_away"]
    assert result.share_quantity == 0
    assert result.share_cost_basis is None
    assert result.realized_pnl == Decimal("798.70")
    assert result.cash_balance == Decimal("10798.70")
    assert result.snapshots
    assert min(snapshot.drawdown for snapshot in result.snapshots) < Decimal("0")
    assert result.snapshots[-1].equity == Decimal("10798.70")
    assert "shares_called_away" in (tmp_path / "wheel.md").read_text()


def test_wheel_validation_skips_call_below_cost_basis(tmp_path: Path) -> None:
    def runner(config: SingleTradePipelineConfig) -> SingleTradePipelineResult:
        if config.option_type is OptionType.PUT:
            return _trade(
                config,
                expiration=date(2025, 1, 10),
                strike=Decimal("100"),
                entry=Decimal("2"),
                exit_price=Decimal("5"),
            )
        return _trade(
            config,
            expiration=date(2025, 1, 17),
            strike=Decimal("95"),
            entry=Decimal("1"),
            exit_price=Decimal("0"),
        )

    result = run_wheel_validation_pipeline(
        WheelValidationConfig(
            strategy=WheelStrategyConfig(initial_cash=Decimal("10000")),
            start_date=date(2025, 1, 3),
            trade_count=2,
            report_path=tmp_path / "wheel.md",
        ),
        trade_runner=runner,
    )

    assert result.share_quantity == 100
    assert result.share_cost_basis == Decimal("98.0065")
    assert result.skipped_entries == (
        (date(2025, 1, 10), "covered call strike 95 below cost basis 98.0065"),
    )
    assert result.snapshots[-1].share_quantity == 100
    assert result.snapshots[-1].stock_value == Decimal("9600")
    assert result.snapshots[-1].open_options == 0


def test_wheel_validation_opens_concurrent_cash_secured_puts_when_allowed(
    tmp_path: Path,
) -> None:
    def runner(config: SingleTradePipelineConfig) -> SingleTradePipelineResult:
        return _trade(
            config,
            expiration=date(2025, 1, 31),
            strike=Decimal("100"),
            entry=Decimal("1"),
            exit_price=Decimal("0"),
        )

    result = run_wheel_validation_pipeline(
        WheelValidationConfig(
            strategy=WheelStrategyConfig(
                initial_cash=Decimal("50000"),
                sell_puts_only_when_flat=False,
            ),
            start_date=date(2025, 1, 3),
            trade_count=3,
            report_path=tmp_path / "wheel.md",
        ),
        trade_runner=runner,
    )

    assert len(result.option_trades) == 3
    assert result.skipped_entries == ()
    assert result.cash_balance == Decimal("50298.05")
    assert result.realized_pnl == Decimal("298.05")
    assert max(snapshot.open_options for snapshot in result.snapshots) == 3


def _trade(
    config: SingleTradePipelineConfig,
    *,
    expiration: date,
    strike: Decimal,
    entry: Decimal,
    exit_price: Decimal,
) -> SingleTradePipelineResult:
    contract = OptionContract(
        underlying_symbol=config.symbol,
        expiration=expiration,
        strike=strike,
        option_type=config.option_type,
    )
    candidate = OptionSelectionCandidate(
        contract=contract,
        as_of_date=config.entry_date,
        spot_price=strike,
        dte=(expiration - config.entry_date).days,
        strike_distance=Decimal("0"),
        strike_distance_pct=Decimal("0"),
        delta=config.target_delta,
        implied_volatility=Decimal("0.20"),
    )
    entry_quote = OptionQuote(
        contract=contract,
        timestamp=datetime.combine(config.entry_date, datetime.min.time(), tzinfo=UTC),
        bid=entry,
        ask=entry,
        last=entry,
        mark=entry,
    )
    pnl = entry * Decimal("100") - Decimal("0.65") - exit_price * Decimal("100") - Decimal("0.65")
    closed = ClosedBacktestPosition(
        position_id=uuid4(),
        contract=contract,
        quantity=-1,
        entry_date=config.entry_date,
        exit_date=expiration,
        entry_fill_price=entry,
        exit_fill_price=exit_price,
        realized_pnl=pnl,
        exit_reason=ExitReason.EXPIRATION,
    )
    entry_snapshot = BacktestAccountSnapshot(
        date=config.entry_date,
        cash_balance=config.initial_cash + entry * Decimal("100") - Decimal("0.65"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=-Decimal("0.65"),
        capital_utilization=Decimal("0"),
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
        entry_price=entry,
        entry_fill_price=entry,
        entry_gross_credit=entry * Decimal("100"),
        entry_commission=Decimal("0.65"),
        entry_net_cash_flow=entry * Decimal("100") - Decimal("0.65"),
        exit_price=exit_price,
        exit_fill_price=exit_price,
        exit_gross_debit=exit_price * Decimal("100"),
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
        underlying_prices=(
            UnderlyingPrice(
                symbol=config.symbol,
                timestamp=datetime.combine(config.entry_date, datetime.min.time(), tzinfo=UTC),
                price=strike,
            ),
            UnderlyingPrice(
                symbol=config.symbol,
                timestamp=datetime.combine(expiration, datetime.min.time(), tzinfo=UTC),
                price=(
                    strike - exit_price
                    if config.option_type is OptionType.PUT
                    else strike + Decimal("1")
                ),
            ),
        ),
        backtest_result=BacktestResult(
            snapshots=(entry_snapshot, exit_snapshot),
            closed_positions=(closed,),
        ),
        audit=audit,
    )
