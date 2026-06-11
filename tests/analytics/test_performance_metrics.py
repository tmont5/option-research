from datetime import date
from decimal import Decimal
from math import sqrt
from statistics import mean, pstdev
from uuid import uuid4

import pytest

from options_quant.analytics import (
    EquityCurvePoint,
    PerformanceAnalyzer,
    PerformanceAnalyzerConfig,
    alpha,
    annualized_return,
    annualized_volatility,
    average_loss,
    average_win,
    beta,
    cagr,
    calmar_ratio,
    expectancy,
    information_ratio,
    maximum_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    tracking_error,
    win_rate,
)
from options_quant.backtest import (
    BacktestAccountSnapshot,
    BacktestResult,
    ClosedBacktestPosition,
    ExitReason,
)
from options_quant.data.models import OptionContract, OptionType


def assert_decimal_close(actual: Decimal | None, expected: float) -> None:
    assert actual is not None
    assert float(actual) == pytest.approx(expected)


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
        entry_fill_price=Decimal("2"),
        exit_fill_price=Decimal("1"),
        realized_pnl=realized_pnl,
        exit_reason=ExitReason.ORDER,
    )


def make_equity_curve() -> list[EquityCurvePoint]:
    return [
        EquityCurvePoint(index=0, equity=Decimal("100")),
        EquityCurvePoint(index=1, equity=Decimal("110")),
        EquityCurvePoint(index=2, equity=Decimal("104.5")),
        EquityCurvePoint(index=3, equity=Decimal("102.41")),
        EquityCurvePoint(index=4, equity=Decimal("120")),
    ]


def test_performance_analyzer_computes_known_backtest_metrics() -> None:
    equity_values = [
        Decimal("100"),
        Decimal("110"),
        Decimal("104.5"),
        Decimal("102.41"),
        Decimal("120"),
    ]
    result = BacktestResult(
        snapshots=tuple(make_snapshot(index, equity) for index, equity in enumerate(equity_values)),
        closed_positions=tuple(
            make_closed_position(pnl)
            for pnl in [
                Decimal("100"),
                Decimal("-50"),
                Decimal("200"),
                Decimal("-25"),
                Decimal("0"),
            ]
        ),
    )
    returns = [0.10, -0.05, -0.02, float(Decimal("120") / Decimal("102.41") - Decimal("1"))]
    annual_return = mean(returns) * 4
    annual_vol = pstdev(returns) * sqrt(4)
    downside_vol = pstdev([-0.05, -0.02]) * sqrt(4)

    report = PerformanceAnalyzer(
        PerformanceAnalyzerConfig(periods_per_year=Decimal("4"))
    ).analyze(result)

    assert_decimal_close(report.cagr, 0.20)
    assert_decimal_close(report.annualized_return, annual_return)
    assert_decimal_close(report.annualized_volatility, annual_vol)
    assert_decimal_close(report.sharpe_ratio, annual_return / annual_vol)
    assert_decimal_close(report.sortino_ratio, annual_return / downside_vol)
    assert_decimal_close(report.maximum_drawdown, -0.069)
    assert_decimal_close(report.calmar_ratio, 0.20 / 0.069)
    assert report.win_rate == Decimal("0.4")
    assert report.average_win == Decimal("150")
    assert report.average_loss == Decimal("-37.5")
    assert report.profit_factor == Decimal("4")
    assert report.expectancy == Decimal("45")
    assert report.benchmark_annualized_return is None
    assert report.alpha is None
    assert report.beta is None
    assert report.tracking_error is None
    assert report.information_ratio is None


def test_performance_analyzer_computes_known_benchmark_relative_metrics() -> None:
    equity_values = [
        Decimal("100"),
        Decimal("110"),
        Decimal("104.5"),
        Decimal("102.41"),
        Decimal("120"),
    ]
    result = BacktestResult(
        snapshots=tuple(make_snapshot(index, equity) for index, equity in enumerate(equity_values)),
        closed_positions=(),
    )
    strategy_returns = [
        0.10,
        -0.05,
        -0.02,
        float(Decimal("120") / Decimal("102.41") - Decimal("1")),
    ]
    benchmark_returns = [
        Decimal("0.05"),
        Decimal("-0.02"),
        Decimal("0.01"),
        Decimal("0.04"),
    ]
    benchmark_float_returns = [float(value) for value in benchmark_returns]
    expected_beta = (
        sum(
            (strategy_return - mean(strategy_returns))
            * (benchmark_return - mean(benchmark_float_returns))
            for strategy_return, benchmark_return in zip(
                strategy_returns,
                benchmark_float_returns,
                strict=True,
            )
        )
        / len(strategy_returns)
    ) / pstdev(benchmark_float_returns) ** 2
    annual_strategy_return = mean(strategy_returns) * 4
    annual_benchmark_return = mean(benchmark_float_returns) * 4
    active_returns = [
        strategy_return - benchmark_return
        for strategy_return, benchmark_return in zip(
            strategy_returns,
            benchmark_float_returns,
            strict=True,
        )
    ]
    expected_tracking_error = pstdev(active_returns) * sqrt(4)

    report = PerformanceAnalyzer(
        PerformanceAnalyzerConfig(periods_per_year=Decimal("4"))
    ).analyze(result, benchmark_returns=benchmark_returns)

    assert_decimal_close(report.benchmark_annualized_return, annual_benchmark_return)
    assert_decimal_close(report.beta, expected_beta)
    assert_decimal_close(
        report.alpha,
        annual_strategy_return - expected_beta * annual_benchmark_return,
    )
    assert_decimal_close(report.tracking_error, expected_tracking_error)
    assert_decimal_close(
        report.information_ratio,
        (annual_strategy_return - annual_benchmark_return) / expected_tracking_error,
    )


