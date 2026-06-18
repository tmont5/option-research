#!/usr/bin/env python
"""Run an offline scanner-style wheel portfolio test from DuckDB."""
# ruff: noqa: E501

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

MULTIPLIER = Decimal("100")
ZERO = Decimal("0")


@dataclass(frozen=True)
class OptionCandidate:
    entry_date: date
    symbol: str
    expiration: date
    strike: Decimal
    option_type: str
    bid: Decimal
    ask: Decimal
    mark: Decimal
    delta: Decimal
    open_interest: int
    underlying: Decimal
    dte: int
    monthly_yield: Decimal
    spread_pct: Decimal
    score: Decimal


@dataclass
class ShortPut:
    candidate: OptionCandidate
    quantity: int
    entry_credit: Decimal
    collateral: Decimal
    exit_date: date | None = None
    exit_value: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    exit_reason: str | None = None


@dataclass
class ShareLot:
    symbol: str
    shares: int
    basis: Decimal
    opened_at: date


@dataclass
class ShortCall:
    candidate: OptionCandidate
    lot: ShareLot
    quantity: int
    entry_credit: Decimal
    exit_date: date | None = None
    exit_value: Decimal = ZERO
    realized_pnl: Decimal = ZERO
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
    parser.add_argument("--max-open-puts", type=int, default=12)
    parser.add_argument("--max-contracts-per-position", type=int, default=5)
    parser.add_argument("--target-capital-utilization", type=Decimal, default=Decimal("0.85"))
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--summary-path", type=Path, required=True)
    args = parser.parse_args()

    config = ScannerStylePutStrategyConfig()
    tier = StockQualityTier(args.tier)
    tier_rule = config.tier_rules[tier]
    symbols = config.symbols_for_tier(tier)
    con = duckdb.connect(str(args.database_path), read_only=True)
    try:
        result = _run(
            con=con,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            entry_weekday=args.entry_weekday,
            initial_cash=args.initial_cash,
            commission=args.commission_per_contract,
            max_open_puts=args.max_open_puts,
            max_contracts_per_position=args.max_contracts_per_position,
            target_capital_utilization=args.target_capital_utilization,
            max_candidates_per_run=config.portfolio.top_n_to_publish,
            put_min_dte=config.put_entry.min_dte,
            put_max_dte=config.put_entry.max_dte,
            put_max_delta=tier_rule.max_delta,
            put_min_monthly_yield=tier_rule.min_put_monthly_yield,
            min_bid=config.put_entry.liquidity.min_bid,
            min_open_interest=config.put_entry.liquidity.min_open_interest,
            max_spread_pct=config.put_entry.liquidity.max_bid_ask_spread_pct,
            call_min_monthly_yield=config.covered_call.min_monthly_yield,
            call_min_strike_above_basis=config.covered_call.min_strike_above_breakeven_pct,
            call_max_strike_above_basis=config.covered_call.max_strike_above_breakeven_pct,
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
            initial_cash=args.initial_cash,
            result=result,
        ),
        encoding="utf-8",
    )
    summary = result["summary"]
    print(f"Report: {args.report_path}", flush=True)
    print(f"Summary JSON: {args.summary_path}", flush=True)
    print(
        "Wheel portfolio: "
        f"puts={summary['closed_puts']} "
        f"calls={summary['closed_calls']} "
        f"assignments={summary['assignments']} "
        f"open_shares={summary['open_share_lots']} "
        f"final_equity={summary['final_equity']:.2f} "
        f"return={summary['return_pct']:.4f} "
        f"avg_util={summary['average_capital_utilization']:.4f} "
        f"max_util={summary['max_capital_utilization']:.4f}",
        flush=True,
    )


