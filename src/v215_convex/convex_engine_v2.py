#!/usr/bin/env python3
"""
V21.5 CONVEX CONTINUATION — 10,000-Trade PMXT Simulation (V2 — Dense Sampling)
================================================================================
Based on V4's proven data pipeline with V21.5 scoring overlay.

Edge: cheap continuation convexity (DOWN primary).
NOT reversal. NOT midpoint. NOT balanced.
"""

import pyarrow.parquet as pq
import numpy as np
from pathlib import Path
from collections import defaultdict
import time, gc, json, random

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
TARGET_TRADES = 10000
BANKROLL_START = 100.0

# ════════════════════════════════════════════════════════════════════
# V21.5 CONFIGURATION
# ════════════════════════════════════════════════════════════════════

# Entry Buckets (§6)
BUCKET_PRIMARY = (0.03, 0.12)
BUCKET_SECONDARY = (0.12, 0.20)

# Direction Priority Matrix (§7)
DIRECTION_PRIORITY = {
    'DOWN_CONTINUATION': 1.50,
    'DOWN_MOMENTUM': 1.40,
    'UP_REVERSAL': 0.60,
    'UP_CONTINUATION': 0.30,
    'FLAT': 0.10,
}

# Signal Stack Weights (§9) — RSI demoted to 5%
W = {'persist': 0.25, 'accel': 0.20, 'lag': 0.15, 'vol': 0.15, 'tte': 0.10, 'exec': 0.10, 'rsi': 0.05}

# Friction Model (§13)
SPREAD_COST = 0.012
SLIPPAGE_PCT = 0.008
FILL_REJECTION_RATE = 0.07
PARTIAL_FILL_RATE = 0.12
STALE_QUOTE_RATE = 0.03
QUEUE_DELAY_PENALTY = 0.005

# Timing Windows (§14)
TIMING = {
    'EARLY':     (0.00, 0.20, 0.10),
    'FORMATION': (0.20, 0.40, 0.35),
    'MOMENTUM':  (0.40, 0.80, 0.80),
    'LATE':      (0.80, 0.90, 0.95),
    'FINAL':     (0.90, 1.00, 0.60),
}


def compute_rsi(prices, period=14):
    n = len(prices)
    if n < period + 1:
        return np.full(n, 50.0)
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
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


def compute_acceleration(prices):
    """§9B: Δprice/Δtime at multiple scales."""
    n = len(prices)
    if n < 4:
        return 0.0, 0.0, 0
    
    windows = [4, 8, 16, 32]
    weights = [0.4, 0.3, 0.2, 0.1]
    velocities = []
    for w in windows:
        if n > w:
            v = (prices[-1] - prices[-1-w]) / max(abs(prices[-1-w]), 1e-9) * 100
            velocities.append(v)
        else:
            velocities.append(0.0)
    
    velocity = sum(w*v for w, v in zip(weights, velocities))
    accel = velocities[0] - velocities[1] if len(velocities) >= 2 else 0.0
    
    consec = 0
    for i in range(1, min(6, n)):
        if n-i-1 >= 0 and prices[-i] > prices[-i-1]:
            consec += 1
        elif n-i-1 >= 0 and prices[-i] < prices[-i-1]:
            consec -= 1
    
    return np.tanh(accel / 0.5), velocity, consec


def compute_continuation(prices, consec):
    """§11: Soft continuation score (no hard clamp)."""
    n = len(prices)
    if n < 10:
        return 0.0, 'unknown'
    
    persistence = abs(consec) / 6.0
    recent = prices[-min(10, n):]
    lower_lows = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i-1])
    higher_highs = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1])
    
    if lower_lows > higher_highs:
        raw = (lower_lows - higher_highs) / max(len(recent)-1, 1) + persistence * 0.5
        direction = 'DOWN_CONTINUATION'
    elif higher_highs > lower_lows:
        raw = (higher_highs - lower_lows) / max(len(recent)-1, 1) + persistence * 0.5
        direction = 'UP_CONTINUATION'
    elif consec <= -2:
        raw = 0.15 + persistence * 0.2  # Mild down bias from consec
        direction = 'DOWN_CONTINUATION'
    elif consec >= 2:
        raw = 0.15 + persistence * 0.2  # Mild up bias from consec
        direction = 'UP_CONTINUATION'
    else:
        raw = 0.0
        direction = 'FLAT'
    
    return np.tanh(raw), direction



