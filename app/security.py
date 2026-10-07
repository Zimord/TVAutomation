"""Webhook secret check, admin token auth and source-IP allowlisting."""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Callable, Sequence

from fastapi import Header, HTTPException, Request

from app.config import IPNetwork


def secrets_equal(provided: str, expected: str) -> bool:
    """Constant-time comparison (no early exit on the first differing byte)."""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _in_networks(ip: str | None, networks: Sequence[IPNetwork]) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in networks)


def ip_allowed(ip: str | None, allowlist: Sequence[IPNetwork]) -> bool:
    return _in_networks(ip, allowlist)


def client_ip(request: Request, trusted_proxies: Sequence[IPNetwork]) -> str | None:
    """The real client IP.

    ``X-Forwarded-For`` is only honoured when the TCP peer is a trusted proxy
    (Caddy on the internal Docker network); otherwise anyone could spoof a
    TradingView IP by sending the header themselves. We walk the header from the
    right and return the first hop that is not one of our own proxies.
    """
    peer = request.client.host if request.client else None
    if not trusted_proxies or not _in_networks(peer, trusted_proxies):
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not _in_networks(hop, trusted_proxies):
            return hop
    return hops[0] if hops else peer


def admin_auth(expected_token: str) -> Callable[[str | None], None]:
    """FastAPI dependency enforcing ``Authorization: Bearer <ADMIN_TOKEN>``."""

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        token = ""
        if authorization and authorization[:7].lower() == "bearer ":
            token = authorization[7:].strip()
        if not token or not secrets_equal(token, expected_token):
            raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})

    return require_admin
