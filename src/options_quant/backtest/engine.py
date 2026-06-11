"""Event-driven options backtesting engine."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from options_quant.data.models import OptionContract, OptionType, TradeSide

ZERO = Decimal("0")


class BacktestModel(BaseModel):
    """Base configuration for immutable backtest value objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BacktestConfig(BacktestModel):
    """Runtime configuration for the backtest engine."""

    initial_cash: Decimal = Field(gt=ZERO, description="Starting account cash.")
    commission_per_contract: Decimal = Field(
        default=ZERO,
        ge=ZERO,
        description="Commission charged on each traded contract.",
    )
    slippage_per_contract: Decimal = Field(
        default=ZERO,
        ge=ZERO,
        description="Adverse price slippage applied to each contract fill.",
    )


class BacktestOrderType(StrEnum):
    """Supported order intents."""

    OPEN = "open"
    CLOSE = "close"


class ExitReason(StrEnum):
    """Reasons a position can be closed."""

    ORDER = "order"
    EARLY_EXIT = "early_exit"
    EXPIRATION = "expiration"


class BacktestOrderEvent(BacktestModel):
    """Order event emitted by a strategy or test scenario."""

    contract: OptionContract = Field(description="Option contract to trade.")
    side: TradeSide = Field(description="Buy or sell side.")
    quantity: int = Field(gt=0, description="Contract quantity.")
    price: Decimal = Field(ge=ZERO, description="Option price before slippage.")
    event_type: BacktestOrderType = Field(description="Open or close intent.")
    position_id: UUID | None = Field(
        default=None,
        description="Existing position identifier for close events.",
    )

    @model_validator(mode="after")
    def validate_position_reference(self) -> Self:
        """Require close events to name the position being closed."""
        if self.event_type is BacktestOrderType.CLOSE and self.position_id is None:
            raise ValueError("close events require position_id")
        return self


class BacktestMarketEvent(BacktestModel):
    """Daily market data event consumed by the engine."""

    date: dt.date = Field(description="Backtest date.")
    option_marks: dict[OptionContract, Decimal] = Field(
        default_factory=dict,
        description="End-of-day option marks by contract.",
    )
    underlying_prices: dict[str, Decimal] = Field(
        default_factory=dict,
        description="End-of-day underlying prices by symbol.",
    )


class BacktestPosition(BacktestModel):
    """Open option position state."""

    position_id: UUID = Field(description="Unique position identifier.")
    contract: OptionContract = Field(description="Position contract.")
    quantity: int = Field(description="Signed quantity; positive long, negative short.")
    entry_price: Decimal = Field(ge=ZERO, description="Raw entry price before slippage.")
    entry_fill_price: Decimal = Field(ge=ZERO, description="Entry price after slippage.")
    entry_date: dt.date = Field(description="Entry date.")
    entry_cash_flow: Decimal = Field(description="Cash impact of opening this position.")
    entry_commission: Decimal = Field(ge=ZERO, description="Opening commission.")

    @property
    def absolute_quantity(self) -> int:
        """Return absolute contract quantity."""
        return abs(self.quantity)


class ClosedBacktestPosition(BacktestModel):
    """Closed position record with realized PnL."""

    position_id: UUID = Field(description="Closed position identifier.")
    contract: OptionContract = Field(description="Closed contract.")
    quantity: int = Field(description="Signed closed quantity.")
    entry_date: dt.date = Field(description="Entry date.")
    exit_date: dt.date = Field(description="Exit date.")
    entry_fill_price: Decimal = Field(ge=ZERO, description="Entry fill price.")
    exit_fill_price: Decimal = Field(ge=ZERO, description="Exit fill price.")
    realized_pnl: Decimal = Field(description="Realized PnL net of slippage and commissions.")
    exit_reason: ExitReason = Field(description="Reason the position closed.")


class BacktestAccountSnapshot(BacktestModel):
    """Daily account state after orders, exits, and marking positions."""

    date: dt.date = Field(description="Snapshot date.")
    cash_balance: Decimal = Field(description="Cash balance.")
    realized_pnl: Decimal = Field(description="Cumulative realized PnL.")
    unrealized_pnl: Decimal = Field(description="Current unrealized PnL.")
    capital_utilization: Decimal = Field(ge=ZERO, description="Capital in use divided by equity.")
    equity: Decimal = Field(description="Cash plus marked value of open positions.")
    open_positions: tuple[BacktestPosition, ...] = Field(description="Open positions.")


