"""Runtime configuration.

Two sources:

* environment / ``.env``: mode flags, secrets, deployment settings (``Settings``)
* ``config/strategies.yaml``: risk limits and per-strategy settings (``StrategiesFile``)
"""

from __future__ import annotations

import ipaddress
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas import STRATEGY_ID_PATTERN, SizeSpec, normalize_symbol

# Published at https://www.tradingview.com/support/solutions/43000529348-about-webhooks/
TRADINGVIEW_WEBHOOK_IPS = ("52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7")
MIN_SECRET_LENGTH = 16

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class ConfigError(RuntimeError):
    """Configuration is missing or unsafe; the service refuses to start."""


def parse_networks(raw: str) -> list[IPNetwork]:
    return [ipaddress.ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # --- Safety flags. Both default ON; live trading needs both explicitly false.
    dry_run: bool = True
    bybit_testnet: bool = True
    bybit_demo: bool = False  # Bybit "Demo Trading" (api-demo.bybit.com), alternative to testnet

    # --- Secrets (never logged; redacted from all log output)
    webhook_secret: SecretStr
    admin_token: SecretStr
    bybit_api_key: SecretStr | None = None
    bybit_api_secret: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # --- Webhook intake
    webhook_ip_allowlist_enabled: bool = True
    webhook_ip_allowlist: str = ",".join(TRADINGVIEW_WEBHOOK_IPS)
    trusted_proxies: str = ""  # CIDRs whose X-Forwarded-For we believe (the Caddy network)
    max_alert_age_seconds: float = Field(default=60, gt=0)
    max_body_bytes: int = Field(default=16_384, gt=0)

    # --- Kill switch
    kill_switch: bool = False
    kill_switch_allows_closes: bool = False

    # --- Exchange
    bybit_category: Literal["linear"] = "linear"
    bybit_settle_coin: str = "USDT"
    bybit_position_mode: Literal["one_way", "hedge"] = "one_way"
    bybit_recv_window: int = Field(default=5000, ge=1000, le=60_000)
    bybit_timeout: int = Field(default=10, ge=1, le=60)
    order_max_attempts: int = Field(default=4, ge=1, le=10)
    dry_run_equity_usd: Decimal = Field(default=Decimal("10000"), gt=0)

    # --- Storage / misc
    database_url: str = "sqlite:///./data/tvbot.db"
    strategies_file: Path = Path("config/strategies.yaml")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("bybit_position_mode", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        for name in ("webhook_secret", "admin_token"):
            if len(getattr(self, name).get_secret_value()) < MIN_SECRET_LENGTH:
                raise ValueError(f"{name.upper()} must be at least {MIN_SECRET_LENGTH} characters")
        if self.webhook_secret.get_secret_value() == self.admin_token.get_secret_value():
            raise ValueError("WEBHOOK_SECRET and ADMIN_TOKEN must be different")
        if self.bybit_testnet and self.bybit_demo:
            raise ValueError(
                "BYBIT_TESTNET and BYBIT_DEMO cannot both be true: Bybit demo trading runs on "
                "mainnet infrastructure. Set BYBIT_TESTNET=false to use demo trading."
            )
        if not self.dry_run and not self.has_api_credentials:
            raise ValueError("DRY_RUN=false requires BYBIT_API_KEY and BYBIT_API_SECRET")
        if bool(self.telegram_bot_token) != bool(self.telegram_chat_id):
            raise ValueError("set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or neither")
        try:
            allowlist = parse_networks(self.webhook_ip_allowlist)
            parse_networks(self.trusted_proxies)
        except ValueError as exc:
            raise ValueError(f"invalid IP/CIDR in WEBHOOK_IP_ALLOWLIST or TRUSTED_PROXIES: {exc}") from exc
        if self.webhook_ip_allowlist_enabled and not allowlist:
            raise ValueError("WEBHOOK_IP_ALLOWLIST is empty; to disable it set WEBHOOK_IP_ALLOWLIST_ENABLED=false")
        return self

    @property
    def has_api_credentials(self) -> bool:
        return bool(self.bybit_api_key and self.bybit_api_secret)

    @property
    def live(self) -> bool:
        """Real orders with real money."""
        return not self.dry_run and not self.bybit_testnet and not self.bybit_demo

    @property
    def environment(self) -> Literal["testnet", "demo", "mainnet"]:
        if self.bybit_testnet:
            return "testnet"
        return "demo" if self.bybit_demo else "mainnet"

    @property
    def mode_label(self) -> str:
        if self.dry_run:
            return f"DRY-RUN/{self.environment}"
        return "LIVE" if self.live else self.environment.upper()

    @property
    def ip_allowlist(self) -> list[IPNetwork]:
        return parse_networks(self.webhook_ip_allowlist)

    @property
    def trusted_proxy_networks(self) -> list[IPNetwork]:
        return parse_networks(self.trusted_proxies)

    def secret_values(self) -> list[str]:
        secrets = [self.webhook_secret, self.admin_token, self.bybit_api_key, self.bybit_api_secret, self.telegram_bot_token]
        return [s.get_secret_value() for s in secrets if s is not None]

    def mode_summary(self) -> dict[str, Any]:
        return {
            "label": self.mode_label,
            "dry_run": self.dry_run,
            "testnet": self.bybit_testnet,
            "demo": self.bybit_demo,
            "live": self.live,
            "environment": self.environment,
            "category": self.bybit_category,
            "position_mode": self.bybit_position_mode,
        }


def load_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'settings'}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"invalid environment configuration: {problems}") from None


