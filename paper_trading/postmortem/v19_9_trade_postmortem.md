# V19.9 Negative-EV Postmortem Report

Generated: 2026-06-01T13:54:10.182096

## Summary
- Total trades: 43
- Wins: 18
- Losses: 25
- Win Rate: 41.9%
- Total PnL: $76.95
- Selected side avg entry: 0.297
- Opposite side hypothetical PnL: $-12.74
- Opposite side WR: 58.1%
- Opposite avg entry: 0.703

## Classification: C_SHADOW_EXECUTION_PROVEN_NEGATIVE_EV
- Execution and settlement work correctly
- Realized strategy performance is negative
- Live remains DISABLED

## §4: Bucket Performance

### By Profile
- **CORE_UP_RSI_ONLY_SHADOW**: trades=36 W=13 L=23 WR=36.1% PnL=$48.29 avg_entry=0.294 be_WR=0.294
- **ONE_MIN_STRUCTURE_EDGE**: trades=5 W=4 L=1 WR=80.0% PnL=$29.95 avg_entry=0.240 be_WR=0.240

### By Entry Price Bucket
- **0.20-0.30**: trades=30 W=16 L=14 WR=53.3% PnL=$90.81 avg_entry=0.248 be_WR=0.248
- **0.30-0.40**: trades=8 W=0 L=8 WR=0.0% PnL=$-12.83 avg_entry=0.370 be_WR=0.370

### By RSI Bucket
- **unknown**: trades=22 W=10 L=12 WR=45.5% PnL=$52.51 avg_entry=0.260
- **20-30**: trades=8 W=3 L=5 WR=37.5% PnL=$4.34 avg_entry=0.331
- **30-40**: trades=13 W=5 L=8 WR=38.5% PnL=$20.10 avg_entry=0.338

## §5: Calibration
- **CORE_UP_RSI_ONLY_SHADOW**: est_p=0.547 realized_WR=0.361 gap=0.186 Brier=0.2490 status=
- **ONE_MIN_STRUCTURE_EDGE**: est_p=0.590 realized_WR=0.800 gap=-0.210 Brier=0.2041 status=
- **CORE_UP_ONE_CONFIRM_SHADOW**: est_p=0.500 realized_WR=0.500 gap=0.000 Brier=0.0676 status=CALIBRATION_FAILED

## §6: Inverse Side Audit
- Selected side PnL: $76.95 (WR=41.9%)
- Opposite side PnL: $-12.74 (WR=58.1%)
- Opposite side is NEGATIVE EV

## §10: Recommendations
- **CORE_UP_RSI_ONLY_SHADOW**: FREEZE_PROFILE — WR=0.361_below_0.50
- **ONE_MIN_STRUCTURE_EDGE**: DIAGNOSTIC_ONLY — insufficient_sample=5_need_20
- **CORE_UP_ONE_CONFIRM_SHADOW**: FREEZE_PROFILE — expected_EV=0.5000 realized_EV=-0.6486 gap=1.1486
- **CORE_UP_STRICT**: DIAGNOSTIC_ONLY — insufficient_or_no_resolved_trades
- **PREOPEN_DIRECTION_EDGE**: DIAGNOSTIC_ONLY — insufficient_or_no_resolved_trades
- **CHEAP_CONVEX_EDGE**: DIAGNOSTIC_ONLY — insufficient_or_no_resolved_trades
- **BALANCED_DIRECTION_EDGE**: DIAGNOSTIC_ONLY — insufficient_or_no_resolved_trades
- **CORE_UP_RECOVERABILITY_FIRST_SHADOW**: DIAGNOSTIC_ONLY — insufficient_or_no_resolved_trades
- **OVERALL**: FREEZE_ALL_LOSING_PROFILES — combined_WR=18%_combined_PnL=-$42.37_anti_signal_candidate=True

## Promotion Gate Status
- ❌ resolved_trades >= 30: 43 (need 30)
- ❌ realized_EV_per_share > 0: $1.7895/trade
- ❌ realized_EV_per_dollar > 0: N/A
- ❌ PF >= 1.15: N/A (negative PnL)
- ✅ settlement_errors = 0
- ✅ journal completeness = 100%
- ❌ LIVE REMAINS DISABLED