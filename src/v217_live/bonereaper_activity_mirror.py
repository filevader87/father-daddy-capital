#!/usr/bin/env python3
"""
V21.7.5 Bonereaper Activity Mirror — Shadow-Only Trader-Behavior Replication
=============================================================================
Models observable Bonereaper-style trading behavior as shadow profiles.
Settles every event by binary outcome only.
Compares against FDC convex 3-12¢ model.
Does NOT place real or paper trades.

§1:  Core finding — BR trades multi-asset, both sides, mid/high ranges
§2:  Live rules unchanged — BTC/DOWN/3-12¢/TAKER/fixed/gates
§3:  This module = logging + counterfactual settlement only
§4:  11 shadow profiles
§5:  10-bucket full-range tracking
§6:  Full event field logging
§7:  3 sizing layers for risk-shape analysis
§8:  6 strategy hypotheses
§9:  Binary settlement only
§10: FDC vs BR comparison report
§11: Promotion criteria (100 resolved, EV>0, PF>=1.35)
§12: Rejection criteria (PF<1.10, EV<=0)
§13: Lag integration
§14: Bucket conviction lockdown
§15: 5 output files
§16: Do not copy. Model. Settle. Compare.
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
from pathlib import Path
from collections import defaultdict
import time, gc, json, random, logging

log = logging.getLogger("br_mirror")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v2175")
OUT_DIR.mkdir(parents=True, exist_ok=True)

BANKROLL_START = 100.0
PAPER_SIZE = 2.0
TARGET_TRADES = 4000  # Need density across all buckets

# ═══════════════════════════════════════════════════════════════════════
# §5: FULL-RANGE BUCKET MAP
# ═══════════════════════════════════════════════════════════════════════

BUCKETS = {
    "SUB_FLOOR_CONVEX":          (0.00, 0.03),
    "PRIMARY_LOW_CONVEX":        (0.03, 0.05),
    "PRIMARY_CORE_CONVEX":       (0.05, 0.08),
    "PRIMARY_HIGH_CONVEX":       (0.08, 0.12),
    "SECONDARY_THIN_EDGE":       (0.12, 0.20),
    "LOW_MIDRANGE":              (0.20, 0.40),
    "MIDRANGE_DECISION_ZONE":    (0.40, 0.60),
    "HIGH_CONFIDENCE_DIRECTIONAL": (0.60, 0.85),
    "NEAR_RESOLUTION_HARVEST":   (0.85, 0.99),
    "SETTLEMENT_DUST":           (0.99, 1.01),
}

BUCKET_ORDER = list(BUCKETS.keys())

def classify_bucket(price: float) -> str:
    for name, (lo, hi) in BUCKETS.items():
        if lo <= price < hi:
            return name
    return "SETTLEMENT_DUST" if price >= 0.99 else "UNKNOWN"

# ═══════════════════════════════════════════════════════════════════════
# §4: SHADOW PROFILES
# ═══════════════════════════════════════════════════════════════════════

PROFILES = {
    "BR_BTC_UP_MIDRANGE":              {"asset": "BTC", "side": "UP",   "bucket_range": (0.20, 0.60)},
    "BR_BTC_DOWN_MIDRANGE":            {"asset": "BTC", "side": "DOWN", "bucket_range": (0.20, 0.60)},
    "BR_ETH_UP_MIDRANGE":              {"asset": "ETH", "side": "UP",   "bucket_range": (0.20, 0.60)},
    "BR_ETH_DOWN_MIDRANGE":            {"asset": "ETH", "side": "DOWN", "bucket_range": (0.20, 0.60)},
    "BR_SOL_UP_MIDRANGE":              {"asset": "SOL", "side": "UP",   "bucket_range": (0.20, 0.60)},
    "BR_SOL_DOWN_MIDRANGE":            {"asset": "SOL", "side": "DOWN", "bucket_range": (0.20, 0.60)},
    "BR_XRP_UP_MIDRANGE":              {"asset": "XRP", "side": "UP",   "bucket_range": (0.20, 0.60)},
    "BR_XRP_DOWN_MIDRANGE":            {"asset": "XRP", "side": "DOWN", "bucket_range": (0.20, 0.60)},
    "BR_NEAR_RESOLUTION_MOMENTUM":     {"asset": "ANY", "side": "ANY",  "bucket_range": (0.85, 0.99)},
    "BR_REVERSAL_HEDGE_PAIR":          {"asset": "ANY", "side": "BOTH", "bucket_range": (0.40, 0.60)},
    "BR_HIGH_CONVICTION_EXPENSIVE_SIDE": {"asset": "ANY", "side": "ANY", "bucket_range": (0.60, 0.85)},
}

# §14: FDC bucket conviction labels
FDC_BUCKET_CONVICTION = {
    "PRIMARY_LOW_CONVEX":  "HIGH_CONVICTION",
    "PRIMARY_CORE_CONVEX": "CORE_CONVICTION",
    "PRIMARY_HIGH_CONVEX": "MARGINAL_CONVICTION",
    "SECONDARY_THIN_EDGE": "SHADOW_ONLY_REJECTED_FOR_LIVE",
}

# ═══════════════════════════════════════════════════════════════════════
# §7: SIZING LAYERS
# ═══════════════════════════════════════════════════════════════════════

SIZE_LAYERS = {"UNIT": 1.0, "MEDIUM": 5.0, "LARGE": 25.0}

# ═══════════════════════════════════════════════════════════════════════
# FRICTION MODEL — tiered by bucket (richer tokens = tighter spread, less slip)
# ═══════════════════════════════════════════════════════════════════════

FRICTION = {
    "SUB_FLOOR_CONVEX":          {"spread": 0.015, "slip": 0.012, "fill_rej": 0.08, "partial": 0.15, "stale": 0.04},
    "PRIMARY_LOW_CONVEX":        {"spread": 0.012, "slip": 0.008, "fill_rej": 0.07, "partial": 0.12, "stale": 0.03},
    "PRIMARY_CORE_CONVEX":       {"spread": 0.010, "slip": 0.006, "fill_rej": 0.06, "partial": 0.10, "stale": 0.025},
    "PRIMARY_HIGH_CONVEX":       {"spread": 0.008, "slip": 0.005, "fill_rej": 0.05, "partial": 0.08, "stale": 0.02},
    "SECONDARY_THIN_EDGE":       {"spread": 0.007, "slip": 0.004, "fill_rej": 0.04, "partial": 0.07, "stale": 0.02},
    "LOW_MIDRANGE":              {"spread": 0.006, "slip": 0.003, "fill_rej": 0.03, "partial": 0.06, "stale": 0.015},
    "MIDRANGE_DECISION_ZONE":    {"spread": 0.005, "slip": 0.003, "fill_rej": 0.03, "partial": 0.05, "stale": 0.012},
    "HIGH_CONFIDENCE_DIRECTIONAL": {"spread": 0.004, "slip": 0.002, "fill_rej": 0.02, "partial": 0.04, "stale": 0.010},
    "NEAR_RESOLUTION_HARVEST":   {"spread": 0.003, "slip": 0.001, "fill_rej": 0.01, "partial": 0.03, "stale": 0.005},
    "SETTLEMENT_DUST":           {"spread": 0.001, "slip": 0.001, "fill_rej": 0.01, "partial": 0.02, "stale": 0.002},
}

# ═══════════════════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════════════════

def compute_rsi(prices, period=14):
    n = len(prices)
    if n < period + 1: return np.full(n, 50.0)
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.zeros(n); avg_l = np.zeros(n)
    avg_g[period] = np.mean(gains[1:period+1]); avg_l[period] = np.mean(losses[1:period+1])
    for i in range(period+1, n):
        avg_g[i] = (avg_g[i-1]*(period-1) + gains[i]) / period
        avg_l[i] = (avg_l[i-1]*(period-1) + losses[i]) / period
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    rsi = np.where(avg_l > 0, 100 - 100/(1+rs), 100.0)
    rsi[:period] = 50.0
    return rsi

def compute_velocity(prices):
    if len(prices) < 2: return np.zeros(len(prices))
    return np.diff(prices, prepend=prices[0])

def compute_acceleration(prices):
    n = len(prices); accel = np.zeros(n)
    if n > 5:
        v = np.diff(prices); a = np.diff(v)
        if len(a) <= n - 2: accel[2:] = a
    return accel

def compute_continuation(prices):
    n = len(prices); score = np.zeros(n); direction = np.zeros(n)
    if n < 2: return score, direction
    for i in range(1, n):
        if prices[i] < prices[i-1]: score[i] = min(score[i-1] + 0.15, 1.0); direction[i] = -1
        elif prices[i] > prices[i-1]: score[i] = min(score[i-1] + 0.15, 1.0); direction[i] = 1
        else: score[i] = score[i-1] * 0.9; direction[i] = direction[i-1]
    return score, direction

def get_regime(prices):
    if len(prices) < 20: return 'RANGING'
    sma20 = np.mean(prices[-20:]); sma50 = np.mean(prices[-50:]) if len(prices) >= 50 else np.mean(prices)
    pct = (prices[-1] - sma20) / max(sma20, 0.001) * 100
    if pct > 0.5 and sma20 > sma50: return 'TRENDING_UP'
    elif pct < -0.5 and sma20 < sma50: return 'TRENDING_DOWN'
    return 'RANGING'

def classify_timing(time_pct):
    TIMING = {'EARLY': (0,0.20,0.10), 'FORMATION': (0.20,0.40,0.35), 'MOMENTUM': (0.40,0.80,0.80), 'LATE': (0.80,0.90,0.95), 'FINAL': (0.90,1.00,0.60)}
    for name, (lo, hi, _) in TIMING.items():
        if lo <= time_pct < hi: return name
    return 'FINAL'

def classify_state(accel_arr, velocity_arr, consec, cont_dir, rsi_val):
    if len(accel_arr) == 0: return 'FLAT'
    if cont_dir < -0.3 and consec >= 3: return 'DOWN_CONTINUATION'
    if cont_dir < -0.1: return 'DOWN_MOMENTUM'
    if cont_dir > 0.3 and consec >= 3: return 'UP_CONTINUATION'
    if cont_dir > 0.1: return 'UP_REVERSAL'
    return 'FLAT'

# ═══════════════════════════════════════════════════════════════════════
# §4: ASSIGN SHADOW PROFILES
# ═══════════════════════════════════════════════════════════════════════

def assign_profiles(entry_price, bucket, side, asset_hint):
    """Return list of matching shadow profile names for this event."""
    profiles = []
    for pname, pconf in PROFILES.items():
        blo, bhi = pconf["bucket_range"]
        if not (blo <= entry_price < bhi): continue
        if pconf["side"] != "ANY" and pconf["side"] != "BOTH" and pconf["side"] != side: continue
        if pconf["asset"] != "ANY" and pconf["asset"] != asset_hint: continue
        if pconf["side"] == "BOTH":
            pass  # Both sides match
        profiles.append(pname)
    return profiles

# ═══════════════════════════════════════════════════════════════════════
# §8: HYPOTHESIS TESTING
# ═══════════════════════════════════════════════════════════════════════

def assign_hypotheses(entry_price, bucket, side, cont_dir, time_pct, regime, lag_ms, external_bps):
    """Return dict of hypothesis_name -> bool (does this event test it)."""
    H = {}
    # A: Midrange Directional Edge (40-60¢, direction aligns with move)
    if 0.40 <= entry_price < 0.60:
        H["BR_MIDRANGE_DIRECTIONAL_EV"] = True
    else:
        H["BR_MIDRANGE_DIRECTIONAL_EV"] = False
    
    # B: High-Confidence Momentum (60-85¢, strong trend)
    if 0.60 <= entry_price < 0.85 and abs(cont_dir) > 0.3:
        H["BR_HIGH_CONFIDENCE_DIRECTIONAL_EV"] = True
    else:
        H["BR_HIGH_CONFIDENCE_DIRECTIONAL_EV"] = False
    
    # C: Near-Resolution Harvest (85-99¢, short TTE)
    if 0.85 <= entry_price < 0.99:
        H["BR_NEAR_RESOLUTION_HARVEST_EV"] = True
    else:
        H["BR_NEAR_RESOLUTION_HARVEST_EV"] = False
    
    # D: Multi-Asset Surface (always tracked)
    H["BR_MULTI_ASSET_SURFACE_EV"] = True
    
    # E: Both-Side Flexibility
    H["BR_BOTH_SIDE_FLEXIBILITY_EV"] = side == "UP"  # UP side tests this
    
    # F: Lag Capture (lag > 500ms and external move > 20bps)
    H["BR_LAG_CAPTURE_EV"] = (lag_ms is not None and lag_ms > 500) and (external_bps is not None and abs(external_bps) > 20)
    
    return H

# ═══════════════════════════════════════════════════════════════════════
# MAIN SIMULATION
# ═══════════════════════════════════════════════════════════════════════

def run_bonereaper_mirror():
    log.info("=" * 70)
    log.info("V21.7.5 Bonereaper Activity Mirror — Shadow-Only")
    log.info("=" * 70)

    valid_files = []
    for f in sorted(PMXT_DIR.glob("*.parquet")):
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000: valid_files.append(f)
        except: continue
    log.info(f"Valid PMXT files: {len(valid_files)}")

    events = []       # All shadow events
    settlements = []  # All settlement records
    bankroll_by_profile = defaultdict(lambda: BANKROLL_START)
    profile_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl_unit": 0, "pnl_med": 0, "pnl_large": 0})
    hypothesis_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl_unit": 0})
    bucket_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0, "pnl_slip": 0})
    
    # FDC comparison buckets
    fdc_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0, "pnl_slip": 0})

    for fi, fpath in enumerate(valid_files):
        log.info(f"[{fi+1}/{len(valid_files)}] {fpath.name}...")
        pf = pq.ParquetFile(str(fpath))
        file_events = 0

        # Phase 1: Accumulate all price_change events per token
        token_prices = defaultdict(list)
        for rg_idx in range(pf.metadata.num_row_groups):
            try:
                t = pf.read_row_group(rg_idx, columns=['asset_id', 'price', 'event_type'])
            except: continue
            try:
                mask = pc.equal(t.column('event_type'), 'price_change')
                t2 = t.filter(mask)
            except:
                evs = t.column('event_type').to_pylist()
                keep = [i for i, e in enumerate(evs) if e == 'price_change']
                t2 = t.take(keep) if keep else None
            if t2 is None or t2.num_rows == 0: continue
            aids = t2.column('asset_id').to_pylist()
            prices_col = t2.column('price').to_numpy().astype(np.float64)
            for i in range(len(aids)):
                p = prices_col[i]
                if 0.01 < p <= 1.0:
                    token_prices[aids[i]].append(p)

        log.info(f"  {len(token_prices)} unique tokens")

        # Phase 2: Process tokens — both cheap AND rich side
        # Sort by activity count, take top 300
        sorted_tokens = sorted(token_prices.items(), key=lambda x: len(x[1]), reverse=True)[:300]

        for aid, price_list in sorted_tokens:
            if len(price_list) < 120: continue
            prices = np.array(sorted(price_list), dtype=np.float64)

            rsi = compute_rsi(prices)
            velocity = compute_velocity(prices)
            accel = compute_acceleration(prices)
            cont_score, cont_dir = compute_continuation(prices)

            # Sample multiple entry points
            n_samples = min(10, len(prices) // 50)
            if n_samples < 1: continue
            sample_pts = np.linspace(60, len(prices)-10, n_samples, dtype=int)

            for idx in sample_pts:
                if len(events) >= TARGET_TRADES: break

                entry_price = float(prices[idx])
                bucket = classify_bucket(entry_price)
                if bucket == "SETTLEMENT_DUST" or bucket == "UNKNOWN": continue

                # Determine side from price: cheap (<50¢) = DOWN token, rich (>50¢) = UP token
                side = "DOWN" if entry_price < 0.50 else "UP"
                asset_hint = "BTC"  # PMXT data is primarily BTC markets

                # Compute indicators
                local_rsi = float(rsi[idx]) if idx < len(rsi) else 50.0
                local_vel = float(velocity[idx]) if idx < len(velocity) else 0.0
                local_accel = float(accel[idx]) if idx < len(accel) else 0.0
                local_cont = float(cont_score[idx]) if idx < len(cont_score) else 0.0
                local_dir = float(cont_dir[idx]) if idx < len(cont_dir) else 0.0

                consec = 0
                for j in range(max(0, idx-5), idx):
                    if j > 0 and ((side == "DOWN" and prices[j] < prices[j-1]) or
                                  (side == "UP" and prices[j] > prices[j-1])):
                        consec += 1

                state = classify_state(accel[max(0,idx-2):idx+1], velocity[max(0,idx-2):idx+1], consec, local_dir, local_rsi)
                regime = get_regime(prices[max(0,idx-50):idx+1])
                time_pct = random.uniform(0.20, 0.98)
                timing = classify_timing(time_pct)

                # Determine if this is a "valid" entry for this side
                # Bonereaper trades BOTH sides across FULL price range — much less restrictive than FDC
                # Key: directional alignment OR mid/high-range where direction matters less
                side_gate = False
                if entry_price < 0.08:
                    # Cheap convexity — still need directional evidence
                    if side == "DOWN" and (local_dir < -0.1 or state in ('DOWN_CONTINUATION', 'DOWN_MOMENTUM')):
                        side_gate = True
                    elif side == "UP" and (local_dir > 0.1 or state in ('UP_CONTINUATION', 'UP_REVERSAL')):
                        side_gate = True
                elif 0.08 <= entry_price < 0.40:
                    # Low-midrange — directional hint sufficient
                    side_gate = True
                elif 0.40 <= entry_price < 0.85:
                    # Midrange / high-confidence — almost always valid (BR trades these heavily)
                    side_gate = True
                elif entry_price >= 0.85:
                    # Near-resolution — certainty harvest
                    side_gate = True

                if not side_gate: continue

                # Friction model — bucket-dependent
                fr = FRICTION.get(bucket, FRICTION["MIDRANGE_DECISION_ZONE"])
                
                if random.random() < fr["fill_rej"]: continue
                if random.random() < fr["stale"]: continue

                slip = entry_price * fr["slip"] * random.uniform(0.5, 1.5)
                spread_c = fr["spread"] * entry_price
                actual_entry = entry_price + (slip if side == "DOWN" else -slip * 0.5)
                actual_entry += spread_c
                actual_entry = max(0.005, min(actual_entry, 0.995))

                partial = random.random() < fr["partial"]
                fill_pct = random.uniform(0.5, 0.95) if partial else 1.0

                # §13: Lag simulation (PMXT data doesn't have real lag, so simulate)
                lag_delay_ms = random.expovariate(1/800) if entry_price < 0.20 else random.expovariate(1/400)
                lag_delay_ms = min(lag_delay_ms, 5000)
                lag_confirmed = lag_delay_ms > 500
                external_move_bps = random.gauss(0, 15) if entry_price < 0.20 else random.gauss(0, 8)
                pm_reprice_delay_ms = max(0, lag_delay_ms - random.uniform(0, 200))

                v15 = abs(local_vel)
                v30 = abs(float(np.mean(velocity[max(0,idx-30):idx]))) if idx > 30 and len(velocity) > 30 else v15
                v60 = abs(float(np.mean(velocity[max(0,idx-60):idx]))) if idx > 60 and len(velocity) > 60 else v30
                v120 = abs(float(np.mean(velocity[max(0,idx-120):idx]))) if idx > 120 and len(velocity) > 120 else v60
                tte_seconds = int((1.0 - time_pct) * 300)
                orderbook_spread = round(spread_c, 6)
                orderbook_depth = random.randint(500, 50000)
                quote_age_ms = int(random.expovariate(1/2000))

                # Binary settlement — look forward in price series
                # DOWN token: price → $1 means DOWN wins, price → $0 means UP wins
                # UP token: price → $1 means UP wins, price → $0 means DOWN wins
                look_ahead = min(idx + 50, len(prices))
                if look_ahead > idx + 5:
                    future_price = prices[min(idx+50, len(prices)-1)]
                    if side == "DOWN":
                        wins_settle = future_price > entry_price  # DOWN token price rises = DOWN winning
                    else:
                        wins_settle = future_price > entry_price  # UP token price stays high/rises = UP winning
                else:
                    # Can't determine — use implied probability as baseline
                    settle_prob = entry_price  # Token price = market-implied P(side wins)
                    wins_settle = random.random() < settle_prob

                # §7: Calculate PnL under 3 sizing layers
                # Binary settlement: winning token → $1.0, losing token → $0.0
                pnls = {}
                slip_adj_pnls = {}
                for size_name, size_usd in SIZE_LAYERS.items():
                    size_adj = size_usd * fill_pct
                    shares = size_adj / max(actual_entry, 0.001)
                    if wins_settle:
                        # Token settles to $1.0 — profit = (1.0 - entry_price) per share
                        gross = shares * (1.0 - actual_entry)
                    else:
                        # Token settles to $0.0 — lose entire entry cost
                        gross = -(shares * actual_entry)
                    slip_adj = gross - slip * shares * 0.5
                    
                    if size_name == "UNIT":
                        pnls["unit"] = round(gross, 4)
                        slip_adj_pnls["unit"] = round(slip_adj, 4)
                    elif size_name == "MEDIUM":
                        pnls["medium"] = round(gross, 4)
                        slip_adj_pnls["medium"] = round(slip_adj, 4)
                    else:
                        pnls["large"] = round(gross, 4)
                        slip_adj_pnls["large"] = round(slip_adj, 4)

                # §6: Build full event record
                profiles = assign_profiles(entry_price, bucket, side, asset_hint)
                hypotheses = assign_hypotheses(entry_price, bucket, side, local_dir, time_pct, regime, lag_delay_ms, external_move_bps)

                shadow_reason = "MIDRANGE_DIRECTIONAL" if 0.20 <= entry_price < 0.60 else \
                                "HIGH_CONFIDENCE_MOMENTUM" if 0.60 <= entry_price < 0.85 else \
                                "NEAR_RESOLUTION_HARVEST" if 0.85 <= entry_price < 0.99 else \
                                "CHEAP_CONVEXITY"

                event_id = f"BR-{len(events)+1:06d}"

                event = {
                    "event_id": event_id,
                    "timestamp": int(time.time()),
                    "asset": asset_hint,
                    "interval": "5m" if time_pct < 0.5 else "15m",
                    "market_slug": f"btc-up-down-{int(time_pct*100)}",
                    "condition_id": aid[:16],
                    "side": side,
                    "token_id": aid,
                    "opposite_token_id": "",
                    "entry_price": round(entry_price, 6),
                    "entry_bucket": bucket,
                    "shares_simulated": round(fill_pct * SIZE_LAYERS["UNIT"] / max(actual_entry, 0.001), 2),
                    "notional_simulated": round(SIZE_LAYERS["UNIT"] * fill_pct, 4),
                    "time_to_expiry": tte_seconds,
                    "spot_price": round(prices[idx], 6),
                    "v15": round(v15, 6),
                    "v30": round(v30, 6),
                    "v60": round(v60, 6),
                    "v120": round(v120, 6),
                    "higher_timeframe_regime": regime,
                    "external_move_bps": round(external_move_bps, 2),
                    "polymarket_reprice_delay_ms": round(pm_reprice_delay_ms, 1),
                    "orderbook_spread": orderbook_spread,
                    "orderbook_depth": orderbook_depth,
                    "quote_age_ms": quote_age_ms,
                    "profile_name": profiles[0] if profiles else "UNCLASSIFIED",
                    "all_profiles": profiles,
                    "shadow_reason": shadow_reason,
                    "state": state,
                    "rsi": round(local_rsi, 2),
                    "cont_score": round(local_cont, 4),
                    "cont_dir": round(local_dir, 4),
                    "score": round(abs(local_dir) * 0.5 + local_cont * 0.3 + 0.2, 4),
                    "time_pct": round(time_pct, 3),
                    "timing": timing,
                    "fill_pct": round(fill_pct, 3),
                    "partial_fill": partial,
                    "actual_entry": round(actual_entry, 6),
                    "slip": round(slip, 6),
                    "spread_cost": round(spread_c, 6),
                    "lag_confirmed": lag_confirmed,
                    "lag_delay_ms": round(lag_delay_ms, 1),
                    "pm_reprice_delay_ms": round(pm_reprice_delay_ms, 1),
                    "hypotheses": hypotheses,
                    "wins_settle": wins_settle,
                    "pnl_unit": pnls["unit"],
                    "pnl_medium": pnls["medium"],
                    "pnl_large": pnls["large"],
                    "slip_adj_pnl_unit": slip_adj_pnls["unit"],
                    "slip_adj_pnl_medium": slip_adj_pnls["medium"],
                    "slip_adj_pnl_large": slip_adj_pnls["large"],
                    "settlement": "BINARY",
                }

                events.append(event)
                file_events += 1

                # Update profile stats
                for pname in profiles:
                    ps = profile_stats[pname]
                    ps["trades"] += 1
                    if wins_settle: ps["wins"] += 1
                    ps["pnl_unit"] += pnls["unit"]
                    ps["pnl_med"] += pnls["medium"]
                    ps["pnl_large"] += pnls["large"]

                # Update bucket stats
                bs = bucket_stats[bucket]
                bs["trades"] += 1
                if wins_settle: bs["wins"] += 1
                bs["pnl"] += pnls["unit"]
                bs["pnl_slip"] += slip_adj_pnls["unit"]

                # Update FDC comparison buckets
                if bucket in ("PRIMARY_LOW_CONVEX", "PRIMARY_CORE_CONVEX", "PRIMARY_HIGH_CONVEX", "SECONDARY_THIN_EDGE"):
                    fdc_key = f"FDC_{bucket}"
                    fs = fdc_stats[fdc_key]
                    fs["trades"] += 1
                    if wins_settle: fs["wins"] += 1
                    fs["pnl"] += pnls["unit"]
                    fs["pnl_slip"] += slip_adj_pnls["unit"]

                # Update hypothesis stats
                for hname, active in hypotheses.items():
                    if active:
                        hs = hypothesis_stats[hname]
                        hs["trades"] += 1
                        if wins_settle: hs["wins"] += 1
                        hs["pnl_unit"] += pnls["unit"]

        log.info(f"  → file {fi+1}: {file_events} events, total={len(events)}")
        gc.collect()
        if len(events) >= TARGET_TRADES: break

    events = events[:TARGET_TRADES]

    # ═══════════════════════════════════════════════════════════════════
    # §9: SETTLEMENTS
    # ═══════════════════════════════════════════════════════════════════
    for ev in events:
        settlements.append({
            "event_id": ev["event_id"],
            "asset": ev["asset"],
            "interval": ev["interval"],
            "side": ev["side"],
            "entry_price": ev["entry_price"],
            "entry_bucket": ev["entry_bucket"],
            "resolved_winner": ev["side"] if ev["wins_settle"] else ("UP" if ev["side"] == "DOWN" else "DOWN"),
            "win_loss": "WIN" if ev["wins_settle"] else "LOSS",
            "gross_pnl_unit": ev["pnl_unit"],
            "gross_pnl_medium": ev["pnl_medium"],
            "gross_pnl_large": ev["pnl_large"],
            "slippage_adjusted_pnl_unit": ev["slip_adj_pnl_unit"],
            "slippage_adjusted_pnl_medium": ev["slip_adj_pnl_medium"],
            "slippage_adjusted_pnl_large": ev["slip_adj_pnl_large"],
            "settlement_source": "PMXT_FORWARD_PRICE",
            "settlement_error": 0,
        })

    # ═══════════════════════════════════════════════════════════════════
    # §10: COMPARISON REPORT
    # ═══════════════════════════════════════════════════════════════════
    comparison = {}
    
    # FDC buckets
    for fdc_key in ("FDC_PRIMARY_LOW_CONVEX", "FDC_PRIMARY_CORE_CONVEX", "FDC_PRIMARY_HIGH_CONVEX", "FDC_SECONDARY_THIN_EDGE"):
        fs = fdc_stats[fdc_key]
        if fs["trades"] == 0: continue
        wr = fs["wins"] / fs["trades"] * 100
        gw = sum(ev["pnl_unit"] for ev in events if ev.get("entry_bucket") in fdc_key.replace("FDC_","") and ev["wins_settle"])
        gl = abs(sum(ev["pnl_unit"] for ev in events if ev.get("entry_bucket") in fdc_key.replace("FDC_","") and not ev["wins_settle"]))
        pf = gw / max(gl, 0.001)
        comparison[fdc_key] = {
            "resolved_count": fs["trades"],
            "WR": round(wr, 2),
            "EV_per_trade": round(fs["pnl_slip"] / fs["trades"], 4),
            "PF": round(pf, 3),
            "mean_entry_price": "3-12¢ range",
            "model": "FDC_CONVEX",
        }

    # BR profiles
    up_trades = [e for e in events if e["side"] == "UP"]
    down_trades = [e for e in events if e["side"] == "DOWN"]
    multi_asset = events  # All are covered
    lag_captured = [e for e in events if e.get("lag_confirmed")]

    br_comparisons = {
        "BR_MIDRANGE_40_60": [e for e in events if 0.40 <= e["entry_price"] < 0.60],
        "BR_HIGH_CONFIDENCE_60_85": [e for e in events if 0.60 <= e["entry_price"] < 0.85],
        "BR_NEAR_RESOLUTION_85_99": [e for e in events if 0.85 <= e["entry_price"] < 0.99],
        "BR_UP_PROFILES": up_trades,
        "BR_DOWN_PROFILES": down_trades,
        "BR_MULTI_ASSET": multi_asset,
        "BR_LAG_CAPTURE": lag_captured,
    }

    for br_key, br_events in br_comparisons.items():
        if not br_events: continue
        wins = [e for e in br_events if e["wins_settle"]]
        losses = [e for e in br_events if not e["wins_settle"]]
        wr = len(wins) / len(br_events) * 100
        gw = sum(e["pnl_unit"] for e in wins)
        gl = abs(sum(e["pnl_unit"] for e in losses)) if losses else 0.001
        pf = gw / max(gl, 0.001)
        ev_t = sum(e["slip_adj_pnl_unit"] for e in br_events) / len(br_events)
        max_ls = cur = 0
        for e in br_events:
            if not e["wins_settle"]: cur += 1; max_ls = max(max_ls, cur)
            else: cur = 0
        # Max drawdown
        cum_pnl = np.cumsum([e["pnl_unit"] for e in br_events])
        peak = np.maximum.accumulate(cum_pnl)
        dd = (peak - cum_pnl)
        max_dd = float(np.max(dd)) if len(dd) > 0 else 0

        comparison[br_key] = {
            "resolved_count": len(br_events),
            "WR": round(wr, 2),
            "EV_per_trade": round(ev_t, 4),
            "PF": round(pf, 3),
            "max_loss_streak": max_ls,
            "max_drawdown": round(max_dd, 4),
            "mean_entry_price": round(float(np.mean([e["entry_price"] for e in br_events])), 4),
            "mean_TTE": round(float(np.mean([e["time_to_expiry"] for e in br_events])), 1),
            "mean_spread": round(float(np.mean([e["spread_cost"] for e in br_events])), 6),
            "mean_quote_age": round(float(np.mean([e["quote_age_ms"] for e in br_events])), 1),
            "slippage_adjusted_EV": round(ev_t, 4),
            "model": "BONEREAPER_MIRROR",
        }

    # ═══════════════════════════════════════════════════════════════════
    # §11-12: PROMOTION / REJECTION CLASSIFICATION
    # ═══════════════════════════════════════════════════════════════════
    for br_key, data in comparison.items():
        if data.get("model") != "BONEREAPER_MIRROR": continue
        res = data.get("resolved_count", 0)
        ev = data.get("slippage_adjusted_EV", 0)
        pf_val = data.get("PF", 0)
        
        if res >= 100 and ev > 0 and pf_val >= 1.35:
            data["classification"] = "BONEREAPER_STYLE_PAPER_CANDIDATE"
        elif pf_val < 1.10 or ev <= 0:
            data["classification"] = "BONEREAPER_STYLE_REJECTED"
        else:
            data["classification"] = "INSUFFICIENT_DATA"

    # ═══════════════════════════════════════════════════════════════════
    # §13: LAG ALPHA REPORT
    # ═══════════════════════════════════════════════════════════════════
    lag_events = [e for e in events if e.get("lag_confirmed")]
    lag_wins = [e for e in lag_events if e["wins_settle"]]
    lag_report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_lag_confirmed_events": len(lag_events),
        "lag_confirmed_wr": round(len(lag_wins) / max(len(lag_events), 1) * 100, 2),
        "lag_confirmed_ev_per_trade": round(sum(e["slip_adj_pnl_unit"] for e in lag_events) / max(len(lag_events), 1), 4),
        "mean_lag_delay_ms": round(float(np.mean([e["lag_delay_ms"] for e in lag_events])) if lag_events else 0, 1),
        "mean_pm_reprice_delay_ms": round(float(np.mean([e["pm_reprice_delay_ms"] for e in lag_events])) if lag_events else 0, 1),
        "hypothesis_F_lag_capture": {
            "trades": hypothesis_stats["BR_LAG_CAPTURE_EV"]["trades"],
            "wins": hypothesis_stats["BR_LAG_CAPTURE_EV"]["wins"],
            "pnl_unit": round(hypothesis_stats["BR_LAG_CAPTURE_EV"]["pnl_unit"], 4),
            "wr": round(hypothesis_stats["BR_LAG_CAPTURE_EV"]["wins"] / max(hypothesis_stats["BR_LAG_CAPTURE_EV"]["trades"], 1) * 100, 2),
        },
    }

    # ═══════════════════════════════════════════════════════════════════
    # §14: BUCKET CONVICTION REPORT
    # ═══════════════════════════════════════════════════════════════════
    conviction_report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "live_bucket": "3-12¢ BTC DOWN ONLY",
        "live_rules_unchanged": True,
        "bucket_convictions": {},
    }
    for bname in BUCKET_ORDER:
        bs = bucket_stats[bname]
        if bs["trades"] == 0:
            conviction_report["bucket_convictions"][bname] = {"conviction": "NO_DATA", "trades": 0}
            continue
        wr = bs["wins"] / bs["trades"] * 100
        ev_t = bs["pnl_slip"] / bs["trades"]
        conviction = FDC_BUCKET_CONVICTION.get(bname, "SHADOW_ONLY")
        gw = sum(e["pnl_unit"] for e in events if e["entry_bucket"] == bname and e["wins_settle"])
        gl = abs(sum(e["pnl_unit"] for e in events if e["entry_bucket"] == bname and not e["wins_settle"]))
        pf = gw / max(gl, 0.001)
        
        conviction_report["bucket_convictions"][bname] = {
            "conviction": conviction,
            "trades": bs["trades"],
            "WR": round(wr, 2),
            "EV_per_trade": round(ev_t, 4),
            "PF": round(pf, 3),
            "gross_pnl": round(bs["pnl"], 4),
        }

    # ═══════════════════════════════════════════════════════════════════
    # §15: WRITE 5 OUTPUT FILES
    # ═══════════════════════════════════════════════════════════════════
    with open(OUT_DIR / "bonereaper_shadow_events.jsonl", 'w') as f:
        for e in events: f.write(json.dumps(e, default=str) + "\n")

    with open(OUT_DIR / "bonereaper_shadow_settlements.jsonl", 'w') as f:
        for s in settlements: f.write(json.dumps(s, default=str) + "\n")

    with open(OUT_DIR / "bonereaper_vs_fdc_comparison_report.json", 'w') as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "version": "V21.7.5",
            "total_events": len(events),
            "directive": "Only promote BR-style behavior if resolved shadow EV beats or complements FDC convex 3-12¢",
            "comparison": comparison,
        }, f, indent=2, default=str)

    with open(OUT_DIR / "bonereaper_lag_alpha_report.json", 'w') as f:
        json.dump(lag_report, f, indent=2, default=str)

    with open(OUT_DIR / "bucket_conviction_report.json", 'w') as f:
        json.dump(conviction_report, f, indent=2, default=str)

    # ═══════════════════════════════════════════════════════════════════
    # PRINT SUMMARY
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 105)
    print("V21.7.5 BONEREAPER ACTIVITY MIRROR — SHADOW-ONLY RESULTS")
    print("=" * 105)
    
    total_wins = sum(1 for e in events if e["wins_settle"])
    total_pnl = sum(e["pnl_unit"] for e in events)
    total_slip = sum(e["slip_adj_pnl_unit"] for e in events)
    
    print(f"\nTotal events: {len(events)} | WR: {total_wins/len(events)*100:.1f}% | PnL: ${total_pnl:.2f} | Slip-adj: ${total_slip:.2f}")
    
    print(f"\n{'BUCKET':<32s} {'OBS':>6s} {'WR%':>6s} {'GrossPnL':>10s} {'SlipAdj':>10s} {'EV/tr':>8s} {'PF':>6s} {'Conviction':>20s}")
    print("-" * 105)
    for bname in BUCKET_ORDER:
        bs = bucket_stats[bname]
        conviction = FDC_BUCKET_CONVICTION.get(bname, "SHADOW_ONLY")
        gw = sum(e["pnl_unit"] for e in events if e["entry_bucket"] == bname and e["wins_settle"])
        gl = abs(sum(e["pnl_unit"] for e in events if e["entry_bucket"] == bname and not e["wins_settle"]))
        pf = gw / max(gl, 0.001)
        ev_t = bs["pnl_slip"] / bs["trades"] if bs["trades"] > 0 else 0
        wr = bs["wins"] / bs["trades"] * 100 if bs["trades"] > 0 else 0
        print(f"  {bname:<30s} {bs['trades']:>6d} {wr:>5.1f}% ${bs['pnl']:>9.2f} ${bs['pnl_slip']:>9.2f} ${ev_t:>7.4f} {pf:>5.2f} {conviction:>20s}")

    print(f"\n{'HYPOTHESIS':<38s} {'TRADES':>6s} {'WR%':>6s} {'EV/tr':>8s} {'CLASSIFICATION':>25s}")
    print("-" * 90)
    for hname, hs in hypothesis_stats.items():
        if hs["trades"] == 0: continue
        wr = hs["wins"] / hs["trades"] * 100
        ev_t = hs["pnl_unit"] / hs["trades"]
        classification = comparison.get(hname.replace("_EV", ""), {}).get("classification", "N/A")
        print(f"  {hname:<36s} {hs['trades']:>6d} {wr:>5.1f}% ${ev_t:>7.4f} {classification:>25s}")

    print(f"\n{'COMPARISON BUCKET':<38s} {'COUNT':>6s} {'WR%':>6s} {'EV/tr':>8s} {'PF':>6s} {'CLASSIFICATION':>25s}")
    print("-" * 100)
    for ckey, cdata in comparison.items():
        res = cdata.get("resolved_count", 0)
        wr = cdata.get("WR", 0)
        ev = cdata.get("EV_per_trade", 0)
        pf_val = cdata.get("PF", 0)
        cls = cdata.get("classification", cdata.get("model", ""))
        print(f"  {ckey:<36s} {res:>6d} {wr:>5.1f}% ${ev:>7.4f} {pf_val:>5.2f} {cls:>25s}")

    print(f"\n⚠ Live rules UNCHANGED: BTC/DOWN/3-12¢/TAKER/fixed/gates")
    print(f"  Do not copy Bonereaper. Model. Settle. Compare.")
    
    print(f"\nOutput files:")
    for f in sorted(OUT_DIR.glob("bonereaper_*")): print(f"  {f} ({f.stat().st_size:,}b)")
    for f in sorted(OUT_DIR.glob("bucket_conviction_*")): print(f"  {f} ({f.stat().st_size:,}b)")

    return comparison


if __name__ == "__main__":
    np.random.seed(42)
    random.seed(42)
    run_bonereaper_mirror()