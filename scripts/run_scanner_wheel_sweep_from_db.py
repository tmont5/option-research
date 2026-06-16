#!/usr/bin/env python
"""Run parameter sweeps for the offline scanner wheel portfolio test."""
# ruff: noqa: E402,I001

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_scanner_wheel_portfolio_from_db as wheel  # noqa: E402
from options_quant.strategies.scanner_put import ScannerStylePutStrategyConfig, StockQualityTier


@dataclass(frozen=True)
class SweepScenario:
    name: str
    max_open_puts: int
    max_contracts_per_position: int
    target_capital_utilization: Decimal
    put_max_delta: Decimal
    put_min_monthly_yield: Decimal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--tier", choices=[tier.value for tier in StockQualityTier], default="A")
    parser.add_argument("--initial-cash", type=Decimal, default=Decimal("500000"))
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal("0.65"))
    parser.add_argument("--entry-weekday", type=int, default=4)
    parser.add_argument("--max-open-puts", type=_csv_ints, default=[8, 12])
    parser.add_argument("--max-contracts-per-position", type=_csv_ints, default=[1, 3, 5])
    parser.add_argument(
        "--target-capital-utilization",
        type=_csv_decimals,
        default=[Decimal("0.55"), Decimal("0.70"), Decimal("0.85")],
    )
    parser.add_argument("--put-max-delta", type=_csv_decimals, default=None)
    parser.add_argument("--put-min-monthly-yield", type=_csv_decimals, default=None)
    parser.add_argument("--max-scenarios", type=int, default=0)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--summary-path", type=Path, required=True)
    args = parser.parse_args()

    config = ScannerStylePutStrategyConfig()
    tier = StockQualityTier(args.tier)
    tier_rule = config.tier_rules[tier]
    symbols = config.symbols_for_tier(tier)
    put_max_deltas = args.put_max_delta or _default_delta_grid(tier_rule.max_delta)
    put_min_monthly_yields = args.put_min_monthly_yield or _default_yield_grid(
        tier_rule.min_put_monthly_yield
    )

    scenarios = _build_scenarios(
        max_open_puts=args.max_open_puts,
        max_contracts_per_position=args.max_contracts_per_position,
        target_capital_utilization=args.target_capital_utilization,
        put_max_delta=put_max_deltas,
        put_min_monthly_yield=put_min_monthly_yields,
    )
    if args.max_scenarios > 0:
        scenarios = scenarios[: args.max_scenarios]

    rows: list[dict[str, Any]] = []
    con = duckdb.connect(str(args.database_path), read_only=True)
    try:
        for idx, scenario in enumerate(scenarios, start=1):
            print(f"[{idx}/{len(scenarios)}] {scenario.name}", flush=True)
            result = wheel._run(
                con=con,
                symbols=symbols,
                start_date=args.start_date,
                end_date=args.end_date,
                entry_weekday=args.entry_weekday,
                initial_cash=args.initial_cash,
                commission=args.commission_per_contract,
                max_open_puts=scenario.max_open_puts,
                max_contracts_per_position=scenario.max_contracts_per_position,
                target_capital_utilization=scenario.target_capital_utilization,
                max_candidates_per_run=config.portfolio.top_n_to_publish,
                put_min_dte=config.put_entry.min_dte,
                put_max_dte=config.put_entry.max_dte,
                put_max_delta=scenario.put_max_delta,
                put_min_monthly_yield=scenario.put_min_monthly_yield,
                min_bid=config.put_entry.liquidity.min_bid,
                min_open_interest=config.put_entry.liquidity.min_open_interest,
                max_spread_pct=config.put_entry.liquidity.max_bid_ask_spread_pct,
                call_min_monthly_yield=config.covered_call.min_monthly_yield,
                call_min_strike_above_basis=config.covered_call.min_strike_above_breakeven_pct,
                call_max_strike_above_basis=config.covered_call.max_strike_above_breakeven_pct,
            )
            summary = result["summary"]
            assert isinstance(summary, dict)
            rows.append(_scenario_row(scenario, summary))
    finally:
        con.close()

    rows.sort(key=_rank_key, reverse=True)
    payload = {
        "database_path": str(args.database_path),
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "tier": tier.value,
        "initial_cash": float(args.initial_cash),
        "scenario_count": len(rows),
        "ranking": rows,
    }
    args.summary_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    args.report_path.write_text(_report(payload), encoding="utf-8")
    print(f"Report: {args.report_path}", flush=True)
    print(f"Summary JSON: {args.summary_path}", flush=True)
    if rows:
        best = rows[0]
        print(
            "Best scenario: "
            f"{best['scenario']} "
            f"return={best['return_pct']:.4f} "
            f"max_dd={best['max_drawdown']:.4f} "
            f"return_to_drawdown={best['return_to_drawdown']:.2f}",
            flush=True,
        )


