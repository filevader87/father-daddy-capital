#!/usr/bin/env python3
"""
V21 PMXT 2000-Trade Simulation V4 — FAST VECTORIZED
======================================================
Uses proven V18.8 PMXT data pipeline: RG-by-RG processing, 
cheap-side filtering, binary pair settlement.

Key V21 upgrades:
- Continuation prior (3:1 Bayesian)
- Both UP and DOWN side scored
- Opportunity scoring (§7 weighted composite)
- Binary settlement via paired tokens
- Execution friction model
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
from pathlib import Path
from collections import defaultdict
import time, gc, json, random

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
TARGET_TRADES = 2000
BANKROLL_START = 100.0
MAX_POSITION_USD = 2.00
MIN_TRADE_SIZE = 0.25

TIER_CONFIG = {
    'severe_oversold_down':  {'max_price': 0.50, 'size_pct': 0.10},
    'severe_overbought_up':  {'max_price': 0.50, 'size_pct': 0.10},
    'oversold_down':         {'max_price': 0.20, 'size_pct': 0.06},
    'overbought_up':         {'max_price': 0.20, 'size_pct': 0.05},
    'direction_down_cheap':  {'max_price': 0.15, 'size_pct': 0.03},
    'direction_up_cheap':    {'max_price': 0.15, 'size_pct': 0.03},
}

SPREAD_COST = 0.01
SLIPPAGE_PCT = 0.005
FILL_REJECTION_RATE = 0.05
PARTIAL_FILL_RATE = 0.10


def compute_rsi(prices, period=14):
    n = len(prices)
    if n < period + 1:
        return np.full(n, 50.0)
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_g = np.zeros(n); avg_l = np.zeros(n)
    avg_g[period] = np.mean(gains[1:period+1])
    avg_l[period] = np.mean(losses[1:period+1])
    for i in range(period+1, n):
        avg_g[i] = (avg_g[i-1]*(period-1) + gains[i]) / period
        avg_l[i] = (avg_l[i-1]*(period-1) + losses[i]) / period
    rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    rsi = np.where(avg_l > 0, 100 - 100/(1+rs), 100.0)
    rsi[:period] = 50.0
    return rsi


def get_rsi_zone(rsi):
    if rsi < 25: return 'severe_oversold'
    if rsi < 30: return 'oversold'
    if rsi < 35: return 'near_oversold'
    if rsi > 73: return 'severe_overbought'
    if rsi > 70: return 'overbought'
    if rsi > 65: return 'near_overbought'
    return 'dead_zone'


def get_regime(prices):
    if len(prices) < 20: return 'unknown'
    r = prices[-20:]
    vol = np.std(r) / max(abs(np.mean(r)), 1e-9)
    trend = (r[-1] - r[0]) / max(abs(r[0]), 1e-9)
    if vol < 0.01: return 'ranging'
    if vol > 0.05: return 'volatile'
    if trend > 0.02: return 'trending_up'
    if trend < -0.02: return 'trending_down'
    return 'balanced'


def detect_direction(prices, lookback=3):
    n = len(prices)
    if n < 4: return 'FLAT', 0.0
    ref = prices[-1-lookback] if n > lookback else prices[0]
    delta = (prices[-1] - ref) / max(abs(ref), 1e-9) * 100
    v15 = (prices[-1] - prices[max(-4, -n)]) / max(abs(prices[max(-4, -n)]), 1e-9) * 100 if n >= 4 else abs(delta)
    velocity = 0.6 * v15 + 0.4 * abs(delta)
    
    consec = 0
    for i in range(1, min(5, n)):
        if n-i-1 >= 0 and prices[-i] > prices[-i-1]: consec += 1
        elif n-i-1 >= 0 and prices[-i] < prices[-i-1]: consec -= 1
    
    persistence = 1.0 if consec > 1 else (-1.0 if consec < -1 else 0.0)
    blended = 0.4 * np.sign(delta) * min(abs(delta), 1.0) + 0.6 * persistence
    
    if blended > 0.3: return 'UP', velocity
    elif blended < -0.3: return 'DOWN', velocity
    return 'FLAT', abs(velocity)


def classify_signal_v21(rsi, direction, velocity, regime, time_pct, price):
    """V21 directional extraction + opportunity scoring — no separate spread param."""
    rsi_zone = get_rsi_zone(rsi)
    
    # Spread heuristic based on price level
    if price < 0.10: spread = 0.02
    elif price < 0.20: spread = 0.015
    elif price < 0.40: spread = 0.01
    else: spread = 0.008
    
    if spread > 0.03: return None, 0.0, 'spread_trap'
    
    # Composite score (§7 weights)
    dir_s = min(1.0, abs(velocity) / 0.5) * (0.8 if direction == 'DOWN' else (0.6 if direction == 'UP' else 0.05))
    mom_s = min(1.0, abs(velocity) / 1.0)
    lag_s = 0.2  # neutral
    vol_s = min(1.0, abs(velocity) * 2) if abs(velocity) > 0.15 else 0.1
    
    if time_pct < 0.20: tte_s = 0.05
    elif time_pct < 0.40: tte_s = 0.40
    elif time_pct < 0.80: tte_s = 0.75
    elif time_pct < 0.90: tte_s = 0.95
    else: tte_s = 0.70
    if abs(velocity) < 0.05: tte_s *= 0.5
    
    exec_s = 0.9 if spread < 0.01 else (0.6 if spread < 0.02 else 0.3)
    cross_s = 0.5
    rsi_s_map = {'severe_oversold':0.95,'oversold':0.80,'near_oversold':0.55,
                 'severe_overbought':0.90,'overbought':0.75,'near_overbought':0.50,'dead_zone':0.30}
    rsi_s = rsi_s_map.get(rsi_zone, 0.30)
    
    composite = 0.25*dir_s + 0.20*mom_s + 0.10*lag_s + 0.10*vol_s + 0.10*tte_s + 0.10*exec_s + 0.10*cross_s + 0.05*rsi_s
    
    if composite < 0.25: return None, composite, 'below_threshold'
    
    # Side selection with continuation prior (3:1)
    down_s = composite
    up_s = composite
    if direction == 'DOWN': down_s *= 1.3; up_s *= 0.4
    elif direction == 'UP': up_s *= 1.1; down_s *= 0.5
    if rsi_zone in ('severe_oversold','oversold','near_oversold'): down_s *= 1.2; up_s *= 0.7
    elif rsi_zone in ('severe_overbought','overbought','near_overbought'): up_s *= 1.15; down_s *= 0.8
    
    if down_s > up_s and down_s >= 0.25:
        tier = 'severe_oversold_down' if rsi_zone == 'severe_oversold' else ('oversold_down' if rsi_zone in ('oversold','near_oversold') else 'direction_down_cheap')
        return 'DOWN', composite, tier
    elif up_s > down_s and up_s >= 0.25:
        tier = 'severe_overbought_up' if rsi_zone == 'severe_overbought' else ('overbought_up' if rsi_zone in ('overbought','near_overbought') else 'direction_up_cheap')
        return 'UP', composite, tier
    return None, composite, 'no_side'


def run_simulation():
    print("=" * 70)
    print("V21 PMXT 2000-TRADE SIMULATION V4 — FAST VECTORIZED")
    print("=" * 70)
    
    files = sorted(PMXT_DIR.glob("*.parquet"))
    valid_files = []
    for f in files:
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000:
                valid_files.append(f)
        except: continue
    print(f"Valid files: {len(valid_files)}/{len(files)}")
    if not valid_files:
        print("ERROR: No valid PMXT files!")
        return
    
    bankroll = BANKROLL_START
    trades = []
    tier_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    side_stats = {'UP': {'trades': 0, 'wins': 0, 'pnl': 0.0}, 'DOWN': {'trades': 0, 'wins': 0, 'pnl': 0.0}}
    score_dist = []
    rsi_dist = []
    entry_price_dist = []
    global_rejections = 0
    global_partials = 0
    
    start_time = time.time()
    
    for fidx, fpath in enumerate(valid_files):
        if len(trades) >= TARGET_TRADES:
            break
        
        print(f"\n[{fidx+1}/{len(valid_files)}] {fpath.name}...", end=" ", flush=True)
        fstart = time.time()
        
        pf = pq.ParquetFile(str(fpath))
        n_rgs = pf.metadata.num_row_groups
        
        # Phase 1: Across ALL RGs, accumulate per-token stats to find pairs
        # Use lightweight aggregation (sum, count, last_price) instead of full price series
        token_stats = {}  # (cid_hex, aid) -> [sum_price, count, last_price, min_price, max_price, prices_20]
        # We'll store sampled prices (every 20th point) for RSI/signal
        token_sampled = defaultdict(list)  # (cid_hex, aid) -> [(price, rg_idx)]
        
        for rg_idx in range(0, n_rgs, 3):  # Sample every 3rd RG for speed
            try:
                t = pf.read_row_group(rg_idx, columns=['market', 'asset_id', 'price', 'event_type'])
            except: continue
            
            # Filter price_change
            try:
                mask_arr = pc.equal(t.column('event_type'), 'price_change')
                t2 = t.filter(mask_arr)
            except:
                evs = t.column('event_type').to_pylist()
                keep = [i for i, e in enumerate(evs) if e == 'price_change']
                t2 = t.take(keep) if keep else None
            
            if t2 is None or len(t2) == 0:
                del t
                if t2: del t2
                continue
            
            n = len(t2)
            mkt_col = t2.column('market')
            aid_col = t2.column('asset_id')
            price_col_np = t2.column('price').to_numpy().astype(np.float64)
            
            # Sample every 10th row for efficiency
            step = max(1, n // 5000)
            
            for i in range(0, n, step):
                p = float(price_col_np[i])
                if p < 0.01 or p > 0.99:
                    continue
                
                mv = mkt_col[i]
                cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
                aid = str(aid_col[i])
                
                key = (cid, aid)
                if key not in token_stats:
                    token_stats[key] = [0.0, 0, p, p, p]  # sum, count, last, min, max
                token_stats[key][0] += p  # sum
                token_stats[key][1] += 1   # count
                token_stats[key][2] = p    # last
                token_stats[key][3] = min(token_stats[key][3], p)  # min
                token_stats[key][4] = max(token_stats[key][4], p)  # max
                
                # Store sampled price for RSI
                if len(token_sampled[key]) < 200:
                    token_sampled[key].append(p)
            
            del t, t2
            gc.collect()
        
        # Phase 2: Find binary pairs (cid with exactly 2 assets)
        cid_aids = defaultdict(set)
        for (cid, aid) in token_stats:
            cid_aids[cid].add(aid)
        
        pairs = [(cid, list(aids)) for cid, aids in cid_aids.items() if len(aids) == 2]
        print(f"tokens={len(token_stats)} pairs={len(pairs)}", end=" ", flush=True)
        
        # Phase 3: For each pair, determine cheap/rich, settle, and generate signals
        file_trades_before = len(trades)
        
        for cid, aid_list in pairs:
            if len(trades) >= TARGET_TRADES:
                break
            
            key1 = (cid, aid_list[0])
            key2 = (cid, aid_list[1])
            
            if key1 not in token_stats or key2 not in token_stats:
                continue
            
            s1 = token_stats[key1]
            s2 = token_stats[key2]
            
            # Need enough observations
            if s1[1] < 30 or s2[1] < 30:
                continue
            
            mean1 = s1[0] / s1[1]
            mean2 = s2[0] / s2[1]
            
            # Cheap token is the one with lower mean price
            if mean1 < mean2:
                cheap_key, cheap_mean = key1, mean1
                rich_key, rich_mean = key2, mean2
                cheap_sampled = token_sampled.get(key1, [])
                rich_sampled = token_sampled.get(key2, [])
                cheap_last = s1[2]
                rich_last = s2[2]
                cheap_min, cheap_max = s1[3], s1[4]
            else:
                cheap_key, cheap_mean = key2, mean2
                rich_key, rich_mean = key1, mean1
                cheap_sampled = token_sampled.get(key2, [])
                rich_sampled = token_sampled.get(key1, [])
                cheap_last = s2[2]
                rich_last = s1[2]
                cheap_min, cheap_max = s2[3], s2[4]
            
            # Only process cheap tokens
            if cheap_mean > 0.55:
                continue
            
            # Need enough price range for signal generation
            if cheap_max - cheap_min < 0.02:
                continue
            
            # Need enough sampled prices for RSI
            if len(cheap_sampled) < 24:
                continue
            
            # BINARY SETTLEMENT: rich_last > cheap_last → rich won → cheap lost
            if rich_last > cheap_last:
                settlement = 0.0  # cheap token lost
                won = False
            else:
                settlement = 1.0  # cheap token won
                won = True
            
            # Generate signals from sampled price series
            cheap_arr = np.array(cheap_sampled)
            rsi_arr = compute_rsi(cheap_arr)
            n_pts = len(cheap_arr)
            
            # Sample ~5 signal points
            step = max(1, n_pts // 5)
            
            for i in range(24, n_pts, step):
                if len(trades) >= TARGET_TRADES:
                    break
                
                current_price = float(cheap_arr[i])
                current_rsi = float(rsi_arr[i])
                
                if current_price < 0.03 or current_price > 0.55:
                    continue
                
                time_pct = i / max(n_pts, 1)
                direction, velocity = detect_direction(cheap_arr[:i+1])
                regime = get_regime(cheap_arr[:i+1])
                
                side, composite, tier = classify_signal_v21(
                    current_rsi, direction, velocity, regime, time_pct, current_price
                )
                
                if side is None:
                    continue
                
                tc = TIER_CONFIG.get(tier, TIER_CONFIG['direction_down_cheap'])
                if current_price > tc['max_price']:
                    continue
                
                # Position sizing
                if bankroll <= 2.0:
                    continue
                position_usd = min(tc['size_pct'] * bankroll, MAX_POSITION_USD)
                position_usd = max(position_usd, MIN_TRADE_SIZE)
                
                # Execution friction
                eff_price = current_price + SPREAD_COST + current_price * SLIPPAGE_PCT
                eff_price = min(eff_price, 0.99)
                if eff_price >= 1.0:
                    continue
                shares = position_usd / eff_price
                
                # Fill simulation
                roll = random.random()
                if roll < FILL_REJECTION_RATE:
                    global_rejections += 1
                    continue
                elif roll < FILL_REJECTION_RATE + PARTIAL_FILL_RATE:
                    fill_pct = 0.5 + random.random() * 0.3
                    shares *= fill_pct
                    position_usd *= fill_pct
                    global_partials += 1
                
                # Binary PnL
                pnl = shares * (settlement - eff_price)
                
                trades.append({
                    'tier': tier, 'side': side, 'entry': eff_price,
                    'shares': shares, 'size_usd': position_usd,
                    'settlement': settlement, 'pnl': pnl, 'won': won,
                    'composite': composite, 'rsi': current_rsi,
                    'velocity': velocity, 'direction': direction,
                    'regime': regime, 'time_pct': time_pct,
                    'cheap_mean': cheap_mean, 'rich_mean': rich_mean,
                })
                
                bankroll += pnl
                
                tier_stats[tier]['trades'] += 1
                if won: tier_stats[tier]['wins'] += 1
                tier_stats[tier]['pnl'] += pnl
                
                side_stats[side]['trades'] += 1
                if won: side_stats[side]['wins'] += 1
                side_stats[side]['pnl'] += pnl
                
                score_dist.append(composite)
                rsi_dist.append(current_rsi)
                entry_price_dist.append(eff_price)
        
        file_elapsed = time.time() - fstart
        file_new = len(trades) - file_trades_before
        wr = sum(1 for t in trades[-file_new:] if t['won']) / max(file_new, 1) * 100 if file_new > 0 else 0
        print(f"new={file_new}, total={len(trades)}, WR={wr:.1f}%, bank=${bankroll:.2f} ({file_elapsed:.0f}s)")
        
        # Free memory
        del token_stats, token_sampled, cid_aids, pairs
        gc.collect()
        
        if len(trades) >= TARGET_TRADES:
            break
    
    # Trim
    trades = trades[:TARGET_TRADES]
    
    # ════════════════════════════════════════════════════════════
    # RESULTS
    # ════════════════════════════════════════════════════════════
    
    total_elapsed = time.time() - start_time
    n = len(trades)
    total_wins = sum(1 for t in trades if t['won'])
    total_pnl = sum(t['pnl'] for t in trades)
    
    print("\n" + "=" * 70)
    print("V21 PMXT 2000-TRADE SIMULATION V4 RESULTS")
    print("=" * 70)
    
    if n == 0:
        print("NO TRADES GENERATED")
        return
    
    wr = total_wins / n * 100
    gp = sum(t['pnl'] for t in trades if t['pnl'] > 0)
    gl = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
    pf = gp / max(gl, 0.01)
    pnls = [t['pnl'] for t in trades]
    mu = np.mean(pnls); sd = np.std(pnls)
    sharpe = mu / max(sd, 0.001) * np.sqrt(252)
    
    cur = 0; best_w = 0; best_l = 0
    for t in trades:
        if t['won']: cur = max(1, cur+1) if cur > 0 else 1; best_w = max(best_w, cur)
        else: cur = min(-1, cur-1) if cur < 0 else -1; best_l = max(best_l, abs(cur))
    
    print(f"\n{'METRIC':<30s} {'VALUE':>15s}")
    print("-" * 47)
    print(f"{'Total trades':<30s} {n:>15d}")
    print(f"{'Wins / Losses':<30s} {total_wins:>7d} / {n-total_wins:<7d}")
    print(f"{'Win rate':<30s} {wr:>14.1f}%")
    print(f"{'Final bankroll':<30s} ${bankroll:>14.2f}")
    print(f"{'Total P&L':<30s} ${total_pnl:>14.2f}")
    print(f"{'ROI':<30s} {(bankroll/BANKROLL_START-1)*100:>14.1f}%")
    print(f"{'Avg trade P&L':<30s} ${mu:>14.4f}")
    print(f"{'Profit factor':<30s} {pf:>15.2f}")
    print(f"{'Sharpe (approx)':<30s} {sharpe:>15.2f}")
    print(f"{'Max win streak':<30s} {best_w:>15d}")
    print(f"{'Max loss streak':<30s} {best_l:>15d}")
    print(f"{'Fill rejections':<30s} {global_rejections:>15d}")
    print(f"{'Partial fills':<30s} {global_partials:>15d}")
    
    print(f"\n{'SIDE BREAKDOWN':}")
    for side in ['UP', 'DOWN']:
        s = side_stats[side]
        if s['trades'] > 0:
            swr = s['wins'] / s['trades'] * 100
            print(f"  {side:<10s}: {s['trades']:>5d} trades, {swr:>5.1f}% WR, ${s['pnl']:>10.2f} P&L")
    
    print(f"\n{'TIER BREAKDOWN':}")
    for tier in ['severe_oversold_down','severe_overbought_up','oversold_down',
                  'overbought_up','direction_down_cheap','direction_up_cheap']:
        s = tier_stats[tier]
        if s['trades'] > 0:
            twr = s['wins'] / s['trades'] * 100
            avg = s['pnl'] / s['trades']
            print(f"  {tier:<30s}: {s['trades']:>5d} trades, {twr:>5.1f}% WR, ${s['pnl']:>8.2f} P&L, ${avg:.4f} avg")
    
    # Settlement
    won_n = sum(1 for t in trades if t['won'])
    print(f"\n{'BINARY SETTLEMENT':}")
    print(f"  Won (settlement=1.0): {won_n} ({won_n/n*100:.1f}%)")
    print(f"  Lost (settlement=0.0): {n-won_n} ({(n-won_n)/n*100:.1f}%)")
    
    # Entry price buckets
    if entry_price_dist:
        ep = np.array(entry_price_dist)
        print(f"\n{'WR BY ENTRY PRICE':}")
        for lo, hi, label in [(0.03,0.08,'<8¢'),(0.08,0.12,'8-12¢'),(0.12,0.20,'12-20¢'),(0.20,0.35,'20-35¢'),(0.35,0.55,'35-55¢')]:
            bucket = [t for t in trades if lo <= t['entry'] < hi]
            if bucket:
                bwr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
                bpnl = sum(t['pnl'] for t in bucket)
                print(f"  {label:<10s}: {len(bucket):>5d} trades, {bwr:>5.1f}% WR, ${bpnl:.2f} P&L")
    
    # RSI buckets
    if rsi_dist:
        rr = np.array(rsi_dist)
        print(f"\n{'WR BY RSI ZONE':}")
        for lo, hi, label in [(0,25,'SevOversold'),(25,35,'Oversold'),(35,65,'DeadZone'),(65,73,'Overbought'),(73,100,'SevOB')]:
            bucket = [t for t in trades if lo <= t['rsi'] < hi]
            if bucket:
                bwr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
                bpnl = sum(t['pnl'] for t in bucket)
                print(f"  {label:<15s}: {len(bucket):>5d} trades, {bwr:>5.1f}% WR, ${bpnl:.2f} P&L")
    
    # Regime distribution
    regimes = defaultdict(int)
    for t in trades: regimes[t['regime']] += 1
    print(f"\n{'REGIME DISTRIBUTION':}")
    for regime, count in sorted(regimes.items(), key=lambda x: -x[1]):
        print(f"  {regime}: {count} ({count/n*100:.1f}%)")
    
    # Monte Carlo
    print(f"\n{'MONTE CARLO (1000 sims)':}")
    mc_profits = 0; mc_finals = []; mc_dds = []
    for _ in range(1000):
        idx = np.random.choice(n, size=n, replace=True)
        sim_b = BANKROLL_START; sim_pk = BANKROLL_START; sim_dd = 0
        for i in idx:
            sim_b += pnls[i]
            sim_pk = max(sim_pk, sim_b)
            sim_dd = max(sim_dd, (sim_pk - sim_b) / max(sim_pk, 1))
        mc_finals.append(sim_b)
        mc_dds.append(sim_dd * 100)
        if sim_b > BANKROLL_START: mc_profits += 1
    
    print(f"  Profitable: {mc_profits/1000*100:.1f}%")
    print(f"  Mean final: ${np.mean(mc_finals):.2f}, Median: ${np.median(mc_finals):.2f}")
    print(f"  Mean max DD: {np.mean(mc_dds):.1f}%")
    p5 = np.percentile([b-BANKROLL_START for b in mc_finals], 5)
    p95 = np.percentile([b-BANKROLL_START for b in mc_finals], 95)
    print(f"  P&L 5th: ${p5:.2f}, 95th: ${p95:.2f}")
    print(f"  Bust rate: {sum(1 for b in mc_finals if b <= 0)/10:.1f}%")
    
    print(f"\n  Elapsed: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")
    
    # Save
    out = Path("/home/naq1987s/father-daddy-capital/output")
    out.mkdir(exist_ok=True)
    results = {
        'version': 'V21_PMXT_v4_fast', 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'config': {'bankroll': BANKROLL_START, 'max_pos': MAX_POSITION_USD,
                   'spread': SPREAD_COST, 'slippage': SLIPPAGE_PCT, 'binary_settlement': True},
        'summary': {'trades': n, 'wins': total_wins, 'wr': wr, 'bankroll': bankroll,
                    'pnl': total_pnl, 'roi': (bankroll/BANKROLL_START-1)*100,
                    'pf': pf, 'sharpe': sharpe},
        'tier_stats': {k: dict(v) for k, v in tier_stats.items()},
        'side_stats': side_stats,
    }
    with open(out / "v21_pmxt_2000_trade_sim_v4.json", 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nSaved to {out / 'v21_pmxt_2000_trade_sim_v4.json'}")


if __name__ == '__main__':
    np.random.seed(42)
    random.seed(42)
    run_simulation()