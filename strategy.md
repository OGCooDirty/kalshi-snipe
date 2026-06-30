# Kalshi Autonomous Agent — Strategy & Guardrails

**Objective:** Grow the funded Kalshi balance via automated, research-driven bets
on *mispriced* markets. This is a bounded experiment with explicitly-designated
risk capital, not a guaranteed income stream. Expected edge is small and
uncertain; the log exists to prove whether edge is real before scaling.

## Core rule — edge or nothing
Place a bet ONLY when research indicates the true probability differs from the
market's ask by a **real margin (≥ 8 percentage points)**. No volume quota.
Zero-trade cycles are correct and expected. Never force a trade to "stay active."

## Where the edge (if any) lives
- News/event-driven markets where public information hasn't fully repriced.
- Thinner markets slow to react, that I can research faster than they update.
- NOT "near-certainties" at 95-99¢ — those are efficiently priced, EV ≈ 0.

## Universe filter
- Single markets only (skip multi-leg `KXMVE` parlays).
- Resolving within ~7 days (so outcomes settle and we learn fast).
- Has real liquidity (24h volume above threshold; a tradeable bid/ask).
- The question must be researchable from public info.

## Sizing
- Small fixed stake per bet: **$2** target, hard server cap **$5/order**.
- Whole contracts; price in cents 1-99.

## Risk guardrails (hard stops)
- **Max concurrent exposure:** $15 of the balance deployed at once.
- **Daily loss limit:** if realized + unrealized P&L for the day ≤ **-$5**, stop
  opening new positions until next day.
- **Kill switch:** if total balance falls below **$20**, halt ALL new trades and
  alert the user.
- No averaging down into a losing thesis. No churn (avoid re-trading the same
  resolved view).

## Per-cycle process
1. `get_balance` + `get_positions`; check all guardrails. If tripped, stop.
2. Scan liquid, single, soon-resolving markets (candidate list).
3. For top candidates: research via web, estimate true probability.
4. edge = (my_true_prob − ask_price). If edge ≥ 0.08 AND risk limits OK →
   place a small order on the favorable side.
5. Log decision (ticker, my prob, price, edge, action, reasoning) to
   `decisions.log`, win or skip.

## Cadence / autonomy limits
- Runs as an in-session loop (while Claude Code is active). The Kalshi key is
  bound to this session, so there is no unattended 24/7 run yet.
- True 24/7 = a standalone agent (Anthropic API key + scheduler) — Phase 2,
  build only if the in-session track record shows real edge.
