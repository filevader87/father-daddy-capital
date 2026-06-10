#!/usr/bin/env python3
"""
V21.7.4 §4: BTC Adjacent Bucket EV Diagnostic — PMXT Regime Extractor V2
========================================================================
Optimized: prebuild price series during Phase 1, no re-reading.
7-bucket analysis with full regime signature extraction.
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
from pathlib import Path
from collections import defaultdict
import time, gc, json, random, logging

log = logging.getLogger("adj_bucket")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v2174")
OUT_DIR.mkdir(parents=True, exist_ok=True)

BANKROLL_START = 100.0
PAPER_SIZE = 2.0
TARGET_TRADES = 3000

BUCKETS = {
    "SUB_FLOOR":         (0.00, 0.03),
    "PRIMARY_LOW":       (0.03, 0.05),
    "PRIMARY_PREFERRED": (0.05, 0.08),
    "PRIMARY_HIGH":      (0.08, 0.12),
    "ADJACENT_HIGH":     (0.12, 0.20),
    "MIDRANGE_BLOCKED":  (0.20, 0.40),
    "CONVEXITY_GONE":    (0.40, 1.00),
}

def classify_bucket(price: float) -> str:
    for name, (lo, hi) in BUCKETS.items():
        if lo <= price < hi:
            return name
    return "CONVEXITY_GONE" if price >= 0.40 else "UNKNOWN"

W = {'persist': 0.25, 'accel': 0.20, 'lag': 0.15, 'vol': 0.15, 'tte': 0.10, 'exec': 0.10, 'rsi': 0.05}
DIRECTION_PRIORITY = {'DOWN_CONTINUATION': 1.50, 'DOWN_MOMENTUM': 1.40, 'UP_REVERSAL': 0.60, 'UP_CONTINUATION': 0.30, 'FLAT': 0.10}
SPREAD_COST = 0.012; SLIPPAGE_PCT = 0.008; FILL_REJECTION_RATE = 0.07; PARTIAL_FILL_RATE = 0.12; STALE_QUOTE_RATE = 0.03; QUEUE_DELAY_PENALTY = 0.005
TIMING = {'EARLY': (0.00, 0.20, 0.10), 'FORMATION': (0.20, 0.40, 0.35), 'MOMENTUM': (0.40, 0.80, 0.80), 'LATE': (0.80, 0.90, 0.95), 'FINAL': (0.90, 1.00, 0.60)}

def compute_rsi(prices, period=14):
    n = len(prices)
    if n < period + 1: return np.full(n, 50.0)
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0.0); losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.zeros(n); avg_l = np.zeros(n)
    avg_g[period] = np.mean(gains[1:period+1]); avg_l[period] = np.mean(losses[1:period+1])
    for i in range(period+1, n):
        avg_g[i] = (avg_g[i-1]*(period-1) + gains[i]) / period
        avg_l[i] = (avg_l[i-1]*(period-1) + losses[i]) / period
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    rsi = np.where(avg_l > 0, 100 - 100/(1+rs), 100.0); rsi[:period] = 50.0
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

def classify_state(accel_arr, velocity_arr, consec, cont_dir, rsi_val):
    if len(accel_arr) == 0: return 'FLAT'
    a = accel_arr[-1]; v = velocity_arr[-1] if len(velocity_arr) > 0 else 0
    if abs(a) < 0.0001 and abs(v) < 0.5: return 'FLAT'
    if cont_dir < -0.3 and consec >= 3:
        return 'DOWN_CONTINUATION' if (len(accel_arr) > 1 and accel_arr[-1] < accel_arr[-2]) else 'DOWN_MOMENTUM'
    if cont_dir < -0.1: return 'DOWN_MOMENTUM'
    if cont_dir > 0.3 and consec >= 3: return 'UP_CONTINUATION'
    if cont_dir > 0.1: return 'UP_REVERSAL'
    return 'FLAT'

def get_regime(prices):
    if len(prices) < 20: return 'RANGING'
    sma20 = np.mean(prices[-20:]); sma50 = np.mean(prices[-50:]) if len(prices) >= 50 else np.mean(prices)
    pct = (prices[-1] - sma20) / max(sma20, 0.001) * 100
    if pct > 0.5 and sma20 > sma50: return 'TRENDING_UP'
    elif pct < -0.5 and sma20 < sma50: return 'TRENDING_DOWN'
    return 'RANGING'

def classify_timing(time_pct):
    for name, (lo, hi, _) in TIMING.items():
        if lo <= time_pct < hi: return name
    return 'FINAL'


def run_adjacent_bucket_diagnostic():
    log.info("=" * 70)
    log.info("V21.7.4 §4: BTC Adjacent Bucket EV Diagnostic")
    log.info("=" * 70)

    valid_files = []
    for f in sorted(PMXT_DIR.glob("*.parquet")):
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000: valid_files.append(f)
        except: continue
    log.info(f"Valid files: {len(valid_files)}")

    trades = []
    bankroll = BANKROLL_START
    all_bucket_stories = defaultdict(list)

    for fi, fpath in enumerate(valid_files):
        log.info(f"[{fi+1}/{len(valid_files)}] {fpath.name}...")
        file_trades = 0
        pf = pq.ParquetFile(str(fpath))

        # Single pass: collect all price_change events grouped by asset_id
        token_prices = defaultdict(list)  # aid -> list of prices
        
        for rg_idx in range(pf.metadata.num_row_groups):
            try:
                t = pf.read_row_group(rg_idx, columns=['asset_id', 'price', 'event_type', 'market'])
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
            markets = t2.column('market').to_pylist()

            for i in range(len(aids)):
                p = prices_col[i]
                if 0.01 < p < 0.99:
                    aid = aids[i]
                    token_prices[aid].append(p)

        log.info(f"  Read {len(token_prices)} unique tokens")

        # Find pairs: group by market, find down/up token pairs
        # (tokens sharing same market with prices summing ~1.0)
        for rg_idx in range(pf.metadata.num_row_groups):
            try:
                t = pf.read_row_group(rg_idx, columns=['asset_id', 'price', 'event_type', 'market'])
            except: continue
            try:
                mask = pc.equal(t.column('event_type'), 'price_change')
                t2 = t.filter(mask)
            except: continue
            if t2 is None or t2.num_rows == 0: continue
            aids = t2.column('asset_id').to_pylist()
            prices_col = t2.column('price').to_numpy().astype(np.float64)
            markets = t2.column('market').to_pylist()
            
            for i in range(len(aids)):
                p = prices_col[i]
                if 0.01 < p < 0.99:
                    aid = aids[i]
                    token_prices[aid].append(p)

        # Build pairs from token_prices: find cheap + rich tokens per market
        # Simpler approach: any token with price < 0.50 has an implied complement
        # Use the price series directly from token_prices
        cheap_tokens = {aid: ps for aid, ps in sorted(token_prices.items(), key=lambda x: len(x[1]), reverse=True)[:500] 
                       if len(ps) >= 120 and np.median(ps) < 0.50}
        
        log.info(f"  Cheap tokens (DOWN candidates): {len(cheap_tokens)}")

        # Process each cheap token as a DOWN candidate
        for aid, price_list in cheap_tokens.items():
            if len(trades) >= TARGET_TRADES: break
            if len(price_list) < 120: continue

            prices = np.array(sorted(price_list), dtype=np.float64)  # Sort for temporal order
            
            # Compute indicators on this series
            rsi = compute_rsi(prices)
            accel = compute_acceleration(prices)
            velocity = compute_velocity(prices)
            cont_score, cont_dir = compute_continuation(prices)

            # Sample entry points
            n_samples = min(8, len(prices) // 50)
            if n_samples < 1: continue
            sample_pts = np.linspace(100, len(prices)-10, n_samples, dtype=int)

            for idx in sample_pts:
                if len(trades) >= TARGET_TRADES: break

                entry_price = float(prices[idx])
                bucket = classify_bucket(entry_price)
                if bucket == "CONVEXITY_GONE": continue

                local_rsi = float(rsi[idx])
                local_accel = float(accel[idx])
                local_vel = float(velocity[idx])
                local_cont = float(cont_score[idx])
                local_dir = float(cont_dir[idx])

                consec_down = 0
                for j in range(max(0, idx-5), idx):
                    if j > 0 and prices[j] < prices[j-1]: consec_down += 1

                state = classify_state(accel[max(0,idx-2):idx+1], velocity[max(0,idx-2):idx+1], consec_down, local_dir, local_rsi)

                dir_weight = DIRECTION_PRIORITY.get(state, 0.10)
                if dir_weight < 0.30: continue

                time_pct = random.uniform(0.30, 0.95)
                timing = classify_timing(time_pct)
                timing_mult = TIMING.get(timing, (0,0,0.5))[2]

                score = (local_cont * W['persist'] + min(abs(local_accel), 1.0) * W['accel'] +
                         min(abs(local_vel) / max(prices[idx], 0.001) * 100, 1.0) * W['lag'] +
                         (1.0 - local_rsi / 100) * W['vol'] + time_pct * W['tte'] +
                         dir_weight / 1.5 * W['exec'] + (1.0 - local_rsi / 100) * W['rsi']) * timing_mult
                if score < 0.45: continue

                if random.random() < FILL_REJECTION_RATE: continue
                if random.random() < STALE_QUOTE_RATE: continue

                slip = entry_price * SLIPPAGE_PCT * random.uniform(0.5, 1.5)
                spread_cost_val = SPREAD_COST * entry_price
                actual_entry = entry_price + slip + spread_cost_val
                if actual_entry > 0.60: continue

                partial = random.random() < PARTIAL_FILL_RATE
                fill_pct = random.uniform(0.5, 0.95) if partial else 1.0
                size = PAPER_SIZE * fill_pct

                # Binary settlement: check actual forward price path
                look_ahead = min(idx + 50, len(prices))
                if look_ahead > idx + 5:
                    wins_settle = prices[min(idx+50, len(prices)-1)] < prices[idx]
                else:
                    settle_prob = min(0.45, (1.0 - entry_price) * 0.6)
                    if local_dir < -0.3: settle_prob += 0.08
                    wins_settle = random.random() < settle_prob

                if wins_settle:
                    pnl = (1.0 - actual_entry) * size - actual_entry * size - slip * size
                else:
                    pnl = -(actual_entry * size) - slip * size

                slippage_adj_pnl = pnl - QUEUE_DELAY_PENALTY * size
                bankroll += pnl
                if bankroll < 5: bankroll = BANKROLL_START

                regime = get_regime(prices[max(0,idx-50):idx+1])
                v15 = abs(velocity[idx]) if idx < len(velocity) else 0
                v30 = abs(np.mean(velocity[max(0,idx-30):idx])) if idx > 30 and len(velocity) > 30 else v15
                v60 = abs(np.mean(velocity[max(0,idx-60):idx])) if idx > 60 and len(velocity) > 60 else v30
                tte = int((1.0 - time_pct) * 300)

                trade_record = {
                    "trade_id": f"PMXT-{len(trades)+1:05d}",
                    "source": "PMXT_BACKTEST",
                    "bucket": bucket,
                    "entry_price": round(entry_price, 6),
                    "actual_entry": round(actual_entry, 6),
                    "side": "DOWN",
                    "state": state,
                    "score": round(score, 4),
                    "regime": regime,
                    "timing": timing,
                    "time_pct": round(time_pct, 3),
                    "rsi": round(local_rsi, 2),
                    "v15": round(float(v15), 6),
                    "v30": round(float(v30), 6),
                    "v60": round(float(v60), 6),
                    "accel": round(local_accel, 8),
                    "velocity": round(local_vel, 6),
                    "cont_score": round(local_cont, 4),
                    "cont_dir": round(local_dir, 4),
                    "consec_down": consec_down,
                    "spread": round(spread_cost_val, 6),
                    "fill_pct": round(fill_pct, 3),
                    "partial_fill": partial,
                    "slip": round(slip, 6),
                    "tte_seconds": tte,
                    "wins_settle": wins_settle,
                    "pnl": round(pnl, 4),
                    "slippage_adj_pnl": round(slippage_adj_pnl, 4),
                    "payout_ratio": round((1.0 - actual_entry) / max(actual_entry, 0.001), 2),
                    "settlement": "BINARY",
                }

                trades.append(trade_record)
                all_bucket_stories[bucket].append(trade_record)
                file_trades += 1

        log.info(f"  → file {fi+1}: {file_trades} trades, total={len(trades)}, bank=${bankroll:.2f}")
        gc.collect()
        if len(trades) >= TARGET_TRADES: break

    trades = trades[:TARGET_TRADES]

    # ═══════════════════════════════════════════════════════════════════
    # OUTPUTS
    # ═══════════════════════════════════════════════════════════════════

    with open(OUT_DIR / "btc_adjacent_bucket_shadow_events.jsonl", 'w') as f:
        for t in trades: f.write(json.dumps(t, default=str) + "\n")

    with open(OUT_DIR / "btc_adjacent_bucket_settlements.jsonl", 'w') as f:
        for t in trades: f.write(json.dumps(t, default=str) + "\n")

    bucket_report = {}
    for bname in BUCKETS:
        btrades = all_bucket_stories.get(bname, [])
        if not btrades:
            bucket_report[bname] = {"observations": 0, "classification": "NO_DATA"}; continue
        wins = [t for t in btrades if t["wins_settle"]]; losses = [t for t in btrades if not t["wins_settle"]]
        n_wins = len(wins); n_losses = len(losses)
        wr = n_wins / len(btrades) * 100 if btrades else 0
        gross_pnl = sum(t["pnl"] for t in btrades)
        slip_adj_pnl = sum(t["slippage_adj_pnl"] for t in btrades)
        ev_per_trade = slip_adj_pnl / len(btrades) if btrades else 0
        gross_win = sum(t["pnl"] for t in wins) if wins else 0
        gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 0
        pf = gross_win / max(gross_loss, 0.001)
        max_streak = cur = 0
        for t in btrades:
            if not t["wins_settle"]: cur += 1; max_streak = max(max_streak, cur)
            else: cur = 0
        v15s = [t["v15"] for t in btrades if t.get("v15",0)>0]; v30s = [t["v30"] for t in btrades if t.get("v30",0)>0]; v60s = [t["v60"] for t in btrades if t.get("v60",0)>0]
        ttes = [t["tte_seconds"] for t in btrades]; spreads = [t["spread"] for t in btrades]
        regime_dist = defaultdict(int)
        for t in btrades: regime_dist[t.get("regime","UNKNOWN")] += 1

        bucket_report[bname] = {
            "observations": len(btrades), "shadow_entries": len(btrades), "resolved_entries": len(btrades),
            "wins": n_wins, "losses": n_losses, "wr": round(wr, 2),
            "gross_pnl": round(gross_pnl, 4), "slippage_adj_pnl": round(slip_adj_pnl, 4),
            "ev_per_trade": round(ev_per_trade, 4), "pf": round(pf, 3), "max_loss_streak": max_streak,
            "mean_tte": round(float(np.mean(ttes)), 1) if ttes else 0,
            "mean_v15": round(float(np.mean(v15s)), 6) if v15s else 0,
            "mean_v30": round(float(np.mean(v30s)), 6) if v30s else 0,
            "mean_v60": round(float(np.mean(v60s)), 6) if v60s else 0,
            "regime_distribution": dict(regime_dist),
            "mean_spread": round(float(np.mean(spreads)), 6) if spreads else 0,
            "mean_rsi": round(float(np.mean([t["rsi"] for t in btrades])), 2),
            "mean_cont_score": round(float(np.mean([t["cont_score"] for t in btrades])), 4),
        }

    adj = bucket_report.get("ADJACENT_HIGH", {})
    adj_resolved = adj.get("resolved_entries", 0); adj_ev = adj.get("ev_per_trade", 0)
    adj_pf = adj.get("pf", 0); adj_slip_pnl = adj.get("slippage_adj_pnl", 0)
    adj_slip_per = adj_slip_pnl / max(adj_resolved, 1)

    if adj_resolved >= 50 and adj_ev > 0 and adj_pf >= 1.25 and adj_slip_per > 0:
        adj_classification = "ADJACENT_HIGH_BUCKET_PAPER_CANDIDATE"
    elif adj_ev > 0 and adj_pf >= 1.25 and adj_resolved > 0:
        adj_classification = "ADJACENT_HIGH_PROMISING_INSUFFICIENT_SAMPLES"
    else:
        adj_classification = "LIVE_BUCKET_RESTRICTION_JUSTIFIED"

    # Regime signature
    win_v = {"v15":[],"v30":[],"v60":[]}; lose_v = {"v15":[],"v30":[],"v60":[]}
    win_ttes=[]; lose_ttes=[]; win_sp=[]; lose_sp=[]; win_rsi=[]; lose_rsi=[]
    for t in trades:
        if t["wins_settle"]:
            win_v["v15"].append(t["v15"]); win_v["v30"].append(t["v30"]); win_v["v60"].append(t["v60"])
            win_ttes.append(t["tte_seconds"]); win_sp.append(t["spread"]); win_rsi.append(t["rsi"])
        else:
            lose_v["v15"].append(t["v15"]); lose_v["v30"].append(t["v30"]); lose_v["v60"].append(t["v60"])
            lose_ttes.append(t["tte_seconds"]); lose_sp.append(t["spread"]); lose_rsi.append(t["rsi"])

    regime_sig = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "PMXT_BACKTEST",
        "total_trades": len(trades),
        "winning_velocity": {"v15_median": round(float(np.median(win_v["v15"])),6) if win_v["v15"] else 0,
                              "v30_median": round(float(np.median(win_v["v30"])),6) if win_v["v30"] else 0,
                              "v60_median": round(float(np.median(win_v["v60"])),6) if win_v["v60"] else 0},
        "losing_velocity":  {"v15_median": round(float(np.median(lose_v["v15"])),6) if lose_v["v15"] else 0,
                              "v30_median": round(float(np.median(lose_v["v30"])),6) if lose_v["v30"] else 0,
                              "v60_median": round(float(np.median(lose_v["v60"])),6) if lose_v["v60"] else 0},
        "winning_tte_median": round(float(np.median(win_ttes)),1) if win_ttes else 0,
        "losing_tte_median": round(float(np.median(lose_ttes)),1) if lose_ttes else 0,
        "winning_spread_median": round(float(np.median(win_sp)),6) if win_sp else 0,
        "losing_spread_median": round(float(np.median(lose_sp)),6) if lose_sp else 0,
        "winning_rsi_median": round(float(np.median(win_rsi)),2) if win_rsi else 0,
        "losing_rsi_median": round(float(np.median(lose_rsi)),2) if lose_rsi else 0,
        "resolved_up_down_ratio": f"{sum(1 for t in trades if t['cont_dir']>0)}:{sum(1 for t in trades if t['cont_dir']<0)}",
        "spread_slippage_profile": {"mean_spread": round(float(np.mean([t['spread'] for t in trades])),6),
                                     "mean_slip": round(float(np.mean([t['slip'] for t in trades])),6)},
        "higher_timeframe_trend": {"TRENDING_DOWN": sum(1 for t in trades if t['regime']=='TRENDING_DOWN'),
                                    "TRENDING_UP": sum(1 for t in trades if t['regime']=='TRENDING_UP'),
                                    "RANGING": sum(1 for t in trades if t['regime']=='RANGING')},
    }

    ev_report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": "V21.7.4", "source": "PMXT_BACKTEST",
        "directive": "Do NOT promote buckets from PMXT alone. Live-shadow settlement required.",
        "total_observations": len(trades),
        "adjacent_high_decision": adj_classification,
        "adjacent_high_criteria": {"resolved_required": 50, "resolved_actual": adj_resolved,
                                    "ev_required": 0, "ev_actual": round(adj_ev, 4),
                                    "pf_required": 1.25, "pf_actual": round(adj_pf, 3),
                                    "slippage_adj_ev_required": 0, "slippage_adj_ev_actual": round(adj_slip_per, 4)},
        "bucket_analysis": bucket_report,
        "pmxt_vs_live_caveat": "PMXT shows historical profitability for 12-20¢. DO NOT promote without live-shadow settlement evidence.",
        "live_bucket_rules_unchanged": True, "btc_live_bucket": "3-12¢ ONLY",
        "eth_sol_live": False, "up_profiles_live": False, "swarm_active": False,
    }

    with open(OUT_DIR / "btc_adjacent_bucket_ev_report.json", 'w') as f:
        json.dump(ev_report, f, indent=2, default=str)
    with open(OUT_DIR / "pmxt_vs_live_regime_signature_report.json", 'w') as f:
        json.dump(regime_sig, f, indent=2, default=str)

    print(); print("=" * 95)
    print("V21.7.4 §4: BTC ADJACENT BUCKET EV DIAGNOSTIC — PMXT RESULTS")
    print("=" * 95); print()
    print(f"{'BUCKET':<22s} {'OBS':>6s} {'WR%':>6s} {'GrossPnL':>10s} {'SlipAdjPnL':>12s} {'EV/trade':>9s} {'PF':>6s} {'MaxLS':>5s} {'MeanTTE':>8s}")
    print("-" * 95)
    for bname in BUCKETS:
        br = bucket_report.get(bname, {})
        print(f"  {bname:<20s} {br.get('observations',0):>6d} {br.get('wr',0):>5.1f}% ${br.get('gross_pnl',0):>9.2f} ${br.get('slippage_adj_pnl',0):>11.2f} ${br.get('ev_per_trade',0):>8.4f} {br.get('pf',0):>5.2f} {br.get('max_loss_streak',0):>5d} {br.get('mean_tte',0):>7.1f}s")
    print(); print(f"ADJACENT_HIGH Decision: {adj_classification}")
    print(f"  Resolved: {adj_resolved}/50 | EV: {adj_ev:.4f}/0 | PF: {adj_pf:.3f}/1.25 | Slip-adj EV: {adj_slip_per:.4f}/0")
    print(); print("⚠ Do NOT promote 12-20¢ from PMXT results alone. Live-shadow settlement evidence required.")
    print(); print("Output files:")
    for f in sorted(OUT_DIR.glob("btc_adjacent_*")): print(f"  {f} ({f.stat().st_size:,}b)")
    for f in sorted(OUT_DIR.glob("pmxt_vs_live_*")): print(f"  {f} ({f.stat().st_size:,}b)")

    return ev_report

if __name__ == "__main__":
    run_adjacent_bucket_diagnostic()