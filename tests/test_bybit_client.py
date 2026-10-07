"""BybitClient retry/idempotency behaviour with the pybit session mocked."""

from decimal import Decimal as D
from unittest.mock import MagicMock

import pytest
import requests
from pybit.exceptions import FailedRequestError, InvalidRequestError

from app.exchange.base import ExchangeError, OrderRejectedError, OrderRequest, OrderUncertainError
from app.exchange.bybit import BybitClient, parse_linear_instrument

REQ = OrderRequest(
    symbol="BTCUSDT", side="Buy", order_type="Market", qty=D("0.002"), order_link_id="tv-abc-o", position_idx=0,
    take_profit=D("52000"), stop_loss=D("49000.5"),
)
FILLED = {"result": {"list": [{"orderId": "123", "orderLinkId": "tv-abc-o", "orderStatus": "Filled",
                               "avgPrice": "50000.1", "cumExecQty": "0.002", "rejectReason": "EC_NoError"}]}}
EMPTY = {"result": {"list": []}}


def api_error(code: int, msg: str = "error") -> InvalidRequestError:
    return InvalidRequestError(request="POST /v5/order/create", message=msg, status_code=code, time="00:00:00", resp_headers={})


def make_client(session: MagicMock, **kwargs) -> BybitClient:
    return BybitClient(api_key="k", api_secret="s", testnet=True, session=session, sleep=lambda _: None, max_attempts=3, **kwargs)


def link_ids(session: MagicMock) -> list[str]:
    return [c.kwargs["orderLinkId"] for c in session.place_order.call_args_list]


def test_order_params():
    session = MagicMock()
    session.place_order.return_value = {"result": {"orderId": "123", "orderLinkId": "tv-abc-o"}}
    session.get_open_orders.return_value = FILLED
    result = make_client(session).place_order(REQ)
    params = session.place_order.call_args.kwargs
    assert params == {
        "category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "0.002",
        "orderLinkId": "tv-abc-o", "positionIdx": 0, "tpslMode": "Full", "takeProfit": "52000", "stopLoss": "49000.5",
    }
    assert (result.order_id, result.status, result.avg_price, result.filled_qty) == ("123", "Filled", D("50000.1"), D("0.002"))
    assert result.reject_reason is None


def test_limit_and_reduce_only_params():
    session = MagicMock()
    session.place_order.return_value = {"result": {"orderId": "1"}}
    session.get_open_orders.return_value = EMPTY
    session.get_order_history.return_value = EMPTY
    req = OrderRequest(symbol="BTCUSDT", side="Sell", order_type="Limit", qty=D("1.50"), price=D("65000.0"),
                       order_link_id="x", position_idx=2, reduce_only=True)
    result = make_client(session).place_order(req)
    params = session.place_order.call_args.kwargs
    assert params["price"] == "65000" and params["timeInForce"] == "GTC" and params["qty"] == "1.5"
    assert params["reduceOnly"] is True and params["positionIdx"] == 2 and "tpslMode" not in params
    assert result.status == "Submitted"  # not found yet: Bybit processes orders asynchronously


def test_timeout_then_found_does_not_resend():
    session = MagicMock()
    session.place_order.side_effect = requests.exceptions.ReadTimeout("read timed out")
    session.get_open_orders.return_value = FILLED
    result = make_client(session).place_order(REQ)
    assert session.place_order.call_count == 1
    assert result.order_id == "123"


def test_timeout_then_not_found_resends_with_same_order_link_id():
    session = MagicMock()
    session.place_order.side_effect = [requests.exceptions.ConnectionError("reset"), {"result": {"orderId": "123"}}]
    session.get_open_orders.side_effect = [EMPTY, FILLED]
    session.get_order_history.return_value = EMPTY
    result = make_client(session).place_order(REQ)
    assert link_ids(session) == ["tv-abc-o", "tv-abc-o"]
    assert result.status == "Filled"


def test_duplicate_order_link_id_returns_existing_order():
    session = MagicMock()
    session.place_order.side_effect = [requests.exceptions.ReadTimeout(), api_error(110072, "OrderLinkedID is duplicate")]
    # First lookup misses (order not visible yet), second finds it.
    session.get_open_orders.side_effect = [EMPTY, FILLED]
    session.get_order_history.return_value = EMPTY
    result = make_client(session).place_order(REQ)
    assert session.place_order.call_count == 2
    assert result.order_id == "123"


def test_definitive_rejection_is_not_retried():
    session = MagicMock()
    session.place_order.side_effect = api_error(110007, "Available balance is insufficient")
    with pytest.raises(OrderRejectedError) as info:
        make_client(session).place_order(REQ)
    assert info.value.code == 110007
    assert session.place_order.call_count == 1
    session.get_open_orders.assert_not_called()


def test_rate_limit_is_retried():
    session = MagicMock()
    session.place_order.side_effect = [api_error(10006, "Too many visits"), {"result": {"orderId": "123"}}]
    session.get_open_orders.return_value = FILLED
    assert make_client(session).place_order(REQ).order_id == "123"
    assert link_ids(session) == ["tv-abc-o", "tv-abc-o"]


