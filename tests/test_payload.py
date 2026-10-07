from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.main import _parse_body
from app.schemas import WebhookPayload, normalize_symbol
from app.services.processor import resolve_intent

BASE = {"strategy_id": "btc-trend-v1", "symbol": "BTCUSDT", "action": "buy", "alert_id": "2026-10-07T12:00:00Z-BTCUSDT"}


def payload(**overrides):
    data = {**BASE, **overrides}
    return WebhookPayload.model_validate({k: v for k, v in data.items() if v is not ...})


def test_minimal_payload_defaults():
    p = payload()
    assert p.order_type == "market"
    assert p.size is None and p.price is None and p.market_position is None
    assert p.dedupe_key == "btc-trend-v1:2026-10-07T12:00:00Z-BTCUSDT"


def test_full_payload():
    p = payload(
        order_type="limit", price=65000.5, size={"mode": "percent_equity", "value": 10},
        take_profit="70000", stop_loss=60000, leverage=3, market_position="long",
    )
    assert p.price == Decimal("65000.5")
    assert p.size.mode == "percent_equity" and p.size.value == Decimal(10)
    assert p.take_profit == Decimal("70000") and p.stop_loss == Decimal("60000")


@pytest.mark.parametrize(
    "raw, expected",
    [("BTCUSDT", "BTCUSDT"), ("BTCUSDT.P", "BTCUSDT"), ("BYBIT:BTCUSDT.P", "BTCUSDT"), (" ethusdt ", "ETHUSDT"), ("1000PEPEUSDT", "1000PEPEUSDT")],
)
def test_symbol_normalization(raw, expected):
    assert normalize_symbol(raw) == expected
    assert payload(symbol=raw).symbol == expected


@pytest.mark.parametrize("bad", ["BTC/USDT", "", "B", 123, None])
def test_invalid_symbol(bad):
    with pytest.raises(ValidationError):
        payload(symbol=bad)


@pytest.mark.parametrize("action", ["BUY", "Sell", "close_long", "CLOSE_SHORT", "close_all"])
def test_action_is_case_insensitive(action):
    assert payload(action=action).action == action.lower()


@pytest.mark.parametrize("action", ["hold", "long", "", None])
def test_invalid_action(action):
    with pytest.raises(ValidationError):
        payload(action=action)


def test_limit_requires_price():
    with pytest.raises(ValidationError, match="price is required"):
        payload(order_type="limit")
    assert payload(order_type="LIMIT", price="100").order_type == "limit"


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        payload(qty=1)
    with pytest.raises(ValidationError):
        payload(size={"mode": "usd", "value": 10, "extra": True})


@pytest.mark.parametrize(
    "size",
    [{"mode": "usd", "value": 0}, {"mode": "usd", "value": -5}, {"mode": "lots", "value": 1}, {"mode": "usd"}, {"value": 1}],
)
def test_invalid_size(size):
    with pytest.raises(ValidationError):
        payload(size=size)


@pytest.mark.parametrize("field", ["price", "take_profit", "stop_loss", "leverage"])
@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity", "abc"])
def test_invalid_numbers(field, value):
    with pytest.raises(ValidationError):
        payload(**{field: value})


def test_blank_optional_fields_are_none():
    p = payload(take_profit="", stop_loss=" ", leverage="", order_type="", market_position="")
    assert p.take_profit is None and p.stop_loss is None and p.leverage is None
    assert p.order_type == "market" and p.market_position is None


@pytest.mark.parametrize("alert_id", ["", "x" * 129, ...])
def test_alert_id_required_and_bounded(alert_id):
    with pytest.raises(ValidationError):
        payload(alert_id=alert_id)


@pytest.mark.parametrize("strategy_id", ["", "has space", "semi;colon", "x" * 65])
def test_strategy_id_pattern(strategy_id):
    with pytest.raises(ValidationError):
        payload(strategy_id=strategy_id)


def test_body_parser_keeps_decimals_exact_and_rejects_nan():
    assert _parse_body(b'{"price": 0.1}') == {"price": Decimal("0.1")}
    assert _parse_body(b'{"price": NaN}') is None
    assert _parse_body(b"[1, 2]") is None
    assert _parse_body(b"not json") is None
    assert _parse_body("ÿ".encode("latin-1")) is None


@pytest.mark.parametrize(
    "action, market_position, size, expected_kind, keeps_size",
    [
        ("buy", None, None, "open_long", True),
        ("buy", "long", None, "open_long", True),
        ("buy", "flat", {"mode": "qty", "value": 1}, "close_short", False),
        ("buy", "short", {"mode": "qty", "value": 1}, "close_short", True),
        ("sell", None, None, "open_short", True),
        ("sell", "short", None, "open_short", True),
        ("sell", "flat", {"mode": "qty", "value": 1}, "close_long", False),
        ("sell", "long", {"mode": "qty", "value": 1}, "close_long", True),
        ("close_long", None, None, "close_long", True),
        ("close_short", "long", None, "close_short", True),
        ("close_all", None, {"mode": "qty", "value": 1}, "close_all", False),
    ],
)
def test_resolve_intent(action, market_position, size, expected_kind, keeps_size):
    p = payload(action=action, market_position=market_position, size=size)
    intent = resolve_intent(p)
    assert intent.kind == expected_kind
    assert intent.size == (p.size if keeps_size else None)
