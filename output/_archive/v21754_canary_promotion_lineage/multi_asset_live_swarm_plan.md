# FDC V21.7.54 Multi-Asset Live Bot Swarm Engagement Plan

**Classification:** PLANNING DOCUMENT — post-lineage-audit rebuild
**Date:** June 16, 2026
**Status:** All live authorization SUSPENDED. This plan defines the path from current state (zero verified trades) to multi-asset live bot swarm.

---

## Current State (Verified by V21.7.54 Audit)

| Metric | Value |
|--------|-------|
| True live orders submitted | 0 |
| True gate passes | 0 |
| V21.7.41 "289 paper trades" | MARKET OBSERVATIONS (not trades) |
| V21.7.42 "live-equivalent" | INVALIDATED (forensic replay reclassified) |
| Active quote source | NORMALIZED_BOOK (not live-eligible) |
| BTC 15m 3-8¢ bucket touches at valid TTE | 0 (structurally impossible) |
| Weather bot paper trades | 5 resolved, 0W/5L, PnL -$7.60 |
| 5m shadow scalp candidates | 241 (BTC/ETH/SOL/XRP), 0 reached 2¢ target |
| Wallet balance | ~$55.29 pUSD |

---

## Phase 0: Foundation Repair (Blocks Everything)

### 0.1 Wire Live-Eligible Quote Source into Active Canary

**Problem:** V21.7.43 patch defines PM_CLOB_READ/PM_WS_BOOK as live-eligible, but V21.7.23 canary watcher still receives NORMALIZED_BOOK and rejects it.

**Action:** Modify `v21723_btc15m_canary_watcher.py` to:
- Use `underlying_quote_source` field (PM_CLOB_READ) as the gate check, NOT `normalized_price_source` (NORMALIZED_BOOK)
- The V21.7.51 observer already records `underlying_quote_source=PM_CLOB_READ` — this data IS available
- The gate logic must check `underlying_quote_source IN [PM_CLOB_READ, PM_WS_BOOK, PM_WS_BEST_BID_ASK]` instead of rejecting on `normalized_price_source=NORMALIZED_BOOK`

**Verify:** Run canary watcher for 1 hour. Confirm `quote_source_gate` passes when PM_CLOB_READ is the underlying source.

### 0.2 Build True Forward Paper Trade Lifecycle

**Problem:** No paper trade in the system has a valid order lifecycle (position_id, entry_timestamp, selected_token_id, entry_price, entry_quote_source, paper_order_created, paper_order_accepted, status).

**Action:** Create `src/v217_live/v21755_forward_paper_lifecycle.py`:
- On each scan where ALL gates pass (price bucket + TTE + quote source + spread), create a paper position record with:
  - `position_id`: `PP-{asset}-{interval}-{timestamp}`
  - `entry_timestamp`: ISO timestamp
  - `selected_side`: DOWN or UP
  - `selected_token_id`: from CLOB book query
  - `entry_price`: best ask from PM_CLOB_READ
  - `entry_quote_source`: PM_CLOB_READ
  - `entry_condition_id`: from market discovery
  - `size_usd`: $5.00
  - `contracts`: size_usd / entry_price
  - `paper_order_created`: true
  - `paper_order_accepted`: true (simulated FAK fill at best ask)
  - `status`: OPENED → FILLED → RESOLVED → SETTLED
- On market expiry, settle via Gamma Events API outcomePrices
- Write to `output/v21755_forward_paper/paper_positions.jsonl` and `paper_settlements.jsonl`

**Verify:** After 24-48 hours, count positions with full lifecycle fields. Must have ≥25 resolved positions per cell.

### 0.3 Re-evaluate Bucket/TTE Feasibility

**Problem:** BTC 15m 3-8¢ and 8-12¢ buckets do not appear at TTE 180-900s. V21.7.50 confirms NO_TOUCHES_OBSERVED. V21.7.51 has 2,430 bucket touches but only 11 in TIER_3_5 and 61 in TIER_5_8 — all on 5m markets, and 0 reached even 2¢ profit target.

**Data from V21.7.51 observer:**

| Bucket | Touch Count (5m) | Assets |
|--------|-----------------|--------|
| EXTENDED_20_25 | 1,072 | BTC/ETH/SOL/XRP |
| EXTENDED_25_30 | 271 | BTC/ETH/SOL/XRP |
| SECONDARY_15_20 | 548 | BTC/ETH/SOL/XRP |
| NEAR_BUCKET_12_15 | 226 | BTC/ETH/SOL/XRP |
| NEAR_8_12 | 241 | BTC/ETH/SOL/XRP |
| TIER_5_8 | 61 | BTC/ETH/SOL/XRP |
| TIER_3_5 | 11 | BTC/ETH/SOL/XRP |

**Action:** The market spends most time at 20-30¢ (EXTENDED buckets). The 3-8¢ tail strategy is structurally impossible for current conditions. Options:

1. **Shift to 5m markets** — more rollovers, faster price movement, more bucket touches at lower prices
2. **Shift target bucket to 12-20¢** — where the market actually spends time (774 touches at 12-20¢)
3. **Shadow-trade 20-25¢** — highest touch count (1,072), but lowest edge per trade
4. **Wait for volatility expansion** — 3-8¢ only appears in extreme moves

