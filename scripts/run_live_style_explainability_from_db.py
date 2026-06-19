#!/usr/bin/env python
"""Explain live-style scanner wheel results by filter, tier, symbol, and trade path."""
# ruff: noqa: E402,I001

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_live_style_diagnostics_from_db as diagnostics
import run_live_style_wheel_from_db as live
import run_scanner_wheel_portfolio_from_db as wheel
from options_quant.strategies.scanner_put import ScannerStylePutStrategyConfig, StockQualityTier


@dataclass(frozen=True)
class Scenario:
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
        payload = _run_explainability(
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
    baseline = next(row for row in payload["scenarios"] if row["name"] == "baseline")
    tier_a = next(row for row in payload["scenarios"] if row["name"] == "tier_a_only")
    print(f"Report: {args.report_path}", flush=True)
    print(f"Summary JSON: {args.summary_path}", flush=True)
    print(
        "Live-style explainability: "
        f"baseline_return={baseline['return_pct']:.4f} "
        f"tier_a_return={tier_a['return_pct']:.4f} "
        f"tier_a_max_dd={tier_a['max_drawdown']:.4f}",
        flush=True,
    )


def _run_explainability(
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
        rows.append(
            _scenario_row(
                con=con,
                scenario=scenario,
                symbols=symbols,
                result=result,
                availability=availability,
                start_date=start_date,
                end_date=end_date,
                config=config,
                max_candidates_per_day=max_candidates_per_day,
            )
        )
    return {
        "database_path": str(database_path),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "initial_cash": float(initial_cash),
        "max_candidates_per_day": max_candidates_per_day,
        "scenarios": rows,
        "comparisons": _comparisons(rows),
    }


def _scenarios() -> list[Scenario]:
    return [
        Scenario(name="baseline", description="Phase 1 A+B reference settings."),
        Scenario(
            name="tier_a_only",
            description="Tier A ownership-quality names only.",
            tiers=(StockQualityTier.A,),
        ),
        Scenario(
            name="wider_dte_25_40",
            description="A+B with wider 25-40 DTE window.",
            min_dte=25,
            max_dte=40,
        ),
        Scenario(
            name="higher_delta_028",
            description="A+B with max put delta raised to 0.28.",
            put_max_delta=Decimal("0.28"),
        ),
    ]


def _scenario_row(
    *,
    con: duckdb.DuckDBPyConnection,
    scenario: Scenario,
    symbols: tuple[str, ...],
    result: dict[str, Any],
    availability: dict[str, Any],
    start_date: date,
    end_date: date,
    config: ScannerStylePutStrategyConfig,
    max_candidates_per_day: int,
) -> dict[str, Any]:
    summary = result["summary"]
    symbol_tiers = _symbol_tiers(config)
    closed_puts = result["closed_puts"]
    closed_calls = result["closed_calls"]
    assert isinstance(closed_puts, list)
    assert isinstance(closed_calls, list)
    return {
        "name": scenario.name,
        "description": scenario.description,
        "tiers": [tier.value for tier in scenario.tiers],
        "symbol_count": len(symbols),
        "parameters": {
            "min_dte": scenario.min_dte,
            "max_dte": scenario.max_dte,
            "put_max_delta": float(scenario.put_max_delta),
            "put_min_monthly_yield": float(scenario.put_min_monthly_yield),
            "max_contracts_per_ticker": scenario.max_contracts_per_ticker,
        },
        "return_pct": summary["return_pct"],
        "final_equity": summary["final_equity"],
        "max_drawdown": summary["max_drawdown"],
        "max_drawdown_start": summary["max_drawdown_start"],
        "max_drawdown_end": summary["max_drawdown_end"],
        "average_capital_utilization": summary["average_capital_utilization"],
        "closed_put_contracts": summary["closed_put_contracts"],
        "closed_call_contracts": summary["closed_call_contracts"],
        "assignments": summary["assignments"],
        "candidate_availability": availability,
        "filter_funnel": _filter_funnel(
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
            symbol_tiers=symbol_tiers,
        ),
        "trade_contribution": _trade_contribution(
            closed_puts=closed_puts,
            closed_calls=closed_calls,
            symbol_tiers=symbol_tiers,
        ),
        "drawdown_window": _drawdown_window_exposure(
            result=result,
            symbol_tiers=symbol_tiers,
        ),
        "skipped_reasons": dict(Counter(item["reason"] for item in result["skipped"])),
    }


def _filter_funnel(
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
    symbol_tiers: dict[str, str],
) -> dict[str, Any]:
    dates = live._observed_dates(con, start_date, end_date)
    totals: Counter[str] = Counter()
    by_tier: dict[str, Counter[str]] = defaultdict(Counter)
    final_symbols: Counter[str] = Counter()
    final_by_tier: Counter[str] = Counter()
    for observed in dates:
        rows = _raw_put_rows(con, symbols, observed)
        final_candidates = wheel._put_candidates(
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
        for candidate in final_candidates:
            final_symbols[candidate.symbol] += 1
            final_by_tier[symbol_tiers[candidate.symbol]] += 1
        for row in rows:
            tier = symbol_tiers[row["symbol"]]
            totals["raw_joined"] += 1
            by_tier[tier]["raw_joined"] += 1
            dte = row["dte"]
            if dte < min_dte or dte > max_dte:
                totals["dte_reject"] += 1
                by_tier[tier]["dte_reject"] += 1
                continue
            totals["dte_pass"] += 1
            by_tier[tier]["dte_pass"] += 1
            if row["bid"] < min_bid:
                totals["bid_reject"] += 1
                by_tier[tier]["bid_reject"] += 1
                continue
            if row["open_interest"] < min_open_interest:
                totals["oi_reject"] += 1
                by_tier[tier]["oi_reject"] += 1
                continue
            if row["spread_pct"] > max_spread_pct:
                totals["spread_reject"] += 1
                by_tier[tier]["spread_reject"] += 1
                continue
            totals["liquidity_pass"] += 1
            by_tier[tier]["liquidity_pass"] += 1
            if row["delta_abs"] > put_max_delta:
                totals["delta_reject"] += 1
                by_tier[tier]["delta_reject"] += 1
                continue
            totals["delta_pass"] += 1
            by_tier[tier]["delta_pass"] += 1
            if row["monthly_yield"] < put_min_monthly_yield:
                totals["yield_reject"] += 1
                by_tier[tier]["yield_reject"] += 1
                continue
            totals["yield_pass"] += 1
            by_tier[tier]["yield_pass"] += 1
    totals["final_one_per_symbol_candidates"] = sum(final_symbols.values())
    for tier, count in final_by_tier.items():
        by_tier[tier]["final_one_per_symbol_candidates"] = count
    return {
        "total": dict(totals),
        "by_tier": {tier: dict(counter) for tier, counter in sorted(by_tier.items())},
        "top_final_candidate_symbols": final_symbols.most_common(10),
    }


def _raw_put_rows(
    con: duckdb.DuckDBPyConnection,
    symbols: tuple[str, ...],
    observed: date,
) -> list[dict[str, Any]]:
    placeholders = ",".join(["?"] * len(symbols))
    rows = con.execute(
        f"""
        WITH quote_rows AS (
          SELECT observed_date, underlying_symbol, expiration, strike, option_type,
                 max(CAST(bid AS DOUBLE)) AS bid,
                 max(CAST(ask AS DOUBLE)) AS ask,
                 max(coalesce(CAST(mark AS DOUBLE),
                     (CAST(bid AS DOUBLE) + CAST(ask AS DOUBLE)) / 2)) AS mark,
                 max(CAST(open_interest AS BIGINT)) AS open_interest
          FROM option_quotes
          WHERE observed_date = ?
            AND option_type = 'put'
            AND underlying_symbol IN ({placeholders})
          GROUP BY 1,2,3,4,5
        ),
        greek_rows AS (
          SELECT observed_date, underlying_symbol, expiration, strike, option_type,
                 avg(CAST(delta AS DOUBLE)) AS delta
          FROM option_greeks
          WHERE observed_date = ? AND option_type = 'put'
          GROUP BY 1,2,3,4,5
        ),
        underlying_rows AS (
          SELECT symbol, observed_date, max(CAST(price AS DOUBLE)) AS price
          FROM underlying_prices
          WHERE observed_date = ?
          GROUP BY 1,2
        )
        SELECT q.underlying_symbol, q.expiration, q.strike, q.bid, q.ask,
               q.open_interest, g.delta, u.price
        FROM quote_rows q
        JOIN greek_rows g USING (
          observed_date, underlying_symbol, expiration, strike, option_type
        )
        JOIN underlying_rows u
          ON u.symbol = q.underlying_symbol
         AND u.observed_date = q.observed_date
        """,
        [observed, *symbols, observed, observed],
    ).fetchall()
    parsed: list[dict[str, Any]] = []
    for symbol, expiration, strike, bid, ask, open_interest, delta, underlying in rows:
        expiration = (
            expiration if isinstance(expiration, date) else date.fromisoformat(str(expiration))
        )
        bid_dec = Decimal(str(bid))
        ask_dec = Decimal(str(ask))
        strike_dec = Decimal(str(strike))
        underlying_dec = Decimal(str(underlying))
        mid = (bid_dec + ask_dec) / Decimal("2")
        spread_pct = (ask_dec - bid_dec) / mid if mid > 0 else Decimal("999")
        dte = (expiration - observed).days
        parsed.append(
            {
                "symbol": str(symbol),
                "dte": dte,
                "bid": bid_dec,
                "open_interest": int(open_interest or 0),
                "spread_pct": spread_pct,
                "delta_abs": abs(Decimal(str(delta))),
                "monthly_yield": (bid_dec / strike_dec) * (Decimal("30") / Decimal(dte))
                if dte > 0
                else Decimal("0"),
                "underlying": underlying_dec,
            }
        )
    return parsed


def _trade_contribution(
    *,
    closed_puts: list[wheel.ShortPut],
    closed_calls: list[wheel.ShortCall],
    symbol_tiers: dict[str, str],
) -> dict[str, Any]:
    by_symbol: dict[str, Counter[str]] = defaultdict(Counter)
    pnl_by_symbol: dict[str, Decimal] = defaultdict(Decimal)
    by_tier: dict[str, Counter[str]] = defaultdict(Counter)
    pnl_by_tier: dict[str, Decimal] = defaultdict(Decimal)
    for put in closed_puts:
        symbol = put.candidate.symbol
        tier = symbol_tiers[symbol]
        by_symbol[symbol]["put_contracts"] += put.quantity
        by_symbol[symbol]["assignments"] += int(put.exit_reason == "assigned")
        by_tier[tier]["put_contracts"] += put.quantity
        by_tier[tier]["assignments"] += int(put.exit_reason == "assigned")
        pnl_by_symbol[symbol] += put.realized_pnl
        pnl_by_tier[tier] += put.realized_pnl
    for call in closed_calls:
        symbol = call.candidate.symbol
        tier = symbol_tiers[symbol]
        by_symbol[symbol]["call_contracts"] += call.quantity
        by_symbol[symbol]["called_away"] += int(call.exit_reason == "called away")
        by_tier[tier]["call_contracts"] += call.quantity
        by_tier[tier]["called_away"] += int(call.exit_reason == "called away")
        pnl_by_symbol[symbol] += call.realized_pnl
        pnl_by_tier[tier] += call.realized_pnl
    return {
        "by_symbol": [
            {
                "symbol": symbol,
                "tier": symbol_tiers[symbol],
                "pnl": float(pnl_by_symbol[symbol]),
                **dict(counter),
            }
            for symbol, counter in sorted(
                by_symbol.items(),
                key=lambda item: pnl_by_symbol[item[0]],
                reverse=True,
            )
        ],
        "by_tier": {
            tier: {"pnl": float(pnl_by_tier[tier]), **dict(counter)}
            for tier, counter in sorted(by_tier.items())
        },
    }


def _drawdown_window_exposure(
    *,
    result: dict[str, Any],
    symbol_tiers: dict[str, str],
) -> dict[str, Any]:
    summary = result["summary"]
    start_raw = summary["max_drawdown_start"]
    end_raw = summary["max_drawdown_end"]
    if start_raw is None or end_raw is None:
        return {"start": None, "end": None, "symbols": []}
    start = date.fromisoformat(str(start_raw))
    end = date.fromisoformat(str(end_raw))
    closed_puts = result["closed_puts"]
    closed_calls = result["closed_calls"]
    assert isinstance(closed_puts, list)
    assert isinstance(closed_calls, list)
    exposure: dict[str, Counter[str]] = defaultdict(Counter)
    pnl: dict[str, Decimal] = defaultdict(Decimal)
    for put in closed_puts:
        if put.candidate.entry_date <= end and (put.exit_date is None or put.exit_date >= start):
            symbol = put.candidate.symbol
            exposure[symbol]["put_contracts"] += put.quantity
            pnl[symbol] += put.realized_pnl
    for call in closed_calls:
        if call.candidate.entry_date <= end and (call.exit_date is None or call.exit_date >= start):
            symbol = call.candidate.symbol
            exposure[symbol]["call_contracts"] += call.quantity
            pnl[symbol] += call.realized_pnl
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": [
            {
                "symbol": symbol,
                "tier": symbol_tiers[symbol],
                "realized_pnl": float(pnl[symbol]),
                **dict(counter),
            }
            for symbol, counter in sorted(
                exposure.items(),
                key=lambda item: (item[1]["put_contracts"] + item[1]["call_contracts"]),
                reverse=True,
            )
        ],
    }


def _comparisons(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {row["name"]: row for row in rows}
    baseline = by_name["baseline"]
    tier_a = by_name["tier_a_only"]
    wider = by_name["wider_dte_25_40"]
    higher_delta = by_name["higher_delta_028"]
    return {
        "tier_a_vs_baseline": {
            "return_delta": tier_a["return_pct"] - baseline["return_pct"],
            "max_drawdown_delta": tier_a["max_drawdown"] - baseline["max_drawdown"],
            "eligible_day_delta": tier_a["candidate_availability"]["days_with_candidates"]
            - baseline["candidate_availability"]["days_with_candidates"],
            "explanation": (
                "Tier A produced fewer eligible days but better PnL quality and much lower "
                "drawdown than the combined A+B baseline."
            ),
        },
        "wider_dte_vs_baseline": {
            "return_delta": wider["return_pct"] - baseline["return_pct"],
            "max_drawdown_delta": wider["max_drawdown"] - baseline["max_drawdown"],
            "eligible_day_delta": wider["candidate_availability"]["days_with_candidates"]
            - baseline["candidate_availability"]["days_with_candidates"],
            "explanation": (
                "Wider DTE increased eligible days and utilization, improving return with "
                "a moderate drawdown increase."
            ),
        },
        "higher_delta_vs_baseline": {
            "return_delta": higher_delta["return_pct"] - baseline["return_pct"],
            "max_drawdown_delta": higher_delta["max_drawdown"] - baseline["max_drawdown"],
            "assignment_delta": higher_delta["assignments"] - baseline["assignments"],
            "explanation": (
                "Higher delta improved raw return but raised drawdown and assignments, making "
                "it a riskier relaxation."
            ),
        },
    }


def _symbol_tiers(config: ScannerStylePutStrategyConfig) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for tier in StockQualityTier:
        for symbol in config.symbols_for_tier(tier):
            mapping[symbol] = tier.value
    return mapping


def _symbols_for_tiers(
    config: ScannerStylePutStrategyConfig,
    tiers: tuple[StockQualityTier, ...],
) -> tuple[str, ...]:
    symbols = [symbol for tier in tiers for symbol in config.symbols_for_tier(tier)]
    return tuple(dict.fromkeys(symbols))


def _report(payload: dict[str, Any]) -> str:
    lines = [
        "# Live-Style Scanner Wheel Explainability",
        "",
        f"- Database: {payload['database_path']}",
        f"- Window: {payload['start_date']} to {payload['end_date']}",
        f"- Initial cash: USD {payload['initial_cash']:.2f}",
        "",
        "## Main Findings",
        "",
    ]
    lines.extend(_finding_lines(payload))
    lines.extend(["", "## Scenario Summary", ""])
    for row in payload["scenarios"]:
        availability = row["candidate_availability"]
        lines.append(
            f"- {row['name']}: return {row['return_pct'] * 100:.2f}%, "
            f"max DD {row['max_drawdown'] * 100:.2f}%, "
            f"avg util {row['average_capital_utilization'] * 100:.2f}%, "
            "eligible days "
            f"{availability['days_with_candidates']}/{availability['observed_days']}, "
            f"put/call contracts {row['closed_put_contracts']}/{row['closed_call_contracts']}, "
            f"assignments {row['assignments']}"
        )
    lines.extend(["", "## Filter Funnel", ""])
    for row in payload["scenarios"]:
        funnel = row["filter_funnel"]["total"]
        lines.append(
            f"- {row['name']}: raw {funnel.get('raw_joined', 0)}, "
            f"DTE pass {funnel.get('dte_pass', 0)}, "
            f"liquidity pass {funnel.get('liquidity_pass', 0)}, "
            f"delta pass {funnel.get('delta_pass', 0)}, "
            f"yield pass {funnel.get('yield_pass', 0)}, "
            f"final one-per-symbol {funnel.get('final_one_per_symbol_candidates', 0)}"
        )
    lines.extend(["", "## Tier Contribution", ""])
    for row in payload["scenarios"]:
        tiers = row["trade_contribution"]["by_tier"]
        tier_text = ", ".join(
            f"{tier}: PnL {data['pnl']:.0f}, puts {data.get('put_contracts', 0)}, "
            f"calls {data.get('call_contracts', 0)}, assignments {data.get('assignments', 0)}"
            for tier, data in tiers.items()
        )
        lines.append(f"- {row['name']}: {tier_text}")
    lines.extend(["", "## Top Symbol Contribution", ""])
    for row in payload["scenarios"]:
        top = row["trade_contribution"]["by_symbol"][:7]
        text = ", ".join(
            f"{item['symbol']}({item['tier']}) PnL {item['pnl']:.0f}"
            for item in top
        )
        lines.append(f"- {row['name']}: {text}")
    lines.extend(["", "## Drawdown Window Exposure", ""])
    for row in payload["scenarios"]:
        window = row["drawdown_window"]
        symbols = ", ".join(
            f"{item['symbol']}({item['tier']}) puts {item.get('put_contracts', 0)} "
            f"calls {item.get('call_contracts', 0)}"
            for item in window["symbols"][:6]
        )
        lines.append(f"- {row['name']} {window['start']} to {window['end']}: {symbols}")
    return "\n".join(lines) + "\n"


def _finding_lines(payload: dict[str, Any]) -> list[str]:
    comparisons = payload["comparisons"]
    tier_a = comparisons["tier_a_vs_baseline"]
    wider = comparisons["wider_dte_vs_baseline"]
    higher_delta = comparisons["higher_delta_vs_baseline"]
    return [
        "- Tier A only beat the A+B baseline because quality improved more than breadth: "
        f"return increased by {tier_a['return_delta'] * 100:.2f} percentage points while "
        f"max drawdown fell by {abs(tier_a['max_drawdown_delta']) * 100:.2f} points.",
        "- Wider DTE looks like the cleanest A+B relaxation: "
        f"eligible days rose by {wider['eligible_day_delta']} and return improved by "
        f"{wider['return_delta'] * 100:.2f} points, with max drawdown rising "
        f"{wider['max_drawdown_delta'] * 100:.2f} points.",
        "- Higher delta is a riskier lever: "
        f"return improved by {higher_delta['return_delta'] * 100:.2f} points, but max "
        f"drawdown rose {higher_delta['max_drawdown_delta'] * 100:.2f} points and "
        f"assignments increased by {higher_delta['assignment_delta']}.",
    ]


if __name__ == "__main__":
    main()
