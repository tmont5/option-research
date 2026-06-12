"""ThetaData market data provider.

This module keeps ThetaData transport and response-shape details behind a
provider interface. Callers receive the internal Pydantic models used elsewhere
in the application.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from importlib import import_module
from typing import Any, Protocol, cast
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionImpliedVolatility,
    OptionOpenInterest,
    OptionQuote,
    OptionStyle,
    OptionType,
    UnderlyingPrice,
)

RawResponse = dict[str, Any]
RawRow = dict[str, Any]


class ThetaDataTransport(Protocol):
    """Minimal transport contract used by the ThetaData provider."""

    def get(self, endpoint: str, params: dict[str, str]) -> RawResponse:
        """Fetch a raw ThetaData response."""


class ThetaDataClient:
    """HTTP client for ThetaData's REST API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:25510",
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def get(self, endpoint: str, params: dict[str, str]) -> RawResponse:
        """Fetch and decode one ThetaData JSON response."""
        query = dict(params)
        if self._api_key is not None:
            query["api_key"] = self._api_key
        url = f"{self._base_url}/{endpoint.lstrip('/')}?{urlencode(query)}"
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=self._timeout_seconds) as response:
            payload = response.read().decode("utf-8")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("ThetaData response must be a JSON object")
        return decoded


