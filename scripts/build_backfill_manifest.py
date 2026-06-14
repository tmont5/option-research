#!/usr/bin/env python
"""Build a Step 6 backfill manifest before pulling long-range ThetaData rows."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from options_quant.data.ingestion.backfill_plan import (
    BackfillPlanConfig,
    build_backfill_plan,
    write_backfill_manifest,
)
from options_quant.data.models import OptionType


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2018, 1, 1))
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--chunk-days", type=int, default=30)
    parser.add_argument("--entry-spacing-days", type=int, default=7)
    parser.add_argument("--entry-weekday", type=int, default=4)
    parser.add_argument("--min-dte", type=int, default=30)
    parser.add_argument("--max-dte", type=int, default=60)
    parser.add_argument("--option-type", choices=[item.value for item in OptionType], default="put")
    parser.add_argument("--include-raw-rows", action="store_true")
    parser.add_argument("--exclude-open-interest", action="store_true")
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=Path("runs/backfill_manifest/manifest.json"),
    )
    args = parser.parse_args()

    result = build_backfill_plan(
        BackfillPlanConfig(
            symbol=args.symbol,
            start_date=args.start_date,
            end_date=args.end_date,
            chunk_days=args.chunk_days,
            entry_spacing_days=args.entry_spacing_days,
            entry_weekday=args.entry_weekday,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            option_type=OptionType(args.option_type),
            include_open_interest=not args.exclude_open_interest,
            include_raw_rows=args.include_raw_rows,
            manifest_path=args.manifest_path,
        )
    )
    write_backfill_manifest(result)
    print(f"Manifest: {result.config.manifest_path}", flush=True)
    print(f"Markdown: {result.config.manifest_path.with_suffix('.md')}", flush=True)
    print(
        "Plan: "
        f"chunks={len(result.chunks)} "
        f"entry_dates={len(result.entry_dates)} "
        f"tasks={len(result.tasks)} "
        f"tables={','.join(result.tables)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