def _build_scenarios(
    *,
    max_open_puts: list[int],
    max_contracts_per_position: list[int],
    target_capital_utilization: list[Decimal],
    put_max_delta: list[Decimal],
    put_min_monthly_yield: list[Decimal],
) -> list[SweepScenario]:
    scenarios: list[SweepScenario] = []
    for open_puts, contracts, utilization, delta, monthly_yield in itertools.product(
        max_open_puts,
        max_contracts_per_position,
        target_capital_utilization,
        put_max_delta,
        put_min_monthly_yield,
    ):
        name = (
            f"open{open_puts}_contracts{contracts}_util{_compact_decimal(utilization)}"
            f"_delta{_compact_decimal(delta)}_yield{_compact_decimal(monthly_yield)}"
        )
        scenarios.append(
            SweepScenario(
                name=name,
                max_open_puts=open_puts,
                max_contracts_per_position=contracts,
                target_capital_utilization=utilization,
                put_max_delta=delta,
                put_min_monthly_yield=monthly_yield,
            )
        )
    return scenarios


def _scenario_row(scenario: SweepScenario, summary: dict[str, Any]) -> dict[str, Any]:
    total_pnl = Decimal(str(summary["total_pnl"]))
    unrealized_share_pnl = Decimal(str(summary["unrealized_share_pnl"]))
    max_drawdown = Decimal(str(summary["max_drawdown"]))
    return_pct = Decimal(str(summary["return_pct"]))
    return_to_drawdown = return_pct / max_drawdown if max_drawdown > 0 else Decimal("0")
    average_capital_utilization = Decimal(str(summary["average_capital_utilization"]))
    deployment_factor = min(Decimal("1"), average_capital_utilization / Decimal("0.25"))
    return_factor = min(Decimal("1"), abs(return_pct) / Decimal("0.05"))
    unrealized_share_pnl_pct = unrealized_share_pnl / total_pnl if total_pnl != 0 else Decimal("0")
    pnl_by_symbol = summary.get("pnl_by_symbol") or {}
    top_symbol = None
    top_symbol_pnl = Decimal("0")
    if isinstance(pnl_by_symbol, dict) and pnl_by_symbol:
        top_symbol, raw_pnl = max(pnl_by_symbol.items(), key=lambda item: Decimal(str(item[1])))
        top_symbol_pnl = Decimal(str(raw_pnl))
    top_symbol_pnl_pct = top_symbol_pnl / total_pnl if total_pnl != 0 else Decimal("0")
    return {
        "scenario": scenario.name,
        "max_open_puts": scenario.max_open_puts,
        "max_contracts_per_position": scenario.max_contracts_per_position,
        "target_capital_utilization": float(scenario.target_capital_utilization),
        "put_max_delta": float(scenario.put_max_delta),
        "put_min_monthly_yield": float(scenario.put_min_monthly_yield),
        "final_equity": float(summary["final_equity"]),
        "total_pnl": float(total_pnl),
        "return_pct": float(return_pct),
        "max_drawdown": float(max_drawdown),
        "max_drawdown_amount": float(summary["max_drawdown_amount"]),
        "return_to_drawdown": float(return_to_drawdown),
        "deployment_adjusted_score": float(return_to_drawdown * deployment_factor * return_factor),
        "annualized_volatility": float(summary["annualized_volatility"]),
        "average_capital_utilization": float(average_capital_utilization),
        "max_capital_utilization": float(summary["max_capital_utilization"]),
        "closed_puts": int(summary["closed_puts"]),
        "closed_put_contracts": int(summary["closed_put_contracts"]),
        "closed_calls": int(summary["closed_calls"]),
        "assignments": int(summary["assignments"]),
        "assigned_contracts": int(summary["assigned_contracts"]),
        "open_share_lots": int(summary["open_share_lots"]),
        "open_shares": int(summary["open_shares"]),
        "realized_option_pnl": float(summary["realized_option_pnl"]),
        "unrealized_share_pnl": float(unrealized_share_pnl),
        "unrealized_share_pnl_pct": float(unrealized_share_pnl_pct),
        "top_symbol": top_symbol,
        "top_symbol_pnl": float(top_symbol_pnl),
        "top_symbol_pnl_pct": float(top_symbol_pnl_pct),
        "monthly_returns": summary["monthly_returns"],
        "put_exit_reasons": summary["put_exit_reasons"],
        "call_exit_reasons": summary["call_exit_reasons"],
    }


