# V18.2 Verification Results

## MC Validation Summary

| Config | Seeds | Cycles | Bankroll | Avg WR | Internal WR | P&L | Sharpe | Gates 4+/5 |
|--------|-------|--------|----------|--------|-------------|-----|--------|------------|
| Baseline | 100 | 1000 | $30 | 87.9% | 87.8% | +$47.83 | 4.79 | 86/100 |
| Long-run | 50 | 2000 | $30 | 86.4% | 86.0% | +$56.24 | 3.81 | 49/50 |
| Long-run | 30 | 5000 | $30 | 91.4% | 91.5% | +$71.79 | 3.59 | 30/30 |

## 8-Seed Cross-Validation (50 seeds × 1000 cycles, $30)

| Master Seed | Trades/Seed | Avg WR | P&L |
|------------|-------------|--------|-----|
| 0 | 8.2 | 89.8% | +$51.39 |
| 1 | 8.7 | 88.1% | +$51.05 |
| 2 | 8.0 | 87.4% | +$45.01 |
| 3 | 8.5 | 88.6% | +$51.09 |
| 4 | 8.4 | 85.4% | +$44.24 |
| 5 | 7.7 | 89.5% | +$45.68 |
| 6 | 8.0 | 86.2% | +$41.99 |
| 7 | 8.0 | 87.0% | +$41.63 |

**Range: 85.4%–89.8%** | **Mean: 87.8%**

## Extended Cycles Cross-Validation (50 seeds × 2000 cycles, $30)

| Master Seed | Trades/Seed | Avg WR | P&L |
|------------|-------------|--------|-----|
| 0 | 11.4 | 86.4% | +$56.24 |
| 1 | 11.8 | 90.2% | +$69.43 |
| 2 | 11.3 | 89.7% | +$66.68 |
| 3 | 11.6 | 87.9% | +$63.62 |
| 4 | 11.5 | 89.1% | +$66.10 |
| 5 | 11.7 | 88.0% | +$65.82 |
| 6 | 11.3 | 89.7% | +$66.35 |
| 7 | 11.9 | 88.5% | +$65.62 |

**Range: 86.4%–90.2%** | **Mean: 88.7%**

## Bankroll Stress Test (50 seeds × 1000 cycles)

| Bankroll | Avg WR | Internal WR | P&L | Sharpe | Verdict |
|----------|--------|-------------|-----|--------|---------|
| $10 | 89.8% | 89.7% | +$17.15 | 5.11 | ✅ |
| $30 | 87.9% | 87.8% | +$47.83 | 4.79 | ✅ Optimal |
| $50 | 88.4% | 89.6% | +$85.04 | 4.54 | ✅ |
| $100 | 78.4%* | 87.8% | +$142.78 | 4.41 | ⚠️ |

*$100 avg WR dragged by 12% of seeds getting <5 trades (signal frequency issue, not edge degradation). Internal WR = actual trade-level performance = 87.8%.

## $100 Bankroll Deep Verification

| Config | Seeds | Cycles | Internal WR | P&L | Gates |
|--------|-------|--------|-------------|-----|-------|
| 100s × 1000c | 100 | 1000 | 87.8% | +$142.78 | 76/100 |
| 100s × 2000c | 100 | 2000 | 87.1% | +$185.94 | 86/100 |
| 50s × 5000c | 50 | 5000 | 90.4% | +$192.74 | 41/50 |

## Journal Breakdown (100 seeds × 1000 cycles, $30)

**By RSI Zone:**
- Ultra-extreme (<18/>82): 94%/88% WR
- Extreme (18-28/72-82): 88% WR
- Strong (28-38/62-72): 83% WR
- Moderate (38-50/50-62): 80% WR

**By Regime:**
- Trending down: 92% WR
- Volatile: 87% WR
- Trending up: 85% WR
- Ranging: **BLOCKED** (71% WR — blacklist)

## V18.2 Integrated Features (from @de1lymoon/Becker)

1. **Markov Transition Matrix** — 20% blend weight, deterministic inner-MC (fixed seed per state), 15pp sanity check vs RSI
2. **Longshot Bias Calibration** — ≤15¢ contracts empirically adjusted (Becker 72.1M trade study: 5¢ wins 4.18% not 5%)
3. **Maker/Taker Edge** — +1.12% per trade for limit orders (Becker: makers +1.12%, takers -1.12%)

## Version History

| Version | WR | Key Changes |
|---------|-----|-------------|
| V18 | 76.5% | Base engine, RSI 35/65, CONF 0.75 |
| V18.1 | 82.8% | RSI 30/70, CONF 0.80, ranging blacklist, multi-indicator |
| V18.2 | 87.8% | RSI 28/72, CONF 0.83, +Markov, +Longshot, +Maker edge |