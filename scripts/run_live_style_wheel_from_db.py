#!/usr/bin/env python
"""Run a live-style scanner wheel simulation from DuckDB."""
# ruff: noqa: E402,I001,E501

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_scanner_wheel_portfolio_from_db as wheel  # noqa: E402
from options_quant.strategies.scanner_put import ScannerStylePutStrategyConfig, StockQualityTier

MULTIPLIER = Decimal("100")
ZERO = Decimal("0")


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
    parser.add_argument("--target-capital-utilization", type=Decimal, default=Decimal("0.75"))
    parser.add_argument("--max-capital-utilization", type=Decimal, default=Decimal("1.00"))
    parser.add_argument("--max-total-positions", type=int, default=10)
    parser.add_argument("--max-contracts-per-ticker", type=int, default=3)
    parser.add_argument("--max-candidates-per-day", type=int, default=50)
    parser.add_argument("--put-max-delta", type=Decimal, default=Decimal("0.25"))
    parser.add_argument("--put-min-monthly-yield", type=Decimal, default=Decimal("0.025"))
    parser.add_argument("--call-min-monthly-yield", type=Decimal, default=Decimal("0.02"))
    parser.add_argument("--min-dte", type=int, default=30)
    parser.add_argument("--max-dte", type=int, default=35)
    parser.add_argument("--winner-early-days", type=int, default=14)
    parser.add_argument(
        "--challenged-policy",
        choices=["assign", "roll"],
        default="assign",
        help="How to handle ITM puts at expiration when an eligible replacement exists.",
    )
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--summary-path", type=Path, required=True)
    args = parser.parse_args()

    config = ScannerStylePutStrategyConfig()
    tiers = _selected_tiers(args)
    symbols = _symbols_for_tiers(config, tiers)
    con = duckdb.connect(str(args.database_path), read_only=True)
    try:
        result = run_live_style(
            con=con,
            tiers=tiers,
            symbols=symbols,
            start_date=args.start_date,
            end_date=args.end_date,
            initial_cash=args.initial_cash,
            commission=args.commission_per_contract,
            target_capital_utilization=args.target_capital_utilization,
            max_capital_utilization=args.max_capital_utilization,
            max_total_positions=args.max_total_positions,
            max_contracts_per_ticker=args.max_contracts_per_ticker,
            max_candidates_per_day=args.max_candidates_per_day,
            put_max_delta=args.put_max_delta,
            put_min_monthly_yield=args.put_min_monthly_yield,
            call_min_monthly_yield=args.call_min_monthly_yield,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            winner_early_days=args.winner_early_days,
            challenged_policy=args.challenged_policy,
            min_bid=config.put_entry.liquidity.min_bid,
            min_open_interest=config.put_entry.liquidity.min_open_interest,
            max_spread_pct=config.put_entry.liquidity.max_bid_ask_spread_pct,
            call_min_strike_above_basis=config.covered_call.min_strike_above_breakeven_pct,
            call_max_strike_above_basis=config.covered_call.max_strike_above_breakeven_pct,
        )
    finally:
        con.close()

    args.summary_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.summary_path.write_text(json.dumps(result["summary"], indent=2) + "\n", encoding="utf-8")
    args.report_path.write_text(
        _report(
            database_path=args.database_path,
            tiers=tiers,
            symbols=symbols,
            challenged_policy=args.challenged_policy,
            result=result,
        ),
        encoding="utf-8",
    )
    summary = result["summary"]
    print(f"Report: {args.report_path}", flush=True)
    print(f"Summary JSON: {args.summary_path}", flush=True)
    print(
        "Live-style wheel: "
        f"return={summary['return_pct']:.4f} "
        f"realized_premium={summary['realized_option_pnl']:.2f} "
        f"final_equity={summary['final_equity']:.2f} "
        f"avg_util={summary['average_capital_utilization']:.4f} "
        f"max_dd={summary['max_drawdown']:.4f} "
        f"assignments={summary['assignments']} "
        f"rolls={summary['rolls']}",
        flush=True,
    )


