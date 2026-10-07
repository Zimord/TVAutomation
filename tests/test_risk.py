"""Risk checks and order planning, end to end through the processor with a fake exchange."""

from datetime import timedelta
from decimal import Decimal as D

from app.exchange.base import OrderRejectedError, OrderUncertainError
from app.exchange.dry_run import DryRunExchange
from tests.conftest import make_strategies
from tests.fakes import FakeExchange, position


def test_happy_path_opens_long_with_default_size_and_leverage(make_harness):
    h = make_harness()
    status, alert = h.run()
    assert status == "executed", alert["reason"]
    assert h.exchange.leverage_calls == [("BTCUSDT", D("2"))]  # strategy default_leverage
    [order] = h.exchange.placed
    assert (order.side, order.order_type, order.qty, order.position_idx, order.reduce_only) == ("Buy", "Market", D("0.002"), 0, False)
    assert order.order_link_id.startswith("tv-") and len(order.order_link_id) <= 36
    steps = [d["step"] for d in alert["decisions"]]
    assert {"intent", "leverage", "sizing", "limits", "daily_loss", "execute"} <= set(steps)
    [stored] = h.store.list_orders()
    assert stored["status"] == "Filled" and stored["qty"] == "0.002" and stored["dry_run"] is False
    assert h.notifier.messages and h.notifier.messages[-1].startswith("ORDER")


def test_leverage_not_reset_when_unchanged(make_harness):
    h = make_harness(FakeExchange(leverage="2"))
    h.run()
    assert h.exchange.leverage_calls == []


def test_symbol_not_in_global_whitelist(make_harness):
    h = make_harness()
    status, alert = h.run(strategy_id="multi", symbol="DOGEUSDT")
    assert status == "rejected" and "global symbol whitelist" in alert["reason"]
    assert h.exchange.placed == []
    assert h.notifier.messages[-1].startswith("REJECTED")


def test_symbol_not_allowed_for_strategy(make_harness):
    h = make_harness()
    status, alert = h.run(symbol="ETHUSDT")
    assert status == "rejected" and "not allowed for strategy" in alert["reason"]


def test_unknown_and_disabled_strategies(make_harness):
    h = make_harness()
    assert h.run(strategy_id="nope")[1]["reason"] == "unknown strategy_id 'nope'"
    status, alert = h.run(strategy_id="disabled")
    assert status == "rejected" and "disabled" in alert["reason"]


def test_leverage_above_strategy_max(make_harness):
    h = make_harness()
    status, alert = h.run(leverage=4)  # strategy max 3, global max 5
    assert status == "rejected" and "exceeds the maximum 3x" in alert["reason"]
    assert h.exchange.placed == [] and h.exchange.leverage_calls == []


def test_existing_bybit_leverage_above_max_is_rejected(make_harness):
    h = make_harness(FakeExchange(leverage="10"))
    status, alert = h.run(strategy_id="multi")  # no leverage in alert, no default_leverage
    assert status == "rejected" and "leverage on Bybit is 10x" in alert["reason"]


def test_max_position_size_per_symbol(make_harness):
    # Existing 0.03 BTC @ 50k = 1500 USDT; +600 = 2100 > BTCUSDT limit 2000
    h = make_harness(FakeExchange(positions=[position(size="0.03")]))
    status, alert = h.run(strategy_id="multi", size={"mode": "usd", "value": 600})
    assert status == "rejected" and alert["reason"].startswith("long BTCUSDT would be 2100.00 USDT")
    status, _ = h.run(strategy_id="multi", size={"mode": "usd", "value": 400})
    assert status == "executed"


def test_max_open_positions(make_harness):
    held = [position("ETHUSDT", mark="3000"), position("SOLUSDT", mark="150")]
    h = make_harness(FakeExchange(positions=held))
    status, alert = h.run(strategy_id="multi", symbol="BTCUSDT")
    assert status == "rejected" and "2 positions already open (max 2)" in alert["reason"]
    # Adding to a symbol that already has a position is fine.
    assert h.run(strategy_id="multi", symbol="ETHUSDT")[0] == "executed"


def test_max_daily_loss_blocks_entries_but_not_exits(make_harness):
    h = make_harness(FakeExchange(realized_pnl="-80", positions=[position(pnl="-30")]))
    status, alert = h.run()
    assert status == "rejected" and "max daily loss is 100 USDT" in alert["reason"]
    assert h.run(action="close_long")[0] == "executed"


