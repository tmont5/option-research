#!/usr/bin/env python
"""Run an initial assignment-aware wheel validation."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path

from options_quant.pipelines.wheel_validation import (
    WheelValidationConfig,
    run_wheel_validation_pipeline,
)
from options_quant.strategies.wheel import WheelStrategyConfig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2025, 1, 3))
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--trade-count", type=int, default=5)
    parser.add_argument("--spacing-days", type=int, default=7)
    parser.add_argument("--initial-cash", type=Decimal, default=Decimal("100000"))
    parser.add_argument("--put-min-dte", type=int, default=30)
    parser.add_argument("--put-max-dte", type=int, default=60)
    parser.add_argument("--put-target-delta", type=Decimal, default=Decimal("-0.10"))
    parser.add_argument(
        "--allow-concurrent-puts",
        action="store_true",
        help="Allow new cash-secured puts while prior puts are still open if cash is available.",
    )
    parser.add_argument("--call-min-dte", type=int, default=30)
    parser.add_argument("--call-max-dte", type=int, default=45)
    parser.add_argument("--call-target-delta", type=Decimal, default=Decimal("0.20"))
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal("0.65"))
    parser.add_argument("--slippage-per-contract", type=Decimal, default=Decimal("0.00"))
    parser.add_argument("--theta-mdds-host")
    parser.add_argument("--theta-mdds-port")
    parser.add_argument("--theta-mdds-type")
    parser.add_argument("--report-path", type=Path, default=Path("runs/wheel_validation/report.md"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = run_wheel_validation_pipeline(
        WheelValidationConfig(
            strategy=WheelStrategyConfig(
                underlying_symbol=args.symbol,
                initial_cash=args.initial_cash,
                put_min_dte=args.put_min_dte,
                put_max_dte=args.put_max_dte,
                put_target_delta=args.put_target_delta,
                call_min_dte=args.call_min_dte,
                call_max_dte=args.call_max_dte,
                call_target_delta=args.call_target_delta,
                sell_puts_only_when_flat=not args.allow_concurrent_puts,
            ),
            start_date=args.start_date,
            end_date=args.end_date,
            trade_count=args.trade_count,
            spacing_days=args.spacing_days,
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
        "Wheel: "
        f"trades={len(result.option_trades)} events={len(result.events)} "
        f"failed={len(result.failed_entries)} skipped={len(result.skipped_entries)} "
        f"cash={result.cash_balance:.2f} realized_pnl={result.realized_pnl:.2f} "
        f"shares={result.share_quantity} basis={result.share_cost_basis}",
        flush=True,
    )


if __name__ == "__main__":
    main()
