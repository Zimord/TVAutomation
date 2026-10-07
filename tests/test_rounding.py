from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.schemas import SizeSpec
from app.services.sizing import SizingError, finalize_entry_qty, floor_to_step, fmt, raw_qty, round_to_tick, split_qty
from tests.fakes import BTC_RULES


@pytest.mark.parametrize(
    "value, step, expected",
    [
        ("0.0019", "0.001", "0.001"),
        ("0.002", "0.001", "0.002"),
        ("1.23456", "0.01", "1.23"),
        ("5.9", "1", "5"),
        ("0.25", "0.1", "0.2"),
        ("12.7", "0.5", "12.5"),
        ("0.000999", "0.001", "0.000"),
        ("123456.789", "100", "123400"),
    ],
)
def test_floor_to_step_never_rounds_up(value, step, expected):
    result = floor_to_step(D(value), D(step))
    assert result == D(expected)
    assert result <= D(value)
    assert (result / D(step)) == (result / D(step)).to_integral_value()


@pytest.mark.parametrize(
    "value, tick, down, up, nearest",
    [
        ("65000.37", "0.1", "65000.3", "65000.4", "65000.4"),
        ("65000.30", "0.1", "65000.3", "65000.3", "65000.3"),
        ("1.23456", "0.0005", "1.2345", "1.2350", "1.2345"),
        ("0.0123456", "0.00001", "0.01234", "0.01235", "0.01235"),
        ("101", "0.5", "101", "101", "101"),
    ],
)
def test_round_to_tick(value, tick, down, up, nearest):
    assert round_to_tick(D(value), D(tick), "down") == D(down)
    assert round_to_tick(D(value), D(tick), "up") == D(up)
    assert round_to_tick(D(value), D(tick), "nearest") == D(nearest)


@pytest.mark.parametrize(
    "value, expected",
    [(D("1E+2"), "100"), (D("0.00100"), "0.001"), (D("5"), "5"), (D("0E-8"), "0"), (D("65000.10"), "65000.1"), (D("1E-7"), "0.0000001")],
)
def test_fmt_has_no_exponent_or_trailing_zeros(value, expected):
    assert fmt(value) == expected


def test_raw_qty_modes():
    assert raw_qty(SizeSpec(mode="usd", value=D(100)), D(50000), None) == D("0.002")
    assert raw_qty(SizeSpec(mode="percent_equity", value=D(10)), D(50000), D(10000)) == D("0.02")
    assert raw_qty(SizeSpec(mode="qty", value=D("0.5")), None, None) == D("0.5")


def test_raw_qty_needs_price_and_equity():
    with pytest.raises(SizingError):
        raw_qty(SizeSpec(mode="usd", value=D(100)), None, None)
    with pytest.raises(SizingError):
        raw_qty(SizeSpec(mode="percent_equity", value=D(10)), D(50000), D(0))


def test_finalize_rounds_down_to_lot_size():
    # $123 at 50,000 = 0.00246 BTC -> 0.002
    assert finalize_entry_qty(D("0.00246"), BTC_RULES, market=True, ref_price=D(50000)) == D("0.002")


def test_finalize_rejects_below_min_qty():
    with pytest.raises(SizingError, match="below the minimum order qty"):
        finalize_entry_qty(D("0.0009"), BTC_RULES, market=True, ref_price=D(50000))


def test_finalize_rejects_below_min_notional():
    rules = replace(BTC_RULES, min_notional=D("100"))
    with pytest.raises(SizingError, match="minimum notional"):
        finalize_entry_qty(D("0.001"), rules, market=True, ref_price=D(50000))


def test_finalize_market_vs_limit_max_qty():
    with pytest.raises(SizingError, match="maximum market"):
        finalize_entry_qty(D("120"), BTC_RULES, market=True, ref_price=D(50000))
    assert finalize_entry_qty(D("120"), BTC_RULES, market=False, ref_price=D(50000)) == D("120")


def test_split_qty():
    assert split_qty(D("250"), D("119")) == [D("119"), D("119"), D("12")]
    assert split_qty(D("5"), D("119")) == [D("5")]
    assert split_qty(D("5"), D("0")) == [D("5")]