def test_daily_loss_can_ignore_unrealized(make_harness):
    strategies = make_strategies(include_unrealized_pnl_in_daily_loss=False)
    h = make_harness(FakeExchange(realized_pnl="-80", positions=[position(pnl="-30")]), strategies=strategies)
    assert h.run()[0] == "executed"


def test_kill_switch_blocks_everything_by_default(make_harness):
    h = make_harness(FakeExchange(positions=[position()]))
    h.kill_switch.engage("testing")
    for action in ("buy", "close_long"):
        status, alert = h.run(action=action)
        assert status == "rejected" and "kill switch is active: testing" in alert["reason"]
    assert h.exchange.placed == []
    h.kill_switch.release()
    assert h.run(action="close_long")[0] == "executed"


def test_kill_switch_can_allow_closes(make_harness):
    h = make_harness(FakeExchange(positions=[position()]), kill_switch_allows_closes=True)
    h.kill_switch.engage("testing")
    assert h.run(action="buy")[0] == "rejected"
    assert h.run(action="close_long")[0] == "executed"


def test_env_kill_switch_cannot_be_released_at_runtime(make_harness):
    h = make_harness(kill_switch=True)
    assert h.kill_switch.release().active is True
    assert h.run()[0] == "rejected"


def test_stale_alert_rejected(make_harness):
    h = make_harness(max_alert_age_seconds=30)
    h.now[0] = h.now[0] + timedelta(seconds=45)
    status, alert = h.run()
    assert status == "rejected" and "stale" in alert["reason"]


def test_below_minimum_qty_rejected(make_harness):
    h = make_harness()
    status, alert = h.run(size={"mode": "usd", "value": 10})  # 0.0002 BTC < 0.001 min
    assert status == "rejected" and "below the minimum order qty" in alert["reason"]


def test_limit_price_rounded_conservatively(make_harness):
    h = make_harness()
    h.run(order_type="limit", price="49999.97")
    h.run(action="sell", order_type="limit", price="50000.01", strategy_id="multi")
    buy, sell = (r for r in h.exchange.placed if not r.reduce_only)
    assert buy.price == D("49999.9")  # buy rounds down
    assert sell.price == D("50000.1")  # sell rounds up


def test_tp_sl_from_strategy_percent_rounded_to_tick(make_harness):
    data = make_strategies().model_dump()
    data["strategies"]["btc-trend-v1"].update(take_profit_pct=D("4"), stop_loss_pct=D("1.5"))
    h = make_harness(FakeExchange(price="50000.3"), strategies=type(make_strategies()).model_validate(data))
    h.run()
    [order] = h.exchange.placed
    assert order.take_profit == D("52000.3")  # 50000.3 * 1.04 = 52000.312
    assert order.stop_loss == D("49250.3")  # 50000.3 * 0.985 = 49250.2955


def test_tp_sl_on_wrong_side_rejected(make_harness):
    h = make_harness()
    status, alert = h.run(take_profit=49000)
    assert status == "rejected" and "take_profit 49000 must be above" in alert["reason"]
    status, alert = h.run(action="sell", strategy_id="multi", stop_loss=49000)
    assert status == "rejected" and "stop_loss 49000 must be above" in alert["reason"]


def test_entry_against_open_position_reverses(make_harness):
    h = make_harness(FakeExchange(positions=[position(side="short", size="0.005")]))
    status, alert = h.run(action="buy")
    assert status == "executed", alert["reason"]
    close, open_ = h.exchange.placed
    assert (close.side, close.qty, close.reduce_only) == ("Buy", D("0.005"), True)
    assert (open_.side, open_.qty, open_.reduce_only) == ("Buy", D("0.002"), False)
    assert close.order_link_id != open_.order_link_id
    assert [o["leg"] for o in h.store.list_orders()] == ["open", "reverse_close"]


def test_reversal_can_be_disabled_in_one_way_mode(make_harness):
    data = make_strategies().model_dump()
    data["strategies"]["btc-trend-v1"]["close_opposite_on_entry"] = False
    h = make_harness(FakeExchange(positions=[position(side="short")]), strategies=type(make_strategies()).model_validate(data))
    status, alert = h.run(action="buy")
    assert status == "rejected" and "close_opposite_on_entry is false" in alert["reason"]


