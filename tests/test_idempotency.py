import re
import threading

from app.db import Database
from app.schemas import WebhookPayload
from app.services.processor import order_link_id
from app.store import Store
from tests.conftest import make_settings


def _payload(**overrides):
    data = {"strategy_id": "btc-trend-v1", "symbol": "BTCUSDT", "action": "buy", "alert_id": "2026-10-07T12:00:00Z-BTCUSDT"}
    data.update(overrides)
    return WebhookPayload.model_validate(data)


def _store(tmp_path):
    db = Database(make_settings(tmp_path).database_url)
    db.create_all()
    return Store(db)


def test_duplicate_alert_id_is_rejected(tmp_path):
    store = _store(tmp_path)
    first = store.accept_alert(_payload(), "1.2.3.4")
    second = store.accept_alert(_payload(action="sell"), "1.2.3.4")  # same id, different body: still a duplicate
    assert first.duplicate_of is None
    assert second.duplicate_of == first.alert_pk
    statuses = {a["id"]: a["status"] for a in store.list_alerts()}
    assert statuses == {first.alert_pk: "queued", second.alert_pk: "duplicate"}


def test_same_alert_id_from_different_strategies_is_not_a_duplicate(tmp_path):
    store = _store(tmp_path)
    assert store.accept_alert(_payload(), None).duplicate_of is None
    assert store.accept_alert(_payload(strategy_id="other"), None).duplicate_of is None


def test_concurrent_deliveries_accept_exactly_one(tmp_path):
    store = _store(tmp_path)
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def deliver():
        barrier.wait()
        accepted = store.accept_alert(_payload(), None)
        with lock:
            results.append(accepted)

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.duplicate_of is None for r in results) == 1
    assert len(results) == 8


def test_duplicates_survive_restart(tmp_path):
    _store(tmp_path).accept_alert(_payload(), None)
    assert _store(tmp_path).accept_alert(_payload(), None).duplicate_of is not None


def test_order_link_id_is_deterministic_and_valid():
    key = _payload().dedupe_key
    a, b = order_link_id(key, "o"), order_link_id(key, "o")
    assert a == b
    for leg in ("o", "xl", "rs", "xl12"):
        link = order_link_id(key, leg)
        assert len(link) <= 36
        assert re.fullmatch(r"[A-Za-z0-9_-]+", link)
    assert order_link_id(key, "o") != order_link_id(key, "xl")
    assert order_link_id(key, "o") != order_link_id(_payload(alert_id="other").dedupe_key, "o")
