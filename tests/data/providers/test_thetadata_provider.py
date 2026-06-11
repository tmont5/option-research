from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from options_quant.data.models import OptionContract, OptionType
from options_quant.data.providers.thetadata import RawResponse, ThetaDataProvider


class MockThetaDataTransport:
    def __init__(self, responses: dict[str, RawResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, endpoint: str, params: dict[str, str]) -> RawResponse:
        self.calls.append((endpoint, params))
        return self.responses[endpoint]


def make_contract() -> OptionContract:
    return OptionContract(
        underlying_symbol="AAPL",
        expiration=date(2026, 7, 17),
        strike=Decimal("200"),
        option_type=OptionType.PUT,
    )


def test_retrieve_underlying_prices_maps_thetadata_rows_to_model() -> None:
    transport = MockThetaDataTransport(
        {
            "/v2/hist/stock/quote": {
                "header": ["date", "ms_of_day", "bid", "ask", "price", "volume"],
                "response": [[20260610, 34_200_000, 205.1, 205.2, 205.15, 1_000_000]],
            }
        }
    )
    provider = ThetaDataProvider(transport)

    prices = provider.retrieve_underlying_prices("AAPL", date(2026, 6, 10), date(2026, 6, 10))

    assert transport.calls == [
        (
            "/v2/hist/stock/quote",
            {"root": "AAPL", "start_date": "20260610", "end_date": "20260610"},
        )
    ]
    assert prices[0].symbol == "AAPL"
    assert prices[0].timestamp == datetime(2026, 6, 10, 9, 30, tzinfo=UTC)
    assert prices[0].price == Decimal("205.15")
    assert prices[0].bid == Decimal("205.1")
    assert prices[0].ask == Decimal("205.2")
    assert prices[0].volume == 1_000_000


def test_retrieve_option_chain_maps_contract_rows() -> None:
    transport = MockThetaDataTransport(
        {
            "/v2/list/contracts": {
                "response": [
                    {
                        "root": "AAPL",
                        "expiration": "2026-07-17",
                        "strike": "200",
                        "right": "P",
                    },
                    {
                        "root": "AAPL",
                        "expiration": "2026-07-17",
                        "strike": "210",
                        "right": "C",
                        "multiplier": 100,
                    },
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    chain = provider.retrieve_option_chain("AAPL", date(2026, 6, 10))

    assert transport.calls == [
        ("/v2/list/contracts", {"root": "AAPL", "date": "20260610"})
    ]
    assert chain.underlying_symbol == "AAPL"
    assert chain.timestamp == datetime(2026, 6, 10, tzinfo=UTC)
    assert len(chain.contracts) == 2
    assert chain.contracts[0].strike == Decimal("200")
    assert chain.contracts[0].option_type is OptionType.PUT
    assert chain.contracts[1].option_type is OptionType.CALL


def test_retrieve_implied_volatility_maps_iv_rows() -> None:
    contract = make_contract()
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/implied_volatility": {
                "header": ["date", "ms_of_day", "iv"],
                "response": [[20260610, 34_200_000, "0.42"]],
            }
        }
    )
    provider = ThetaDataProvider(transport)

    observations = provider.retrieve_implied_volatility(
        contract,
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert transport.calls == [
        (
            "/v2/hist/option/implied_volatility",
            {
                "root": "AAPL",
                "exp": "20260717",
                "strike": "200",
                "right": "P",
                "start_date": "20260610",
                "end_date": "20260610",
            },
        )
    ]
    assert observations[0].contract == contract
    assert observations[0].implied_volatility == Decimal("0.42")


def test_retrieve_greeks_maps_greek_rows() -> None:
    contract = make_contract()
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/greeks": {
                "response": [
                    {
                        "timestamp": "2026-06-10T14:30:00+00:00",
                        "delta": "-0.32",
                        "gamma": "0.012",
                        "theta": "-0.04",
                        "vega": "0.18",
                        "rho": "-0.03",
                        "implied_volatility": "0.42",
                    }
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    greeks = provider.retrieve_greeks(contract, date(2026, 6, 10), date(2026, 6, 10))

    assert transport.calls[0][0] == "/v2/hist/option/greeks"
    assert greeks[0].contract == contract
    assert greeks[0].timestamp == datetime(2026, 6, 10, 14, 30, tzinfo=UTC)
    assert greeks[0].delta == Decimal("-0.32")
    assert greeks[0].gamma == Decimal("0.012")
    assert greeks[0].theta == Decimal("-0.04")
    assert greeks[0].vega == Decimal("0.18")
    assert greeks[0].rho == Decimal("-0.03")
    assert greeks[0].implied_volatility == Decimal("0.42")


def test_retrieve_open_interest_maps_open_interest_rows() -> None:
    contract = make_contract()
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/open_interest": {
                "header": ["date", "open_interest"],
                "response": [[20260610, 1_500]],
            }
        }
    )
    provider = ThetaDataProvider(transport)

    observations = provider.retrieve_open_interest(
        contract,
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert transport.calls[0][0] == "/v2/hist/option/open_interest"
    assert observations[0].contract == contract
    assert observations[0].timestamp == datetime(2026, 6, 10, tzinfo=UTC)
    assert observations[0].open_interest == 1_500
