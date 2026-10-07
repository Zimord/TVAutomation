"""SQLite persistence via SQLAlchemy 2.x."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite stores datetimes without a timezone)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Alert(Base):
    """Every webhook that reached the app from an allowed IP."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Idempotency key "<strategy_id>:<alert_id>". NULL for anything that was not
    # accepted (duplicates, bad secret, invalid payload); SQLite allows many NULLs.
    dedupe_key: Mapped[str | None] = mapped_column(String(256), unique=True)
    alert_id: Mapped[str | None] = mapped_column(String(128), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64))
    source_ip: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # secret already removed
    # queued | processing | executed | dry_run | rejected | skipped | error
    # | duplicate | unauthorized | invalid | abandoned
    status: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)

    decisions: Mapped[list[Decision]] = relationship(back_populates="alert", order_by="Decision.id")
    orders: Mapped[list[Order]] = relationship(back_populates="alert", order_by="Order.id")


class Decision(Base):
    """One step of the processing pipeline (risk check, sizing, execution...)."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_pk: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    step: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(16))  # pass | reject | info | skip | error
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    alert: Mapped[Alert] = relationship(back_populates="decisions")


class Order(Base):
    """An order we placed (or would have placed, in dry-run)."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_pk: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    order_link_id: Mapped[str] = mapped_column(String(36), unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    leg: Mapped[str] = mapped_column(String(24))  # open | close | reverse_close
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(8))
    qty: Mapped[str] = mapped_column(String(40))
    price: Mapped[str | None] = mapped_column(String(40))
    take_profit: Mapped[str | None] = mapped_column(String(40))
    stop_loss: Mapped[str | None] = mapped_column(String(40))
    reduce_only: Mapped[bool]
    # pending -> Bybit orderStatus (New, Filled, ...) | DryRun | Rejected | Uncertain | Error
    status: Mapped[str] = mapped_column(String(32), index=True)
    avg_price: Mapped[str | None] = mapped_column(String(40))
    filled_qty: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[bool]
    environment: Mapped[str] = mapped_column(String(8))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    alert: Mapped[Alert] = relationship(back_populates="orders")


class AppState(Base):
    """Small key/value store (kill switch state survives restarts)."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Database:
    def __init__(self, url: str):
        parsed = make_url(url)
        connect_args: dict[str, Any] = {}
        is_sqlite = parsed.get_backend_name() == "sqlite"
        if is_sqlite:
            connect_args["check_same_thread"] = False  # web threads + worker thread
            if parsed.database and parsed.database != ":memory:":
                Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, connect_args=connect_args)
        if is_sqlite:
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