**Recommendation:** Shadow-trade 5m markets at 12-20¢ bucket first. This is where the market spends real time AND the 5m observer already has data.

---

## Phase 1: Single-Asset Single-Interval Shadow Validation

### 1.1 BTC 5m DOWN 12-20¢ Shadow Paper

**Why BTC first:** Most data, most liquid, most bucket touches (61 in TIER_5_8, 548 in SECONDARY_15_20).

**Config:**
- Asset: BTC
- Interval: 5m
- Side: DOWN
- Bucket: 12-20¢ (SECONDARY_15_20 + NEAR_BUCKET_12_15)
- Mode: SHADOW PAPER (no live orders)
- Size: $5 simulated
- Order type: FAK simulated fill at best ask
- Quote source: PM_CLOB_READ (already available in V21.7.51 observer)

**Gates:**
- `ask >= 0.12 AND ask <= 0.20`
- `TTE >= 60s AND TTE <= 240s` (5m window = 300s total, adjust TTE gate for 5m)
- `spread <= 0.02`
- `underlying_quote_source IN [PM_CLOB_READ, PM_WS_BOOK]`
- `quote_age_ms <= 3000`
- `condition_id valid (0x prefix)`
- `token_id maps to DOWN side`

**Exit:** Hold to expiry, settle via Gamma Events API.

**Success criteria (25+ trades):**
- WR ≥ 50% (or EV > 0 after friction)
- PF ≥ 1.25
- Max DD ≤ 15%
- Settlement errors = 0
- All positions have full lifecycle fields

### 1.2 Parallel: ETH/SOL/XRP 5m Shadow Paper

Run identical shadow paper for ETH, SOL, XRP simultaneously. V21.7.51 observer already tracks all four assets on 5m.

**Rationale:** V21.7.51 shows XRP has the most bucket touches (687), SOL second (620), ETH third (602), BTC fourth (521). Multi-asset diversification increases opportunity count.

---

## Phase 2: Multi-Asset Shadow Swarm (4 Assets × 5m)

### 2.1 Architecture

```
V21.7.56 Multi-Asset Shadow Swarm
├── BTC_5M_SHADOW_CELL
│   ├── DOWN 12-20¢ paper
│   └── UP 12-20¢ paper (shadow only, DOWN takes priority)
├── ETH_5M_SHADOW_CELL
│   ├── DOWN 12-20¢ paper
│   └── UP 12-20¢ paper
├── SOL_5M_SHADOW_CELL
│   ├── DOWN 12-20¢ paper
│   └── UP 12-20¢ paper
├── XRP_5M_SHADOW_CELL
│   ├── DOWN 12-20¢ paper
│   └── UP 12-20¢ paper
├── Shared Services
│   ├── Market discovery (V21.7.51 observer, already running)
│   ├── Quote source: PM_CLOB_READ per asset
│   ├── Settlement: Gamma Events API
│   └── Risk manager: max 1 open position per asset, max 4 total
```

### 2.2 Risk Limits

- Max 1 open position per asset
- Max 4 concurrent open positions (1 per asset)
- $5 per position ($20 max total exposure)
- Max 1 trade per asset per 5m window
- Max 5 trades per asset per day
- Max daily loss: $10 total across all cells
- Max weekly loss: $20 total
- Post-fill freeze: no new entry on same asset until previous position settles

### 2.3 Data Flow

```
V21.7.51 1s Observer (already running, PID 31275)
    ↓ bucket_touches_1s.jsonl (all assets, all intervals)
V21.7.56 Shadow Swarm Scanner
    ↓ reads bucket_touches, filters by cell config
    ↓ checks gates (price, TTE, quote source, spread)
    ↓ creates paper position if all gates pass
V21.7.56 Paper Position Manager
    ↓ tracks open positions
    ↓ checks market expiry
    ↓ settles via Gamma Events API
V21.7.56 Settlement & Journal
    ↓ writes paper_positions.jsonl
    ↓ writes paper_settlements.jsonl
    ↓ writes promotion_metrics.json
```

### 2.4 Success Criteria (50+ trades per asset)

- WR ≥ 45% per asset (5m markets are harder to predict)
- PF ≥ 1.25 per asset
- Net EV > 0 per asset
- Max DD ≤ 15% per asset
- Settlement errors = 0
- Journal completeness = 100%
- All positions have valid lifecycle (position_id, entry_quote_source, paper_order_created, etc.)

---

## Phase 3: Live Micro-Canary (Single Asset, $5)

### 3.1 Promotion Gate

After Phase 2 produces 50+ shadow trades per asset with positive EV:
- Select the BEST performing asset/cell
- Promote to live micro-canary with $5 position size
- FAK/FOK orders only
- Max 1 trade per day
- Post-fill freeze
- First-loss rule: pause after first live loss, manual review required

### 3.2 Live Order Path

