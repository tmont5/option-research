from __future__ import annotations

from collections.abc import Callable
from datetime import date, time

from options_quant.data.providers import ThetaDataOptionEndpoints


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


class RecordingThetaOptionClient:
    def __init__(self, response: object | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.response = response or MockPandasFrame([{"value": "ok"}])

    def __getattr__(self, method_name: str) -> Callable[..., object]:
        def call(**params: object) -> object:
            self.calls.append((method_name, dict(params)))
            return self.response

        return call


def test_list_endpoints_route_to_thetadata_methods() -> None:
    client = RecordingThetaOptionClient()
    endpoints = ThetaDataOptionEndpoints(client=client)

    symbols = endpoints.list_symbols()
    endpoints.list_dates(
        request_type="quote",
        symbol="SPY",
        expiration=date(2026, 7, 17),
        strike="500",
        right="put",
    )
    endpoints.list_expirations(symbol=["SPY", "QQQ"])
    endpoints.list_strikes(symbol="SPY", expiration=date(2026, 7, 17))
    endpoints.list_contracts(
        request_type="trade",
        query_date=date(2026, 6, 10),
        symbol="SPY",
        max_dte=45,
    )

    assert symbols == [{"value": "ok"}]
    assert client.calls == [
        ("option_list_symbols", {}),
        (
            "option_list_dates",
            {
                "request_type": "quote",
                "symbol": "SPY",
                "expiration": date(2026, 7, 17),
                "strike": "500",
                "right": "put",
            },
        ),
        ("option_list_expirations", {"symbol": ["SPY", "QQQ"]}),
        ("option_list_strikes", {"symbol": "SPY", "expiration": date(2026, 7, 17)}),
        (
            "option_list_contracts",
            {
                "request_type": "trade",
                "date": date(2026, 6, 10),
                "symbol": "SPY",
                "max_dte": 45,
            },
        ),
    ]


def test_snapshot_endpoints_route_and_drop_none_params() -> None:
    client = RecordingThetaOptionClient(response=MockPolarsFrame([{"delta": "-0.10"}]))
    endpoints = ThetaDataOptionEndpoints(client=client)

    rows = endpoints.snapshot_greeks_all(
        symbol="SPY",
        expiration=date(2026, 7, 17),
        strike="500",
        right="put",
        max_dte=None,
        use_market_value=True,
    )

    assert rows == [{"delta": "-0.10"}]
    assert client.calls == [
        (
            "option_snapshot_greeks_all",
            {
                "symbol": "SPY",
                "expiration": date(2026, 7, 17),
                "strike": "500",
                "right": "put",
                "use_market_value": True,
            },
        )
    ]


def test_history_endpoints_cover_market_data_and_greeks() -> None:
    client = RecordingThetaOptionClient()
    endpoints = ThetaDataOptionEndpoints(client=client)

    endpoints.history_quote(
        symbol="SPY",
        expiration=date(2026, 7, 17),
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
        interval="1m",
    )
    endpoints.history_trade_quote(
        symbol="SPY",
        expiration=date(2026, 7, 17),
        date=date(2026, 6, 10),
        exclusive=True,
    )
    endpoints.history_greeks_implied_volatility(
        symbol="SPY",
        expiration=date(2026, 7, 17),
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
    )
    endpoints.history_trade_greeks_third_order(
        symbol="SPY",
        expiration=date(2026, 7, 17),
        date=date(2026, 6, 10),
    )

    assert [call[0] for call in client.calls] == [
        "option_history_quote",
        "option_history_trade_quote",
        "option_history_greeks_implied_volatility",
        "option_history_trade_greeks_third_order",
    ]
    assert client.calls[0][1]["interval"] == "1m"
    assert client.calls[1][1]["exclusive"] is True


def test_at_time_endpoints_route_to_thetadata_methods() -> None:
    client = RecordingThetaOptionClient()
    endpoints = ThetaDataOptionEndpoints(client=client)

    endpoints.at_time_trade(
        symbol="SPY",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
        time_of_day=time(10, 0),
        expiration=date(2026, 7, 17),
        strike="500",
        right="put",
    )
    endpoints.at_time_quote(
        symbol="SPY",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
        time_of_day="10:00:00",
        expiration=date(2026, 7, 17),
        max_dte=45,
    )

    assert client.calls == [
        (
            "option_at_time_trade",
            {
                "symbol": "SPY",
                "start_date": date(2026, 6, 1),
                "end_date": date(2026, 6, 10),
                "time_of_day": time(10, 0),
                "expiration": date(2026, 7, 17),
                "strike": "500",
                "right": "put",
            },
        ),
        (
            "option_at_time_quote",
            {
                "symbol": "SPY",
                "start_date": date(2026, 6, 1),
                "end_date": date(2026, 6, 10),
                "time_of_day": "10:00:00",
                "expiration": date(2026, 7, 17),
                "strike": "*",
                "right": "both",
                "max_dte": 45,
            },
        ),
    ]


def test_row_list_response_is_normalized() -> None:
    client = RecordingThetaOptionClient(response=[{"symbol": "SPY"}])
    endpoints = ThetaDataOptionEndpoints(client=client)

    assert endpoints.list_symbols() == [{"symbol": "SPY"}]