def _rank_key(row: dict[str, Any]) -> tuple[float, float, float]:
    max_util_penalty = max(0.0, float(row["max_capital_utilization"]) - 1.0)
    concentration_penalty = max(0.0, float(row["top_symbol_pnl_pct"]) - 0.60)
    unrealized_penalty = max(0.0, float(row["unrealized_share_pnl_pct"]) - 0.60)
    adjusted = (
        float(row["deployment_adjusted_score"])
        - max_util_penalty
        - concentration_penalty
        - unrealized_penalty
    )
    return (adjusted, float(row["return_pct"]), -float(row["max_drawdown"]))


def _report(payload: dict[str, Any]) -> str:
    rows = payload["ranking"]
    assert isinstance(rows, list)
    lines = [
        f"# Scanner Wheel Parameter Sweep - Tier {payload['tier']}",
        "",
        f"- Database: {payload['database_path']}",
        f"- Window: {payload['start_date']} to {payload['end_date']}",
        f"- Initial cash: USD {payload['initial_cash']:.2f}",
        f"- Scenarios: {payload['scenario_count']}",
        "",
        "## Ranking Notes",
        "",
        "- Ranked by deployment-adjusted return-to-drawdown, with soft penalties for >100% "
        "max gross exposure, high single-symbol dependence, and high unrealized-share dependence.",
        "- The deployment adjustment downranks scenarios with tiny absolute return or very "
        "low average utilization.",
        "- Return includes marked-to-market open shares at the final date.",
        "- Monthly returns are realized option returns only, matching the single-run wheel report.",
        "",
        "## Top Scenarios",
        "",
    ]
    for idx, row in enumerate(rows[:10], start=1):
        lines.extend(_scenario_lines(idx, row))
    lines.extend(["", "## Full Scenario Table", ""])
    for idx, row in enumerate(rows, start=1):
        lines.append(
            f"{idx}. {row['scenario']}: return {row['return_pct'] * 100:.2f}%, "
            f"max DD {row['max_drawdown'] * 100:.2f}%, "
            f"adj score {row['deployment_adjusted_score']:.2f}, "
            f"ret/DD {row['return_to_drawdown']:.2f}, "
            f"avg util {row['average_capital_utilization'] * 100:.1f}%, "
            f"max util {row['max_capital_utilization'] * 100:.1f}%, "
            f"assignments {row['assignments']}, "
            f"top symbol {row['top_symbol']} {row['top_symbol_pnl_pct'] * 100:.1f}%"
        )
    return "\n".join(lines) + "\n"


def _scenario_lines(idx: int, row: dict[str, Any]) -> list[str]:
    return [
        f"{idx}. {row['scenario']}",
        f"- Return: {row['return_pct'] * 100:.2f}% | "
        f"Max drawdown: {row['max_drawdown'] * 100:.2f}% | "
        f"Adj score: {row['deployment_adjusted_score']:.2f} | "
        f"Return/DD: {row['return_to_drawdown']:.2f}",
        f"- Avg/max utilization: {row['average_capital_utilization'] * 100:.1f}% / "
        f"{row['max_capital_utilization'] * 100:.1f}%",
        f"- Puts/contracts: {row['closed_puts']} / {row['closed_put_contracts']} | "
        f"Calls: {row['closed_calls']} | Assignments: {row['assignments']} "
        f"({row['assigned_contracts']} contracts)",
        f"- PnL: USD {row['total_pnl']:.2f} total, "
        f"USD {row['realized_option_pnl']:.2f} realized options, "
        f"USD {row['unrealized_share_pnl']:.2f} unrealized shares",
        f"- Dependence: top symbol {row['top_symbol']} = "
        f"{row['top_symbol_pnl_pct'] * 100:.1f}% of PnL; unrealized shares = "
        f"{row['unrealized_share_pnl_pct'] * 100:.1f}% of PnL",
        "",
    ]


def _csv_ints(value: str) -> list[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def _csv_decimals(value: str) -> list[Decimal]:
    return [Decimal(part.strip()) for part in value.split(",") if part.strip()]


def _default_delta_grid(tier_max_delta: Decimal) -> list[Decimal]:
    return sorted({Decimal("0.20"), Decimal("0.24"), tier_max_delta})


def _default_yield_grid(tier_min_monthly_yield: Decimal) -> list[Decimal]:
    return sorted({tier_min_monthly_yield, tier_min_monthly_yield + Decimal("0.01")})


def _compact_decimal(value: Decimal) -> str:
    return str(value.normalize()).replace(".", "p")


if __name__ == "__main__":
    main()
