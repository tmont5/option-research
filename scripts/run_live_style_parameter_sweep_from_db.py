#!/usr/bin/env python
"""Run controlled live-style scanner wheel parameter sweeps from DuckDB data."""
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

import run_live_style_diagnostics_from_db as diagnostics
import run_live_style_wheel_from_db as live
from options_quant.strategies.scanner_put import ScannerStylePutStrategyConfig, StockQualityTier


@dataclass(frozen=True)
class SweepScenario:
    name: str
    description: str
    tiers: tuple[StockQualityTier, ...] = (StockQualityTier.A, StockQualityTier.B)
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
    parser.add_argument("--initial-cash", type=Decimal, default=Decimal("500000"))
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal("0.65"))
    parser.add_argument("--max-candidates-per-day", type=int, default=50)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--summary-path", type=Path, required=True)
    args = parser.parse_args()

    config = ScannerStylePutStrategyConfig()
    con = duckdb.connect(str(args.database_path), read_only=True)
    try:
        payload = _run_sweep(
            con=con,
            database_path=args.database_path,
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
    best = payload["ranked_scenarios"][0]
    baseline = next(row for row in payload["ranked_scenarios"] if row["name"] == "baseline")
    print(f"Report: {args.report_path}", flush=True)
    print(f"Summary JSON: {args.summary_path}", flush=True)
    print(
        "Live-style sweep: "
        f"baseline_return={baseline['return_pct']:.4f} "
        f"best={best['name']} "
        f"best_return={best['return_pct']:.4f} "
        f"best_max_dd={best['max_drawdown']:.4f}",
        flush=True,
    )


def _run_sweep(
    *,
    con: duckdb.DuckDBPyConnection,
    database_path: Path,
    start_date: date,
    end_date: date,
    initial_cash: Decimal,
    commission: Decimal,
    max_candidates_per_day: int,
    config: ScannerStylePutStrategyConfig,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for idx, scenario in enumerate(_scenarios(), start=1):
        print(f"[{idx}] {scenario.name}", flush=True)
        symbols = _symbols_for_tiers(config, scenario.tiers)
        result = live.run_live_style(
            con=con,
            tiers=scenario.tiers,
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
        availability = diagnostics._candidate_availability(
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
        rows.append(_scenario_row(scenario, symbols, result, availability))

    ranked_rows = sorted(rows, key=_rank_key, reverse=True)
    return {
        "database_path": str(database_path),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "initial_cash": float(initial_cash),
        "max_candidates_per_day": max_candidates_per_day,
        "scenario_count": len(ranked_rows),
        "ranking_method": (
            "risk-adjusted score = return / max drawdown, penalized for drawdown above "
            "8%, low utilization, high assignment rate, and single-symbol concentration"
        ),
        "ranked_scenarios": ranked_rows,
    }


def _scenarios() -> list[SweepScenario]:
    return [
        SweepScenario(name="baseline", description="Current Phase 1 reference settings."),
        SweepScenario(
            name="tier_a_only",
            description="Tier A ownership-quality names only.",
            tiers=(StockQualityTier.A,),
        ),
        SweepScenario(
            name="tier_b_only",
            description="Tier B quality names only.",
            tiers=(StockQualityTier.B,),
        ),
        SweepScenario(
            name="wider_dte_25_40",
            description="Wider put selection window while keeping current yield and delta.",
            min_dte=25,
            max_dte=40,
        ),
        SweepScenario(
            name="lower_yield_2pct",
            description="Lower put monthly yield floor to 2.0%.",
            put_min_monthly_yield=Decimal("0.020"),
        ),
        SweepScenario(
            name="lower_yield_1_5pct",
            description="Lower put monthly yield floor to 1.5%.",
            put_min_monthly_yield=Decimal("0.015"),
        ),
        SweepScenario(
            name="higher_delta_028",
            description="Allow puts up to 0.28 absolute delta.",
            put_max_delta=Decimal("0.28"),
        ),
        SweepScenario(
            name="higher_delta_030",
            description="Allow puts up to 0.30 absolute delta.",
            put_max_delta=Decimal("0.30"),
        ),
        SweepScenario(
            name="larger_ticker_size_5",
            description="Allow up to five contracts per ticker.",
            max_contracts_per_ticker=5,
        ),
        SweepScenario(
            name="deployment_combo",
            description="Wider DTE, 2.0% yield floor, 0.28 delta, and larger ticker sizing.",
            min_dte=25,
            max_dte=40,
            put_max_delta=Decimal("0.28"),
            put_min_monthly_yield=Decimal("0.020"),
            max_total_positions=12,
            max_contracts_per_ticker=5,
        ),
    ]


def _scenario_row(
    scenario: SweepScenario,
    symbols: tuple[str, ...],
    result: dict[str, Any],
    availability: dict[str, Any],
) -> dict[str, Any]:
    summary = result["summary"]
    skipped_reasons = Counter(item["reason"] for item in result["skipped"])
    return_pct = Decimal(str(summary["return_pct"]))
    max_drawdown = Decimal(str(summary["max_drawdown"]))
    avg_util = Decimal(str(summary["average_capital_utilization"]))
    assignments = Decimal(str(summary["assignments"]))
    closed_puts = Decimal(str(summary["closed_puts"]))
    pnl_by_symbol = summary["pnl_by_symbol"]
    top_symbol, top_symbol_pnl, top_symbol_pnl_pct = _top_symbol(
        pnl_by_symbol,
        Decimal(str(summary["total_pnl"])),
    )
    return {
        "name": scenario.name,
        "description": scenario.description,
        "tiers": [tier.value for tier in scenario.tiers],
        "symbol_count": len(symbols),
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
        "return_pct": float(return_pct),
        "realized_option_pnl": summary["realized_option_pnl"],
        "average_capital_utilization": float(avg_util),
        "max_observed_capital_utilization": summary["max_capital_utilization"],
        "max_drawdown": float(max_drawdown),
        "return_to_drawdown": float(return_pct / max_drawdown) if max_drawdown > 0 else 0.0,
        "risk_adjusted_score": float(
            _risk_adjusted_score(
                return_pct=return_pct,
                max_drawdown=max_drawdown,
                average_capital_utilization=avg_util,
                assignment_rate=assignments / closed_puts if closed_puts > 0 else Decimal("0"),
                top_symbol_pnl_pct=top_symbol_pnl_pct,
            )
        ),
        "closed_puts": summary["closed_puts"],
        "closed_put_contracts": summary["closed_put_contracts"],
        "closed_calls": summary["closed_calls"],
        "closed_call_contracts": summary["closed_call_contracts"],
        "assignments": summary["assignments"],
        "assigned_contracts": summary["assigned_contracts"],
        "skipped_count": summary["skipped_count"],
        "skipped_reasons": dict(sorted(skipped_reasons.items())),
        "candidate_availability": availability,
        "top_symbol": top_symbol,
        "top_symbol_pnl": float(top_symbol_pnl),
        "top_symbol_pnl_pct": float(top_symbol_pnl_pct),
        "pnl_by_symbol": pnl_by_symbol,
        "put_exit_reasons": summary["put_exit_reasons"],
        "call_exit_reasons": summary["call_exit_reasons"],
    }


def _risk_adjusted_score(
    *,
    return_pct: Decimal,
    max_drawdown: Decimal,
    average_capital_utilization: Decimal,
    assignment_rate: Decimal,
    top_symbol_pnl_pct: Decimal,
) -> Decimal:
    if max_drawdown <= 0:
        return Decimal("0")
    return_to_drawdown = return_pct / max_drawdown
    utilization_factor = min(Decimal("1"), average_capital_utilization / Decimal("0.25"))
    excess_drawdown_penalty = max(Decimal("0"), max_drawdown - Decimal("0.08")) * Decimal("10")
    assignment_penalty = max(Decimal("0"), assignment_rate - Decimal("0.10"))
    concentration_penalty = max(Decimal("0"), top_symbol_pnl_pct - Decimal("0.35"))
    return return_to_drawdown * utilization_factor - (
        excess_drawdown_penalty + assignment_penalty + concentration_penalty
    )


def _rank_key(row: dict[str, Any]) -> tuple[float, float, float]:
    return (
        float(row["risk_adjusted_score"]),
        float(row["return_pct"]),
        -float(row["max_drawdown"]),
    )


def _top_symbol(
    pnl_by_symbol: dict[str, float],
    total_pnl: Decimal,
) -> tuple[str | None, Decimal, Decimal]:
    if not pnl_by_symbol:
        return None, Decimal("0"), Decimal("0")
    symbol, raw_pnl = max(pnl_by_symbol.items(), key=lambda item: Decimal(str(item[1])))
    pnl = Decimal(str(raw_pnl))
    pct = pnl / total_pnl if total_pnl > 0 else Decimal("0")
    return symbol, pnl, pct


def _symbols_for_tiers(
    config: ScannerStylePutStrategyConfig,
    tiers: tuple[StockQualityTier, ...],
) -> tuple[str, ...]:
    symbols = [symbol for tier in tiers for symbol in config.symbols_for_tier(tier)]
    return tuple(dict.fromkeys(symbols))


def _report(payload: dict[str, Any]) -> str:
    rows = payload["ranked_scenarios"]
    baseline = next(row for row in rows if row["name"] == "baseline")
    best = rows[0]
    lines = [
        "# Live-Style Scanner Wheel Parameter Sweep",
        "",
        f"- Database: {payload['database_path']}",
        f"- Window: {payload['start_date']} to {payload['end_date']}",
        f"- Initial cash: USD {payload['initial_cash']:.2f}",
        f"- Candidate cap per day: {payload['max_candidates_per_day']}",
        f"- Scenarios: {payload['scenario_count']}",
        "",
        "## Ranking Method",
        "",
        "- Scenarios are ranked by return-to-drawdown, adjusted for deployment and penalized "
        "for drawdown above 8%, assignment rate above 10%, and heavy single-symbol dependence.",
        "- This intentionally favors useful risk tradeoffs over the highest raw return.",
        "",
        "## Phase 2 Takeaway",
        "",
        _takeaway(best, baseline),
        "",
        "## Ranked Scenarios",
        "",
    ]
    for idx, row in enumerate(rows, start=1):
        availability = row["candidate_availability"]
        lines.append(
            f"{idx}. {row['name']}: score {row['risk_adjusted_score']:.2f}, "
            f"return {row['return_pct'] * 100:.2f}%, "
            f"max DD {row['max_drawdown'] * 100:.2f}%, "
            f"avg util {row['average_capital_utilization'] * 100:.2f}%, "
            f"max util {row['max_observed_capital_utilization'] * 100:.2f}%, "
            f"puts/calls {row['closed_put_contracts']}/{row['closed_call_contracts']}, "
            f"assignments {row['assignments']}, "
            "eligible days "
            f"{availability['days_with_candidates']}/{availability['observed_days']}, "
            f"avg candidates/day {availability['average_candidates_per_day']:.1f}"
        )
    lines.extend(["", "## Scenario Details", ""])
    for row in rows:
        lines.extend(_scenario_detail(row))
    return "\n".join(lines) + "\n"


def _takeaway(best: dict[str, Any], baseline: dict[str, Any]) -> str:
    if best["name"] == baseline["name"]:
        return (
            "- The baseline remains the best risk-adjusted scenario in this sweep. Looser "
            "variants should be treated as research candidates only if their added drawdown "
            "is acceptable."
        )
    return (
        f"- Best risk-adjusted scenario: {best['name']} "
        f"({best['return_pct'] * 100:.2f}% return, {best['max_drawdown'] * 100:.2f}% max DD) "
        f"vs baseline ({baseline['return_pct'] * 100:.2f}% return, "
        f"{baseline['max_drawdown'] * 100:.2f}% max DD)."
    )


def _scenario_detail(row: dict[str, Any]) -> list[str]:
    availability = row["candidate_availability"]
    reasons = row["skipped_reasons"] or {"none": 0}
    reason_text = ", ".join(f"{reason}: {count}" for reason, count in reasons.items())
    top_symbols = sorted(row["pnl_by_symbol"].items(), key=lambda item: item[1], reverse=True)[:5]
    symbol_text = ", ".join(f"{symbol} {pnl:.0f}" for symbol, pnl in top_symbols) or "none"
    return [
        f"### {row['name']}",
        "",
        f"- Description: {row['description']}",
        f"- Tiers: {'+'.join(row['tiers'])}; symbols: {row['symbol_count']}",
        f"- Parameters: DTE {row['min_dte']}-{row['max_dte']}, "
        f"max delta {row['put_max_delta']:.2f}, "
        f"min monthly yield {row['put_min_monthly_yield'] * 100:.1f}%, "
        f"max contracts/ticker {row['max_contracts_per_ticker']}",
        f"- Results: return {row['return_pct'] * 100:.2f}%, "
        f"max DD {row['max_drawdown'] * 100:.2f}%, "
        f"avg util {row['average_capital_utilization'] * 100:.2f}%, "
        f"final equity USD {row['final_equity']:.2f}",
        f"- Activity: put contracts {row['closed_put_contracts']}, "
        f"call contracts {row['closed_call_contracts']}, assignments {row['assignments']}, "
        f"skips {row['skipped_count']}",
        f"- Candidate availability: {availability['days_with_candidates']}/"
        f"{availability['observed_days']} eligible days, "
        f"{availability['zero_candidate_days']} zero-candidate days, "
        f"{availability['average_candidates_per_day']:.1f} avg candidates/day",
        f"- Skipped reasons: {reason_text}",
        f"- Best PnL symbols: {symbol_text}",
        "",
    ]


if __name__ == "__main__":
    main()
