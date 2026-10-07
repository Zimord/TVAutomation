from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import ConfigError, Settings, StrategiesFile, load_strategies
from tests.conftest import STRATEGIES, make_settings

ROOT = Path(__file__).resolve().parents[1]


def test_safe_defaults():
    settings = Settings(_env_file=None, webhook_secret="w" * 32, admin_token="a" * 32)
    assert settings.dry_run is True and settings.bybit_testnet is True and settings.live is False
    assert settings.mode_label == "DRY-RUN/testnet"
    assert settings.webhook_ip_allowlist_enabled and len(settings.ip_allowlist) == 4


def test_live_requires_both_flags_off(tmp_path):
    assert make_settings(tmp_path, dry_run=False, bybit_testnet=True).live is False
    assert make_settings(tmp_path, dry_run=True, bybit_testnet=False).live is False
    assert make_settings(tmp_path, dry_run=False, bybit_testnet=False, bybit_demo=True).live is False
    live = make_settings(tmp_path, dry_run=False, bybit_testnet=False)
    assert live.live is True and live.mode_label == "LIVE"


def test_trading_requires_api_keys(tmp_path):
    with pytest.raises(ValidationError, match="requires BYBIT_API_KEY"):
        make_settings(tmp_path, dry_run=False, bybit_api_key=None, bybit_api_secret=None)
    assert make_settings(tmp_path, dry_run=True, bybit_api_key=None, bybit_api_secret=None).has_api_credentials is False


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"webhook_secret": "short"}, "at least 16"),
        ({"admin_token": "x" * 10}, "at least 16"),
        ({"admin_token": "test-webhook-secret-0123456789"}, "must be different"),
        ({"bybit_testnet": True, "bybit_demo": True}, "cannot both be true"),
        ({"telegram_bot_token": "123:abc"}, "TELEGRAM_CHAT_ID"),
        ({"webhook_ip_allowlist": "not-an-ip"}, "invalid IP"),
        ({"webhook_ip_allowlist": " , "}, "empty"),
    ],
)
def test_unsafe_settings_rejected(tmp_path, overrides, message):
    with pytest.raises(ValidationError, match=message):
        make_settings(tmp_path, **overrides)


def _strategies(**patch):
    data = yaml.safe_load(yaml.safe_dump(STRATEGIES))
    for path, value in patch.items():
        node = data
        *parents, leaf = path.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return data


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"strategies.btc-trend-v1.allowed_symbols": ["DOGEUSDT"]}, "not in risk.allowed_symbols"),
        ({"strategies.btc-trend-v1.default_leverage": 4}, "default_leverage exceeds"),
        ({"risk.max_position_usd": {"BTCUSDT": 100}}, "default"),
        ({"risk.allowed_symbols": []}, "at least 1"),
        ({"strategies.btc-trend-v1.unknown_key": 1}, "Extra inputs"),
        ({"strategies.btc-trend-v1.stop_loss_pct": 150}, "less than 100"),
    ],
)
def test_invalid_strategies(patch, message):
    with pytest.raises(ValidationError, match=message):
        StrategiesFile.model_validate(_strategies(**patch))


def test_strategy_symbols_are_normalized():
    data = _strategies(**{"risk.allowed_symbols": ["btcusdt.p", "ETHUSDT", "solusdt"],
                          "strategies.btc-trend-v1.allowed_symbols": ["BYBIT:BTCUSDT.P"]})
    cfg = StrategiesFile.model_validate(data)
    assert cfg.risk.allowed_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert cfg.strategies["btc-trend-v1"].allowed_symbols == ["BTCUSDT"]
    assert cfg.risk.position_limit("BTCUSDT") == Decimal(2000)
    assert cfg.risk.position_limit("ETHUSDT") == Decimal(1000)


def test_shipped_example_config_is_valid():
    cfg = load_strategies(ROOT / "config" / "strategies.example.yaml")
    assert "btc-trend-v1" in cfg.strategies


def test_missing_strategies_file(tmp_path):
    with pytest.raises(ConfigError, match="copy config/strategies.example.yaml"):
        load_strategies(tmp_path / "nope.yaml")