# ---------------------------------------------------------------------------
# strategies.yaml
# ---------------------------------------------------------------------------


class RiskConfig(BaseModel):
    """Account-wide limits. Every new entry must pass all of them."""

    model_config = ConfigDict(extra="forbid")

    allowed_symbols: list[str] = Field(min_length=1)
    max_leverage: Decimal = Field(gt=0)
    max_open_positions: int = Field(ge=1)
    max_daily_loss_usd: Decimal = Field(gt=0)
    max_position_usd: dict[str, Decimal]
    include_unrealized_pnl_in_daily_loss: bool = True

    @field_validator("allowed_symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        return [normalize_symbol(s) for s in value]

    @field_validator("max_position_usd")
    @classmethod
    def _position_limits(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        if "default" not in value:
            raise ValueError("max_position_usd needs a 'default' entry")
        limits: dict[str, Decimal] = {}
        for key, limit in value.items():
            if limit <= 0:
                raise ValueError(f"max_position_usd[{key}] must be > 0")
            limits["default" if key == "default" else normalize_symbol(key)] = limit
        return limits

    def position_limit(self, symbol: str) -> Decimal:
        return self.max_position_usd.get(symbol, self.max_position_usd["default"])


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    allowed_symbols: list[str] = Field(min_length=1)
    default_size: SizeSpec | None = None
    default_leverage: Decimal | None = Field(default=None, gt=0)
    max_leverage: Decimal | None = Field(default=None, gt=0)
    max_position_usd: Decimal | None = Field(default=None, gt=0)
    take_profit_pct: Decimal | None = Field(default=None, gt=0)
    stop_loss_pct: Decimal | None = Field(default=None, gt=0, lt=100)
    # Entering long while short (or vice versa) first closes the opposite position.
    close_opposite_on_entry: bool = True

    @field_validator("allowed_symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        return [normalize_symbol(s) for s in value]


class StrategiesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk: RiskConfig
    strategies: dict[str, StrategyConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _cross_check(self) -> StrategiesFile:
        for strategy_id, strategy in self.strategies.items():
            if not re.fullmatch(STRATEGY_ID_PATTERN, strategy_id):
                raise ValueError(f"invalid strategy id {strategy_id!r}")
            outside = set(strategy.allowed_symbols) - set(self.risk.allowed_symbols)
            if outside:
                raise ValueError(f"strategy {strategy_id}: symbols {sorted(outside)} are not in risk.allowed_symbols")
            ceiling = min(x for x in (self.risk.max_leverage, strategy.max_leverage) if x is not None)
            if strategy.default_leverage is not None and strategy.default_leverage > ceiling:
                raise ValueError(f"strategy {strategy_id}: default_leverage exceeds max leverage {ceiling}")
        return self


def load_strategies(path: Path) -> StrategiesFile:
    if not path.exists():
        raise ConfigError(f"strategies file not found: {path} (copy config/strategies.example.yaml to get started)")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        return StrategiesFile.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid {path}:\n{exc}") from None
