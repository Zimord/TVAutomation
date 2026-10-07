#!/usr/bin/env python3
"""Send sample TradingView-style webhooks to a running instance and report what happened.

Exercises the whole pipeline: auth, validation, idempotency, ticker normalisation,
risk checks, entries, reversal-free exits and order placement. Intended for a local
instance pointed at Bybit TESTNET (or in DRY_RUN). Standard library only.

    # terminal 1 (see README "End-to-end test against testnet")
    uvicorn app.main:create_app --factory --port 8000
    # terminal 2
    python scripts/send_test_webhooks.py --strategy btc-trend-v1 --symbol BTCUSDT --usd 100

Refuses to run against a LIVE instance unless --allow-live is given.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BYBIT_PUBLIC = {"testnet": "https://api-testnet.bybit.com", "demo": "https://api.bybit.com", "mainnet": "https://api.bybit.com"}


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode(errors="replace")


def last_price(environment: str, symbol: str) -> float | None:
    url = f"{BYBIT_PUBLIC[environment]}/v5/market/tickers?category=linear&symbol={symbol}"
    try:
        status, body = http("GET", url)
        return float(body["result"]["list"][0]["lastPrice"]) if status == 200 else None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--strategy", default="btc-trend-v1")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--usd", type=float, default=100, help="notional per test entry (must clear Bybit's minimum order size)")
    parser.add_argument("--delay", type=float, default=3, help="seconds to wait between steps")
    parser.add_argument("--secret", help="defaults to WEBHOOK_SECRET from the environment or .env")
    parser.add_argument("--admin-token", help="defaults to ADMIN_TOKEN from the environment or .env")
    parser.add_argument("--allow-live", action="store_true", help="permit running against a LIVE instance (real money)")
    args = parser.parse_args()

    env = {**load_dotenv(Path(".env")), **os.environ}
    secret = args.secret or env.get("WEBHOOK_SECRET")
    admin = args.admin_token or env.get("ADMIN_TOKEN")
    if not secret or not admin:
        print("WEBHOOK_SECRET and ADMIN_TOKEN are required (env, .env, or --secret/--admin-token)")
        return 2
    auth = {"Authorization": f"Bearer {admin}"}
    base = args.base_url.rstrip("/")

    status, health = http("GET", f"{base}/health", headers=auth)
    if status != 200:
        print(f"GET /health failed ({status}): {health}")
        return 1
    mode = health["mode"]
    print(f"Instance mode: {mode['label']}  (kill switch active: {health['kill_switch']['active']})")
    if mode["live"] and not args.allow_live:
        print("Refusing to send test orders to a LIVE instance. Use --allow-live if you really mean it.")
        return 1
    if mode["dry_run"]:
        print("DRY_RUN is on: orders are simulated, so close steps will be 'skipped' (no real position).")

    run = uuid.uuid4().hex[:8]
    counter = iter(range(1, 1000))
    size = {"mode": "usd", "value": args.usd}

    def alert(action: str, **extra: Any) -> dict[str, Any]:
        body = {"secret": secret, "strategy_id": args.strategy, "symbol": args.symbol, "action": action,
                "order_type": "market", "alert_id": f"e2e-{run}-{next(counter)}"}
        body.update(extra)
        return body

    price = last_price(mode["environment"], args.symbol)
    first = alert("buy", size=size)
    steps: list[tuple[str, Any, int]] = [
        ("open long (market, usd size)", first, 200),
        ("resend the same alert -> duplicate, ignored", first, 200),
        ("wrong secret -> 401", {**alert("buy", size=size), "secret": "definitely-not-the-secret"}, 401),
        ("limit order without price -> 422", alert("buy", order_type="limit"), 422),
        ("non-JSON body -> 400", b"buy BTCUSDT", 400),
    ]
    if price:
        # Marketable limit (above the ask) with an unrounded price: fills now, exercises tick rounding.
        steps.append(("marketable limit buy, unrounded price (adds to long)",
                      alert("buy", order_type="limit", price=round(price * 1.0053719, 7), size=size), 200))
    steps += [
        ("strategy-style exit: sell + market_position=flat, TradingView ticker '.P' -> close long",
         alert("sell", symbol=f"{args.symbol}.P", market_position="flat"), 200),
        ("open short", alert("sell", size=size), 200),
        ("close_short", alert("close_short"), 200),
        ("close_all with nothing open -> skipped", alert("close_all"), 200),
        ("symbol outside the whitelist -> rejected by risk checks", alert("buy", symbol="NOTAREALUSDT", size=size), 200),
    ]

    failures = 0
    for label, body, expected in steps:
        status, response = http("POST", f"{base}/webhook", body)
        ok = status == expected
        failures += not ok
        print(f"[{'ok' if ok else 'UNEXPECTED'}] {label}: HTTP {status} {json.dumps(response)[:160]}")
        if status == 403:
            print("    403 = source IP not allowlisted. For local testing set WEBHOOK_IP_ALLOWLIST_ENABLED=false.")
            return 1
        time.sleep(args.delay)

    time.sleep(args.delay)
    _, alerts = http("GET", f"{base}/alerts?limit={len(steps) + 2}", headers=auth)
    print("\nAlerts (newest first):")
    for a in alerts.get("alerts", []):
        if (a.get("alert_id") or "").startswith(f"e2e-{run}") or a["status"] in ("unauthorized", "invalid"):
            print(f"  #{a['id']:<5} {a['status']:<12} {a.get('action') or '-':<12} {(a.get('reason') or '')[:110]}")
    _, orders = http("GET", f"{base}/orders?limit=10", headers=auth)
    print("\nRecent orders:")
    for o in orders.get("orders", []):
        print(f"  {o['created_at']} {o['leg']:<13} {o['side']:<4} {o['qty']:>10} {o['symbol']:<12} {o['status']:<10} "
              f"avg={o['avg_price']} link={o['order_link_id']}")
    _, positions = http("GET", f"{base}/positions", headers=auth)
    print(f"\nOpen positions: {json.dumps(positions.get('positions', []))}")
    print(f"\n{'All HTTP responses as expected.' if not failures else f'{failures} unexpected HTTP response(s).'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