class BacktestResult(BacktestModel):
    """Completed backtest result."""

    snapshots: tuple[BacktestAccountSnapshot, ...] = Field(description="Daily account snapshots.")
    closed_positions: tuple[ClosedBacktestPosition, ...] = Field(
        description="Closed position records."
    )


EarlyExitRule = Callable[[BacktestPosition, BacktestMarketEvent], BacktestOrderEvent | None]


class BacktestEngine:
    """Event-driven daily options backtesting engine."""

    def __init__(
        self,
        config: BacktestConfig,
        *,
        early_exit_rules: list[EarlyExitRule] | None = None,
    ) -> None:
        self._config = config
        self._early_exit_rules = early_exit_rules or []
        self._cash_balance = config.initial_cash
        self._realized_pnl = ZERO
        self._positions: dict[UUID, BacktestPosition] = {}
        self._closed_positions: list[ClosedBacktestPosition] = []

    def run(
        self,
        market_events: list[BacktestMarketEvent],
        orders_by_date: dict[dt.date, list[BacktestOrderEvent]] | None = None,
    ) -> BacktestResult:
        """Process daily market events and return account history."""
        orders = orders_by_date or {}
        snapshots: list[BacktestAccountSnapshot] = []
        for market_event in sorted(market_events, key=lambda event: event.date):
            for order in orders.get(market_event.date, []):
                self._process_order(order, market_event.date, ExitReason.ORDER)
            self._process_early_exits(market_event)
            self._process_expirations(market_event)
            snapshots.append(self._snapshot(market_event))
        return BacktestResult(
            snapshots=tuple(snapshots),
            closed_positions=tuple(self._closed_positions),
        )

    def _process_early_exits(self, market_event: BacktestMarketEvent) -> None:
        for position in tuple(self._positions.values()):
            for rule in self._early_exit_rules:
                order = rule(position, market_event)
                if order is not None:
                    self._process_order(order, market_event.date, ExitReason.EARLY_EXIT)
                    break

    def _process_expirations(self, market_event: BacktestMarketEvent) -> None:
        for position in tuple(self._positions.values()):
            if market_event.date >= position.contract.expiration:
                exit_price = _intrinsic_value(
                    position.contract,
                    market_event.underlying_prices[position.contract.underlying_symbol],
                )
                order = BacktestOrderEvent(
                    contract=position.contract,
                    side=_opposite_side(position.quantity),
                    quantity=position.absolute_quantity,
                    price=exit_price,
                    event_type=BacktestOrderType.CLOSE,
                    position_id=position.position_id,
                )
                self._process_order(order, market_event.date, ExitReason.EXPIRATION)

    def _process_order(
        self,
        order: BacktestOrderEvent,
        trade_date: dt.date,
        exit_reason: ExitReason,
    ) -> None:
        match order.event_type:
            case BacktestOrderType.OPEN:
                self._open_position(order, trade_date)
            case BacktestOrderType.CLOSE:
                self._close_position(order, trade_date, exit_reason)

    def _open_position(self, order: BacktestOrderEvent, trade_date: dt.date) -> None:
        position_id = order.position_id or uuid4()
        signed_quantity = _signed_quantity(order.side, order.quantity)
        fill_price = _fill_price(order.side, order.price, self._config.slippage_per_contract)
        commission = _commission(self._config, order.quantity)
        cash_flow = _trade_cash_flow(order.side, fill_price, order.quantity, order.contract)
        net_cash_flow = cash_flow - commission
        self._cash_balance += net_cash_flow
        self._positions[position_id] = BacktestPosition(
            position_id=position_id,
            contract=order.contract,
            quantity=signed_quantity,
            entry_price=order.price,
            entry_fill_price=fill_price,
            entry_date=trade_date,
            entry_cash_flow=net_cash_flow,
            entry_commission=commission,
        )

    def _close_position(
        self,
        order: BacktestOrderEvent,
        trade_date: dt.date,
        exit_reason: ExitReason,
    ) -> None:
        if order.position_id is None:
            raise ValueError("close events require position_id")
        position = self._positions[order.position_id]
        if order.contract != position.contract:
            raise ValueError("close order contract must match position contract")
        if order.quantity != position.absolute_quantity:
            raise ValueError("close order quantity must fully close the position")
        expected_side = _opposite_side(position.quantity)
        if order.side is not expected_side:
            raise ValueError("close order side must offset the open position")
        fill_price = _fill_price(order.side, order.price, self._config.slippage_per_contract)
        commission = _commission(self._config, order.quantity)
        cash_flow = _trade_cash_flow(order.side, fill_price, order.quantity, order.contract)
        net_cash_flow = cash_flow - commission
        realized_pnl = position.entry_cash_flow + net_cash_flow
        self._cash_balance += net_cash_flow
        self._realized_pnl += realized_pnl
        del self._positions[position.position_id]
        self._closed_positions.append(
            ClosedBacktestPosition(
                position_id=position.position_id,
                contract=position.contract,
                quantity=position.quantity,
                entry_date=position.entry_date,
                exit_date=trade_date,
                entry_fill_price=position.entry_fill_price,
                exit_fill_price=fill_price,
                realized_pnl=realized_pnl,
                exit_reason=exit_reason,
            )
        )

    def _snapshot(self, market_event: BacktestMarketEvent) -> BacktestAccountSnapshot:
        unrealized_pnl = sum(
            (
                _unrealized_pnl(position, _mark_for_position(position, market_event))
                for position in self._positions.values()
            ),
            ZERO,
        )
        marked_value = sum(
            (
                _position_mark_value(position, _mark_for_position(position, market_event))
                for position in self._positions.values()
            ),
            ZERO,
        )
        equity = self._cash_balance + marked_value
        capital_in_use = sum(
            (
                _capital_in_use(position, _mark_for_position(position, market_event))
                for position in self._positions.values()
            ),
            ZERO,
        )
        capital_utilization = ZERO if equity == ZERO else capital_in_use / equity
        return BacktestAccountSnapshot(
            date=market_event.date,
            cash_balance=self._cash_balance,
            realized_pnl=self._realized_pnl,
            unrealized_pnl=unrealized_pnl,
            capital_utilization=capital_utilization,
            equity=equity,
            open_positions=tuple(self._positions.values()),
        )