def run_live_style(
    *,
    con: duckdb.DuckDBPyConnection,
    tiers: tuple[StockQualityTier, ...],
    symbols: tuple[str, ...],
    start_date: date,
    end_date: date,
    initial_cash: Decimal,
    commission: Decimal,
    target_capital_utilization: Decimal,
    max_capital_utilization: Decimal,
    max_total_positions: int,
    max_contracts_per_ticker: int,
    max_candidates_per_day: int,
    put_max_delta: Decimal,
    put_min_monthly_yield: Decimal,
    call_min_monthly_yield: Decimal,
    min_dte: int,
    max_dte: int,
    winner_early_days: int,
    challenged_policy: str,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
    call_min_strike_above_basis: Decimal,
    call_max_strike_above_basis: Decimal,
) -> dict[str, Any]:
    cash = initial_cash
    open_puts: list[wheel.ShortPut] = []
    closed_puts: list[wheel.ShortPut] = []
    open_calls: list[wheel.ShortCall] = []
    closed_calls: list[wheel.ShortCall] = []
    share_lots: list[wheel.ShareLot] = []
    roll_events: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    equity_curve: list[dict[str, float | str]] = []
    utilization_samples: list[Decimal] = []

    trading_dates = _observed_dates(con, start_date, end_date)
    for current in trading_dates:
        cash = _process_live_puts(
            con=con,
            open_puts=open_puts,
            closed_puts=closed_puts,
            share_lots=share_lots,
            roll_events=roll_events,
            current=current,
            cash=cash,
            commission=commission,
            challenged_policy=challenged_policy,
            min_dte=min_dte,
            max_dte=max_dte,
            put_max_delta=put_max_delta,
            put_min_monthly_yield=put_min_monthly_yield,
            min_bid=min_bid,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
            winner_early_days=winner_early_days,
            max_contracts_per_ticker=max_contracts_per_ticker,
        )
        cash = _process_live_calls(
            con=con,
            open_calls=open_calls,
            closed_calls=closed_calls,
            share_lots=share_lots,
            current=current,
            cash=cash,
            commission=commission,
            winner_early_days=winner_early_days,
        )
        cash = _open_covered_calls(
            con=con,
            share_lots=share_lots,
            open_calls=open_calls,
            current=current,
            cash=cash,
            commission=commission,
            max_total_positions=max_total_positions,
            call_min_monthly_yield=call_min_monthly_yield,
            min_dte=min_dte,
            max_dte=max_dte,
            min_bid=min_bid,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
            call_min_strike_above_basis=call_min_strike_above_basis,
            call_max_strike_above_basis=call_max_strike_above_basis,
        )
        cash = _open_live_puts(
            con=con,
            symbols=symbols,
            open_puts=open_puts,
            open_calls=open_calls,
            share_lots=share_lots,
            skipped=skipped,
            current=current,
            cash=cash,
            initial_cash=initial_cash,
            commission=commission,
            target_capital_utilization=target_capital_utilization,
            max_capital_utilization=max_capital_utilization,
            max_total_positions=max_total_positions,
            max_contracts_per_ticker=max_contracts_per_ticker,
            max_candidates_per_day=max_candidates_per_day,
            min_dte=min_dte,
            max_dte=max_dte,
            put_max_delta=put_max_delta,
            put_min_monthly_yield=put_min_monthly_yield,
            min_bid=min_bid,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
        )

        equity_row = _equity_row(con, current, cash, open_puts, open_calls, share_lots)
        equity_curve.append(equity_row)
        utilization_samples.append(
            _gross_exposure(con, current, open_puts, share_lots) / initial_cash
        )

    final_date = trading_dates[-1] if trading_dates else end_date
    cash = _liquidate_open_options(
        con=con,
        open_puts=open_puts,
        closed_puts=closed_puts,
        open_calls=open_calls,
        closed_calls=closed_calls,
        final_date=final_date,
        cash=cash,
        commission=commission,
    )
    final_share_value = sum(
        (_underlying_on_or_before(con, lot.symbol, final_date) or lot.basis) * Decimal(lot.shares)
        for lot in share_lots
    )
    final_equity = cash + final_share_value
    if equity_curve:
        equity_curve[-1] = {
            **equity_curve[-1],
            "equity": float(final_equity),
            "share_value": float(final_share_value),
        }
    risk = wheel._risk_metrics(equity_curve, initial_cash)
    monthly_realized = _monthly_realized(closed_puts, closed_calls, initial_cash)
    monthly_equity = _monthly_equity_returns(equity_curve)
    realized_option_pnl = sum((put.realized_pnl for put in closed_puts), ZERO) + sum(
        (call.realized_pnl for call in closed_calls),
        ZERO,
    )
    unrealized_share_pnl = sum(
        (
            (_underlying_on_or_before(con, lot.symbol, final_date) or lot.basis) - lot.basis
        )
        * Decimal(lot.shares)
        for lot in share_lots
    )
    total_pnl = final_equity - initial_cash
    summary = {
        "initial_cash": float(initial_cash),
        "tiers": [tier.value for tier in tiers],
        "symbols": list(symbols),
        "symbol_count": len(symbols),
        "max_candidates_per_day": max_candidates_per_day,
        "cash": float(cash),
        "final_share_value": float(final_share_value),
        "final_equity": float(final_equity),
        "total_pnl": float(total_pnl),
        "return_pct": float(total_pnl / initial_cash),
        "realized_option_pnl": float(realized_option_pnl),
        "unrealized_share_pnl": float(unrealized_share_pnl),
        "closed_puts": len(closed_puts),
        "closed_put_contracts": sum(put.quantity for put in closed_puts),
        "closed_calls": len(closed_calls),
        "closed_call_contracts": sum(call.quantity for call in closed_calls),
        "assignments": sum(1 for put in closed_puts if put.exit_reason == "assigned"),
        "assigned_contracts": sum(
            put.quantity for put in closed_puts if put.exit_reason == "assigned"
        ),
        "rolls": len(roll_events),
        "open_share_lots": len(share_lots),
        "open_shares": sum(lot.shares for lot in share_lots),
        "average_capital_utilization": float(_average(utilization_samples)),
        "max_capital_utilization": float(max(utilization_samples, default=ZERO)),
        "max_drawdown": float(risk["max_drawdown"]),
        "max_drawdown_amount": float(risk["max_drawdown_amount"]),
        "max_drawdown_start": risk["max_drawdown_start"],
        "max_drawdown_end": risk["max_drawdown_end"],
        "max_underwater_days": risk["max_underwater_days"],
        "best_day_return": float(risk["best_day_return"]),
        "worst_day_return": float(risk["worst_day_return"]),
        "daily_volatility": float(risk["daily_volatility"]),
        "annualized_volatility": float(risk["annualized_volatility"]),
        "monthly_realized_returns": monthly_realized,
        "monthly_equity_returns": monthly_equity,
        "put_exit_reasons": wheel._count_reasons(closed_puts),
        "call_exit_reasons": wheel._count_reasons(closed_calls),
        "pnl_by_symbol": wheel._pnl_by_symbol(closed_puts, closed_calls, share_lots, con, final_date),
        "roll_events": roll_events,
        "skipped_count": len(skipped),
    }
    return {
        "summary": summary,
        "equity_curve": equity_curve,
        "closed_puts": closed_puts,
        "closed_calls": closed_calls,
        "share_lots": share_lots,
        "roll_events": roll_events,
        "skipped": skipped,
    }


