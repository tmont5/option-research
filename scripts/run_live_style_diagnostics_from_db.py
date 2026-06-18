#!/usr/bin/env python
"""Diagnose live-style wheel utilization constraints from DuckDB data."""
# ruff: noqa: E402,I001

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_scanner_wheel_portfolio_from_db as wheel
import run_live_style_wheel_from_db as live
from options_quant.strategies.scanner_put import ScannerStylePutStrategyConfig, StockQualityTier


@dataclass(frozen=True)
class Scenario:
    name: str
    min_dte: int = 30
    max_dte: int = 35
    put_max_delta: Decimal = Decimal("0.25")
    put_min_monthly_yield: Decimal = Decimal("0.025")
    max_total_positions: int = 10
    max_contracts_per_ticker: int = 3
    target_capital_utilization: Decimal = Decimal("0.75")
    max_capital_utilization: Decimal = Decimal("1.00")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--tier",
        choices=[tier.value for tier in StockQualityTier],
        action="append",
        help="Scanner tier to include. Repeat to scan multiple tiers together. Defaults to A.",
    )
    parser.add_argument("--initial-cash", type=Decimal, default=Decimal("500000"))
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal("0.65"))
    parser.add_argument("--max-candidates-per-day", type=int, default=50)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--summary-path", type=Path, required=True)
    args = parser.parse_args()

    config = ScannerStylePutStrategyConfig()
    tiers = _selected_tiers(args.tier)
    symbols = _symbols_for_tiers(config, tiers)
    con = duckdb.connect(str(args.database_path), read_only=True)
    try:
        payload = _run_diagnostics(
            con=con,
            database_path=args.database_path,
            tiers=tiers,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            initial_cash=args.initial_cash,
            commission=args.commission_per_contract,
            max_candidates_per_day=args.max_candidates_per_day,
            config=config,
        )
    finally:
        con.close()

    args.summary_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    args.report_path.write_text(_report(payload), encoding="utf-8")
    baseline = payload["scenarios"][0]
    best_util = max(payload["scenarios"], key=lambda item: item["average_capital_utilization"])
    print(f"Report: {args.report_path}", flush=True)
    print(f"Summary JSON: {args.summary_path}", flush=True)
    print(
        "Live-style diagnostics: "
        f"baseline_util={baseline['average_capital_utilization']:.4f} "
        f"best_util={best_util['name']}:{best_util['average_capital_utilization']:.4f}",
        flush=True,
    )