def test_return_and_risk_metric_functions_use_known_inputs() -> None:
    equity_curve = make_equity_curve()
    returns = [
        Decimal("0.10"),
        Decimal("-0.05"),
        Decimal("-0.02"),
        Decimal("0.1717605702568108583146177131"),
    ]

    assert_decimal_close(cagr(equity_curve, Decimal("4")), 0.20)
    assert_decimal_close(annualized_return(returns, Decimal("4")), sum(float(r) for r in returns))
    assert_decimal_close(
        annualized_volatility(returns, Decimal("4")),
        pstdev([float(value) for value in returns]) * sqrt(4),
    )
    assert_decimal_close(maximum_drawdown(equity_curve), -0.069)


def test_benchmark_relative_metric_functions_use_known_inputs() -> None:
    strategy_returns = [
        Decimal("0.10"),
        Decimal("-0.05"),
        Decimal("-0.02"),
        Decimal("0.1717605702568108583146177131"),
    ]
    benchmark_returns = [
        Decimal("0.05"),
        Decimal("-0.02"),
        Decimal("0.01"),
        Decimal("0.04"),
    ]
    strategy_float_returns = [float(value) for value in strategy_returns]
    benchmark_float_returns = [float(value) for value in benchmark_returns]
    expected_beta = (
        sum(
            (strategy_return - mean(strategy_float_returns))
            * (benchmark_return - mean(benchmark_float_returns))
            for strategy_return, benchmark_return in zip(
                strategy_float_returns,
                benchmark_float_returns,
                strict=True,
            )
        )
        / len(strategy_float_returns)
    ) / pstdev(benchmark_float_returns) ** 2
    active_returns = [
        strategy_return - benchmark_return
        for strategy_return, benchmark_return in zip(
            strategy_float_returns,
            benchmark_float_returns,
            strict=True,
        )
    ]
    expected_tracking_error = pstdev(active_returns) * sqrt(4)

    assert_decimal_close(beta(strategy_returns, benchmark_returns), expected_beta)
    assert_decimal_close(
        alpha(strategy_returns, benchmark_returns, periods_per_year=Decimal("4")),
        mean(strategy_float_returns) * 4 - expected_beta * mean(benchmark_float_returns) * 4,
    )
    assert_decimal_close(
        tracking_error(strategy_returns, benchmark_returns, periods_per_year=Decimal("4")),
        expected_tracking_error,
    )
    assert_decimal_close(
        information_ratio(strategy_returns, benchmark_returns, periods_per_year=Decimal("4")),
        ((mean(strategy_float_returns) - mean(benchmark_float_returns)) * 4)
        / expected_tracking_error,
    )


def test_sharpe_and_sortino_return_none_when_volatility_is_zero() -> None:
    returns = [Decimal("0.01"), Decimal("0.01"), Decimal("0.01")]

    assert sharpe_ratio(returns, periods_per_year=Decimal("252")) is None
    assert sortino_ratio(returns, periods_per_year=Decimal("252")) is None


def test_calmar_ratio_returns_none_when_no_drawdown() -> None:
    assert calmar_ratio(Decimal("0.10"), Decimal("0")) is None


def test_benchmark_relative_metrics_handle_zero_benchmark_variance() -> None:
    strategy_returns = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]
    benchmark_returns = [Decimal("0.01"), Decimal("0.01"), Decimal("0.01")]

    assert beta(strategy_returns, benchmark_returns) is None
    assert alpha(strategy_returns, benchmark_returns) is None


def test_benchmark_relative_metrics_reject_misaligned_returns() -> None:
    with pytest.raises(ValueError, match="same length"):
        beta([Decimal("0.01")], [Decimal("0.01"), Decimal("0.02")])

    with pytest.raises(ValueError, match="must not be empty"):
        tracking_error([], [])


def test_trade_metrics_handle_wins_losses_and_breakeven_trades() -> None:
    trade_pnls = [
        Decimal("100"),
        Decimal("-50"),
        Decimal("200"),
        Decimal("-25"),
        Decimal("0"),
    ]

    assert win_rate(trade_pnls) == Decimal("0.4")
    assert average_win(trade_pnls) == Decimal("150")
    assert average_loss(trade_pnls) == Decimal("-37.5")
    assert profit_factor(trade_pnls) == Decimal("4")
    assert expectancy(trade_pnls) == Decimal("45")


def test_trade_metrics_return_none_without_closed_trades() -> None:
    assert win_rate([]) is None
    assert average_win([]) is None
    assert average_loss([]) is None
    assert profit_factor([]) is None
    assert expectancy([]) is None


def test_profit_factor_returns_none_when_there_are_no_losses() -> None:
    assert profit_factor([Decimal("100"), Decimal("50")]) is None


def test_equity_curve_metrics_reject_empty_curves() -> None:
    with pytest.raises(ValueError, match="equity curve must contain at least one point"):
        cagr([], Decimal("252"))

    with pytest.raises(ValueError, match="equity curve must contain at least one point"):
        maximum_drawdown([])
