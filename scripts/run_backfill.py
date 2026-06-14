#!/usr/bin/env python
"""Run a bounded Step 6 ThetaData backfill into DuckDB."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from options_quant.data.ingestion.backfill_runner import (
    BackfillRunnerConfig,
    run_backfill_runner,
)
from options_quant.data.models import OptionType


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2018, 1, 1))
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--chunk-days", type=int, default=30)
    parser.add_argument("--min-dte", type=int, default=30)
    parser.add_argument("--max-dte", type=int, default=60)
    parser.add_argument("--option-type", choices=[item.value for item in OptionType], default="put")
    parser.add_argument("--exclude-open-interest", action="store_true")
    parser.add_argument("--max-chunks", type=int)
    parser.add_argument("--max-contracts", type=int)
    parser.add_argument(
        "--database-path", type=Path, default=Path("runs/backfill/market_data.duckdb")
    )
    parser.add_argument("--manifest-path", type=Path, default=Path("runs/backfill/manifest.json"))
    parser.add_argument("--report-path", type=Path, default=Path("runs/backfill/report.md"))
    parser.add_argument("--reset-database", action="store_true")
    parser.add_argument("--theta-mdds-host")
    parser.add_argument("--theta-mdds-port")
    parser.add_argument("--theta-mdds-type")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = run_backfill_runner(
        BackfillRunnerConfig(
            symbol=args.symbol,
            start_date=args.start_date,
            end_date=args.end_date,
            chunk_days=args.chunk_days,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            option_type=OptionType(args.option_type),
            include_open_interest=not args.exclude_open_interest,
            max_chunks=args.max_chunks,
            max_contracts=args.max_contracts,
            database_path=args.database_path,
            manifest_path=args.manifest_path,
            report_path=args.report_path,
            reset_database=args.reset_database,
            theta_mdds_host=args.theta_mdds_host,
            theta_mdds_port=args.theta_mdds_port,
            theta_mdds_type=args.theta_mdds_type,
            verbose=args.verbose,
        )
    )
    print(f"Database: {result.config.database_path}", flush=True)
    print(f"Manifest: {result.config.manifest_path}", flush=True)
    print(f"Report: {result.config.report_path}", flush=True)
    print(
        "Backfill: "
        f"chunks={result.chunks_completed}/{result.chunks_planned} "
        f"underlying={result.underlying_prices} "
        f"contracts={result.contracts_selected} "
        f"quotes={result.option_quotes} "
        f"greeks={result.option_greeks}",
        flush=True,
    )


if __name__ == "__main__":
    main()