def _run_diagnostics(
    *,
    con: duckdb.DuckDBPyConnection,
    database_path: Path,
    tiers: tuple[StockQualityTier, ...],
    symbols: tuple[str, ...],
    start_date: date,
    end_date: date,
    initial_cash: Decimal,
    commission: Decimal,
    max_candidates_per_day: int,
    config: ScannerStylePutStrategyConfig,
) -> dict[str, Any]:
    scenarios = _scenarios()
    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        result = live.run_live_style(
            con=con,
            tiers=tiers,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            initial_cash=initial_cash,
            commission=commission,
            target_capital_utilization=scenario.target_capital_utilization,
            max_capital_utilization=scenario.max_capital_utilization,
            max_total_positions=scenario.max_total_positions,
            max_contracts_per_ticker=scenario.max_contracts_per_ticker,
            max_candidates_per_day=max_candidates_per_day,
            put_max_delta=scenario.put_max_delta,
            put_min_monthly_yield=scenario.put_min_monthly_yield,
            call_min_monthly_yield=config.covered_call.min_monthly_yield,
            min_dte=scenario.min_dte,
            max_dte=scenario.max_dte,
            winner_early_days=14,
            challenged_policy="assign",
            min_bid=config.put_entry.liquidity.min_bid,
            min_open_interest=config.put_entry.liquidity.min_open_interest,
            max_spread_pct=config.put_entry.liquidity.max_bid_ask_spread_pct,
            call_min_strike_above_basis=config.covered_call.min_strike_above_breakeven_pct,
            call_max_strike_above_basis=config.covered_call.max_strike_above_breakeven_pct,
        )
        summary = result["summary"]
        availability = _candidate_availability(
            con=con,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            min_dte=scenario.min_dte,
            max_dte=scenario.max_dte,
            put_max_delta=scenario.put_max_delta,
            put_min_monthly_yield=scenario.put_min_monthly_yield,
            min_bid=config.put_entry.liquidity.min_bid,
            min_open_interest=config.put_entry.liquidity.min_open_interest,
            max_spread_pct=config.put_entry.liquidity.max_bid_ask_spread_pct,
            max_candidates_per_day=max_candidates_per_day,
        )
        skipped_reasons = Counter(item["reason"] for item in result["skipped"])
        rows.append(
            {
                "name": scenario.name,
                "min_dte": scenario.min_dte,
                "max_dte": scenario.max_dte,
                "put_max_delta": float(scenario.put_max_delta),
                "put_min_monthly_yield": float(scenario.put_min_monthly_yield),
                "max_total_positions": scenario.max_total_positions,
                "max_contracts_per_ticker": scenario.max_contracts_per_ticker,
                "target_capital_utilization": float(scenario.target_capital_utilization),
                "max_capital_utilization": float(scenario.max_capital_utilization),
                "final_equity": summary["final_equity"],
                "total_pnl": summary["total_pnl"],
                "return_pct": summary["return_pct"],
                "realized_option_pnl": summary["realized_option_pnl"],
                "average_capital_utilization": summary["average_capital_utilization"],
                "max_observed_capital_utilization": summary["max_capital_utilization"],
                "max_drawdown": summary["max_drawdown"],
                "closed_put_contracts": summary["closed_put_contracts"],
                "closed_call_contracts": summary["closed_call_contracts"],
                "assignments": summary["assignments"],
                "skipped_count": summary["skipped_count"],
                "skipped_reasons": dict(sorted(skipped_reasons.items())),
                "candidate_availability": availability,
                "pnl_by_symbol": summary["pnl_by_symbol"],
            }
        )
    return {
        "database_path": str(database_path),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "tiers": [tier.value for tier in tiers],
        "symbols": list(symbols),
        "symbol_count": len(symbols),
        "max_candidates_per_day": max_candidates_per_day,
        "scenarios": rows,
    }


def _scenarios() -> list[Scenario]:
    return [
        Scenario(name="baseline"),
        Scenario(name="wider_dte_25_40", min_dte=25, max_dte=40),
        Scenario(name="lower_yield_2pct", put_min_monthly_yield=Decimal("0.020")),
        Scenario(name="higher_delta_028", put_max_delta=Decimal("0.28")),
        Scenario(name="more_positions_12", max_total_positions=12),
        Scenario(name="larger_ticker_size_5", max_contracts_per_ticker=5),
        Scenario(
            name="deployment_combo",
            min_dte=25,
            max_dte=40,
            put_max_delta=Decimal("0.28"),
            put_min_monthly_yield=Decimal("0.020"),
            max_total_positions=12,
            max_contracts_per_ticker=5,
        ),
    ]


def _candidate_availability(
    *,
    con: duckdb.DuckDBPyConnection,
    symbols: tuple[str, ...],
    start_date: date,
    end_date: date,
    min_dte: int,
    max_dte: int,
    put_max_delta: Decimal,
    put_min_monthly_yield: Decimal,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
    max_candidates_per_day: int,
) -> dict[str, Any]:
    observed_dates = live._observed_dates(con, start_date, end_date)
    candidate_counts: list[int] = []
    symbol_counts: list[int] = []
    top_days: list[dict[str, Any]] = []
    zero_candidate_dates: list[str] = []
    for observed in observed_dates:
        candidates = wheel._put_candidates(
            con=con,
            symbols=symbols,
            entry_date=observed,
            min_dte=min_dte,
            max_dte=max_dte,
            max_delta=put_max_delta,
            min_monthly_yield=put_min_monthly_yield,
            min_bid=min_bid,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
            max_candidates=max_candidates_per_day,
        )
        candidate_counts.append(len(candidates))
        symbol_counts.append(len({candidate.symbol for candidate in candidates}))
        if not candidates:
            zero_candidate_dates.append(observed.isoformat())
        if candidates:
            top = candidates[0]
            top_days.append(
                {
                    "date": observed.isoformat(),
                    "candidate_count": len(candidates),
                    "top_symbol": top.symbol,
                    "top_expiration": top.expiration.isoformat(),
                    "top_strike": float(top.strike),
                    "top_delta": float(top.delta),
                    "top_monthly_yield": float(top.monthly_yield),
                    "top_score": float(top.score),
                }
            )
    return {
        "observed_days": len(observed_dates),
        "days_with_candidates": sum(1 for count in candidate_counts if count > 0),
        "zero_candidate_days": len(zero_candidate_dates),
        "zero_candidate_dates_sample": zero_candidate_dates[:10],
        "average_candidates_per_day": _average(candidate_counts),
        "median_candidates_per_day": _median(candidate_counts),
        "max_candidates_seen": max(candidate_counts, default=0),
        "average_symbols_per_day": _average(symbol_counts),
        "median_symbols_per_day": _median(symbol_counts),
        "top_candidate_days_sample": top_days[:10],
    }


