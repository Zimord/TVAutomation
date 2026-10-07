"""Bybit V5 adapter built on pybit (official SDK).

Retry policy
------------
* Reads and idempotent writes (set-leverage) are retried with exponential
  backoff on network errors and transient retCodes.
* Order placement is never blindly retried. Every order carries a deterministic
  ``orderLinkId``. When the outcome of a request is unknown (timeout, connection
  reset, server error), we first look the order up by ``orderLinkId``; only if
  it does not exist do we resend, with the SAME ``orderLinkId``, so Bybit
  rejects any second copy as a duplicate (retCode 110072). Requests Bybit
  rejected outright (rate limit, recv_window) are safe to resend.

Note that pybit itself already retries retCodes 10002/10006 internally (those
are rejected-before-processing, so safe) and does NOT retry network errors
unless ``force_retry=True``, which we leave off.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import requests
from pybit.exceptions import FailedRequestError, InvalidRequestError
from pybit.unified_trading import HTTP

from app.exchange.base import (
    ExchangeError,
    InstrumentRules,
    OrderRejectedError,
    OrderRequest,
    OrderResult,
    OrderUncertainError,
    Position,
)
from app.services.sizing import fmt

log = logging.getLogger(__name__)

# Bybit rejected the request without acting on it: always safe to resend.
RETRY_SAFE_CODES = frozenset({10002, 10006, 10018})  # recv_window, rate limit, IP rate limit
# Server-side timeout / internal error: the request may or may not have been applied.
AMBIGUOUS_CODES = frozenset({10000, 10016})
DUPLICATE_ORDER_LINK_ID = 110072
LEVERAGE_NOT_MODIFIED = 110043
TERMINAL_ORDER_STATUSES = frozenset(
    {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}
)
TRANSPORT_ERRORS = (FailedRequestError, requests.exceptions.RequestException)


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _describe(exc: BaseException) -> str:
    if isinstance(exc, FailedRequestError | InvalidRequestError):
        return f"{exc.message} (code {exc.status_code})"
    return f"{type(exc).__name__}: {exc}"


def parse_linear_instrument(item: dict[str, Any]) -> InstrumentRules:
    lot = item.get("lotSizeFilter") or {}
    price = item.get("priceFilter") or {}
    lev = item.get("leverageFilter") or {}
    max_qty = _dec(lot.get("maxOrderQty"))
    rules = InstrumentRules(
        symbol=item["symbol"],
        status=item.get("status", ""),
        qty_step=_dec(lot.get("qtyStep")),
        min_qty=_dec(lot.get("minOrderQty")),
        max_qty=max_qty,
        max_market_qty=_dec(lot.get("maxMktOrderQty"), str(max_qty)),
        min_notional=_dec(lot.get("minNotionalValue")),
        tick_size=_dec(price.get("tickSize")),
        min_price=_dec(price.get("minPrice")),
        max_price=_dec(price.get("maxPrice")),
        min_leverage=_dec(lev.get("minLeverage"), "1"),
        max_leverage=_dec(lev.get("maxLeverage"), "1"),
        leverage_step=_dec(lev.get("leverageStep"), "0.01"),
    )
    if rules.qty_step <= 0 or rules.tick_size <= 0:
        raise ExchangeError(f"instrument {rules.symbol} has invalid qtyStep/tickSize")
    return rules


def parse_position(item: dict[str, Any]) -> Position | None:
    size = _dec(item.get("size"))
    side = {"Buy": "long", "Sell": "short"}.get(item.get("side", ""))
    if size <= 0 or side is None:
        return None
    return Position(
        symbol=item["symbol"],
        side=side,  # type: ignore[arg-type]
        size=size,
        avg_price=_dec(item.get("avgPrice")),
        mark_price=_dec(item.get("markPrice")),
        leverage=_dec_or_none(item.get("leverage")),
        unrealized_pnl=_dec(item.get("unrealisedPnl")),
        position_idx=int(item.get("positionIdx") or 0),
    )


def parse_order(item: dict[str, Any]) -> OrderResult:
    reject = item.get("rejectReason")
    keep = (
        "orderId", "orderLinkId", "symbol", "side", "orderType", "price", "qty", "orderStatus",
        "avgPrice", "cumExecQty", "cumExecValue", "rejectReason", "cancelType", "createdTime", "updatedTime",
    )
    return OrderResult(
        order_link_id=item.get("orderLinkId", ""),
        order_id=item.get("orderId"),
        status=item.get("orderStatus") or "Unknown",
        avg_price=_dec_or_none(item.get("avgPrice")) or None,  # Bybit uses "0" for "no fills yet"
        filled_qty=_dec_or_none(item.get("cumExecQty")),
        reject_reason=None if reject in (None, "", "EC_NoError") else reject,
        raw={k: item[k] for k in keep if k in item},
    )


class BybitClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        api_secret: str | None,
        testnet: bool,
        demo: bool = False,
        category: str = "linear",
        settle_coin: str = "USDT",
        recv_window: int = 5000,
        timeout: int = 10,
        max_attempts: int = 4,
        instrument_ttl_seconds: float = 3600,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if category != "linear":
            # Spot needs: basePrecision instead of qtyStep, marketUnit for market
            # buys, balances instead of positions, and no leverage.
            raise NotImplementedError("only category=linear (USDT perpetuals) is implemented")
        self.category = category
        self.settle_coin = settle_coin
        self.environment = "testnet" if testnet else ("demo" if demo else "mainnet")
        self.authenticated = bool(api_key and api_secret)
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._instrument_ttl = instrument_ttl_seconds
        self._instruments: dict[str, tuple[float, InstrumentRules]] = {}
        self._session = session or HTTP(
            testnet=testnet,
            demo=demo,
            api_key=api_key or None,
            api_secret=api_secret or None,
            recv_window=recv_window,
            timeout=timeout,
            log_requests=False,
            logging_level=logging.WARNING,
        )

    # ---- plumbing -----------------------------------------------------------

    def _backoff(self, attempt: int) -> None:
        delay = min(8.0, 0.5 * 2 ** (attempt - 1))
        self._sleep(delay * random.uniform(0.8, 1.2))

    def _require_auth(self) -> None:
        if not self.authenticated:
            raise ExchangeError("Bybit API credentials are not configured")

    def _call(self, method: str, *, attempts: int | None = None, **params: Any) -> dict[str, Any]:
        """Call an idempotent endpoint, retrying transient failures."""
        attempts = attempts or self.max_attempts
        fn = getattr(self._session, method)
        for attempt in range(1, attempts + 1):
            try:
                return fn(**params).get("result") or {}
            except InvalidRequestError as exc:
                transient = exc.status_code in RETRY_SAFE_CODES or exc.status_code in AMBIGUOUS_CODES
                if not transient or attempt == attempts:
                    raise ExchangeError(f"{method}: {exc.message}", code=exc.status_code) from exc
                log.warning("bybit transient error, retrying", extra={"method": method, "error": _describe(exc), "attempt": attempt})
            except TRANSPORT_ERRORS as exc:
                if attempt == attempts:
                    raise ExchangeError(f"{method} failed after {attempt} attempt(s): {_describe(exc)}") from exc
                log.warning("bybit request failed, retrying", extra={"method": method, "error": _describe(exc), "attempt": attempt})
            self._backoff(attempt)
        raise AssertionError("unreachable")

    # ---- market data ----------------------------------------------------------

    def get_instrument(self, symbol: str) -> InstrumentRules:
        now = time.monotonic()
        cached = self._instruments.get(symbol)
        if cached and now - cached[0] < self._instrument_ttl:
            return cached[1]
        result = self._call("get_instruments_info", category=self.category, symbol=symbol)
        items = result.get("list") or []
        if not items:
            raise ExchangeError(f"unknown instrument {symbol} on Bybit {self.environment}")
        rules = parse_linear_instrument(items[0])
        self._instruments[symbol] = (now, rules)
        return rules

    def get_last_price(self, symbol: str) -> Decimal:
        items = self._call("get_tickers", category=self.category, symbol=symbol).get("list") or []
        if not items:
            raise ExchangeError(f"no ticker for {symbol}")
        price = _dec(items[0].get("lastPrice"))
        if price <= 0:
            raise ExchangeError(f"invalid last price for {symbol}")
        return price

    def ping(self) -> bool:
        try:
            self._call("get_server_time", attempts=1)
            return True
        except ExchangeError:
            return False

    # ---- account --------------------------------------------------------------

    def get_positions(self) -> list[Position]:
        """All open positions settled in ``settle_coin`` (linear requires symbol or settleCoin)."""
        self._require_auth()
        positions: list[Position] = []
        cursor = ""
        for _ in range(50):  # pagination guard
            params: dict[str, Any] = {"category": self.category, "settleCoin": self.settle_coin, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            result = self._call("get_positions", **params)
            positions.extend(p for p in map(parse_position, result.get("list") or []) if p)
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        return positions

    def get_leverage(self, symbol: str) -> Decimal | None:
        self._require_auth()
        items = self._call("get_positions", category=self.category, symbol=symbol).get("list") or []
        for item in items:
            leverage = _dec_or_none(item.get("leverage"))
            if leverage:
                return leverage
        return None

    def get_equity_usd(self) -> Decimal:
        self._require_auth()
        # Only accountType=UNIFIED is supported by Bybit V5 now (classic accounts are not).
        items = self._call("get_wallet_balance", accountType="UNIFIED").get("list") or []
        if not items:
            raise ExchangeError("wallet balance unavailable (is this a Unified Trading Account?)")
        return _dec(items[0].get("totalEquity"))

    def get_realized_pnl_since(self, since_ms: int) -> Decimal:
        """Sum of closed PnL since ``since_ms`` (window must be <= 7 days)."""
        self._require_auth()
        total = Decimal(0)
        cursor = ""
        end_ms = int(time.time() * 1000)
        for _ in range(100):
            params: dict[str, Any] = {"category": self.category, "startTime": since_ms, "endTime": end_ms, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = self._call("get_closed_pnl", **params)
            total += sum((_dec(i.get("closedPnl")) for i in result.get("list") or []), Decimal(0))
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        return total

    def check_api_key(self) -> dict[str, Any]:
        self._require_auth()
        info = self._call("get_api_key_information")
        permissions = info.get("permissions") or {}
        ips = info.get("ips") or []
        return {
            "read_only": str(info.get("readOnly")) == "1",
            "withdraw_enabled": "Withdraw" in (permissions.get("Wallet") or []),
            "ip_restricted": bool(ips) and "*" not in ips,
            "ips": ips,
            "unified": info.get("uta") in (1, "1", None),
            "permissions": permissions,
        }

    # ---- trading --------------------------------------------------------------

    def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        self._require_auth()
        value = fmt(leverage)
        try:
            self._call("set_leverage", category=self.category, symbol=symbol, buyLeverage=value, sellLeverage=value)
        except ExchangeError as exc:
            if exc.code != LEVERAGE_NOT_MODIFIED:
                raise

    def get_order(self, symbol: str, order_link_id: str, *, attempts: int | None = None) -> OrderResult | None:
        """Find an order by orderLinkId. The realtime endpoint only keeps the most
        recent ~500 closed orders (and is cleared on Bybit restarts), so fall back
        to order history."""
        self._require_auth()
        for method in ("get_open_orders", "get_order_history"):
            result = self._call(method, attempts=attempts, category=self.category, symbol=symbol, orderLinkId=order_link_id)
            items = result.get("list") or []
            if items:
                return parse_order(items[0])
        return None

    def _order_params(self, req: OrderRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "category": self.category,
            "symbol": req.symbol,
            "side": req.side,
            "orderType": req.order_type,
            "qty": fmt(req.qty),
            "orderLinkId": req.order_link_id,
            "positionIdx": req.position_idx,
        }
        if req.order_type == "Limit":
            if req.price is None:
                raise ValueError("limit order without price")
            params["price"] = fmt(req.price)
            params["timeInForce"] = "GTC"
        if req.reduce_only:
            params["reduceOnly"] = True
        if req.take_profit is not None or req.stop_loss is not None:
            params["tpslMode"] = "Full"
            if req.take_profit is not None:
                params["takeProfit"] = fmt(req.take_profit)
            if req.stop_loss is not None:
                params["stopLoss"] = fmt(req.stop_loss)
        return params

    def _lookup_quietly(self, req: OrderRequest) -> OrderResult | None:
        try:
            return self.get_order(req.symbol, req.order_link_id, attempts=1)
        except ExchangeError as exc:
            log.warning("order lookup failed", extra={"order_link_id": req.order_link_id, "error": str(exc)})
            return None

    def _confirm(self, req: OrderRequest, order_id: str | None) -> OrderResult:
        """Bybit's create-order is asynchronous; poll briefly for the resulting state."""
        result = OrderResult(order_link_id=req.order_link_id, order_id=order_id, status="Submitted")
        polls = 4 if req.order_type == "Market" else 1
        for _ in range(polls):
            self._sleep(0.5)
            found = self._lookup_quietly(req)
            if found is not None:
                result = found
                if found.status in TERMINAL_ORDER_STATUSES:
                    break
        return result

    def place_order(self, req: OrderRequest) -> OrderResult:
        self._require_auth()
        params = self._order_params(req)
        last_error = "no attempt made"
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._session.place_order(**params)
            except InvalidRequestError as exc:
                code = exc.status_code
                if code == DUPLICATE_ORDER_LINK_ID:
                    # A previous attempt already created this order.
                    existing = self._lookup_quietly(req)
                    if existing is not None:
                        log.warning("order already existed (duplicate orderLinkId)", extra={"order_link_id": req.order_link_id})
                        return existing
                    raise OrderUncertainError(
                        f"Bybit reports orderLinkId {req.order_link_id} already exists but it could not be fetched", code=code
                    ) from exc
                if code in RETRY_SAFE_CODES:
                    last_error = _describe(exc)
                    if attempt == self.max_attempts:
                        raise OrderRejectedError(f"gave up after repeated rejections: {last_error}", code=code) from exc
                    log.warning("order rejected transiently, resending", extra={"order_link_id": req.order_link_id, "error": last_error})
                    self._backoff(attempt)
                    continue
                if code not in AMBIGUOUS_CODES:
                    raise OrderRejectedError(f"{exc.message} (retCode {code})", code=code) from exc
                last_error = _describe(exc)
            except TRANSPORT_ERRORS as exc:
                last_error = _describe(exc)
            else:
                return self._confirm(req, (response.get("result") or {}).get("orderId"))

            # Outcome unknown: the order may or may not have reached the matching engine.
            log.warning(
                "order outcome unknown, reconciling by orderLinkId",
                extra={"order_link_id": req.order_link_id, "error": last_error, "attempt": attempt},
            )
            self._backoff(attempt)
            existing = self._lookup_quietly(req)
            if existing is not None:
                return existing
            # Not found, so resend with the same orderLinkId. If the first request
            # does land later, Bybit rejects whichever copy arrives second.

        raise OrderUncertainError(
            f"could not confirm order {req.order_link_id} after {self.max_attempts} attempts "
            f"({last_error}); check Bybit manually"
        )
