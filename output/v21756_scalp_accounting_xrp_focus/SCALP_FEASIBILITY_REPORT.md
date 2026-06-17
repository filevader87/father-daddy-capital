# Scalp Feasibility Report — V21.7.56

## Summary

| Metric | Scalp | Hold-to-Expiry |
|--------|-------|----------------|
| Total PnL | $14.86 | $-126.24 |
| Positions | 22 | 44 |
| Win Rate | 100% (all scalp exits) | ~50% |
| Edge | BID REPRICING | BINARY RESOLUTION |

## Key Findings

1. **Scalp edge is real** — $14.86 profit from 22 exits, 100% win rate
2. **Hold-to-expiry is negative** — $-126.24 loss from 44 positions
3. **Best cell: XRP_5m_DOWN** — 6/6 scalp exits, +$4.15, 100% WR
4. **Worst cells: SOL, ETH UP** — hold PF < 0.2, should be retired from hold strategy
5. **Edge requires immediate exit** — 3¢ bid profit threshold, must exit quickly
6. **Spread survival** — Spread <= 0.03 gate enforced on entry

## Scalp Profitable Cells

- **XRP_5m_DOWN_30_60**: 6 exits, WR=100.0%, PnL=$4.15, PF=inf
- **ETH_5m_DOWN_30_60**: 4 exits, WR=100.0%, PnL=$2.78, PF=inf
- **ETH_5m_UP_30_60**: 3 exits, WR=100.0%, PnL=$2.63, PF=inf
- **SOL_5m_UP_30_60**: 3 exits, WR=100.0%, PnL=$1.72, PF=inf
- **SOL_5m_DOWN_30_60**: 2 exits, WR=100.0%, PnL=$1.49, PF=inf
- **BTC_5m_DOWN_30_60**: 2 exits, WR=100.0%, PnL=$0.87, PF=inf
- **BTC_5m_UP_30_60**: 1 exits, WR=100.0%, PnL=$0.78, PF=inf
- **XRP_5m_UP_30_60**: 1 exits, WR=100.0%, PnL=$0.44, PF=inf

## Hold-Negative Cells

- **ETH_5m_UP_30_60**: 6 holds, WR=0.0%, PnL=$-30.0, PF=0.0
- **SOL_5m_DOWN_30_60**: 6 holds, WR=0.0%, PnL=$-30.0, PF=0.0
- **SOL_5m_UP_30_60**: 4 holds, WR=0.0%, PnL=$-20.0, PF=0.0
- **BTC_5m_UP_30_60**: 8 holds, WR=25.0%, PnL=$-19.59, PF=0.35
- **BTC_5m_DOWN_30_60**: 6 holds, WR=33.33%, PnL=$-11.13, PF=0.44
- **ETH_5m_DOWN_30_60**: 6 holds, WR=33.33%, PnL=$-10.0, PF=0.5
- **XRP_5m_UP_30_60**: 8 holds, WR=50.0%, PnL=$-5.52, PF=0.72

## Verdict

**SCALP_EDGE_DETECTED_BUT_HOLD_BLEEDS**

Continue scalp-focused forward paper. XRP_5m_DOWN is the best cell.
Retire SOL and ETH UP from hold strategy. Need 25+ resolved scalp exits per cell before promotion review.
