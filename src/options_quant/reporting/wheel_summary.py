"""Comparison helpers for wheel validation reports."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

ONE = Decimal("1")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class WheelReportComparison:
    """Normalized metrics for one wheel validation report."""

    label: str
    path: Path
    symbol: str
    start_date: date
    end_date: date
    entry_dates: int
    option_trades: int
    event_count: int
    failed_entries: int
    skipped_entries: int
    final_equity: Decimal
    realized_pnl: Decimal
    total_return: Decimal
    annualized_return: Decimal
    max_drawdown: Decimal
    min_equity: Decimal
    ending_shares: int
    max_shares: int
    max_open_options: int
    event_counts: dict[str, int]


def load_wheel_report_comparison(
    path: Path | str,
    *,
    label: str | None = None,
    initial_cash: Decimal = Decimal("500000"),
) -> WheelReportComparison:
    """Load comparable metrics from a wheel validation markdown report."""
    report_path = Path(path)
    summary = extract_json_summary(report_path.read_text(encoding="utf-8"))
    return _comparison_from_summary(
        summary,
        path=report_path,
        label=label if label is not None else report_path.parent.name,
        initial_cash=initial_cash,
    )


def extract_json_summary(markdown: str) -> dict[str, Any]:
    """Extract the JSON summary object from a wheel validation markdown report."""
    marker = "## JSON Summary"
    marker_index = markdown.find(marker)
    if marker_index == -1:
        raise ValueError("report does not contain a JSON Summary section")
    json_start = markdown.find("{", marker_index)
    if json_start == -1:
        raise ValueError("report JSON Summary section does not contain an object")

    depth = 0
    in_string = False
    escaped = False
    for index in range(json_start, len(markdown)):
        character = markdown[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                parsed = json.loads(markdown[json_start : index + 1])
                if not isinstance(parsed, dict):
                    raise ValueError("report JSON Summary is not an object")
                return parsed
    raise ValueError("report JSON Summary object is incomplete")


def render_wheel_comparison_markdown(comparisons: list[WheelReportComparison]) -> str:
    """Render wheel report comparisons as a markdown table."""
    headers = [
        "Run",
        "Window",
        "Entries",
        "Trades",
        "Final Equity",
        "PnL",
        "Return",
        "Ann. Return",
        "Max DD",
        "Min Equity",
        "Max Shares",
        "Max Options",
        "Failed",
        "Skipped",
    ]
    alignments = ["---", "---", *["---:" for _ in headers[2:]]]
    rows = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(alignments) + " |",
    ]
    for comparison in comparisons:
        rows.append(
            "| "
            + " | ".join(
                [
                    comparison.label,
                    f"{comparison.start_date.isoformat()} to {comparison.end_date.isoformat()}",
                    str(comparison.entry_dates),
                    str(comparison.option_trades),
                    _format_currency(comparison.final_equity),
                    _format_currency(comparison.realized_pnl),
                    _format_percent(comparison.total_return),
                    _format_percent(comparison.annualized_return),
                    _format_percent(comparison.max_drawdown),
                    _format_currency(comparison.min_equity),
                    str(comparison.max_shares),
                    str(comparison.max_open_options),
                    str(comparison.failed_entries),
                    str(comparison.skipped_entries),
                ]
            )
            + " |"
        )
    rows.extend(["", "## Event Mix", ""])
    rows.extend(_render_event_mix(comparisons))
    return "\n".join(rows) + "\n"


def _comparison_from_summary(
    summary: dict[str, Any],
    *,
    path: Path,
    label: str,
    initial_cash: Decimal,
) -> WheelReportComparison:
    start_date = date.fromisoformat(_string_value(summary["start_date"]))
    end_date = date.fromisoformat(_string_value(summary["end_date"]))
    final_equity = Decimal(_string_value(summary["final_equity"]))
    realized_pnl = Decimal(_string_value(summary["realized_pnl"]))
    max_drawdown = Decimal(_string_value(summary["max_drawdown"]))
    min_equity = Decimal(_string_value(summary["min_equity"]))
    snapshots = _list_value(summary.get("snapshots", []))
    events = _list_value(summary.get("events", []))
    event_counts = Counter(
        _string_value(event["event_type"]) for event in events if isinstance(event, dict)
    )
    days = max((end_date - start_date).days, 1)
    total_return = final_equity / initial_cash - ONE
    annualized_return = Decimal(str((float(final_equity / initial_cash) ** (365 / days)) - 1))
    return WheelReportComparison(
        label=label,
        path=path,
        symbol=_string_value(summary["symbol"]),
        start_date=start_date,
        end_date=end_date,
        entry_dates=int(summary["entry_dates"]),
        option_trades=int(summary["option_trades"]),
        event_count=len(events),
        failed_entries=len(_list_value(summary.get("failed_entries", []))),
        skipped_entries=len(_list_value(summary.get("skipped_entries", []))),
        final_equity=final_equity,
        realized_pnl=realized_pnl,
        total_return=total_return,
        annualized_return=annualized_return,
        max_drawdown=max_drawdown,
        min_equity=min_equity,
        ending_shares=int(summary["share_quantity"]),
        max_shares=_max_snapshot_int(snapshots, "share_quantity"),
        max_open_options=_max_snapshot_int(snapshots, "open_options"),
        event_counts=dict(sorted(event_counts.items())),
    )


def _render_event_mix(comparisons: list[WheelReportComparison]) -> list[str]:
    event_types = sorted(
        {event_type for comparison in comparisons for event_type in comparison.event_counts}
    )
    rows = [
        "| Run | " + " | ".join(event_types) + " |",
        "| --- | " + " | ".join("---:" for _ in event_types) + " |",
    ]
    for comparison in comparisons:
        rows.append(
            "| "
            + " | ".join(
                [comparison.label]
                + [str(comparison.event_counts.get(event_type, 0)) for event_type in event_types]
            )
            + " |"
        )
    return rows


def _list_value(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("expected list value in report summary")
    return value


def _string_value(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected string value in report summary")
    return value


def _max_snapshot_int(snapshots: list[object], key: str) -> int:
    values = [
        int(snapshot[key])
        for snapshot in snapshots
        if isinstance(snapshot, dict) and isinstance(snapshot.get(key), int)
    ]
    return max(values, default=0)


def _format_percent(value: Decimal) -> str:
    quantized = (value * HUNDRED).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(quantized) + "%"


def _format_currency(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "$" + str(quantized)
