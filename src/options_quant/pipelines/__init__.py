"""Runnable research pipelines."""

from options_quant.pipelines.one_week import (
    OneWeekPipelineConfig,
    OneWeekPipelineResult,
    run_one_week_pipeline,
)
from options_quant.pipelines.single_trade import (
    SingleTradePipelineConfig,
    SingleTradePipelineResult,
    run_single_trade_pipeline,
)

__all__ = [
    "OneWeekPipelineConfig",
    "OneWeekPipelineResult",
    "SingleTradePipelineConfig",
    "SingleTradePipelineResult",
    "run_one_week_pipeline",
    "run_single_trade_pipeline",
]
