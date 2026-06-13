#!/usr/bin/env python
"""Run a small weekly batch of auditable ThetaData trades."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path

from options_quant.data.models import OptionType
from options_quant.pipelines import BatchValidationConfig, run_batch_validation_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2025, 1, 3))
    parser.add_argument("--trade-count", type=int, default=5)
    parser.add_argument("--spacing-days", type=int, default=7)
    parser.add_argument("--target-dte", type=int, default=45)
    parser.add_argument("--target-delta", type=Decimal, default=Decimal("-0.10"))
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal("0.65"))
    parser.add_argument("--slippage-per-contract", type=Decimal, default=Decimal("0.00"))
    parser.add_argument("--theta-mdds-host")
    parser.add_argument("--theta-mdds-port")
    parser.add_argument("--theta-mdds-type")
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("runs/batch_validation/report.md"),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = run_batch_validation_pipeline(
        BatchValidationConfig(
            symbol=args.symbol,
            start_date=args.start_date,
            trade_count=args.trade_count,
            spacing_days=args.spacing_days,
            target_dte=args.target_dte,
            target_delta=args.target_delta,
            option_type=OptionType.PUT,
            quantity=args.quantity,
            commission_per_contract=args.commission_per_contract,
            slippage_per_contract=args.slippage_per_contract,
            theta_mdds_host=args.theta_mdds_host,
            theta_mdds_port=args.theta_mdds_port,
            theta_mdds_type=args.theta_mdds_type,
            report_path=args.report_path,
            verbose=args.verbose,
        )
    )
    print(f"Report: {result.config.report_path}", flush=True)
    print(
        "Batch: "
        f"completed={result.metrics.completed_trades} failed={result.metrics.failed_trades} "
        f"total_pnl={result.metrics.total_realized_pnl:.2f} "
        f"final_equity={result.metrics.final_equity:.2f}",
        flush=True,
    )
    print(
        "Risk: "
        f"win_rate={result.metrics.win_rate} "
        f"per_trade_sharpe={_ratio_text(result.metrics.per_trade_sharpe)} "
        f"sharpe_note={result.metrics.sharpe_note} "
        f"max_drawdown={result.metrics.max_drawdown}",
        flush=True,
    )


def _ratio_text(value: Decimal | None) -> str:
    if value is None:
        return "None"
    return f"{value:.4f}"


if __name__ == "__main__":
    main()
