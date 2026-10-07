"""Checks run once at startup, before the worker starts taking alerts."""

from __future__ import annotations

import logging

from app.config import ConfigError, Settings
from app.exchange.base import Exchange, ExchangeError
from app.services.notifier import Notifier
from app.services.sizing import fmt
from app.store import Store

log = logging.getLogger(__name__)


def verify_api_key(settings: Settings, exchange: Exchange, notifier: Notifier) -> None:
    """Inspect the Bybit key's permissions via /v5/user/query-api.

    LIVE mode refuses to start if the key can withdraw, is not IP-restricted,
    or cannot be verified. Other modes only warn.
    """
    if not settings.has_api_credentials:
        log.info("no Bybit API keys configured: dry run uses public market data and DRY_RUN_EQUITY_USD")
        return
    try:
        info = exchange.check_api_key()
    except ExchangeError as exc:
        if settings.live:
            raise ConfigError(f"could not verify Bybit API key permissions; refusing to start LIVE: {exc}") from exc
        log.warning("could not verify Bybit API key permissions", extra={"error": str(exc)})
        return

    fatal: list[str] = []
    warnings: list[str] = []
    if info.get("withdraw_enabled"):
        (fatal if settings.live else warnings).append("API key has WITHDRAW permission; create a key with trade permission only")
    if not info.get("ip_restricted"):
        (fatal if settings.live else warnings).append("API key is not IP-restricted; bind it to this server's IP")
    if info.get("read_only") and not settings.dry_run:
        fatal.append("API key is read-only, so orders would fail")
    if not info.get("unified"):
        warnings.append("account does not look like a Unified Trading Account; wallet/equity calls may fail")

    for message in warnings:
        log.warning(message)
        notifier.send(f"WARNING: {message}")
    if fatal:
        raise ConfigError("unsafe Bybit API key: " + "; ".join(fatal))
    log.info("Bybit API key checked", extra={"ips": info.get("ips"), "read_only": info.get("read_only")})


def reconcile_unconfirmed_orders(settings: Settings, exchange: Exchange, store: Store, notifier: Notifier) -> None:
    """Orders left 'pending'/'Submitted'/'Uncertain' by a crash or timeout: ask
    Bybit what actually happened (looked up by orderLinkId)."""
    if settings.dry_run or not exchange.authenticated:
        return
    for order_pk, symbol, link_id in store.unconfirmed_orders():
        try:
            found = exchange.get_order(symbol, link_id)
        except ExchangeError as exc:
            log.warning("could not reconcile order", extra={"order_link_id": link_id, "error": str(exc)})
            continue
        if found is None:
            store.update_order(order_pk, status="NotFound", error="not found on Bybit during startup reconciliation")
            continue
        store.update_order(
            order_pk,
            status=found.status,
            exchange_order_id=found.order_id,
            avg_price=fmt(found.avg_price) if found.avg_price is not None else None,
            filled_qty=fmt(found.filled_qty) if found.filled_qty is not None else None,
            raw=found.raw,
        )
        notifier.send(f"Reconciled order {link_id} ({symbol}) after restart: {found.status}")
