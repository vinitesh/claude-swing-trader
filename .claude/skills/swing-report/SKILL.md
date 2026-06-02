---
name: swing-report
description: Per-strategy paper-trading P&L report for swing_platform, with interpretation vs the SPY benchmark. Use when the user asks "how are my strategies doing / show the bake-off / P&L / which strategy is winning / report". READ-ONLY — never triggers run-live, sync, or order placement.
---

# swing-report — strategy bake-off P&L report

Pulls the per-strategy performance table from the live EC2 deployment and
interprets it: which strategy is ahead, win rates, and how the book compares
to just buying SPY.

## CRITICAL SAFETY RULE

**Observation-only.** The ONLY docker command allowed is `report`. NEVER run
`run-live`, `sync`, or `warm-earnings` from this skill. Those place orders or
mutate state. If the DB looks stale (last sync was long ago), say so — do NOT
run sync yourself to "freshen" it. Tell the user the 7pm cron sync will update
it, or they can explicitly ask for a manual sync as a separate action.

## Steps

1. **Pull the bake-off report (read-only):**
   ```bash
   ssh ec2-swing 'cd ~/swing_platform && docker compose --profile jobs run --rm report 2>&1 | tail -16'
   ```

2. **Pull machine-readable JSON for precise numbers:**
   ```bash
   ssh ec2-swing 'cd ~/swing_platform && docker compose --profile jobs run --rm report --json 2>&1 | tail -60'
   ```
   Use the JSON for exact figures; the table for the visual.

3. **Fetch the SPY benchmark for the same period** (so "good" is contextualized).
   The MCP tools `mcp__yahoo-finance__*` or `mcp__alpaca__*` may be available —
   use ToolSearch to find a price-history tool, fetch SPY's return since the
   first run date. If unavailable, note the benchmark is unavailable and skip.

## Interpreting the numbers

Be honest, not cheerleading. Key reads:

- **Realized P&L** is only meaningful once positions have CLOSED. Early on,
  everything is open and realized P&L is ~$0 — that's expected, say so.
- **Win rate** below ~40% is fine for trend/pullback strategies (they rely on
  big winners); RSI(2) should run ~65-75% once it has a sample.
- **Profit factor** (gross win / gross loss): >1.5 good, ~1.0-1.2 marginal,
  <1.0 losing.
- **Sample size matters**: under ~20 closed trades per strategy, treat all
  numbers as noise. Say "too early to conclude" explicitly.
- **vs SPY**: if the combined book isn't beating SPY on a risk-adjusted basis
  after a meaningful sample, say so plainly. Underperforming SPY is the default
  outcome for most retail strategies and the user knows this.

## Output format

```
Strategy bake-off — paper trading (data since <first run date>, <N> trading days)

Strategy      Signals  Orders  Open  Closed  Realized P&L  Win%  PF
pullback_ema  ...
rsi2          ...
donchian      ...
TOTAL         ...

vs SPY buy-and-hold same period: <SPY return> 

Read: <1-3 sentences. Which strategy leads, whether sample is big enough to
mean anything, honest call on whether the book is earning its keep.>
```

## Reminders specific to this platform

- **donchian** failed walk-forward validation and is running paper-only to
  gather real-world data. If it's leading, that's interesting but expected to
  be regime-dependent — note the caveat.
- Realized P&L in v1 sync uses entry_price as a proxy for exits in some paths;
  for exact P&L the note in the report output says to pull Alpaca order history.
  Mention this caveat if precise P&L matters to the user's question.
- Holding periods: rsi2 ~2-5 days, pullback_ema ~4-15 days, donchian weeks.
  A strategy with all-open positions and zero closed just hasn't had time yet.