def _process_live_puts(
    *,
    con: duckdb.DuckDBPyConnection,
    open_puts: list[wheel.ShortPut],
    closed_puts: list[wheel.ShortPut],
    share_lots: list[wheel.ShareLot],
    roll_events: list[dict[str, Any]],
    current: date,
    cash: Decimal,
    commission: Decimal,
    challenged_policy: str,
    min_dte: int,
    max_dte: int,
    put_max_delta: Decimal,
    put_min_monthly_yield: Decimal,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
    winner_early_days: int,
    max_contracts_per_ticker: int,
) -> Decimal:
    for put in list(open_puts):
        candidate = put.candidate
        if current <= candidate.entry_date:
            continue
        mark, ask = wheel._quote_for(con, candidate, current)
        days_open = (current - candidate.entry_date).days
        if mark is not None and ask is not None and candidate.bid > ZERO:
            capture = (candidate.bid - mark) / candidate.bid
            if days_open < winner_early_days and capture >= Decimal("0.50"):
                wheel._close_short_option(put, current, ask, commission, "50% capture before 2w")
            elif days_open >= winner_early_days and capture >= Decimal("0.90"):
                wheel._close_short_option(put, current, ask, commission, "90% capture after 2w")
        if put.exit_date is None and current >= candidate.expiration:
            underlying = wheel._underlying_for(con, candidate.symbol, candidate.expiration)
            underlying = underlying or _underlying_on_or_before(con, candidate.symbol, current)
            if underlying is not None and underlying < candidate.strike:
                rolled = False
                if challenged_policy == "roll":
                    rolled, cash = _try_roll_put(
                        con=con,
                        put=put,
                        open_puts=open_puts,
                        closed_puts=closed_puts,
                        roll_events=roll_events,
                        current=current,
                        cash=cash,
                        commission=commission,
                        min_dte=min_dte,
                        max_dte=max_dte,
                        put_max_delta=put_max_delta,
                        put_min_monthly_yield=put_min_monthly_yield,
                        min_bid=min_bid,
                        min_open_interest=min_open_interest,
                        max_spread_pct=max_spread_pct,
                        max_contracts_per_ticker=max_contracts_per_ticker,
                    )
                    if rolled:
                        continue
                if not rolled:
                    put.exit_date = candidate.expiration
                    put.exit_reason = "assigned"
                    put.realized_pnl = put.entry_credit
                    cash -= candidate.strike * MULTIPLIER * Decimal(put.quantity)
                    credit_per_share = put.entry_credit / (MULTIPLIER * Decimal(put.quantity))
                    share_lots.append(
                        wheel.ShareLot(
                            symbol=candidate.symbol,
                            shares=put.quantity * 100,
                            basis=candidate.strike - credit_per_share,
                            opened_at=put.exit_date,
                        )
                    )
            else:
                put.exit_date = candidate.expiration
                put.exit_reason = "expired OTM"
                put.realized_pnl = put.entry_credit
        if put.exit_date is not None and put in open_puts:
            if put.exit_reason not in {"assigned", "rolled"}:
                cash -= put.exit_value
            open_puts.remove(put)
            closed_puts.append(put)
    return cash


