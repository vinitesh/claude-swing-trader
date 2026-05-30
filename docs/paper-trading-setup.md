# Paper Trading Setup — Step-by-Step

This guide gets two strategies (PullbackEMA + RSI(2)) trading the full S&P 500
on a free Alpaca paper account. After 60-90 days of paper performance, decide
whether to graduate to small live capital.

## ⚠️ Secrets handling — read this first

**Never paste API keys, secrets, or tokens into a chat assistant, AI tool,
shared terminal, log file, screenshot, or commit.** This includes Claude Code,
ChatGPT, Slack, screen recordings — anything that produces a transcript.

Once a secret is in a transcript or log, treat it as compromised even if the
medium "feels private". Rotate immediately: log into the provider, regenerate
the key, and the old one becomes worthless.

Workflow that keeps secrets out of harm's way:

1. Generate the key on the provider's website.
2. Open `.env` directly in a local editor (`vim`, `nano`, `code`, `TextEdit`).
3. Paste the value into the file. Save. Close the editor.
4. Verify the application picked it up with a one-liner that shows ONLY a
   prefix (never the full secret):
   ```bash
   .venv/bin/python -c "from core.config import init; s, _ = init(); \
       print('key prefix:', s.alpaca_api_key[:4] + '...' if s.alpaca_api_key else '(empty)'); \
       print('secret len:', len(s.alpaca_secret_key))"
   ```
5. To tell anyone (human or AI) about the keys, say *"I've added them to
   `.env`"* — don't paste the values.

If you accidentally leak a key:
- **Alpaca:** dashboard → API Keys → Regenerate / Delete.
- **Telegram bot:** message @BotFather, `/revoke`, generate a new bot.
- **GitHub PAT, AWS key, etc.:** revoke in the provider's settings; check git
  history with `git log -S 'leaked-prefix' --all` and rewrite history if
  needed.

The `.env` file itself is gitignored (see `.gitignore`) so you're safe from
accidentally committing it — but only as long as you don't override that.

## What you'll have running at the end

- **Daily run** at 4:05pm ET (after market close): scans S&P 500, picks signals,
  submits bracket orders to Alpaca paper account.
- **Both strategies competing** for $50k of paper capital ($25k cap each via
  `capital_allocation_usd`). The remaining $50k is buffer.
- **Telegram alert** for every signal, fill, and a daily summary.
- **SQLite trade log** in `trading.db` for per-strategy attribution.
- **Daily sync** at 7pm ET to reconcile broker state with our DB.

---

## Step 1 — Sign up for Alpaca paper trading

1. Go to https://alpaca.markets and create a free account.
2. After confirming your email, log in. The dashboard defaults to **Paper
   Trading** — confirm the toggle in the top-right says "Paper" (not "Live").
3. Click **Generate New Key** under "Paper Trading API Keys".
4. Copy both **API Key ID** and **Secret Key** — Alpaca only shows the secret
   ONCE.
5. Paper account starts with **$100,000 in simulated cash**.

## Step 2 — Configure `.env`

```bash
cd ~/Documents/Personal/swing_platform
cp .env.example .env
```

Edit `.env`:

```ini
# Alpaca PAPER credentials
ALPACA_API_KEY=PKxxxxxxxxxxxxxxxxxx
ALPACA_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_FEED=iex            # 'iex' is free; 'sip' requires a paid plan

# Trading mode — keep this paper for now!
TRADING_MODE=paper

# Database
DATABASE_URL=sqlite:///./trading.db

# Logging
LOG_LEVEL=INFO
LOG_DIR=./logs

# Telegram (optional but recommended — see Step 4)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

## Step 3 — Verify it works

```bash
cd ~/Documents/Personal/swing_platform
.venv/bin/python -m core.cli run-live --dry-run
```

Expected output:

```
Run complete — mode=dry-run
  Signals found:     N        # likely 5-50 depending on market
  Orders submitted:  N        # all dry-run (nothing went to broker)
  Skipped (dedupe):  0
  Rejected (risk):   0
```

If you see `exit status 1` or auth errors, your `.env` keys are wrong.

## Step 4 — (Optional) Telegram alerts

Without Telegram, alerts go to logs only. With Telegram, you get a push
notification every time a signal fires.

1. Open Telegram, message **@BotFather**, send `/newbot`. Pick a name. BotFather
   gives you a token like `123456789:AABBccDDeeFFggHHiiJJkkLLmmNN`.
2. Save the token in `.env` as `TELEGRAM_BOT_TOKEN`.
3. Send `/start` to your new bot from your personal Telegram account.
4. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
   Find `"chat":{"id":12345678,...}` — that number is `TELEGRAM_CHAT_ID`.
5. Save it in `.env` as `TELEGRAM_CHAT_ID`.
6. Enable in `config/config.yaml`:

```yaml
notifications:
  telegram:
    enabled: true
