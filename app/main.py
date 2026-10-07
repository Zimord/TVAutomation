"""FastAPI app: TradingView webhook intake plus an admin API.

Run with:  uvicorn app.main:create_app --factory
(one worker process only: the alert queue and kill switch live in-process)
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from app import __version__
from app.config import Settings, StrategiesFile, load_settings, load_strategies
from app.db import Database
from app.exchange import build_exchange
from app.exchange.base import Exchange, ExchangeError
from app.logging_setup import setup_logging
from app.schemas import WebhookPayload
from app.security import admin_auth, client_ip, ip_allowed, secrets_equal
from app.services.killswitch import KillSwitch
from app.services.notifier import Notifier, build_notifier
from app.services.processor import AlertProcessor, QueuedAlert
from app.services.startup import reconcile_unconfirmed_orders, verify_api_key
from app.services.worker import AlertWorker
from app.store import Store

log = logging.getLogger("app")


class KillRequest(BaseModel):
    reason: str = Field(default="manual kill via API", max_length=500)


def _no_json_constants(name: str) -> Any:
    raise ValueError(f"{name} is not valid in a payload")


def _parse_body(body: bytes) -> dict[str, Any] | None:
    # parse_float=Decimal keeps prices/sizes exact (0.1 stays 0.1, not 0.1000000000000000055).
    try:
        data = json.loads(body, parse_float=Decimal, parse_constant=_no_json_constants)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _position_dict(p: Any) -> dict[str, Any]:
    return {
        "symbol": p.symbol,
        "side": p.side,
        "size": str(p.size),
        "avg_price": str(p.avg_price),
        "mark_price": str(p.mark_price),
        "notional_usd": f"{p.notional:.2f}",
        "leverage": str(p.leverage) if p.leverage is not None else None,
        "unrealized_pnl": str(p.unrealized_pnl),
        "position_idx": p.position_idx,
    }


def create_app(
    settings: Settings | None = None,
    *,
    strategies: StrategiesFile | None = None,
    exchange: Exchange | None = None,
    notifier: Notifier | None = None,
    configure_logging: bool = True,
) -> FastAPI:
    settings = settings or load_settings()
    if configure_logging:
        setup_logging(settings.log_level, settings.secret_values())
    strategies = strategies or load_strategies(settings.strategies_file)

    db = Database(settings.database_url)
    db.create_all()
    store = Store(db)
    exchange = exchange or build_exchange(settings)
    notifier = notifier or build_notifier(settings)
    kill_switch = KillSwitch(store, settings.kill_switch)
    processor = AlertProcessor(
        settings=settings, strategies=strategies, exchange=exchange, store=store, kill_switch=kill_switch, notifier=notifier
    )
    worker = AlertWorker(processor)
    require_admin = admin_auth(settings.admin_token.get_secret_value())
    allowlist = settings.ip_allowlist
    trusted_proxies = settings.trusted_proxy_networks

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        abandoned = await run_in_threadpool(store.abandon_unfinished_alerts)
        if abandoned:
            log.warning("alerts abandoned by a restart were not replayed", extra={"count": abandoned})
            notifier.send(f"{abandoned} queued alert(s) were dropped by a restart and NOT executed")
        await run_in_threadpool(verify_api_key, settings, exchange, notifier)
        await run_in_threadpool(reconcile_unconfirmed_orders, settings, exchange, store, notifier)
        worker.start()
        startup = {
            "version": __version__,
            "mode": settings.mode_summary(),
            "strategies": sorted(strategies.strategies),
            "kill_switch": kill_switch.status().active,
            "ip_allowlist": settings.webhook_ip_allowlist_enabled,
        }
        if settings.live:
            log.warning("LIVE TRADING ENABLED: real orders with real funds", extra=startup)
        else:
            log.info("service started", extra=startup)
        notifier.send(f"service started (strategies: {', '.join(startup['strategies'])})")
        try:
            yield
        finally:
            worker.stop()
            notifier.close()
            db.dispose()

    app = FastAPI(
        title="TradingView -> Bybit",
        version=__version__,
        docs_url=None,  # no unauthenticated schema/docs endpoints
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.worker = worker
    app.state.kill_switch = kill_switch
    app.state.exchange = exchange

    # ---- webhook ----------------------------------------------------------------

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        ip = client_ip(request, trusted_proxies)
        if settings.webhook_ip_allowlist_enabled and not ip_allowed(ip, allowlist):
            log.warning("webhook from non-allowlisted IP rejected", extra={"source_ip": ip})
            return JSONResponse({"detail": "forbidden"}, status_code=403)

        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > settings.max_body_bytes:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            return JSONResponse({"detail": "payload too large"}, status_code=413)

        # TradingView sends text/plain unless the message is valid JSON, so the
        # body is parsed regardless of Content-Type.
        data = _parse_body(body)
        if data is None:
            # Never store the raw body: it may contain the secret.
            await run_in_threadpool(
                store.record_intake_failure, status="invalid", reason="body is not a JSON object", source_ip=ip, data=None
            )
            log.warning("webhook body is not a JSON object", extra={"source_ip": ip, "bytes": len(body)})
            return JSONResponse({"detail": "body must be a JSON object"}, status_code=400)

        provided = data.pop("secret", None)
        if not isinstance(provided, str) or not secrets_equal(provided, settings.webhook_secret.get_secret_value()):
            await run_in_threadpool(
                store.record_intake_failure, status="unauthorized", reason="missing or wrong secret", source_ip=ip, data=data
            )
            log.warning("webhook with missing or wrong secret", extra={"source_ip": ip})
            return JSONResponse({"detail": "unauthorized"}, status_code=401)

        try:
            payload = WebhookPayload.model_validate(data)
        except ValidationError as exc:
            errors = [{"field": ".".join(map(str, e["loc"])) or "body", "error": e["msg"]} for e in exc.errors()]
            reason = "; ".join(f"{e['field']}: {e['error']}" for e in errors)
            await run_in_threadpool(store.record_intake_failure, status="invalid", reason=reason, source_ip=ip, data=data)
            log.warning("invalid webhook payload", extra={"source_ip": ip, "errors": errors})
            notifier.send(f"INVALID alert rejected: {reason}")
            return JSONResponse({"detail": errors}, status_code=422)

        accepted = await run_in_threadpool(store.accept_alert, payload, ip)
        if accepted.duplicate_of is not None:
            log.info(
                "duplicate alert ignored",
                extra={"alert_id": payload.alert_id, "strategy_id": payload.strategy_id, "duplicate_of": accepted.duplicate_of},
            )
            return JSONResponse({"status": "duplicate", "id": accepted.alert_pk, "duplicate_of": accepted.duplicate_of})

        worker.submit(QueuedAlert(accepted.alert_pk, payload, accepted.received_at))
        log.info(
            "alert accepted",
            extra={"alert_pk": accepted.alert_pk, "alert_id": payload.alert_id, "strategy_id": payload.strategy_id,
                   "symbol": payload.symbol, "action": payload.action, "source_ip": ip},
        )
        return JSONResponse({"status": "accepted", "id": accepted.alert_pk})

    # ---- admin API (Authorization: Bearer <ADMIN_TOKEN>) ----------------------------

    admin = [Depends(require_admin)]

    @app.get("/health", dependencies=admin)
    def health(deep: bool = False) -> JSONResponse:
        db_ok = store.ping()
        worker_ok = worker.is_alive()
        body: dict[str, Any] = {
            "status": "ok" if db_ok and worker_ok else "degraded",
            "version": __version__,
            "mode": settings.mode_summary(),
            "kill_switch": kill_switch.status().to_dict(),
            "queue_depth": worker.depth(),
            "worker_alive": worker_ok,
            "database": db_ok,
        }
        if deep:
            body["exchange_reachable"] = exchange.ping()
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    @app.get("/positions", dependencies=admin)
    def positions() -> dict[str, Any]:
        if not exchange.authenticated:
            return {"positions": [], "note": "no Bybit API keys configured"}
        try:
            current = exchange.get_positions()
        except ExchangeError as exc:
            raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc
        return {"environment": exchange.environment, "positions": [_position_dict(p) for p in current]}

    @app.get("/orders", dependencies=admin)
    def orders(
        limit: int = Query(50, ge=1, le=500),
        symbol: str | None = None,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        return {"orders": store.list_orders(limit=limit, symbol=symbol.upper() if symbol else None, strategy_id=strategy_id)}

    @app.get("/alerts", dependencies=admin)
    def alerts(limit: int = Query(50, ge=1, le=500), status: str | None = None) -> dict[str, Any]:
        return {"alerts": store.list_alerts(limit=limit, status=status)}

    @app.post("/kill", dependencies=admin)
    def kill(body: KillRequest | None = None) -> dict[str, Any]:
        reason = (body or KillRequest()).reason
        status = kill_switch.engage(reason)
        log.warning("kill switch engaged", extra={"reason": reason})
        notifier.send(f"KILL SWITCH ENGAGED: {reason}")
        return {"kill_switch": status.to_dict()}

    @app.post("/resume", dependencies=admin)
    def resume() -> dict[str, Any]:
        status = kill_switch.release()
        if status.active:
            log.warning("resume requested but KILL_SWITCH env flag is still set")
            return {"kill_switch": status.to_dict(), "note": "KILL_SWITCH env flag is set; change it and restart to resume"}
        log.warning("kill switch released")
        notifier.send("kill switch released: trading resumed")
        return {"kill_switch": status.to_dict()}

    return app