def _try_roll_put(
    *,
    con: duckdb.DuckDBPyConnection,
    put: wheel.ShortPut,
    open_puts: list[wheel.ShortPut],
    closed_puts: list[wheel.ShortPut],
    roll_events: list[dict[str, Any]],
    current: date,
    cash: Decimal,
    commission: Decimal,
    min_dte: int,
    max_dte: int,
    put_max_delta: Decimal,
    put_min_monthly_yield: Decimal,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
    max_contracts_per_ticker: int,
) -> tuple[bool, Decimal]:
    replacement = _best_put_candidate(
        con=con,
        symbols=(put.candidate.symbol,),
        current=current,
        min_dte=min_dte,
        max_dte=max_dte,
        put_max_delta=put_max_delta,
        put_min_monthly_yield=put_min_monthly_yield,
        min_bid=min_bid,
        min_open_interest=min_open_interest,
        max_spread_pct=max_spread_pct,
    )
    if replacement is None:
        return False, cash
    quantity = min(put.quantity, max_contracts_per_ticker)
    old_ask = wheel._quote_for(con, put.candidate, current)[1]
    underlying = _underlying_on_or_before(con, put.candidate.symbol, current) or put.candidate.strike
    intrinsic = max(put.candidate.strike - underlying, ZERO)
    exit_price = old_ask if old_ask is not None and old_ask > ZERO else intrinsic
    put.exit_date = current
    put.exit_value = exit_price * MULTIPLIER * Decimal(put.quantity) + commission * Decimal(put.quantity)
    put.realized_pnl = put.entry_credit - put.exit_value
    put.exit_reason = "rolled"
    cash -= put.exit_value
    entry_credit = replacement.bid * MULTIPLIER * Decimal(quantity) - commission * Decimal(quantity)
    cash += entry_credit
    if put in open_puts:
        open_puts.remove(put)
    closed_puts.append(put)
    open_puts.append(
        wheel.ShortPut(
            candidate=replacement,
            quantity=quantity,
            entry_credit=entry_credit,
            collateral=replacement.strike * MULTIPLIER * Decimal(quantity),
        )
    )
    roll_events.append(
        {
            "date": current.isoformat(),
            "symbol": replacement.symbol,
            "new_expiration": replacement.expiration.isoformat(),
            "new_strike": float(replacement.strike),
            "quantity": quantity,
        }
    )
    return True, cash


