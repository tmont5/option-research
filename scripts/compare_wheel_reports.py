#!/usr/bin/env python
"""Compare wheel validation reports."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from options_quant.reporting.wheel_summary import (
    load_wheel_report_comparison,
    render_wheel_comparison_markdown,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "reports",
        nargs="+",
        help="Report paths, optionally prefixed as label=path.",
    )
    parser.add_argument("--initial-cash", type=Decimal, default=Decimal("500000"))
    parser.add_argument("--output-path", type=Path)
    args = parser.parse_args()

    comparisons = [
        load_wheel_report_comparison(path, label=label, initial_cash=args.initial_cash)
        for label, path in (_parse_report_arg(report_arg) for report_arg in args.reports)
    ]
    output = render_wheel_comparison_markdown(comparisons)
    if args.output_path is None:
        print(output, end="")
        return
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(output, encoding="utf-8")
    print(f"wrote comparison to {args.output_path}", flush=True)


def _parse_report_arg(value: str) -> tuple[str | None, Path]:
    label, separator, path = value.partition("=")
    if separator == "":
        return None, Path(value)
    return label, Path(path)


if __name__ == "__main__":
    main()