```
V21.7.56 Live Micro-Canary
    ↓ signal detected (all gates pass)
    ↓ pre-submit checks (wallet, collateral, CLOB client, risk limits)
    ↓ build signed order (sig_type=3, POLY_1271)
    ↓ submit to CLOB (FAK)
    ↓ confirm fill
    ↓ journal position
    ↓ monitor to settlement
    ↓ post-trade review
```

### 3.3 First Live Trade Checklist

- [ ] PM_CLOB_READ verified as quote source
- [ ] Condition_id verified at entry time (not retroactive)
- [ ] Token_id maps to selected side
- [ ] TTE within gate
- [ ] Ask within bucket
- [ ] Spread ≤ 0.02
- [ ] Wallet balance ≥ $5
- [ ] CLOB client functional
- [ ] sig_type=3 working
- [ ] FAK order submitted
- [ ] Fill confirmed
- [ ] Position journaled
- [ ] Settlement verified
- [ ] Post-trade review completed

---

## Phase 4: Multi-Asset Live Swarm

### 4.1 Gradual Scaling

| Stage | Assets | Interval | Size | Max Concurrent | Max Daily Trades | Requirement |
|-------|--------|----------|------|----------------|-----------------|-------------|
| 4a | 1 | 5m | $5 | 1 | 1 | Phase 3 first live trade settled |
| 4b | 1 | 5m | $5 | 1 | 3 | 3+ live trades, WR ≥ 50% |
| 4c | 2 | 5m | $5 | 2 | 4 | 10+ live trades, net PnL > 0 |
| 4d | 3 | 5m | $5 | 3 | 6 | 20+ live trades, PF ≥ 1.25 |
| 4e | 4 | 5m | $5 | 4 | 8 | 30+ live trades, net PnL > 0 |
| 4f | 4 | 5m | $10 | 4 | 8 | 50+ live trades, PF ≥ 1.5, WR ≥ 55% |

### 4.2 Expansion Gates (Per Asset)

Before adding an asset to the live swarm:
- 50+ shadow paper trades for that asset
- WR ≥ 45%
- PF ≥ 1.25
- Net EV > 0
- Settlement errors = 0
- All positions have valid lifecycle
- Independent review of sample trades

### 4.3 Swarm Coordinator

```
V21.7.58 Live Swarm Coordinator
├── Asset cells (BTC/ETH/SOL/XRP)
│   ├── Each cell: independent gate check, order submission, settlement
│   └── Post-fill freeze per asset
├── Risk Manager
│   ├── Global max concurrent: 4
│   ├── Global max daily loss: $10
│   ├── Per-asset max daily trades: 2
│   └── Circuit breaker: halt all on 3 consecutive losses
├── Settlement Service
│   └── Gamma Events API polling per asset
├── Journal & Audit
│   ├── All trades logged with full lifecycle
│   └── Daily reconciliation against CLOB balance
```

---

## Phase 5: Weather Bot Reintegration (Parallel Track)

### 5.1 Current State
- 5 resolved paper trades, 0W/5L, PnL -$7.60
- 0% WR, PF 0.0, EV -$1.52/trade
- LIVE BLOCKED, needs 25+ resolved with positive EV
- V21.7.52 sigma calibration (commit 7dc6645) fixed the 0.3°C fixed sigma issue
- V21.7.52 daily calibration cron fires tomorrow 7am for first time

### 5.2 Path
- Wait for V21.7.52 daily calibration to produce calibrated sigma
- Run paper validation until 25+ trades resolved
- If WR ≥ 40% and EV > 0 after calibration: promote to live micro-canary
- If still negative: quarantine permanently, reallocate capital to crypto swarm

---

## Timeline Estimate

| Phase | Duration | Dependencies |
|-------|----------|-------------|
| 0: Foundation repair | 2-3 days | Quote source fix, paper lifecycle builder |
| 1: Single-asset shadow | 3-5 days | 25+ trades at 36 trades/day frequency |
| 2: Multi-asset shadow | 5-7 days | 50+ trades per asset |
| 3: Live micro-canary | 3-5 days | First live trade + 3-5 follow-on trades |
| 4: Multi-asset live swarm | 2-4 weeks | Gradual scaling through stages 4a-4f |
| 5: Weather (parallel) | 1-2 weeks | Depends on calibration results |

**Total estimate: 6-10 weeks to full multi-asset live swarm**

---

## Hard Rules (Non-Negotiable)

1. **No live orders until Phase 0 complete** — quote source must be PM_CLOB_READ
2. **No promotion without valid lifecycle** — every trade must have position_id, entry_quote_source, paper_order_created
3. **No retroactive reclassification** — condition_id must be verified at entry time
4. **No skipping phases** — each phase has explicit success criteria
5. **No size increase without 50+ live trades** — $5 until proven
6. **No new assets without 50+ shadow trades** — per asset
7. **Circuit breaker: 3 consecutive losses halts all live trading** — manual review required
8. **Daily reconciliation** — CLOB balance must match journal
9. **No mixing backtest with forward paper** — separate files, separate metrics
10. **V21.7.54 audit outputs are the source of truth** — not V21.7.41/V21.7.42 claims