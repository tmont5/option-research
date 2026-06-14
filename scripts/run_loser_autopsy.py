#!/usr/bin/env python
"""Run a focused autopsy for the six-month validation loser."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path

from options_quant.data.models import OptionType
from options_quant.pipelines import LoserAutopsyConfig, run_loser_autopsy_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--entry-date", type=date.fromisoformat, default=date(2025, 2, 21))
    parser.add_argument("--target-dte", type=int, default=45)
    parser.add_argument("--target-delta", type=Decimal, default=Decimal("-0.10"))
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal("0.65"))
    parser.add_argument("--slippage-per-contract", type=Decimal, default=Decimal("0.00"))
    parser.add_argument("--theta-mdds-host")
    parser.add_argument("--theta-mdds-port")
    parser.add_argument("--theta-mdds-type")
    parser.add_argument("--report-path", type=Path, default=Path("runs/loser_autopsy/report.md"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = run_loser_autopsy_pipeline(
        LoserAutopsyConfig(
            symbol=args.symbol,
            entry_date=args.entry_date,
            target_dte=args.target_dte,
            target_delta=args.target_delta,
            option_type=OptionType.PUT,
            quantity=args.quantity,
            commission_per_contract=args.commission_per_contract,
            slippage_per_contract=args.slippage_per_contract,
            theta_mdds_host=args.theta_mdds_host,
            theta_mdds_port=args.theta_mdds_port,
            theta_mdds_type=args.theta_mdds_type,
            report_path=args.report_path,
            verbose=args.verbose,
        )
    )
    selected = result.trade.selected_candidate
    print(f"Report: {result.config.report_path}", flush=True)
    print(
        "Trade: "
        f"{selected.contract.underlying_symbol} {selected.contract.expiration} "
        f"{selected.contract.strike}{selected.contract.option_type.value[0].upper()} "
        f"entry={result.trade.audit.entry_price} pnl={result.trade.audit.realized_pnl}",
        flush=True,
    )
    for trigger in result.stop_triggers:
        print(
            "Stop: "
            f"{trigger.multiple}x date={trigger.observed_date} "
            f"mark={trigger.option_mark} pnl={trigger.unrealized_pnl}",
            flush=True,
        )


if __name__ == "__main__":
    main()
