#!/usr/bin/env python
"""Run an offline scanner-style cash-secured put portfolio test from DuckDB."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb

from options_quant.strategies.scanner_put import ScannerStylePutStrategyConfig, StockQualityTier

CONTRACT_MULTIPLIER = Decimal("100")
ZERO = Decimal("0")


@dataclass(frozen=True)
class Candidate:
    entry_date: date
    symbol: str
    expiration: date
    strike: Decimal
    bid: Decimal
    ask: Decimal
    mark: Decimal
    delta: Decimal
    open_interest: int
    volume: int | None
    underlying: Decimal
    dte: int
    monthly_yield: Decimal
    spread_pct: Decimal
    score: Decimal


@dataclass
class Position:
    candidate: Candidate
    entry_credit: Decimal
    collateral: Decimal
    exit_date: date | None = None
    exit_value: Decimal | None = None
    realized_pnl: Decimal | None = None
    exit_reason: str | None = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--tier", choices=[tier.value for tier in StockQualityTier], default="A")
    parser.add_argument("--initial-cash", type=Decimal, default=Decimal("500000"))
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal("0.65"))
    parser.add_argument("--entry-weekday", type=int, default=4)
    parser.add_argument("--max-open-positions", type=int, default=4)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--summary-path", type=Path, required=True)
    args = parser.parse_args()

    config = ScannerStylePutStrategyConfig()
    tier = StockQualityTier(args.tier)
    tier_rule = config.tier_rules[tier]
    symbols = config.symbols_for_tier(tier)
    con = duckdb.connect(str(args.database_path), read_only=True)
    try:
        result = _run_portfolio(
            con=con,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            entry_weekday=args.entry_weekday,
            initial_cash=args.initial_cash,
            commission_per_contract=args.commission_per_contract,
            max_open_positions=args.max_open_positions,
            max_candidates_per_run=config.portfolio.top_n_to_publish,
            min_dte=config.put_entry.min_dte,
            max_dte=config.put_entry.max_dte,
            max_delta=tier_rule.max_delta,
            min_monthly_yield=tier_rule.min_put_monthly_yield,
            min_bid=config.put_entry.liquidity.min_bid,
            min_open_interest=config.put_entry.liquidity.min_open_interest,
            max_spread_pct=config.put_entry.liquidity.max_bid_ask_spread_pct,
        )
    finally:
        con.close()

    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.summary_path.parent.mkdir(parents=True, exist_ok=True)
    args.summary_path.write_text(json.dumps(result["summary"], indent=2) + "\n", encoding="utf-8")
    args.report_path.write_text(
        _report(
            database_path=args.database_path,
            tier=tier,
            start_date=args.start_date,
            end_date=args.end_date,
            initial_cash=args.initial_cash,
            result=result,
        ),
        encoding="utf-8",
    )
    summary = result["summary"]
    print(f"Report: {args.report_path}", flush=True)
    print(f"Summary JSON: {args.summary_path}", flush=True)
    print(
        "Portfolio: "
        f"closed={summary['closed_trades']} "
        f"pnl={summary['total_realized_pnl']:.2f} "
        f"final_equity={summary['final_equity']:.2f} "
        f"return={summary['return_pct']:.4f} "
        f"win_rate={summary['win_rate']} "
        f"assigned={summary['assigned_or_itm_expiration']} "
        f"max_open={summary['max_open_positions']} "
        f"skipped={summary['skipped_count']}",
        flush=True,
    )


def _run_portfolio(
    *,
    con: duckdb.DuckDBPyConnection,
    symbols: tuple[str, ...],
    start_date: date,
    end_date: date,
    entry_weekday: int,
    initial_cash: Decimal,
    commission_per_contract: Decimal,
    max_open_positions: int,
    max_candidates_per_run: int,
    min_dte: int,
    max_dte: int,
    max_delta: Decimal,
    min_monthly_yield: Decimal,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
) -> dict[str, object]:
    cash = initial_cash
    open_positions: list[Position] = []
    closed: list[Position] = []
    skipped: list[dict[str, object]] = []
    scans = 0

    for entry_date in _entry_dates(start_date, end_date, entry_weekday):
        scans += 1
        for position in list(open_positions):
            _close_position(con, position, entry_date, commission_per_contract)
            if position.exit_date is not None and position.exit_date <= entry_date:
                cash -= position.exit_value or ZERO
                open_positions.remove(position)
                closed.append(position)
        reserved = sum((position.collateral for position in open_positions), ZERO)
        accepted_today = 0
        candidates = _candidates(
            con=con,
            symbols=symbols,
            entry_date=entry_date,
            min_dte=min_dte,
            max_dte=max_dte,
            max_delta=max_delta,
            min_monthly_yield=min_monthly_yield,
            min_bid=min_bid,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
            max_candidates=max_candidates_per_run,
        )
        for candidate in candidates:
            if (
                accepted_today >= max_candidates_per_run
                or len(open_positions) >= max_open_positions
            ):
                break
            if any(position.candidate.symbol == candidate.symbol for position in open_positions):
                skipped.append(
                    {
                        "date": entry_date.isoformat(),
                        "symbol": candidate.symbol,
                        "reason": "symbol already open",
                    }
                )
                continue
            collateral = candidate.strike * CONTRACT_MULTIPLIER
            available_cash = cash - reserved
            if collateral > available_cash:
                skipped.append(
                    {
                        "date": entry_date.isoformat(),
                        "symbol": candidate.symbol,
                        "reason": "insufficient collateral",
                        "collateral": float(collateral),
                        "available_cash": float(available_cash),
                    }
                )
                continue
            entry_credit = candidate.bid * CONTRACT_MULTIPLIER - commission_per_contract
            open_positions.append(
                Position(candidate=candidate, entry_credit=entry_credit, collateral=collateral)
            )
            cash += entry_credit
            reserved += collateral
            accepted_today += 1

    for position in list(open_positions):
        _close_position(con, position, end_date + timedelta(days=60), commission_per_contract)
        cash -= position.exit_value or ZERO
        open_positions.remove(position)
        closed.append(position)

    total_pnl = sum((position.realized_pnl or ZERO for position in closed), ZERO)
    final_equity = initial_cash + total_pnl
    wins = [position for position in closed if (position.realized_pnl or ZERO) > ZERO]
    assigned = [
        position
        for position in closed
        if position.exit_reason == "assigned/intrinsic at expiration"
    ]
    by_reason: dict[str, int] = defaultdict(int)
    by_symbol: dict[str, Decimal] = defaultdict(Decimal)
    for position in closed:
        by_reason[position.exit_reason or "unknown"] += 1
        by_symbol[position.candidate.symbol] += position.realized_pnl or ZERO

    max_open = _max_open_positions(closed)
    capital = _capital_utilization(closed, start_date, end_date + timedelta(days=60), initial_cash)
    monthly = _monthly_returns(closed, initial_cash)
    summary = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "scans": scans,
        "initial_cash": float(initial_cash),
        "final_equity": float(final_equity),
        "total_realized_pnl": float(total_pnl),
        "return_pct": float(total_pnl / initial_cash),
        "closed_trades": len(closed),
        "win_rate": float(Decimal(len(wins)) / Decimal(len(closed))) if closed else None,
        "assigned_or_itm_expiration": len(assigned),
        "max_open_positions": max_open,
        "average_capital_utilization": float(capital["average"]),
        "max_capital_utilization": float(capital["max"]),
        "average_reserved_collateral": float(capital["average_reserved"]),
        "max_reserved_collateral": float(capital["max_reserved"]),
        "skipped_count": len(skipped),
        "exit_reasons": dict(sorted(by_reason.items())),
        "monthly_returns": monthly,
        "pnl_by_symbol": {symbol: float(pnl) for symbol, pnl in sorted(by_symbol.items())},
    }
    return {
        "summary": summary,
        "closed": closed,
        "skipped": skipped,
    }


def _candidates(
    *,
    con: duckdb.DuckDBPyConnection,
    symbols: tuple[str, ...],
    entry_date: date,
    min_dte: int,
    max_dte: int,
    max_delta: Decimal,
    min_monthly_yield: Decimal,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
    max_candidates: int,
) -> list[Candidate]:
    placeholders = ",".join(["?"] * len(symbols))
    rows = con.execute(
        f"""
        WITH quote_rows AS (
          SELECT
            q.observed_date,
            q.underlying_symbol,
            q.expiration,
            q.strike,
            q.option_type,
            max(CAST(q.bid AS DOUBLE)) AS bid,
            max(CAST(q.ask AS DOUBLE)) AS ask,
            max(
              coalesce(
                CAST(q.mark AS DOUBLE),
                (CAST(q.bid AS DOUBLE) + CAST(q.ask AS DOUBLE)) / 2
              )
            ) AS mark,
            max(CAST(q.open_interest AS BIGINT)) AS open_interest,
            max(CAST(q.volume AS BIGINT)) AS volume
          FROM option_quotes q
          WHERE q.observed_date = ?
            AND q.option_type = 'put'
            AND q.underlying_symbol IN ({placeholders})
          GROUP BY 1,2,3,4,5
        ),
        greek_rows AS (
          SELECT
            observed_date,
            underlying_symbol,
            expiration,
            strike,
            option_type,
            avg(CAST(delta AS DOUBLE)) AS delta
          FROM option_greeks
          WHERE observed_date = ?
            AND option_type = 'put'
          GROUP BY 1,2,3,4,5
        ),
        underlying_rows AS (
          SELECT symbol, observed_date, max(CAST(price AS DOUBLE)) AS price
          FROM underlying_prices
          WHERE observed_date = ?
          GROUP BY 1,2
        )
        SELECT
          q.underlying_symbol,
          q.expiration,
          q.strike,
          q.bid,
          q.ask,
          q.mark,
          q.open_interest,
          q.volume,
          g.delta,
          u.price
        FROM quote_rows q
        JOIN greek_rows g USING (
          observed_date, underlying_symbol, expiration, strike, option_type
        )
        JOIN underlying_rows u
          ON u.symbol = q.underlying_symbol
         AND u.observed_date = q.observed_date
        """,
        [entry_date, *symbols, entry_date, entry_date],
    ).fetchall()
    candidates: list[Candidate] = []
    for (
        symbol,
        expiration,
        strike,
        bid,
        ask,
        mark,
        open_interest,
        volume,
        delta,
        underlying,
    ) in rows:
        expiration = (
            expiration if isinstance(expiration, date) else date.fromisoformat(str(expiration))
        )
        dte = (expiration - entry_date).days
        if dte < min_dte or dte > max_dte:
            continue
        bid = Decimal(str(bid))
        ask = Decimal(str(ask))
        mark = Decimal(str(mark))
        strike = Decimal(str(strike))
        delta_abs = abs(Decimal(str(delta)))
        underlying = Decimal(str(underlying))
        open_interest = int(open_interest or 0)
        volume = None if volume is None else int(volume)
        if bid < min_bid or open_interest < min_open_interest or delta_abs > max_delta:
            continue
        mid = (bid + ask) / Decimal("2")
        if mid <= ZERO:
            continue
        spread_pct = (ask - bid) / mid
        if spread_pct > max_spread_pct:
            continue
        monthly_yield = (bid / strike) * (Decimal("30") / Decimal(dte))
        if monthly_yield < min_monthly_yield:
            continue
        cushion = (underlying - strike) / underlying
        score = (
            monthly_yield
            + cushion / Decimal("10")
            - spread_pct / Decimal("4")
            + Decimal(open_interest).ln() / Decimal("1000")
        )
        candidates.append(
            Candidate(
                entry_date=entry_date,
                symbol=str(symbol),
                expiration=expiration,
                strike=strike,
                bid=bid,
                ask=ask,
                mark=mark,
                delta=delta_abs,
                open_interest=open_interest,
                volume=volume,
                underlying=underlying,
                dte=dte,
                monthly_yield=monthly_yield,
                spread_pct=spread_pct,
                score=score,
            )
        )
    candidates.sort(
        key=lambda candidate: (candidate.score, candidate.monthly_yield, candidate.open_interest),
        reverse=True,
    )
    picked: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.symbol in seen:
            continue
        picked.append(candidate)
        seen.add(candidate.symbol)
        if len(picked) >= max_candidates:
            break
    return picked


def _close_position(
    con: duckdb.DuckDBPyConnection,
    position: Position,
    up_to: date,
    commission_per_contract: Decimal,
) -> None:
    candidate = position.candidate
    halfway = candidate.entry_date + timedelta(
        days=(candidate.expiration - candidate.entry_date).days // 2
    )
    three_week = candidate.entry_date + timedelta(days=21)
    observed_date = candidate.entry_date + timedelta(days=1)
    last_seen = candidate.entry_date
    while observed_date <= min(candidate.expiration, up_to):
        mark, ask = _quote_for(con, candidate, observed_date)
        if mark is not None and ask is not None:
            last_seen = observed_date
            capture = (candidate.bid - mark) / candidate.bid
            if observed_date < halfway and capture >= Decimal("0.50"):
                _close(
                    position,
                    observed_date,
                    ask * CONTRACT_MULTIPLIER + commission_per_contract,
                    "50% capture before halfway",
                )
                return
            if observed_date >= three_week and capture >= Decimal("0.75"):
                _close(
                    position,
                    observed_date,
                    ask * CONTRACT_MULTIPLIER + commission_per_contract,
                    "75% capture after 21d",
                )
                return
            if (candidate.expiration - observed_date).days <= 3 and capture >= Decimal("0.90"):
                _close(
                    position,
                    observed_date,
                    ask * CONTRACT_MULTIPLIER + commission_per_contract,
                    "90% capture near expiration",
                )
                return
        observed_date += timedelta(days=1)
    underlying = _underlying_for(con, candidate.symbol, candidate.expiration) or _underlying_for(
        con, candidate.symbol, last_seen
    )
    intrinsic = ZERO if underlying is None else max(ZERO, candidate.strike - underlying)
    reason = "expired OTM" if intrinsic == ZERO else "assigned/intrinsic at expiration"
    _close(position, candidate.expiration, intrinsic * CONTRACT_MULTIPLIER, reason)


def _close(position: Position, exit_date: date, exit_value: Decimal, exit_reason: str) -> None:
    position.exit_date = exit_date
    position.exit_value = exit_value
    position.realized_pnl = position.entry_credit - exit_value
    position.exit_reason = exit_reason


def _quote_for(
    con: duckdb.DuckDBPyConnection,
    candidate: Candidate,
    observed_date: date,
) -> tuple[Decimal | None, Decimal | None]:
    row = con.execute(
        """
        SELECT
          max(coalesce(CAST(mark AS DOUBLE), (CAST(bid AS DOUBLE) + CAST(ask AS DOUBLE)) / 2)),
          max(CAST(ask AS DOUBLE))
        FROM option_quotes
        WHERE observed_date = ?
          AND underlying_symbol = ?
          AND expiration = ?
          AND strike = ?
          AND option_type = 'put'
        """,
        [observed_date, candidate.symbol, candidate.expiration, candidate.strike],
    ).fetchone()
    if row is None or row[0] is None:
        return None, None
    mark = Decimal(str(row[0]))
    ask = Decimal(str(row[1])) if row[1] is not None else mark
    return mark, ask


def _underlying_for(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    observed_date: date,
) -> Decimal | None:
    row = con.execute(
        """
        SELECT max(CAST(price AS DOUBLE))
        FROM underlying_prices
        WHERE symbol = ? AND observed_date = ?
        """,
        [symbol, observed_date],
    ).fetchone()
    return None if row is None or row[0] is None else Decimal(str(row[0]))


def _max_open_positions(closed: list[Position]) -> int:
    events: list[tuple[date, int]] = []
    for position in closed:
        events.append((position.candidate.entry_date, 1))
        events.append((position.exit_date or position.candidate.expiration, -1))
    current = 0
    max_open = 0
    for _, delta in sorted(events):
        current += delta
        max_open = max(max_open, current)
    return max_open


def _capital_utilization(
    closed: list[Position],
    start_date: date,
    end_date: date,
    initial_cash: Decimal,
) -> dict[str, Decimal]:
    reserved_by_day: list[Decimal] = []
    current = start_date
    while current <= end_date:
        reserved = sum(
            (
                position.collateral
                for position in closed
                if position.candidate.entry_date <= current
                and (position.exit_date is None or current < position.exit_date)
            ),
            ZERO,
        )
        reserved_by_day.append(reserved)
        current += timedelta(days=1)
    if not reserved_by_day:
        return {
            "average": ZERO,
            "max": ZERO,
            "average_reserved": ZERO,
            "max_reserved": ZERO,
        }
    average_reserved = sum(reserved_by_day, ZERO) / Decimal(len(reserved_by_day))
    max_reserved = max(reserved_by_day)
    return {
        "average": average_reserved / initial_cash,
        "max": max_reserved / initial_cash,
        "average_reserved": average_reserved,
        "max_reserved": max_reserved,
    }


def _monthly_returns(
    closed: list[Position],
    initial_cash: Decimal,
) -> dict[str, dict[str, float | int]]:
    by_month: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"trades": 0, "pnl": ZERO}
    )
    for position in closed:
        month = (position.exit_date or position.candidate.expiration).strftime("%Y-%m")
        by_month[month]["trades"] = int(by_month[month]["trades"]) + 1
        by_month[month]["pnl"] = Decimal(by_month[month]["pnl"]) + (
            position.realized_pnl or ZERO
        )
    return {
        month: {
            "trades": int(values["trades"]),
            "pnl": float(Decimal(values["pnl"])),
            "return_pct": float(Decimal(values["pnl"]) / initial_cash),
        }
        for month, values in sorted(by_month.items())
    }


def _entry_dates(start_date: date, end_date: date, entry_weekday: int) -> list[date]:
    first = start_date
    while first.weekday() != entry_weekday:
        first += timedelta(days=1)
    dates: list[date] = []
    current = first
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=7)
    return dates


def _report(
    *,
    database_path: Path,
    tier: StockQualityTier,
    start_date: date,
    end_date: date,
    initial_cash: Decimal,
    result: dict[str, object],
) -> str:
    closed = result["closed"]
    assert isinstance(closed, list)
    summary = result["summary"]
    assert isinstance(summary, dict)
    by_reason = summary["exit_reasons"]
    assert isinstance(by_reason, dict)
    by_symbol = summary["pnl_by_symbol"]
    assert isinstance(by_symbol, dict)
    top = sorted(closed, key=lambda position: position.realized_pnl or ZERO, reverse=True)[:10]
    bottom = sorted(closed, key=lambda position: position.realized_pnl or ZERO)[:10]
    lines = [
        f"# Scanner Portfolio Test - Tier {tier.value} 2025H1",
        "",
        f"Offline cash-secured put simulation from {database_path}.",
        "",
        "## Assumptions",
        "",
        f"- Initial cash: USD {_money(initial_cash)}",
        f"- Universe: Tier {tier.value} only.",
        f"- Entry cadence: Fridays from {start_date} through {end_date}.",
        "- Candidate filters: 20-35 DTE puts, tier delta cap, tier monthly-yield "
        "floor, bid >= 0.10, OI >= 50, bid/ask spread <= 12%.",
        "- Selection: up to 4 ideas per weekly scan, one open position per ticker, "
        "max 4 open puts, one contract each.",
        "- Credit: entry at bid, exit at ask, 0.65 commission per contract each side.",
        "- Exit rules: 50% capture before halfway, 75% capture after 21 days, "
        "90% capture within 3 days of expiration. Otherwise settle at expiration "
        "intrinsic value.",
        "- Not modeled yet: earnings avoidance, technical support/drawdown filters, "
        "sector caps, VIX reserve, assignment-to-covered-call wheel.",
        "",
        "## Results",
        "",
        f"- Closed trades: {summary['closed_trades']}",
        f"- Total realized PnL: USD {summary['total_realized_pnl']:.2f}",
        f"- Final equity: USD {summary['final_equity']:.2f}",
        f"- Total return on initial cash: {summary['return_pct'] * 100:.2f}%",
        f"- Win rate: {summary['win_rate'] * 100:.2f}%"
        if summary["win_rate"] is not None
        else "- Win rate: n/a",
        f"- Assigned / intrinsic expiration outcomes: {summary['assigned_or_itm_expiration']}",
        f"- Max open positions: {summary['max_open_positions']}",
        f"- Average capital utilization: {summary['average_capital_utilization'] * 100:.2f}%",
        f"- Max capital utilization: {summary['max_capital_utilization'] * 100:.2f}%",
        f"- Average reserved collateral: USD {summary['average_reserved_collateral']:.2f}",
        f"- Max reserved collateral: USD {summary['max_reserved_collateral']:.2f}",
        f"- Skipped candidates: {summary['skipped_count']}",
        "",
        "## Monthly Returns",
        "",
    ]
    monthly = summary["monthly_returns"]
    assert isinstance(monthly, dict)
    for month, values in sorted(monthly.items()):
        assert isinstance(values, dict)
        lines.append(
            f"- {month}: {values['trades']} trades, PnL USD {values['pnl']:.2f}, "
            f"return {values['return_pct'] * 100:.2f}%"
        )
    lines.extend(
        [
            "",
            "## Exit Reasons",
            "",
        ]
    )
    for reason, count in sorted(by_reason.items()):
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## PnL By Symbol", ""])
    for symbol, pnl in sorted(by_symbol.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- {symbol}: USD {pnl:.2f}")
    lines.extend(["", "## Top Trades", ""])
    for position in top:
        lines.append(_trade_line(position))
    lines.extend(["", "## Worst Trades", ""])
    for position in bottom:
        lines.append(_trade_line(position))
    return "\n".join(lines) + "\n"


def _trade_line(position: Position) -> str:
    candidate = position.candidate
    pnl = position.realized_pnl or ZERO
    return (
        f"- {candidate.entry_date} {candidate.symbol} {candidate.expiration} "
        f"{_money(candidate.strike)}P: PnL USD {_money(pnl)}, exit {position.exit_date} "
        f"({position.exit_reason}), entry yield {_percent(candidate.monthly_yield)}, "
        f"delta {candidate.delta.quantize(Decimal('0.001'))}"
    )


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _percent(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


if __name__ == "__main__":
    main()
