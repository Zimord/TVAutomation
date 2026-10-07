from __future__ import annotations

from app.config import Settings
from app.exchange.base import Exchange
from app.exchange.bybit import BybitClient
from app.exchange.dry_run import DryRunExchange


def build_exchange(settings: Settings) -> Exchange:
    client = BybitClient(
        api_key=settings.bybit_api_key.get_secret_value() if settings.bybit_api_key else None,
        api_secret=settings.bybit_api_secret.get_secret_value() if settings.bybit_api_secret else None,
        testnet=settings.bybit_testnet,
        demo=settings.bybit_demo,
        category=settings.bybit_category,
        settle_coin=settings.bybit_settle_coin,
        recv_window=settings.bybit_recv_window,
        timeout=settings.bybit_timeout,
        max_attempts=settings.order_max_attempts,
    )
    if settings.dry_run:
        return DryRunExchange(client, settings.dry_run_equity_usd)
    return client
