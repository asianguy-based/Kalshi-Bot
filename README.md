# Kalshi Node — invite-only arbitrage bot

A single-operator web app that scans Kalshi binary markets for arbitrage that
survives fees, and executes it behind hard risk limits.

Access is one password box. There is no signup, no username, no password reset.

---

## Quick start

```bash
git clone <this repo> && cd Kalshi-Bot

# 1. Generate secrets. Prints the access passphrase ONCE.
pip install bcrypt cryptography
python3 scripts/bootstrap_secrets.py

# 2. Point it at your domain
echo "DOMAIN=bot.yourdomain.com" >> .env

# 3. Up
docker compose up -d --build
```

Then open `https://bot.yourdomain.com`, enter the passphrase, paste your Kalshi
key ID and PEM private key, and press **VERIFY KALSHI KEY**.

`dry_run_mode` defaults to **true**. Leave it there until the log shows the
opportunities it finds are ones you agree with.

Run the tests:

```bash
cd web && python3 -m unittest test_arbitrage -v     # 27 tests
```

---

## Security model

**Access.** One shared passphrase, stored only as a bcrypt hash in the
environment (base64-wrapped, because raw bcrypt hashes contain `$` and get
mangled by shell expansion). Five free attempts per IP, then exponential
backoff to a 15-minute cap. No passphrase configured means every login is
refused — a misconfigured deployment is closed, not open.

**Secrets.** The app refuses to start if `SECRET_KEY`, `ENCRYPTION_KEY`, or the
passphrase hash is missing. Kalshi credentials are Fernet-encrypted at rest and
never returned by any API response.

**Sessions.** HttpOnly, SameSite=Lax, Secure, `session_protection="strong"`,
8-hour lifetime. Every state-changing API call requires a CSRF token bound to
the session.

**Trade surface.** `/api/bot/trade` accepts an opportunity **id**, not a ticker
and size. The server decides what may be traded and how large. A client cannot
name an arbitrary market.

**Transport.** Caddy terminates TLS with a real auto-renewed certificate. The
app container publishes no ports; it is reachable only through the proxy. HSTS,
CSP, `X-Frame-Options: DENY`, and `noindex` are set on every response.

**Container.** Runs as an unprivileged user with `no-new-privileges`. Only the
database is a volume — application code is baked into the image, so writing to
the volume cannot alter what executes.

---

## The strategy

The edge is not "two markets disagree." It is structural mispricing:

**1. YES/NO cross-spread (single market).** Buying YES and NO on the same
market guarantees a 100c payout per pair. If `ask_yes + ask_no < 100 - fees`,
that is locked profit.

The ask is derived from the *opposite* side's bid — `ask_yes = 100 - best_no_bid`.
Kalshi's orderbook returns resting bids only. Reading the `yes` book as an ask
is the classic mistake and it manufactures phantom profit.

**2. Mutually-exclusive event basket.** When exactly one outcome of an event
resolves YES, buying YES on every outcome pays 100c. If `sum(ask_yes) < 100 - fees`,
buy every leg.

This only works if the outcomes really are mutually exclusive *and* collectively
exhaustive. The API does not reliably tell you that, so baskets require an
explicit `event_allowlist`. Guessing here means buying a "basket" that isn't one.

**Fees are not optional.** Kalshi charges `ceil(0.07 × contracts × P × (1-P))`
dollars, peaking near 50c prices — roughly 1.75c per contract. A 1c gross spread
at 50c is a *loss*. Every opportunity is reported net of fees, and the test suite
asserts a 99c basket is rejected.

---

## Risk controls

| Control | Behaviour |
|---|---|
| `dry_run_mode` | Detects and logs, sends nothing. Default on. |
| `max_bet_amount` | Per-trade cost ceiling. |
| `max_exposure` | Total open exposure ceiling. |
| `daily_loss_limit` | Trips the breaker for the day when hit. |
| `max_orders_per_day` | Hard order count cap. |
| Consecutive errors | 5 in a row trips the breaker and stops the engine. |
| Re-validation | Orderbook re-fetched before every order; vanished edge cancels. |
| Fill-or-kill | Legs are FOK limit orders — never partial. |
| Partial-fill halt | If leg 2 fails after leg 1 filled, the bot halts for manual unwind. |

The breaker only resets from the dashboard, deliberately: a halt means a human
should look at the account before trading resumes.

**Why one worker.** The engine holds in-process state — the running thread, the
opportunity cache, the exposure counters. Two Gunicorn workers would be two bots
trading one account with two disagreeing views of risk.

---

## Configuration

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session signing. Required. |
| `ENCRYPTION_KEY` | Fernet key for stored credentials. Required. Changing it makes stored keys undecryptable. |
| `ACCESS_PASSPHRASE_HASH_B64` | Base64 of the bcrypt passphrase hash. Required. |
| `DOMAIN` | Hostname for the TLS certificate. |
| `REQUIRE_HTTPS` | Secure cookies + HSTS. Default true. |
| `TRUST_PROXY` | Read client IP from `X-Forwarded-For`. True behind Caddy. |
| `BOT_DB_PATH` | SQLite path. Default `/app/data/bot_data.db`. |

---

## Layout

```
web/app.py             routes, config validation, CSRF, security headers
web/auth.py            passphrase check + brute-force throttle
web/kalshi_client.py   RSA-PSS request signing, market + order endpoints
web/arbitrage.py       fee model, spread and basket detection
web/risk.py            limits, circuit breaker, P/L tracking
web/bot_engine.py      scan loop, opportunity cache, execution
web/test_arbitrage.py  27 tests over fees, detection, and risk
scripts/bootstrap_secrets.py
Caddyfile  docker-compose.yml
```
