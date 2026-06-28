#!/usr/bin/env python3
"""
V21 PMXT 2000-Trade Simulation — V2 (Efficient Vectorized)
============================================================
PMXT orderbook parquet data with V21 directional extraction.

KEY FIXES vs V1:
1. Process all 76 RGs per file (not capped at 20)
2. Better sampling: stride through price series instead of single-row scan
3. Proper binary settlement: compare pair tokens, NOT absolute final price
4. Higher trade generation: relax filters, more market coverage
5. Track UP/DOWN pairs per condition_id for true settlement
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
from pathlib import Path
from collections import defaultdict
import time, gc, json, sys, math, random

# ════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
TARGET_TRADES = 2000
BANKROLL_START = 100.0
MAX_POSITION_USD = 2.00
DAILY_LOSS_LIMIT = 10.0
MIN_TRADE_SIZE = 0.25
MIN_DATA_POINTS = 50  # Minimum price observations per token

# V21 scoring weights (§7)
W_DIR = 0.25; W_MOM = 0.20; W_LAG = 0.10; W_VOL = 0.10
W_TTE = 0.10; W_EXEC = 0.10; W_CROSS = 0.10; W_RSI = 0.05

# Tier configuration
TIER_CONFIG = {
    'severe_oversold_down':  {'max_price': 0.50, 'size_pct': 0.10, 'base_wr': 0.80},
    'severe_overbought_up':  {'max_price': 0.50, 'size_pct': 0.10, 'base_wr': 0.87},
    'oversold_down':         {'max_price': 0.20, 'size_pct': 0.06, 'base_wr': 0.74},
    'overbought_up':         {'max_price': 0.20, 'size_pct': 0.05, 'base_wr': 0.71},
    'direction_down_cheap':  {'max_price': 0.15, 'size_pct': 0.03, 'base_wr': 0.68},
    'direction_up_cheap':    {'max_price': 0.15, 'size_pct': 0.03, 'base_wr': 0.70},
}

RSI_OVERSOLD_SEVERE = 25; RSI_OVERSOLD = 30; RSI_NEAR_OVERSOLD = 35
RSI_OVERBOUGHT_SEVERE = 73; RSI_OVERBOUGHT = 70; RSI_NEAR_OVERBOUGHT = 65

SPREAD_COST = 0.01
SLIPPAGE_PCT = 0.005
FILL_REJECTION_RATE = 0.05
PARTIAL_FILL_RATE = 0.10

MAX_DAILY_TRADES = 50  # Increased for more trades
CONTINUATION_PRIOR = 3.0


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


def detect_direction(prices, lookback=3, min_change=0.05):
    n = len(prices)
    if n < lookback + 1:
        return 'FLAT', 0.0
    
    ref = prices[-1 - lookback] if n > lookback else prices[0]
    spot_delta = (prices[-1] - ref) / max(abs(ref), 1e-9) * 100
    
    # Multi-velocity
    v15 = (prices[-1] - prices[max(-4, -n)]) / max(abs(prices[max(-4, -n)]), 1e-9) * 100 if n >= 4 else abs(spot_delta)
    v30 = (prices[-1] - prices[max(-8, -n)]) / max(abs(prices[max(-8, -n)]), 1e-9) * 100 if n >= 8 else 0
    velocity = 0.5 * v15 + 0.3 * v30 + 0.2 * spot_delta if n >= 8 else abs(spot_delta)
    
    # Consecutive candles
    consec = 0
    for i in range(1, min(5, n)):
        if prices[-i] > prices[-i-1]:
            consec += 1
        elif prices[-i] < prices[-i-1]:
            consec -= 1
    
    persistence = 1.0 if consec > 1 else (-1.0 if consec < -1 else 0.0)
    blended = 0.4 * (1.0 if spot_delta > 0 else (-1.0 if spot_delta < 0 else 0.0)) + 0.6 * persistence
    
    if blended > 0.3 and velocity > min_change:
        return 'UP', velocity
    elif blended < -0.3 and velocity < -min_change:
        return 'DOWN', velocity
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


def compute_score(rsi, direction, velocity, regime, time_pct, spread, rsi_zone):
    """V21.5 composite opportunity score."""
    if spread > 0.03:
        return 0.0, 'spread_trap'
    
    # Directional persistence (25%)
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
    
    if abs(velocity) < 0.05:
        tte_score *= 0.5
    
    if spread > 0.02: exec_score = 0.3
    elif spread > 0.01: exec_score = 0.6
    else: exec_score = 0.9
    
    cross_score = 0.5
    
    rsi_scores = {
        'severe_oversold': 0.95, 'oversold': 0.80, 'near_oversold': 0.55,
        'severe_overbought': 0.90, 'overbought': 0.75, 'near_overbought': 0.50,
        'dead_zone': 0.30
    }
    rsi_score = rsi_scores.get(rsi_zone, 0.30)
    
    composite = (
        W_DIR * dir_score + W_MOM * mom_score + W_LAG * lag_score +
        W_VOL * vol_score + W_TTE * tte_score + W_EXEC * exec_score +
        W_CROSS * cross_score + W_RSI * rsi_score
    )
    return composite, None


def get_rsi_zone(rsi):
    if rsi < 25: return 'severe_oversold'
    if rsi < 30: return 'oversold'
    if rsi < 35: return 'near_oversold'
    if rsi > 73: return 'severe_overbought'
    if rsi > 70: return 'overbought'
    if rsi > 65: return 'near_overbought'
    return 'dead_zone'


def classify_signal(rsi, direction, velocity, regime, time_pct, spread, price):
    """Classify signal using V21 directional extraction."""
    rsi_zone = get_rsi_zone(rsi)
    composite, reason = compute_score(rsi, direction, velocity, regime, time_pct, spread, rsi_zone)
    
    if reason == 'spread_trap' or composite < 0.25:
        return None, composite, 'below_threshold'
    
    # Side selection: continuation prior + RSI zone alignment
    down_score = composite
    up_score = composite
    
    if direction == 'DOWN':  # Continuation bias
        down_score *= 1.3 * 1.0  # bare continuation
        up_score *= 0.4
    elif direction == 'UP':
        up_score *= 1.1 * 1.0
        down_score *= 0.5
    
    # RSI zone alignment
    if rsi_zone in ('severe_oversold', 'oversold', 'near_oversold'):
        down_score *= 1.2; up_score *= 0.7
    elif rsi_zone in ('severe_overbought', 'overbought', 'near_overbought'):
        up_score *= 1.15; down_score *= 0.8
    
    # Determine tier
    if down_score > up_score and down_score >= 0.25:
        if rsi_zone == 'severe_oversold': tier = 'severe_oversold_down'
        elif rsi_zone in ('oversold', 'near_oversold'): tier = 'oversold_down'
        else: tier = 'direction_down_cheap'
        return 'DOWN', composite, tier
    elif up_score > down_score and up_score >= 0.25:
        if rsi_zone == 'severe_overbought': tier = 'severe_overbought_up'
        elif rsi_zone in ('overbought', 'near_overbought'): tier = 'overbought_up'
        else: tier = 'direction_up_cheap'
        return 'UP', composite, tier
    return None, composite, 'no_side_advantage'


def run_simulation():
    print("=" * 70)
    print("V21 PMXT 2000-TRADE SIMULATION V2 — VECTORIZED")
    print("=" * 70)
    print(f"Binary settlement | Directional engine | Continuation prior {CONTINUATION_PRIOR}:1")
    
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
    total_scanned = 0
    total_candidates = 0
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
        # Process ALL row groups for maximum data
        
        # Phase 1: Accumulate all cheap-side token price series per (cid, aid)
        # This maps each token to its price history across all RGs
        token_series = defaultdict(list)  # (cid, aid) -> [(ts, price)]
        # Also track cid -> [aid1, aid2] for binary pair resolution
        cid_aids = defaultdict(set)
        
        for rg_idx in range(n_rgs):
            try:
                t = pf.read_row_group(rg_idx, columns=['market', 'asset_id', 'price', 'event_type'])
            except:
                continue
            
            mask = pc.equal(t.column('event_type'), 'price_change')
            t2 = t.filter(mask)
            n = len(t2)
            if n == 0:
                del t, t2
                continue
            
            mkt_col = t2.column('market')
            aid_col = t2.column('asset_id')
            price_col = t2.column('price').to_numpy().astype(np.float64)
            
            # Sample every 10th row for memory efficiency but keep coverage
            step = max(1, n // 50000)  # ~50K sampled per RG at most
            
            for i in range(0, n, step):
                mv = mkt_col[i]
                cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
                aid = str(aid_col[i])
                p = float(price_col[i])
                
                # Keep cheap side AND its pair token (for settlement)
                if 0.01 < p < 0.99:
                    token_series[(cid, aid)].append(p)
                    cid_aids[cid].add(aid)
            
            del t, t2
            if rg_idx % 10 == 0:
                gc.collect()
        
        print(f"tokens={len(token_series)} pairs={sum(1 for c in cid_aids.values() if len(c)>=2)}", end=" ", flush=True)
        
        # Phase 2: For tokens with enough data, generate signals and settle trades
        file_trades = 0
        file_scanned = 0
        
        # Only process tokens where we have the binary pair (UP/DOWN)
        # Settlement requires knowing which side won
        paired_tokens = []
        for cid, aids in cid_aids.items():
            if len(aids) == 2:
                aid_list = list(aids)
                paired_tokens.append((cid, aid_list[0], aid_list[1]))
        
        print(f"pairs={len(paired_tokens)}", end=" ", flush=True)
        
        for cid, aid1, aid2 in paired_tokens:
            if len(trades) >= TARGET_TRADES:
                break
            
            key1 = (cid, aid1)
            key2 = (cid, aid2)
            
            s1 = token_series.get(key1, [])
            s2 = token_series.get(key2, [])
            
            if len(s1) < MIN_DATA_POINTS or len(s2) < MIN_DATA_POINTS:
                continue
            
            # Convert to numpy arrays
            p1 = np.array(s1)
            p2 = np.array(s2)
            
            # Determine which is cheap (the one we'd buy) and which is expensive
            mean1 = np.mean(p1[-50:]) if len(p1) > 50 else np.mean(p1)
            mean2 = np.mean(p2[-50:]) if len(p2) > 50 else np.mean(p2)
            
            if mean1 < mean2:
                cheap_key, cheap_prices = key1, p1
                rich_key, rich_prices = key2, p2
                cheap_mean, rich_mean = mean1, mean2
            else:
                cheap_key, cheap_prices = key2, p2
                rich_key, rich_prices = key1, p1
                cheap_mean, rich_mean = mean2, mean1
            
            # Only trade cheap-side tokens (< 0.50)
            if cheap_mean > 0.50:
                continue
            
            # Determine settlement: in PMXT data, final price tells us who won
            # If the cheap token's final price > 0.50 → it won (settlement = 1.0)
            # If cheap token stayed < 0.50 → it lost (settlement = 0.0)
            # Use the rich token as confirmation: rich > 0.50 means rich won → cheap lost
            final_cheap = float(cheap_prices[-1])
            final_rich = float(rich_prices[-1])
            
            # Binary settlement: token with final price > 0.50 won
            # In a proper binary pair: winner → 1.0, loser → 0.0
            # But PMXT prices fluctuate, so we use the LAST few prices to determine settlement
            settlement_window = min(20, len(cheap_prices))
            settlement_cheap = float(np.mean(cheap_prices[-settlement_window:]))
            settlement_rich = float(np.mean(rich_prices[-settlement_window:]))
            
            if settlement_rich > settlement_cheap:
                rich_won = True
            else:
                rich_won = False
            
            # Generate RSI and direction signals
            rsi_arr = compute_rsi(cheap_prices)
            
            # Sample signal points
            n_points = len(cheap_prices)
            step = max(1, n_points // 5)  # ~5 signal points per market
            
            for i in range(24, n_points, step):
                if len(trades) >= TARGET_TRADES:
                    break
                
                total_scanned += 1
                
                window = cheap_prices[:i+1]
                current_price = float(cheap_prices[i])
                current_rsi = float(rsi_arr[i])
                
                # Skip bad prices
                if current_price < 0.03 or current_price > 0.60:
                    continue
                
                # Time progression
                time_pct = i / max(n_points, 1)
                
                # Direction detection on cheap token price movement
                direction, velocity = detect_direction(window)
                
                # For cheap tokens going UP = cheap price rising = settlement probability increasing
                # For cheap tokens going DOWN = cheap price falling = settlement probability decreasing
                # If we BUY the cheap token, we want it to win (settle to 1.0)
                # If we BUY the rich token (short cheap), we want cheap to lose (settle to 0.0)
                
                # Regime
                regime = get_regime(window)
                
                # Spread estimate based on price level
                if current_price < 0.10: spread = 0.02
                elif current_price < 0.20: spread = 0.015
                elif current_price < 0.40: spread = 0.01
                else: spread = 0.008
                
                # V21 signal classification
                side, composite, tier = classify_signal(
                    current_rsi, direction, velocity, regime, time_pct, spread, current_price
                )
                
                if side is None:
                    if tier == 'spread_trap':
                        spread_traps += 1
                    continue
                
                total_candidates += 1
                
                # Tier config
                tc = TIER_CONFIG.get(tier, TIER_CONFIG['direction_down_cheap'])
                max_price = tc['max_price']
                size_pct = tc['size_pct']
                
                # Price gate
                if current_price > max_price or current_price < 0.03:
                    continue
                
                # Score gate (lowered to 0.25 for more trades)
                if composite < 0.25:
                    continue
                
                # Daily limits
                day_key = int(time.time()) // 86400  # Use current day
                if daily_trades_count[day_key] >= MAX_DAILY_TRADES:
                    continue
                if daily_pnl[day_key] <= -DAILY_LOSS_LIMIT:
                    continue
                
                # Position sizing
                if bankroll <= 2.0:
                    continue
                
                position_usd = min(size_pct * bankroll, MAX_POSITION_USD)
                position_usd = max(position_usd, MIN_TRADE_SIZE)
                
                # Execution friction
                eff_price = current_price + SPREAD_COST + current_price * SLIPPAGE_PCT
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
                
                # BINARY SETTLEMENT
                # We bought the cheap token at eff_price
                # Settlement: did this token win or lose?
                if rich_won:
                    # Rich token won → cheap token LOST → settlement = 0.0
                    settlement = 0.0
                    won = False
                else:
                    # Rich token lost → cheap token WON → settlement = 1.0
                    settlement = 1.0
                    won = True
                
                pnl = shares * (settlement - eff_price)
                
                # Record trade
                trade = {
                    'tier': tier,
                    'side': side,
                    'entry': eff_price,
                    'shares': shares,
                    'size_usd': position_usd,
                    'settlement': settlement,
                    'pnl': pnl,
                    'won': won,
                    'composite': composite,
                    'rsi': current_rsi,
                    'velocity': velocity,
                    'direction': direction,
                    'regime': regime,
                    'time_pct': time_pct,
                    'spread': spread,
                    'cheap_mean': cheap_mean,
                    'rich_mean': rich_mean,
                    'final_cheap': final_cheap,
                    'final_rich': final_rich,
                }
                trades.append(trade)
                
                bankroll += pnl
                daily_pnl[day_key] += pnl
                daily_trades_count[day_key] += 1
                
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
        file_new_trades = len(trades) - file_trades
        if file_new_trades > 0:
            file_wr = sum(1 for t in trades[-file_new_trades:] if t['won']) / file_new_trades * 100
        else:
            file_wr = 0
        print(f"trades={len(trades)}, WR={file_wr:.1f}%, bank=${bankroll:.2f} ({file_elapsed:.0f}s)")
        file_trades = len(trades)
        
        gc.collect()
        
        if len(trades) >= TARGET_TRADES:
            break
    
    total_elapsed = time.time() - start_time
    
    # ════════════════════════════════════════════════════════════════════
    # RESULTS
    # ════════════════════════════════════════════════════════════════════
    
    n_trades = len(trades)
    total_wins = sum(1 for t in trades if t['won'])
    total_pnl = sum(t['pnl'] for t in trades)
    
    print("\n" + "=" * 70)
    print("V21 PMXT 2000-TRADE SIMULATION V2 RESULTS")
    print("=" * 70)
    
    if n_trades == 0:
        print("NO TRADES GENERATED")
        print(f"Scanned: {total_scanned} | Candidates: {total_candidates} | Spread traps: {spread_traps}")
        return
    
    overall_wr = total_wins / n_trades * 100
    avg_pnl = total_pnl / n_trades
    max_loss = min(t['pnl'] for t in trades)
    max_win = max(t['pnl'] for t in trades)
    
    # Streaks
    current_streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    for t in trades:
        if t['won']:
            current_streak = max(1, current_streak + 1) if current_streak > 0 else 1
            max_win_streak = max(max_win_streak, current_streak)
        else:
            current_streak = min(-1, current_streak - 1) if current_streak < 0 else -1
            max_loss_streak = max(max_loss_streak, abs(current_streak))
    
    gross_profit = sum(t['pnl'] for t in trades if t['pnl'] > 0)
    gross_loss = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
    profit_factor = gross_profit / max(gross_loss, 0.01)
    
    pnls = [t['pnl'] for t in trades]
    mean_pnl = np.mean(pnls)
    std_pnl = np.std(pnls)
    sharpe_approx = mean_pnl / max(std_pnl, 0.001) * np.sqrt(252)
    
    print(f"\n{'METRIC':<30s} {'VALUE':>15s}")
    print("-" * 47)
    print(f"{'Total trades':<30s} {n_trades:>15d}")
    print(f"{'Total wins':<30s} {total_wins:>15d}")
    print(f"{'Total losses':<30s} {n_trades - total_wins:>15d}")
    print(f"{'Win rate':<30s} {overall_wr:>14.1f}%")
    print(f"{'Final bankroll':<30s} ${bankroll:>14.2f}")
    print(f"{'Starting bankroll':<30s} ${BANKROLL_START:>14.2f}")
    print(f"{'Total P&L':<30s} ${total_pnl:>14.2f}")
    print(f"{'ROI':<30s} {(bankroll/BANKROLL_START-1)*100:>14.1f}%")
    print(f"{'Avg trade P&L':<30s} ${avg_pnl:>14.4f}")
    print(f"{'Max single win':<30s} ${max_win:>14.2f}")
    print(f"{'Max single loss':<30s} ${max_loss:>14.2f}")
    print(f"{'Profit factor':<30s} {profit_factor:>15.2f}")
    print(f"{'Sharpe (approx)':<30s} {sharpe_approx:>15.2f}")
    print(f"{'Max win streak':<30s} {max_win_streak:>15d}")
    print(f"{'Max loss streak':<30s} {max_loss_streak:>15d}")
    print(f"{'Fill rejections':<30s} {global_rejections:>15d}")
    print(f"{'Partial fills':<30s} {global_partials:>15d}")
    print(f"{'Spread traps refused':<30s} {spread_traps:>15d}")
    print(f"{'Total scanned':<30s} {total_scanned:>15d}")
    print(f"{'Total candidates':<30s} {total_candidates:>15d}")
    
    print(f"\n{'SIDE BREAKDOWN':}")
    print(f"{'Side':<10s} {'Trades':>8s} {'Wins':>8s} {'WR%':>8s} {'P&L':>12s}")
    print("-" * 50)
    for side in ['UP', 'DOWN']:
        s = side_stats[side]
        if s['trades'] > 0:
            wr = s['wins'] / s['trades'] * 100
            print(f"{side:<10s} {s['trades']:>8d} {s['wins']:>8d} {wr:>7.1f}% ${s['pnl']:>10.2f}")
    
    print(f"\n{'TIER BREAKDOWN':}")
    print(f"{'Tier':<30s} {'Trades':>7s} {'Wins':>7s} {'WR%':>7s} {'P&L':>12s} {'AvgPnL':>8s}")
    print("-" * 75)
    for tier in ['severe_oversold_down', 'severe_overbought_up', 'oversold_down',
                  'overbought_up', 'direction_down_cheap', 'direction_up_cheap']:
        s = tier_stats[tier]
        if s['trades'] > 0:
            t_wr = s['wins'] / s['trades'] * 100
            avg = s['pnl'] / s['trades']
            print(f"{tier:<30s} {s['trades']:>7d} {s['wins']:>7d} {t_wr:>6.1f}% ${s['pnl']:>10.2f} ${avg:>7.4f}")
    
    # Settlement breakdown
    won_trades = [t for t in trades if t['won']]
    lost_trades = [t for t in trades if not t['won']]
    print(f"\n{'BINARY SETTLEMENT':}")
    print(f"  Settled at 1.0 (won): {len(won_trades)} ({len(won_trades)/max(n_trades,1)*100:.1f}%)")
    print(f"  Settled at 0.0 (lost): {len(lost_trades)} ({len(lost_trades)/max(n_trades,1)*100:.1f}%)")
    
    if entry_price_dist:
        ep = np.array(entry_price_dist)
        print(f"\n{'ENTRY PRICE DISTRIBUTION':}")
        print(f"  Mean: {np.mean(ep):.4f}, Median: {np.median(ep):.4f}")
        print(f"  Ultra-cheap (<0.10): {np.sum(ep < 0.10)} ({np.sum(ep < 0.10)/len(ep)*100:.1f}%)")
        print(f"  Cheap (0.10-0.20): {np.sum((ep >= 0.10) & (ep < 0.20))} ({np.sum((ep >= 0.10) & (ep < 0.20))/len(ep)*100:.1f}%)")
        print(f"  Moderate (0.20-0.40): {np.sum((ep >= 0.20) & (ep < 0.40))} ({np.sum((ep >= 0.20) & (ep < 0.40))/len(ep)*100:.1f}%)")
        print(f"  Expensive (0.40-0.50): {np.sum((ep >= 0.40) & (ep <= 0.50))} ({np.sum((ep >= 0.40) & (ep <= 0.50))/len(ep)*100:.1f}%)")
    
    if rsi_dist:
        rr = np.array(rsi_dist)
        print(f"\n{'RSI DISTRIBUTION':}")
        print(f"  Mean: {np.mean(rr):.1f}, Median: {np.median(rr):.1f}")
        print(f"  Severe oversold (<25): {np.sum(rr < 25)} ({np.sum(rr < 25)/len(rr)*100:.1f}%)")
        print(f"  Oversold (25-35): {np.sum((rr >= 25) & (rr < 35))} ({np.sum((rr >= 25) & (rr < 35))/len(rr)*100:.1f}%)")
        print(f"  Dead zone (35-65): {np.sum((rr >= 35) & (rr <= 65))} ({np.sum((rr >= 35) & (rr <= 65))/len(rr)*100:.1f}%)")
        print(f"  Overbought (65-73): {np.sum((rr > 65) & (rr <= 73))} ({np.sum((rr > 65) & (rr <= 73))/len(rr)*100:.1f}%)")
        print(f"  Severe overbought (>73): {np.sum(rr > 73)} ({np.sum(rr > 73)/len(rr)*100:.1f}%)")
    
    regimes = defaultdict(int)
    for t in trades:
        regimes[t['regime']] += 1
    print(f"\n{'REGIME DISTRIBUTION':}")
    for regime, count in sorted(regimes.items(), key=lambda x: -x[1]):
        print(f"  {regime}: {count} ({count/n_trades*100:.1f}%)")
    
    # Win rate by entry price bucket
    print(f"\n{'WR BY ENTRY PRICE BUCKET':}")
    for lo, hi, label in [(0.03, 0.08, '<8¢'), (0.08, 0.12, '8-12¢'), (0.12, 0.20, '12-20¢'), (0.20, 0.35, '20-35¢'), (0.35, 0.50, '35-50¢')]:
        bucket = [t for t in trades if lo <= t['entry'] < hi]
        if bucket:
            b_wr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
            b_pnl = sum(t['pnl'] for t in bucket)
            print(f"  {label:<10s}: {len(bucket):>5d} trades, {b_wr:>6.1f}% WR, ${b_pnl:.2f} P&L")
    
    # Win rate by RSI zone
    print(f"\n{'WR BY RSI ZONE':}")
    for rsi_lo, rsi_hi, label in [(0,25,'SevereOversold'), (25,35,'Oversold'), (35,65,'DeadZone'), (65,73,'Overbought'), (73,100,'SevereOB')]:
        bucket = [t for t in trades if rsi_lo <= t['rsi'] < rsi_hi]
        if bucket:
            b_wr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
            b_pnl = sum(t['pnl'] for t in bucket)
            print(f"  {label:<18s}: {len(bucket):>5d} trades, {b_wr:>6.1f}% WR, ${b_pnl:.2f} P&L")
    
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
        if sim_bank > BANKROLL_START:
            mc_profits += 1
    
    mc_profit_pct = mc_profits / 1000 * 100
    mc_5th = np.percentile([b - BANKROLL_START for b in mc_finals], 5)
    mc_95th = np.percentile([b - BANKROLL_START for b in mc_finals], 95)
    
    print(f"  Profitable sims: {mc_profit_pct:.1f}%")
    print(f"  Mean final: ${np.mean(mc_finals):.2f}, Median: ${np.median(mc_finals):.2f}")
    print(f"  Mean max drawdown: {np.mean(mc_dds):.1f}%")
    print(f"  P&L 5th pctile: ${mc_5th:.2f}, 95th: ${mc_95th:.2f}")
    print(f"  Bust rate (bankroll ≤ $0): {sum(1 for b in mc_finals if b <= 0)/10:.1f}%")
    
    print(f"\n  Total time: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")
    
    # Save results
    output_dir = Path("/home/naq1987s/father-daddy-capital/output")
    output_dir.mkdir(exist_ok=True)
    
    results = {
        'version': 'V21_PMXT_v2',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'config': {
            'bankroll_start': BANKROLL_START,
            'max_position': MAX_POSITION_USD,
            'spread_cost': SPREAD_COST,
            'slippage_pct': SLIPPAGE_PCT,
            'continuation_prior': CONTINUATION_PRIOR,
            'binary_settlement': True,
        },
        'summary': {
            'total_trades': n_trades,
            'total_wins': total_wins,
            'win_rate': overall_wr,
            'final_bankroll': bankroll,
            'total_pnl': total_pnl,
            'roi_pct': (bankroll/BANKROLL_START-1)*100,
            'profit_factor': profit_factor,
            'sharpe_approx': sharpe_approx,
        },
        'tier_stats': {k: dict(v) for k, v in tier_stats.items()},
        'side_stats': side_stats,
    }
    
    out_file = output_dir / "v21_pmxt_2000_trade_sim_v2.json"
    with open(out_file, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Results saved to {out_file}")


if __name__ == '__main__':
    np.random.seed(42)
    random.seed(42)
    run_simulation()