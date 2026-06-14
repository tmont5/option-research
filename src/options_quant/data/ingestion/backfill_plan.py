"""Backfill planning helpers for long-range ThetaData research datasets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from options_quant.data.models import OptionType

DEFAULT_TABLES = (
    "underlying_prices",
    "option_chains",
    "option_quotes",
    "option_greeks",
    "option_open_interest",
)


class BackfillPlanConfig(BaseModel):
    """Configuration for a resumable research-data backfill manifest."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(default="SPY", min_length=1)
    start_date: date = Field(default=date(2018, 1, 1))
    end_date: date = Field(description="Inclusive final data date.")
    chunk_days: int = Field(default=30, gt=0)
    entry_spacing_days: int = Field(default=7, gt=0)
    entry_weekday: int = Field(default=4, ge=0, le=6, description="Monday=0, Friday=4.")
    min_dte: int = Field(default=30, ge=0)
    max_dte: int = Field(default=60, ge=0)
    option_type: OptionType = Field(default=OptionType.PUT)
    include_open_interest: bool = Field(default=True)
    include_raw_rows: bool = Field(default=False)
    manifest_path: Path = Field(default=Path("runs/backfill_manifest/manifest.json"))

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        """Validate date and option windows."""
        if self.start_date > self.end_date:
            raise ValueError("start_date must be less than or equal to end_date")
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must be less than or equal to max_dte")
        return self


@dataclass(frozen=True)
class BackfillDateChunk:
    """One contiguous date chunk for resumable ingestion."""

    index: int
    start_date: date
    end_date: date


@dataclass(frozen=True)
class BackfillTask:
    """One resumable unit of work in the backfill manifest."""

    task_id: str
    task_type: str
    symbol: str
    start_date: date
    end_date: date
    status: str = "pending"


@dataclass(frozen=True)
class BackfillPlanResult:
    """Complete backfill plan."""

    config: BackfillPlanConfig
    chunks: tuple[BackfillDateChunk, ...]
    entry_dates: tuple[date, ...]
    tasks: tuple[BackfillTask, ...]
    tables: tuple[str, ...]


def build_backfill_plan(config: BackfillPlanConfig) -> BackfillPlanResult:
    """Build a deterministic manifest for a long-range SPY research backfill."""
    chunks = tuple(_date_chunks(config))
    entry_dates = tuple(_entry_dates(config))
    task_types = ["underlying_eod", "option_chain", "option_quotes", "option_greeks"]
    if config.include_open_interest:
        task_types.append("option_open_interest")
    if config.include_raw_rows:
        task_types.append("raw_rows")
    tasks = tuple(
        BackfillTask(
            task_id=f"{config.symbol.lower()}-{chunk.index:04d}-{task_type}",
            task_type=task_type,
            symbol=config.symbol,
            start_date=chunk.start_date,
            end_date=chunk.end_date,
        )
        for chunk in chunks
        for task_type in task_types
    )
    return BackfillPlanResult(
        config=config,
        chunks=chunks,
        entry_dates=entry_dates,
        tasks=tasks,
        tables=_tables(config),
    )


def write_backfill_manifest(result: BackfillPlanResult) -> None:
    """Write JSON and Markdown backfill manifests."""
    result.config.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    result.config.manifest_path.write_text(
        json.dumps(_payload(result), indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path = result.config.manifest_path.with_suffix(".md")
    markdown_path.write_text(_markdown(result), encoding="utf-8")


def _date_chunks(config: BackfillPlanConfig) -> list[BackfillDateChunk]:
    chunks: list[BackfillDateChunk] = []
    current = config.start_date
    index = 1
    while current <= config.end_date:
        chunk_end = min(current + timedelta(days=config.chunk_days - 1), config.end_date)
        chunks.append(BackfillDateChunk(index=index, start_date=current, end_date=chunk_end))
        current = chunk_end + timedelta(days=1)
        index += 1
    return chunks


def _entry_dates(config: BackfillPlanConfig) -> list[date]:
    first = config.start_date
    while first.weekday() != config.entry_weekday:
        first += timedelta(days=1)
    dates: list[date] = []
    current = first
    while current <= config.end_date:
        dates.append(current)
        current += timedelta(days=config.entry_spacing_days)
    return dates


def _tables(config: BackfillPlanConfig) -> tuple[str, ...]:
    tables = list(DEFAULT_TABLES)
    if config.include_raw_rows:
        tables.append("raw_thetadata_rows")
    return tuple(tables)


def _payload(result: BackfillPlanResult) -> dict[str, object]:
    config = result.config
    return {
        "symbol": config.symbol,
        "start_date": config.start_date.isoformat(),
        "end_date": config.end_date.isoformat(),
        "chunk_days": config.chunk_days,
        "entry_spacing_days": config.entry_spacing_days,
        "entry_weekday": config.entry_weekday,
        "min_dte": config.min_dte,
        "max_dte": config.max_dte,
        "option_type": config.option_type.value,
        "include_open_interest": config.include_open_interest,
        "include_raw_rows": config.include_raw_rows,
        "tables": list(result.tables),
        "chunk_count": len(result.chunks),
        "entry_date_count": len(result.entry_dates),
        "task_count": len(result.tasks),
        "chunks": [
            {
                "index": chunk.index,
                "start_date": chunk.start_date.isoformat(),
                "end_date": chunk.end_date.isoformat(),
            }
            for chunk in result.chunks
        ],
        "entry_dates": [entry_date.isoformat() for entry_date in result.entry_dates],
        "tasks": [
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "symbol": task.symbol,
                "start_date": task.start_date.isoformat(),
                "end_date": task.end_date.isoformat(),
                "status": task.status,
            }
            for task in result.tasks
        ],
        "quality_checks": [
            "missing_market_days",
            "duplicate_rows",
            "zero_or_invalid_iv",
            "missing_underlying_price",
            "missing_entry_quote",
            "missing_exit_quote",
            "holiday_or_no_data_entry",
        ],
    }


def _markdown(result: BackfillPlanResult) -> str:
    config = result.config
    lines = [
        "# Backfill Manifest",
        "",
        "## Contract",
        "",
        f"- Symbol: {config.symbol}",
        f"- Date range: {config.start_date} to {config.end_date}",
        f"- Chunk size: {config.chunk_days} days",
        f"- Entry cadence: weekday {config.entry_weekday}, every {config.entry_spacing_days} days",
        f"- Option window: {config.min_dte}-{config.max_dte} DTE {config.option_type.value}",
        f"- Tables: {', '.join(result.tables)}",
        "",
        "## Counts",
        "",
        f"- Chunks: {len(result.chunks)}",
        f"- Entry dates: {len(result.entry_dates)}",
        f"- Tasks: {len(result.tasks)}",
        "",
        "## Resumability",
        "",
        "- Treat each task as idempotent.",
        "- Mark task status outside this generated manifest or regenerate and diff by task_id.",
        "- Ingestion should upsert or delete-and-reload each task window to avoid duplicate rows.",
        "",
        "## Quality Checks",
        "",
        "- Missing market days",
        "- Duplicate rows",
        "- Zero or invalid IV",
        "- Missing underlying price",
        "- Missing entry quote",
        "- Missing exit quote",
        "- Holiday or no-data entry dates",
        "",
    ]
    return "\n".join(lines) + "\n"
