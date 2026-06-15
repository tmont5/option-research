from __future__ import annotations

from decimal import Decimal

from options_quant.reporting.wheel_summary import (
    extract_json_summary,
    load_wheel_report_comparison,
    render_wheel_comparison_markdown,
)


def test_extract_json_summary_handles_nested_sections() -> None:
    markdown = """# Wheel Validation

## JSON Summary

{
  "symbol": "SPY",
  "events": [
    {
      "event_type": "put_expired_otm",
      "description": "short put expired out of the money"
    }
  ]
}

## Events
"""

    summary = extract_json_summary(markdown)

    assert summary["symbol"] == "SPY"
    assert summary["events"][0]["event_type"] == "put_expired_otm"


def test_load_wheel_report_comparison_calculates_metrics(tmp_path) -> None:
    report = tmp_path / "report.md"
    report.write_text(
        """# Wheel Validation

## JSON Summary

{
  "symbol": "SPY",
  "start_date": "2025-01-01",
  "end_date": "2025-12-31",
  "entry_dates": 52,
  "option_trades": 2,
  "events": [
    {
      "event_type": "put_expired_otm"
    },
    {
      "event_type": "shares_called_away"
    }
  ],
  "failed_entries": [],
  "skipped_entries": [
    {
      "entry_date": "2025-01-08",
      "reason": "covered call active"
    }
  ],
  "cash_balance": "525000.00",
  "realized_pnl": "25000.00",
  "share_quantity": 0,
  "share_cost_basis": null,
  "final_equity": "525000.00",
  "max_drawdown": "-0.0500",
  "min_equity": "475000.00",
  "snapshots": [
    {
      "share_quantity": 0,
      "open_options": 1
    },
    {
      "share_quantity": 200,
      "open_options": 3
    }
  ]
}

## Events
""",
        encoding="utf-8",
    )

    comparison = load_wheel_report_comparison(
        report,
        label="sample",
        initial_cash=Decimal("500000"),
    )

    assert comparison.label == "sample"
    assert comparison.total_return == Decimal("0.05")
    assert comparison.max_shares == 200
    assert comparison.max_open_options == 3
    assert comparison.event_counts == {"put_expired_otm": 1, "shares_called_away": 1}


def test_render_wheel_comparison_markdown_includes_scorecard(tmp_path) -> None:
    report = tmp_path / "report.md"
    report.write_text(
        """# Wheel Validation

## JSON Summary

{
  "symbol": "SPY",
  "start_date": "2025-01-01",
  "end_date": "2025-12-31",
  "entry_dates": 52,
  "option_trades": 1,
  "events": [],
  "failed_entries": [],
  "skipped_entries": [],
  "cash_balance": "510000.00",
  "realized_pnl": "10000.00",
  "share_quantity": 0,
  "share_cost_basis": null,
  "final_equity": "510000.00",
  "max_drawdown": "-0.0100",
  "min_equity": "495000.00",
  "snapshots": []
}
""",
        encoding="utf-8",
    )
    comparison = load_wheel_report_comparison(report, label="one-year")

    markdown = render_wheel_comparison_markdown([comparison])

    assert "| Run | Window | Entries | Trades |" in markdown
    assert "| one-year | 2025-01-01 to 2025-12-31 | 52 | 1 |" in markdown
    assert "$510000.00" in markdown
