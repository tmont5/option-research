#!/usr/bin/env python
"""Run the one-week live ThetaData pipeline."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path

from options_quant.data.models import OptionType
from options_quant.pipelines import OneWeekPipelineConfig, run_one_week_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="ANET")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 6, 8))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2026, 6, 12))
    parser.add_argument("--min-dte", type=int, default=30)
    parser.add_argument("--max-dte", type=int, default=45)
    parser.add_argument("--target-delta", type=Decimal, default=Decimal("-0.30"))
    parser.add_argument("--max-contracts", type=int)
    parser.add_argument("--min-strike", type=Decimal)
    parser.add_argument("--max-strike", type=Decimal)
    parser.add_argument(
        "--theta-mdds-host",
        help="ThetaData MDDS host override, e.g. 127.0.0.1 or localhost.",
    )
    parser.add_argument(
        "--theta-mdds-port",
        help="ThetaData MDDS port override. Must match the running Theta Terminal.",
    )
    parser.add_argument(
        "--theta-mdds-type",
        help="ThetaData MDDS environment override, e.g. PROD or STAGE.",
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=Path("runs/one_week_pipeline/pipeline.duckdb"),
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("runs/one_week_pipeline/report.md"),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = run_one_week_pipeline(
        OneWeekPipelineConfig(
            symbol=args.symbol,
            start_date=args.start_date,
            end_date=args.end_date,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            option_type=OptionType.PUT,
            target_delta=args.target_delta,
            max_contracts=args.max_contracts,
            min_strike=args.min_strike,
            max_strike=args.max_strike,
            theta_mdds_host=args.theta_mdds_host,
            theta_mdds_port=args.theta_mdds_port,
            theta_mdds_type=args.theta_mdds_type,
            database_path=args.database_path,
            report_path=args.report_path,
            verbose=args.verbose,
        )
    )
    selected = result.selected_candidate
    final_snapshot = result.backtest_result.snapshots[-1]
    print(f"Report: {result.config.report_path}", flush=True)
    print(f"DuckDB: {result.config.database_path}", flush=True)
    print(
        "Selected: "
        f"{selected.contract.underlying_symbol} {selected.contract.expiration} "
        f"{selected.contract.strike} {selected.contract.option_type.value} "
        f"delta={selected.delta} iv={selected.implied_volatility}",
        flush=True,
    )
    print(
        "Final: "
        f"date={final_snapshot.date} equity={final_snapshot.equity} "
        f"unrealized_pnl={final_snapshot.unrealized_pnl}",
        flush=True,
    )


if __name__ == "__main__":
    main()