class ThetaDataPythonClient:
    """Adapter for ThetaData's Python library.

    The library returns Polars or Pandas dataframes. This adapter converts those
    frames into the raw response shape consumed by ThetaDataProvider so the
    normalization logic stays shared across REST and Python-library transports.
    """

    DEFAULT_ENDPOINT_METHODS = {
        "/v2/hist/stock/eod": "stock_history_eod",
        "/v2/hist/stock/quote": "stock_history_quote",
        "/v2/hist/option/eod": "option_history_eod",
        "/v2/list/contracts": "option_list_contracts",
        "/v2/hist/option/implied_volatility": "option_history_greeks_implied_volatility",
        "/v2/hist/option/greeks": "option_history_greeks_all",
        "/v2/hist/option/greeks_first_order": "option_history_greeks_first_order",
        "/v2/hist/option/open_interest": "option_history_open_interest",
    }
    DEFAULT_ENDPOINT_PARAMS = {
        "/v2/list/contracts": {"request_type": "quote"},
    }
    DEFAULT_PARAMETER_ALIASES = {
        "root": "symbol",
        "exp": "expiration",
    }

    def __init__(
        self,
        client: Any | None = None,
        *,
        email: str | None = None,
        password: str | None = None,
        creds_file: str | None = None,
        dataframe_type: str = "pandas",
        endpoint_methods: dict[str, str] | None = None,
        endpoint_params: dict[str, dict[str, str]] | None = None,
        parameter_aliases: dict[str, str] | None = None,
    ) -> None:
        if client is None:
            theta_module = cast(Any, import_module("thetadata"))
            theta_client_class = theta_module.ThetaClient
            client_kwargs: dict[str, str] = {"dataframe_type": dataframe_type}
            if email is not None:
                client_kwargs["email"] = email
            if password is not None:
                client_kwargs["password"] = password
            if creds_file is not None:
                client_kwargs["creds_file"] = creds_file
            client = theta_client_class(**client_kwargs)
        self._client = client
        self._endpoint_methods = endpoint_methods or self.DEFAULT_ENDPOINT_METHODS
        self._endpoint_params = endpoint_params or self.DEFAULT_ENDPOINT_PARAMS
        self._parameter_aliases = parameter_aliases or self.DEFAULT_PARAMETER_ALIASES

    def get(self, endpoint: str, params: dict[str, str]) -> RawResponse:
        """Fetch data through the ThetaData Python library."""
        method_name = self._endpoint_methods.get(endpoint)
        if method_name is None:
            raise ValueError(f"unsupported ThetaData Python endpoint: {endpoint}")
        method = getattr(self._client, method_name)
        endpoint_params = self._endpoint_params.get(endpoint, {})
        frame = method(**self._python_kwargs(endpoint_params | params))
        rows = _dataframe_rows(frame)
        return {"response": rows}

    def _python_kwargs(self, params: dict[str, str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        for key, value in params.items():
            python_key = self._parameter_aliases.get(key, key)
            kwargs[python_key] = _python_value(python_key, value)
        return kwargs


class ThetaDataProvider:
    """Normalize ThetaData responses into internal market data models."""

    def __init__(self, transport: ThetaDataTransport | None = None) -> None:
        self._transport = transport if transport is not None else ThetaDataClient()

    def retrieve_underlying_prices(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[UnderlyingPrice]:
        """Return underlying prices for an inclusive date range."""
        response = self._transport.get(
            "/v2/hist/stock/quote",
            {
                "root": symbol,
                "start_date": _thetadata_date(start_date),
                "end_date": _thetadata_date(end_date),
            },
        )
        return [
            UnderlyingPrice(
                symbol=symbol,
                timestamp=_row_timestamp(row, start_date),
                price=_required_decimal(row, "price", "last", "close", "mark"),
                bid=_optional_decimal(row, "bid"),
                ask=_optional_decimal(row, "ask"),
                volume=_optional_int(row, "volume"),
            )
            for row in _rows(response)
        ]

    def retrieve_underlying_eod_prices(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[UnderlyingPrice]:
        """Return end-of-day underlying prices for an inclusive date range."""
        response = self._transport.get(
            "/v2/hist/stock/eod",
            {
                "root": symbol,
                "start_date": _thetadata_date(start_date),
                "end_date": _thetadata_date(end_date),
            },
        )
        return [
            UnderlyingPrice(
                symbol=symbol,
                timestamp=_row_timestamp(row, start_date),
                price=_required_decimal(row, "price", "close", "last", "mark"),
                bid=_optional_decimal(row, "bid"),
                ask=_optional_decimal(row, "ask"),
                volume=_optional_int(row, "volume"),
            )
            for row in _rows(response)
        ]

    def retrieve_option_chain(self, symbol: str, as_of_date: date) -> OptionChain:
        """Return an option chain snapshot for one underlying and date."""
        response = self._transport.get(
            "/v2/list/contracts",
            {
                "root": symbol,
                "date": _thetadata_date(as_of_date),
            },
        )
        contracts = tuple(_contract_from_row(symbol, row) for row in _rows(response))
        return OptionChain(
            underlying_symbol=symbol,
            timestamp=datetime.combine(as_of_date, time.min, tzinfo=UTC),
            contracts=contracts,
        )

    def retrieve_implied_volatility(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionImpliedVolatility]:
        """Return implied volatility observations for one option contract."""
        response = self._transport.get(
            "/v2/hist/option/implied_volatility",
            _contract_params(contract, start_date, end_date),
        )
        return [
            OptionImpliedVolatility(
                contract=contract,
                timestamp=_row_timestamp(row, start_date),
                implied_volatility=_required_decimal(
                    row, "implied_volatility", "implied_vol", "iv"
                ),
            )
            for row in _rows(response)
        ]

    def retrieve_option_eod_quotes(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionQuote]:
        """Return end-of-day option price observations for one contract."""
        response = self._transport.get(
            "/v2/hist/option/eod",
            _contract_params(contract, start_date, end_date),
        )
        quotes: list[OptionQuote] = []
        for row in _rows(response):
            bid = _optional_decimal(row, "bid")
            ask = _optional_decimal(row, "ask")
            mark = _optional_decimal(row, "mark", "price", "close")
            last = _optional_decimal(row, "last", "close", "price")
            if bid is None:
                bid = mark if mark is not None else last
            if ask is None:
                ask = mark if mark is not None else last
            if bid is None or ask is None:
                raise ValueError(
                    "ThetaData option EOD row must include bid/ask or a mark/close price"
                )
            quotes.append(
                OptionQuote(
                    contract=contract,
                    timestamp=_row_timestamp(row, start_date),
                    bid=bid,
                    ask=ask,
                    last=last,
                    mark=mark,
                    volume=_optional_int(row, "volume"),
                    open_interest=_optional_int(row, "open_interest", "oi"),
                )
            )
        return quotes

    def retrieve_greeks(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionGreek]:
        """Return Greek observations for one option contract."""
        response = self._transport.get(
            "/v2/hist/option/greeks",
            _contract_params(contract, start_date, end_date),
        )
        return [
            OptionGreek(
                contract=contract,
                timestamp=_row_timestamp(row, start_date),
                delta=_optional_decimal(row, "delta"),
                gamma=_optional_decimal(row, "gamma"),
                theta=_optional_decimal(row, "theta"),
                vega=_optional_decimal(row, "vega"),
                rho=_optional_decimal(row, "rho"),
                implied_volatility=_optional_decimal(
                    row, "implied_volatility", "implied_vol", "iv"
                ),
            )
            for row in _rows(response)
        ]

    def retrieve_first_order_greeks(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionGreek]:
        """Return first-order Greek observations for one option contract."""
        response = self._transport.get(
            "/v2/hist/option/greeks_first_order",
            _contract_params(contract, start_date, end_date),
        )
        return [
            OptionGreek(
                contract=contract,
                timestamp=_row_timestamp(row, start_date),
                delta=_optional_decimal(row, "delta"),
                gamma=None,
                theta=_optional_decimal(row, "theta"),
                vega=_optional_decimal(row, "vega"),
                rho=_optional_decimal(row, "rho"),
                implied_volatility=_optional_decimal(
                    row, "implied_volatility", "implied_vol", "iv"
                ),
            )
            for row in _rows(response)
        ]

    def retrieve_open_interest(
        self,
        contract: OptionContract,
        start_date: date,
        end_date: date,
    ) -> list[OptionOpenInterest]:
        """Return open interest observations for one option contract."""
        response = self._transport.get(
            "/v2/hist/option/open_interest",
            _contract_params(contract, start_date, end_date),
        )
        return [
            OptionOpenInterest(
                contract=contract,
                timestamp=_row_timestamp(row, start_date),
                open_interest=_required_int(row, "open_interest", "oi"),
            )
            for row in _rows(response)
        ]


def _contract_params(
    contract: OptionContract,
    start_date: date,
    end_date: date,
) -> dict[str, str]:
    return {
        "root": contract.underlying_symbol,
        "exp": _thetadata_date(contract.expiration),
        "strike": str(contract.strike),
        "right": _thetadata_right(contract.option_type),
        "start_date": _thetadata_date(start_date),
        "end_date": _thetadata_date(end_date),
    }


def _contract_from_row(symbol: str, row: RawRow) -> OptionContract:
    return OptionContract(
        underlying_symbol=str(row.get("underlying_symbol", row.get("root", symbol))),
        expiration=_row_date(row, "expiration", "exp", "expiration_date"),
        strike=_required_decimal(row, "strike"),
        option_type=_option_type(row),
        multiplier=_optional_int(row, "multiplier") or 100,
        style=_option_style(row),
    )


def _rows(response: RawResponse) -> list[RawRow]:
    data = response.get("response", response.get("data", []))
    if not isinstance(data, list):
        raise ValueError("ThetaData response data must be a list")
    if not data:
        return []
    if all(isinstance(row, dict) for row in data):
        return [dict(row) for row in data]

    header = response.get("header", response.get("columns"))
    if not isinstance(header, list) or not all(isinstance(column, str) for column in header):
        raise ValueError("ThetaData row arrays require a string header")
    rows: list[RawRow] = []
    for row in data:
        if not isinstance(row, list):
            raise ValueError("ThetaData rows must be objects or arrays")
        rows.append(dict(zip(header, row, strict=True)))
    return rows


def _dataframe_rows(frame: Any) -> list[RawRow]:
    if isinstance(frame, list):
        return [dict(row) for row in frame]
    if hasattr(frame, "to_dicts"):
        rows = frame.to_dicts()
        return [dict(row) for row in rows]
    if hasattr(frame, "to_dict"):
        rows = frame.to_dict(orient="records")
        return [dict(row) for row in rows]
    raise TypeError("ThetaData Python response must be a dataframe or row list")


def _row_timestamp(row: RawRow, fallback_date: date) -> datetime:
    timestamp = row.get("timestamp", row.get("datetime"))
    if timestamp is not None:
        if isinstance(timestamp, datetime):
            return _aware(timestamp)
        if isinstance(timestamp, str):
            return _aware(datetime.fromisoformat(timestamp))

    row_date = _optional_date(row, "date", "trade_date") or fallback_date
    ms_of_day = _optional_int(row, "ms_of_day")
    if ms_of_day is not None:
        return datetime.combine(row_date, time.min, tzinfo=UTC) + timedelta(milliseconds=ms_of_day)
    return datetime.combine(row_date, time.min, tzinfo=UTC)


def _row_date(row: RawRow, *keys: str) -> date:
    result = _optional_date(row, *keys)
    if result is None:
        raise ValueError(f"missing required date field: {keys[0]}")
    return result


def _optional_date(row: RawRow, *keys: str) -> date | None:
    value = _first_present(row, *keys)
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text)


def _option_type(row: RawRow) -> OptionType:
    value = _first_present(row, "option_type", "right", "cp")
    if value is None:
        raise ValueError("missing required option type field")
    normalized = str(value).lower()
    if normalized in {"c", "call"}:
        return OptionType.CALL
    if normalized in {"p", "put"}:
        return OptionType.PUT
    raise ValueError(f"unsupported option type: {value}")


def _option_style(row: RawRow) -> OptionStyle:
    value = _first_present(row, "style", "exercise_style")
    if value is None:
        return OptionStyle.AMERICAN
    return OptionStyle(str(value).lower())


def _required_decimal(row: RawRow, *keys: str) -> Decimal:
    value = _first_present(row, *keys)
    if value is None:
        raise ValueError(f"missing required decimal field: {keys[0]}")
    return Decimal(str(value))


def _optional_decimal(row: RawRow, *keys: str) -> Decimal | None:
    value = _first_present(row, *keys)
    if value is None:
        return None
    return Decimal(str(value))


def _required_int(row: RawRow, *keys: str) -> int:
    value = _first_present(row, *keys)
    if value is None:
        raise ValueError(f"missing required integer field: {keys[0]}")
    return int(value)


def _optional_int(row: RawRow, *keys: str) -> int | None:
    value = _first_present(row, *keys)
    if value is None:
        return None
    return int(value)


def _first_present(row: RawRow, *keys: str) -> Any | None:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _thetadata_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _thetadata_right(option_type: OptionType) -> str:
    match option_type:
        case OptionType.CALL:
            return "C"
        case OptionType.PUT:
            return "P"


def _python_value(key: str, value: str) -> Any:
    if key in {"date", "start_date", "end_date", "expiration"}:
        return _parse_thetadata_date(value)
    return value


def _parse_thetadata_date(value: str) -> date:
    if len(value) == 8 and value.isdigit():
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    return date.fromisoformat(value)
