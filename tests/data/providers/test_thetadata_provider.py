from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import options_quant.data.providers.thetadata as thetadata_provider_module
from options_quant.data.models import OptionContract, OptionType
from options_quant.data.providers.thetadata import (
    RawResponse,
    ThetaDataProvider,
    ThetaDataPythonClient,
)


class MockThetaDataTransport:
    def __init__(self, responses: dict[str, RawResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, endpoint: str, params: dict[str, str]) -> RawResponse:
        self.calls.append((endpoint, params))
        return self.responses[endpoint]


class MockPandasFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self.rows


class MockPolarsFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dicts(self) -> list[dict[str, object]]:
        return self.rows


class MockThetaPythonLibraryClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def stock_history_quote(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> MockPandasFrame:
        self.calls.append(
            (
                "stock_history_quote",
                {"symbol": symbol, "start_date": start_date, "end_date": end_date},
            )
        )
        return MockPandasFrame(
            [
                {
                    "date": date(2026, 6, 10),
                    "ms_of_day": 34_200_000,
                    "bid": "205.1",
                    "ask": "205.2",
                    "price": "205.15",
                    "volume": 1_000_000,
                }
            ]
        )

    def stock_history_eod(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> MockPandasFrame:
        self.calls.append(
            (
                "stock_history_eod",
                {"symbol": symbol, "start_date": start_date, "end_date": end_date},
            )
        )
        return MockPandasFrame(
            [
                {
                    "date": date(2026, 6, 10),
                    "close": "205.15",
                    "volume": 1_000_000,
                }
            ]
        )

    def option_history_eod(
        self,
        symbol: str,
        expiration: date,
        strike: str,
        right: str,
        start_date: date,
        end_date: date,
    ) -> MockPandasFrame:
        self.calls.append(
            (
                "option_history_eod",
                {
                    "symbol": symbol,
                    "expiration": expiration,
                    "strike": strike,
                    "right": right,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
        )
        return MockPandasFrame(
            [
                {
                    "date": date(2026, 6, 10),
                    "bid": "3.10",
                    "ask": "3.30",
                    "close": "3.20",
                    "volume": 250,
                    "open_interest": 1_500,
                }
            ]
        )

    def option_list_contracts(
        self,
        request_type: str,
        date: date,
        symbol: str,
    ) -> MockPandasFrame:
        self.calls.append(
            (
                "option_list_contracts",
                {"request_type": request_type, "date": date, "symbol": symbol},
            )
        )
        return MockPandasFrame(
            [
                {
                    "root": symbol,
                    "expiration": make_contract().expiration,
                    "strike": "200",
                    "right": "P",
                    "multiplier": 100,
                }
            ]
        )

    def option_history_greeks_all(
        self,
        symbol: str,
        expiration: date,
        interval: str,
        start_time: str,
        end_time: str,
        strike: str,
        right: str,
        start_date: date,
        end_date: date,
    ) -> MockPolarsFrame:
        self.calls.append(
            (
                "option_history_greeks_all",
                {
                    "symbol": symbol,
                    "expiration": expiration,
                    "interval": interval,
                    "start_time": start_time,
                    "end_time": end_time,
                    "strike": strike,
                    "right": right,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
        )
        return MockPolarsFrame(
            [
                {
                    "timestamp": "2026-06-10T14:30:00+00:00",
                    "delta": "-0.32",
                    "gamma": "0.012",
                    "theta": "-0.04",
                    "vega": "0.18",
                    "rho": "-0.03",
                    "iv": "0.42",
                }
            ]
        )

    def option_history_greeks_first_order(
        self,
        symbol: str,
        expiration: date,
        interval: str,
        start_time: str,
        end_time: str,
        strike: str,
        right: str,
        start_date: date,
        end_date: date,
    ) -> MockPolarsFrame:
        self.calls.append(
            (
                "option_history_greeks_first_order",
                {
                    "symbol": symbol,
                    "expiration": expiration,
                    "interval": interval,
                    "start_time": start_time,
                    "end_time": end_time,
                    "strike": strike,
                    "right": right,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
        )
        return MockPolarsFrame(
            [
                {
                    "timestamp": "2026-06-10T14:30:00+00:00",
                    "delta": "-0.32",
                    "theta": "-0.04",
                    "vega": "0.18",
                    "rho": "-0.03",
                    "iv": "0.42",
                }
            ]
        )


class RecordingThetaClientConstructor:
    def __init__(self) -> None:
        self.kwargs: dict[str, str] | None = None

    def __call__(self, **kwargs: str) -> MockThetaPythonLibraryClient:
        self.kwargs = kwargs
        return MockThetaPythonLibraryClient()


def test_python_client_forwards_mdds_overrides(monkeypatch: Any) -> None:
    constructor = RecordingThetaClientConstructor()

    monkeypatch.setattr(
        thetadata_provider_module,
        "import_module",
        lambda _: SimpleNamespace(ThetaClient=constructor),
    )

    ThetaDataPythonClient(
        creds_file="/tmp/creds.txt",
        dataframe_type="pandas",
        mdds_host="127.0.0.1",
        mdds_port="25510",
        mdds_type="PROD",
    )

    assert constructor.kwargs == {
        "dataframe_type": "pandas",
        "creds_file": "/tmp/creds.txt",
        "mdds_host": "127.0.0.1",
        "mdds_port": "25510",
        "mdds_type": "PROD",
    }


def make_contract() -> OptionContract:
    return OptionContract(
        underlying_symbol="AAPL",
        expiration=date(2026, 7, 17),
        strike=Decimal("200"),
        option_type=OptionType.PUT,
    )


def test_retrieve_underlying_eod_prices_uses_thetadata_root_alias() -> None:
    transport = MockThetaDataTransport(
        {
            "/v2/hist/stock/eod": {
                "header": ["date", "close", "volume"],
                "response": [[20260610, "485.15", 1_000_000]],
            }
        }
    )
    provider = ThetaDataProvider(transport)

    prices = provider.retrieve_underlying_eod_prices(
        "BRK-B",
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert transport.calls == [
        (
            "/v2/hist/stock/eod",
            {"root": "BRK.B", "start_date": "20260610", "end_date": "20260610"},
        )
    ]
    assert prices[0].symbol == "BRK-B"


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

    assert transport.calls == [("/v2/list/contracts", {"root": "AAPL", "date": "20260610"})]
    assert chain.underlying_symbol == "AAPL"
    assert chain.timestamp == datetime(2026, 6, 10, tzinfo=UTC)
    assert len(chain.contracts) == 2
    assert chain.contracts[0].strike == Decimal("200")
    assert chain.contracts[0].option_type is OptionType.PUT
    assert chain.contracts[1].option_type is OptionType.CALL


def test_retrieve_option_chain_normalizes_thetadata_root_alias() -> None:
    transport = MockThetaDataTransport(
        {
            "/v2/list/contracts": {
                "response": [
                    {
                        "root": "BRKB",
                        "expiration": "2026-07-17",
                        "strike": "480",
                        "right": "P",
                    },
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    chain = provider.retrieve_option_chain("BRK-B", date(2026, 6, 10))

    assert transport.calls == [("/v2/list/contracts", {"root": "BRKB", "date": "20260610"})]
    assert chain.underlying_symbol == "BRK-B"
    assert chain.contracts[0].underlying_symbol == "BRK-B"


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


def test_retrieve_implied_volatility_maps_live_python_column_name() -> None:
    contract = make_contract()
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/implied_volatility": {
                "response": [
                    {
                        "timestamp": "2026-06-10T20:00:00+00:00",
                        "implied_vol": "0.5548",
                    }
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    observations = provider.retrieve_implied_volatility(
        contract,
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert observations[0].implied_volatility == Decimal("0.5548")


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


def test_retrieve_underlying_eod_prices_maps_eod_rows() -> None:
    transport = MockThetaDataTransport(
        {
            "/v2/hist/stock/eod": {
                "header": ["date", "close", "volume"],
                "response": [[20260610, "205.15", 1_000_000]],
            }
        }
    )
    provider = ThetaDataProvider(transport)

    prices = provider.retrieve_underlying_eod_prices(
        "AAPL",
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert transport.calls == [
        (
            "/v2/hist/stock/eod",
            {"root": "AAPL", "start_date": "20260610", "end_date": "20260610"},
        )
    ]
    assert prices[0].symbol == "AAPL"
    assert prices[0].timestamp == datetime(2026, 6, 10, tzinfo=UTC)
    assert prices[0].price == Decimal("205.15")
    assert prices[0].volume == 1_000_000


def test_retrieve_option_eod_quotes_maps_eod_rows() -> None:
    contract = make_contract()
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/eod": {
                "response": [
                    {
                        "date": "2026-06-10",
                        "bid": "3.10",
                        "ask": "3.30",
                        "close": "3.20",
                        "volume": 250,
                        "oi": 1_500,
                    }
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    quotes = provider.retrieve_option_eod_quotes(
        contract,
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert transport.calls[0][0] == "/v2/hist/option/eod"
    assert quotes[0].contract == contract
    assert quotes[0].timestamp == datetime(2026, 6, 10, tzinfo=UTC)
    assert quotes[0].bid == Decimal("3.10")
    assert quotes[0].ask == Decimal("3.30")
    assert quotes[0].last == Decimal("3.20")
    assert quotes[0].mark == Decimal("3.20")
    assert quotes[0].open_interest == 1_500


def test_retrieve_option_eod_quotes_skips_inverted_bid_ask_rows() -> None:
    contract = make_contract()
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/eod": {
                "response": [
                    {
                        "date": "2026-06-10",
                        "bid": "3.40",
                        "ask": "3.20",
                        "close": "3.30",
                    },
                    {
                        "date": "2026-06-11",
                        "bid": "3.10",
                        "ask": "3.30",
                        "close": "3.20",
                    },
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    quotes = provider.retrieve_option_eod_quotes(
        contract,
        date(2026, 6, 10),
        date(2026, 6, 11),
    )

    assert len(quotes) == 1
    assert quotes[0].timestamp == datetime(2026, 6, 11, tzinfo=UTC)
    assert quotes[0].bid == Decimal("3.10")
    assert quotes[0].ask == Decimal("3.30")


def test_retrieve_option_eod_quotes_uses_thetadata_root_alias() -> None:
    contract = OptionContract(
        underlying_symbol="BRK-B",
        expiration=date(2026, 7, 17),
        strike=Decimal("480"),
        option_type=OptionType.PUT,
    )
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/eod": {
                "response": [
                    {
                        "symbol": "BRKB",
                        "expiration": "2026-07-17",
                        "strike": "480",
                        "right": "P",
                        "date": "2026-06-10",
                        "bid": "6.10",
                        "ask": "6.30",
                        "close": "6.20",
                    }
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    quotes = provider.retrieve_option_eod_quotes(
        contract,
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert transport.calls == [
        (
            "/v2/hist/option/eod",
            {
                "root": "BRKB",
                "exp": "20260717",
                "strike": "480",
                "right": "P",
                "start_date": "20260610",
                "end_date": "20260610",
            },
        )
    ]
    assert quotes[0].contract == contract


def test_retrieve_option_eod_quotes_filters_rows_to_requested_contract() -> None:
    contract = make_contract()
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/eod": {
                "response": [
                    {
                        "symbol": "AAPL",
                        "expiration": "2026-07-17",
                        "strike": "200",
                        "right": "PUT",
                        "date": "2026-06-10",
                        "bid": "3.10",
                        "ask": "3.30",
                        "close": "3.20",
                    },
                    {
                        "symbol": "AAPL",
                        "expiration": "2026-07-17",
                        "strike": "195",
                        "right": "PUT",
                        "date": "2026-06-10",
                        "bid": "1.10",
                        "ask": "1.20",
                        "close": "1.15",
                    },
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    quotes = provider.retrieve_option_eod_quotes(
        contract,
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert len(quotes) == 1
    assert quotes[0].contract == contract
    assert quotes[0].mark == Decimal("3.20")


def test_retrieve_first_order_greeks_omits_gamma() -> None:
    contract = make_contract()
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/greeks_first_order": {
                "response": [
                    {
                        "timestamp": "2026-06-10T14:30:00+00:00",
                        "delta": "-0.32",
                        "theta": "-0.04",
                        "vega": "0.18",
                        "rho": "-0.03",
                        "iv": "0.42",
                    }
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    greeks = provider.retrieve_first_order_greeks(
        contract,
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert transport.calls[0][0] == "/v2/hist/option/greeks_first_order"
    assert greeks[0].delta == Decimal("-0.32")
    assert greeks[0].gamma is None
    assert greeks[0].theta == Decimal("-0.04")
    assert greeks[0].vega == Decimal("0.18")
    assert greeks[0].rho == Decimal("-0.03")
    assert greeks[0].implied_volatility == Decimal("0.42")


def test_retrieve_first_order_greeks_maps_live_python_iv_column_name() -> None:
    contract = make_contract()
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/greeks_first_order": {
                "response": [
                    {
                        "timestamp": "2026-06-10T20:00:00+00:00",
                        "delta": "-0.2749",
                        "theta": "-0.1287",
                        "vega": "16.8641",
                        "rho": "-4.8021",
                        "implied_vol": "0.5548",
                    }
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    greeks = provider.retrieve_first_order_greeks(
        contract,
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert greeks[0].delta == Decimal("-0.2749")
    assert greeks[0].gamma is None
    assert greeks[0].implied_volatility == Decimal("0.5548")


def test_retrieve_first_order_greeks_treats_nonpositive_iv_as_missing() -> None:
    contract = make_contract()
    transport = MockThetaDataTransport(
        {
            "/v2/hist/option/greeks_first_order": {
                "response": [
                    {
                        "timestamp": "2026-06-10T20:00:00+00:00",
                        "delta": "-0.2749",
                        "theta": "-0.1287",
                        "vega": "16.8641",
                        "rho": "-4.8021",
                        "implied_vol": "0.0",
                    }
                ]
            }
        }
    )
    provider = ThetaDataProvider(transport)

    greeks = provider.retrieve_first_order_greeks(
        contract,
        date(2026, 6, 10),
        date(2026, 6, 10),
    )

    assert greeks[0].delta == Decimal("-0.2749")
    assert greeks[0].implied_volatility is None


def test_python_client_adapter_maps_stock_price_dataframe_to_provider_models() -> None:
    library_client = MockThetaPythonLibraryClient()
    provider = ThetaDataProvider(ThetaDataPythonClient(client=library_client))

    prices = provider.retrieve_underlying_prices("AAPL", date(2026, 6, 10), date(2026, 6, 11))

    assert library_client.calls == [
        (
            "stock_history_quote",
            {
                "symbol": "AAPL",
                "start_date": date(2026, 6, 10),
                "end_date": date(2026, 6, 11),
            },
        )
    ]
    assert prices[0].symbol == "AAPL"
    assert prices[0].timestamp == datetime(2026, 6, 10, 9, 30, tzinfo=UTC)
    assert prices[0].price == Decimal("205.15")


def test_python_client_adapter_maps_eod_methods() -> None:
    library_client = MockThetaPythonLibraryClient()
    provider = ThetaDataProvider(ThetaDataPythonClient(client=library_client))

    prices = provider.retrieve_underlying_eod_prices("AAPL", date(2026, 6, 10), date(2026, 6, 11))
    quotes = provider.retrieve_option_eod_quotes(
        make_contract(), date(2026, 6, 10), date(2026, 6, 11)
    )
    greeks = provider.retrieve_first_order_greeks(
        make_contract(), date(2026, 6, 10), date(2026, 6, 11)
    )

    assert library_client.calls == [
        (
            "stock_history_eod",
            {
                "symbol": "AAPL",
                "start_date": date(2026, 6, 10),
                "end_date": date(2026, 6, 11),
            },
        ),
        (
            "option_history_eod",
            {
                "symbol": "AAPL",
                "expiration": date(2026, 7, 17),
                "strike": "200",
                "right": "P",
                "start_date": date(2026, 6, 10),
                "end_date": date(2026, 6, 11),
            },
        ),
        (
            "option_history_greeks_first_order",
            {
                "interval": "1m",
                "start_time": "16:00:00",
                "end_time": "16:00:00",
                "symbol": "AAPL",
                "expiration": date(2026, 7, 17),
                "strike": "200",
                "right": "P",
                "start_date": date(2026, 6, 10),
                "end_date": date(2026, 6, 11),
            },
        ),
    ]
    assert prices[0].price == Decimal("205.15")
    assert quotes[0].mark == Decimal("3.20")
    assert greeks[0].gamma is None


def test_python_client_adapter_adds_default_endpoint_params() -> None:
    library_client = MockThetaPythonLibraryClient()
    provider = ThetaDataProvider(ThetaDataPythonClient(client=library_client))

    chain = provider.retrieve_option_chain("AAPL", date(2026, 6, 10))

    assert library_client.calls == [
        (
            "option_list_contracts",
            {
                "request_type": "quote",
                "date": date(2026, 6, 10),
                "symbol": "AAPL",
            },
        )
    ]
    assert chain.contracts == (make_contract(),)


def test_python_client_adapter_converts_contract_params_and_polars_frames() -> None:
    library_client = MockThetaPythonLibraryClient()
    provider = ThetaDataProvider(ThetaDataPythonClient(client=library_client))

    greeks = provider.retrieve_greeks(make_contract(), date(2026, 6, 10), date(2026, 6, 11))

    assert library_client.calls == [
        (
            "option_history_greeks_all",
            {
                "symbol": "AAPL",
                "expiration": date(2026, 7, 17),
                "interval": "1m",
                "start_time": "16:00:00",
                "end_time": "16:00:00",
                "strike": "200",
                "right": "P",
                "start_date": date(2026, 6, 10),
                "end_date": date(2026, 6, 11),
            },
        )
    ]
    assert greeks[0].contract == make_contract()
    assert greeks[0].delta == Decimal("-0.32")
    assert greeks[0].implied_volatility == Decimal("0.42")


def test_python_client_adapter_allows_endpoint_method_overrides() -> None:
    class CustomClient:
        def __init__(self) -> None:
            self.called = False

        def custom_stock_quote(
            self,
            symbol: str,
            start_date: date,
            end_date: date,
        ) -> list[dict[str, object]]:
            self.called = True
            return [
                {
                    "date": start_date,
                    "price": "100",
                }
            ]

    custom_client = CustomClient()
    provider = ThetaDataProvider(
        ThetaDataPythonClient(
            client=custom_client,
            endpoint_methods={"/v2/hist/stock/quote": "custom_stock_quote"},
        )
    )

    prices = provider.retrieve_underlying_prices("MSFT", date(2026, 6, 10), date(2026, 6, 10))

    assert custom_client.called
    assert prices[0].symbol == "MSFT"
    assert prices[0].price == Decimal("100")