def _selected_tiers(values: list[str] | None) -> tuple[StockQualityTier, ...]:
    tiers = tuple(StockQualityTier(value) for value in (values or [StockQualityTier.A.value]))
    return tuple(dict.fromkeys(tiers))


def _symbols_for_tiers(
    config: ScannerStylePutStrategyConfig,
    tiers: tuple[StockQualityTier, ...],
) -> tuple[str, ...]:
    symbols = [symbol for tier in tiers for symbol in config.symbols_for_tier(tier)]
    return tuple(dict.fromkeys(symbols))


def _average(values: list[int]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    midpoint = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return float(sorted_values[midpoint])
    return (sorted_values[midpoint - 1] + sorted_values[midpoint]) / 2


def _report(payload: dict[str, Any]) -> str:
    tier_label = "+".join(payload["tiers"])
    lines = [
        f"# Live-Style Utilization Diagnostics - Tier {tier_label}",
        "",
        f"- Database: {payload['database_path']}",
        f"- Window: {payload['start_date']} to {payload['end_date']}",
        f"- Universe: {payload['symbol_count']} symbols",
        f"- Candidate cap per day: {payload['max_candidates_per_day']}",
        "",
        "## Scenario Summary",
        "",
    ]
    for scenario in payload["scenarios"]:
        availability = scenario["candidate_availability"]
        lines.append(
            f"- {scenario['name']}: return {scenario['return_pct'] * 100:.2f}%, "
            f"avg util {scenario['average_capital_utilization'] * 100:.2f}%, "
            f"max util {scenario['max_observed_capital_utilization'] * 100:.2f}%, "
            f"max DD {scenario['max_drawdown'] * 100:.2f}%, "
            f"put contracts {scenario['closed_put_contracts']}, "
            "eligible days "
            f"{availability['days_with_candidates']}/{availability['observed_days']}, "
            f"avg candidates/day {availability['average_candidates_per_day']:.1f}"
        )
    lines.extend(["", "## Skipped Reasons", ""])
    for scenario in payload["scenarios"]:
        reasons = scenario["skipped_reasons"] or {"none": 0}
        text = ", ".join(f"{reason}: {count}" for reason, count in reasons.items())
        lines.append(f"- {scenario['name']}: {text}")
    lines.extend(["", "## Candidate Availability", ""])
    for scenario in payload["scenarios"]:
        availability = scenario["candidate_availability"]
        lines.append(
            f"- {scenario['name']}: zero-candidate days {availability['zero_candidate_days']}, "
            f"median candidates/day {availability['median_candidates_per_day']:.1f}, "
            f"max candidates seen {availability['max_candidates_seen']}, "
            f"median symbols/day {availability['median_symbols_per_day']:.1f}"
        )
    lines.extend(["", "## Best PnL Symbols", ""])
    for scenario in payload["scenarios"]:
        pnl_by_symbol = scenario["pnl_by_symbol"]
        top = sorted(pnl_by_symbol.items(), key=lambda item: item[1], reverse=True)[:5]
        text = ", ".join(f"{symbol} {pnl:.0f}" for symbol, pnl in top)
        lines.append(f"- {scenario['name']}: {text}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
