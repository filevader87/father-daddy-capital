#!/usr/bin/env python3
"""
V21.7.18 PMXT 5000-Trade Backtest V2 — RELAXED GATES
======================================================
V1 failed: 148 trades, 4.1% WR. Problems:
1. Composite gate 0.25 too strict → most trades filtered
2. RSI tiers only capture extreme zones → dead_zone dominates
3. Settlement model needs validation

V2 fixes:
- Lower composite threshold to 0.15
- Add dead_zone_cheap (DOWN at ≤20¢) and dead_zone_rich (UP at ≤20¢)
- Process ALL RGs (not skip-every-other)
- Lower min data points from 40 to 20
- Better settlement: use actual price movement direction, not just final average
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
from pathlib import Path
from collections import defaultdict
import time, gc, json, random

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v21718_hardening")
OUT_DIR.mkdir(parents=True, exist_ok=True)

TARGET_TRADES = 5000
BANKROLL_START = 100.0
CANARY_POSITION_USD = 1.00
MAX_POSITION_USD = 2.00
MIN_TRADE_SIZE = 0.25

TIER_CONFIG = {
    'severe_oversold_down':  {'max_price': 0.50, 'size_pct': 0.10, 'base_wr': 0.80},
    'severe_overbought_up':  {'max_price': 0.50, 'size_pct': 0.10, 'base_wr': 0.87},
    'oversold_down':         {'max_price': 0.20, 'size_pct': 0.06, 'base_wr': 0.74},
    'overbought_up':         {'max_price': 0.20, 'size_pct': 0.05, 'base_wr': 0.71},
    'dead_zone_down_cheap':  {'max_price': 0.20, 'size_pct': 0.04, 'base_wr': 0.55},
    'dead_zone_up_cheap':    {'max_price': 0.20, 'size_pct': 0.03, 'base_wr': 0.52},
    'direction_down_cheap':  {'max_price': 0.15, 'size_pct': 0.03, 'base_wr': 0.68},
    'direction_up_cheap':    {'max_price': 0.15, 'size_pct': 0.03, 'base_wr': 0.70},
    'canary_btc_down_15m':   {'max_price': 0.08, 'size_pct': 0.01, 'base_wr': 0.75},
}

SPREAD_COST = 0.01
SLIPPAGE_PCT = 0.005
FILL_REJECTION_RATE = 0.05
PARTIAL_FILL_RATE = 0.10
RESOLUTION_TIME_S = 900
REDEMPTION_TIME_S = 60
REDEEM_GAS_COST = 0.50
ANNUAL_OPPORTUNITY_RATE = 0.05

W_DIR=0.25; W_MOM=0.20; W_LAG=0.10; W_VOL=0.10; W_TTE=0.10; W_EXEC=0.10; W_CROSS=0.10; W_RSI=0.05
MIN_COMPOSITE = 0.15  # Lowered from 0.25


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
    v_short = (prices[-1] - prices[-4]) / max(abs(prices[-4]), 1e-9) * 100
    v_med = (prices[-1] - prices[max(-8, -n)]) / max(abs(prices[max(-8, -n)]), 1e-9) * 100 if n >= 8 else abs(v_short)
    velocity = 0.5 * v_short + 0.3 * v_med + 0.2 * v_short
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
    
    if composite < MIN_COMPOSITE:
        return None, composite, 'below_threshold'
    
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
    
    # V2: dead_zone gets directional bonus if price is cheap
    if rsi_zone == 'dead_zone' and price <= 0.20:
        down_score *= 1.1  # Slight cheap-side bias
    
    if down_score > up_score and down_score >= MIN_COMPOSITE:
        if 0.03 <= price <= 0.08 and rsi_zone in ('severe_oversold','oversold'):
            tier = 'canary_btc_down_15m'
        elif rsi_zone == 'severe_oversold': tier = 'severe_oversold_down'
        elif rsi_zone in ('oversold','near_oversold'): tier = 'oversold_down'
        elif rsi_zone == 'dead_zone': tier = 'dead_zone_down_cheap'
        else: tier = 'direction_down_cheap'
        return 'DOWN', composite, tier
    elif up_score > down_score and up_score >= MIN_COMPOSITE:
        if rsi_zone == 'severe_overbought': tier = 'severe_overbought_up'
        elif rsi_zone in ('overbought','near_overbought'): tier = 'overbought_up'
        elif rsi_zone == 'dead_zone': tier = 'dead_zone_up_cheap'
        else: tier = 'direction_up_cheap'
        return 'UP', composite, tier
    return None, composite, 'no_side_advantage'


def compute_resolution_friction(entry_price, position_usd, settlement, won):
    spread_adj = SPREAD_COST + entry_price * SLIPPAGE_PCT
    lockup_days = (RESOLUTION_TIME_S + REDEMPTION_TIME_S) / 86400
    lockup_penalty = position_usd * lockup_days * ANNUAL_OPPORTUNITY_RATE / 365
    gas_cost = REDEEM_GAS_COST if won else 0.0
    total_friction = spread_adj * position_usd / entry_price + lockup_penalty + gas_cost
    return total_friction


def process_rg_pair_data(prices_aid1, prices_aid2, n_points_threshold=20):
    """V2: Lower threshold from 40 to 20 for more data points."""
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
    
    # V3: Binary settlement — compare final cheap vs rich prices.
    # In PM binary markets: if rich price ends > cheap price → rich won → cheap lost.
    # But we're buying the CHEAP token. So cheap_won = (settlement_cheap > settlement_rich).
    # However, PMXT orderbook data shows prices BEFORE resolution. The "final" price
    # is just the last observed price, not the resolution. For proper backtesting,
    # use direction: if cheap price rose relative to its mean → cheap momentum → cheap likely won.
    settlement_window = max(5, n // 10)
    settlement_cheap = np.mean(cheap_prices[-settlement_window:])
    settlement_rich = np.mean(rich_prices[-settlement_window:])
    
    # Rich-won heuristic: the token with higher final price relative to its
    # initial range is the winner. For binary UP/DOWN: if market resolves UP,
    # the UP token → $1 and DOWN → $0. We approximate by price momentum.
    cheap_initial = np.mean(cheap_prices[:max(5, n//10)])
    cheap_final = settlement_cheap
    rich_initial = np.mean(rich_prices[:max(5, n//10)])
    rich_final = settlement_rich
    
    # Price change direction determines winner
    cheap_pct_change = (cheap_final - cheap_initial) / max(cheap_initial, 0.01)
    rich_pct_change = (rich_final - rich_initial) / max(rich_initial, 0.01)
    
    if rich_pct_change > cheap_pct_change:
        # Rich token rose more → rich won → cheap lost
        cheap_won = False
    elif cheap_pct_change > rich_pct_change:
        # Cheap token rose more (or fell less) → cheap won
        cheap_won = True
    else:
        # Tie-break: absolute price comparison
        cheap_won = settlement_cheap > settlement_rich
    
    settlement = 1.0 if cheap_won else 0.0
    rsi_arr = compute_rsi(cheap_prices)
    results = []
    # V2: More signal points — step by 5 instead of n//8
    step = max(3, n // 12)
    
    for i in range(14, n, step):
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
        if composite < MIN_COMPOSITE:
            continue
        
        results.append({
            'entry_price': current_price, 'settlement': settlement, 'won': cheap_won,
            'side': side, 'tier': tier, 'composite': composite,
            'rsi': current_rsi, 'velocity': velocity,
            'direction': direction, 'regime': regime, 'time_pct': time_pct,
            'cheap_mean': float(np.mean(cheap_prices)),
            'rich_mean': float(np.mean(rich_prices)),
        })
    
    return results


def run_simulation():
    print("=" * 70)
    print("V21.7.18 PMXT 5000-TRADE BACKTEST V2 — RELAXED GATES")
    print("=" * 70)
    print(f"Composite min: {MIN_COMPOSITE} | Binary settlement | V21.7.18 friction")
    
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
    bankroll_canary = BANKROLL_START
    trades = []
    canary_trades = []
    tier_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0, 'friction': 0.0})
    side_stats = {'UP': {'trades': 0, 'wins': 0, 'pnl': 0.0}, 'DOWN': {'trades': 0, 'wins': 0, 'pnl': 0.0}}
    profile_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0, 'predictions': [], 'outcomes': []})
    friction_total = 0.0
    global_rejections = 0
    global_partials = 0
    score_dist = []
    rsi_dist = []
    entry_price_dist = []
    
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
        
        # V2: Process ALL RGs (not skip)
        for rg_idx in range(0, n_rgs):
            if len(trades) >= TARGET_TRADES:
                break
            
            try:
                t = pf.read_row_group(rg_idx, columns=['market', 'asset_id', 'price', 'event_type'])
            except:
                continue
            
            event_col = t.column('event_type')
            try:
                mask_arr = pc.equal(event_col, 'price_change')
                idxs = np.where(mask_arr.to_numpy())[0]
            except:
                evs = event_col.to_pylist()
                idxs = np.array([i for i, e in enumerate(evs) if e == 'price_change'])
            if len(idxs) == 0:
                del t
                continue
            
            mkt_col = t.column('market')
            aid_col = t.column('asset_id')
            price_col = t.column('price').to_numpy().astype(np.float64)
            
            pair_prices = defaultdict(dict)
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
            
            for cid, aids in pair_prices.items():
                if len(trades) >= TARGET_TRADES:
                    break
                if len(aids) != 2:
                    continue
                
                aid_list = list(aids)
                p1 = np.array(aids[aid_list[0]])
                p2 = np.array(aids[aid_list[1]])
                
                if len(p1) < 20 or len(p2) < 20:
                    continue
                
                signals = process_rg_pair_data(p1, p2, n_points_threshold=20)
                
                for sig in signals:
                    if len(trades) >= TARGET_TRADES:
                        break
                    
                    entry_price = sig['entry_price']
                    settlement = sig['settlement']
                    won = sig['won']
                    tier = sig['tier']
                    
                    is_canary = (sig['side'] == 'DOWN' and 0.03 <= entry_price <= 0.08 
                                  and tier in ('canary_btc_down_15m', 'severe_oversold_down', 'oversold_down'))
                    
                    tc = TIER_CONFIG.get(tier, TIER_CONFIG['direction_down_cheap'])
                    
                    if is_canary:
                        position_usd = CANARY_POSITION_USD
                    else:
                        position_usd = min(tc['size_pct'] * bankroll, MAX_POSITION_USD)
                        position_usd = max(position_usd, MIN_TRADE_SIZE)
                    
                    if bankroll <= 2.0:
                        continue
                    
                    eff_price = entry_price + SPREAD_COST + entry_price * SLIPPAGE_PCT
                    eff_price = min(eff_price, 0.99)
                    shares = position_usd / eff_price
                    
                    roll = random.random()
                    if roll < FILL_REJECTION_RATE:
                        global_rejections += 1
                        continue
                    elif roll < FILL_REJECTION_RATE + PARTIAL_FILL_RATE:
                        fill_pct = 0.5 + random.random() * 0.3
                        shares *= fill_pct
                        position_usd *= fill_pct
                        global_partials += 1
                    
                    raw_pnl = shares * (settlement - eff_price)
                    friction = compute_resolution_friction(entry_price, position_usd, settlement, won)
                    net_pnl = raw_pnl - friction
                    friction_total += friction
                    
                    trade = {
                        'tier': tier, 'side': sig['side'], 'entry': eff_price,
                        'shares': shares, 'size_usd': position_usd,
                        'settlement': settlement, 'pnl': net_pnl, 'raw_pnl': raw_pnl,
                        'friction': friction, 'won': won, 'composite': sig['composite'],
                        'rsi': sig['rsi'], 'velocity': sig['velocity'],
                        'direction': sig['direction'], 'regime': sig['regime'],
                        'time_pct': sig['time_pct'], 'is_canary': is_canary,
                    }
                    trades.append(trade)
                    bankroll += net_pnl
                    
                    profile = f"{sig['side']}_{tier}" if not is_canary else "BTC_DOWN_15M_CANARY"
                    profile_stats[profile]['trades'] += 1
                    if won: profile_stats[profile]['wins'] += 1
                    profile_stats[profile]['pnl'] += net_pnl
                    profile_stats[profile]['predictions'].append(tc['base_wr'])
                    profile_stats[profile]['outcomes'].append(1 if won else 0)
                    
                    if is_canary:
                        canary_trades.append(trade)
                        bankroll_canary += net_pnl
                    
                    tier_stats[tier]['trades'] += 1
                    tier_stats[tier]['friction'] += friction
                    if won: tier_stats[tier]['wins'] += 1
                    tier_stats[tier]['pnl'] += net_pnl
                    
                    side_stats[sig['side']]['trades'] += 1
                    if won: side_stats[sig['side']]['wins'] += 1
                    side_stats[sig['side']]['pnl'] += net_pnl
                    
                    score_dist.append(sig['composite'])
                    rsi_dist.append(sig['rsi'])
                    entry_price_dist.append(eff_price)
            
            del pair_prices
        
        file_elapsed = time.time() - fstart
        wr = sum(1 for t in trades if t['won']) / max(len(trades), 1) * 100
        print(f"trades={len(trades)}, WR={wr:.1f}%, bank=${bankroll:.2f} ({file_elapsed:.0f}s)", flush=True)
        gc.collect()
        
        if len(trades) >= TARGET_TRADES:
            break
    
    if len(trades) > TARGET_TRADES:
        trades = trades[:TARGET_TRADES]
    
    # ════════════════════════════════════════════════════════════
    # RESULTS
    # ════════════════════════════════════════════════════════════
    
    total_elapsed = time.time() - start_time
    n_trades = len(trades)
    total_wins = sum(1 for t in trades if t['won'])
    total_pnl = sum(t['pnl'] for t in trades)
    total_raw_pnl = sum(t['raw_pnl'] for t in trades)
    total_friction = sum(t['friction'] for t in trades)
    canary_n = len(canary_trades)
    canary_wins = sum(1 for t in canary_trades if t['won']) if canary_n > 0 else 0
    canary_pnl = sum(t['pnl'] for t in canary_trades) if canary_n > 0 else 0
    
    print("\n" + "=" * 70)
    print("V21.7.18 PMXT 5000-TRADE BACKTEST V2 RESULTS")
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
    print(f"{'Total P&L (net)':<30s} ${total_pnl:>14.2f}")
    print(f"{'Total P&L (raw)':<30s} ${total_raw_pnl:>14.2f}")
    print(f"{'Total friction':<30s} ${total_friction:>14.2f}")
    print(f"{'Friction per trade':<30s} ${total_friction/max(n_trades,1):>14.4f}")
    print(f"{'ROI':<30s} {(bankroll/BANKROLL_START-1)*100:>14.1f}%")
    print(f"{'Avg trade P&L':<30s} ${mean_pnl:>14.4f}")
    print(f"{'Profit factor':<30s} {profit_factor:>15.2f}")
    print(f"{'Sharpe (approx)':<30s} {sharpe:>15.2f}")
    print(f"{'Max win streak':<30s} {best_w:>15d}")
    print(f"{'Max loss streak':<30s} {best_l:>15d}")
    print(f"{'Fill rejections':<30s} {global_rejections:>15d}")
    print(f"{'Partial fills':<30s} {global_partials:>15d}")
    
    # Canary
    print(f"\n{'V21.7.17 CANARY (3-8¢ DOWN, $1)':}")
    print(f"{'Canary trades':<30s} {canary_n:>15d}")
    if canary_n > 0:
        print(f"{'Canary WR':<30s} {canary_wins/canary_n*100:>14.1f}%")
        print(f"{'Canary P&L':<30s} ${canary_pnl:>14.2f}")
        print(f"{'Canary bankroll':<30s} ${bankroll_canary:>14.2f}")
    
    # Side
    print(f"\n{'SIDE BREAKDOWN':}")
    print(f"{'Side':<10s} {'Trades':>8s} {'Wins':>8s} {'WR%':>8s} {'P&L':>12s}")
    print("-" * 50)
    for side in ['UP', 'DOWN']:
        s = side_stats[side]
        if s['trades'] > 0:
            wr = s['wins'] / s['trades'] * 100
            print(f"{side:<10s} {s['trades']:>8d} {s['wins']:>8d} {wr:>7.1f}% ${s['pnl']:>10.2f}")
    
    # Tier
    print(f"\n{'TIER BREAKDOWN':}")
    print(f"{'Tier':<30s} {'Trades':>7s} {'Wins':>7s} {'WR%':>7s} {'P&L':>12s} {'Friction':>10s}")
    print("-" * 75)
    for tier in ['canary_btc_down_15m','severe_oversold_down','severe_overbought_up','oversold_down',
                  'overbought_up','dead_zone_down_cheap','dead_zone_up_cheap','direction_down_cheap','direction_up_cheap']:
        s = tier_stats[tier]
        if s['trades'] > 0:
            t_wr = s['wins'] / s['trades'] * 100
            print(f"{tier:<30s} {s['trades']:>7d} {s['wins']:>7d} {t_wr:>6.1f}% ${s['pnl']:>10.2f} ${s['friction']:>9.2f}")
    
    # Entry price
    if entry_price_dist:
        ep = np.array(entry_price_dist)
        print(f"\n{'ENTRY PRICE DISTRIBUTION':}")
        print(f"  Mean: {np.mean(ep):.4f}, Median: {np.median(ep):.4f}")
        for lo, hi, label in [(0.03,0.08,'3-8¢ CANARY'),(0.08,0.12,'8-12¢'),(0.12,0.20,'12-20¢'),(0.20,0.35,'20-35¢'),(0.35,0.50,'35-50¢')]:
            n = int(np.sum((ep >= lo) & (ep < hi)))
            wr = sum(1 for i,t in enumerate(trades) if lo <= t['entry'] < hi and t['won']) / max(n,1) * 100
            pnl = sum(t['pnl'] for t in trades if lo <= t['entry'] < hi)
            print(f"  {label:<15s}: {n:>5d} trades, {wr:>6.1f}% WR, ${pnl:.2f} P&L")
    
    # RSI
    if rsi_dist:
        rr = np.array(rsi_dist)
        print(f"\n{'RSI DISTRIBUTION':}")
        print(f"  Mean: {np.mean(rr):.1f}")
        for lo, hi, label in [(0,25,'SevereOversold'),(25,35,'Oversold'),(35,65,'DeadZone'),(65,73,'Overbought'),(73,100,'SevereOB')]:
            n = int(np.sum((rr >= lo) & (rr < hi)))
            wr = sum(1 for i,t in enumerate(trades) if lo <= t['rsi'] < hi and t['won']) / max(n,1) * 100
            pnl = sum(t['pnl'] for t in trades if lo <= t['rsi'] < hi)
            print(f"  {label:<18s}: {n:>5d} trades, {wr:>6.1f}% WR, ${pnl:.2f} P&L")
    
    # Profile calibration
    print(f"\n{'V21.7.18 CALIBRATION BY PROFILE':}")
    print(f"{'Profile':<35s} {'N':>5s} {'WR%':>7s} {'PredWR':>7s} {'P&L':>10s} {'Brier':>7s}")
    print("-" * 75)
    for profile in sorted(profile_stats.keys()):
        s = profile_stats[profile]
        if s['trades'] >= 3:
            wr = s['wins'] / s['trades'] * 100
            pred_wr = np.mean(s['predictions']) * 100
            brier = np.mean([(p-o)**2 for p, o in zip(s['predictions'], s['outcomes'])])
            print(f"  {profile:<33s} {s['trades']:>5d} {wr:>6.1f}% {pred_wr:>6.1f}% ${s['pnl']:>9.2f} {brier:>6.4f}")
    
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
    bust = sum(1 for b in mc_finals if b <= 0)
    print(f"  Bust rate: {bust/10:.1f}%")
    print(f"\n  Total time: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")
    
    # Save
    results = {
        'version': 'V21.7.18_PMXT_5000_V2',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'config': {
            'bankroll_start': BANKROLL_START, 'max_position': MAX_POSITION_USD,
            'canary_position': CANARY_POSITION_USD, 'min_composite': MIN_COMPOSITE,
            'spread_cost': SPREAD_COST, 'slippage_pct': SLIPPAGE_PCT,
            'resolution_time_s': RESOLUTION_TIME_S, 'redeem_gas_cost': REDEEM_GAS_COST,
        },
        'summary': {
            'total_trades': n_trades, 'win_rate': overall_wr,
            'final_bankroll': bankroll, 'total_pnl': total_pnl,
            'total_raw_pnl': total_raw_pnl, 'total_friction': total_friction,
            'roi_pct': (bankroll/BANKROLL_START-1)*100,
            'profit_factor': profit_factor, 'sharpe_approx': sharpe,
            'avg_trade_pnl': mean_pnl,
        },
        'canary': {
            'trades': canary_n, 'wins': canary_wins,
            'win_rate': canary_wins/max(canary_n,1)*100 if canary_n > 0 else 0,
            'pnl': canary_pnl, 'final_bankroll': bankroll_canary,
        },
        'tier_stats': {k: dict(v) for k, v in tier_stats.items()},
        'side_stats': side_stats,
        'profile_stats': {k: {'trades': v['trades'], 'wins': v['wins'], 'pnl': v['pnl']} for k, v in profile_stats.items()},
        'monte_carlo': {
            'profitable_pct': mc_profits/10,
            'mean_final': float(np.mean(mc_finals)),
            'median_final': float(np.median(mc_finals)),
            'mean_max_dd_pct': float(np.mean(mc_dds)),
        },
    }
    
    out_file = OUT_DIR / "v21718_pmxt_5000_trade_backtest.json"
    with open(out_file, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {out_file}")
    
    # Calibration
    calibration = {}
    for profile, s in profile_stats.items():
        if s['trades'] >= 5:
            preds = np.array(s['predictions'])
            outcomes = np.array(s['outcomes'])
            brier = float(np.mean((preds - outcomes)**2))
            calibration[profile] = {
                'sample_size': s['trades'],
                'win_rate': float(s['wins'] / s['trades']),
                'predicted_wr': float(np.mean(preds)),
                'brier_score': brier,
                'pnl': float(s['pnl']),
            }
    cal_out = OUT_DIR / "v21718_pmxt_calibration_drift.json"
    with open(cal_out, 'w') as f:
        json.dump({'version': 'V21.7.18_V2', 'profiles': calibration}, f, indent=2, default=str)
    print(f"Calibration saved to {cal_out}")


if __name__ == '__main__':
    np.random.seed(42)
    random.seed(42)
    run_simulation()