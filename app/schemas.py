"""Webhook payload schema and shared value types."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STRATEGY_ID_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}$")

Action = Literal["buy", "sell", "close_long", "close_short", "close_all"]
SizeMode = Literal["usd", "percent_equity", "qty"]


def normalize_symbol(raw: str) -> str:
    """Map a TradingView ticker to a Bybit symbol.

    TradingView lists Bybit USDT perpetuals as ``BYBIT:BTCUSDT.P``, so
    ``{{ticker}}`` renders as ``BTCUSDT.P``. Bybit expects ``BTCUSDT``.
    """
    symbol = raw.strip().upper()
    if ":" in symbol:
        symbol = symbol.split(":", 1)[1]
    if symbol.endswith(".P"):
        symbol = symbol[:-2]
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"invalid symbol {raw!r}")
    return symbol


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class SizeSpec(BaseModel):
    """How big an order should be.

    * ``usd``: notional value in USDT (not margin), e.g. 100 = $100 of BTC.
    * ``percent_equity``: notional as a percent of account equity (200 = 2x equity).
    * ``qty``: base-coin quantity, e.g. 0.01 BTC.
    """

    model_config = ConfigDict(extra="forbid")

    mode: SizeMode
    value: Decimal = Field(gt=0, allow_inf_nan=False)

    @field_validator("mode", mode="before")
    @classmethod
    def _lower_mode(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


class WebhookPayload(BaseModel):
    """Alert body, minus the ``secret`` (checked and stripped before validation)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    strategy_id: str = Field(pattern=STRATEGY_ID_PATTERN)
    symbol: str
    action: Action
    order_type: Literal["market", "limit"] = "market"
    price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    size: SizeSpec | None = None
    take_profit: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    stop_loss: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    leverage: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    alert_id: str = Field(min_length=1, max_length=128)
    # Optional: TradingView's {{strategy.market_position}} (position *after* the
    # order). Lets a single strategy alert distinguish exits from entries.
    market_position: Literal["long", "short", "flat"] | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("symbol must be a string")
        return normalize_symbol(value)

    @field_validator("action", "market_position", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        value = _blank_to_none(value)
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("order_type", mode="before")
    @classmethod
    def _order_type(cls, value: Any) -> Any:
        value = _blank_to_none(value)
        if value is None:
            return "market"
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("price", "take_profit", "stop_loss", "leverage", "size", mode="before")
    @classmethod
    def _blank_optional(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _check_consistency(self) -> WebhookPayload:
        if self.order_type == "limit" and self.price is None:
            raise ValueError("price is required for limit orders")
        return self

    @property
    def dedupe_key(self) -> str:
        """Idempotency key. Scoped per strategy so two strategies firing in the
        same second on the same ticker do not collide."""
        return f"{self.strategy_id}:{self.alert_id}"