def _run(
    *,
    con: duckdb.DuckDBPyConnection,
    symbols: tuple[str, ...],
    start_date: date,
    end_date: date,
    entry_weekday: int,
    initial_cash: Decimal,
    commission: Decimal,
    max_open_puts: int,
    max_contracts_per_position: int,
    target_capital_utilization: Decimal,
    max_candidates_per_run: int,
    put_min_dte: int,
    put_max_dte: int,
    put_max_delta: Decimal,
    put_min_monthly_yield: Decimal,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
    call_min_monthly_yield: Decimal,
    call_min_strike_above_basis: Decimal,
    call_max_strike_above_basis: Decimal,
) -> dict[str, object]:
    cash = initial_cash
    open_puts: list[ShortPut] = []
    closed_puts: list[ShortPut] = []
    share_lots: list[ShareLot] = []
    open_calls: list[ShortCall] = []
    closed_calls: list[ShortCall] = []
    skipped: list[dict[str, object]] = []
    utilization_samples: list[Decimal] = []

    for entry_date in _entry_dates(start_date, end_date, entry_weekday):
        cash = _process_puts(con, open_puts, closed_puts, share_lots, entry_date, cash, commission)
        cash = _process_calls(con, open_calls, closed_calls, share_lots, entry_date, cash, commission)

        for lot in list(share_lots):
            if any(call.lot is lot for call in open_calls):
                continue
            call_candidate = _covered_call_candidate(
                con=con,
                lot=lot,
                entry_date=entry_date,
                min_monthly_yield=call_min_monthly_yield,
                min_strike_above_basis=call_min_strike_above_basis,
                max_strike_above_basis=call_max_strike_above_basis,
                min_bid=min_bid,
                min_open_interest=min_open_interest,
                max_spread_pct=max_spread_pct,
            )
            if call_candidate is None:
                continue
            quantity = lot.shares // 100
            entry_credit = call_candidate.bid * MULTIPLIER * Decimal(quantity) - commission * Decimal(quantity)
            cash += entry_credit
            open_calls.append(
                ShortCall(
                    candidate=call_candidate,
                    lot=lot,
                    quantity=quantity,
                    entry_credit=entry_credit,
                )
            )

        reserved = _reserved_collateral(open_puts)
        utilization_samples.append(reserved / initial_cash)
        if len(open_puts) >= max_open_puts:
            continue
        candidates = _put_candidates(
            con=con,
            symbols=symbols,
            entry_date=entry_date,
            min_dte=put_min_dte,
            max_dte=put_max_dte,
            max_delta=put_max_delta,
            min_monthly_yield=put_min_monthly_yield,
            min_bid=min_bid,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
            max_candidates=max_candidates_per_run,
        )
        for candidate in candidates:
            if len(open_puts) >= max_open_puts:
                break
            if any(position.candidate.symbol == candidate.symbol for position in open_puts):
                skipped.append({"date": entry_date.isoformat(), "symbol": candidate.symbol, "reason": "put already open"})
                continue
            if any(lot.symbol == candidate.symbol for lot in share_lots):
                skipped.append({"date": entry_date.isoformat(), "symbol": candidate.symbol, "reason": "shares already held"})
                continue
            per_contract_collateral = candidate.strike * MULTIPLIER
            target_reserved = initial_cash * target_capital_utilization
            available = min(cash - reserved, target_reserved - reserved)
            quantity = int(available // per_contract_collateral)
            quantity = min(quantity, max_contracts_per_position)
            if quantity <= 0:
                skipped.append({"date": entry_date.isoformat(), "symbol": candidate.symbol, "reason": "no collateral room"})
                continue
            collateral = per_contract_collateral * Decimal(quantity)
            entry_credit = candidate.bid * MULTIPLIER * Decimal(quantity) - commission * Decimal(quantity)
            cash += entry_credit
            reserved += collateral
            open_puts.append(
                ShortPut(
                    candidate=candidate,
                    quantity=quantity,
                    entry_credit=entry_credit,
                    collateral=collateral,
                )
            )

    final_date = end_date + timedelta(days=90)
    cash = _process_puts(con, open_puts, closed_puts, share_lots, final_date, cash, commission)
    cash = _process_calls(con, open_calls, closed_calls, share_lots, final_date, cash, commission)
    final_share_value = sum(
        (_underlying_for(con, lot.symbol, final_date) or _latest_underlying(con, lot.symbol) or lot.basis)
        * Decimal(lot.shares)
        for lot in share_lots
    )
    final_equity = cash + final_share_value
    realized_pnl = (cash + final_share_value) - initial_cash
    equity_curve = _equity_curve(
        closed_puts=closed_puts,
        closed_calls=closed_calls,
        share_lots=share_lots,
        con=con,
        start=start_date,
        end=final_date,
        initial_cash=initial_cash,
    )
    risk = _risk_metrics(equity_curve, initial_cash)
    capital = _capital_stats(closed_puts, share_lots, con, start_date, final_date, initial_cash)
    monthly = _monthly_returns(closed_puts, closed_calls, initial_cash)
    summary = {
        "initial_cash": float(initial_cash),
        "cash": float(cash),
        "final_share_value": float(final_share_value),
        "final_equity": float(final_equity),
        "total_pnl": float(realized_pnl),
        "realized_option_pnl": float(
            sum((put.realized_pnl for put in closed_puts), ZERO)
            + sum((call.realized_pnl for call in closed_calls), ZERO)
        ),
        "unrealized_share_pnl": float(
            sum(
                (
                    (
                        _underlying_for(con, lot.symbol, final_date)
                        or _latest_underlying(con, lot.symbol)
                        or lot.basis
                    )
                    - lot.basis
                )
                * Decimal(lot.shares)
                for lot in share_lots
            )
        ),
        "return_pct": float(realized_pnl / initial_cash),
        "closed_puts": len(closed_puts),
        "closed_put_contracts": sum(put.quantity for put in closed_puts),
        "closed_calls": len(closed_calls),
        "closed_call_contracts": sum(call.quantity for call in closed_calls),
        "assignments": sum(1 for put in closed_puts if put.exit_reason == "assigned"),
        "assigned_contracts": sum(
            put.quantity for put in closed_puts if put.exit_reason == "assigned"
        ),
        "open_share_lots": len(share_lots),
        "open_shares": sum(lot.shares for lot in share_lots),
        "open_share_positions": _open_share_positions(share_lots, con, final_date),
        "average_capital_utilization": float(capital["average"]),
        "max_capital_utilization": float(capital["max"]),
        "average_reserved_collateral": float(capital["average_reserved"]),
        "max_reserved_collateral": float(capital["max_reserved"]),
        "max_drawdown": float(risk["max_drawdown"]),
        "max_drawdown_amount": float(risk["max_drawdown_amount"]),
        "max_drawdown_start": risk["max_drawdown_start"],
        "max_drawdown_end": risk["max_drawdown_end"],
        "max_underwater_days": risk["max_underwater_days"],
        "best_day_return": float(risk["best_day_return"]),
        "worst_day_return": float(risk["worst_day_return"]),
        "daily_volatility": float(risk["daily_volatility"]),
        "annualized_volatility": float(risk["annualized_volatility"]),
        "closed_option_win_rate": float(_option_win_rate(closed_puts, closed_calls)),
        "average_closed_option_pnl": float(_average_option_pnl(closed_puts, closed_calls)),
        "worst_closed_option_pnl": float(_worst_option_pnl(closed_puts, closed_calls)),
        "skipped_count": len(skipped),
        "monthly_returns": monthly,
        "put_exit_reasons": _count_reasons(closed_puts),
        "call_exit_reasons": _count_reasons(closed_calls),
        "pnl_by_symbol": _pnl_by_symbol(closed_puts, closed_calls, share_lots, con, final_date),
    }
    return {
        "summary": summary,
        "equity_curve": equity_curve,
        "closed_puts": closed_puts,
        "closed_calls": closed_calls,
        "share_lots": share_lots,
        "skipped": skipped,
    }


def _process_puts(
    con: duckdb.DuckDBPyConnection,
    open_puts: list[ShortPut],
    closed_puts: list[ShortPut],
    share_lots: list[ShareLot],
    up_to: date,
    cash: Decimal,
    commission: Decimal,
) -> Decimal:
    for position in list(open_puts):
        _close_put(con, position, up_to, commission)
        if position.exit_date is None or position.exit_date > up_to:
            continue
        if position.exit_reason == "assigned":
            cash -= position.candidate.strike * MULTIPLIER * Decimal(position.quantity)
            credit_per_share = position.entry_credit / (MULTIPLIER * Decimal(position.quantity))
            basis = position.candidate.strike - credit_per_share
            share_lots.append(
                ShareLot(
                    symbol=position.candidate.symbol,
                    shares=position.quantity * 100,
                    basis=basis,
                    opened_at=position.exit_date,
                )
            )
        else:
            cash -= position.exit_value
        open_puts.remove(position)
        closed_puts.append(position)
    return cash


def _process_calls(
    con: duckdb.DuckDBPyConnection,
    open_calls: list[ShortCall],
    closed_calls: list[ShortCall],
    share_lots: list[ShareLot],
    up_to: date,
    cash: Decimal,
    commission: Decimal,
) -> Decimal:
    for call in list(open_calls):
        _close_call(con, call, up_to, commission)
        if call.exit_date is None or call.exit_date > up_to:
            continue
        if call.exit_reason == "called away":
            proceeds = call.candidate.strike * MULTIPLIER * Decimal(call.quantity)
            cash += proceeds
            if call.lot in share_lots:
                share_lots.remove(call.lot)
        else:
            cash -= call.exit_value
        open_calls.remove(call)
        closed_calls.append(call)
    return cash


def _put_candidates(
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
) -> list[OptionCandidate]:
    candidates = _option_candidates(
        con=con,
        symbols=symbols,
        entry_date=entry_date,
        option_type="put",
        min_dte=min_dte,
        max_dte=max_dte,
        min_bid=min_bid,
        min_open_interest=min_open_interest,
        max_spread_pct=max_spread_pct,
    )
    filtered = [
        candidate
        for candidate in candidates
        if candidate.delta <= max_delta and candidate.monthly_yield >= min_monthly_yield
    ]
    filtered.sort(key=lambda item: (item.score, item.monthly_yield, item.open_interest), reverse=True)
    return _one_per_symbol(filtered, max_candidates)


def _covered_call_candidate(
    *,
    con: duckdb.DuckDBPyConnection,
    lot: ShareLot,
    entry_date: date,
    min_monthly_yield: Decimal,
    min_strike_above_basis: Decimal,
    max_strike_above_basis: Decimal,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
) -> OptionCandidate | None:
    candidates = _option_candidates(
        con=con,
        symbols=(lot.symbol,),
        entry_date=entry_date,
        option_type="call",
        min_dte=20,
        max_dte=35,
        min_bid=min_bid,
        min_open_interest=min_open_interest,
        max_spread_pct=max_spread_pct,
    )
    lower = lot.basis * (Decimal("1") + min_strike_above_basis)
    upper = lot.basis * (Decimal("1") + max_strike_above_basis)
    eligible = [
        candidate
        for candidate in candidates
        if lower <= candidate.strike <= upper and candidate.monthly_yield >= min_monthly_yield
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda item: (item.monthly_yield, item.strike), reverse=True)
    return eligible[0]


def _option_candidates(
    *,
    con: duckdb.DuckDBPyConnection,
    symbols: tuple[str, ...],
    entry_date: date,
    option_type: str,
    min_dte: int,
    max_dte: int,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
) -> list[OptionCandidate]:
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
            AND option_type = ?
            AND underlying_symbol IN ({placeholders})
          GROUP BY 1,2,3,4,5
        ),
        greek_rows AS (
          SELECT observed_date, underlying_symbol, expiration, strike, option_type,
                 avg(CAST(delta AS DOUBLE)) AS delta
          FROM option_greeks
          WHERE observed_date = ? AND option_type = ?
          GROUP BY 1,2,3,4,5
        ),
        underlying_rows AS (
          SELECT symbol, observed_date, max(CAST(price AS DOUBLE)) AS price
          FROM underlying_prices
          WHERE observed_date = ?
          GROUP BY 1,2
        )
        SELECT q.underlying_symbol, q.expiration, q.strike, q.option_type, q.bid,
               q.ask, q.mark, q.open_interest, g.delta, u.price
        FROM quote_rows q
        JOIN greek_rows g USING (
          observed_date, underlying_symbol, expiration, strike, option_type
        )
        JOIN underlying_rows u
          ON u.symbol = q.underlying_symbol
         AND u.observed_date = q.observed_date
        """,
        [entry_date, option_type, *symbols, entry_date, option_type, entry_date],
    ).fetchall()
    candidates: list[OptionCandidate] = []
    for symbol, expiration, strike, option_type, bid, ask, mark, open_interest, delta, underlying in rows:
        expiration = expiration if isinstance(expiration, date) else date.fromisoformat(str(expiration))
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
        if bid < min_bid or open_interest < min_open_interest:
            continue
        mid = (bid + ask) / Decimal("2")
        if mid <= ZERO:
            continue
        spread_pct = (ask - bid) / mid
        if spread_pct > max_spread_pct:
            continue
        monthly_yield = (bid / strike) * (Decimal("30") / Decimal(dte))
        cushion = abs(underlying - strike) / underlying
        score = (
            monthly_yield
            + cushion / Decimal("20")
            - spread_pct / Decimal("4")
            + Decimal(open_interest).ln() / Decimal("1000")
        )
        candidates.append(
            OptionCandidate(
                entry_date=entry_date,
                symbol=str(symbol),
                expiration=expiration,
                strike=strike,
                option_type=str(option_type),
                bid=bid,
                ask=ask,
                mark=mark,
                delta=delta_abs,
                open_interest=open_interest,
                underlying=underlying,
                dte=dte,
                monthly_yield=monthly_yield,
                spread_pct=spread_pct,
                score=score,
            )
        )
    return candidates


def _one_per_symbol(candidates: list[OptionCandidate], limit: int) -> list[OptionCandidate]:
    picked: list[OptionCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.symbol in seen:
            continue
        picked.append(candidate)
        seen.add(candidate.symbol)
        if len(picked) >= limit:
            break
    return picked


def _close_put(
    con: duckdb.DuckDBPyConnection,
    position: ShortPut,
    up_to: date,
    commission: Decimal,
) -> None:
    candidate = position.candidate
    halfway = candidate.entry_date + timedelta(days=(candidate.expiration - candidate.entry_date).days // 2)
    three_week = candidate.entry_date + timedelta(days=21)
    observed = candidate.entry_date + timedelta(days=1)
    while observed <= min(candidate.expiration, up_to):
        mark, ask = _quote_for(con, candidate, observed)
        if mark is not None and ask is not None:
            capture = (candidate.bid - mark) / candidate.bid
            if observed < halfway and capture >= Decimal("0.50"):
                _close_short_option(position, observed, ask, commission, "50% capture before halfway")
                return
            if observed >= three_week and capture >= Decimal("0.75"):
                _close_short_option(position, observed, ask, commission, "75% capture after 21d")
                return
            if (candidate.expiration - observed).days <= 3 and capture >= Decimal("0.90"):
                _close_short_option(position, observed, ask, commission, "90% capture near expiration")
                return
        observed += timedelta(days=1)
    underlying = _underlying_for(con, candidate.symbol, candidate.expiration)
    if underlying is not None and underlying < candidate.strike:
        position.exit_date = candidate.expiration
        position.exit_reason = "assigned"
        position.realized_pnl = position.entry_credit
        return
    position.exit_date = candidate.expiration
    position.exit_reason = "expired OTM"
    position.realized_pnl = position.entry_credit


def _close_call(
    con: duckdb.DuckDBPyConnection,
    call: ShortCall,
    up_to: date,
    commission: Decimal,
) -> None:
    candidate = call.candidate
    observed = candidate.entry_date + timedelta(days=1)
    while observed <= min(candidate.expiration, up_to):
        mark, ask = _quote_for(con, candidate, observed)
        if mark is not None and ask is not None:
            capture = (candidate.bid - mark) / candidate.bid
            if capture >= Decimal("0.75"):
                _close_short_call(call, observed, ask, commission, "75% call capture")
                return
        observed += timedelta(days=1)
    underlying = _underlying_for(con, candidate.symbol, candidate.expiration)
    if underlying is not None and underlying > candidate.strike:
        share_pnl = (candidate.strike - call.lot.basis) * Decimal(call.lot.shares)
        call.exit_date = candidate.expiration
        call.exit_reason = "called away"
        call.realized_pnl = call.entry_credit + share_pnl
        return
    call.exit_date = candidate.expiration
    call.exit_reason = "call expired OTM"
    call.realized_pnl = call.entry_credit


def _close_short_option(
    position: ShortPut,
    exit_date: date,
    ask: Decimal,
    commission: Decimal,
    reason: str,
) -> None:
    exit_value = ask * MULTIPLIER * Decimal(position.quantity) + commission * Decimal(position.quantity)
    position.exit_date = exit_date
    position.exit_value = exit_value
    position.realized_pnl = position.entry_credit - exit_value
    position.exit_reason = reason


def _close_short_call(
    call: ShortCall,
    exit_date: date,
    ask: Decimal,
    commission: Decimal,
    reason: str,
) -> None:
    exit_value = ask * MULTIPLIER * Decimal(call.quantity) + commission * Decimal(call.quantity)
    call.exit_date = exit_date
    call.exit_value = exit_value
    call.realized_pnl = call.entry_credit - exit_value
    call.exit_reason = reason


def _reserved_collateral(open_puts: list[ShortPut]) -> Decimal:
    return sum((position.collateral for position in open_puts), ZERO)


def _quote_for(
    con: duckdb.DuckDBPyConnection,
    candidate: OptionCandidate,
    observed: date,
) -> tuple[Decimal | None, Decimal | None]:
    row = con.execute(
        """
        SELECT max(coalesce(CAST(mark AS DOUBLE),
                   (CAST(bid AS DOUBLE) + CAST(ask AS DOUBLE)) / 2)),
               max(CAST(ask AS DOUBLE))
        FROM option_quotes
        WHERE observed_date = ?
          AND underlying_symbol = ?
          AND expiration = ?
          AND CAST(strike AS DOUBLE) = ?
          AND option_type = ?
        """,
        [
            observed,
            candidate.symbol,
            candidate.expiration,
            float(candidate.strike),
            candidate.option_type,
        ],
    ).fetchone()
    if row is None or row[0] is None:
        return None, None
    mark = Decimal(str(row[0]))
    ask = Decimal(str(row[1])) if row[1] is not None else mark
    return mark, ask


def _underlying_for(con: duckdb.DuckDBPyConnection, symbol: str, observed: date) -> Decimal | None:
    row = con.execute(
        """
        SELECT max(CAST(price AS DOUBLE))
        FROM underlying_prices
        WHERE symbol = ? AND observed_date = ?
        """,
        [symbol, observed],
    ).fetchone()
    return None if row is None or row[0] is None else Decimal(str(row[0]))


def _latest_underlying(con: duckdb.DuckDBPyConnection, symbol: str) -> Decimal | None:
    row = con.execute(
        """
        SELECT CAST(price AS DOUBLE)
        FROM underlying_prices
        WHERE symbol = ?
        ORDER BY observed_date DESC
        LIMIT 1
        """,
        [symbol],
    ).fetchone()
    return None if row is None or row[0] is None else Decimal(str(row[0]))


def _entry_dates(start: date, end: date, weekday: int) -> list[date]:
    current = start
    while current.weekday() != weekday:
        current += timedelta(days=1)
    dates: list[date] = []
    while current <= end:
        dates.append(current)
        current += timedelta(days=7)
    return dates


def _capital_stats(
    closed_puts: list[ShortPut],
    share_lots: list[ShareLot],
    con: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    initial_cash: Decimal,
) -> dict[str, Decimal]:
    samples: list[Decimal] = []
    current = start
    while current <= end:
        put_collateral = sum(
            (
                put.collateral
                for put in closed_puts
                if put.candidate.entry_date <= current
                and (put.exit_date is None or current < put.exit_date)
            ),
            ZERO,
        )
        stock_exposure = sum(
            (
                (_underlying_for(con, lot.symbol, current) or lot.basis)
                * Decimal(lot.shares)
                for lot in share_lots
                if lot.opened_at <= current
            ),
            ZERO,
        )
        samples.append(put_collateral + stock_exposure)
        current += timedelta(days=1)
    if not samples:
        return {"average": ZERO, "max": ZERO, "average_reserved": ZERO, "max_reserved": ZERO}
    average_reserved = sum(samples, ZERO) / Decimal(len(samples))
    max_reserved = max(samples)
    return {
        "average": average_reserved / initial_cash,
        "max": max_reserved / initial_cash,
        "average_reserved": average_reserved,
        "max_reserved": max_reserved,
    }


def _equity_curve(
    *,
    closed_puts: list[ShortPut],
    closed_calls: list[ShortCall],
    share_lots: list[ShareLot],
    con: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    initial_cash: Decimal,
) -> list[dict[str, float | str]]:
    curve: list[dict[str, float | str]] = []
    current = start
    while current <= end:
        option_pnl = sum(
            (
                _position_pnl_to_date(con, put, current)
                for put in closed_puts
                if put.candidate.entry_date <= current
            ),
            ZERO,
        ) + sum(
            (
                _call_pnl_to_date(con, call, current)
                for call in closed_calls
                if call.candidate.entry_date <= current
            ),
            ZERO,
        )
        share_pnl = sum(
            (
                ((_underlying_for(con, lot.symbol, current) or lot.basis) - lot.basis)
                * Decimal(lot.shares)
                for lot in share_lots
                if lot.opened_at <= current
            ),
            ZERO,
        )
        equity = initial_cash + option_pnl + share_pnl
        curve.append(
            {
                "date": current.isoformat(),
                "equity": float(equity),
                "option_pnl": float(option_pnl),
                "share_pnl": float(share_pnl),
            }
        )
        current += timedelta(days=1)
    return curve


def _position_pnl_to_date(
    con: duckdb.DuckDBPyConnection,
    put: ShortPut,
    observed: date,
) -> Decimal:
    if put.exit_date is not None and observed >= put.exit_date:
        return put.realized_pnl
    mark, _ = _quote_for(con, put.candidate, observed)
    if mark is None:
        return ZERO
    return put.entry_credit - mark * MULTIPLIER * Decimal(put.quantity)


def _call_pnl_to_date(
    con: duckdb.DuckDBPyConnection,
    call: ShortCall,
    observed: date,
) -> Decimal:
    if call.exit_date is not None and observed >= call.exit_date:
        return call.realized_pnl
    mark, _ = _quote_for(con, call.candidate, observed)
    if mark is None:
        return ZERO
    return call.entry_credit - mark * MULTIPLIER * Decimal(call.quantity)


def _risk_metrics(
    equity_curve: list[dict[str, float | str]],
    initial_cash: Decimal,
) -> dict[str, Decimal | int | str | None]:
    if not equity_curve:
        return {
            "max_drawdown": ZERO,
            "max_drawdown_amount": ZERO,
            "max_drawdown_start": None,
            "max_drawdown_end": None,
            "max_underwater_days": 0,
            "best_day_return": ZERO,
            "worst_day_return": ZERO,
            "daily_volatility": ZERO,
            "annualized_volatility": ZERO,
        }
    peak = Decimal(str(equity_curve[0]["equity"]))
    peak_date = str(equity_curve[0]["date"])
    max_drawdown = ZERO
    max_drawdown_amount = ZERO
    max_start: str | None = None
    max_end: str | None = None
    underwater_start: date | None = None
    max_underwater_days = 0
    returns: list[Decimal] = []
    previous = peak
    for row in equity_curve:
        equity = Decimal(str(row["equity"]))
        row_date = date.fromisoformat(str(row["date"]))
        if previous != ZERO:
            returns.append((equity - previous) / previous)
        previous = equity
        if equity >= peak:
            peak = equity
            peak_date = str(row["date"])
            if underwater_start is not None:
                max_underwater_days = max(max_underwater_days, (row_date - underwater_start).days)
                underwater_start = None
            continue
        if underwater_start is None:
            underwater_start = row_date
        drawdown_amount = peak - equity
        drawdown = drawdown_amount / peak if peak else ZERO
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_amount = drawdown_amount
            max_start = peak_date
            max_end = str(row["date"])
    if underwater_start is not None:
        max_underwater_days = max(
            max_underwater_days,
            (date.fromisoformat(str(equity_curve[-1]["date"])) - underwater_start).days,
        )
    daily_vol = _stddev(returns)
    return {
        "max_drawdown": max_drawdown,
        "max_drawdown_amount": max_drawdown_amount,
        "max_drawdown_start": max_start,
        "max_drawdown_end": max_end,
        "max_underwater_days": max_underwater_days,
        "best_day_return": max(returns, default=ZERO),
        "worst_day_return": min(returns, default=ZERO),
        "daily_volatility": daily_vol,
        "annualized_volatility": daily_vol * Decimal("252").sqrt(),
    }


def _stddev(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return ZERO
    mean = sum(values, ZERO) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), ZERO) / Decimal(len(values) - 1)
    return variance.sqrt()


def _option_win_rate(closed_puts: list[ShortPut], closed_calls: list[ShortCall]) -> Decimal:
    items = [*closed_puts, *closed_calls]
    if not items:
        return ZERO
    wins = sum(1 for item in items if item.realized_pnl > ZERO)
    return Decimal(wins) / Decimal(len(items))


def _average_option_pnl(closed_puts: list[ShortPut], closed_calls: list[ShortCall]) -> Decimal:
    items = [*closed_puts, *closed_calls]
    if not items:
        return ZERO
    return sum((item.realized_pnl for item in items), ZERO) / Decimal(len(items))


def _worst_option_pnl(closed_puts: list[ShortPut], closed_calls: list[ShortCall]) -> Decimal:
    items = [*closed_puts, *closed_calls]
    return min((item.realized_pnl for item in items), default=ZERO)


def _monthly_returns(
    closed_puts: list[ShortPut],
    closed_calls: list[ShortCall],
    initial_cash: Decimal,
) -> dict[str, dict[str, float | int]]:
    rows: dict[str, dict[str, Decimal | int]] = defaultdict(lambda: {"trades": 0, "pnl": ZERO})
    for position in [*closed_puts, *closed_calls]:
        month = (position.exit_date or position.candidate.expiration).strftime("%Y-%m")
        rows[month]["trades"] = int(rows[month]["trades"]) + 1
        rows[month]["pnl"] = Decimal(rows[month]["pnl"]) + position.realized_pnl
    return {
        month: {
            "trades": int(values["trades"]),
            "pnl": float(Decimal(values["pnl"])),
            "return_pct": float(Decimal(values["pnl"]) / initial_cash),
        }
        for month, values in sorted(rows.items())
    }


def _count_reasons(items: list[ShortPut] | list[ShortCall]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[item.exit_reason or "unknown"] += 1
    return dict(sorted(counts.items()))


def _pnl_by_symbol(
    closed_puts: list[ShortPut],
    closed_calls: list[ShortCall],
    share_lots: list[ShareLot],
    con: duckdb.DuckDBPyConnection,
    final_date: date,
) -> dict[str, float]:
    values: dict[str, Decimal] = defaultdict(Decimal)
    for put in closed_puts:
        values[put.candidate.symbol] += put.realized_pnl
    for call in closed_calls:
        values[call.candidate.symbol] += call.realized_pnl
    for lot in share_lots:
        mark = _underlying_for(con, lot.symbol, final_date) or _latest_underlying(con, lot.symbol) or lot.basis
        values[lot.symbol] += (mark - lot.basis) * Decimal(lot.shares)
    return {symbol: float(pnl) for symbol, pnl in sorted(values.items())}


def _open_share_positions(
    share_lots: list[ShareLot],
    con: duckdb.DuckDBPyConnection,
    final_date: date,
) -> list[dict[str, float | int | str]]:
    rows: list[dict[str, float | int | str]] = []
    for lot in share_lots:
        mark = _underlying_for(con, lot.symbol, final_date) or _latest_underlying(con, lot.symbol) or lot.basis
        rows.append(
            {
                "symbol": lot.symbol,
                "shares": lot.shares,
                "basis": float(lot.basis),
                "mark": float(mark),
                "market_value": float(mark * Decimal(lot.shares)),
                "unrealized_pnl": float((mark - lot.basis) * Decimal(lot.shares)),
            }
        )
    return rows


def _report(
    *,
    database_path: Path,
    tier: StockQualityTier,
    initial_cash: Decimal,
    result: dict[str, object],
) -> str:
    summary = result["summary"]
    assert isinstance(summary, dict)
    monthly = summary["monthly_returns"]
    assert isinstance(monthly, dict)
    lines = [
        f"# Scanner Wheel Portfolio Test - Tier {tier.value} 2025H1",
        "",
        f"Offline wheel-style simulation from {database_path}.",
        "",
        "## Assumptions",
        "",
        f"- Initial cash: USD {_money(initial_cash)}",
        "- More deployment: max 12 open cash-secured puts, up to 5 contracts per position, target 85% collateral utilization.",
        "- Put filters: scanner Tier A criteria available in the DB.",
        "- Assignment is carried as shares instead of immediately realizing intrinsic loss.",
        "- Covered calls are attempted on assigned shares when 20-35 DTE calls are 5-10% above basis and meet 2% monthly yield.",
        "- Still not modeled: earnings avoidance, technical support/drawdown, sector caps, VIX reserve.",
        "",
        "## Results",
        "",
        f"- Closed put positions: {summary['closed_puts']}",
        f"- Closed put contracts: {summary['closed_put_contracts']}",
        f"- Closed covered-call positions: {summary['closed_calls']}",
        f"- Closed covered-call contracts: {summary['closed_call_contracts']}",
        f"- Assignment events: {summary['assignments']}",
        f"- Assigned contracts: {summary['assigned_contracts']}",
        f"- Open share lots at end: {summary['open_share_lots']} ({summary['open_shares']} shares)",
        f"- Final equity: USD {summary['final_equity']:.2f}",
        f"- Total PnL: USD {summary['total_pnl']:.2f}",
        f"- Realized option PnL: USD {summary['realized_option_pnl']:.2f}",
        f"- Unrealized share PnL: USD {summary['unrealized_share_pnl']:.2f}",
        f"- Return: {summary['return_pct'] * 100:.2f}%",
        f"- Max drawdown: {summary['max_drawdown'] * 100:.2f}% "
        f"(USD {summary['max_drawdown_amount']:.2f}, "
        f"{summary['max_drawdown_start']} to {summary['max_drawdown_end']})",
        f"- Max underwater days: {summary['max_underwater_days']}",
        f"- Best daily return: {summary['best_day_return'] * 100:.2f}%",
        f"- Worst daily return: {summary['worst_day_return'] * 100:.2f}%",
        f"- Daily volatility: {summary['daily_volatility'] * 100:.2f}%",
        f"- Annualized volatility: {summary['annualized_volatility'] * 100:.2f}%",
        f"- Closed option win rate: {summary['closed_option_win_rate'] * 100:.2f}%",
        f"- Average closed option PnL: USD {summary['average_closed_option_pnl']:.2f}",
        f"- Worst closed option PnL: USD {summary['worst_closed_option_pnl']:.2f}",
        f"- Average capital utilization: {summary['average_capital_utilization'] * 100:.2f}%",
        f"- Max capital utilization: {summary['max_capital_utilization'] * 100:.2f}%",
        f"- Average reserved collateral: USD {summary['average_reserved_collateral']:.2f}",
        f"- Max reserved collateral: USD {summary['max_reserved_collateral']:.2f}",
        f"- Skipped candidates: {summary['skipped_count']}",
        "",
        "## Monthly Realized Returns",
        "",
    ]
    for month, values in sorted(monthly.items()):
        assert isinstance(values, dict)
        lines.append(
            f"- {month}: {values['trades']} closes, PnL USD {values['pnl']:.2f}, "
            f"return {values['return_pct'] * 100:.2f}%"
        )
    lines.extend(["", "## Put Exit Reasons", ""])
    for reason, count in summary["put_exit_reasons"].items():
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## Call Exit Reasons", ""])
    for reason, count in summary["call_exit_reasons"].items():
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## PnL By Symbol", ""])
    for symbol, pnl in sorted(summary["pnl_by_symbol"].items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- {symbol}: USD {pnl:.2f}")
    open_shares = summary["open_share_positions"]
    assert isinstance(open_shares, list)
    if open_shares:
        lines.extend(["", "## Open Shares Marked To Market", ""])
        for row in open_shares:
            assert isinstance(row, dict)
            lines.append(
                f"- {row['symbol']}: {row['shares']} shares, basis USD {row['basis']:.2f}, "
                f"mark USD {row['mark']:.2f}, market value USD {row['market_value']:.2f}, "
                f"unrealized PnL USD {row['unrealized_pnl']:.2f}"
            )
    return "\n".join(lines) + "\n"


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


if __name__ == "__main__":
    main()
