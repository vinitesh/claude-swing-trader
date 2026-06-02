---
name: swing-analyze
description: Deep analysis of swing_platform paper-trading performance — per-strategy trends, win/loss attribution, regime read, and what (if anything) to change. Use when the user asks "analyze my trading / why is X losing / what should I tune / deep dive / how's the edge holding up". READ-ONLY — never triggers run-live, sync, or order placement; never auto-tunes params.
---

# swing-analyze — deep performance analysis

Goes beyond the headline P&L table: looks at per-strategy trade distributions,
trends over time, which symbols/setups are working, current market regime, and
whether the live results are tracking the backtest expectations. Ends with a
disciplined recommendation (usually "keep gathering data").

## CRITICAL SAFETY RULES

1. **Observation-only.** ONLY read-only commands: `report`, DB SELECT queries,
   log reads, read-only market data via MCP. NEVER `run-live`, `sync`,
   `warm-earnings`, or any order placement.
2. **Never auto-tune.** This skill ANALYZES and RECOMMENDS. It must not edit
   YAML params, flip enabled flags, or change order types. If analysis suggests
   a change, present it as a recommendation for the user to approve — do not
   implement inside this skill.
3. **Respect "let it run as designed."** The user has been clear: changes during
   paper trading happen only when data screams loud enough, not on intuition.
   Default recommendation should usually be "keep running, sample too small."

## Steps

1. **Headline report (read-only):**
   ```bash
   ssh ec2-swing 'cd ~/swing_platform && docker compose --profile jobs run --rm report --json 2>&1 | tail -80'
   ```

2. **Per-trade detail from the DB** — query closed positions for distribution
   analysis (read-only SELECTs only):
   ```bash
   ssh ec2-swing 'cd ~/swing_platform && docker compose --profile jobs run --rm shell -c "python -c \"
   from persistence.db import session_scope
   from persistence.models import Position
   from sqlalchemy import select
   with session_scope() as s:
       rows = s.execute(select(Position).where(Position.is_open==False)).scalars().all()
       for p in rows:
           print(p.strategy_name, p.symbol, p.opened_at, p.closed_at, p.realized_pnl, p.exit_reason)
   \""'
   ```
   NOTE: `shell -c \"python -c ...\"` is acceptable ONLY for read-only SELECT
   queries. Never use it to call runner/sync code.

3. **Run history** — how many runs, any gaps, error patterns:
   ```bash
   ssh ec2-swing 'cd ~/swing_platform && docker compose --profile jobs run --rm shell -c "python -c \"
   from persistence.db import session_scope
   from persistence.models import Run
   from sqlalchemy import select, desc
   with session_scope() as s:
       for r in s.execute(select(Run).order_by(desc(Run.started_at)).limit(15)).scalars().all():
           print(r.started_at, r.mode, r.signals_found, r.orders_submitted, r.error or 'ok')
   \""'
   ```

4. **Market regime read** — use MCP market-data tools (ToolSearch for
   `mcp__yahoo-finance__*` / `mcp__alpaca__*`) to check:
   - SPY vs its 200-day SMA (bull/bear regime — affects all 3 strategies)
   - VIX level (calm <20, stressed >25)
   This frames whether the strategies are operating in their favorable regime.

## Analysis dimensions

Work through these, but only report the ones with enough data to be meaningful:

- **Per-strategy expectancy**: avg win × win rate − avg loss × loss rate. Is it
  positive? Compare to the backtest expectancy (pullback_ema ~+0.2R,
  rsi2 thin-but-positive, donchian needs big winners).
- **Exit reason mix**: are stops dominating? take-profits? time-stops? A
  strategy hitting mostly stops in a regime it should like = warning.
- **Trend over time**: is recent-week performance diverging from the first week?
  (Regime shift signal.)
- **Live vs backtest gap**: the backtests showed test Sharpe — pullback_ema 0.93,
  rsi2 1.42, donchian -0.21. Is live tracking or diverging? Expect live to be
  WORSE than backtest (slippage, gaps, no survivorship help).
- **Slippage** (if intended_entry vs actual_fill is tracked — it may not be yet):
  flag if RSI(2) fills are consistently above intended entry (gap drag).

## Output format

```
swing_platform — deep analysis (<N> trading days, <M> closed trades)

REGIME
  SPY vs 200-SMA: <above/below>  ·  VIX: <level> (<calm/stressed>)
  → strategies are in a <favorable/unfavorable> regime for <which>

PER-STRATEGY
  pullback_ema:  <closed n> trades, expectancy <±R>, exit mix <...>, read
  rsi2:          ...
  donchian:      ...

LIVE vs BACKTEST
  <which strategies are tracking expectation, which are diverging>

VERDICT
  <Honest call. Usually: "Sample too small (n=X), keep running."
   Only recommend a change if the data is unambiguous, and present it as a
   recommendation for the user to approve — never implement it here.>
```

## Anti-patterns to avoid (call these out if the user is tempted)

- Changing order types / params after a 1-2 week losing streak (noise, not signal)
- Killing a strategy before ~30 closed trades
- Adding a new strategy because existing ones aren't winning fast enough
- Reading meaning into <20-trade samples

The disciplined default is patience. Say so when the data warrants it.
