"""Exchange-agnostic types. The processor only talks to the ``Exchange`` protocol,
so a spot adapter (or another venue) can be added without touching risk logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

OrderSide = Literal["Buy", "Sell"]
PositionSide = Literal["long", "short"]


@dataclass(frozen=True)
class InstrumentRules:
    """Trading rules from Bybit's instruments-info endpoint."""

    symbol: str
    status: str
    qty_step: Decimal
    min_qty: Decimal
    max_qty: Decimal  # limit orders
    max_market_qty: Decimal
    min_notional: Decimal
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    min_leverage: Decimal
    max_leverage: Decimal
    leverage_step: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    side: PositionSide
    size: Decimal
    avg_price: Decimal
    mark_price: Decimal
    leverage: Decimal | None
    unrealized_pnl: Decimal
    position_idx: int

    @property
    def notional(self) -> Decimal:
        return self.size * self.mark_price


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    order_type: Literal["Market", "Limit"]
    qty: Decimal
    order_link_id: str
    position_idx: int
    price: Decimal | None = None
    reduce_only: bool = False
    take_profit: Decimal | None = None
    stop_loss: Decimal | None = None


@dataclass
class OrderResult:
    order_link_id: str
    order_id: str | None
    status: str  # Bybit orderStatus, "Submitted" if not yet known, or "DryRun"
    avg_price: Decimal | None = None
    filled_qty: Decimal | None = None
    reject_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class ExchangeError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class OrderRejectedError(ExchangeError):
    """The exchange definitively refused the order. Nothing was placed."""


class OrderUncertainError(ExchangeError):
    """We could not confirm whether the order exists. Needs a manual check."""


class Exchange(Protocol):
    environment: str
    authenticated: bool

    def get_instrument(self, symbol: str) -> InstrumentRules: ...
    def get_last_price(self, symbol: str) -> Decimal: ...
    def get_positions(self) -> list[Position]: ...
    def get_leverage(self, symbol: str) -> Decimal | None: ...
    def get_equity_usd(self) -> Decimal: ...
    def get_realized_pnl_since(self, since_ms: int) -> Decimal: ...
    def set_leverage(self, symbol: str, leverage: Decimal) -> None: ...
    def place_order(self, request: OrderRequest) -> OrderResult: ...
    def get_order(self, symbol: str, order_link_id: str) -> OrderResult | None: ...
    def check_api_key(self) -> dict[str, Any]: ...
    def ping(self) -> bool: ...
