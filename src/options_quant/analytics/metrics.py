"""Backtest performance analytics."""

from __future__ import annotations

from decimal import Decimal
from math import sqrt
from statistics import mean, pstdev

from pydantic import BaseModel, ConfigDict, Field

from options_quant.backtest import BacktestResult

TRADING_DAYS_PER_YEAR = Decimal("252")
SQRT_TRADING_DAYS = Decimal(str(sqrt(252)))
ZERO = Decimal("0")
ONE = Decimal("1")


class PerformanceAnalyticsModel(BaseModel):
    """Base configuration for immutable analytics objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PerformanceReport(PerformanceAnalyticsModel):
    """Computed backtest performance metrics."""

    cagr: Decimal = Field(description="Compound annual growth rate.")
    annualized_return: Decimal = Field(description="Arithmetic annualized return.")
    annualized_volatility: Decimal = Field(ge=ZERO, description="Annualized return volatility.")
    sharpe_ratio: Decimal | None = Field(description="Annualized Sharpe ratio.")
    sortino_ratio: Decimal | None = Field(description="Annualized Sortino ratio.")
    maximum_drawdown: Decimal = Field(le=ZERO, description="Maximum drawdown as a negative return.")
    calmar_ratio: Decimal | None = Field(description="CAGR divided by absolute max drawdown.")
    win_rate: Decimal | None = Field(description="Winning closed trades divided by closed trades.")
    average_win: Decimal | None = Field(description="Average realized PnL of winning trades.")
    average_loss: Decimal | None = Field(description="Average realized PnL of losing trades.")
    profit_factor: Decimal | None = Field(description="Gross profit divided by gross loss.")
    expectancy: Decimal | None = Field(description="Average expected PnL per closed trade.")
    benchmark_annualized_return: Decimal | None = Field(
        default=None,
        description="Arithmetic annualized benchmark return.",
    )
    alpha: Decimal | None = Field(
        default=None,
        description="Annualized return unexplained by benchmark beta.",
    )
    beta: Decimal | None = Field(default=None, description="Strategy beta to benchmark returns.")
    tracking_error: Decimal | None = Field(
        default=None,
        ge=ZERO,
        description="Annualized standard deviation of active returns.",
    )
    information_ratio: Decimal | None = Field(
        default=None,
        description="Annualized active return divided by tracking error.",
    )


class PerformanceAnalyzerConfig(PerformanceAnalyticsModel):
    """Configuration for performance analytics."""

    periods_per_year: Decimal = Field(
        default=TRADING_DAYS_PER_YEAR,
        gt=ZERO,
        description="Return periods per year.",
    )
    risk_free_rate: Decimal = Field(
        default=ZERO,
        description="Annual risk-free rate used for Sharpe and Sortino.",
    )


class EquityCurvePoint(PerformanceAnalyticsModel):
    """A point in an equity curve."""

    index: int = Field(ge=0, description="Ordered observation index.")
    equity: Decimal = Field(gt=ZERO, description="Account equity.")


class PerformanceAnalyzer:
    """Compute performance metrics from backtest results."""

    def __init__(self, config: PerformanceAnalyzerConfig | None = None) -> None:
        self._config = config if config is not None else PerformanceAnalyzerConfig()

    def analyze(
        self,
        result: BacktestResult,
        benchmark_returns: list[Decimal] | None = None,
    ) -> PerformanceReport:
        """Return a complete performance report for a backtest result."""
        equity_curve = [
            EquityCurvePoint(index=index, equity=snapshot.equity)
            for index, snapshot in enumerate(result.snapshots)
        ]
        if not equity_curve:
            raise ValueError("backtest result must contain at least one snapshot")
        returns = _period_returns(equity_curve)
        aligned_benchmark_returns = _aligned_benchmark_returns(returns, benchmark_returns)
        trade_pnls = [position.realized_pnl for position in result.closed_positions]
        max_drawdown = maximum_drawdown(equity_curve)
        cagr_value = cagr(equity_curve, self._config.periods_per_year)
        annualized_return_value = annualized_return(returns, self._config.periods_per_year)
        return PerformanceReport(
            cagr=cagr_value,
            annualized_return=annualized_return_value,
            annualized_volatility=annualized_volatility(
                returns,
                self._config.periods_per_year,
            ),
            sharpe_ratio=sharpe_ratio(
                returns,
                periods_per_year=self._config.periods_per_year,
                risk_free_rate=self._config.risk_free_rate,
            ),
            sortino_ratio=sortino_ratio(
                returns,
                periods_per_year=self._config.periods_per_year,
                risk_free_rate=self._config.risk_free_rate,
            ),
            maximum_drawdown=max_drawdown,
            calmar_ratio=calmar_ratio(cagr_value, max_drawdown),
            win_rate=win_rate(trade_pnls),
            average_win=average_win(trade_pnls),
            average_loss=average_loss(trade_pnls),
            profit_factor=profit_factor(trade_pnls),
            expectancy=expectancy(trade_pnls),
            benchmark_annualized_return=(
                annualized_return(aligned_benchmark_returns, self._config.periods_per_year)
                if aligned_benchmark_returns is not None
                else None
            ),
            alpha=alpha(
                returns,
                aligned_benchmark_returns,
                periods_per_year=self._config.periods_per_year,
                risk_free_rate=self._config.risk_free_rate,
            )
            if aligned_benchmark_returns is not None
            else None,
            beta=beta(returns, aligned_benchmark_returns)
            if aligned_benchmark_returns is not None
            else None,
            tracking_error=tracking_error(
                returns,
                aligned_benchmark_returns,
                periods_per_year=self._config.periods_per_year,
            )
            if aligned_benchmark_returns is not None
            else None,
            information_ratio=information_ratio(
                returns,
                aligned_benchmark_returns,
                periods_per_year=self._config.periods_per_year,
            )
            if aligned_benchmark_returns is not None
            else None,
        )


def cagr(equity_curve: list[EquityCurvePoint], periods_per_year: Decimal) -> Decimal:
    """Compute compound annual growth rate from an equity curve."""
    _validate_equity_curve(equity_curve)
    if len(equity_curve) == 1:
        return ZERO
    total_return = equity_curve[-1].equity / equity_curve[0].equity
    years = Decimal(len(equity_curve) - 1) / periods_per_year
    return Decimal(str(float(total_return) ** (1 / float(years)))) - ONE


def annualized_return(returns: list[Decimal], periods_per_year: Decimal) -> Decimal:
    """Compute arithmetic annualized return from periodic returns."""
    if not returns:
        return ZERO
    return _decimal_mean(returns) * periods_per_year


def annualized_volatility(returns: list[Decimal], periods_per_year: Decimal) -> Decimal:
    """Compute annualized population volatility from periodic returns."""
    if len(returns) < 2:
        return ZERO
    return _decimal_pstdev(returns) * Decimal(str(sqrt(float(periods_per_year))))


def sharpe_ratio(
    returns: list[Decimal],
    *,
    periods_per_year: Decimal = TRADING_DAYS_PER_YEAR,
    risk_free_rate: Decimal = ZERO,
) -> Decimal | None:
    """Compute annualized Sharpe ratio from periodic returns."""
    volatility = annualized_volatility(returns, periods_per_year)
    if volatility == ZERO:
        return None
    excess_return = annualized_return(returns, periods_per_year) - risk_free_rate
    return excess_return / volatility


def sortino_ratio(
    returns: list[Decimal],
    *,
    periods_per_year: Decimal = TRADING_DAYS_PER_YEAR,
    risk_free_rate: Decimal = ZERO,
) -> Decimal | None:
    """Compute annualized Sortino ratio from periodic returns."""
    downside_returns = [period_return for period_return in returns if period_return < ZERO]
    downside_deviation = annualized_volatility(downside_returns, periods_per_year)
    if downside_deviation == ZERO:
        return None
    excess_return = annualized_return(returns, periods_per_year) - risk_free_rate
    return excess_return / downside_deviation


def maximum_drawdown(equity_curve: list[EquityCurvePoint]) -> Decimal:
    """Compute maximum drawdown as the most negative peak-to-trough return."""
    _validate_equity_curve(equity_curve)
    peak = equity_curve[0].equity
    max_drawdown = ZERO
    for point in equity_curve:
        peak = max(peak, point.equity)
        drawdown = point.equity / peak - ONE
        max_drawdown = min(max_drawdown, drawdown)
    return max_drawdown


def calmar_ratio(cagr_value: Decimal, max_drawdown: Decimal) -> Decimal | None:
    """Compute Calmar ratio."""
    if max_drawdown == ZERO:
        return None
    return cagr_value / abs(max_drawdown)


def beta(strategy_returns: list[Decimal], benchmark_returns: list[Decimal]) -> Decimal | None:
    """Compute strategy beta to benchmark periodic returns."""
    _validate_aligned_returns(strategy_returns, benchmark_returns)
    benchmark_variance = _population_variance(benchmark_returns)
    if benchmark_variance == ZERO:
        return None
    return _population_covariance(strategy_returns, benchmark_returns) / benchmark_variance


def alpha(
    strategy_returns: list[Decimal],
    benchmark_returns: list[Decimal],
    *,
    periods_per_year: Decimal = TRADING_DAYS_PER_YEAR,
    risk_free_rate: Decimal = ZERO,
) -> Decimal | None:
    """Compute annualized CAPM-style alpha."""
    beta_value = beta(strategy_returns, benchmark_returns)
    if beta_value is None:
        return None
    strategy_annual_return = annualized_return(strategy_returns, periods_per_year)
    benchmark_annual_return = annualized_return(benchmark_returns, periods_per_year)
    return strategy_annual_return - (
        risk_free_rate + beta_value * (benchmark_annual_return - risk_free_rate)
    )


def tracking_error(
    strategy_returns: list[Decimal],
    benchmark_returns: list[Decimal],
    *,
    periods_per_year: Decimal = TRADING_DAYS_PER_YEAR,
) -> Decimal:
    """Compute annualized tracking error from active returns."""
    _validate_aligned_returns(strategy_returns, benchmark_returns)
    active_returns = [
        strategy_return - benchmark_return
        for strategy_return, benchmark_return in zip(
            strategy_returns,
            benchmark_returns,
            strict=True,
        )
    ]
    return annualized_volatility(active_returns, periods_per_year)


def information_ratio(
    strategy_returns: list[Decimal],
    benchmark_returns: list[Decimal],
    *,
    periods_per_year: Decimal = TRADING_DAYS_PER_YEAR,
) -> Decimal | None:
    """Compute annualized information ratio."""
    active_risk = tracking_error(
        strategy_returns,
        benchmark_returns,
        periods_per_year=periods_per_year,
    )
    if active_risk == ZERO:
        return None
    active_return = annualized_return(
        [
            strategy_return - benchmark_return
            for strategy_return, benchmark_return in zip(
                strategy_returns,
                benchmark_returns,
                strict=True,
            )
        ],
        periods_per_year,
    )
    return active_return / active_risk


def win_rate(trade_pnls: list[Decimal]) -> Decimal | None:
    """Compute winning trade percentage."""
    if not trade_pnls:
        return None
    wins = sum(1 for pnl in trade_pnls if pnl > ZERO)
    return Decimal(wins) / Decimal(len(trade_pnls))


def average_win(trade_pnls: list[Decimal]) -> Decimal | None:
    """Compute average winning trade PnL."""
    wins = [pnl for pnl in trade_pnls if pnl > ZERO]
    if not wins:
        return None
    return sum(wins, ZERO) / Decimal(len(wins))


def average_loss(trade_pnls: list[Decimal]) -> Decimal | None:
    """Compute average losing trade PnL."""
    losses = [pnl for pnl in trade_pnls if pnl < ZERO]
    if not losses:
        return None
    return sum(losses, ZERO) / Decimal(len(losses))


def profit_factor(trade_pnls: list[Decimal]) -> Decimal | None:
    """Compute gross profit divided by gross loss."""
    if not trade_pnls:
        return None
    gross_profit = sum((pnl for pnl in trade_pnls if pnl > ZERO), ZERO)
    gross_loss = abs(sum((pnl for pnl in trade_pnls if pnl < ZERO), ZERO))
    if gross_loss == ZERO:
        return None
    return gross_profit / gross_loss


def expectancy(trade_pnls: list[Decimal]) -> Decimal | None:
    """Compute expected PnL per trade."""
    if not trade_pnls:
        return None
    return sum(trade_pnls, ZERO) / Decimal(len(trade_pnls))


def _period_returns(equity_curve: list[EquityCurvePoint]) -> list[Decimal]:
    return [
        equity_curve[index].equity / equity_curve[index - 1].equity - ONE
        for index in range(1, len(equity_curve))
    ]


def _decimal_mean(values: list[Decimal]) -> Decimal:
    return Decimal(str(mean(values)))


def _decimal_pstdev(values: list[Decimal]) -> Decimal:
    return Decimal(str(pstdev(values)))


def _population_variance(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return ZERO
    average = _decimal_mean(values)
    return sum(((value - average) ** 2 for value in values), ZERO) / Decimal(len(values))


def _population_covariance(first: list[Decimal], second: list[Decimal]) -> Decimal:
    _validate_aligned_returns(first, second)
    first_average = _decimal_mean(first)
    second_average = _decimal_mean(second)
    return sum(
        (
            (first_value - first_average) * (second_value - second_average)
            for first_value, second_value in zip(first, second, strict=True)
        ),
        ZERO,
    ) / Decimal(len(first))


def _aligned_benchmark_returns(
    strategy_returns: list[Decimal],
    benchmark_returns: list[Decimal] | None,
) -> list[Decimal] | None:
    if benchmark_returns is None:
        return None
    _validate_aligned_returns(strategy_returns, benchmark_returns)
    return benchmark_returns


def _validate_aligned_returns(first: list[Decimal], second: list[Decimal]) -> None:
    if len(first) != len(second):
        raise ValueError("strategy and benchmark returns must have the same length")
    if not first:
        raise ValueError("strategy and benchmark returns must not be empty")


def _validate_equity_curve(equity_curve: list[EquityCurvePoint]) -> None:
    if not equity_curve:
        raise ValueError("equity curve must contain at least one point")