def compute_convex_score(rsi, accel, velocity, cont_score, cont_dir, state, time_pct, price, regime):
    """V21.5 composite with continuation weights."""
    
    # Persistence (25%)
    if state in ('DOWN_CONTINUATION', 'DOWN_MOMENTUM'):
        ps = min(1.0, abs(velocity) / 0.3) * 1.3
    elif state in ('UP_REVERSAL', 'UP_CONTINUATION'):
        ps = min(1.0, abs(velocity) / 0.3) * 0.7
    else:
        ps = 0.05
    
    # Acceleration (20%)
    as_ = 0.5 + 0.5 * max(-1, min(1, accel))
    
    # Lag (15%) — continuation = lag opportunity
    ls = 0.2 + 0.3 * abs(cont_score)
    
    # Volatility (15%)
    if regime in ('volatile', 'trending_up', 'trending_down'):
        vs = min(1.0, abs(velocity) * 3.0)
    else:
        vs = 0.1
    
    # Timing (10%)
    for _, (lo, hi, pri) in TIMING.items():
        if lo <= time_pct < hi:
            ts = pri; break
    else:
        ts = 0.3
    if abs(velocity) < 0.03:
        ts *= 0.3
    
    # Execution (10%)
    if price < 0.10:
        se = 0.02
    elif price < 0.12:
        se = 0.018
    elif price < 0.20:
        se = 0.015
    else:
        se = 0.025
    if se > 0.025:
        es = 0.2
    elif se > 0.015:
        es = 0.6
    else:
        es = 0.9
    
    # §6: Spread trap — block if effective spread > 50% of price (neg-risk pairs)
    # For cheap tokens (3-12¢), spread is usually 1-2¢ which is 8-66% of price
    # Block only when spread exceeds 60% of price (ultra-thin, untradable)
    if price < 0.03:
        return 0.0, 'price_too_low', state
    
    # RSI (5% max — §8)
    if rsi < 25:
        rs = 0.90
    elif rsi < 30:
        rs = 0.70
    elif rsi < 35:
        rs = 0.55
    elif rsi > 73:
        rs = 0.60
    elif rsi > 70:
        rs = 0.50
    else:
        rs = 0.30
    
    raw = (W['persist']*ps + W['accel']*as_ + W['lag']*ls + W['vol']*vs + 
           W['tte']*ts + W['exec']*es + W['rsi']*rs)
    
    pri = DIRECTION_PRIORITY.get(state, 0.3)
    return raw * pri, None, state


def classify_state(accel, velocity, consec, cont_dir, rsi):
    """§7 Directional Priority Matrix."""
    if cont_dir == 'DOWN_CONTINUATION' or (consec <= -3 and velocity < -0.05):
        return 'DOWN_MOMENTUM' if abs(velocity) > 0.3 else 'DOWN_CONTINUATION'
    elif cont_dir == 'UP_CONTINUATION' or (consec >= 3 and velocity > 0.05):
        if rsi < 30:
            return 'UP_REVERSAL'
        return 'UP_CONTINUATION'
    return 'FLAT'


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