```

Test it:

```bash
.venv/bin/python -c "
from core.config import init
from notifications import build_notifier
from notifications.telegram_notifier import fmt_summary
s, c = init()
n = build_notifier(s, c)
print('notifier:', n.name)
ok = n.send(fmt_summary('paper', 0, 0, 0, 0, 0.0))
print('sent:', ok)
"
```

You should see a Telegram message arrive within seconds.

## Step 5 — First real paper run

```bash
cd ~/Documents/Personal/swing_platform
.venv/bin/python -m core.cli run-live
```

This time orders **really go to Alpaca paper** (not real money). Verify on
the Alpaca dashboard: positions tab should show whatever opened.

### After-hours order behavior

Orders submitted after 4pm ET sit in the queue and fill at the next day's
open. This is correct behavior — the strategies were validated as
"end-of-day signal, fill at next-day open" so the simulated and live
behavior match.

## Step 6 — Daily automation (cron)

Edit your crontab:

```bash
crontab -e
```

Add these two lines (adjust your timezone — defaults to system):

```cron
# Run-live: daily at 4:05pm ET on weekdays (after close)
5 16 * * 1-5 cd /Users/vinitesh.gulati/Documents/Personal/swing_platform && .venv/bin/python -m core.cli run-live >> logs/cron.log 2>&1

# Sync: daily at 7pm ET on weekdays (reconcile broker state with DB)
0 19 * * 1-5 cd /Users/vinitesh.gulati/Documents/Personal/swing_platform && .venv/bin/python -m core.cli sync >> logs/cron.log 2>&1
```

Note: macOS cron uses **system local time**. Run `date` to confirm it's set
to ET (or adjust the hours accordingly if you're not in ET).

## Step 7 — Monitoring

### Daily routine (~5 min)

1. **Morning**: glance at Telegram summary from yesterday's run.
2. **After 4pm ET**: wait for run-live's signal alerts.
3. **After 7pm ET**: check `swingbot report` for per-strategy P&L:

```bash
cd ~/Documents/Personal/swing_platform
.venv/bin/python -m core.cli report
```

Output:

```
Strategy Bake-Off Report
┌──────────────┬─────────┬────────┬──────┬────────┬───────────────┬───────┐
│ Strategy     │ Signals │ Orders │ Open │ Closed │ Realized P&L  │ Win % │
│ pullback_ema │      45 │     32 │    3 │     29 │     $+842.50  │ 41.4% │
│ rsi2         │      89 │     76 │    5 │     71 │   $+1,234.80  │ 71.8% │
│ TOTAL        │     134 │    108 │   -- │     -- │   $+2,077.30  │   --  │
└──────────────┴─────────┴────────┴──────┴────────┴───────────────┴───────┘
```

### Weekly routine (~15 min)

1. Compare `swingbot report` against same period SPY return.
2. Check the `logs/cron.log` for any errors.
3. Sanity-check: do open position counts in our DB match Alpaca dashboard?
   If not, run `swingbot sync` and investigate.

### Kill switch

If something goes wrong (strategy losing badly, broker outage, your code
broke), disable everything:

```bash
# Stop cron jobs
crontab -e   # comment out the swing_platform lines

# Close all open Alpaca paper positions immediately
.venv/bin/python -c "
from core.live_runner import LiveRunner
from core.config import init
s, c = init()
runner = LiveRunner(settings=s, config=c, dry_run=False)
for p in runner.broker.get_positions():
    print(f'closing {p.symbol}')
    runner.broker.close_position(p.symbol)
"
```

## Step 8 — When to graduate to live capital

**Don't be in a hurry.** Suggested gates before risking real money:

1. **At least 60 trading days** of paper performance.
2. **Combined Sharpe ≥ 0.8** across both strategies (we wouldn't expect to
   match the backtest 1.0+).
3. **Max drawdown ≤ -15%** during the paper period.
4. **No major bugs** — every signal in the DB matches an Alpaca order.
5. **Telegram alerts arrived reliably** — you trust the monitoring.

If you pass all five: switch to a separate Alpaca **live** account, redeploy
with `TRADING_MODE=live` and `ALPACA_BASE_URL=https://api.alpaca.markets`,
and start with 10-20% of the capital you eventually plan to deploy.

## Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ValueError: You must supply a method of authentication` | `.env` missing or empty | Verify `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set |
| `is_market_open() failed` | Network or Alpaca outage | Re-run later; check https://status.alpaca.markets |
| `0 signals found` consistently | Market in bear regime, or universe too narrow | Normal — strategies don't trade every day |
| Orders rejected with "exceeds buying power" | Day-trading buying power exhausted | Reduce `capital_allocation_usd` per strategy or `allocation_pct` |
| DB `positions` rows don't match Alpaca | Drift from manual broker actions | Run `swingbot sync` |

## Useful CLI commands

```bash
# List all known strategies
.venv/bin/python -m core.cli list-strategies

# Scan only (no orders, no DB writes)
.venv/bin/python -m core.cli scan

# Dry-run (full pipeline, no broker submission)
.venv/bin/python -m core.cli run-live --dry-run

# Live paper trading run
.venv/bin/python -m core.cli run-live

# Single-strategy run
.venv/bin/python -m core.cli run-live --strategy rsi2

# Reconcile DB ↔ broker
.venv/bin/python -m core.cli sync

# Per-strategy report
.venv/bin/python -m core.cli report
.venv/bin/python -m core.cli report --json   # for scripts

# Backtest (any window)
.venv/bin/python -m core.cli backtest --strategy rsi2 --start 2024-01-01 --end 2024-12-31
```
