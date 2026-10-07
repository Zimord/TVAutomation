import logging
from unittest.mock import MagicMock

import requests

from app.services.notifier import TelegramNotifier

TOKEN = "123456:SECRET-BOT-TOKEN"


def test_message_is_prefixed_with_mode():
    session = MagicMock()
    session.post.return_value.status_code = 200
    notifier = TelegramNotifier(TOKEN, "42", "TESTNET", client=session)
    notifier.send("ORDER filled")
    notifier.close()
    call = session.post.call_args
    assert call.args[0] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert call.kwargs["json"]["chat_id"] == "42"
    assert call.kwargs["json"]["text"] == "[TESTNET] ORDER filled"


def test_failures_never_log_the_token(caplog):
    session = MagicMock()
    session.post.side_effect = requests.exceptions.ConnectionError(f"failed to reach https://api.telegram.org/bot{TOKEN}/sendMessage")
    notifier = TelegramNotifier(TOKEN, "42", "LIVE", client=session)
    with caplog.at_level(logging.WARNING):
        notifier.send("hello")
        notifier.close()
    assert "telegram send failed" in caplog.text
    assert TOKEN not in caplog.text
    assert all(TOKEN not in str(vars(r)) for r in caplog.records)
