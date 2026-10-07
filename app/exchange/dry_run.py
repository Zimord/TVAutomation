"""DRY_RUN wrapper: real reads, simulated writes.

Market data (instrument rules, prices) always comes from Bybit's public API so
rounding and sizing are realistic. Account data is used when API keys are
configured; otherwise the account is assumed flat with ``fallback_equity``.
Nothing that changes exchange state is ever sent.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.exchange.base import InstrumentRules, OrderRequest, OrderResult, Position
from app.exchange.bybit import BybitClient

log = logging.getLogger(__name__)


class DryRunExchange:
    def __init__(self, inner: BybitClient, fallback_equity: Decimal):
        self.inner = inner
        self.environment = inner.environment
        self.authenticated = inner.authenticated
        self.fallback_equity = fallback_equity

    def get_instrument(self, symbol: str) -> InstrumentRules:
        return self.inner.get_instrument(symbol)

    def get_last_price(self, symbol: str) -> Decimal:
        return self.inner.get_last_price(symbol)

    def get_positions(self) -> list[Position]:
        return self.inner.get_positions() if self.authenticated else []

    def get_leverage(self, symbol: str) -> Decimal | None:
        return self.inner.get_leverage(symbol) if self.authenticated else None

    def get_equity_usd(self) -> Decimal:
        return self.inner.get_equity_usd() if self.authenticated else self.fallback_equity

    def get_realized_pnl_since(self, since_ms: int) -> Decimal:
        return self.inner.get_realized_pnl_since(since_ms) if self.authenticated else Decimal(0)

    def check_api_key(self) -> dict[str, Any]:
        return self.inner.check_api_key()

    def ping(self) -> bool:
        return self.inner.ping()

    def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        log.info("DRY RUN: would set leverage", extra={"symbol": symbol, "leverage": str(leverage)})

    def place_order(self, request: OrderRequest) -> OrderResult:
        log.info(
            "DRY RUN: would place order",
            extra={
                "symbol": request.symbol,
                "side": request.side,
                "order_type": request.order_type,
                "qty": str(request.qty),
                "price": str(request.price) if request.price is not None else None,
                "reduce_only": request.reduce_only,
                "order_link_id": request.order_link_id,
            },
        )
        return OrderResult(order_link_id=request.order_link_id, order_id=None, status="DryRun")

    def get_order(self, symbol: str, order_link_id: str) -> OrderResult | None:
        return None
