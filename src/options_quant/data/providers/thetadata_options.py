"""ThetaData option endpoint access.

This module exposes ThetaData's option-specific Python-library endpoints without
forcing each vendor endpoint into the narrower application market-data provider
interface. Methods intentionally mirror ThetaData's Python client names and
return normalized row dictionaries from Pandas, Polars, or row-list responses.
"""

from __future__ import annotations

from datetime import date, time
from importlib import import_module
from typing import Any, Literal, cast

RawRow = dict[str, Any]
RequestType = Literal["trade", "quote"]


class ThetaDataOptionEndpoints:
    """Facade around ThetaData's Python option endpoints."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        email: str | None = None,
        password: str | None = None,
        creds_file: str | None = None,
        dataframe_type: str = "pandas",
    ) -> None:
        if client is None:
            client = _build_theta_client(
                email=email,
                password=password,
                creds_file=creds_file,
                dataframe_type=dataframe_type,
            )
        self._client = client

    def list_symbols(self) -> list[RawRow]:
        """List all traded option symbols."""
        return self._call("option_list_symbols")

    def list_dates(
        self,
        *,
        request_type: RequestType,
        symbol: str,
        expiration: date,
        strike: str = "*",
        right: str = "both",
    ) -> list[RawRow]:
        """List available data dates for an option request type."""
        return self._call(
            "option_list_dates",
            request_type=request_type,
            symbol=symbol,
            expiration=expiration,
            strike=strike,
            right=right,
        )

    def list_expirations(self, *, symbol: str | list[str]) -> list[RawRow]:
        """List available option expirations for one or more symbols."""
        return self._call("option_list_expirations", symbol=symbol)

    def list_strikes(self, *, symbol: str | list[str], expiration: date) -> list[RawRow]:
        """List available strikes for a symbol and expiration."""
        return self._call("option_list_strikes", symbol=symbol, expiration=expiration)

    def list_contracts(
        self,
        *,
        request_type: RequestType,
        query_date: date,
        symbol: str | list[str] | None = None,
        max_dte: int | None = None,
    ) -> list[RawRow]:
        """List contracts traded or quoted on a date."""
        return self._call(
            "option_list_contracts",
            request_type=request_type,
            date=query_date,
            symbol=symbol,
            max_dte=max_dte,
        )

    def snapshot_ohlc(self, **params: Any) -> list[RawRow]:
        """Return current option OHLC snapshot rows."""
        return self._call("option_snapshot_ohlc", **params)

    def snapshot_trade(self, **params: Any) -> list[RawRow]:
        """Return current option trade snapshot rows."""
        return self._call("option_snapshot_trade", **params)

    def snapshot_quote(self, **params: Any) -> list[RawRow]:
        """Return current option quote snapshot rows."""
        return self._call("option_snapshot_quote", **params)

    def snapshot_open_interest(self, **params: Any) -> list[RawRow]:
        """Return current option open-interest snapshot rows."""
        return self._call("option_snapshot_open_interest", **params)

    def snapshot_market_value(self, **params: Any) -> list[RawRow]:
        """Return current option market-value snapshot rows."""
        return self._call("option_snapshot_market_value", **params)

    def snapshot_greeks_implied_volatility(self, **params: Any) -> list[RawRow]:
        """Return current option implied-volatility snapshot rows."""
        return self._call("option_snapshot_greeks_implied_volatility", **params)

    def snapshot_greeks_all(self, **params: Any) -> list[RawRow]:
        """Return current option full-Greeks snapshot rows."""
        return self._call("option_snapshot_greeks_all", **params)

    def snapshot_greeks_first_order(self, **params: Any) -> list[RawRow]:
        """Return current option first-order Greeks snapshot rows."""
        return self._call("option_snapshot_greeks_first_order", **params)

    def snapshot_greeks_second_order(self, **params: Any) -> list[RawRow]:
        """Return current option second-order Greeks snapshot rows."""
        return self._call("option_snapshot_greeks_second_order", **params)

    def snapshot_greeks_third_order(self, **params: Any) -> list[RawRow]:
        """Return current option third-order Greeks snapshot rows."""
        return self._call("option_snapshot_greeks_third_order", **params)

    def history_eod(self, **params: Any) -> list[RawRow]:
        """Return option end-of-day history rows."""
        return self._call("option_history_eod", **params)

    def history_ohlc(self, **params: Any) -> list[RawRow]:
        """Return option OHLC history rows."""
        return self._call("option_history_ohlc", **params)

    def history_trade(self, **params: Any) -> list[RawRow]:
        """Return option trade history rows."""
        return self._call("option_history_trade", **params)

    def history_quote(self, **params: Any) -> list[RawRow]:
        """Return option quote history rows."""
        return self._call("option_history_quote", **params)

    def history_trade_quote(self, **params: Any) -> list[RawRow]:
        """Return option trade-and-quote history rows."""
        return self._call("option_history_trade_quote", **params)

    def history_open_interest(self, **params: Any) -> list[RawRow]:
        """Return option open-interest history rows."""
        return self._call("option_history_open_interest", **params)

    def history_greeks_eod(self, **params: Any) -> list[RawRow]:
        """Return option end-of-day Greeks history rows."""
        return self._call("option_history_greeks_eod", **params)

    def history_greeks_all(self, **params: Any) -> list[RawRow]:
        """Return option full-Greeks history rows."""
        return self._call("option_history_greeks_all", **params)

    def history_trade_greeks_all(self, **params: Any) -> list[RawRow]:
        """Return option trade-based full-Greeks history rows."""
        return self._call("option_history_trade_greeks_all", **params)

    def history_greeks_first_order(self, **params: Any) -> list[RawRow]:
        """Return option first-order Greeks history rows."""
        return self._call("option_history_greeks_first_order", **params)

    def history_trade_greeks_first_order(self, **params: Any) -> list[RawRow]:
        """Return option trade-based first-order Greeks history rows."""
        return self._call("option_history_trade_greeks_first_order", **params)

    def history_greeks_second_order(self, **params: Any) -> list[RawRow]:
        """Return option second-order Greeks history rows."""
        return self._call("option_history_greeks_second_order", **params)

    def history_trade_greeks_second_order(self, **params: Any) -> list[RawRow]:
        """Return option trade-based second-order Greeks history rows."""
        return self._call("option_history_trade_greeks_second_order", **params)

    def history_greeks_third_order(self, **params: Any) -> list[RawRow]:
        """Return option third-order Greeks history rows."""
        return self._call("option_history_greeks_third_order", **params)

    def history_trade_greeks_third_order(self, **params: Any) -> list[RawRow]:
        """Return option trade-based third-order Greeks history rows."""
        return self._call("option_history_trade_greeks_third_order", **params)

    def history_greeks_implied_volatility(self, **params: Any) -> list[RawRow]:
        """Return option implied-volatility history rows."""
        return self._call("option_history_greeks_implied_volatility", **params)

    def history_trade_greeks_implied_volatility(self, **params: Any) -> list[RawRow]:
        """Return option trade-based implied-volatility history rows."""
        return self._call("option_history_trade_greeks_implied_volatility", **params)

    def at_time_trade(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        time_of_day: time | str,
        expiration: date | str,
        strike: str = "*",
        right: str = "both",
        max_dte: int | None = None,
        strike_range: int | None = None,
    ) -> list[RawRow]:
        """Return option trade rows at a specific time of day."""
        return self._call(
            "option_at_time_trade",
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            time_of_day=time_of_day,
            expiration=expiration,
            strike=strike,
            right=right,
            max_dte=max_dte,
            strike_range=strike_range,
        )

    def at_time_quote(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        time_of_day: time | str,
        expiration: date | str,
        strike: str = "*",
        right: str = "both",
        max_dte: int | None = None,
        strike_range: int | None = None,
    ) -> list[RawRow]:
        """Return option quote rows at a specific time of day."""
        return self._call(
            "option_at_time_quote",
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            time_of_day=time_of_day,
            expiration=expiration,
            strike=strike,
            right=right,
            max_dte=max_dte,
            strike_range=strike_range,
        )

    def _call(self, method_name: str, **params: Any) -> list[RawRow]:
        method = getattr(self._client, method_name)
        response = method(**_drop_none(params))
        return _rows_from_frame(response)


def _build_theta_client(
    *,
    email: str | None,
    password: str | None,
    creds_file: str | None,
    dataframe_type: str,
) -> Any:
    theta_module = cast(Any, import_module("thetadata"))
    client_kwargs: dict[str, str] = {"dataframe_type": dataframe_type}
    if email is not None:
        client_kwargs["email"] = email
    if password is not None:
        client_kwargs["password"] = password
    if creds_file is not None:
        client_kwargs["creds_file"] = creds_file
    return theta_module.ThetaClient(**client_kwargs)


def _drop_none(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


def _rows_from_frame(frame: Any) -> list[RawRow]:
    if isinstance(frame, list):
        return [dict(row) for row in frame]
    if hasattr(frame, "to_dicts"):
        rows = frame.to_dicts()
        return [dict(row) for row in rows]
    if hasattr(frame, "to_dict"):
        rows = frame.to_dict(orient="records")
        return [dict(row) for row in rows]
    raise TypeError("ThetaData option endpoint response must be a dataframe or row list")