def _process_live_calls(
    *,
    con: duckdb.DuckDBPyConnection,
    open_calls: list[wheel.ShortCall],
    closed_calls: list[wheel.ShortCall],
    share_lots: list[wheel.ShareLot],
    current: date,
    cash: Decimal,
    commission: Decimal,
    winner_early_days: int,
) -> Decimal:
    for call in list(open_calls):
        candidate = call.candidate
        if current <= candidate.entry_date:
            continue
        mark, ask = wheel._quote_for(con, candidate, current)
        days_open = (current - candidate.entry_date).days
        if mark is not None and ask is not None and candidate.bid > ZERO:
            capture = (candidate.bid - mark) / candidate.bid
            if days_open < winner_early_days and capture >= Decimal("0.50"):
                wheel._close_short_call(call, current, ask, commission, "50% call capture before 2w")
            elif days_open >= winner_early_days and capture >= Decimal("0.90"):
                wheel._close_short_call(call, current, ask, commission, "90% call capture after 2w")
        if call.exit_date is None and current >= candidate.expiration:
            underlying = wheel._underlying_for(con, candidate.symbol, candidate.expiration)
            underlying = underlying or _underlying_on_or_before(con, candidate.symbol, current)
            if underlying is not None and underlying > candidate.strike:
                share_pnl = (candidate.strike - call.lot.basis) * Decimal(call.lot.shares)
                call.exit_date = candidate.expiration
                call.exit_reason = "called away"
                call.realized_pnl = call.entry_credit + share_pnl
                cash += candidate.strike * MULTIPLIER * Decimal(call.quantity)
                if call.lot in share_lots:
                    share_lots.remove(call.lot)
            else:
                call.exit_date = candidate.expiration
                call.exit_reason = "call expired OTM"
                call.realized_pnl = call.entry_credit
        if call.exit_date is not None:
            if call.exit_reason not in {"called away", "call expired OTM"}:
                cash -= call.exit_value
            open_calls.remove(call)
            closed_calls.append(call)
    return cash


def _open_covered_calls(
    *,
    con: duckdb.DuckDBPyConnection,
    share_lots: list[wheel.ShareLot],
    open_calls: list[wheel.ShortCall],
    current: date,
    cash: Decimal,
    commission: Decimal,
    max_total_positions: int,
    call_min_monthly_yield: Decimal,
    min_dte: int,
    max_dte: int,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
    call_min_strike_above_basis: Decimal,
    call_max_strike_above_basis: Decimal,
) -> Decimal:
    for lot in list(share_lots):
        if len(open_calls) >= max_total_positions:
            break
        if any(call.lot is lot for call in open_calls):
            continue
        candidates = wheel._option_candidates(
            con=con,
            symbols=(lot.symbol,),
            entry_date=current,
            option_type="call",
            min_dte=min_dte,
            max_dte=max_dte,
            min_bid=min_bid,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
        )
        lower = lot.basis * (Decimal("1") + call_min_strike_above_basis)
        upper = lot.basis * (Decimal("1") + call_max_strike_above_basis)
        eligible = [
            candidate
            for candidate in candidates
            if lower <= candidate.strike <= upper
            and candidate.monthly_yield >= call_min_monthly_yield
        ]
        if not eligible:
            continue
        eligible.sort(key=lambda item: (item.monthly_yield, item.strike), reverse=True)
        candidate = eligible[0]
        quantity = lot.shares // 100
        entry_credit = candidate.bid * MULTIPLIER * Decimal(quantity) - commission * Decimal(quantity)
        cash += entry_credit
        open_calls.append(
            wheel.ShortCall(candidate=candidate, lot=lot, quantity=quantity, entry_credit=entry_credit)
        )
    return cash


