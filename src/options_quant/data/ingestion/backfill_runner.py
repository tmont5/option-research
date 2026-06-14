"""Execute resumable ThetaData backfill chunks into DuckDB."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from options_quant.data.ingestion.backfill_plan import (
    BackfillDateChunk,
    BackfillPlanConfig,
    build_backfill_plan,
    write_backfill_manifest,
)
from options_quant.data.ingestion.thetadata_eod import (
    ThetaDataEODIngestionConfig,
    ThetaDataEODIngestionPipeline,
    ThetaDataEODIngestionResult,
    ThetaDataEODProvider,
)
from options_quant.data.models import OptionType
from options_quant.data.providers import ThetaDataProvider, ThetaDataPythonClient
from options_quant.data.storage import DuckDBStorage


class BackfillRunnerConfig(BaseModel):
    """Configuration for a bounded Step 6 backfill execution."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(default="SPY", min_length=1)
    start_date: date = Field(description="Inclusive first data date.")
    end_date: date = Field(description="Inclusive final data date.")
    chunk_days: int = Field(default=30, gt=0)
    min_dte: int = Field(default=30, ge=0)
    max_dte: int = Field(default=60, ge=0)
    option_type: OptionType = Field(default=OptionType.PUT)
    include_open_interest: bool = Field(default=True)
    max_chunks: int | None = Field(default=None, gt=0)
    max_contracts: int | None = Field(default=None, gt=0)
    database_path: Path = Field(default=Path("runs/backfill/market_data.duckdb"))
    manifest_path: Path = Field(default=Path("runs/backfill/manifest.json"))
    report_path: Path = Field(default=Path("runs/backfill/report.md"))
    reset_database: bool = Field(default=False)
    theta_mdds_host: str | None = Field(default=None, min_length=1)
    theta_mdds_port: str | None = Field(default=None, min_length=1)
    theta_mdds_type: str | None = Field(default=None, min_length=1)
    verbose: bool = Field(default=False)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        """Validate date and option windows."""
        if self.start_date > self.end_date:
            raise ValueError("start_date must be less than or equal to end_date")
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must be less than or equal to max_dte")
        return self


@dataclass(frozen=True)
class BackfillChunkResult:
    """One executed backfill chunk."""

    index: int
    start_date: date
    end_date: date
    underlying_prices: int
    option_chains: int
    contracts_selected: int
    option_quotes: int
    option_greeks: int


@dataclass(frozen=True)
class BackfillRunnerResult:
    """Summary of a bounded backfill execution."""

    config: BackfillRunnerConfig
    chunks_planned: int
    chunks_completed: int
    entry_dates_planned: int
    chunk_results: tuple[BackfillChunkResult, ...]

    @property
    def underlying_prices(self) -> int:
        """Total underlying rows inserted."""
        return sum(chunk.underlying_prices for chunk in self.chunk_results)

    @property
    def option_chains(self) -> int:
        """Total chain snapshots inserted."""
        return sum(chunk.option_chains for chunk in self.chunk_results)

    @property
    def contracts_selected(self) -> int:
        """Total selected contract occurrences across executed chunks."""
        return sum(chunk.contracts_selected for chunk in self.chunk_results)

    @property
    def option_quotes(self) -> int:
        """Total option quote rows inserted."""
        return sum(chunk.option_quotes for chunk in self.chunk_results)

    @property
    def option_greeks(self) -> int:
        """Total option Greek rows inserted."""
        return sum(chunk.option_greeks for chunk in self.chunk_results)


