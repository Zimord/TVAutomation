"""Persistence operations: alerts, decisions, orders and app state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.db import Alert, AppState, Database, Decision, Order, utcnow
from app.schemas import WebhookPayload

UNFINISHED_ALERT_STATUSES = ("queued", "processing")
# Orders whose outcome we never learned (process died mid-request, or Bybit had
# not reported a final state yet).
UNCONFIRMED_ORDER_STATUSES = ("pending", "Submitted", "Uncertain")
MAX_STRING = 512


def jsonable(value: Any, depth: int = 0) -> Any:
    """Make an arbitrary parsed-JSON value safe to store (bounded, no secrets)."""
    if depth > 5:
        return str(value)[:MAX_STRING]
    if isinstance(value, dict):
        return {
            str(k)[:64]: jsonable(v, depth + 1)
            for k, v in list(value.items())[:50]
            if str(k).lower() != "secret"
        }
    if isinstance(value, list | tuple):
        return [jsonable(v, depth + 1) for v in value[:50]]
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:MAX_STRING]


def iso(ts: datetime | None) -> str | None:
    return ts.isoformat(timespec="seconds") + "Z" if ts else None


@dataclass(frozen=True)
class AcceptedAlert:
    alert_pk: int
    received_at: datetime
    duplicate_of: int | None  # set when this alert_id was seen before


class Store:
    def __init__(self, db: Database):
        self.db = db

    # ---- alerts -----------------------------------------------------------

    def record_intake_failure(self, *, status: str, reason: str, source_ip: str | None, data: Any) -> int:
        safe = jsonable(data) if data is not None else None

        def field(name: str) -> str | None:
            value = safe.get(name) if isinstance(safe, dict) else None
            return str(value)[:64] if value is not None else None

        with self.db.session() as s:
            row = Alert(
                status=status,
                reason=reason[:4000],
                source_ip=source_ip,
                payload=safe if isinstance(safe, dict) else None,
                alert_id=field("alert_id"),
                strategy_id=field("strategy_id"),
                symbol=field("symbol"),
                action=field("action"),
                received_at=utcnow(),
            )
            s.add(row)
            s.flush()
            return row.id

    def accept_alert(self, payload: WebhookPayload, source_ip: str | None) -> AcceptedAlert:
        """Insert the alert; the UNIQUE dedupe_key makes duplicates fail atomically."""
        common = {
            "alert_id": payload.alert_id,
            "strategy_id": payload.strategy_id,
            "symbol": payload.symbol,
            "action": payload.action,
            "source_ip": source_ip,
            "payload": payload.model_dump(mode="json"),
        }
        with self.db.session() as s:
            row = Alert(dedupe_key=payload.dedupe_key, status="queued", received_at=utcnow(), **common)
            s.add(row)
            try:
                s.flush()
                return AcceptedAlert(row.id, row.received_at, None)
            except IntegrityError:
                s.rollback()

        with self.db.session() as s:
            original = s.scalar(select(Alert.id).where(Alert.dedupe_key == payload.dedupe_key))
            dup = Alert(
                status="duplicate",
                reason=f"duplicate of alert #{original}",
                received_at=utcnow(),
                processed_at=utcnow(),
                **common,
            )
            s.add(dup)
            s.flush()
            return AcceptedAlert(dup.id, dup.received_at, original)

    def set_alert_status(self, alert_pk: int, status: str, reason: str | None = None, *, finished: bool = False) -> None:
        values: dict[str, Any] = {"status": status}
        if reason is not None:
            values["reason"] = reason[:4000]
        if finished:
            values["processed_at"] = utcnow()
        with self.db.session() as s:
            s.execute(update(Alert).where(Alert.id == alert_pk).values(**values))

    def abandon_unfinished_alerts(self) -> int:
        """Alerts still queued at startup were lost in a restart. They are NOT
        replayed: an old signal executed late is worse than a missed one."""
        with self.db.session() as s:
            result = s.execute(
                update(Alert)
                .where(Alert.status.in_(UNFINISHED_ALERT_STATUSES))
                .values(status="abandoned", reason="service restarted before processing finished", processed_at=utcnow())
            )
            return result.rowcount or 0

    def add_decision(self, alert_pk: int, step: str, outcome: str, message: str, data: dict[str, Any] | None = None) -> None:
        with self.db.session() as s:
            s.add(
                Decision(
                    alert_pk=alert_pk,
                    step=step,
                    outcome=outcome,
                    message=message[:4000],
                    data=jsonable(data) if data else None,
                    ts=utcnow(),
                )
            )

    def list_alerts(self, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        with self.db.session() as s:
            query = select(Alert).options(selectinload(Alert.decisions)).order_by(Alert.id.desc()).limit(limit)
            if status:
                query = query.where(Alert.status == status)
            return [
                {
                    "id": a.id,
                    "alert_id": a.alert_id,
                    "strategy_id": a.strategy_id,
                    "symbol": a.symbol,
                    "action": a.action,
                    "status": a.status,
                    "reason": a.reason,
                    "source_ip": a.source_ip,
                    "received_at": iso(a.received_at),
                    "processed_at": iso(a.processed_at),
                    "payload": a.payload,
                    "decisions": [
                        {"ts": iso(d.ts), "step": d.step, "outcome": d.outcome, "message": d.message}
                        for d in a.decisions
                    ],
                }
                for a in s.scalars(query)
            ]

    # ---- orders -----------------------------------------------------------

    def create_order(self, **fields: Any) -> int:
        with self.db.session() as s:
            row = Order(created_at=utcnow(), updated_at=utcnow(), **fields)
            s.add(row)
            s.flush()
            return row.id

    def update_order(self, order_pk: int, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        if "raw" in fields:
            fields["raw"] = jsonable(fields["raw"])
        with self.db.session() as s:
            s.execute(update(Order).where(Order.id == order_pk).values(**fields))

    def unconfirmed_orders(self) -> list[tuple[int, str, str]]:
        with self.db.session() as s:
            rows = s.execute(
                select(Order.id, Order.symbol, Order.order_link_id).where(
                    Order.status.in_(UNCONFIRMED_ORDER_STATUSES), Order.dry_run.is_(False)
                )
            )
            return [(r.id, r.symbol, r.order_link_id) for r in rows]

    def list_orders(self, *, limit: int = 50, symbol: str | None = None, strategy_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.session() as s:
            query = (
                select(Order, Alert.strategy_id, Alert.alert_id)
                .join(Alert, Order.alert_pk == Alert.id)
                .order_by(Order.id.desc())
                .limit(limit)
            )
            if symbol:
                query = query.where(Order.symbol == symbol)
            if strategy_id:
                query = query.where(Alert.strategy_id == strategy_id)
            return [
                {
                    "id": o.id,
                    "alert_pk": o.alert_pk,
                    "strategy_id": strategy,
                    "alert_id": alert_id,
                    "order_link_id": o.order_link_id,
                    "exchange_order_id": o.exchange_order_id,
                    "leg": o.leg,
                    "symbol": o.symbol,
                    "side": o.side,
                    "order_type": o.order_type,
                    "qty": o.qty,
                    "price": o.price,
                    "take_profit": o.take_profit,
                    "stop_loss": o.stop_loss,
                    "reduce_only": o.reduce_only,
                    "status": o.status,
                    "avg_price": o.avg_price,
                    "filled_qty": o.filled_qty,
                    "error": o.error,
                    "dry_run": o.dry_run,
                    "environment": o.environment,
                    "created_at": iso(o.created_at),
                    "updated_at": iso(o.updated_at),
                }
                for o, strategy, alert_id in s.execute(query)
            ]

    # ---- app state ----------------------------------------------------------

    def get_state(self, key: str) -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.get(AppState, key)
            return dict(row.value) if row else None

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        with self.db.session() as s:
            row = s.get(AppState, key)
            if row is None:
                s.add(AppState(key=key, value=value, updated_at=utcnow()))
            else:
                row.value = value
                row.updated_at = utcnow()

    def ping(self) -> bool:
        try:
            with self.db.session() as s:
                s.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
