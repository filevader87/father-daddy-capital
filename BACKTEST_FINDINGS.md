# PMXT Historical Backtest Findings — V18.2 Validation

**Date:** 2026-05-25 (9 hours of CLOB orderbook data)
**Source:** archive.pmxt.dev/Polymarket/v2
**Markets:** 6,612 binary markets with trades, 41,862 total trades

## Critical Findings

### 1. Cheap-Side Base Win Rate: ~43%
Tokens that hit ≤20¢ at some point only win **42.9%** of the time.
The cheaper the token, the less likely it wins:
- ≤5¢: 39.6% WR
- ≤10¢: 41.9% WR  
- ≤15¢: 42.6% WR
- ≤20¢: 42.9% WR

### 2. Token Price RSI Has Predictive Power — But Only One Direction
When a cheap token has **oversold RSI** (<28):
- RSI < 18: **75.9% WR** (536/706)
- RSI < 28: **74.1% WR** (605/816)
- Combined: **75.0% WR** (1,141/1,522)

When a cheap token has **overbought RSI** (>72):
- RSI > 72: **8.9% WR** (5/56)
- RSI > 82: **11.8% WR** (114/970)
- Combined: **11.6% WR** (119/1,026)

**The overbought zone for cheap tokens is a LOSS leader.**

### 3. V18.2 MC vs Historical Delta
| Metric | MC (hard-mode) | Historical | Delta |
|--------|---------------|------------|-------|
| Avg WR | 84.6% | 63.8% | -20.8pp |
| Qualified WR | 90.7% | 63.4% | -27.3pp |

The -21pp gap comes from:
- V18.2 MC overbought zone trades (12% real WR ← massive losses)
- V18.2 MC assumed 85%+ base edge that doesn't exist at 43% base rate
- Token price RSI ≠ Crypto (BTC) price RSI — different signal sources

### 4. BTC Price Context Matters
On 2026-05-25, BTC fell -1.99% (downtrend):
- DOWN contracts won more (trend-follows price)
- V18.2's MC simulates both directions equally
- Real WR depends heavily on which direction BTC is trending

## Action Items for V18.3

1. **Kill overbought RSI zone for cheap-side signals** — 12% WR is fatal
2. **Only trade oversold cheap tokens** (RSI < 28) — 75% WR validated
3. **Add BTC trend context** — buy cheap UP in downtrend oversold bounces
4. **Recalibrate MC** with 43% base rate (not 85%+ assumed edge)
5. **Signal source:** compute RSI from BTC price (not token price)
