from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from options_quant.backtest import (
    BacktestAccountSnapshot,
    BacktestResult,
    ClosedBacktestPosition,
    ExitReason,
)
from options_quant.data.models import OptionContract, OptionQuote, OptionType
from options_quant.pipelines.loser_autopsy import (
    DailyAutopsyRow,
    StopTrigger,
    _stop_triggers,
)
from options_quant.pipelines.single_trade import (
    SingleTradePipelineConfig,
    SingleTradePipelineResult,
    TradeAudit,
)
from options_quant.strategies.selection import OptionSelectionCandidate


def test_stop_triggers_find_first_mark_multiple() -> None:
    trade = _single_trade_result(
        config=_single_trade_result_config(),
        pnl=Decimal("-300"),
    )
    rows = (
        DailyAutopsyRow(
            observed_date=date(2025, 2, 21),
            option_mark=Decimal("2.00"),
            underlying_price=Decimal("590"),
            unrealized_pnl=Decimal("-0.65"),
            multiple_of_credit=Decimal("1"),
        ),
        DailyAutopsyRow(
            observed_date=date(2025, 3, 1),
            option_mark=Decimal("4.50"),
            underlying_price=Decimal("560"),
            unrealized_pnl=Decimal("-250.65"),
            multiple_of_credit=Decimal("2.25"),
        ),
        DailyAutopsyRow(
            observed_date=date(2025, 3, 8),
            option_mark=Decimal("10.25"),
            underlying_price=Decimal("540"),
            unrealized_pnl=Decimal("-825.65"),
            multiple_of_credit=Decimal("5.125"),
        ),
    )

    triggers = _stop_triggers(trade, rows)

    assert triggers == [
        StopTrigger(
            multiple=Decimal("2"),
            trigger_price=Decimal("4.00"),
            observed_date=date(2025, 3, 1),
            option_mark=Decimal("4.50"),
            unrealized_pnl=Decimal("-250.65"),
        ),
        StopTrigger(
            multiple=Decimal("3"),
            trigger_price=Decimal("6.00"),
            observed_date=date(2025, 3, 8),
            option_mark=Decimal("10.25"),
            unrealized_pnl=Decimal("-825.65"),
        ),
        StopTrigger(
            multiple=Decimal("5"),
            trigger_price=Decimal("10.00"),
            observed_date=date(2025, 3, 8),
            option_mark=Decimal("10.25"),
            unrealized_pnl=Decimal("-825.65"),
        ),
    ]


def _single_trade_result_config() -> SingleTradePipelineConfig:
    return SingleTradePipelineConfig(entry_date=date(2025, 2, 21))


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
