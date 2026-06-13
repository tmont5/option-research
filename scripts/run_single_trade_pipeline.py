#!/usr/bin/env python
"""Run one live ThetaData trade from selection through expiration."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path

from options_quant.data.models import OptionType
from options_quant.pipelines import SingleTradePipelineConfig, run_single_trade_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--entry-date", type=date.fromisoformat, default=date(2025, 1, 3))
    parser.add_argument("--target-dte", type=int, default=45)
    parser.add_argument("--target-delta", type=Decimal, default=Decimal("-0.10"))
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal("0.65"))
    parser.add_argument("--slippage-per-contract", type=Decimal, default=Decimal("0.00"))
    parser.add_argument("--theta-mdds-host")
    parser.add_argument("--theta-mdds-port")
    parser.add_argument("--theta-mdds-type")
    parser.add_argument("--report-path", type=Path, default=Path("runs/single_trade/report.md"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = run_single_trade_pipeline(
        SingleTradePipelineConfig(
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
    selected = result.selected_candidate
    print(f"Report: {result.config.report_path}", flush=True)
    print(
        "Selected: "
        f"{selected.contract.underlying_symbol} {selected.contract.expiration} "
        f"{selected.contract.strike} {selected.contract.option_type.value} "
        f"dte={selected.dte} delta={selected.delta} iv={selected.implied_volatility}",
        flush=True,
    )
    print(
        "PnL: " f"realized={result.audit.realized_pnl} final_equity={result.audit.final_equity}",
        flush=True,
    )


if __name__ == "__main__":
    main()
