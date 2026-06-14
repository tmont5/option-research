#!/usr/bin/env python
"""Run cash-secured portfolio validation for weekly ThetaData trades."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path

from options_quant.data.models import OptionType
from options_quant.pipelines import PortfolioValidationConfig, run_portfolio_validation_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2025, 1, 3))
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        help="Generate weekly entries through this inclusive date. Overrides trade-count.",
    )
    parser.add_argument("--trade-count", type=int, default=5)
    parser.add_argument("--spacing-days", type=int, default=7)
    parser.add_argument("--target-dte", type=int, default=45)
    parser.add_argument("--target-delta", type=Decimal, default=Decimal("-0.10"))
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--initial-cash", type=Decimal, default=Decimal("100000"))
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal("0.65"))
    parser.add_argument("--slippage-per-contract", type=Decimal, default=Decimal("0.00"))
    parser.add_argument(
        "--take-profit-pct",
        type=Decimal,
        help="Close early when option mark falls by this fraction of entry credit, e.g. 0.50.",
    )
    parser.add_argument(
        "--stop-loss-pct",
        type=Decimal,
        help=(
            "Close early when option mark rises by this fraction over entry credit; "
            "1.00 is a 2x credit stop."
        ),
    )
    parser.add_argument("--theta-mdds-host")
    parser.add_argument("--theta-mdds-port")
    parser.add_argument("--theta-mdds-type")
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("runs/portfolio_validation/report.md"),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = run_portfolio_validation_pipeline(
        PortfolioValidationConfig(
            symbol=args.symbol,
            start_date=args.start_date,
            end_date=args.end_date,
            trade_count=args.trade_count,
            spacing_days=args.spacing_days,
            target_dte=args.target_dte,
            target_delta=args.target_delta,
            option_type=OptionType.PUT,
            quantity=args.quantity,
            initial_cash=args.initial_cash,
            commission_per_contract=args.commission_per_contract,
            slippage_per_contract=args.slippage_per_contract,
            take_profit_pct=args.take_profit_pct,
            stop_loss_pct=args.stop_loss_pct,
            theta_mdds_host=args.theta_mdds_host,
            theta_mdds_port=args.theta_mdds_port,
            theta_mdds_type=args.theta_mdds_type,
            report_path=args.report_path,
            verbose=args.verbose,
        )
    )
    print(f"Report: {result.config.report_path}", flush=True)
    print(
        "Portfolio: "
        f"accepted={result.metrics.accepted_trades} "
        f"skipped={result.metrics.skipped_trades} "
        f"failed={result.metrics.failed_trades} "
        f"total_pnl={result.metrics.total_realized_pnl:.2f} "
        f"final_equity={result.metrics.final_equity:.2f}",
        flush=True,
    )
    print(
        "Risk: "
        f"win_rate={result.metrics.win_rate} "
        f"max_drawdown={result.metrics.max_drawdown} "
        f"max_capital_utilization={result.metrics.max_capital_utilization}",
        flush=True,
    )


if __name__ == "__main__":
    main()
