"""In-memory stand-ins for the Bybit client and the notifier."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
from typing import Any

from app.exchange.base import InstrumentRules, OrderRequest, OrderResult, Position

BTC_RULES = InstrumentRules(
    symbol="BTCUSDT",
    status="Trading",
    qty_step=D("0.001"),
    min_qty=D("0.001"),
    max_qty=D("1190"),
    max_market_qty=D("119"),
    min_notional=D("5"),
    tick_size=D("0.1"),
    min_price=D("0.1"),
    max_price=D("1999999.8"),
    min_leverage=D("1"),
    max_leverage=D("100"),
    leverage_step=D("0.01"),
)


def position(symbol: str = "BTCUSDT", side: str = "long", size: str = "0.01", mark: str = "50000",
             pnl: str = "0", idx: int = 0, leverage: str = "2") -> Position:
    return Position(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        size=D(size),
        avg_price=D(mark),
        mark_price=D(mark),
        leverage=D(leverage),
        unrealized_pnl=D(pnl),
        position_idx=idx,
    )


class FakeExchange:
    environment = "testnet"

    def __init__(self, *, price: str = "50000", positions: list[Position] | None = None, equity: str = "10000",
                 realized_pnl: str = "0", leverage: str | None = "1", authenticated: bool = True):
        self.authenticated = authenticated
        self.price = D(price)
        self.positions = list(positions or [])
        self.equity = D(equity)
        self.realized_pnl = D(realized_pnl)
        self.leverage = D(leverage) if leverage is not None else None
        self.rules: dict[str, InstrumentRules] = {}
        self.placed: list[OrderRequest] = []
        self.leverage_calls: list[tuple[str, D]] = []
        self.place_error: Exception | None = None
        self.order_status = "Filled"
        self.api_key_info: dict[str, Any] = {
            "read_only": False, "withdraw_enabled": False, "ip_restricted": True, "ips": ["203.0.113.10"], "unified": True,
        }

    def get_instrument(self, symbol: str) -> InstrumentRules:
        return self.rules.get(symbol) or replace(BTC_RULES, symbol=symbol)

    def get_last_price(self, symbol: str) -> D:
        return self.price

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def get_leverage(self, symbol: str) -> D | None:
        return self.leverage

    def get_equity_usd(self) -> D:
        return self.equity

    def get_realized_pnl_since(self, since_ms: int) -> D:
        return self.realized_pnl

    def set_leverage(self, symbol: str, leverage: D) -> None:
        self.leverage_calls.append((symbol, leverage))
        self.leverage = leverage

    def place_order(self, request: OrderRequest) -> OrderResult:
        if self.place_error is not None:
            raise self.place_error
        self.placed.append(request)
        filled = request.qty if self.order_status == "Filled" else D(0)
        return OrderResult(
            order_link_id=request.order_link_id,
            order_id=f"oid-{len(self.placed)}",
            status=self.order_status,
            avg_price=self.price if filled else None,
            filled_qty=filled,
        )

    def get_order(self, symbol: str, order_link_id: str) -> OrderResult | None:
        return None

    def check_api_key(self) -> dict[str, Any]:
        return self.api_key_info

    def ping(self) -> bool:
        return True


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)

    def close(self) -> None:
        pass