def _fill_price(side: TradeSide, price: Decimal, slippage: Decimal) -> Decimal:
    match side:
        case TradeSide.BUY:
            return price + slippage
        case TradeSide.SELL:
            return max(ZERO, price - slippage)


def _trade_cash_flow(
    side: TradeSide,
    fill_price: Decimal,
    quantity: int,
    contract: OptionContract,
) -> Decimal:
    notional = fill_price * Decimal(quantity) * Decimal(contract.multiplier)
    match side:
        case TradeSide.BUY:
            return -notional
        case TradeSide.SELL:
            return notional


def _commission(config: BacktestConfig, quantity: int) -> Decimal:
    return config.commission_per_contract * Decimal(quantity)


def _signed_quantity(side: TradeSide, quantity: int) -> int:
    match side:
        case TradeSide.BUY:
            return quantity
        case TradeSide.SELL:
            return -quantity


def _opposite_side(position_quantity: int) -> TradeSide:
    if position_quantity > 0:
        return TradeSide.SELL
    return TradeSide.BUY


def _unrealized_pnl(position: BacktestPosition, mark_price: Decimal) -> Decimal:
    mark_cash_flow = _trade_cash_flow(
        _opposite_side(position.quantity),
        mark_price,
        position.absolute_quantity,
        position.contract,
    )
    return position.entry_cash_flow + mark_cash_flow


def _position_mark_value(position: BacktestPosition, mark_price: Decimal) -> Decimal:
    return mark_price * Decimal(position.quantity) * Decimal(position.contract.multiplier)


def _capital_in_use(position: BacktestPosition, mark_price: Decimal) -> Decimal:
    if position.quantity > 0:
        return (
            mark_price
            * Decimal(position.absolute_quantity)
            * Decimal(position.contract.multiplier)
        )
    return position.contract.strike * Decimal(position.absolute_quantity) * Decimal(
        position.contract.multiplier
    )


def _mark_for_position(position: BacktestPosition, market_event: BacktestMarketEvent) -> Decimal:
    mark = market_event.option_marks.get(position.contract)
    if mark is not None:
        return mark
    if market_event.date >= position.contract.expiration:
        return _intrinsic_value(
            position.contract,
            market_event.underlying_prices[position.contract.underlying_symbol],
        )
    raise ValueError(f"missing option mark for open position {position.position_id}")


def _intrinsic_value(contract: OptionContract, underlying_price: Decimal) -> Decimal:
    match contract.option_type:
        case OptionType.CALL:
            return max(ZERO, underlying_price - contract.strike)
        case OptionType.PUT:
            return max(ZERO, contract.strike - underlying_price)
