#!/usr/bin/env python
"""Run a wheel-shaped compact ThetaData backfill into DuckDB."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from options_quant.data.ingestion.thetadata_eod import (
    ThetaDataEODIngestionConfig,
    ThetaDataEODIngestionPipeline,
    ThetaDataEODIngestionResult,
)
from options_quant.data.models import OptionType
from options_quant.data.providers import ThetaDataProvider, ThetaDataPythonClient
from options_quant.data.storage import DuckDBStorage


@dataclass(frozen=True)
class EntryBackfillResult:
    entry_date: date
    option_type: OptionType
    start_date: date
    end_date: date
    target_delta: Decimal
    result: ThetaDataEODIngestionResult


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--spacing-days", type=int, default=7)
    parser.add_argument(
        "--entry-weekday",
        type=int,
        default=4,
        help="Monday=0, Friday=4.",
    )
    parser.add_argument("--put-min-dte", type=int, default=30)
    parser.add_argument("--put-max-dte", type=int, default=35)
    parser.add_argument("--put-target-delta", type=Decimal, default=Decimal("-0.25"))
    parser.add_argument("--call-min-dte", type=int, default=30)
    parser.add_argument("--call-max-dte", type=int, default=35)
    parser.add_argument("--call-target-delta", type=Decimal, default=Decimal("0.25"))
    parser.add_argument("--contracts-around-target", type=int, default=7)
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--reset-database", action="store_true")
    parser.add_argument("--theta-mdds-host")
    parser.add_argument("--theta-mdds-port")
    parser.add_argument("--theta-mdds-type")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.reset_database and args.database_path.exists():
        args.database_path.unlink()
    args.database_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    provider = ThetaDataProvider(
        ThetaDataPythonClient(
            mdds_host=args.theta_mdds_host,
            mdds_port=args.theta_mdds_port,
            mdds_type=args.theta_mdds_type,
        )
    )
    storage = DuckDBStorage(args.database_path)
    pipeline = ThetaDataEODIngestionPipeline(provider, storage)
    results: list[EntryBackfillResult] = []
    try:
        for entry_date in _entry_dates(
            args.start_date,
            args.end_date,
            spacing_days=args.spacing_days,
            entry_weekday=args.entry_weekday,
        ):
            results.append(
                _ingest_entry(
                    pipeline,
                    symbol=args.symbol,
                    entry_date=entry_date,
                    option_type=OptionType.PUT,
                    min_dte=args.put_min_dte,
                    max_dte=args.put_max_dte,
                    target_delta=args.put_target_delta,
                    contracts_around_target=args.contracts_around_target,
                    verbose=args.verbose,
                )
            )
            results.append(
                _ingest_entry(
                    pipeline,
                    symbol=args.symbol,
                    entry_date=entry_date,
                    option_type=OptionType.CALL,
                    min_dte=args.call_min_dte,
                    max_dte=args.call_max_dte,
                    target_delta=args.call_target_delta,
                    contracts_around_target=args.contracts_around_target,
                    verbose=args.verbose,
                )
            )
    finally:
        storage.close()

    args.report_path.write_text(
        json.dumps(_report_payload(args, results), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Database: {args.database_path}", flush=True)
    print(f"Report: {args.report_path}", flush=True)
    print(
        "Compact wheel backfill: "
        f"entries={len({result.entry_date for result in results})} "
        f"passes={len(results)} "
        f"underlying={sum(result.result.underlying_prices for result in results)} "
        f"contracts={sum(result.result.contracts_selected for result in results)} "
        f"quotes={sum(result.result.option_quotes for result in results)} "
        f"greeks={sum(result.result.option_greeks for result in results)}",
        flush=True,
    )


def _ingest_entry(
    pipeline: ThetaDataEODIngestionPipeline,
    *,
    symbol: str,
    entry_date: date,
    option_type: OptionType,
    min_dte: int,
    max_dte: int,
    target_delta: Decimal,
    contracts_around_target: int,
    verbose: bool,
) -> EntryBackfillResult:
    end_date = entry_date + timedelta(days=max_dte)
    if verbose:
        print(
            f"ingesting {entry_date} {option_type.value} "
            f"{min_dte}-{max_dte} DTE target_delta={target_delta}",
            flush=True,
        )
    result = pipeline.ingest(
        ThetaDataEODIngestionConfig(
            symbol=symbol,
            start_date=entry_date,
            end_date=end_date,
            chain_as_of_date=entry_date,
            min_dte=min_dte,
            max_dte=max_dte,
            option_type=option_type,
            target_delta=target_delta,
            contracts_around_target=contracts_around_target,
        )
    )
    return EntryBackfillResult(
        entry_date=entry_date,
        option_type=option_type,
        start_date=entry_date,
        end_date=end_date,
        target_delta=target_delta,
        result=result,
    )


def _entry_dates(
    start_date: date,
    end_date: date,
    *,
    spacing_days: int,
    entry_weekday: int,
) -> list[date]:
    first = start_date
    while first.weekday() != entry_weekday:
        first += timedelta(days=1)
    dates: list[date] = []
    current = first
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=spacing_days)
    return dates


def _report_payload(
    args: argparse.Namespace,
    results: list[EntryBackfillResult],
) -> dict[str, object]:
    return {
        "symbol": args.symbol,
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "spacing_days": args.spacing_days,
        "entry_weekday": args.entry_weekday,
        "database_path": str(args.database_path),
        "contracts_around_target": args.contracts_around_target,
        "put": {
            "min_dte": args.put_min_dte,
            "max_dte": args.put_max_dte,
            "target_delta": str(args.put_target_delta),
        },
        "call": {
            "min_dte": args.call_min_dte,
            "max_dte": args.call_max_dte,
            "target_delta": str(args.call_target_delta),
        },
        "entries": [
            {
                "entry_date": result.entry_date.isoformat(),
                "option_type": result.option_type.value,
                "start_date": result.start_date.isoformat(),
                "end_date": result.end_date.isoformat(),
                "target_delta": str(result.target_delta),
                "underlying_prices": result.result.underlying_prices,
                "option_chains": result.result.option_chains,
                "contracts_selected": result.result.contracts_selected,
                "option_quotes": result.result.option_quotes,
                "option_greeks": result.result.option_greeks,
            }
            for result in results
        ],
    }


if __name__ == "__main__":
    main()
