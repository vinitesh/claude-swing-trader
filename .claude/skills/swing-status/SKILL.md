---
name: swing-status
description: Quick health check of the swing_platform paper-trading system on EC2. Use when the user asks "is it running / did the cron fire / any errors / system status / health check / is everything ok". READ-ONLY — never triggers run-live, sync, or order placement.
---

# swing-status — paper-trading system health check

A fast, **read-only** health probe of the live swing_platform deployment on EC2.
Answers: is cron alive, did the latest scheduled runs fire, how many positions
are open, were there any errors.

## CRITICAL SAFETY RULE

This skill is **observation-only**. NEVER run any of these (they place/cancel
real paper orders or mutate state):
- `docker compose ... run-live`  (places orders)
- `docker compose ... sync`      (mutates DB to match broker)
- `docker compose ... warm-earnings` (network calls, fine but let cron own it)
- any `swingbot run-live` / `swingbot sync`

ONLY run the read-only commands listed below. If the user wants to actually
trigger a run, tell them to wait for cron OR ask them to explicitly confirm
they want a manual trigger — and even then, do it as a separate explicit
action, never silently inside this skill.

## Connection

The EC2 box is reachable via the SSH alias `ec2-swing` (configured in
`~/.ssh/config`). If the public IP changed (instance restart), the alias may
be stale — if ssh fails with connection refused, tell the user the IP likely
changed and they need to update `~/.ssh/config`.

## Steps

Run these read-only commands over ssh and synthesize a status summary:

1. **Time + cron daemon:**
   ```bash
   ssh ec2-swing 'date; systemctl is-active cron'
   ```

2. **Scheduled jobs registered:**
   ```bash
   ssh ec2-swing 'crontab -l | grep -v "^#" | grep -v "^$"'
   ```

3. **Did the system cron actually invoke the jobs? (proves the daemon fired):**
   ```bash
   ssh ec2-swing 'sudo grep CRON /var/log/syslog 2>/dev/null | grep -i swing | tail -10 || journalctl -u cron --since "yesterday" 2>&1 | tail -15'
   ```

4. **Recent app output / errors (last 40 lines of cron.log):**
   ```bash
   ssh ec2-swing 'tail -40 ~/swing_platform/logs/cron.log 2>&1'
   ```
   Scan for `Traceback`, `KeyError`, `Error`, `APIError`, or `Run complete`.

5. **Last run recorded in the DB + open position count (read-only `report`):**
   ```bash
   ssh ec2-swing 'cd ~/swing_platform && docker compose --profile jobs run --rm report 2>&1 | tail -16'
   ```
   NOTE: `report` is the ONLY docker compose command this skill may run. It
   reads trading.db and prints a table; it places no orders.

6. **Web UI liveness (optional):**
   ```bash
   ssh ec2-swing 'curl -sS http://127.0.0.1:8082/healthz'
   ```

## Output format

Give the user a tight status block:

```
swing_platform — system status (as of <EC2 time>)

Cron daemon:     active / inactive
Jobs registered: run-live (4:05pm), sync (7pm), warm-earnings (2am)
Last run-live:   <timestamp> — <ok / FAILED with reason>
Last sync:       <timestamp> — <ok / FAILED>
Open positions:  <N>  (pullback_ema: a, rsi2: b, donchian: c)
Errors in log:   <none / summary of any traceback>
Web UI:          ok / down
```

Then a one-line verdict: "✅ Healthy, next run at 4:05pm ET" or
"⚠️ <specific problem> — <what to check>".

## Schedule reference (America/New_York)

| Time   | Job           | Days     |
|--------|---------------|----------|
| 02:00  | warm-earnings | daily    |
| 16:05  | run-live      | Mon-Fri  |
| 19:00  | sync          | Mon-Fri  |

If the current time is before today's 16:05 and you see no run-live today,
that's EXPECTED — not a problem. Don't report it as a failure.

## Common failure signatures

- `KeyError: "Strategy 'X' not in registry"` → a strategy enabled in YAML but
  its Python module isn't in the deployed image. Fix: sync the module + rebuild.
- `APIError ... stop_price must be <= base_price` → Alpaca rejected a bracket
  whose stop wasn't at least 1 cent below market. Some orders rejected, others
  fine. Not fatal.
- `Connection refused` on ssh → EC2 public IP changed; update `~/.ssh/config`.
- Empty / stale cron.log with no recent entries → cron daemon stopped, OR the
  jobs haven't reached their scheduled time yet.
