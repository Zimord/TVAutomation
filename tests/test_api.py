"""HTTP layer: auth, IP allowlist, validation, idempotency, kill switch and admin endpoints."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.logging_setup import JsonFormatter
from app.main import create_app
from tests.conftest import ADMIN_TOKEN, TV_IP, WEBHOOK_SECRET, make_settings, make_strategies
from tests.fakes import FakeExchange, RecordingNotifier, position

AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


class Env:
    def __init__(self, client: TestClient, exchange: FakeExchange, notifier: RecordingNotifier):
        self.client, self.exchange, self.notifier = client, exchange, notifier

    def post(self, **fields: Any):
        body = {"secret": WEBHOOK_SECRET, "strategy_id": "btc-trend-v1", "symbol": "BTCUSDT", "action": "buy",
                "alert_id": uuid.uuid4().hex}
        body.update(fields)
        return self.client.post("/webhook", content=json.dumps({k: v for k, v in body.items() if v is not ...}))

    def drain(self) -> None:
        self.client.app.state.worker.wait_idle()

    def alerts(self) -> list[dict[str, Any]]:
        return self.client.get("/alerts", headers=AUTH).json()["alerts"]


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    exchange = FakeExchange(positions=[position("ETHUSDT", mark="3000")])
    notifier = RecordingNotifier()
    settings = make_settings(tmp_path, trusted_proxies="172.30.0.0/24")
    app = create_app(settings, strategies=make_strategies(), exchange=exchange, notifier=notifier, configure_logging=False)
    with TestClient(app, client=(TV_IP, 40000)) as client:
        yield Env(client, exchange, notifier)


def test_valid_alert_is_accepted_then_executed(env):
    response = env.post(symbol="BTCUSDT.P")
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    env.drain()
    [alert] = env.alerts()
    assert alert["status"] == "executed" and alert["symbol"] == "BTCUSDT" and alert["source_ip"] == TV_IP
    assert len(env.exchange.placed) == 1


def test_duplicate_alert_is_acknowledged_but_not_executed(env):
    alert_id = "2026-10-07T12:00:00Z-BTCUSDT"
    first = env.post(alert_id=alert_id)
    second = env.post(alert_id=alert_id)
    assert first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json() == {"status": "duplicate", "id": 2, "duplicate_of": 1}
    env.drain()
    assert len(env.exchange.placed) == 1
    assert {a["status"] for a in env.alerts()} == {"executed", "duplicate"}


def test_text_plain_body_is_accepted(env):
    body = json.dumps({"secret": WEBHOOK_SECRET, "strategy_id": "btc-trend-v1", "symbol": "BTCUSDT", "action": "buy", "alert_id": "a1"})
    response = env.client.post("/webhook", content=body, headers={"Content-Type": "text/plain; charset=utf-8"})
    assert response.status_code == 200


@pytest.mark.parametrize("secret", ["wrong-secret-wrong-secret-xx", "", None, 12345, ...])
def test_bad_secret_is_unauthorized_and_never_stored(env, secret):
    response = env.post(secret=secret)
    assert response.status_code == 401 and response.json() == {"detail": "unauthorized"}
    [alert] = env.alerts()
    assert alert["status"] == "unauthorized"
    assert "secret" not in (alert["payload"] or {})
    assert env.exchange.placed == []


def test_valid_secret_is_not_stored(env):
    env.post()
    env.drain()
    dumped = json.dumps(env.alerts())
    assert WEBHOOK_SECRET not in dumped


def test_non_allowlisted_ip_is_forbidden(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings, strategies=make_strategies(), exchange=FakeExchange(), notifier=RecordingNotifier(), configure_logging=False)
    with TestClient(app, client=("203.0.113.9", 40000)) as client:
        response = client.post("/webhook", content=json.dumps({"secret": WEBHOOK_SECRET}))
        assert response.status_code == 403
        # Spoofed X-Forwarded-For from an untrusted peer is ignored.
        response = client.post("/webhook", content="{}", headers={"X-Forwarded-For": TV_IP})
        assert response.status_code == 403


def test_forwarded_ip_trusted_only_from_proxy(tmp_path):
    settings = make_settings(tmp_path, trusted_proxies="172.30.0.0/24")
    app = create_app(settings, strategies=make_strategies(), exchange=FakeExchange(), notifier=RecordingNotifier(), configure_logging=False)
    body = json.dumps({"secret": WEBHOOK_SECRET, "strategy_id": "btc-trend-v1", "symbol": "BTCUSDT", "action": "buy", "alert_id": "x"})
    with TestClient(app, client=("172.30.0.5", 40000)) as client:  # request arrives via Caddy
        assert client.post("/webhook", content=body, headers={"X-Forwarded-For": TV_IP}).status_code == 200
        # Attacker-supplied left-most entry does not help: the right-most non-proxy hop is used.
        assert client.post("/webhook", content=body, headers={"X-Forwarded-For": f"{TV_IP}, 198.51.100.7"}).status_code == 403
        assert client.post("/webhook", content=body).status_code == 403  # proxy itself is not TradingView


def test_allowlist_can_be_disabled_for_local_testing(tmp_path):
    settings = make_settings(tmp_path, webhook_ip_allowlist_enabled=False)
    app = create_app(settings, strategies=make_strategies(), exchange=FakeExchange(), notifier=RecordingNotifier(), configure_logging=False)
    body = json.dumps({"secret": WEBHOOK_SECRET, "strategy_id": "btc-trend-v1", "symbol": "BTCUSDT", "action": "buy", "alert_id": "x"})
    with TestClient(app, client=("127.0.0.1", 40000)) as client:
        assert client.post("/webhook", content=body).status_code == 200


@pytest.mark.parametrize("body", [b"not json", b"[1,2]", b'{"a": NaN}', b""])
def test_malformed_body(env, body):
    response = env.client.post("/webhook", content=body)
    assert response.status_code == 400
    assert env.alerts()[0]["status"] == "invalid"


def test_invalid_payload_returns_field_errors(env):
    response = env.post(order_type="limit", size={"mode": "usd", "value": -1})
    assert response.status_code == 422
    fields = {e["field"] for e in response.json()["detail"]}
    assert "size.value" in fields
    [alert] = env.alerts()
    assert alert["status"] == "invalid" and "size.value" in alert["reason"]
    assert any(m.startswith("INVALID") for m in env.notifier.messages)


def test_oversized_body_rejected(env):
    response = env.post(alert_id="x" * 20000)
    assert response.status_code == 413


def test_admin_endpoints_require_token(env):
    for method, path in [("get", "/health"), ("get", "/positions"), ("get", "/orders"), ("get", "/alerts"), ("post", "/kill"), ("post", "/resume")]:
        assert getattr(env.client, method)(path).status_code == 401
        assert getattr(env.client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert getattr(env.client, method)(path, headers={"Authorization": WEBHOOK_SECRET}).status_code == 401


def test_docs_are_disabled(env):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert env.client.get(path).status_code == 404


def test_health(env):
    body = env.client.get("/health", headers=AUTH).json()
    assert body["status"] == "ok" and body["worker_alive"] is True and body["database"] is True
    assert body["mode"]["label"] == "TESTNET" and body["mode"]["live"] is False
    assert body["kill_switch"]["active"] is False
    assert env.client.get("/health?deep=true", headers=AUTH).json()["exchange_reachable"] is True


def test_positions(env):
    body = env.client.get("/positions", headers=AUTH).json()
    assert body["positions"][0]["symbol"] == "ETHUSDT" and body["positions"][0]["notional_usd"] == "30.00"


def test_orders_listing_and_filters(env):
    env.post()
    env.post(strategy_id="multi", symbol="SOLUSDT")
    env.drain()
    orders = env.client.get("/orders", headers=AUTH).json()["orders"]
    assert [o["symbol"] for o in orders] == ["SOLUSDT", "BTCUSDT"]
    assert all(o["status"] == "Filled" for o in orders)
    filtered = env.client.get("/orders?strategy_id=multi", headers=AUTH).json()["orders"]
    assert [o["symbol"] for o in filtered] == ["SOLUSDT"]
    assert len(env.client.get("/orders?symbol=btcusdt", headers=AUTH).json()["orders"]) == 1


def test_kill_and_resume(env):
    killed = env.client.post("/kill", headers=AUTH, json={"reason": "manual test"}).json()
    assert killed["kill_switch"]["active"] is True and killed["kill_switch"]["reason"] == "manual test"
    assert env.client.post("/kill", headers=AUTH).status_code == 200  # body optional
    assert env.post().status_code == 200  # still acknowledged to TradingView...
    env.drain()
    assert env.alerts()[0]["status"] == "rejected"  # ...but not executed
    assert env.exchange.placed == []
    resumed = env.client.post("/resume", headers=AUTH).json()
    assert resumed["kill_switch"]["active"] is False
    env.post()
    env.drain()
    assert env.alerts()[0]["status"] == "executed"


def test_env_kill_switch_survives_resume(tmp_path):
    settings = make_settings(tmp_path, kill_switch=True)
    app = create_app(settings, strategies=make_strategies(), exchange=FakeExchange(), notifier=RecordingNotifier(), configure_logging=False)
    with TestClient(app, client=(TV_IP, 40000)) as client:
        body = client.post("/resume", headers=AUTH).json()
        assert body["kill_switch"]["active"] is True and "env flag" in body["note"]


def test_queued_alerts_are_abandoned_not_replayed_after_restart(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings, strategies=make_strategies(), exchange=FakeExchange(), notifier=RecordingNotifier(), configure_logging=False)
    # Simulate an alert accepted right before a crash: stored as queued, never processed.
    from app.schemas import WebhookPayload

    payload = WebhookPayload.model_validate({"strategy_id": "btc-trend-v1", "symbol": "BTCUSDT", "action": "buy", "alert_id": "z"})
    app.state.store.accept_alert(payload, TV_IP)
    exchange = FakeExchange()
    notifier = RecordingNotifier()
    app2 = create_app(settings, strategies=make_strategies(), exchange=exchange, notifier=notifier, configure_logging=False)
    with TestClient(app2, client=(TV_IP, 40000)) as client:
        alerts = client.get("/alerts", headers=AUTH).json()["alerts"]
    assert alerts[0]["status"] == "abandoned"
    assert exchange.placed == []
    assert any("NOT executed" in m for m in notifier.messages)


def test_live_mode_refuses_key_with_withdraw_permission(tmp_path):
    from app.config import ConfigError

    exchange = FakeExchange()
    exchange.api_key_info["withdraw_enabled"] = True
    settings = make_settings(tmp_path, bybit_testnet=False)
    assert settings.live
    app = create_app(settings, strategies=make_strategies(), exchange=exchange, notifier=RecordingNotifier(), configure_logging=False)
    with pytest.raises(ConfigError, match="WITHDRAW"), TestClient(app):
        pass


def test_testnet_only_warns_about_unrestricted_key(tmp_path):
    exchange = FakeExchange()
    exchange.api_key_info.update(withdraw_enabled=True, ip_restricted=False)
    notifier = RecordingNotifier()
    app = create_app(make_settings(tmp_path), strategies=make_strategies(), exchange=exchange, notifier=notifier, configure_logging=False)
    with TestClient(app):
        pass
    assert sum("WARNING" in m for m in notifier.messages) == 2


def test_log_formatter_redacts_secrets():
    formatter = JsonFormatter([WEBHOOK_SECRET, ADMIN_TOKEN, "short"])
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "token %s in message", (ADMIN_TOKEN,), None)
    record.payload = {"secret": WEBHOOK_SECRET}
    line = formatter.format(record)
    assert WEBHOOK_SECRET not in line and ADMIN_TOKEN not in line
    entry = json.loads(line)
    assert entry["msg"] == "token ***REDACTED*** in message" and entry["level"] == "INFO"
