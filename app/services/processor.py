"""Turns a validated alert into (real or simulated) Bybit orders.

Pipeline for every alert, each step recorded as a ``Decision`` row:

    freshness -> strategy -> intent -> kill switch -> symbol whitelist
    -> instrument -> [entries] leverage, price, sizing, position limit,
       max open positions, daily loss, TP/SL
    -> execute (close opposite?, set leverage?, place order)
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from app.config import Settings, StrategiesFile, StrategyConfig
from app.db import utcnow
from app.exchange.base import (
    Exchange,
    ExchangeError,
    InstrumentRules,
    OrderRejectedError,
    OrderRequest,
    OrderResult,
    OrderUncertainError,
    Position,
    PositionSide,
)
from app.schemas import SizeSpec, WebhookPayload
from app.services.killswitch import KillSwitch
from app.services.notifier import Notifier
from app.services.sizing import (
    SizingError,
    finalize_entry_qty,
    floor_to_step,
    fmt,
    raw_qty,
    round_to_tick,
    split_qty,
)
from app.store import Store

log = logging.getLogger(__name__)

IntentKind = Literal["open_long", "open_short", "close_long", "close_short", "close_all"]
# Final states meaning "this order did nothing" when nothing was filled.
NO_FILL_STATUSES = frozenset({"Rejected", "Cancelled", "Deactivated"})


@dataclass(frozen=True)
class Intent:
    kind: IntentKind
    size: SizeSpec | None = None  # for closes: None means the whole position

    @property
    def is_close(self) -> bool:
        return self.kind.startswith("close")


def resolve_intent(payload: WebhookPayload) -> Intent:
    """Map ``action`` (plus optional ``market_position``) to what we will do.

    ``market_position`` is TradingView's position *after* the strategy order,
    so a generic ``{{strategy.order.action}}`` alert can tell exits from entries:

        action  market_position  ->  intent
        buy     absent / long        open_long
        buy     flat                 close_short (whole position)
        buy     short                close_short (partial, by size)
        sell    absent / short       open_short
        sell    flat                 close_long  (whole position)
        sell    long                 close_long  (partial, by size)
    """
    action, position, size = payload.action, payload.market_position, payload.size
    if action == "buy":
        if position == "flat":
            return Intent("close_short")
        if position == "short":
            return Intent("close_short", size)
        return Intent("open_long", size)
    if action == "sell":
        if position == "flat":
            return Intent("close_long")
        if position == "long":
            return Intent("close_long", size)
        return Intent("open_short", size)
    if action == "close_all":
        return Intent("close_all")
    return Intent(action, size)


def order_link_id(dedupe_key: str, leg: str) -> str:
    """Deterministic Bybit orderLinkId (max 36 chars, [A-Za-z0-9_-]).

    Derived from the alert's idempotency key, so the same alert can never
    produce two different orders, even across retries or restarts.
    """
    digest = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:24]
    return f"tv-{digest}-{leg}"


@dataclass(frozen=True)
class QueuedAlert:
    alert_pk: int
    payload: WebhookPayload
    received_at: datetime  # naive UTC


@dataclass(frozen=True)
class LeverageStep:
    symbol: str
    leverage: Decimal


@dataclass(frozen=True)
class OrderStep:
    leg: Literal["open", "close", "reverse_close"]
    request: OrderRequest


PlanStep = LeverageStep | OrderStep


class Rejected(Exception):
    def __init__(self, step: str, reason: str):
        super().__init__(reason)
        self.step = step
        self.reason = reason


class NothingToDo(Exception):
    pass


def _find(positions: list[Position], symbol: str, side: PositionSide) -> Position | None:
    return next((p for p in positions if p.symbol == symbol and p.side == side), None)


def _opposite(side: PositionSide) -> PositionSide:
    return "short" if side == "long" else "long"


class AlertProcessor:
    def __init__(
        self,
        *,
        settings: Settings,
        strategies: StrategiesFile,
        exchange: Exchange,
        store: Store,
        kill_switch: KillSwitch,
        notifier: Notifier,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.settings = settings
        self.strategies = strategies
        self.exchange = exchange
        self.store = store
        self.kill_switch = kill_switch
        self.notifier = notifier
        self._clock = clock

    # ---- entry point ------------------------------------------------------

    def process(self, item: QueuedAlert) -> str:
        p = item.payload
        label = f"{p.strategy_id} {p.symbol} {p.action}"
        self.store.set_alert_status(item.alert_pk, "processing")
        notify: str | None = None
        try:
            status, reason = self._handle(item)
            notify = f"{'DRY RUN' if status == 'dry_run' else 'ORDER'} {label}: {reason}"
        except (Rejected, SizingError) as exc:
            step = exc.step if isinstance(exc, Rejected) else "sizing"
            status, reason = "rejected", str(exc)
            self._decide(item, step, "reject", reason)
            notify = f"REJECTED {label}: {reason}"
        except NothingToDo as exc:
            status, reason = "skipped", str(exc)
            self._decide(item, "plan", "skip", reason)
        except OrderUncertainError as exc:
            status, reason = "error", str(exc)
            self._decide(item, "execute", "error", reason)
            notify = f"ERROR - CHECK BYBIT MANUALLY {label}: {reason}"
        except ExchangeError as exc:
            status, reason = "error", f"exchange error: {exc}"
            self._decide(item, "exchange", "error", reason)
            notify = f"ERROR {label}: {reason}"
        except Exception as exc:
            log.exception("unexpected error processing alert", extra={"alert_pk": item.alert_pk})
            status, reason = "error", f"internal error: {type(exc).__name__}: {exc}"
            self._decide(item, "internal", "error", reason)
            notify = f"ERROR {label}: {reason}"

        self.store.set_alert_status(item.alert_pk, status, reason, finished=True)
        log.info(
            "alert processed",
            extra={"alert_pk": item.alert_pk, "strategy_id": p.strategy_id, "symbol": p.symbol,
                   "action": p.action, "status": status, "reason": reason},
        )
        if notify:
            self.notifier.send(notify)
        return status

    # ---- pipeline -----------------------------------------------------------

    def _handle(self, item: QueuedAlert) -> tuple[str, str]:
        p = item.payload
        age = (self._clock() - item.received_at).total_seconds()
        if age > self.settings.max_alert_age_seconds:
            raise Rejected("freshness", f"alert is stale: queued {age:.0f}s ago (max {self.settings.max_alert_age_seconds:.0f}s)")

        strategy = self.strategies.strategies.get(p.strategy_id)
        if strategy is None:
            raise Rejected("strategy", f"unknown strategy_id {p.strategy_id!r}")
        if not strategy.enabled:
            raise Rejected("strategy", f"strategy {p.strategy_id} is disabled")

        intent = resolve_intent(p)
        self._decide(item, "intent", "info", f"{p.action} (market_position={p.market_position}) -> {intent.kind}")

        kill = self.kill_switch.status()
        if kill.active and not (intent.is_close and self.settings.kill_switch_allows_closes):
            raise Rejected("kill_switch", f"kill switch is active: {kill.reason}")

        risk = self.strategies.risk
        if p.symbol not in risk.allowed_symbols:
            raise Rejected("symbol", f"{p.symbol} is not in the global symbol whitelist")
        if p.symbol not in strategy.allowed_symbols:
            raise Rejected("symbol", f"{p.symbol} is not allowed for strategy {p.strategy_id}")

        rules = self.exchange.get_instrument(p.symbol)
        if rules.status != "Trading":
            raise Rejected("instrument", f"{p.symbol} status is {rules.status!r}, not 'Trading'")

        positions = self.exchange.get_positions()
        if intent.is_close:
            plan = self._plan_close(item, intent, rules, positions)
        else:
            plan = self._plan_open(item, intent, strategy, rules, positions)

        executed = self._execute(item, plan)
        summary = "; ".join(self._describe_fill(step, result) for step, result in executed)
        return ("dry_run" if self.settings.dry_run else "executed"), summary

    # ---- planning: closes -----------------------------------------------------

    def _plan_close(self, item: QueuedAlert, intent: Intent, rules: InstrumentRules, positions: list[Position]) -> list[PlanStep]:
        symbol = item.payload.symbol
        targets: list[Position] = []
        if intent.kind in ("close_long", "close_all") and (pos := _find(positions, symbol, "long")):
            targets.append(pos)
        if intent.kind in ("close_short", "close_all") and (pos := _find(positions, symbol, "short")):
            targets.append(pos)
        if not targets:
            wanted = {"close_long": "long", "close_short": "short"}.get(intent.kind, "")
            raise NothingToDo(f"no open {wanted + ' ' if wanted else ''}position on {symbol}")

        plan: list[PlanStep] = []
        for pos in targets:
            qty = pos.size
            if intent.size is not None:
                ref = self.exchange.get_last_price(symbol) if intent.size.mode != "qty" else None
                equity = self.exchange.get_equity_usd() if intent.size.mode == "percent_equity" else None
                partial = floor_to_step(raw_qty(intent.size, ref, equity), rules.qty_step)
                if partial <= 0 or partial < rules.min_qty:
                    raise Rejected("sizing", f"partial close qty {fmt(partial)} is below the minimum {fmt(rules.min_qty)}")
                qty = min(partial, pos.size)
            plan.extend(self._close_steps(item, pos, qty, rules, leg="close"))
            self._decide(item, "plan", "info", f"close {fmt(qty)} of {pos.side} {fmt(pos.size)} {symbol} (reduce-only market)")
        return plan

    def _close_steps(self, item: QueuedAlert, pos: Position, qty: Decimal, rules: InstrumentRules,
                     *, leg: Literal["close", "reverse_close"]) -> list[OrderStep]:
        code = ("x" if leg == "close" else "r") + pos.side[0]  # xl / xs / rl / rs
        chunks = split_qty(qty, rules.max_market_qty)
        steps = []
        for n, chunk in enumerate(chunks, start=1):
            steps.append(
                OrderStep(
                    leg=leg,
                    request=OrderRequest(
                        symbol=pos.symbol,
                        side="Sell" if pos.side == "long" else "Buy",
                        order_type="Market",
                        qty=chunk,
                        order_link_id=order_link_id(item.payload.dedupe_key, code + (str(n) if len(chunks) > 1 else "")),
                        position_idx=self._position_idx(pos.side),
                        reduce_only=True,
                    ),
                )
            )
        return steps

    # ---- planning: entries ----------------------------------------------------

    def _plan_open(self, item: QueuedAlert, intent: Intent, strategy: StrategyConfig,
                   rules: InstrumentRules, positions: list[Position]) -> list[PlanStep]:
        p = item.payload
        symbol = p.symbol
        risk = self.strategies.risk
        side: PositionSide = "long" if intent.kind == "open_long" else "short"
        same = _find(positions, symbol, side)
        opposite = _find(positions, symbol, _opposite(side))
        plan: list[PlanStep] = []

        # 1. Opposite position (one-way mode would otherwise net it out).
        if opposite is not None:
            if strategy.close_opposite_on_entry:
                plan.extend(self._close_steps(item, opposite, opposite.size, rules, leg="reverse_close"))
                self._decide(item, "reverse", "info", f"will close {opposite.side} {fmt(opposite.size)} {symbol} before opening {side}")
            elif self.settings.bybit_position_mode == "one_way":
                raise Rejected("position", f"{opposite.side} position is open on {symbol} and close_opposite_on_entry is false")

        # 2. Leverage.
        max_leverage = min(x for x in (risk.max_leverage, strategy.max_leverage, rules.max_leverage) if x is not None)
        leverage = p.leverage if p.leverage is not None else strategy.default_leverage
        current = self.exchange.get_leverage(symbol)
        if leverage is not None:
            if leverage > max_leverage:
                raise Rejected("leverage", f"leverage {fmt(leverage)}x exceeds the maximum {fmt(max_leverage)}x")
            if leverage < rules.min_leverage:
                raise Rejected("leverage", f"leverage {fmt(leverage)}x is below Bybit's minimum {fmt(rules.min_leverage)}x")
            leverage = floor_to_step(leverage, rules.leverage_step)
            if current != leverage:
                plan.append(LeverageStep(symbol, leverage))
        elif current is not None and current > max_leverage:
            raise Rejected(
                "leverage",
                f"{symbol} leverage on Bybit is {fmt(current)}x, above the maximum {fmt(max_leverage)}x; "
                "send leverage in the alert or set default_leverage for this strategy",
            )
        self._decide(item, "leverage", "pass", f"leverage {fmt(leverage) if leverage else 'unchanged'} (max {fmt(max_leverage)}x)")

        # 3. Price.
        market = p.order_type == "market"
        price: Decimal | None = None
        if market:
            ref_price = self.exchange.get_last_price(symbol)
        else:
            assert p.price is not None  # enforced by the schema
            # Round in the conservative direction: never pay more / sell for less.
            price = round_to_tick(p.price, rules.tick_size, "down" if side == "long" else "up")
            if price < rules.min_price or (rules.max_price > 0 and price > rules.max_price):
                raise Rejected("price", f"limit price {fmt(price)} is outside Bybit's allowed range")
            ref_price = price

        # 4. Size.
        size = intent.size or strategy.default_size
        if size is None:
            raise Rejected("sizing", "alert has no size and the strategy has no default_size")
        equity = self.exchange.get_equity_usd() if size.mode == "percent_equity" else None
        qty = finalize_entry_qty(raw_qty(size, ref_price, equity), rules, market=market, ref_price=ref_price)
        notional = qty * ref_price
        self._decide(item, "sizing", "pass", f"{size.mode}={fmt(size.value)} -> qty {fmt(qty)} (~{notional:.2f} USDT @ {fmt(ref_price)})")

        # 5. Max position size for this symbol.
        limit = risk.position_limit(symbol)
        if strategy.max_position_usd is not None:
            limit = min(limit, strategy.max_position_usd)
        existing = same.notional if same else Decimal(0)
        if existing + notional > limit:
            raise Rejected(
                "position_limit",
                f"{side} {symbol} would be {existing + notional:.2f} USDT (existing {existing:.2f} + order {notional:.2f}), "
                f"above the limit of {fmt(limit)} USDT",
            )

        # 6. Max open positions (counted per symbol).
        open_symbols = {pos.symbol for pos in positions}
        if symbol not in open_symbols and len(open_symbols) >= risk.max_open_positions:
            raise Rejected("open_positions", f"{len(open_symbols)} positions already open (max {risk.max_open_positions})")
        self._decide(item, "limits", "pass", f"position {existing + notional:.2f}/{fmt(limit)} USDT, open positions {len(open_symbols)}/{risk.max_open_positions}")

        # 7. Max daily loss.
        self._check_daily_loss(item, positions)

        # 8. Take profit / stop loss.
        take_profit, stop_loss = self._tp_sl(p, strategy, side, ref_price, rules)

        plan.append(
            OrderStep(
                leg="open",
                request=OrderRequest(
                    symbol=symbol,
                    side="Buy" if side == "long" else "Sell",
                    order_type="Market" if market else "Limit",
                    qty=qty,
                    price=price,
                    order_link_id=order_link_id(p.dedupe_key, "o"),
                    position_idx=self._position_idx(side),
                    take_profit=take_profit,
                    stop_loss=stop_loss,
                ),
            )
        )
        return plan

    def _check_daily_loss(self, item: QueuedAlert, positions: list[Position]) -> None:
        risk = self.strategies.risk
        midnight = self._clock().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)
        realized = self.exchange.get_realized_pnl_since(int(midnight.timestamp() * 1000))
        unrealized = sum((pos.unrealized_pnl for pos in positions), Decimal(0)) if risk.include_unrealized_pnl_in_daily_loss else Decimal(0)
        pnl = realized + unrealized
        if pnl <= -risk.max_daily_loss_usd:
            raise Rejected(
                "daily_loss",
                f"today's PnL is {pnl:.2f} USDT (realized {realized:.2f}, unrealized {unrealized:.2f}); "
                f"max daily loss is {fmt(risk.max_daily_loss_usd)} USDT",
            )
        self._decide(item, "daily_loss", "pass", f"today's PnL {pnl:.2f} USDT (limit -{fmt(risk.max_daily_loss_usd)})")

    def _tp_sl(self, p: WebhookPayload, strategy: StrategyConfig, side: PositionSide,
               ref_price: Decimal, rules: InstrumentRules) -> tuple[Decimal | None, Decimal | None]:
        long = side == "long"
        hundred = Decimal(100)
        take_profit, stop_loss = p.take_profit, p.stop_loss
        if take_profit is None and strategy.take_profit_pct is not None:
            factor = 1 + strategy.take_profit_pct / hundred if long else 1 - strategy.take_profit_pct / hundred
            take_profit = ref_price * factor
        if stop_loss is None and strategy.stop_loss_pct is not None:
            factor = 1 - strategy.stop_loss_pct / hundred if long else 1 + strategy.stop_loss_pct / hundred
            stop_loss = ref_price * factor
        if take_profit is not None:
            take_profit = round_to_tick(take_profit, rules.tick_size, "nearest")
            if take_profit <= 0 or (take_profit <= ref_price if long else take_profit >= ref_price):
                raise Rejected("tp_sl", f"take_profit {fmt(take_profit)} must be {'above' if long else 'below'} the entry price {fmt(ref_price)} for a {side}")
        if stop_loss is not None:
            stop_loss = round_to_tick(stop_loss, rules.tick_size, "nearest")
            if stop_loss <= 0 or (stop_loss >= ref_price if long else stop_loss <= ref_price):
                raise Rejected("tp_sl", f"stop_loss {fmt(stop_loss)} must be {'below' if long else 'above'} the entry price {fmt(ref_price)} for a {side}")
        return take_profit, stop_loss

    def _position_idx(self, side: PositionSide) -> int:
        if self.settings.bybit_position_mode == "one_way":
            return 0
        return 1 if side == "long" else 2

    # ---- execution ------------------------------------------------------------

    def _execute(self, item: QueuedAlert, plan: list[PlanStep]) -> list[tuple[OrderStep, OrderResult]]:
        done: list[tuple[OrderStep, OrderResult]] = []
        for step in plan:
            if isinstance(step, LeverageStep):
                self.exchange.set_leverage(step.symbol, step.leverage)
                self._decide(item, "execute", "info", f"set {step.symbol} leverage to {fmt(step.leverage)}x")
                continue

            req = step.request
            order_pk = self.store.create_order(
                alert_pk=item.alert_pk,
                order_link_id=req.order_link_id,
                leg=step.leg,
                symbol=req.symbol,
                side=req.side,
                order_type=req.order_type,
                qty=fmt(req.qty),
                price=fmt(req.price) if req.price is not None else None,
                take_profit=fmt(req.take_profit) if req.take_profit is not None else None,
                stop_loss=fmt(req.stop_loss) if req.stop_loss is not None else None,
                reduce_only=req.reduce_only,
                status="pending",
                dry_run=self.settings.dry_run,
                environment=self.exchange.environment,
            )
            after = f" (after {len(done)} executed order(s))" if done else ""
            try:
                result = self.exchange.place_order(req)
            except OrderRejectedError as exc:
                self.store.update_order(order_pk, status="Rejected", error=str(exc))
                raise Rejected("execute", f"Bybit rejected the {step.leg} order{after}: {exc}") from exc
            except OrderUncertainError as exc:
                self.store.update_order(order_pk, status="Uncertain", error=str(exc))
                raise
            except ExchangeError as exc:
                self.store.update_order(order_pk, status="Error", error=str(exc))
                raise

            self.store.update_order(
                order_pk,
                status=result.status,
                exchange_order_id=result.order_id,
                avg_price=fmt(result.avg_price) if result.avg_price is not None else None,
                filled_qty=fmt(result.filled_qty) if result.filled_qty is not None else None,
                error=result.reject_reason,
                raw=result.raw,
            )
            self._decide(item, "execute", "info", self._describe_fill(step, result), {"order_link_id": req.order_link_id})
            if result.status in NO_FILL_STATUSES and not result.filled_qty:
                raise Rejected(
                    "execute",
                    f"{step.leg} order ended {result.status} without a fill{after}"
                    + (f": {result.reject_reason}" if result.reject_reason else ""),
                )
            done.append((step, result))
        return done

    @staticmethod
    def _describe_fill(step: OrderStep, result: OrderResult) -> str:
        req = step.request
        price = f" @ {fmt(result.avg_price)}" if result.avg_price else (f" limit {fmt(req.price)}" if req.price else "")
        extras = "".join(
            f" {name} {fmt(value)}" for name, value in (("TP", req.take_profit), ("SL", req.stop_loss)) if value is not None
        )
        return f"{step.leg}: {req.side} {fmt(req.qty)} {req.symbol} {req.order_type.lower()}{extras} -> {result.status}{price}"

    def _decide(self, item: QueuedAlert, step: str, outcome: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.store.add_decision(item.alert_pk, step, outcome, message, data)
        log.info(message, extra={"alert_pk": item.alert_pk, "step": step, "outcome": outcome})