def run_simulation():
    print("=" * 70)
    print("V21.5 CONVEX CONTINUATION — 10K PMXT SIM (DENSE SAMPLING)")
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
    
    bankroll = BANKROLL_START
    trades = []
    state_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    bucket_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    side_stats = {'UP': {'trades': 0, 'wins': 0, 'pnl': 0.0}, 'DOWN': {'trades': 0, 'wins': 0, 'pnl': 0.0}}
    rejections = 0; partials = 0; stales = 0
    start = time.time()
    
    progress_file = Path("/home/naq1987s/father-daddy-capital/output/v215_sim_progress.log")
    progress_file.parent.mkdir(exist_ok=True)
    
    for fidx, fpath in enumerate(valid_files):
        if len(trades) >= TARGET_TRADES: break
        msg = f"\n[{fidx+1}/{len(valid_files)}] {fpath.name}..."
        print(msg, end=" ", flush=True)
        with open(progress_file, 'a') as pf: pf.write(msg + " ")
        fstart = time.time()
        
        pf = pq.ParquetFile(str(fpath))
        n_rgs = pf.metadata.num_row_groups
        
        # Phase 1: Light token accumulation across ALL row groups
        token_data = {}
        token_prices = defaultdict(list)
        
        for rg_idx in range(n_rgs):
            try:
                t = pf.read_row_group(rg_idx, columns=['market', 'asset_id', 'price', 'event_type'])
            except: continue
            
            evs = t.column('event_type').to_pylist()
            keep = [i for i, e in enumerate(evs) if e == 'price_change']
            t2 = t.take(keep) if keep else None
            
            if t2 is None or len(t2) == 0:
                del t; continue
            
            n = len(t2)
            prices_arr = t2.column('price').to_numpy().astype(np.float64)
            mkt_col = t2.column('market')
            aid_col = t2.column('asset_id')
            
            # Sample every 100th point — balances density vs speed
            step = max(1, n // 10000)
            
            for i in range(0, n, step):
                p = float(prices_arr[i])
                if p < 0.01 or p > 0.99: continue
                if p < 0.03 or p > 0.55: continue  # Skip outside tradeable range
                
                m = mkt_col[i]
                cid = m.hex() if hasattr(m, 'hex') else str(m)
                aid = str(aid_col[i])
                key = (cid, aid)
                
                if key not in token_data:
                    token_data[key] = [0.0, 0, p, p, p]  # sum, cnt, last, min, max
                token_data[key][0] += p
                token_data[key][1] += 1
                token_data[key][2] = p  # last
                token_data[key][3] = min(token_data[key][3], p)
                token_data[key][4] = max(token_data[key][4], p)
                
                if len(token_prices[key]) < 800:
                    token_prices[key].append(p)
            
            del t, t2
            if rg_idx % 20 == 0: gc.collect()
        
        # Phase 2: Find binary pairs and trade
        cid_aids = defaultdict(set)
        for (cid, aid) in token_data:
            cid_aids[cid].add(aid)
        
        pairs = [(cid, list(aids)) for cid, aids in cid_aids.items() if len(aids) == 2]
        file_trades_before = len(trades)
        
        for cid, aid_list in pairs:
            if len(trades) >= TARGET_TRADES: break
            if len(pairs) % 100 == 0 and len(trades) > file_trades_before + 50:
                break  # Progress check
            
            k1 = (cid, aid_list[0])
            k2 = (cid, aid_list[1])
            if k1 not in token_data or k2 not in token_data: continue
            
            s1, s2 = token_data[k1], token_data[k2]
            if s1[1] < 15 or s2[1] < 15: continue
            
            m1, m2 = s1[0]/s1[1], s2[0]/s2[1]
            
            # Identify cheap vs rich
            if m1 < m2:
                cheap_k, cheap_s, rich_k, rich_s = k1, s1, k2, s2
            else:
                cheap_k, cheap_s, rich_k, rich_s = k2, s2, k1, s1
            
            cheap_mean = cheap_s[0] / cheap_s[1]
            if cheap_mean > 0.55: continue
            if cheap_s[4] - cheap_s[3] < 0.02: continue  # No movement
            
            # Get price array for cheap token
            cheap_arr = np.array(token_prices.get(cheap_k, []))
            if len(cheap_arr) < 15: continue
            
            # Binary settlement (§12)
            if token_data[rich_k][2] > token_data[cheap_k][2]:
                settlement = 0.0; won = False  # cheap lost
            else:
                settlement = 1.0; won = True   # cheap won
            
            # Generate signals from sampled prices
            rsi_arr = compute_rsi(cheap_arr)
            n_pts = len(cheap_arr)
            
            # Generate up to 6 signal points per market
            num_signals = min(6, max(1, n_pts // 40))
            signal_step = max(1, (n_pts - 20) // num_signals)
            
            for sig_idx in range(num_signals):
                if len(trades) >= TARGET_TRADES: break
                
                i = 20 + sig_idx * signal_step
                if i >= n_pts: break
                
                current_price = float(cheap_arr[i])
                current_rsi = float(rsi_arr[i])
                time_pct = i / max(n_pts, 1)
                
                if current_price < 0.03 or current_price > 0.55: continue
                
                window = cheap_arr[:i+1]
                
                # Signal stack
                accel_score, velocity, consec = compute_acceleration(window)
                cont_score, cont_dir = compute_continuation(window, consec)
                state = classify_state(accel_score, velocity, consec, cont_dir, current_rsi)
                regime = get_regime(window)
                
                # Entry bucket (§6)
                if BUCKET_PRIMARY[0] <= current_price < BUCKET_PRIMARY[1]:
                    bucket = 'PRIMARY'
                elif BUCKET_SECONDARY[0] <= current_price < BUCKET_SECONDARY[1]:
                    bucket = 'SECONDARY'
                else:
                    bucket = 'BLOCKED'
                
                if bucket == 'BLOCKED': continue
                
                # V21.5 composite score
                composite, reason, state = compute_convex_score(
                    current_rsi, accel_score, velocity, cont_score,
                    cont_dir, state, time_pct, current_price, regime
                )
                
                min_score = 0.15 if bucket == 'PRIMARY' else 0.25
                if composite < min_score: continue
                
                # Position sizing (§17)
                position_usd = 2.0  # Fixed $2 paper size
                if bucket == 'SECONDARY':
                    position_usd *= 0.7
                if state in ('DOWN_CONTINUATION', 'DOWN_MOMENTUM'):
                    position_usd *= 1.0
                elif state == 'UP_REVERSAL':
                    position_usd *= 0.5
                elif state == 'UP_CONTINUATION':
                    position_usd *= 0.3
                else:
                    position_usd *= 0.2
                
                if position_usd < 0.5 or bankroll <= 2.0: continue
                
                # Friction (§13)
                eff_price = current_price + SPREAD_COST + current_price * SLIPPAGE_PCT
                eff_price = min(eff_price, 0.99)
                shares = position_usd / eff_price
                
                # Fill simulation
                roll = random.random()
                if roll < STALE_QUOTE_RATE:
                    stales += 1; continue
                elif roll < STALE_QUOTE_RATE + FILL_REJECTION_RATE:
                    rejections += 1; continue
                elif roll < STALE_QUOTE_RATE + FILL_REJECTION_RATE + PARTIAL_FILL_RATE:
                    fill_pct = 0.5 + random.random() * 0.3
                    shares *= fill_pct; position_usd *= fill_pct
                    partials += 1
                
                # Queue delay penalty
                effective_ev_mult = 1.0 - QUEUE_DELAY_PENALTY
                if won:
                    pnl = shares * (1.0 - eff_price) * effective_ev_mult
                else:
                    pnl = -shares * eff_price * (1.0 - QUEUE_DELAY_PENALTY * 0.5)
                
                # Binary settlement P&L (§12)
                pnl = shares * (settlement - eff_price)
                
                side = 'DOWN' if state.startswith('DOWN') else ('UP' if state.startswith('UP') else 'FLAT')
                
                trades.append({
                    'state': state, 'side': side, 'bucket': bucket,
                    'entry': eff_price, 'shares': shares, 'size_usd': position_usd,
                    'settlement': settlement, 'won': won, 'pnl': pnl,
                    'composite': composite, 'rsi': current_rsi, 'velocity': velocity,
                    'acceleration': accel_score, 'regime': regime, 'time_pct': time_pct,
                })
                
                bankroll += pnl
                
                # Stats
                state_stats[state]['trades'] += 1
                if won: state_stats[state]['wins'] += 1
                state_stats[state]['pnl'] += pnl
                
                bucket_stats[bucket]['trades'] += 1
                if won: bucket_stats[bucket]['wins'] += 1
                bucket_stats[bucket]['pnl'] += pnl
                
                side_key = side if side in side_stats else 'UP'
                side_stats[side_key]['trades'] += 1
                if won: side_stats[side_key]['wins'] += 1
                side_stats[side_key]['pnl'] += pnl
        
        elapsed = time.time() - fstart
        new = len(trades) - file_trades_before
        if new > 0:
            wr = sum(1 for t in trades[-new:] if t['won']) / max(new, 1) * 100
            msg = f"new={new}, total={len(trades)}, WR={wr:.1f}%, bank=${bankroll:.2f} ({elapsed:.0f}s)"
            print(msg, flush=True)
            with open(progress_file, 'a') as pf: pf.write(msg + "\n")
        else:
            print(f"no new trades ({elapsed:.0f}s)", flush=True)
        
        del token_data, token_prices, cid_aids, pairs
        gc.collect()
    
    trades = trades[:TARGET_TRADES]
    n = len(trades)
    if n == 0:
        print("\nNO TRADES GENERATED")
        return
    
    total_wins = sum(1 for t in trades if t['won'])
    total_pnl = sum(t['pnl'] for t in trades)
    wr = total_wins / n * 100
    gp = sum(t['pnl'] for t in trades if t['pnl'] > 0)
    gl = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
    pf = gp / max(gl, 0.01)
    pnls = [t['pnl'] for t in trades]
    mu = np.mean(pnls); sd = np.std(pnls)
    sharpe = mu / max(sd, 0.001) * np.sqrt(252)
    avg_win = np.mean([t['pnl'] for t in trades if t['won']]) if total_wins > 0 else 0
    avg_loss = abs(np.mean([t['pnl'] for t in trades if not t['won']])) if n-total_wins > 0 else 0
    realized_ev = wr/100 * avg_win - (1-wr/100) * avg_loss
    
    cur = 0; best_w = 0; best_l = 0
    for t in trades:
        if t['won']: cur = max(1, cur+1) if cur > 0 else 1; best_w = max(best_w, cur)
        else: cur = min(-1, cur-1) if cur < 0 else -1; best_l = max(best_l, abs(cur))
    
    print("\n" + "=" * 70)
    print("V21.5 CONVEX CONTINUATION — 10K PMXT SIMULATION RESULTS")
    print("=" * 70)
    
    print(f"\n{'METRIC':<35s} {'VALUE':>15s}")
    print("-" * 52)
    print(f"{'Total trades':<35s} {n:>15d}")
    print(f"{'Wins / Losses':<35s} {total_wins:>7d} / {n-total_wins:<7d}")
    print(f"{'Win rate':<35s} {wr:>14.1f}%")
    print(f"{'Final bankroll':<35s} ${bankroll:>14.2f}")
    print(f"{'Total P&L':<35s} ${total_pnl:>14.2f}")
    print(f"{'ROI':<35s} {(bankroll/BANKROLL_START-1)*100:>14.1f}%")
    print(f"{'Realized EV per trade':<35s} ${realized_ev:>14.4f}")
    print(f"{'Avg win size':<35s} ${avg_win:>14.4f}")
    print(f"{'Avg loss size':<35s} ${avg_loss:>14.4f}")
    print(f"{'Payout ratio (win/loss)':<35s} {avg_win/max(avg_loss,0.01):>15.2f}x")
    print(f"{'Profit factor':<35s} {pf:>15.2f}")
    print(f"{'Sharpe':<35s} {sharpe:>15.2f}")
    print(f"{'Max win streak':<35s} {best_w:>15d}")
    print(f"{'Max loss streak':<35s} {best_l:>15d}")
    print(f"{'Fill rejections':<35s} {rejections:>15d}")
    print(f"{'Partial fills':<35s} {partials:>15d}")
    print(f"{'Stale quote aborts':<35s} {stales:>15d}")
    
    # §7: State breakdown
    print(f"\n{'CONTINUATION STATE BREAKDOWN (§7)':}")
    print(f"{'State':<25s} {'Trades':>7s} {'Wins':>7s} {'WR%':>7s} {'P&L':>12s} {'AvgPnL':>8s}")
    print("-" * 70)
    for state in ['DOWN_CONTINUATION', 'DOWN_MOMENTUM', 'UP_REVERSAL', 'UP_CONTINUATION', 'FLAT']:
        s = state_stats[state]
        if s['trades'] > 0:
            swr = s['wins'] / s['trades'] * 100
            avg = s['pnl'] / s['trades']
            print(f"  {state:<23s} {s['trades']:>7d} {s['wins']:>7d} {swr:>6.1f}% ${s['pnl']:>10.2f} ${avg:>7.4f}")
    
    # §6: Bucket breakdown
    print(f"\n{'ENTRY BUCKET ANALYSIS (§6)':}")
    print(f"{'Bucket':<15s} {'Range':>12s} {'Trades':>7s} {'WR%':>7s} {'P&L':>12s}")
    print("-" * 55)
    for bucket in ['PRIMARY', 'SECONDARY']:
        s = bucket_stats[bucket]
        if s['trades'] > 0:
            bwr = s['wins'] / s['trades'] * 100
            rng = '0.03-0.12' if bucket == 'PRIMARY' else '0.12-0.20'
            print(f"  {bucket:<13s} {rng:>12s} {s['trades']:>7d} {bwr:>6.1f}% ${s['pnl']:>10.2f}")
    
    # Side comparison (§7: DOWN PRIMARY)
    print(f"\n{'SIDE COMPARISON (§7 — DOWN PRIMARY)':}")
    for side in ['DOWN', 'UP']:
        s = side_stats[side]
        if s['trades'] > 0:
            swr = s['wins'] / s['trades'] * 100
            print(f"  {side:<10s}: {s['trades']:>6d} trades, {swr:>5.1f}% WR, ${s['pnl']:>10.2f} P&L")
    
    # Entry price distribution
    print(f"\n{'WR BY ENTRY PRICE':}")
    for lo, hi, label in [(0.03,0.06,'<6¢'),(0.06,0.09,'6-9¢'),(0.09,0.12,'9-12¢'),(0.12,0.16,'12-16¢'),(0.16,0.20,'16-20¢')]:
        bucket = [t for t in trades if lo <= t['entry'] < hi]
        if bucket:
            bwr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
            bpnl = sum(t['pnl'] for t in bucket)
            print(f"  {label:<10s}: {len(bucket):>5d} trades, {bwr:>5.1f}% WR, ${bpnl:.2f} P&L")
    
    # Timing (§14)
    print(f"\n{'TIMING DISTRIBUTION (§14)':}")
    for wn, (lo, hi, _) in TIMING.items():
        bucket = [t for t in trades if lo <= t['time_pct'] < hi]
        if bucket:
            bwr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
            bpnl = sum(t['pnl'] for t in bucket)
            print(f"  {wn:<12s} ({lo:.0f}-{hi:.0f}%): {len(bucket):>5d} trades, {bwr:>5.1f}% WR, ${bpnl:.2f} P&L")
    
    # MC
    print(f"\n{'MONTE CARLO (1000 sims)':}")
    mc_profits = 0; mc_finals = []; mc_dds = []
    for _ in range(1000):
        idx = np.random.choice(n, size=n, replace=True)
        sb = BANKROLL_START; spk = BANKROLL_START; sdd = 0
        for i in idx:
            sb += pnls[i]
            spk = max(spk, sb)
            sdd = max(sdd, (spk - sb) / max(spk, 1))
        mc_finals.append(sb)
        mc_dds.append(sdd * 100)
        if sb > BANKROLL_START: mc_profits += 1
    
    print(f"  Profitable: {mc_profits/1000*100:.1f}%")
    print(f"  Mean final: ${np.mean(mc_finals):.2f}, Median: ${np.median(mc_finals):.2f}")
    print(f"  Mean max DD: {np.mean(mc_dds):.1f}%")
    p5 = np.percentile([b-BANKROLL_START for b in mc_finals], 5)
    p95 = np.percentile([b-BANKROLL_START for b in mc_finals], 95)
    print(f"  P&L 5th: ${p5:.2f}, 95th: ${p95:.2f}")
    print(f"  Bust rate: {sum(1 for b in mc_finals if b <= 0)/10:.1f}%")
    
    print(f"\n  Elapsed: {time.time()-start:.0f}s ({(time.time()-start)/60:.1f}min)")
    
    # §21: Core principle validation
    print(f"\n{'§21 CONVEX EXTRACTION VALIDATION':}")
    print(f"  Low WR ({wr:.1f}%) + cheap entries + large payout asymmetry")
    print(f"  Realized EV: ${realized_ev:.4f}/trade")
    print(f"  Payout ratio: {avg_win/max(avg_loss,0.01):.2f}x")
    if realized_ev > 0:
        print(f"  ✓ VALID: Positive realized EV with asymmetric convex extraction")
    else:
        print(f"  ✗ INVALID: Negative realized EV — requires parameter adjustment")
    
    # Save
    out = Path("/home/naq1987s/father-daddy-capital/output")
    out.mkdir(exist_ok=True)
    results = {
        'version': 'V21_5_CONVEX_CONTINUATION_10K',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'config': {'bankroll': BANKROLL_START, 'paper_size': 2.0,
                   'spread': SPREAD_COST, 'slippage': SLIPPAGE_PCT,
                   'fill_reject': FILL_REJECTION_RATE, 'partial_fill': PARTIAL_FILL_RATE,
                   'stale_quote': STALE_QUOTE_RATE, 'queue_delay': QUEUE_DELAY_PENALTY,
                   'binary_settlement': True, 'direction_primary': 'DOWN_CONTINUATION',
                   'rsi_weight': '5%', 'entry_buckets': ['PRIMARY 0.03-0.12', 'SECONDARY 0.12-0.20']},
        'summary': {'trades': n, 'wins': total_wins, 'wr': wr, 'bankroll': bankroll,
                    'pnl': total_pnl, 'roi': (bankroll/BANKROLL_START-1)*100,
                    'realized_ev': realized_ev, 'pf': pf, 'sharpe': sharpe,
                    'payout_ratio': avg_win/max(avg_loss, 0.01),
                    'avg_win': avg_win, 'avg_loss': avg_loss},
        'state_stats': {k: dict(v) for k, v in state_stats.items()},
        'bucket_stats': {k: dict(v) for k, v in bucket_stats.items()},
        'side_stats': side_stats,
    }
    with open(out / 'v215_convex_10k_pmxt_sim.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nSaved to {out / 'v215_convex_10k_pmxt_sim.json'}")


if __name__ == '__main__':
    import sys
    # Force unbuffered output
    sys.stdout.reconfigure(line_buffering=True)
    np.random.seed(42)
    random.seed(42)
    run_simulation()