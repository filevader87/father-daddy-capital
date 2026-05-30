# V19.7f Production Profile

**Until audit + live-shadow validation is complete, the following profile is IN EFFECT.**

## Signal Zones

| Zone | Direction | Status | Rationale |
|------|-----------|--------|-----------|
| RSI < 20 | UP | **BLOCKED** | Knife-catch zone, no data |
| RSI 20-28 | UP | **CANDIDATE** | Deep oversold, primary signal |
| RSI 28-35 | UP | **CANDIDATE** | Near-oversold, requires confirmations |
| RSI 35-55 | — | **DEAD** | No trades (33% WR on PMXT) |
| RSI 55-70 | DOWN | **SHADOW** | 51% MC WR, confidence capped below MIN_CONFIDENCE |
| RSI 70-82 | DOWN | **SHADOW** | Requires 2+ contra confirmations, still marginal |
| RSI > 82 | — | **BLOCKED** | Parabolic, no data |

## Production Rules

1. **Oversold UP = candidate** — RSI 20-35 UP only
2. **DOWN = shadow or micro only** — logged but not traded at full size
3. **RSI 55-70 DOWN = disabled/shadow** — zero trades in production MC
4. **RSI 70-82 DOWN = shadow** unless stronger evidence appears
5. **Multi-asset = discovery/scoring enabled, NOT full-size trading** — BTC 5m is primary, ETH/SOL/XRP 15m are discovery-only until validated

## Deploy Gate (V19.7f)

Deployment requires ALL of:
- ✅ Net EV/trade > 0 (after slippage penalty)
- ✅ Avg Profit Factor >= 1.25
- ✅ Avg Bankroll DD <= 15% (hard-mode MC)
- ✅ Avg trades/seed >= 5 (minimum opportunity)

Qualified WR is **diagnostic only**, not a deployment criterion.

## Market Classification

- **Accepted**: BTC/ETH/SOL/XRP 5m/15m Up/Down binaries only
- **Rejected**: daily, weekly, monthly, strikes, ranges, ladders, mismatched assets
- **Ambiguous = NO TRADE** (classify_market rejects anything unclear)

## Discovery Architecture

- Paginated **active-market scan**: `/markets?active=true&closed=false&limit=500&offset=N`
- NOT event-first (`/events`)
- Deduplicated by `conditionId`
- Full-object validation via `classify_market()` (not just question strings)
- Shadow discovery cron runs every 5 minutes — no orders

## Drawdown Accounting

- **Primary DD metric**: Bankroll DD (aggregate across all zones) = 8.9% in MC
- **Zone DD** (RSI 20-28: 43%, RSI 28-35: 49%) is per-sequence PnL drawdown — NOT bankroll risk
- Zone DD indicates signal quality, not sizing. Use bankroll DD for position sizing.

## What Must Be Validated Before Full Trading

1. Live-shadow discovery for 2-4 hours — verify BTC/ETH/SOL/XRP markets appear
2. Per-asset classification accuracy (no false accepts on strike/daily markets)
3. Pagination completeness (verify multiple pages fetched)
4. Ablation results with Wilson CI (300+ trades per enabled zone)
5. No individual enabled zone with bankroll DD > 25%
6. Brier score and calibration error acceptable
7. DOWN zones remain shadow until live data validates positive EV