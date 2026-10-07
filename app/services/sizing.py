"""Order sizing and rounding to Bybit's lot size / tick size (pure functions)."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.exchange.base import InstrumentRules
    from app.schemas import SizeSpec

_ROUNDING = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_UP}


class SizingError(ValueError):
    """The requested size cannot be turned into a valid order."""


def fmt(value: Decimal) -> str:
    """Plain decimal string for the API: no exponent, no trailing zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round down to a multiple of ``step``. Quantities are never rounded up,
    so rounding can only make an order smaller, never bigger."""
    if step <= 0:
        raise ValueError("step must be positive")
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return (units * step).quantize(step)


def round_to_tick(value: Decimal, tick: Decimal, mode: Literal["down", "up", "nearest"]) -> Decimal:
    if tick <= 0:
        raise ValueError("tick must be positive")
    units = (value / tick).to_integral_value(rounding=_ROUNDING[mode])
    return (units * tick).quantize(tick)


def raw_qty(size: SizeSpec, ref_price: Decimal | None, equity: Decimal | None) -> Decimal:
    """Unrounded base-coin quantity for a size spec."""
    if size.mode == "qty":
        return size.value
    if ref_price is None or ref_price <= 0:
        raise SizingError("reference price unavailable")
    if size.mode == "usd":
        return size.value / ref_price
    if equity is None or equity <= 0:
        raise SizingError("account equity unavailable or zero")
    return equity * size.value / Decimal(100) / ref_price


def finalize_entry_qty(qty: Decimal, rules: InstrumentRules, *, market: bool, ref_price: Decimal) -> Decimal:
    """Round to the lot size and enforce min/max quantity and minimum notional."""
    rounded = floor_to_step(qty, rules.qty_step)
    if rounded <= 0 or rounded < rules.min_qty:
        raise SizingError(
            f"qty {fmt(qty)} rounds to {fmt(rounded)}, below the minimum order qty "
            f"{fmt(rules.min_qty)} for {rules.symbol}"
        )
    max_qty = rules.max_market_qty if market else rules.max_qty
    if max_qty > 0 and rounded > max_qty:
        raise SizingError(f"qty {fmt(rounded)} exceeds the maximum {'market' if market else 'limit'} order qty {fmt(max_qty)}")
    notional = rounded * ref_price
    if rules.min_notional > 0 and notional < rules.min_notional:
        raise SizingError(
            f"order value {notional:.2f} USDT is below the minimum notional {fmt(rules.min_notional)} USDT for {rules.symbol}"
        )
    return rounded


def split_qty(total: Decimal, max_chunk: Decimal) -> list[Decimal]:
    """Split a close into chunks no larger than ``max_chunk`` (0 = no limit)."""
    if max_chunk <= 0 or total <= max_chunk:
        return [total]
    chunks: list[Decimal] = []
    remaining = total
    while remaining > 0:
        chunk = min(remaining, max_chunk)
        chunks.append(chunk)
        remaining -= chunk
    return chunks