def test_hedge_mode_position_indexes(make_harness):
    h = make_harness(FakeExchange(positions=[position(side="short", idx=2)]), bybit_position_mode="hedge")
    h.run(action="buy")
    close, open_ = h.exchange.placed
    assert close.position_idx == 2 and open_.position_idx == 1


def test_close_without_position_is_skipped(make_harness):
    h = make_harness()
    status, alert = h.run(action="close_long")
    assert status == "skipped" and alert["reason"] == "no open long position on BTCUSDT"
    assert h.exchange.placed == []


def test_strategy_exit_via_market_position_flat_closes_everything(make_harness):
    h = make_harness(FakeExchange(positions=[position(size="0.03")]))
    status, _ = h.run(action="sell", market_position="flat", symbol="BTCUSDT.P")
    assert status == "executed"
    [order] = h.exchange.placed
    assert (order.side, order.qty, order.reduce_only) == ("Sell", D("0.03"), True)


def test_partial_close_is_capped_at_position_size(make_harness):
    h = make_harness(FakeExchange(positions=[position(size="0.03")]))
    h.run(action="sell", market_position="long", size={"mode": "qty", "value": "0.0105"})
    h.run(action="close_long", size={"mode": "qty", "value": 5})
    partial, capped = h.exchange.placed
    assert partial.qty == D("0.010") and partial.reduce_only
    assert capped.qty == D("0.03")


def test_close_all_in_hedge_mode_closes_both_sides(make_harness):
    held = [position(side="long", size="0.01", idx=1), position(side="short", size="0.02", idx=2)]
    h = make_harness(FakeExchange(positions=held), bybit_position_mode="hedge")
    h.run(action="close_all")
    sides = {(o.side, o.qty, o.position_idx) for o in h.exchange.placed}
    assert sides == {("Sell", D("0.01"), 1), ("Buy", D("0.02"), 2)}


def test_large_close_split_into_max_market_qty_chunks(make_harness):
    h = make_harness(FakeExchange(positions=[position(size="250")]))
    h.run(action="close_long")
    assert [o.qty for o in h.exchange.placed] == [D("119"), D("119"), D("12")]
    assert len({o.order_link_id for o in h.exchange.placed}) == 3


def test_percent_equity_sizing(make_harness):
    h = make_harness(FakeExchange(equity="20000"))
    h.run(size={"mode": "percent_equity", "value": 5})  # 1000 USDT / 50000 = 0.02
    assert h.exchange.placed[0].qty == D("0.02")


def test_dry_run_places_nothing_but_records_everything(make_harness):
    inner = FakeExchange()
    h = make_harness(DryRunExchange(inner, D("10000")), dry_run=True)  # type: ignore[arg-type]
    status, alert = h.run()
    assert status == "dry_run"
    assert inner.placed == [] and inner.leverage_calls == []
    [order] = h.store.list_orders()
    assert order["status"] == "DryRun" and order["dry_run"] is True
    assert h.notifier.messages[-1].startswith("DRY RUN")


def test_exchange_rejection_recorded(make_harness):
    exchange = FakeExchange()
    exchange.place_error = OrderRejectedError("Available balance is insufficient (retCode 110007)", code=110007)
    h = make_harness(exchange)
    status, alert = h.run()
    assert status == "rejected" and "110007" in alert["reason"]
    assert h.store.list_orders()[0]["status"] == "Rejected"


def test_uncertain_order_flagged_for_manual_check(make_harness):
    exchange = FakeExchange()
    exchange.place_error = OrderUncertainError("could not confirm order")
    h = make_harness(exchange)
    status, _ = h.run()
    assert status == "error"
    assert h.store.list_orders()[0]["status"] == "Uncertain"
    assert "CHECK BYBIT MANUALLY" in h.notifier.messages[-1]


def test_market_order_cancelled_without_fill_is_a_rejection(make_harness):
    exchange = FakeExchange()
    exchange.order_status = "Cancelled"
    h = make_harness(exchange)
    status, alert = h.run()
    assert status == "rejected" and "ended Cancelled without a fill" in alert["reason"]


def test_failed_reverse_close_does_not_open(make_harness):
    exchange = FakeExchange(positions=[position(side="short")])
    exchange.place_error = OrderRejectedError("reduce-only rejected", code=110017)
    h = make_harness(exchange)
    status, _ = h.run(action="buy")
    assert status == "rejected"
    assert [o["leg"] for o in h.store.list_orders()] == ["reverse_close"]