def _open_live_puts(
    *,
    con: duckdb.DuckDBPyConnection,
    symbols: tuple[str, ...],
    open_puts: list[wheel.ShortPut],
    open_calls: list[wheel.ShortCall],
    share_lots: list[wheel.ShareLot],
    skipped: list[dict[str, Any]],
    current: date,
    cash: Decimal,
    initial_cash: Decimal,
    commission: Decimal,
    target_capital_utilization: Decimal,
    max_capital_utilization: Decimal,
    max_total_positions: int,
    max_contracts_per_ticker: int,
    max_candidates_per_day: int,
    min_dte: int,
    max_dte: int,
    put_max_delta: Decimal,
    put_min_monthly_yield: Decimal,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
) -> Decimal:
    candidates = wheel._put_candidates(
        con=con,
        symbols=symbols,
        entry_date=current,
        min_dte=min_dte,
        max_dte=max_dte,
        max_delta=put_max_delta,
        min_monthly_yield=put_min_monthly_yield,
        min_bid=min_bid,
        min_open_interest=min_open_interest,
        max_spread_pct=max_spread_pct,
        max_candidates=max_candidates_per_day,
    )
    for candidate in candidates:
        if _position_count(open_puts, open_calls, share_lots) >= max_total_positions:
            break
        if _has_symbol_exposure(candidate.symbol, open_puts, open_calls, share_lots):
            skipped.append({"date": current.isoformat(), "symbol": candidate.symbol, "reason": "symbol exposure"})
            continue
        current_exposure = _gross_exposure(con, current, open_puts, share_lots)
        if current_exposure >= initial_cash * target_capital_utilization:
            break
        per_contract_collateral = candidate.strike * MULTIPLIER
        max_exposure_room = initial_cash * max_capital_utilization - current_exposure
        target_room = initial_cash * target_capital_utilization - current_exposure
        available = min(cash, max_exposure_room, target_room)
        quantity = min(int(available // per_contract_collateral), max_contracts_per_ticker)
        if quantity <= 0:
            skipped.append({"date": current.isoformat(), "symbol": candidate.symbol, "reason": "no collateral room"})
            continue
        entry_credit = candidate.bid * MULTIPLIER * Decimal(quantity) - commission * Decimal(quantity)
        cash += entry_credit
        open_puts.append(
            wheel.ShortPut(
                candidate=candidate,
                quantity=quantity,
                entry_credit=entry_credit,
                collateral=per_contract_collateral * Decimal(quantity),
            )
        )
    return cash


def _best_put_candidate(
    *,
    con: duckdb.DuckDBPyConnection,
    symbols: tuple[str, ...],
    current: date,
    min_dte: int,
    max_dte: int,
    put_max_delta: Decimal,
    put_min_monthly_yield: Decimal,
    min_bid: Decimal,
    min_open_interest: int,
    max_spread_pct: Decimal,
) -> wheel.OptionCandidate | None:
    candidates = wheel._put_candidates(
        con=con,
        symbols=symbols,
        entry_date=current,
        min_dte=min_dte,
        max_dte=max_dte,
        max_delta=put_max_delta,
        min_monthly_yield=put_min_monthly_yield,
        min_bid=min_bid,
        min_open_interest=min_open_interest,
        max_spread_pct=max_spread_pct,
        max_candidates=1,
    )
    return candidates[0] if candidates else None


def _liquidate_open_options(
    *,
    con: duckdb.DuckDBPyConnection,
    open_puts: list[wheel.ShortPut],
    closed_puts: list[wheel.ShortPut],
    open_calls: list[wheel.ShortCall],
    closed_calls: list[wheel.ShortCall],
    final_date: date,
    cash: Decimal,
    commission: Decimal,
) -> Decimal:
    for put in list(open_puts):
        _, ask = wheel._quote_for(con, put.candidate, final_date)
        if ask is None:
            underlying = _underlying_on_or_before(con, put.candidate.symbol, final_date) or put.candidate.strike
            ask = max(put.candidate.strike - underlying, ZERO)
        wheel._close_short_option(put, final_date, ask, commission, "liquidated at final mark")
        cash -= put.exit_value
        open_puts.remove(put)
        closed_puts.append(put)
    for call in list(open_calls):
        _, ask = wheel._quote_for(con, call.candidate, final_date)
        if ask is None:
            underlying = _underlying_on_or_before(con, call.candidate.symbol, final_date) or call.candidate.strike
            ask = max(underlying - call.candidate.strike, ZERO)
        wheel._close_short_call(call, final_date, ask, commission, "liquidated at final mark")
        cash -= call.exit_value
        open_calls.remove(call)
        closed_calls.append(call)
    return cash


def _observed_dates(con: duckdb.DuckDBPyConnection, start: date, end: date) -> list[date]:
    return [
        row[0]
        for row in con.execute(
            """
            SELECT DISTINCT observed_date
            FROM option_quotes
            WHERE observed_date BETWEEN ? AND ?
            ORDER BY observed_date
            """,
            [start, end],
        ).fetchall()
    ]


def _equity_row(
    con: duckdb.DuckDBPyConnection,
    current: date,
    cash: Decimal,
    open_puts: list[wheel.ShortPut],
    open_calls: list[wheel.ShortCall],
    share_lots: list[wheel.ShareLot],
) -> dict[str, float | str]:
    put_liability = sum((_option_mark_value(con, put, current) for put in open_puts), ZERO)
    call_liability = sum((_call_mark_value(con, call, current) for call in open_calls), ZERO)
    share_value = sum(
        (_underlying_on_or_before(con, lot.symbol, current) or lot.basis) * Decimal(lot.shares)
        for lot in share_lots
    )
    equity = cash + share_value - put_liability - call_liability
    return {
        "date": current.isoformat(),
        "equity": float(equity),
        "cash": float(cash),
        "share_value": float(share_value),
        "put_liability": float(put_liability),
        "call_liability": float(call_liability),
    }


def _option_mark_value(
    con: duckdb.DuckDBPyConnection,
    put: wheel.ShortPut,
    current: date,
) -> Decimal:
    mark, _ = wheel._quote_for(con, put.candidate, current)
    if mark is None:
        underlying = _underlying_on_or_before(con, put.candidate.symbol, current) or put.candidate.strike
        mark = max(put.candidate.strike - underlying, ZERO)
    return mark * MULTIPLIER * Decimal(put.quantity)


def _call_mark_value(
    con: duckdb.DuckDBPyConnection,
    call: wheel.ShortCall,
    current: date,
) -> Decimal:
    mark, _ = wheel._quote_for(con, call.candidate, current)
    if mark is None:
        underlying = _underlying_on_or_before(con, call.candidate.symbol, current) or call.candidate.strike
        mark = max(underlying - call.candidate.strike, ZERO)
    return mark * MULTIPLIER * Decimal(call.quantity)


def _gross_exposure(
    con: duckdb.DuckDBPyConnection,
    current: date,
    open_puts: list[wheel.ShortPut],
    share_lots: list[wheel.ShareLot],
) -> Decimal:
    put_collateral = sum((put.collateral for put in open_puts), ZERO)
    stock_exposure = sum(
        (_underlying_on_or_before(con, lot.symbol, current) or lot.basis) * Decimal(lot.shares)
        for lot in share_lots
    )
    return put_collateral + stock_exposure


def _underlying_on_or_before(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    observed: date,
) -> Decimal | None:
    row = con.execute(
        """
        SELECT CAST(price AS DOUBLE)
        FROM underlying_prices
        WHERE symbol = ? AND observed_date <= ?
        ORDER BY observed_date DESC
        LIMIT 1
        """,
        [symbol, observed],
    ).fetchone()
    return None if row is None or row[0] is None else Decimal(str(row[0]))


def _position_count(
    open_puts: list[wheel.ShortPut],
    open_calls: list[wheel.ShortCall],
    share_lots: list[wheel.ShareLot],
) -> int:
    uncovered_lots = [
        lot for lot in share_lots if not any(call.lot is lot for call in open_calls)
    ]
    return len(open_puts) + len(open_calls) + len(uncovered_lots)


def _has_symbol_exposure(
    symbol: str,
    open_puts: list[wheel.ShortPut],
    open_calls: list[wheel.ShortCall],
    share_lots: list[wheel.ShareLot],
) -> bool:
    return (
        any(put.candidate.symbol == symbol for put in open_puts)
        or any(call.candidate.symbol == symbol for call in open_calls)
        or any(lot.symbol == symbol for lot in share_lots)
    )


def _monthly_realized(
    closed_puts: list[wheel.ShortPut],
    closed_calls: list[wheel.ShortCall],
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


def _monthly_equity_returns(
    equity_curve: list[dict[str, float | str]],
) -> dict[str, dict[str, float]]:
    by_month: dict[str, list[dict[str, float | str]]] = defaultdict(list)
    for row in equity_curve:
        by_month[str(row["date"])[:7]].append(row)
    out: dict[str, dict[str, float]] = {}
    previous_equity: Decimal | None = None
    for month, rows in sorted(by_month.items()):
        end_equity = Decimal(str(rows[-1]["equity"]))
        start_equity = previous_equity or Decimal(str(rows[0]["equity"]))
        out[month] = {
            "start_equity": float(start_equity),
            "end_equity": float(end_equity),
            "return_pct": float((end_equity - start_equity) / start_equity)
            if start_equity
            else 0.0,
        }
        previous_equity = end_equity
    return out


def _average(values: list[Decimal]) -> Decimal:
    if not values:
        return ZERO
    return sum(values, ZERO) / Decimal(len(values))


def _selected_tiers(args: argparse.Namespace) -> tuple[StockQualityTier, ...]:
    values = args.tier or [StockQualityTier.A.value]
    tiers = tuple(StockQualityTier(value) for value in values)
    return tuple(dict.fromkeys(tiers))


def _symbols_for_tiers(
    config: ScannerStylePutStrategyConfig,
    tiers: tuple[StockQualityTier, ...],
) -> tuple[str, ...]:
    symbols = [symbol for tier in tiers for symbol in config.symbols_for_tier(tier)]
    return tuple(dict.fromkeys(symbols))


def _report(
    *,
    database_path: Path,
    tiers: tuple[StockQualityTier, ...],
    symbols: tuple[str, ...],
    challenged_policy: str,
    result: dict[str, Any],
) -> str:
    summary = result["summary"]
    tier_label = "+".join(tier.value for tier in tiers)
    lines = [
        f"# Live-Style Wheel Simulation - Tier {tier_label}",
        "",
        f"- Database: {database_path}",
        f"- Universe: {len(symbols)} symbols from tier(s) {tier_label}.",
        f"- Symbols: {', '.join(symbols)}",
        f"- Challenged-put policy: {challenged_policy}",
        "- Entry cadence: any observed trading day with eligible candidates",
        "- Defaults: 30-35 DTE, put delta <= 0.25, put monthly yield >= 2.5%, max 3 contracts per ticker.",
        f"- Candidate scan cap: top {summary['max_candidates_per_day']} candidates per day.",
        "",
        "## Results",
        "",
        f"- Final equity: USD {summary['final_equity']:.2f}",
        f"- Total PnL: USD {summary['total_pnl']:.2f}",
        f"- Return: {summary['return_pct'] * 100:.2f}%",
        f"- Realized option PnL: USD {summary['realized_option_pnl']:.2f}",
        f"- Unrealized share PnL: USD {summary['unrealized_share_pnl']:.2f}",
        f"- Average capital utilization: {summary['average_capital_utilization'] * 100:.2f}%",
        f"- Max capital utilization: {summary['max_capital_utilization'] * 100:.2f}%",
        f"- Max drawdown: {summary['max_drawdown'] * 100:.2f}% "
        f"(USD {summary['max_drawdown_amount']:.2f}, "
        f"{summary['max_drawdown_start']} to {summary['max_drawdown_end']})",
        f"- Closed put positions/contracts: {summary['closed_puts']} / {summary['closed_put_contracts']}",
        f"- Closed call positions/contracts: {summary['closed_calls']} / {summary['closed_call_contracts']}",
        f"- Assignments: {summary['assignments']} ({summary['assigned_contracts']} contracts)",
        f"- Rolls: {summary['rolls']}",
        f"- Open shares at end: {summary['open_share_lots']} lots / {summary['open_shares']} shares",
        "",
        "## Monthly Realized Option Returns",
        "",
    ]
    for month, values in summary["monthly_realized_returns"].items():
        lines.append(
            f"- {month}: {values['trades']} closes, PnL USD {values['pnl']:.2f}, "
            f"return {values['return_pct'] * 100:.2f}%"
        )
    lines.extend(["", "## Monthly Equity Returns", ""])
    for month, values in summary["monthly_equity_returns"].items():
        lines.append(
            f"- {month}: start USD {values['start_equity']:.2f}, "
            f"end USD {values['end_equity']:.2f}, return {values['return_pct'] * 100:.2f}%"
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
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