def run_backfill_runner(
    config: BackfillRunnerConfig,
    *,
    provider: ThetaDataEODProvider | None = None,
    storage: DuckDBStorage | None = None,
) -> BackfillRunnerResult:
    """Build the manifest and execute its date chunks into DuckDB."""
    plan = build_backfill_plan(
        BackfillPlanConfig(
            symbol=config.symbol,
            start_date=config.start_date,
            end_date=config.end_date,
            chunk_days=config.chunk_days,
            min_dte=config.min_dte,
            max_dte=config.max_dte,
            option_type=config.option_type,
            include_open_interest=config.include_open_interest,
            manifest_path=config.manifest_path,
        )
    )
    write_backfill_manifest(plan)

    if config.reset_database and config.database_path.exists():
        config.database_path.unlink()
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.report_path.parent.mkdir(parents=True, exist_ok=True)

    owns_storage = storage is None
    active_storage = storage or DuckDBStorage(config.database_path)
    active_provider = provider or _live_provider(config)
    pipeline = ThetaDataEODIngestionPipeline(active_provider, active_storage)
    chunks = _bounded_chunks(plan.chunks, config.max_chunks)
    chunk_results: list[BackfillChunkResult] = []
    try:
        for chunk in chunks:
            if config.verbose:
                print(
                    f"ingesting chunk {chunk.index}: {chunk.start_date} to {chunk.end_date}",
                    flush=True,
                )
            chunk_ingestion = pipeline.ingest(
                ThetaDataEODIngestionConfig(
                    symbol=config.symbol,
                    start_date=chunk.start_date,
                    end_date=chunk.end_date,
                    chain_as_of_date=chunk.start_date,
                    min_dte=config.min_dte,
                    max_dte=config.max_dte,
                    option_type=config.option_type,
                    max_contracts=config.max_contracts,
                )
            )
            chunk_results.append(_chunk_result(chunk, chunk_ingestion))
    finally:
        if owns_storage:
            active_storage.close()

    result = BackfillRunnerResult(
        config=config,
        chunks_planned=len(plan.chunks),
        chunks_completed=len(chunk_results),
        entry_dates_planned=len(plan.entry_dates),
        chunk_results=tuple(chunk_results),
    )
    _write_report(result)
    return result


def _bounded_chunks(
    chunks: tuple[BackfillDateChunk, ...],
    max_chunks: int | None,
) -> tuple[BackfillDateChunk, ...]:
    if max_chunks is None:
        return chunks
    return chunks[:max_chunks]


def _chunk_result(
    chunk: BackfillDateChunk,
    result: ThetaDataEODIngestionResult,
) -> BackfillChunkResult:
    return BackfillChunkResult(
        index=chunk.index,
        start_date=chunk.start_date,
        end_date=chunk.end_date,
        underlying_prices=result.underlying_prices,
        option_chains=result.option_chains,
        contracts_selected=result.contracts_selected,
        option_quotes=result.option_quotes,
        option_greeks=result.option_greeks,
    )


def _live_provider(config: BackfillRunnerConfig) -> ThetaDataProvider:
    client = ThetaDataPythonClient(
        mdds_host=config.theta_mdds_host,
        mdds_port=config.theta_mdds_port,
        mdds_type=config.theta_mdds_type,
    )
    return ThetaDataProvider(client)


def _write_report(result: BackfillRunnerResult) -> None:
    payload = {
        "symbol": result.config.symbol,
        "start_date": result.config.start_date.isoformat(),
        "end_date": result.config.end_date.isoformat(),
        "database_path": str(result.config.database_path),
        "manifest_path": str(result.config.manifest_path),
        "chunks_planned": result.chunks_planned,
        "chunks_completed": result.chunks_completed,
        "entry_dates_planned": result.entry_dates_planned,
        "underlying_prices": result.underlying_prices,
        "option_chains": result.option_chains,
        "contracts_selected": result.contracts_selected,
        "option_quotes": result.option_quotes,
        "option_greeks": result.option_greeks,
        "chunks": [
            {
                "index": chunk.index,
                "start_date": chunk.start_date.isoformat(),
                "end_date": chunk.end_date.isoformat(),
                "underlying_prices": chunk.underlying_prices,
                "option_chains": chunk.option_chains,
                "contracts_selected": chunk.contracts_selected,
                "option_quotes": chunk.option_quotes,
                "option_greeks": chunk.option_greeks,
            }
            for chunk in result.chunk_results
        ],
    }
    lines = [
        "# Backfill Run",
        "",
        "## JSON Summary",
        "",
        "~~~json",
        json.dumps(payload, indent=2),
        "~~~",
        "",
        "## Chunks",
        "",
    ]
    if not result.chunk_results:
        lines.append("- No chunks executed")
    for chunk in result.chunk_results:
        lines.append(
            "- "
            f"{chunk.index}: {chunk.start_date} to {chunk.end_date}; "
            f"underlying={chunk.underlying_prices} "
            f"chains={chunk.option_chains} "
            f"contracts={chunk.contracts_selected} "
            f"quotes={chunk.option_quotes} "
            f"greeks={chunk.option_greeks}"
        )
    result.config.report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
