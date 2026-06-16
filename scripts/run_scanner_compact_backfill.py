#!/usr/bin/env python
"""Run a resumable scanner-universe compact ThetaData backfill into DuckDB."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from options_quant.data.ingestion.thetadata_eod import (
    ThetaDataEODIngestionConfig,
    ThetaDataEODIngestionPipeline,
    ThetaDataEODIngestionResult,
)
from options_quant.data.models import OptionType
from options_quant.data.providers import ThetaDataProvider, ThetaDataPythonClient
from options_quant.data.storage import DuckDBStorage
from options_quant.strategies.scanner_put import ScannerStylePutStrategyConfig, StockQualityTier


@dataclass(frozen=True)
class ScannerBackfillTask:
    symbol: str
    entry_date: date
    option_type: OptionType
    start_date: date
    end_date: date
    min_dte: int
    max_dte: int
    target_delta: Decimal

    @property
    def task_id(self) -> str:
        return f"{self.symbol}-{self.entry_date.isoformat()}-{self.option_type.value}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--tier",
        choices=[tier.value for tier in StockQualityTier],
        action="append",
    )
    parser.add_argument("--symbols", help="Comma-separated explicit symbol list.")
    parser.add_argument("--spacing-days", type=int, default=7)
    parser.add_argument("--entry-weekday", type=int, default=4, help="Monday=0, Friday=4.")
    parser.add_argument("--put-min-dte", type=int, default=20)
    parser.add_argument("--put-max-dte", type=int, default=35)
    parser.add_argument("--put-target-delta", type=Decimal, default=Decimal("-0.25"))
    parser.add_argument("--call-min-dte", type=int, default=20)
    parser.add_argument("--call-max-dte", type=int, default=35)
    parser.add_argument("--call-target-delta", type=Decimal, default=Decimal("0.25"))
    parser.add_argument("--contracts-around-target", type=int, default=7)
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--reset-database", action="store_true")
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--max-tasks", type=int, help="Limit tasks for smoke tests.")
    parser.add_argument("--theta-mdds-host")
    parser.add_argument("--theta-mdds-port")
    parser.add_argument("--theta-mdds-type")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.reset_database and args.database_path.exists():
        args.database_path.unlink()
    if args.reset_state and args.state_path.exists():
        args.state_path.unlink()
    args.database_path.parent.mkdir(parents=True, exist_ok=True)
    args.state_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    symbols = _selected_symbols(args)
    tasks = _build_tasks(args, symbols)
    if args.max_tasks is not None:
        tasks = tasks[: args.max_tasks]
    state = _load_state(args.state_path)

    provider = ThetaDataProvider(
        ThetaDataPythonClient(
            mdds_host=args.theta_mdds_host,
            mdds_port=args.theta_mdds_port,
            mdds_type=args.theta_mdds_type,
        )
    )
    storage = DuckDBStorage(args.database_path)
    pipeline = ThetaDataEODIngestionPipeline(provider, storage)
    try:
        for index, task in enumerate(tasks, start=1):
            current = state["tasks"].get(task.task_id, {})
            if current.get("status") == "completed":
                if args.verbose:
                    print(f"[{index}/{len(tasks)}] skip completed {task.task_id}", flush=True)
                continue
            if current.get("status") == "failed" and not args.retry_failures:
                if args.verbose:
                    print(f"[{index}/{len(tasks)}] skip failed {task.task_id}", flush=True)
                continue

            print(f"[{index}/{len(tasks)}] ingest {task.task_id}", flush=True)
            _mark_task(args.state_path, state, task, status="running")
            try:
                result = _ingest_task(
                    pipeline,
                    task,
                    contracts_around_target=args.contracts_around_target,
                )
            except Exception as error:
                _mark_task(
                    args.state_path,
                    state,
                    task,
                    status="failed",
                    error=str(error),
                )
                print(f"failed {task.task_id}: {error}", flush=True)
                continue
            _mark_task(args.state_path, state, task, status="completed", result=result)
    finally:
        storage.close()

    args.report_path.write_text(
        json.dumps(_report_payload(args, symbols, tasks, state), indent=2) + "\n",
        encoding="utf-8",
    )
    counts = _state_counts(state)
    print(f"Database: {args.database_path}", flush=True)
    print(f"State: {args.state_path}", flush=True)
    print(f"Report: {args.report_path}", flush=True)
    print(
        "Scanner compact backfill: "
        f"symbols={len(symbols)} tasks={len(tasks)} "
        f"completed={counts['completed']} failed={counts['failed']} "
        f"running={counts['running']}",
        flush=True,
    )


def _selected_symbols(args: argparse.Namespace) -> tuple[str, ...]:
    if args.symbols:
        return tuple(symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip())
    config = ScannerStylePutStrategyConfig()
    tiers = tuple(StockQualityTier(tier) for tier in args.tier or [StockQualityTier.A.value])
    return tuple(symbol for tier in tiers for symbol in config.symbols_for_tier(tier))


def _build_tasks(args: argparse.Namespace, symbols: tuple[str, ...]) -> list[ScannerBackfillTask]:
    tasks: list[ScannerBackfillTask] = []
    entry_dates = _entry_dates(
        args.start_date,
        args.end_date,
        spacing_days=args.spacing_days,
        entry_weekday=args.entry_weekday,
    )
    for symbol in symbols:
        for entry_date in entry_dates:
            tasks.append(
                ScannerBackfillTask(
                    symbol=symbol,
                    entry_date=entry_date,
                    option_type=OptionType.PUT,
                    start_date=entry_date,
                    end_date=entry_date + timedelta(days=args.put_max_dte),
                    min_dte=args.put_min_dte,
                    max_dte=args.put_max_dte,
                    target_delta=args.put_target_delta,
                )
            )
            tasks.append(
                ScannerBackfillTask(
                    symbol=symbol,
                    entry_date=entry_date,
                    option_type=OptionType.CALL,
                    start_date=entry_date,
                    end_date=entry_date + timedelta(days=args.call_max_dte),
                    min_dte=args.call_min_dte,
                    max_dte=args.call_max_dte,
                    target_delta=args.call_target_delta,
                )
            )
    return tasks


def _ingest_task(
    pipeline: ThetaDataEODIngestionPipeline,
    task: ScannerBackfillTask,
    *,
    contracts_around_target: int,
) -> ThetaDataEODIngestionResult:
    return pipeline.ingest(
        ThetaDataEODIngestionConfig(
            symbol=task.symbol,
            start_date=task.start_date,
            end_date=task.end_date,
            chain_as_of_date=task.entry_date,
            min_dte=task.min_dte,
            max_dte=task.max_dte,
            option_type=task.option_type,
            target_delta=task.target_delta,
            contracts_around_target=contracts_around_target,
        )
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


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"tasks": {}}
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _mark_task(
    path: Path,
    state: dict[str, Any],
    task: ScannerBackfillTask,
    *,
    status: str,
    result: ThetaDataEODIngestionResult | None = None,
    error: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "symbol": task.symbol,
        "entry_date": task.entry_date.isoformat(),
        "option_type": task.option_type.value,
        "start_date": task.start_date.isoformat(),
        "end_date": task.end_date.isoformat(),
        "min_dte": task.min_dte,
        "max_dte": task.max_dte,
        "target_delta": str(task.target_delta),
        "status": status,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if result is not None:
        payload["result"] = {
            "underlying_prices": result.underlying_prices,
            "option_chains": result.option_chains,
            "contracts_selected": result.contracts_selected,
            "option_quotes": result.option_quotes,
            "option_greeks": result.option_greeks,
        }
    if error is not None:
        payload["error"] = error
    state["tasks"][task.task_id] = payload
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _state_counts(state: dict[str, Any]) -> dict[str, int]:
    counts = {"completed": 0, "failed": 0, "running": 0}
    for task in state["tasks"].values():
        status = task.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _report_payload(
    args: argparse.Namespace,
    symbols: tuple[str, ...],
    tasks: list[ScannerBackfillTask],
    state: dict[str, Any],
) -> dict[str, Any]:
    counts = _state_counts(state)
    return {
        "symbols": list(symbols),
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "spacing_days": args.spacing_days,
        "entry_weekday": args.entry_weekday,
        "database_path": str(args.database_path),
        "state_path": str(args.state_path),
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
        "task_count": len(tasks),
        "completed": counts["completed"],
        "failed": counts["failed"],
        "running": counts["running"],
        "failed_tasks": [
            task for task in state["tasks"].values() if task.get("status") == "failed"
        ],
    }


if __name__ == "__main__":
    main()
