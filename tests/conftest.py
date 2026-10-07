from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings, StrategiesFile
from app.db import Database, utcnow
from app.schemas import WebhookPayload
from app.services.killswitch import KillSwitch
from app.services.processor import AlertProcessor, QueuedAlert
from app.store import Store
from tests.fakes import FakeExchange, RecordingNotifier

WEBHOOK_SECRET = "test-webhook-secret-0123456789"
ADMIN_TOKEN = "test-admin-token-0123456789abcdef"
TV_IP = "52.89.214.238"

STRATEGIES: dict[str, Any] = {
    "risk": {
        "allowed_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "max_leverage": 5,
        "max_open_positions": 2,
        "max_daily_loss_usd": 100,
        "max_position_usd": {"default": 1000, "BTCUSDT": 2000},
    },
    "strategies": {
        "btc-trend-v1": {
            "allowed_symbols": ["BTCUSDT"],
            "default_size": {"mode": "usd", "value": 100},
            "default_leverage": 2,
            "max_leverage": 3,
        },
        "multi": {
            "allowed_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            "default_size": {"mode": "usd", "value": 100},
        },
        "disabled": {"enabled": False, "allowed_symbols": ["BTCUSDT"]},
    },
}


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "webhook_secret": WEBHOOK_SECRET,
        "admin_token": ADMIN_TOKEN,
        "database_url": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        "dry_run": False,
        "bybit_testnet": True,
        "bybit_api_key": "test-key",
        "bybit_api_secret": "test-api-secret",
    }
    values.update(overrides)
    return Settings(**values)


def make_strategies(**risk_overrides: Any) -> StrategiesFile:
    data = copy.deepcopy(STRATEGIES)
    data["risk"].update(risk_overrides)
    return StrategiesFile.model_validate(data)


@dataclass
class Harness:
    settings: Settings
    store: Store
    exchange: FakeExchange
    notifier: RecordingNotifier
    kill_switch: KillSwitch
    processor: AlertProcessor
    now: list[datetime] = field(default_factory=lambda: [utcnow()])

    def run(self, **fields: Any) -> tuple[str, dict[str, Any]]:
        """Accept and process one alert; returns (status, stored alert)."""
        data = {"strategy_id": "btc-trend-v1", "symbol": "BTCUSDT", "action": "buy", "alert_id": uuid.uuid4().hex}
        data.update(fields)
        payload = WebhookPayload.model_validate(data)
        accepted = self.store.accept_alert(payload, "test")
        status = self.processor.process(QueuedAlert(accepted.alert_pk, payload, accepted.received_at))
        return status, self.alert(accepted.alert_pk)

    def alert(self, alert_pk: int) -> dict[str, Any]:
        return next(a for a in self.store.list_alerts(limit=500) if a["id"] == alert_pk)


@pytest.fixture
def make_harness(tmp_path: Path) -> Callable[..., Harness]:
    def factory(exchange: FakeExchange | None = None, strategies: StrategiesFile | None = None, **settings_overrides: Any) -> Harness:
        settings = make_settings(tmp_path, **settings_overrides)
        db = Database(settings.database_url)
        db.create_all()
        store = Store(db)
        exchange = exchange or FakeExchange()
        notifier = RecordingNotifier()
        kill_switch = KillSwitch(store, settings.kill_switch)
        harness = Harness(settings, store, exchange, notifier, kill_switch, processor=None)  # type: ignore[arg-type]
        harness.processor = AlertProcessor(
            settings=settings,
            strategies=strategies or make_strategies(),
            exchange=exchange,  # type: ignore[arg-type]
            store=store,
            kill_switch=kill_switch,
            notifier=notifier,
            clock=lambda: harness.now[0],
        )
        return harness

    return factory
