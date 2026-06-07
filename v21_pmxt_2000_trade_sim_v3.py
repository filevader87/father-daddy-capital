#!/usr/bin/env python3
"""
V21 PMXT 2000-Trade Simulation V3 — MEMORY EFFICIENT
======================================================
Streams PMXT data per row group. No accumulation of all prices in memory.
For each binary pair detected in a RG, generates signals + settles using 
the pair's final price WITHIN that RG (not across the whole file).

Binary settlement: compare cheap vs rich token in same condition_id.
Rich final > cheap final → rich won → cheap lost → settlement = 0.0
Rich final < cheap final → rich lost → cheap won → settlement = 1.0

Memory: ~200-400MB per file, GC between files.
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
MAX_DAILY_TRADES = 100
DAILY_LOSS_LIMIT = 10.0

TIER_CONFIG = {
    'severe_oversold_down':  {'max_price': 0.50, 'size_pct': 0.10, 'base_wr': 0.80},
    'severe_overbought_up':  {'max_price': 0.50, 'size_pct': 0.10, 'base_wr': 0.87},
    'oversold_down':         {'max_price': 0.20, 'size_pct': 0.06, 'base_wr': 0.74},
    'overbought_up':         {'max_price': 0.20, 'size_pct': 0.05, 'base_wr': 0.71},
    'direction_down_cheap':  {'max_price': 0.15, 'size_pct': 0.03, 'base_wr': 0.68},
    'direction_up_cheap':    {'max_price': 0.15, 'size_pct': 0.03, 'base_wr': 0.70},
}

SPREAD_COST = 0.01
SLIPPAGE_PCT = 0.005
FILL_REJECTION_RATE = 0.05
PARTIAL_FILL_RATE = 0.10
CONTINUATION_PRIOR = 3.0

W_DIR=0.25; W_MOM=0.20; W_LAG=0.10; W_VOL=0.10; W_TTE=0.10; W_EXEC=0.10; W_CROSS=0.10; W_RSI=0.05


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


def detect_direction(prices):
    n = len(prices)
    if n < 4:
        return 'FLAT', 0.0
    
    # Velocity at multiple scales
    v_short = (prices[-1] - prices[-4]) / max(abs(prices[-4]), 1e-9) * 100
    v_med = (prices[-1] - prices[max(-8, -n)]) / max(abs(prices[max(-8, -n)]), 1e-9) * 100 if n >= 8 else abs(v_short)
    velocity = 0.5 * v_short + 0.3 * v_med + 0.2 * v_short  # simplified blend
    
    # Consecutive direction
    consec = 0
    for i in range(1, min(5, n)):
        if n-i-1 >= 0 and prices[-i] > prices[-i-1]: consec += 1
        elif n-i-1 >= 0 and prices[-i] < prices[-i-1]: consec -= 1
    
    persistence = 1.0 if consec > 1 else (-1.0 if consec < -1 else 0.0)
    blended = 0.4 * np.sign(v_short) * min(abs(v_short), 1.0) + 0.6 * persistence
    
    if blended > 0.3 or (consec >= 3 and v_short > 0.05):
        return 'UP', abs(velocity)
    elif blended < -0.3 or (consec <= -3 and v_short < -0.05):
        return 'DOWN', abs(velocity)
    return 'FLAT', abs(velocity)


def get_regime(prices):
    if len(prices) < 20:
        return 'unknown'
    recent = prices[-20:]
    volatility = np.std(recent) / max(abs(np.mean(recent)), 1e-9)
    trend = (recent[-1] - recent[0]) / max(abs(recent[0]), 1e-9)
    if volatility < 0.01: return 'ranging'
    if volatility > 0.05: return 'volatile'
    if trend > 0.02: return 'trending_up'
    if trend < -0.02: return 'trending_down'
    return 'balanced'


def get_rsi_zone(rsi):
    if rsi < 25: return 'severe_oversold'
    if rsi < 30: return 'oversold'
    if rsi < 35: return 'near_oversold'
    if rsi > 73: return 'severe_overbought'
    if rsi > 70: return 'overbought'
    if rsi > 65: return 'near_overbought'
    return 'dead_zone'


def classify_signal(rsi, direction, velocity, regime, time_pct, spread, price):
    rsi_zone = get_rsi_zone(rsi)
    
    if spread > 0.03:
        return None, 0.0, 'spread_trap'
    
    # Scoring
    if direction == 'DOWN':
        dir_score = min(1.0, abs(velocity) / 0.5) * 0.8
    elif direction == 'UP':
        dir_score = min(1.0, abs(velocity) / 0.5) * 0.6
    else:
        dir_score = 0.05
    
    mom_score = min(1.0, abs(velocity) / 1.0)
    lag_score = 0.2
    vol_score = min(1.0, abs(velocity) * 2.0) if abs(velocity) > 0.15 else 0.1
    
    if time_pct < 0.20: tte_score = 0.05
    elif time_pct < 0.40: tte_score = 0.40
    elif time_pct < 0.80: tte_score = 0.75
    elif time_pct < 0.90: tte_score = 0.95
    else: tte_score = 0.70
    if abs(velocity) < 0.05: tte_score *= 0.5
    
    if spread > 0.02: exec_score = 0.3
    elif spread > 0.01: exec_score = 0.6
    else: exec_score = 0.9
    cross_score = 0.5
    
    rsi_map = {'severe_oversold':0.95,'oversold':0.80,'near_oversold':0.55,
               'severe_overbought':0.90,'overbought':0.75,'near_overbought':0.50,'dead_zone':0.30}
    rsi_score = rsi_map.get(rsi_zone, 0.30)
    
    composite = (W_DIR*dir_score + W_MOM*mom_score + W_LAG*lag_score +
                 W_VOL*vol_score + W_TTE*tte_score + W_EXEC*exec_score +
                 W_CROSS*cross_score + W_RSI*rsi_score)
    
    if composite < 0.25:
        return None, composite, 'below_threshold'
    
    # Side selection with continuation prior
    down_score = composite
    up_score = composite
    if direction == 'DOWN':
        down_score *= 1.3; up_score *= 0.4
    elif direction == 'UP':
        up_score *= 1.1; down_score *= 0.5
    
    if rsi_zone in ('severe_oversold','oversold','near_oversold'):
        down_score *= 1.2; up_score *= 0.7
    elif rsi_zone in ('severe_overbought','overbought','near_overbought'):
        up_score *= 1.15; down_score *= 0.8
    
    if down_score > up_score and down_score >= 0.25:
        if rsi_zone == 'severe_oversold': tier = 'severe_oversold_down'
        elif rsi_zone in ('oversold','near_oversold'): tier = 'oversold_down'
        else: tier = 'direction_down_cheap'
        return 'DOWN', composite, tier
    elif up_score > down_score and up_score >= 0.25:
        if rsi_zone == 'severe_overbought': tier = 'severe_overbought_up'
        elif rsi_zone in ('overbought','near_overbought'): tier = 'overbought_up'
        else: tier = 'direction_up_cheap'
        return 'UP', composite, tier
    return None, composite, 'no_side_advantage'


def process_rg_pair_data(prices_aid1, prices_aid2, n_points_threshold=40):
    """
    Given two paired token price arrays, determine:
    - Which is cheap, which is rich
    - Settlement outcome (who won)
    - Individual trade signals
    Returns list of (entry_price, settlement, side, rsi, velocity, regime, time_pct, tier, composite) tuples
    """
    # Determine cheap vs rich
    mean1 = np.mean(prices_aid1[-min(50,len(prices_aid1)):])
    mean2 = np.mean(prices_aid2[-min(50,len(prices_aid2)):])
    
    if mean1 < mean2:
        cheap_prices = prices_aid1
        rich_prices = prices_aid2
    else:
        cheap_prices = prices_aid2
        rich_prices = prices_aid1
    
    n = len(cheap_prices)
    if n < n_points_threshold:
        return []
    
    # Settlement: use final 10% of prices to determine winner
    settlement_window = max(5, n // 10)
    settlement_cheap = np.mean(cheap_prices[-settlement_window:])
    settlement_rich = np.mean(rich_prices[-settlement_window:])
    
    rich_won = settlement_rich > settlement_cheap
    
    # Generate RSI for cheap token
    rsi_arr = compute_rsi(cheap_prices)
    
    # Sample signal points
    results = []
    step = max(1, n // 8)  # ~8 signal points per market
    
    for i in range(24, n, step):
        current_price = float(cheap_prices[i])
        current_rsi = float(rsi_arr[i])
        
        if current_price < 0.03 or current_price > 0.60:
            continue
        
        time_pct = i / max(n, 1)
        window = cheap_prices[:i+1]
        direction, velocity = detect_direction(window)
        regime = get_regime(window)
        
        if current_price < 0.10: spread = 0.02
        elif current_price < 0.20: spread = 0.015
        elif current_price < 0.40: spread = 0.01
        else: spread = 0.008
        
        side, composite, tier = classify_signal(current_rsi, direction, velocity, regime, time_pct, spread, current_price)
        
        if side is None:
            continue
        
        tc = TIER_CONFIG.get(tier, TIER_CONFIG['direction_down_cheap'])
        if current_price > tc['max_price'] or current_price < 0.03:
            continue
        if composite < 0.25:
            continue
        
        # Settlement
        if rich_won:
            settlement = 0.0
            won = False
        else:
            settlement = 1.0
            won = True
        
        results.append({
            'entry_price': current_price,
            'settlement': settlement,
            'won': won,
            'side': side,
            'tier': tier,
            'composite': composite,
            'rsi': current_rsi,
            'velocity': velocity,
            'direction': direction,
            'regime': regime,
            'time_pct': time_pct,
            'cheap_mean': float(np.mean(cheap_prices)),
            'rich_mean': float(np.mean(rich_prices)),
        })
    
    return results


def run_simulation():
    print("=" * 70)
    print("V21 PMXT 2000-TRADE SIMULATION V3 — MEMORY EFFICIENT")
    print("=" * 70)
    print(f"Binary settlement | Continuation prior {CONTINUATION_PRIOR}:1 | Friction model")
    
    files = sorted(PMXT_DIR.glob("*.parquet"))
    valid_files = []
    for f in files:
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000:
                valid_files.append(f)
        except:
            continue
    
    print(f"Valid files: {len(valid_files)}/{len(files)}")
    if not valid_files:
        print("ERROR: No valid PMXT files!")
        return
    
    bankroll = BANKROLL_START
    trades = []
    daily_pnl = defaultdict(float)
    daily_trades_count = defaultdict(int)
    tier_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    side_stats = {'UP': {'trades': 0, 'wins': 0, 'pnl': 0.0}, 'DOWN': {'trades': 0, 'wins': 0, 'pnl': 0.0}}
    score_dist = []
    rsi_dist = []
    entry_price_dist = []
    spread_traps = 0
    global_rejections = 0
    global_partials = 0
    
    start_time = time.time()
    
    for fidx, fpath in enumerate(valid_files):
        if len(trades) >= TARGET_TRADES:
            break
        
        print(f"\n[{fidx+1}/{len(valid_files)}] {fpath.name}...", end=" ", flush=True)
        fstart = time.time()
        
        try:
            pf = pq.ParquetFile(str(fpath))
        except Exception as e:
            print(f"ERROR: {e}")
            continue
        
        n_rgs = pf.metadata.num_row_groups
        
        # Stream through RGs, process each market pair
        for rg_idx in range(0, n_rgs, 2):  # Skip every other RG for speed
            if len(trades) >= TARGET_TRADES:
                break
            
            try:
                t = pf.read_row_group(rg_idx, columns=['market', 'asset_id', 'price', 'event_type'])
            except:
                continue
            
            # Filter price_change events
            event_col = t.column('event_type')
            try:
                mask_arr = pc.equal(event_col, 'price_change')
                idxs = np.where(mask_arr.to_numpy())[0]
            except:
                # Fallback: manual filter
                evs = event_col.to_pylist()
                idxs = np.array([i for i, e in enumerate(evs) if e == 'price_change'])
            if len(idxs) == 0:
                del t
                continue
            
            mkt_col = t.column('market')
            aid_col = t.column('asset_id')
            price_col = t.column('price').to_numpy().astype(np.float64)
            
            # Build per (cid, aid) price series from this RG
            pair_prices = defaultdict(dict)  # cid -> {aid: [prices]}
            
            # Sample for efficiency (every 5th row)
            step = max(1, len(idxs) // 10000)
            for i in idxs[::step]:
                mv = mkt_col[i.as_py() if hasattr(i, 'as_py') else i]
                cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
                aid = str(aid_col[i])
                p = float(price_col[i])
                
                if 0.01 < p < 0.99:
                    if aid not in pair_prices[cid]:
                        pair_prices[cid][aid] = []
                    pair_prices[cid][aid].append(p)
            
            del t
            
            # Process pairs (cid with exactly 2 aids = binary market)
            for cid, aids in pair_prices.items():
                if len(trades) >= TARGET_TRADES:
                    break
                
                if len(aids) != 2:
                    continue
                
                aid_list = list(aids)
                p1 = np.array(aids[aid_list[0]])
                p2 = np.array(aids[aid_list[1]])
                
                # Need enough data for both tokens
                if len(p1) < 40 or len(p2) < 40:
                    continue
                
                # Get trade signals
                signals = process_rg_pair_data(p1, p2, n_points_threshold=40)
                
                for sig in signals:
                    if len(trades) >= TARGET_TRADES:
                        break
                    
                    entry_price = sig['entry_price']
                    settlement = sig['settlement']
                    won = sig['won']
                    
                    # Position sizing
                    tc = TIER_CONFIG.get(sig['tier'], TIER_CONFIG['direction_down_cheap'])
                    position_usd = min(tc['size_pct'] * bankroll, MAX_POSITION_USD)
                    position_usd = max(position_usd, MIN_TRADE_SIZE)
                    
                    if bankroll <= 2.0:
                        continue
                    
                    # Execution friction
                    eff_price = entry_price + SPREAD_COST + entry_price * SLIPPAGE_PCT
                    eff_price = min(eff_price, 0.99)
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
                        'tier': sig['tier'],
                        'side': sig['side'],
                        'entry': eff_price,
                        'shares': shares,
                        'size_usd': position_usd,
                        'settlement': settlement,
                        'pnl': pnl,
                        'won': won,
                        'composite': sig['composite'],
                        'rsi': sig['rsi'],
                        'velocity': sig['velocity'],
                        'direction': sig['direction'],
                        'regime': sig['regime'],
                        'time_pct': sig['time_pct'],
                    })
                    
                    bankroll += pnl
                    
                    tier_stats[sig['tier']]['trades'] += 1
                    if won: tier_stats[sig['tier']]['wins'] += 1
                    tier_stats[sig['tier']]['pnl'] += pnl
                    
                    side_stats[sig['side']]['trades'] += 1
                    if won: side_stats[sig['side']]['wins'] += 1
                    side_stats[sig['side']]['pnl'] += pnl
                    
                    score_dist.append(sig['composite'])
                    rsi_dist.append(sig['rsi'])
                    entry_price_dist.append(eff_price)
            
            del pair_prices
        
        file_elapsed = time.time() - fstart
        file_new = len(trades) - sum(v['trades'] for v in tier_stats.values()) + len(trades)
        wr = sum(1 for t in trades if t['won']) / max(len(trades), 1) * 100
        print(f"trades={len(trades)}, WR={wr:.1f}%, bank=${bankroll:.2f} ({file_elapsed:.0f}s)", flush=True)
        
        gc.collect()
        
        if len(trades) >= TARGET_TRADES:
            break
    
    # Trim to target
    if len(trades) > TARGET_TRADES:
        trades = trades[:TARGET_TRADES]
    
    # ════════════════════════════════════════════════════════════
    # RESULTS
    # ════════════════════════════════════════════════════════════
    
    total_elapsed = time.time() - start_time
    n_trades = len(trades)
    total_wins = sum(1 for t in trades if t['won'])
    total_pnl = sum(t['pnl'] for t in trades)
    
    print("\n" + "=" * 70)
    print("V21 PMXT 2000-TRADE SIMULATION V3 RESULTS")
    print("=" * 70)
    
    if n_trades == 0:
        print("NO TRADES GENERATED")
        return
    
    overall_wr = total_wins / n_trades * 100
    gross_profit = sum(t['pnl'] for t in trades if t['pnl'] > 0)
    gross_loss = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
    profit_factor = gross_profit / max(gross_loss, 0.01)
    pnls = [t['pnl'] for t in trades]
    mean_pnl = np.mean(pnls)
    std_pnl = np.std(pnls)
    sharpe = mean_pnl / max(std_pnl, 0.001) * np.sqrt(252)
    
    # Streaks
    cur = 0; best_w = 0; best_l = 0
    for t in trades:
        if t['won']:
            cur = max(1, cur + 1) if cur > 0 else 1
            best_w = max(best_w, cur)
        else:
            cur = min(-1, cur - 1) if cur < 0 else -1
            best_l = max(best_l, abs(cur))
    
    print(f"\n{'METRIC':<30s} {'VALUE':>15s}")
    print("-" * 47)
    print(f"{'Total trades':<30s} {n_trades:>15d}")
    print(f"{'Wins / Losses':<30s} {total_wins:>7d} / {n_trades-total_wins:<7d}")
    print(f"{'Win rate':<30s} {overall_wr:>14.1f}%")
    print(f"{'Final bankroll':<30s} ${bankroll:>14.2f}")
    print(f"{'Total P&L':<30s} ${total_pnl:>14.2f}")
    print(f"{'ROI':<30s} {(bankroll/BANKROLL_START-1)*100:>14.1f}%")
    print(f"{'Avg trade P&L':<30s} ${mean_pnl:>14.4f}")
    print(f"{'Profit factor':<30s} {profit_factor:>15.2f}")
    print(f"{'Sharpe (approx)':<30s} {sharpe:>15.2f}")
    print(f"{'Max win streak':<30s} {best_w:>15d}")
    print(f"{'Max loss streak':<30s} {best_l:>15d}")
    print(f"{'Fill rejections':<30s} {global_rejections:>15d}")
    print(f"{'Partial fills':<30s} {global_partials:>15d}")
    
    # Side breakdown
    print(f"\n{'SIDE BREAKDOWN':}")
    print(f"{'Side':<10s} {'Trades':>8s} {'Wins':>8s} {'WR%':>8s} {'P&L':>12s}")
    print("-" * 50)
    for side in ['UP', 'DOWN']:
        s = side_stats[side]
        if s['trades'] > 0:
            wr = s['wins'] / s['trades'] * 100
            print(f"{side:<10s} {s['trades']:>8d} {s['wins']:>8d} {wr:>7.1f}% ${s['pnl']:>10.2f}")
    
    # Tier breakdown
    print(f"\n{'TIER BREAKDOWN':}")
    print(f"{'Tier':<30s} {'Trades':>7s} {'Wins':>7s} {'WR%':>7s} {'P&L':>12s} {'AvgPnL':>8s}")
    print("-" * 75)
    for tier in ['severe_oversold_down','severe_overbought_up','oversold_down',
                  'overbought_up','direction_down_cheap','direction_up_cheap']:
        s = tier_stats[tier]
        if s['trades'] > 0:
            t_wr = s['wins'] / s['trades'] * 100
            avg = s['pnl'] / s['trades']
            print(f"{tier:<30s} {s['trades']:>7d} {s['wins']:>7d} {t_wr:>6.1f}% ${s['pnl']:>10.2f} ${avg:>7.4f}")
    
    # Settlement
    won_n = sum(1 for t in trades if t['won'])
    lost_n = n_trades - won_n
    print(f"\n{'BINARY SETTLEMENT':}")
    print(f"  Won (settlement=1.0): {won_n} ({won_n/max(n_trades,1)*100:.1f}%)")
    print(f"  Lost (settlement=0.0): {lost_n} ({lost_n/max(n_trades,1)*100:.1f}%)")
    
    # Entry price distribution
    if entry_price_dist:
        ep = np.array(entry_price_dist)
        print(f"\n{'ENTRY PRICE DISTRIBUTION':}")
        print(f"  Mean: {np.mean(ep):.4f}, Median: {np.median(ep):.4f}")
        for lo, hi, label in [(0.03,0.08,'<8¢'),(0.08,0.12,'8-12¢'),(0.12,0.20,'12-20¢'),(0.20,0.35,'20-35¢'),(0.35,0.50,'35-50¢')]:
            n = np.sum((ep >= lo) & (ep < hi))
            wr = sum(1 for i,t in enumerate(trades) if lo <= t['entry'] < hi and t['won']) / max(n,1) * 100
            pnl = sum(t['pnl'] for t in trades if lo <= t['entry'] < hi)
            print(f"  {label:<10s}: {n:>5d} trades, {wr:>6.1f}% WR, ${pnl:.2f} P&L")
    
    # RSI distribution
    if rsi_dist:
        rr = np.array(rsi_dist)
        print(f"\n{'RSI DISTRIBUTION':}")
        print(f"  Mean: {np.mean(rr):.1f}")
        for lo, hi, label in [(0,25,'SevereOversold'),(25,35,'Oversold'),(35,65,'DeadZone'),(65,73,'Overbought'),(73,100,'SevereOB')]:
            n = np.sum((rr >= lo) & (rr < hi))
            wr = sum(1 for i,t in enumerate(trades) if lo <= t['rsi'] < hi and t['won']) / max(n,1) * 100
            pnl = sum(t['pnl'] for t in trades if lo <= t['rsi'] < hi)
            print(f"  {label:<18s}: {n:>5d} trades, {wr:>6.1f}% WR, ${pnl:.2f} P&L")
    
    # Regime distribution
    regimes = defaultdict(int)
    for t in trades:
        regimes[t['regime']] += 1
    print(f"\n{'REGIME DISTRIBUTION':}")
    for regime, count in sorted(regimes.items(), key=lambda x: -x[1]):
        print(f"  {regime}: {count} ({count/n_trades*100:.1f}%)")
    
    # Monte Carlo
    print(f"\n{'MONTE CARLO ROBUSTNESS (1000 sims)':}")
    mc_profits = 0
    mc_finals = []
    mc_dds = []
    for _ in range(1000):
        idx = np.random.choice(n_trades, size=n_trades, replace=True)
        sim_bank = BANKROLL_START
        sim_peak = BANKROLL_START
        sim_max_dd = 0
        for i in idx:
            sim_bank += pnls[i]
            sim_peak = max(sim_peak, sim_bank)
            dd = (sim_peak - sim_bank) / max(sim_peak, 1)
            sim_max_dd = max(sim_max_dd, dd)
        mc_finals.append(sim_bank)
        mc_dds.append(sim_max_dd * 100)
        if sim_bank > BANKROLL_START: mc_profits += 1
    
    print(f"  Profitable sims: {mc_profits/1000*100:.1f}%")
    print(f"  Mean final: ${np.mean(mc_finals):.2f}, Median: ${np.median(mc_finals):.2f}")
    print(f"  Mean max DD: {np.mean(mc_dds):.1f}%")
    print(f"  P&L 5th pctile: ${np.percentile([b-BANKROLL_START for b in mc_finals], 5):.2f}")
    print(f"  P&L 95th pctile: ${np.percentile([b-BANKROLL_START for b in mc_finals], 95):.2f}")
    print(f"  Bust rate: {sum(1 for b in mc_finals if b <= 0)/10:.1f}%")
    
    print(f"\n  Total time: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")
    
    # Save
    output_dir = Path("/home/naq1987s/father-daddy-capital/output")
    output_dir.mkdir(exist_ok=True)
    
    results = {
        'version': 'V21_PMXT_v3',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'config': {'bankroll_start': BANKROLL_START, 'max_position': MAX_POSITION_USD,
                   'spread_cost': SPREAD_COST, 'slippage_pct': SLIPPAGE_PCT,
                   'continuation_prior': CONTINUATION_PRIOR, 'binary_settlement': True},
        'summary': {'total_trades': n_trades, 'win_rate': overall_wr,
                    'final_bankroll': bankroll, 'total_pnl': total_pnl,
                    'roi_pct': (bankroll/BANKROLL_START-1)*100,
                    'profit_factor': profit_factor, 'sharpe_approx': sharpe},
        'tier_stats': {k: dict(v) for k, v in tier_stats.items()},
        'side_stats': side_stats,
    }
    
    out_file = output_dir / "v21_pmxt_2000_trade_sim_v3.json"
    with open(out_file, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {out_file}")


if __name__ == '__main__':
    np.random.seed(42)
    random.seed(42)
    run_simulation()