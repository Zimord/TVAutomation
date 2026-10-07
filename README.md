# TradingView → Bybit webhook trader

A self-hosted service that receives TradingView alert webhooks and places the matching orders on
Bybit (V5 API, USDT perpetuals). It records every alert, every risk decision and every order result
in SQLite, and can notify you on Telegram.

```
TradingView ──HTTPS POST──▶ Caddy (TLS, :443) ──▶ FastAPI /webhook
                                                     │  IP allowlist → secret → schema → dedupe
                                                     │  store alert, enqueue, return 200 (< 3 s)
                                                     ▼
                                             worker thread (one alert at a time, FIFO)
                                                     │  strategy → kill switch → symbol whitelist
                                                     │  → leverage → sizing/rounding → position limit
                                                     │  → max open positions → daily loss → TP/SL
                                                     ▼
                                             Bybit V5 (pybit)  ──▶  SQLite + Telegram
```

> **Risk warning.** This software places real orders with real money when configured to. Automated
> trading can lose money quickly, including through bugs, bad alerts, exchange outages or market gaps.
> Run it on testnet first, start tiny, and only trade funds you can afford to lose.

---

## Contents

1. [Safety model](#1-safety-model)
2. [Quick start (local, dry run)](#2-quick-start-local-dry-run)
3. [Configuration](#3-configuration)
4. [Webhook payload](#4-webhook-payload)
5. [TradingView alert setup and templates](#5-tradingview-alert-setup-and-templates)
6. [Bybit API keys (testnet first)](#6-bybit-api-keys-testnet-first)
7. [VPS setup](#7-vps-setup)
8. [Domain and HTTPS](#8-domain-and-https)
9. [Operating the service](#9-operating-the-service)
10. [End-to-end test against testnet](#10-end-to-end-test-against-testnet)
11. [Unit tests](#11-unit-tests)
12. [Going-live checklist](#12-going-live-checklist)
13. [Troubleshooting](#13-troubleshooting)
14. [Design notes and limitations](#14-design-notes-and-limitations)
15. [API and documentation verification (Oct 2026)](#15-api-and-documentation-verification-oct-2026)

---

## 1. Safety model

| `DRY_RUN` | `BYBIT_TESTNET` | `BYBIT_DEMO` | Mode | What happens |
|---|---|---|---|---|
| `true` (default) | `true` (default) | `false` | **DRY-RUN/testnet** | Full pipeline against testnet market data. **No orders are sent.** |
| `false` | `true` | `false` | **TESTNET** | Real orders on Bybit testnet (fake money). |
| `false` | `false` | `true` | **DEMO** | Real orders on Bybit Demo Trading (fake money, mainnet prices). |
| `true` | `false` | `false` | **DRY-RUN/mainnet** | Mainnet prices and account data (if keys are set). **No orders are sent.** |
| `false` | `false` | `false` | **LIVE** | Real orders, real money. |

Both safety flags default to on. Live trading only happens when you explicitly set **both** to
`false`. Other safeguards:

* **Authentication.** A shared secret goes in the JSON body (TradingView can't send custom headers) and
  is compared in constant time. It's stripped before anything is stored or logged.
* **Source IP allowlist.** Only TradingView's published webhook IPs are accepted. `X-Forwarded-For`
  is trusted only when the request comes from Caddy's Docker network.
* **Idempotency.** `strategy_id:alert_id` is a UNIQUE key in the database, so a replayed alert is
  acknowledged but never executed. Each order also gets a deterministic Bybit `orderLinkId` derived
  from that key, so retries can't create a second order.
* **Safe retries.** A transient API error is retried with backoff. When the outcome is unknown (for
  example a timeout), the service first looks the order up by `orderLinkId`, and only resends with
  the *same* `orderLinkId` if the order isn't there.
* **Risk limits** in `config/strategies.yaml`:
  * symbol whitelist (global and per strategy)
  * max leverage
  * max position notional per symbol
  * max open positions
  * max daily loss (realized and unrealized, per UTC day)

  Anything that breaks a limit is rejected and recorded with the reason.
* **Kill switch.** `KILL_SWITCH=true` in the environment, or `POST /kill`, rejects every new order.
  The runtime flag persists across restarts.
* **Stale alerts are never replayed.** Alerts still queued when the process stops are marked
  `abandoned` at the next start. An alert that waited longer than `MAX_ALERT_AGE_SECONDS` is rejected.
* **API key self-check.** At startup the service queries the key's permissions. In LIVE mode it
  **refuses to start** if the key can withdraw or isn't IP-restricted.
* **Secrets** come only from `.env`. They're redacted from every log line, and the Telegram token
  never appears in logs.

---

## 2. Quick start (local, dry run)

Requires Python 3.12+ (Docker uses 3.12).

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env                 # then set WEBHOOK_SECRET and ADMIN_TOKEN
cp config/strategies.example.yaml config/strategies.yaml

# Local testing only: there is no proxy and requests come from 127.0.0.1
export WEBHOOK_IP_ALLOWLIST_ENABLED=false   # Windows PowerShell: $env:WEBHOOK_IP_ALLOWLIST_ENABLED="false"

uvicorn app.main:create_app --factory --port 8000
```

Then, in another terminal:

```bash
python scripts/send_test_webhooks.py
```

In dry run without API keys, the service uses Bybit's public market data (instrument rules and
prices), so rounding and sizing are realistic. The account is assumed flat, with
`DRY_RUN_EQUITY_USD` of equity.

Always run with **exactly one worker process**. The alert queue and its worker thread live inside the process.

---

## 3. Configuration

### `.env` (secrets and mode flags)

See [.env.example](.env.example) for the full list with comments. The important ones:

| Variable | Default | Notes |
|---|---|---|
| `DRY_RUN` | `true` | Simulate orders. |
| `BYBIT_TESTNET` | `true` | Use `api-testnet.bybit.com`. |
| `BYBIT_DEMO` | `false` | Use Bybit Demo Trading. Needs `BYBIT_TESTNET=false`. |
| `WEBHOOK_SECRET` | – | Required, at least 16 chars. Goes in every alert message. |
| `ADMIN_TOKEN` | – | Required, at least 16 chars, and different from the webhook secret. Used as a Bearer token for the admin API. |
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | – | Required unless `DRY_RUN=true`. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | – | Optional. Set both or neither. |
| `WEBHOOK_IP_ALLOWLIST_ENABLED` | `true` | Set to `false` only for local testing. |
| `WEBHOOK_IP_ALLOWLIST` | TradingView's 4 IPs | Comma-separated IPs or CIDRs. |
| `TRUSTED_PROXIES` | (set by compose) | CIDRs allowed to set `X-Forwarded-For`. |
| `MAX_ALERT_AGE_SECONDS` | `60` | Alerts queued longer than this are rejected. |
| `KILL_SWITCH` | `false` | Hard stop. `POST /resume` can't override it. |
| `KILL_SWITCH_ALLOWS_CLOSES` | `false` | Lets close actions through while the kill switch is active. |
| `BYBIT_POSITION_MODE` | `one_way` | `one_way` or `hedge`. Must match your Bybit account setting. |

Generate secrets with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### `config/strategies.yaml` (risk and strategies)

Copy [config/strategies.example.yaml](config/strategies.example.yaml). The file is validated at
startup, and the service refuses to start on any error: unknown keys, a strategy symbol missing from
the global whitelist, or a `default_leverage` above the maximum. Restart after editing.

```yaml
risk:                                  # account-wide; applies to every entry
  allowed_symbols: [BTCUSDT, ETHUSDT]
  max_leverage: 5
  max_open_positions: 3                # symbols with an open position
  max_daily_loss_usd: 250              # entries rejected once today's PnL <= -250
  include_unrealized_pnl_in_daily_loss: true
  max_position_usd: {default: 1000, BTCUSDT: 2500}   # notional per symbol, per side

strategies:
  btc-trend-v1:
    allowed_symbols: [BTCUSDT]
    default_size: {mode: usd, value: 100}
    default_leverage: 2
    max_leverage: 3                    # effective max = min(global, strategy, Bybit's)
    max_position_usd: 1000             # effective = min(global for symbol, strategy)
    stop_loss_pct: 2.0                 # used when the alert has no stop_loss
    take_profit_pct: 4.0
    close_opposite_on_entry: true
```

All USD amounts are **notional** values in USDT, not margin. Leverage only changes how much margin a
position uses.

---

## 4. Webhook payload

`POST /webhook` with a JSON body. TradingView sends `text/plain` unless your message is valid JSON;
the service parses the body either way.

```json
{
  "secret": "…",
  "strategy_id": "btc-trend-v1",
  "symbol": "BTCUSDT",
  "action": "buy",
  "order_type": "market",
  "price": 65000.5,
  "size": {"mode": "usd", "value": 100},
  "take_profit": 68000,
  "stop_loss": 63500,
  "leverage": 2,
  "alert_id": "BTCUSDT.P-L-buy-2026-10-07T12:00:00Z",
  "market_position": "long"
}
```

| Field | Required | Description |
|---|---|---|
| `secret` | yes | Must equal `WEBHOOK_SECRET`. |
| `strategy_id` | yes | Must exist and be enabled in `strategies.yaml`. |
| `symbol` | yes | Bybit symbol. TradingView forms are normalised: `BTCUSDT.P` and `BYBIT:BTCUSDT.P` both become `BTCUSDT`. |
| `action` | yes | `buy`, `sell`, `close_long`, `close_short` or `close_all` (case-insensitive). |
| `order_type` | no | `market` (default) or `limit`. |
| `price` | for limit | Limit price, rounded to the tick size. Buys round down and sells round up. |
| `size` | no | `{"mode": "usd" \| "percent_equity" \| "qty", "value": >0}`. Defaults to the strategy's `default_size`. |
| `take_profit` / `stop_loss` | no | Absolute prices, rounded to the tick size. Default to the strategy's `*_pct`. Must be on the correct side of the entry price. |
| `leverage` | no | Set on Bybit before the entry. Defaults to `default_leverage`. Rejected above the maximum. |
| `alert_id` | yes | Idempotency key, unique per strategy. Up to 128 chars. |
| `market_position` | no | TradingView's `{{strategy.market_position}}`. Lets a single strategy alert tell exits apart from entries (see below). |

Size modes:

* `usd`: notional in USDT. `100` means $100 of exposure.
* `percent_equity`: notional as a percentage of total account equity. `5` means 5% of equity and
  `200` means 2× equity.
* `qty`: quantity in the base coin. `0.01` means 0.01 BTC.

Quantities are always rounded **down** to the lot size. An order below Bybit's minimum quantity or
minimum notional is rejected, never rounded up.

### What each action does

| `action` | `market_position` | Result |
|---|---|---|
| `buy` | absent or `long` | Open or add to a long. If a short is open and `close_opposite_on_entry` is true, the short is closed first (reduce-only) and then the long is opened. |
| `sell` | absent or `short` | Open or add to a short. Mirror of the above. |
| `sell` | `flat` | Close the **whole** long (a strategy exit). |
| `buy` | `flat` | Close the **whole** short. |
| `sell` | `long` | **Partial** close of the long by `size` (or the whole long if `size` is omitted). |
| `buy` | `short` | **Partial** close of the short. |
| `close_long` / `close_short` | – | Close that side: the whole position, or `size` if given (capped at the position). |
| `close_all` | – | Close both sides on the symbol. |

Closes are reduce-only market orders. They skip the position-size, open-positions and daily-loss
limits, because they only reduce risk, but they still respect the kill switch. Closing when there's
no position is recorded as `skipped`.

### HTTP responses

TradingView ignores the response body, but the codes show up in its alert log:

| Code | Meaning |
|---|---|
| `200 {"status":"accepted"}` | Stored and queued. The outcome is visible in `/alerts`, `/orders` and Telegram. |
| `200 {"status":"duplicate"}` | Already seen. Ignored. |
| `400` | The body isn't a JSON object. |
| `401` | Missing or wrong secret. |
| `403` | The source IP isn't allowlisted. |
| `413` | The body is too large. |
| `422` | Schema error. The response lists the offending fields. |

---

## 5. TradingView alert setup and templates

### Before you start

* **Webhook alerts need a paid TradingView plan, and 2-factor authentication must be enabled** on
  the TradingView account.
* Put Bybit perpetual charts on the `BYBIT:BTCUSDT.P` symbol. `{{ticker}}` then renders as
  `BTCUSDT.P`, which the service normalises.
* TradingView only posts to ports 80/443, gives up after **3 seconds**, and doesn't support IPv6
  (your domain needs an A record).

### Creating an alert

1. On the chart, open **Create Alert**.
   * For a strategy, choose it under *Condition* and pick *Order fills only*.
   * For an indicator, choose its condition and *Once per bar close*.
2. On the **Notifications** tab, enable **Webhook URL** and enter `https://bot.example.com/webhook`.
3. Paste one of the templates below into **Message**, and replace `YOUR_WEBHOOK_SECRET` and the
   `strategy_id`.
4. After it fires, check the **Webhook status** column in the alert log, then `GET /alerts` on the service.

`alert_id` must be unique per firing. `{{timenow}}` has one-second resolution, so combine it with
`{{ticker}}`, plus the order id and action for strategies. This stops two different alerts in the
same second from looking like duplicates.

### Template A: indicator or manual alert (one alert per action)

Create one alert per signal. This one buys; copy it and change `"action"` for the others
(`"sell"`, `"close_long"`, `"close_short"`, `"close_all"`):

```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "strategy_id": "btc-trend-v1",
  "symbol": "{{ticker}}",
  "action": "buy",
  "order_type": "market",
  "size": {"mode": "usd", "value": 100},
  "alert_id": "{{ticker}}-buy-{{timenow}}"
}
```

A limit entry at the bar's close, with explicit stop/target values taken from indicator plots:

```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "strategy_id": "btc-trend-v1",
  "symbol": "{{ticker}}",
  "action": "buy",
  "order_type": "limit",
  "price": {{close}},
  "stop_loss": {{plot("Stop")}},
  "take_profit": {{plot("Target")}},
  "alert_id": "{{ticker}}-buy-limit-{{timenow}}"
}
```

### Template B: any strategy, one alert for everything

`market_position` tells exits apart from entries. Sizing comes from the strategy's `default_size`:

```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "strategy_id": "btc-trend-v1",
  "symbol": "{{ticker}}",
  "action": "{{strategy.order.action}}",
  "market_position": "{{strategy.market_position}}",
  "order_type": "market",
  "alert_id": "{{ticker}}-{{strategy.order.id}}-{{strategy.order.action}}-{{timenow}}"
}
```

* Entries and reversals open `default_size`. Reversals close the old side first.
* Full exits (`market_position` = `flat`) close the whole position.
* A partial exit closes the whole position, because this template sends no size. Pyramiding adds
  `default_size` on each add. If you need exact quantities, use Template C.
* Avoid `"size": {"mode": "qty", "value": {{strategy.order.contracts}}}` with **reversing** strategies.
  On a reversal TradingView's contract count includes the closing quantity, so the new position
  would be oversized (the position limit would usually catch it).

### Template C: Pine Script, explicit message per order (most control)

Each `strategy.*` call carries its own JSON fragment in `alert_message`. Your secret stays in the
alert dialog, not in the script. Set the alert **Message** to exactly:

```
{"secret": "YOUR_WEBHOOK_SECRET", "alert_id": "{{ticker}}-{{strategy.order.id}}-{{strategy.order.action}}-{{timenow}}", {{strategy.order.alert_message}}}
```

Pine Script v6 example:

```pine
//@version=6
strategy("BTC trend (webhook)", overlay = true)

fast = ta.ema(close, 20)
slow = ta.ema(close, 50)

// JSON fragments (no braces). The alert message wraps them with the secret and alert_id.
base(action) => '"strategy_id": "btc-trend-v1", "symbol": "' + syminfo.ticker + '", "action": "' + action + '"'
longMsg  = base("buy")  + ', "size": {"mode": "usd", "value": 100}, "stop_loss": ' + str.tostring(close * 0.98, format.mintick)
shortMsg = base("sell") + ', "size": {"mode": "usd", "value": 100}, "stop_loss": ' + str.tostring(close * 1.02, format.mintick)

if ta.crossover(fast, slow)
    strategy.entry("L", strategy.long, alert_message = longMsg)    // reverses an open short
if ta.crossunder(fast, slow)
    strategy.entry("S", strategy.short, alert_message = shortMsg)  // reverses an open long

// Explicit exits, e.g. a time stop:
if strategy.position_size > 0 and bar_index - strategy.opentrades.entry_bar_index(0) > 50
    strategy.close("L", alert_message = base("close_long"))
```

Every `strategy.entry`, `strategy.close` and `strategy.exit` call that can fill **must** set
`alert_message`. Otherwise the placeholder renders empty, the body becomes invalid JSON, and the alert
is rejected with `400`. That's safe, but the order is lost. Don't duplicate stops: if Bybit holds the
`stop_loss` (as above), don't also send a `strategy.exit` stop for the same level.

---

## 6. Bybit API keys (testnet first)

Always create a key with **trade permission only, withdrawals disabled, and IP-restricted to your
server**. The service checks this at startup. In LIVE mode it refuses to start with a key that can
withdraw or isn't IP-bound.

### Testnet

1. Register at <https://testnet.bybit.com>. This is a separate account from mainnet.
2. Request test funds from the testnet **Assets** page (the faucet).
3. Make sure the account is a **Unified Trading Account**. New accounts are; the service reads
   equity from `accountType=UNIFIED`.
4. Go to **Profile → API → Create New Key → System-generated API Keys**.
   * Usage: **API Transaction**. Permissions: **Read-Write**.
   * **IP restriction:** choose *Only IPs with permissions granted…* and enter your VPS's public
     IPv4. For local testing from home, add your home IP too, or create a separate key.
   * Tick **only** the contract/Unified Trading permissions **Orders** and **Positions**.
   * Leave **Wallet** (transfers and withdrawals), Exchange, Earn, Copy Trading and the rest **unticked**.
5. Copy the key and secret into `.env` as `BYBIT_API_KEY` and `BYBIT_API_SECRET`, then set
   `DRY_RUN=false` and leave `BYBIT_TESTNET=true`.
6. In the testnet trading UI, check the position mode for USDT perpetuals. One-way is the default
   and matches `BYBIT_POSITION_MODE=one_way`. Also choose cross or isolated margin per symbol;
   the service doesn't change margin mode.

**Demo Trading** is an alternative to testnet. It runs fake funds against mainnet prices. Log in to
mainnet, switch to *Demo Trading*, create an API key there, then set `BYBIT_TESTNET=false` and
`BYBIT_DEMO=true`.

### Mainnet

Follow the same steps on <https://www.bybit.com>. Strongly consider creating a **sub-account** that
holds only the capital this bot may trade, and create the key on that sub-account. This caps the
damage from any bug or bad alert. It also makes the daily-loss limit accurate, because closed PnL is
measured account-wide.

---

## 7. VPS setup

Any small Linux VPS works: 1 vCPU and 1 GB RAM is plenty. Two things to check:

* **Location.** Bybit blocks API access from restricted jurisdictions, so check Bybit's terms for
  both you and the VPS region. Bybit's servers are in Asia, so Singapore or Tokyo gives the lowest
  order latency. TradingView's webhook senders are AWS us-west-2; their latency matters little,
  because the service replies in milliseconds.
* **IPv4.** The VPS needs a static public IPv4 address. Bind your API key to it.

Ubuntu 24.04 example, run as root and then as your user:

```bash
# 1. Admin user with SSH key login (then disable password auth in /etc/ssh/sshd_config)
adduser deploy && usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy

# 2. Updates, firewall, brute-force protection, accurate clock (Bybit rejects skewed timestamps)
apt update && apt -y upgrade
apt -y install ufw fail2ban unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
timedatectl set-ntp true
ufw default deny incoming && ufw default allow outgoing
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp
ufw enable

# 3. Docker Engine + compose plugin
curl -fsSL https://get.docker.com | sh
usermod -aG docker deploy
```

Docker bypasses ufw for *published* ports. That's fine here, because only Caddy's 80/443 are
published and the app's port 8000 is internal to the compose network. Never add a `ports:` mapping
to the `app` service.

Deploy (as `deploy`):

```bash
git clone <your-repo-url> tvbot && cd tvbot      # or copy the folder with scp/rsync
cp .env.example .env && chmod 600 .env && nano .env
cp config/strategies.example.yaml config/strategies.yaml && nano config/strategies.yaml
docker compose up -d --build
docker compose logs -f app
```

---

## 8. Domain and HTTPS

1. Create a DNS **A record**, for example `bot.example.com`, pointing to the VPS IPv4. An AAAA
   record alone won't work, because TradingView doesn't support IPv6.
2. Set `DOMAIN=bot.example.com` and `ACME_EMAIL=you@example.com` in `.env`.
3. Run `docker compose up -d`. Caddy gets a Let's Encrypt certificate automatically and renews it.
   Ports 80 and 443 must be reachable from the internet for this.
4. Verify:

```bash
curl -i https://bot.example.com/health                                          # 401 = TLS + app OK
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" https://bot.example.com/health    # 200 + mode details
curl -s -X POST https://bot.example.com/webhook -d '{}'   # 403 from your own IP: the allowlist works
```

---

## 9. Operating the service

Every endpoint except `/webhook` requires `Authorization: Bearer $ADMIN_TOKEN`. The interactive
docs (`/docs`, `/openapi.json`) are disabled.

```bash
H="Authorization: Bearer $ADMIN_TOKEN"; U=https://bot.example.com

curl -s -H "$H" "$U/health"                       # mode, kill switch, queue depth, DB, worker
curl -s -H "$H" "$U/health?deep=true"             # + Bybit reachability
curl -s -H "$H" "$U/positions"                    # live positions from Bybit
curl -s -H "$H" "$U/orders?limit=20"              # recent orders (filters: symbol, strategy_id)
curl -s -H "$H" "$U/alerts?limit=20"              # alerts + every decision step and reason
curl -s -H "$H" "$U/alerts?status=rejected"
curl -s -X POST -H "$H" -H "Content-Type: application/json" -d '{"reason":"volatility"}' "$U/kill"
curl -s -X POST -H "$H" "$U/resume"
```

**Alert statuses**

| Status | Meaning |
|---|---|
| `queued` / `processing` | In progress. |
| `executed` | Orders placed. |
| `dry_run` | Simulated. |
| `rejected` | A risk rule or the exchange refused it; the reason is recorded. |
| `skipped` | Nothing to close. |
| `error` | An exchange or internal error. |
| `duplicate` | Already seen. |
| `unauthorized` | Missing or wrong secret. |
| `invalid` | Malformed body or payload. |
| `abandoned` | Dropped by a restart and not replayed. |

**Order statuses**

* Bybit's own statuses: `New`, `Filled`, `PartiallyFilled`, `Cancelled`, `Rejected`, and so on.
* `DryRun`: simulated order.
* `Submitted`: Bybit hadn't reported a state yet.
* `Uncertain`: the service couldn't confirm the order existed. **Check Bybit manually.** Any order
  left `Submitted` or `Uncertain` is reconciled at the next startup.

**Logs** are one JSON object per line on stdout:

```bash
docker compose logs -f app
```

**Telegram** reports fills, rejections, errors, invalid alerts, the kill switch, startup and restarts.
Every message is tagged with the mode, for example `[LIVE]` or `[TESTNET]`. To set it up, create a
bot with @BotFather and send it a message. Then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` in your browser to find your chat id.

**Backups.** The database lives in the `tvbot_app-data` Docker volume.

```bash
docker compose exec app python -c "import sqlite3; s=sqlite3.connect('data/tvbot.db'); d=sqlite3.connect('data/backup.db'); s.backup(d)"
docker compose cp app:/app/data/backup.db ./tvbot-$(date +%F).db
```

**Updating.** `git pull && docker compose up -d --build`. Queued alerts are not replayed after a
restart, so update when no signals are expected.

---

## 10. End-to-end test against testnet

The script [scripts/send_test_webhooks.py](scripts/send_test_webhooks.py) sends a sequence of
realistic alerts, then prints the resulting alerts, orders and positions. The sequence is:

* a market open, then an exact duplicate
* a request with the wrong secret, and an invalid payload
* a marketable limit order with an unrounded price
* a strategy-style exit using the `.P` ticker
* a short open and close, a `close_all` on a flat book, and a non-whitelisted symbol

It refuses to run against a LIVE instance unless you pass `--allow-live`.

1. Create testnet API keys ([section 6](#6-bybit-api-keys-testnet-first)), with your current IP
   allowed on the key.
2. In `.env`, set `DRY_RUN=false`, `BYBIT_TESTNET=true`, the keys, and
   `WEBHOOK_IP_ALLOWLIST_ENABLED=false` (local only).
3. Start the service:

   ```bash
   uvicorn app.main:create_app --factory --port 8000
   ```

4. In another terminal, run the script:

   ```bash
   python scripts/send_test_webhooks.py --strategy btc-trend-v1 --symbol BTCUSDT --usd 100
   ```

5. Compare the output with the testnet UI: order history, positions, and TP/SL on the position.

Against a deployed instance, the IP allowlist stays on, so test with real TradingView alerts instead.
Point an alert at a testnet deployment, or send the script's requests from a temporarily allowlisted
IP using `--base-url https://bot.example.com`.

---

## 11. Unit tests

```bash
pip install -r requirements-dev.txt
pytest
```

The Bybit client is mocked throughout; there are no network calls. Coverage:

| Area | What's tested |
|---|---|
| Payload validation | Symbol normalisation, type and NaN handling, action mapping |
| Rounding | Lot size, tick size, minimum quantity and notional, chunking |
| Risk checks | Every limit, kill switch, staleness, reversals, hedge mode, partial closes, dry run |
| Idempotency | Duplicates, including concurrent deliveries and across restarts; `orderLinkId` format |
| Bybit client | Retry and reconciliation: timeouts, 110072 duplicates, rate limits, definitive rejections |
| HTTP layer | Auth, IP allowlist and proxy handling, admin endpoints, startup key checks, log redaction |

---

## 12. Going-live checklist

- [ ] Ran on **testnet** (or Demo) with your real alerts for long enough to see entries, exits,
      reversals and rejections. Compared `/orders` with the TradingView Strategy Tester.
- [ ] Reviewed `GET /alerts?status=rejected`: every rejection was intended.
- [ ] Created a **mainnet sub-account** funded with only what this bot may risk.
- [ ] Created the mainnet API key on that sub-account:
  - [ ] Orders + Positions only
  - [ ] **No withdraw, no transfer**
  - [ ] **IP-restricted** to the VPS IP
- [ ] Bybit position mode matches `BYBIT_POSITION_MODE`, and margin mode (cross or isolated) is set
      per symbol the way you want.
- [ ] Ran a mainnet dry run (`DRY_RUN=true`, `BYBIT_TESTNET=false`, mainnet keys) for a while. This
      checks the key permissions at startup and shows sizing against your real equity, without
      sending orders.
- [ ] `strategies.yaml` limits are conservative:
  - [ ] small `max_position_usd`
  - [ ] low `max_leverage`
  - [ ] `max_daily_loss_usd` you can live with
  - [ ] whitelist contains only the symbols you trade
- [ ] Production secrets are fresh (not reused from testing), `.env` is `chmod 600`, and alert
      messages in TradingView use the new `WEBHOOK_SECRET`.
- [ ] `WEBHOOK_IP_ALLOWLIST_ENABLED=true`. `curl http://<vps-ip>:8000` fails, and posting to
      `/webhook` from your own IP returns 403.
- [ ] Telegram works (try `POST /kill` then `POST /resume`).
- [ ] Practised the kill switch: `POST /kill` → send an alert → it's rejected → `POST /resume`.
      Know how to flatten positions manually in the Bybit app.
- [ ] Database backup routine in place. The VPS clock is NTP-synced.
- [ ] Set `DRY_RUN=false` and `BYBIT_TESTNET=false`, then `docker compose up -d`. The logs show
      `LIVE TRADING ENABLED` and Telegram shows `[LIVE] service started`.
- [ ] First live trade at minimum size; verified on Bybit (order, position, TP/SL). Scale up gradually.

---

## 13. Troubleshooting

| Symptom | Likely cause |
|---|---|
| TradingView alert log shows the webhook failed or timed out | DNS A record, firewall (80/443), certificate not issued yet (`docker compose logs caddy`), or the domain only has AAAA. |
| `403` for real TradingView alerts | TradingView changed its IPs (check their docs and update `WEBHOOK_IP_ALLOWLIST`), or `TRUSTED_PROXIES` doesn't match the compose subnet. |
| `401` | `secret` in the alert doesn't match `WEBHOOK_SECRET` (watch for stray spaces or quotes). |
| `422` | Template error. The response and `/alerts` name the field. A common one is unquoted text in a number field. |
| Alert `rejected` | Read the reason in `/alerts` or Telegram: a risk limit, minimum order size, TP/SL on the wrong side, or a stale alert. |
| Bybit `10003` / `10004` | Wrong key or secret, or testnet keys used against mainnet (or the reverse). |
| Bybit `10010` | The request IP isn't in the key's IP whitelist. |
| Bybit `10002` | Server clock skew. Check with `timedatectl`. |
| Bybit `10001` "position idx not match position mode" | `BYBIT_POSITION_MODE` doesn't match the account. |
| Bybit `110007` / `110004` | Insufficient balance. |
| HTTP 403 from Bybit on every call | The VPS is in a region Bybit blocks, or an IP rate limit was hit. |
| Startup fails with "unsafe Bybit API key" | The key can withdraw or isn't IP-restricted (enforced in LIVE). |
| Startup fails with "strategies file not found" | Copy `config/strategies.example.yaml` to `config/strategies.yaml`. |

---

## 14. Design notes and limitations

* **USDT perpetuals (`linear`) only.** Spot is structured for but not implemented. Adding it means:
  * a spot branch in `BybitClient` that reads `basePrecision` / `minOrderAmt`
  * `marketUnit` for market buys
  * balances instead of positions, and no leverage

  The processor only talks to the `Exchange` protocol in `app/exchange/base.py`.
* **One process, one worker thread.** Alerts are handled strictly in arrival order, so risk checks
  can't race each other. Throughput is a few alerts per second, far more than TradingView will send.
* **Daily loss** is Bybit's closed PnL since 00:00 UTC (it includes fees) plus current unrealized
  PnL. It covers *all* activity on the account, hence the sub-account advice. Hitting the limit
  blocks new entries only; it doesn't flatten positions.
* **Resting limit orders** aren't counted against position limits, and stale ones aren't cancelled
  automatically. Fill status is captured at placement (market orders are polled until final). Later
  fills of limit orders show up in `/positions` and in Bybit.
* **TP/SL** are attached to the entry in `Full` mode with Bybit's default trigger (last price).
  `close_opposite_on_entry` reversals use two orders: a reduce-only close, then the new entry.
* **Kill switch** stops new orders. It doesn't cancel orders or close positions.
* **Strategy config** is read at startup; restart to apply changes. The database schema is created
  automatically. There are no migrations yet; add Alembic if you extend the models.

---

## 15. API and documentation verification (Oct 2026)

The design was checked against the current Bybit V5 docs, the pybit 5.17.0 source, and TradingView's
webhook documentation. Most details matched the original spec. These items needed a change or were
worth calling out:

| Topic | Finding | How it's handled |
|---|---|---|
| TradingView timeout | Exactly **3 seconds** | The endpoint stores, enqueues and returns in milliseconds. |
| TradingView IPs | `52.89.214.238`, `34.212.75.30`, `54.218.53.128`, `52.32.178.7`. **No IPv6.** | Default allowlist (configurable). An A record is required. |
| TradingView requirements | 2FA must be enabled to use webhook alerts | Documented in section 5. |
| TradingView content type | Bodies that aren't valid JSON are sent as `text/plain` | The body is parsed regardless of `Content-Type`. |
| TradingView guidance | TradingView advises against putting credentials in webhook bodies | Unavoidable (no custom headers), so the secret is a random service-specific token rather than an account credential. It travels only over HTTPS, is combined with the IP allowlist, and is never stored or logged. |
| Bybit perpetual tickers | `{{ticker}}` is `BTCUSDT.P` | Normalised to `BTCUSDT`. |
| Wallet balance | Only `accountType=UNIFIED` is supported | Classic accounts don't work; the startup check warns. |
| Market orders | Bybit converts market orders to IOC limits with slippage protection. `slippageTolerance` (2025) is optional. | A market order that ends `Cancelled` with no fill is reported as a rejection. |
| `orderLinkId` | At most 36 chars, `[A-Za-z0-9_-]`, unique | `tv-<24 hex of sha256(strategy:alert_id)>-<leg>` (≤ 32 chars). |
| Order lookup | `/v5/order/realtime` keeps only about 500 recent closed orders and can be cleared on Bybit restarts | Lookups fall back to `/v5/order/history`. |
| Set leverage | Returns `110043` when unchanged | Treated as success. The service also skips the call when the leverage already matches. |
| Position list | Linear requires `symbol` or `settleCoin` | Uses `settleCoin=USDT` with cursor pagination. |
| Closed PnL | 7-day maximum window | The daily window is always shorter. |
| pybit retries | pybit already retries `10002`/`10006` internally, and network errors only with `force_retry` (off) | Network errors go through the reconcile-then-resend logic. |
| Demo Trading | `api-demo.bybit.com` exists alongside testnet | Supported via `BYBIT_DEMO=true`. |
| API key info | `/v5/user/query-api` exposes `Withdraw` permission and bound IPs | Used for the startup safety check. |
| Starlette 1.7 TestClient | Now prefers `httpx2` over `httpx` | Dev dependency. Production uses `requests` for Telegram. |

---

## Project layout

```
app/
  main.py              FastAPI app: /webhook + admin API, lifespan (startup checks, worker)
  config.py            .env settings + strategies.yaml models and validation
  schemas.py           webhook payload schema, symbol normalisation
  security.py          constant-time secret check, admin auth, IP allowlist / proxy handling
  db.py, store.py      SQLAlchemy models (alerts, decisions, orders, app_state) and queries
  logging_setup.py     JSON logging with secret redaction
  exchange/
    base.py            exchange-agnostic types + Exchange protocol
    bybit.py           pybit adapter: rounding inputs, retries, orderLinkId reconciliation
    dry_run.py         real reads, simulated writes
  services/
    processor.py       intent resolution, risk checks, order planning and execution
    sizing.py          sizing and lot/tick rounding (pure functions)
    worker.py          single FIFO worker thread
    killswitch.py      env + persisted runtime kill switch
    notifier.py        Telegram
    startup.py         API key permission check, order reconciliation
config/strategies.example.yaml
scripts/send_test_webhooks.py
tests/                 pytest suite (Bybit mocked)
Dockerfile, docker-compose.yml, Caddyfile, .env.example
```