def test_persistent_rate_limit_gives_up_as_rejected():
    session = MagicMock()
    session.place_order.side_effect = api_error(10006, "Too many visits")
    with pytest.raises(OrderRejectedError):
        make_client(session).place_order(REQ)
    assert session.place_order.call_count == 3


def test_ambiguous_errors_exhaust_into_uncertain():
    session = MagicMock()
    session.place_order.side_effect = [api_error(10016, "Server error"), FailedRequestError(
        request="", message="Bad Request. Retries exceeded maximum.", status_code=400, time="", resp_headers=None),
        requests.exceptions.ReadTimeout()]
    session.get_open_orders.return_value = EMPTY
    session.get_order_history.return_value = EMPTY
    with pytest.raises(OrderUncertainError, match="check Bybit manually"):
        make_client(session).place_order(REQ)
    assert link_ids(session) == ["tv-abc-o"] * 3


def test_order_lookup_falls_back_to_history():
    session = MagicMock()
    session.get_open_orders.return_value = EMPTY
    session.get_order_history.return_value = FILLED
    assert make_client(session).get_order("BTCUSDT", "tv-abc-o").order_id == "123"


def test_set_leverage_not_modified_is_ok():
    session = MagicMock()
    session.set_leverage.side_effect = api_error(110043, "Set leverage has not been modified.")
    make_client(session).set_leverage("BTCUSDT", D("2"))
    assert session.set_leverage.call_args.kwargs == {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "2", "sellLeverage": "2"}


def test_reads_retry_transient_failures():
    session = MagicMock()
    session.get_tickers.side_effect = [requests.exceptions.ConnectionError(), api_error(10016), {"result": {"list": [{"lastPrice": "50000.5"}]}}]
    assert make_client(session).get_last_price("BTCUSDT") == D("50000.5")


def test_reads_do_not_retry_bad_requests():
    session = MagicMock()
    session.get_tickers.side_effect = api_error(10001, "params error")
    with pytest.raises(ExchangeError):
        make_client(session).get_last_price("BTCUSDT")
    assert session.get_tickers.call_count == 1


def test_instrument_parsing_and_cache():
    item = {
        "symbol": "BTCUSDT", "status": "Trading",
        "lotSizeFilter": {"maxOrderQty": "1190.000", "minOrderQty": "0.001", "qtyStep": "0.001",
                          "postOnlyMaxOrderQty": "1190.000", "maxMktOrderQty": "119.000", "minNotionalValue": "5"},
        "priceFilter": {"minPrice": "0.10", "maxPrice": "1999999.80", "tickSize": "0.10"},
        "leverageFilter": {"minLeverage": "1", "maxLeverage": "100.00", "leverageStep": "0.01"},
    }
    rules = parse_linear_instrument(item)
    assert (rules.qty_step, rules.min_qty, rules.max_market_qty, rules.tick_size, rules.min_notional) == (
        D("0.001"), D("0.001"), D("119"), D("0.1"), D("5"))
    session = MagicMock()
    session.get_instruments_info.return_value = {"result": {"list": [item]}}
    client = make_client(session)
    client.get_instrument("BTCUSDT")
    client.get_instrument("BTCUSDT")
    assert session.get_instruments_info.call_count == 1


def test_positions_paginate_and_skip_empty():
    session = MagicMock()
    session.get_positions.side_effect = [
        {"result": {"list": [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.01", "avgPrice": "50000", "markPrice": "51000",
                              "leverage": "2", "unrealisedPnl": "10", "positionIdx": 0},
                             {"symbol": "ETHUSDT", "side": "", "size": "0", "positionIdx": 0}],
                    "nextPageCursor": "page2"}},
        {"result": {"list": [{"symbol": "SOLUSDT", "side": "Sell", "size": "3", "avgPrice": "150", "markPrice": "140",
                              "leverage": "1", "unrealisedPnl": "30", "positionIdx": 0}], "nextPageCursor": ""}},
    ]
    positions = make_client(session).get_positions()
    assert [(p.symbol, p.side, p.size) for p in positions] == [("BTCUSDT", "long", D("0.01")), ("SOLUSDT", "short", D("3"))]
    assert positions[0].notional == D("510.00")
    assert session.get_positions.call_args_list[1].kwargs["cursor"] == "page2"


def test_api_key_check_detects_withdraw_permission():
    session = MagicMock()
    session.get_api_key_information.return_value = {"result": {
        "readOnly": 0, "ips": ["*"], "uta": 1,
        "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": ["AccountTransfer", "Withdraw"]}}}
    info = make_client(session).check_api_key()
    assert info["withdraw_enabled"] is True and info["ip_restricted"] is False and info["read_only"] is False


def test_unauthenticated_client_refuses_private_calls():
    client = BybitClient(api_key=None, api_secret=None, testnet=True, session=MagicMock())
    with pytest.raises(ExchangeError, match="credentials"):
        client.place_order(REQ)
