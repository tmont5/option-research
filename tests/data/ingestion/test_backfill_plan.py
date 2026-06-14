from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from options_quant.data.ingestion.backfill_plan import (
    BackfillPlanConfig,
    build_backfill_plan,
    write_backfill_manifest,
)


def test_backfill_plan_builds_resumable_tasks_and_weekly_entries(tmp_path: Path) -> None:
    result = build_backfill_plan(
        BackfillPlanConfig(
            symbol="SPY",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 31),
            chunk_days=10,
            manifest_path=tmp_path / "manifest.json",
        )
    )

    assert [(chunk.start_date, chunk.end_date) for chunk in result.chunks] == [
        (date(2025, 1, 1), date(2025, 1, 10)),
        (date(2025, 1, 11), date(2025, 1, 20)),
        (date(2025, 1, 21), date(2025, 1, 30)),
        (date(2025, 1, 31), date(2025, 1, 31)),
    ]
    assert result.entry_dates == (
        date(2025, 1, 3),
        date(2025, 1, 10),
        date(2025, 1, 17),
        date(2025, 1, 24),
        date(2025, 1, 31),
    )
    assert len(result.tasks) == 20
    assert result.tasks[0].task_id == "spy-0001-underlying_eod"
    assert result.tasks[-1].task_id == "spy-0004-option_open_interest"
    assert "option_open_interest" in result.tables


def test_backfill_manifest_writes_json_and_markdown(tmp_path: Path) -> None:
    result = build_backfill_plan(
        BackfillPlanConfig(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 3),
            manifest_path=tmp_path / "manifest.json",
        )
    )

    write_backfill_manifest(result)

    payload = json.loads((tmp_path / "manifest.json").read_text())
    assert payload["symbol"] == "SPY"
    assert payload["task_count"] == 5
    assert payload["quality_checks"] == [
        "missing_market_days",
        "duplicate_rows",
        "zero_or_invalid_iv",
        "missing_underlying_price",
        "missing_entry_quote",
        "missing_exit_quote",
        "holiday_or_no_data_entry",
    ]
    markdown = (tmp_path / "manifest.md").read_text()
    assert "Backfill Manifest" in markdown
    assert "Ingestion should upsert or delete-and-reload" in markdown


def test_backfill_plan_can_include_raw_rows_and_exclude_open_interest() -> None:
    result = build_backfill_plan(
        BackfillPlanConfig(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 1),
            include_raw_rows=True,
            include_open_interest=False,
        )
    )

    assert [task.task_type for task in result.tasks] == [
        "underlying_eod",
        "option_chain",
        "option_quotes",
        "option_greeks",
        "raw_rows",
    ]
    assert "raw_thetadata_rows" in result.tables
    assert "option_open_interest" in result.tables


def test_backfill_plan_config_validates_ranges() -> None:
    with pytest.raises(ValidationError, match="start_date must be less than or equal to end_date"):
        BackfillPlanConfig(start_date=date(2025, 1, 2), end_date=date(2025, 1, 1))

    with pytest.raises(ValidationError, match="min_dte must be less than or equal to max_dte"):
        BackfillPlanConfig(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 2),
            min_dte=60,
            max_dte=30,
        )
