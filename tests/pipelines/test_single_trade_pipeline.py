from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from options_quant.data.models import OptionContract, OptionQuote
from options_quant.pipelines import SingleTradePipelineConfig, run_single_trade_pipeline


class FakeEndpoints:
    def list_expirations(self, *, symbol: str) -> list[dict[str, object]]:
        assert symbol == "SPY"
        return [
            {"symbol": symbol, "expiration": date(2025, 2, 14)},
            {"symbol": symbol, "expiration": date(2025, 2, 21)},
        ]

    def list_strikes(self, *, symbol: str, expiration: date | str) -> list[dict[str, object]]:
        assert symbol == "SPY"
        assert expiration == date(2025, 2, 14)
        return [
            {"symbol": symbol, "strike": Decimal("520")},
            {"symbol": symbol, "strike": Decimal("530")},
        ]

    def history_greeks_first_order(self, **params: object) -> list[dict[str, object]]:
        strike_param = str(params["strike"])
        start_date = params["start_date"]
        end_date = params["end_date"]
        if strike_param == "*":
            return [
                {
                    "symbol": "SPY",
                    "expiration": date(2025, 2, 14),
                    "strike": Decimal("530"),
                    "right": "PUT",
                    "timestamp": datetime(2025, 1, 3, 21, tzinfo=UTC),
                    "delta": Decimal("-0.11"),
                    "theta": Decimal("-0.04"),
                    "vega": Decimal("0.22"),
                    "rho": Decimal("-0.07"),
                    "implied_vol": Decimal("0.18"),
                    "underlying_price": Decimal("590"),
                }
            ]
        strike = Decimal(strike_param)
        if strike == Decimal("520"):
            raise NoDataFoundError("No data found")
        rows = [
            {
                "timestamp": datetime(2025, 1, 3, 21, tzinfo=UTC),
                "delta": Decimal("-0.11"),
                "theta": Decimal("-0.04"),
                "vega": Decimal("0.22"),
                "rho": Decimal("-0.07"),
                "implied_vol": Decimal("0"),
                "underlying_price": Decimal("590"),
            }
        ]
        if start_date != end_date:
            rows.extend(
                [
                    {
                        "timestamp": datetime(2025, 1, 10, 21, tzinfo=UTC),
                        "delta": Decimal("-0.20"),
                        "theta": Decimal("-0.04"),
                        "vega": Decimal("0.22"),
                        "rho": Decimal("-0.07"),
                        "implied_vol": Decimal("0.20"),
                        "underlying_price": Decimal("550"),
                    },
                    {
                        "timestamp": datetime(2025, 2, 14, 21, tzinfo=UTC),
                        "delta": Decimal("0"),
                        "theta": Decimal("0"),
                        "vega": Decimal("0"),
                        "rho": Decimal("0"),
                        "implied_vol": Decimal("0.01"),
                        "underlying_price": Decimal("525"),
                    },
                ]
            )
        return rows


class NoDataFoundError(Exception):
    pass


class FakeProvider:
    def retrieve_option_eod_quotes(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionQuote]:
        assert contract.strike == Decimal("530")
        assert start_date == date(2025, 1, 3)
        assert end_date == date(2025, 2, 14)
        return [
            OptionQuote(
                contract=contract,
                timestamp=datetime(2025, 1, 3, 21, tzinfo=UTC),
                bid=Decimal("1.90"),
                ask=Decimal("2.10"),
                last=Decimal("2.00"),
                mark=Decimal("2.00"),
                volume=100,
                open_interest=1000,
            ),
            OptionQuote(
                contract=contract,
                timestamp=datetime(2025, 1, 10, 21, tzinfo=UTC),
                bid=Decimal("3.90"),
                ask=Decimal("4.10"),
                last=Decimal("4.00"),
                mark=Decimal("4.00"),
            ),
            OptionQuote(
                contract=contract,
                timestamp=datetime(2025, 2, 13, 21, tzinfo=UTC),
                bid=Decimal("5.00"),
                ask=Decimal("5.20"),
                last=Decimal("5.10"),
                mark=Decimal("5.10"),
            ),
        ]


def test_single_trade_pipeline_selects_and_explains_pnl(tmp_path: Path) -> None:
    result = run_single_trade_pipeline(
        SingleTradePipelineConfig(
            entry_date=date(2025, 1, 3),
            target_dte=45,
            target_delta=Decimal("-0.10"),
            report_path=tmp_path / "report.md",
        ),
        endpoints=FakeEndpoints(),
        provider=FakeProvider(),
    )

    assert result.selected_candidate.contract.expiration == date(2025, 2, 14)
    assert result.selected_candidate.dte == 42
    assert result.selected_candidate.contract.strike == Decimal("530")
    assert result.selected_candidate.delta == Decimal("-0.11")
    assert result.audit.entry_gross_credit == Decimal("200.00")
    assert result.audit.entry_commission == Decimal("0.65")
    assert result.audit.entry_net_cash_flow == Decimal("199.35")
    assert result.audit.exit_gross_debit == Decimal("500")
    assert result.audit.exit_commission == Decimal("0.65")
    assert result.audit.realized_pnl == Decimal("-301.30")
    assert result.audit.final_equity == Decimal("99698.70")
    assert "PnL = entry gross credit" in (tmp_path / "report.md").read_text()


def test_single_trade_pipeline_can_stop_loss_before_expiration(tmp_path: Path) -> None:
    result = run_single_trade_pipeline(
        SingleTradePipelineConfig(
            entry_date=date(2025, 1, 3),
            target_dte=45,
            target_delta=Decimal("-0.10"),
            stop_loss_pct=Decimal("1.00"),
            report_path=tmp_path / "report.md",
        ),
        endpoints=FakeEndpoints(),
        provider=FakeProvider(),
    )

    closed = result.backtest_result.closed_positions[0]
    assert closed.exit_date == date(2025, 1, 10)
    assert closed.exit_reason.value == "early_exit"
    assert result.audit.exit_price == Decimal("4.00")
    assert result.audit.realized_pnl == Decimal("-201.30")
