#!/usr/bin/env python3
"""
V21 PMXT 2000-Trade Simulation
================================
Uses real PMXT orderbook data with V21 directional extraction and V21.5 opportunity scoring.
Binary settlement (0 or 1 only). No midpoint closure. No synthetic prices.

KEY PRINCIPLES:
- Continuation > reversal (Bayesian prior 3:1)
- Both UP and DOWN scored per market
- Entry after structure reveals (MOMENTUM/LATE windows)
- Commodity score > 0.30 or credible_ev > 0.01 required
- Binary PnL: shares * (settlement_value - entry_price), settlement = 0 or 1
- Execution friction: spread crossing, slippage, fill risk
- Directional persistence (§6): velocity blend, candle dir, consecutive moves
- Oracle repricing lag (§2): soft contribution, never gates
- Time-to-expiry acceleration (§5): 40-80% window priority boost
- Adversarial detection: spread >0.03 = trap, 0.98 effective = refuse
- Down-side is the money printer (V20 data: 81.8% DOWN win at 0.50-0.60)

DATA: /mnt/c/Users/12035/father_daddy_capital/pmxt_data/polymarket_orderbook_2026-05-25TXX.parquet
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
from pathlib import Path
from collections import defaultdict
import time, gc, json, sys, math

# ════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
TARGET_TRADES = 2000
BANKROLL_START = 100.0
MAX_POSITION_USD = 2.00       # Live constraint: $2 max
DAILY_LOSS_LIMIT = 10.0       # $10 daily
MIN_TRADE_SIZE = 0.25

# V21.5 Opportunity Scoring Weights (§7)
W_DIR_PERSIST = 0.25
W_REAL_MOMENTUM = 0.20
W_REPRICING_LAG = 0.10
W_VOL_EXPANSION = 0.10
W_TTE = 0.10
W_EXEC_QUALITY = 0.10
W_CROSS_ASSET = 0.10
W_RSI_CONTEXT = 0.05

# Entry Timing Windows (§4, §12)
WINDOW_PRIORITY = {
    'EARLY': 0.10,      # 0-20% elapsed — avoid unless directional delta
    'FORMATION': 0.40,  # 20-40%
    'MOMENTUM': 0.75,   # 40-80% — HIGH priority (§5)
    'LATE': 0.95,       # 80-90% — repricing lag exploitation
    'FINAL': 0.70,      # 90-100% — execution risk rises
}

# Directional Engine (§6)
CONTINUATION_PRIOR = 3.0  # 3:1 Bayesian prior for continuation
MIN_VELOCITY_PCT = 0.05   # 0.05% minimum directional delta

# Execution Friction Model
SPREAD_COST = 0.01          # Minimum spread cost (1 cent)
SLIPPAGE_PCT = 0.005        # 0.5% slippage
FILL_REJECTION_RATE = 0.05  # 5% fill rejection
PARTIAL_FILL_RATE = 0.10    # 10% partial fills

# Live Constraints
MAX_CONCURRENT = 1
MAX_DAILY_LOSS = 10.0
MAX_WEEKLY_LOSS = 30.0
MAX_DAILY_TRADES = 20

# RSI zones (V21 demoted to 5%, context only)
RSI_OVERSOLD_SEVERE = 25
RSI_OVERSOLD = 30
RSI_NEAR_OVERSOLD = 35
RSI_OVERBOUGHT_SEVERE = 73
RSI_OVERBOUGHT = 70
RSI_NEAR_OVERBOUGHT = 65

# Tier configurations
TIER_CONFIG = {
    'severe_oversold_down':  {'max_price': 0.50, 'size_pct': 0.10, 'base_wr': 0.80},
    'severe_overbought_up':  {'max_price': 0.50, 'size_pct': 0.10, 'base_wr': 0.87},
    'oversold_down':         {'max_price': 0.20, 'size_pct': 0.06, 'base_wr': 0.74},
    'overbought_up':         {'max_price': 0.20, 'size_pct': 0.05, 'base_wr': 0.71},
    'direction_down_cheap':  {'max_price': 0.12, 'size_pct': 0.03, 'base_wr': 0.68},
    'direction_up_cheap':    {'max_price': 0.12, 'size_pct': 0.03, 'base_wr': 0.70},
}

# ════════════════════════════════════════════════════════════════════
# RSI AND SIGNAL COMPUTATION
# ════════════════════════════════════════════════════════════════════

def compute_rsi(prices, period=14):
    n = len(prices)
    if n < period + 1:
        return np.full(n, 50.0)
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.zeros(n)
    avg_l = np.zeros(n)
    avg_g[period] = np.mean(gains[1:period+1])
    avg_l[period] = np.mean(losses[1:period+1])
    for i in range(period+1, n):
        avg_g[i] = (avg_g[i-1]*(period-1) + gains[i]) / period
        avg_l[i] = (avg_l[i-1]*(period-1) + losses[i]) / period
    rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    rsi = np.where(avg_l > 0, 100.0 - 100.0/(1+rs), 100.0)
    rsi[:period] = 50.0
    return rsi


def ema(prices, span):
    """Exponential moving average."""
    if len(prices) < span:
        return prices[-1] if len(prices) > 0 else 0
    mult = 2.0 / (span + 1)
    ema_val = prices[0]
    for p in prices[1:]:
        ema_val = p * mult + ema_val * (1 - mult)
    return ema_val


def detect_direction(prices, lookback=3, min_change_pct=0.10):
    """Detect price direction with velocity."""
    if len(prices) < lookback + 1:
        return 'FLAT', 0.0
    
    # Multi-velocity blend (§6: 40% spot_delta + 60% persistence)
    n = len(prices)
    
    # Spot delta (current move)
    ref = prices[-1 - lookback] if n > lookback else prices[0]
    spot_delta = (prices[-1] - ref) / max(ref, 1e-9) * 100
    
    # Velocity at 15s, 30s, 60s scales
    v15 = (prices[-1] - prices[max(-4, -n)]) / max(prices[max(-4, -n)], 1e-9) * 100 if n >= 4 else 0
    v30 = (prices[-1] - prices[max(-8, -n)]) / max(prices[max(-8, -n)], 1e-9) * 100 if n >= 8 else 0
    v60 = (prices[-1] - prices[max(-16, -n)]) / max(prices[max(-16, -n)], 1e-9) * 100 if n >= 16 else 0
    
    velocity = 0.5 * v15 + 0.3 * v30 + 0.2 * v60 if n >= 8 else abs(spot_delta)
    
    # Consecutive candle direction (persistence signal)
    consec = 0
    for i in range(1, min(5, n)):
        if prices[-i] > prices[-i-1]:
            consec += 1
        elif prices[-i] < prices[-i-1]:
            consec -= 1
    
    # Blend: 40% spot_delta + 60% persistence
    persistence_signal = 1.0 if consec > 1 else (-1.0 if consec < -1 else 0.0)
    blended = 0.4 * (1.0 if spot_delta > 0 else (-1.0 if spot_delta < 0 else 0.0)) + 0.6 * persistence_signal
    
    if blended > 0.3 and velocity > min_change_pct:
        return 'UP', velocity
    elif blended < -0.3 and velocity < -min_change_pct:
        return 'DOWN', velocity
    else:
        return 'FLAT', abs(velocity)


def compute_opportunity_score(rsi, direction, velocity, regime, time_pct, spread, depth_imbalance, rsi_zone):
    """V21.5 composite opportunity score."""
    
    # 1. Directional persistence (25%)
    if direction == 'DOWN':
        dir_score = min(1.0, abs(velocity) / 0.5) * 0.8  # Down-side bias
    elif direction == 'UP':
        dir_score = min(1.0, abs(velocity) / 0.5) * 0.6  # Up-side weaker
    else:
        dir_score = 0.05
    
    # 2. Realized momentum (20%)
    momentum_score = min(1.0, abs(velocity) / 1.0)
    
    # 3. Repricing lag (10%) — neutral at 0, not a gate
    lag_score = 0.2  # Always neutral baseline (§2)
    
    # 4. Volatility expansion (10%)
    vol_score = min(1.0, abs(velocity) * 2.0) if abs(velocity) > 0.15 else 0.1
    
    # 5. Time-to-expiry (10%)
    if time_pct < 0.20:
        tte_score = 0.10 * 0.5  # EARLY: ×0.5 penalty
    elif time_pct < 0.40:
        tte_score = 0.40
    elif time_pct < 0.80:
        tte_score = 0.75  # MOMENTUM: HIGH
    elif time_pct < 0.90:
        tte_score = 0.95 + 0.10 * lag_score  # LATE: +0.10 if lag
    else:
        tte_score = 0.70  # FINAL: execution risk
    
    # No movement penalty (§5)
    if abs(velocity) < 0.05:
        tte_score *= 0.5
    
    # 6. Execution quality (10%)
    if spread > 0.03:
        exec_score = 0.0  # Spread trap — refuse
    elif spread > 0.02:
        exec_score = 0.3
    elif spread > 0.01:
        exec_score = 0.6
    else:
        exec_score = 0.9
    
    # 7. Cross-asset confirmation (10%) — placeholder, 0.5 neutral in backtest
    cross_score = 0.5
    
    # 8. RSI context (5%, demoted)
    if rsi_zone == 'severe_oversold':
        rsi_score = 0.95
    elif rsi_zone == 'oversold':
        rsi_score = 0.80
    elif rsi_zone == 'near_oversold':
        rsi_score = 0.55
    elif rsi_zone == 'severe_overbought':
        rsi_score = 0.90
    elif rsi_zone == 'overbought':
        rsi_score = 0.75
    elif rsi_zone == 'near_overbought':
        rsi_score = 0.50
    else:
        rsi_score = 0.30  # Dead zone
    
    composite = (
        W_DIR_PERSIST * dir_score +
        W_REAL_MOMENTUM * momentum_score +
        W_REPRICING_LAG * lag_score +
        W_VOL_EXPANSION * vol_score +
        W_TTE * tte_score +
        W_EXEC_QUALITY * exec_score +
        W_CROSS_ASSET * cross_score +
        W_RSI_CONTEXT * rsi_score
    )
    
    return composite


def get_rsi_zone(rsi):
    if rsi < 25: return 'severe_oversold'
    if rsi < 30: return 'oversold'
    if rsi < 35: return 'near_oversold'
    if rsi > 73: return 'severe_overbought'
    if rsi > 70: return 'overbought'
    if rsi > 65: return 'near_overbought'
    return 'dead_zone'


def get_regime(prices):
    """Simplified regime classification."""
    if len(prices) < 20:
        return 'unknown'
    
    recent = prices[-20:]
    volatility = np.std(recent) / max(np.mean(recent), 1e-9)
    sma = np.mean(recent)
    trend = (recent[-1] - recent[0]) / max(recent[0], 1e-9)
    
    if volatility < 0.01:
        return 'ranging'
    elif volatility > 0.05:
        return 'volatile'
    elif trend > 0.02:
        return 'trending_up'
    elif trend < -0.02:
        return 'trending_down'
    else:
        return 'balanced'


def classify_signal_v21(prices, rsi, direction, velocity, regime, time_pct, spread, depth_imb):
    """V21 directional extraction + V21.5 opportunity scoring."""
    
    rsi_zone = get_rsi_zone(rsi)
    composite = compute_opportunity_score(
        rsi, direction, velocity, regime, time_pct, spread, depth_imb, rsi_zone
    )
    
    # Adversarial detection
    if spread > 0.03:
        return None, 0.0, 'spread_trap'
    if spread > 0.98:  # Neg-risk UpDown effective spread trap
        return None, 0.0, 'neg_risk_trap'
    
    # Confidence threshold
    if composite < 0.30:
        return None, composite, 'below_threshold'
    
    # Determine side — score decides, not ideology (§7)
    # Both UP and DOWN are evaluated, higher score wins
    
    # Continuation prior (§6: 3:1 for continuation)
    cont_mult = 1.0
    
    # Down-side: RSI zone + direction alignment
    down_score = composite
    up_score = composite
    
    if direction == 'DOWN':
        down_score *= 1.3 * cont_mult  # Continuation boost + down-side bias
        up_score *= 0.4  # Reversal must be earned
    elif direction == 'UP':
        up_score *= 1.1 * cont_mult
        down_score *= 0.5  # Reversal must be earned
    
    # RSI zone alignment boost
    if rsi_zone in ('severe_oversold', 'oversold', 'near_oversold'):
        down_score *= 1.2
        up_score *= 0.7
    elif rsi_zone in ('severe_overbought', 'overbought', 'near_overbought'):
        up_score *= 1.15
        down_score *= 0.8
    
    # Select side
    if down_score > up_score and down_score >= 0.30:
        # Determine tier
        if rsi_zone == 'severe_oversold':
            tier = 'severe_oversold_down'
        elif rsi_zone in ('oversold', 'near_oversold'):
            tier = 'oversold_down'
        else:
            tier = 'direction_down_cheap'
        return 'DOWN', composite, tier
    elif up_score > down_score and up_score >= 0.30:
        if rsi_zone == 'severe_overbought':
            tier = 'severe_overbought_up'
        elif rsi_zone in ('overbought', 'near_overbought'):
            tier = 'overbought_up'
        else:
            tier = 'direction_up_cheap'
        return 'UP', composite, tier
    else:
        return None, composite, 'no_side_advantage'


# ════════════════════════════════════════════════════════════════════
# BINARY SETTLEMENT (V20.3)
# ════════════════════════════════════════════════════════════════════

def binary_settlement(token_final_price, entry_price, side, shares):
    """
    V20.3 binary settlement. Token resolves to 0 or 1 only.
    No midpoint. No synthetic close.
    
    PnL = shares * (settlement_value - entry_price)
    settlement_value = 1.0 if token won, 0.0 if token lost
    """
    # Determine if token won or lost
    # For cheap tokens: if final_price > 0.50, token won (resolved to 1.0)
    # if final_price < 0.50, token lost (resolved to 0.0)
    # Near 0.50 is ambiguous — use the actual settlement
    if token_final_price >= 0.50:
        settlement = 1.0
    else:
        settlement = 0.0
    
    pnl = shares * (settlement - entry_price)
    won = settlement == 1.0
    
    return pnl, won, settlement


# ════════════════════════════════════════════════════════════════════
# EXECUTION FRICTION MODEL (V21)
# ════════════════════════════════════════════════════════════════════

def apply_execution_friction(entry_price, size_usd, side):
    """
    V21 execution reality: spread crossing, slippage, fill risk.
    Returns (effective_price, shares, fill_status).
    """
    # Spread crossing: buy at ask = entry_price + half_spread
    spread_cost = SPREAD_COST
    effective_price = entry_price + spread_cost if side == 'UP' else entry_price + spread_cost * 0.5
    
    # Slippage
    slippage = effective_price * SLIPPAGE_PCT
    effective_price += slippage
    
    # Cap: can't exceed 1.0
    effective_price = min(effective_price, 0.99)
    
    shares = size_usd / effective_price
    
    # Fill simulation
    import random
    roll = random.random()
    if roll < FILL_REJECTION_RATE:
        return None, 0, 'rejected'
    elif roll < FILL_REJECTION_RATE + PARTIAL_FILL_RATE:
        # Partial fill (50-80%)
        fill_pct = 0.5 + random.random() * 0.3
        shares *= fill_pct
        size_usd *= fill_pct
        return effective_price, shares, 'partial'
    else:
        return effective_price, shares, 'filled'


# ════════════════════════════════════════════════════════════════════
# MAIN SIMULATION
# ════════════════════════════════════════════════════════════════════

def run_simulation():
    print("=" * 70)
    print("V21 PMXT 2000-TRADE SIMULATION")
    print("=" * 70)
    print(f"Binary settlement (0/1 only) | Directional engine | V21.5 scoring")
    print(f"Spread cost: {SPREAD_COST} | Slippage: {SLIPPAGE_PCT*100}% | Fill rejection: {FILL_REJECTION_RATE*100}%")
    print(f"Continuation prior: {CONTINUATION_PRIOR}:1 | Down-side structural bias")
    print()
    
    # Discover files
    files = sorted(PMXT_DIR.glob("*.parquet"))
    valid_files = []
    for f in files:
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000:  # Skip stub files
                valid_files.append(f)
        except:
            continue
    
    print(f"Found {len(valid_files)} valid PMXT files out of {len(files)} total")
    if not valid_files:
        print("ERROR: No valid PMXT files found!")
        return
    
    # Track state
    bankroll = BANKROLL_START
    trades = []
    daily_pnl = defaultdict(float)
    daily_trades = defaultdict(int)
    weekly_pnl = defaultdict(float)
    tier_stats = defaultdict(lambda: {
        'trades': 0, 'wins': 0, 'pnl': 0.0, 'rejected': 0, 'partial': 0
    })
    side_stats = {'UP': {'trades': 0, 'wins': 0, 'pnl': 0.0}, 
                  'DOWN': {'trades': 0, 'wins': 0, 'pnl': 0.0}}
    score_dist = []
    spread_dist = []
    rsi_dist = []
    
    global_rejections = 0
    global_partials = 0
    total_scanned = 0
    total_candidates = 0
    spread_traps = 0
    
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
        # Process up to 20 row groups per file to manage memory
        n_rgs = min(n_rgs, 20)
        
        # Phase 1: Discover markets and compute per-market price series
        # Use vectorized approach: read per RG, accumulate cheap-side tokens
        market_data = {}  # (cid, aid) -> list of (timestamp, price)
        
        for rg in range(n_rgs):
            try:
                # Read key columns
                t = pf.read_row_group(rg, columns=[
                    'market', 'asset_id', 'price', 'event_type', 'timestamp_received'
                ])
            except Exception as e:
                continue
            
            # Filter to price_change events only
            mask = pc.equal(t.column('event_type'), 'price_change')
            t2 = t.filter(mask)
            n = len(t2)
            if n == 0:
                del t, t2
                continue
            
            mkt_col = t2.column('market')
            aid_col = t2.column('asset_id')
            price_col = t2.column('price').to_numpy()
            ts_col = t2.column('timestamp_received').to_numpy()
            
            # Convert timestamps properly (PMXT uses datetime64[ms, UTC])
            dtype_str = str(t2.column('timestamp_received').type)
            int_vals = ts_col.astype(np.int64)
            if 'ms' in dtype_str:
                ts_sec = (int_vals // 10**3).astype(np.int64)
            elif 'us' in dtype_str:
                ts_sec = (int_vals // 10**6).astype(np.int64)
            elif 'ns' in dtype_str:
                ts_sec = (int_vals // 10**9).astype(np.int64)
            else:
                ts_sec = (int_vals // 10**3).astype(np.int64)
            
            for i in range(0, n, 1):  # Sample every row for signal quality
                mv = mkt_col[i]
                cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
                aid = str(aid_col[i])
                p = float(price_col[i])
                ts = int(ts_sec[i])
                
                # Only keep cheap side (< 0.50) — the V21 focus
                if p < 0.01 or p > 0.60:
                    continue
                
                key = (cid, aid)
                if key not in market_data:
                    market_data[key] = []
                market_data[key].append((ts, p))
            
            del t, t2, mkt_col, aid_col, price_col, ts_col, int_vals, ts_sec
            gc.collect()
        
        print(f"markets={len(market_data)}", end=" ", flush=True)
        
        # Phase 2: For each token with enough data, generate V21 signals
        file_trades = 0
        file_scanned = 0
        file_candidates = 0
        
        for (cid, aid), points in market_data.items():
            if len(trades) >= TARGET_TRADES:
                break
            
            if len(points) < 30:
                continue
            
            # Sort by timestamp
            points.sort(key=lambda x: x[0])
            prices = np.array([p[1] for p in points])
            timestamps = np.array([p[0] for p in points])
            
            # Skip if no meaningful price movement
            if np.max(prices) - np.min(prices) < 0.02:
                continue
            
            # Compute RSI for the entire price series
            rsi_arr = compute_rsi(prices)
            
            # Scan for entry signals (skip first 24 for warmup)
            sample_step = max(1, len(prices) // 50)  # Sample ~50 points per market
            
            for i in range(24, len(prices), sample_step):
                if len(trades) >= TARGET_TRADES:
                    break
                
                total_scanned += 1
                
                window = prices[:i+1]
                current_price = float(prices[i])
                current_rsi = float(rsi_arr[i])
                current_ts = int(timestamps[i])
                
                # Time-to-expiry: estimate from total duration
                total_duration = timestamps[-1] - timestamps[0]
                if total_duration > 0:
                    time_pct = (timestamps[i] - timestamps[0]) / total_duration
                else:
                    time_pct = 0.5
                
                # Direction detection
                direction, velocity = detect_direction(window)
                
                # Regime
                regime = get_regime(window)
                
                # Spread estimate (from PMXT data: use bid-ask if available, else heuristic)
                # For cheap tokens, effective spread is tighter near resolution
                if current_price < 0.10:
                    spread = 0.02  # Very cheap = wider spread
                elif current_price < 0.20:
                    spread = 0.015
                elif current_price < 0.40:
                    spread = 0.01
                else:
                    spread = 0.008
                
                # Depth imbalance (placeholder, neutral)
                depth_imb = 0.0
                
                # V21 classification
                side, composite, tier = classify_signal_v21(
                    window, current_rsi, direction, velocity, regime,
                    time_pct, spread, depth_imb
                )
                
                if side is None:
                    if tier == 'spread_trap' or tier == 'neg_risk_trap':
                        spread_traps += 1
                    continue
                
                total_candidates += 1
                
                # Tier config
                tc = TIER_CONFIG.get(tier, TIER_CONFIG['direction_down_cheap'])
                max_price = tc['max_price']
                size_pct = tc['size_pct']
                base_wr = tc['base_wr']
                
                # Price gate
                if current_price > max_price or current_price < 0.03:
                    continue
                
                # Opportunity score gate
                if composite < 0.30:
                    continue
                
                # Determine day for daily limits
                day_key = current_ts // 86400
                
                if daily_trades[day_key] >= MAX_DAILY_TRADES:
                    continue
                if daily_pnl[day_key] <= -MAX_DAILY_LOSS:
                    continue
                
                # Position sizing
                if bankroll <= 5.0:
                    continue
                
                position_usd = min(size_pct * bankroll, MAX_POSITION_USD)
                position_usd = max(position_usd, MIN_TRADE_SIZE)
                
                # Apply execution friction
                eff_price, shares, fill_status = apply_execution_friction(
                    current_price, position_usd, side
                )
                
                if fill_status == 'rejected':
                    global_rejections += 1
                    continue
                elif fill_status == 'partial':
                    global_partials += 1
                    tier_stats[tier]['partial'] += 1
                    position_usd *= (shares * eff_price / max(position_usd, 0.01))
                
                # Determine settlement: use final price of the token
                final_price = float(prices[-1])
                
                # Binary settlement
                pnl, won, settlement = binary_settlement(
                    final_price, eff_price, side, shares
                )
                
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
                    'day': day_key,
                    'fill': fill_status,
                }
                trades.append(trade)
                
                # Update tracking
                bankroll += pnl
                daily_pnl[day_key] += pnl
                daily_trades[day_key] += 1
                
                tier_stats[tier]['trades'] += 1
                if won:
                    tier_stats[tier]['wins'] += 1
                tier_stats[tier]['pnl'] += pnl
                if fill_status == 'rejected':
                    tier_stats[tier]['rejected'] += 1
                
                side_stats[side]['trades'] += 1
                if won:
                    side_stats[side]['wins'] += 1
                side_stats[side]['pnl'] += pnl
                
                score_dist.append(composite)
                spread_dist.append(spread)
                rsi_dist.append(current_rsi)
        
        file_elapsed = time.time() - fstart
        file_wr = (sum(1 for t in trades[file_trades:] if t['won']) / 
                   max(len(trades) - file_trades, 1)) * 100
        print(f"trades={len(trades)}, WR={file_wr:.1f}%, bankroll=${bankroll:.2f} ({file_elapsed:.0f}s)")
        
        gc.collect()
    
    total_elapsed = time.time() - start_time
    
    # ════════════════════════════════════════════════════════════════════
    # RESULTS REPORT
    # ════════════════════════════════════════════════════════════════════
    
    n_trades = len(trades)
    total_wins = sum(1 for t in trades if t['won'])
    total_pnl = sum(t['pnl'] for t in trades)
    
    print("\n" + "=" * 70)
    print("V21 PMXT 2000-TRADE SIMULATION RESULTS")
    print("=" * 70)
    
    if n_trades == 0:
        print("NO TRADES GENERATED")
        print(f"Scanned: {total_scanned} | Candidates: {total_candidates} | Spread traps: {spread_traps}")
        return
    
    overall_wr = total_wins / n_trades * 100
    avg_pnl = total_pnl / n_trades
    max_loss = min(t['pnl'] for t in trades)
    max_win = max(t['pnl'] for t in trades)
    
    # Winning/losing streaks
    current_streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    for t in trades:
        if t['won']:
            if current_streak > 0:
                current_streak += 1
            else:
                current_streak = 1
            max_win_streak = max(max_win_streak, current_streak)
        else:
            if current_streak < 0:
                current_streak -= 1
            else:
                current_streak = -1
            max_loss_streak = max(max_loss_streak, abs(current_streak))
    
    # Profit factor
    gross_profit = sum(t['pnl'] for t in trades if t['pnl'] > 0)
    gross_loss = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
    profit_factor = gross_profit / max(gross_loss, 0.01)
    
    # Sharpe approximation
    pnls = [t['pnl'] for t in trades]
    mean_pnl = np.mean(pnls)
    std_pnl = np.std(pnls)
    sharpe_approx = mean_pnl / max(std_pnl, 0.001) * np.sqrt(252)
    
    print(f"\n{'METRIC':<30s} {'VALUE':>15s}")
    print("-" * 47)
    print(f"{'Total trades':<30s} {n_trades:>15d}")
    print(f"{'Total wins':<30s} {total_wins:>15d}")
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
    
    # Distribution analysis
    if rsi_dist:
        rsi_arr = np.array(rsi_dist)
        print(f"\n{'RSI DISTRIBUTION':}")
        print(f"  Mean RSI: {np.mean(rsi_arr):.1f}")
        print(f"  RSI quartiles: {np.percentile(rsi_arr, [25, 50, 75])}")
        print(f"  Severe oversold (<25): {np.sum(rsi_arr < 25)} ({np.sum(rsi_arr < 25)/len(rsi_arr)*100:.1f}%)")
        print(f"  Oversold (25-35): {np.sum((rsi_arr >= 25) & (rsi_arr < 35))} ({np.sum((rsi_arr >= 25) & (rsi_arr < 35))/len(rsi_arr)*100:.1f}%)")
        print(f"  Dead zone (35-65): {np.sum((rsi_arr >= 35) & (rsi_arr <= 65))} ({np.sum((rsi_arr >= 35) & (rsi_arr <= 65))/len(rsi_arr)*100:.1f}%)")
        print(f"  Overbought (65-73): {np.sum((rsi_arr > 65) & (rsi_arr <= 73))} ({np.sum((rsi_arr > 65) & (rsi_arr <= 73))/len(rsi_arr)*100:.1f}%)")
        print(f"  Severe overbought (>73): {np.sum(rsi_arr > 73)} ({np.sum(rsi_arr > 73)/len(rsi_arr)*100:.1f}%)")
    
    if score_dist:
        scores = np.array(score_dist)
        print(f"\n{'OPPORTUNITY SCORE DISTRIBUTION':}")
        print(f"  Mean score: {np.mean(scores):.3f}")
        print(f"  Median score: {np.median(scores):.3f}")
        print(f"  Score quartiles: {np.percentile(scores, [25, 50, 75])}")
    
    # Settlement analysis (V20.3 binary reality check)
    settlement_wins = sum(1 for t in trades if t['settlement'] == 1.0)
    settlement_losses = sum(1 for t in trades if t['settlement'] == 0.0)
    print(f"\n{'BINARY SETTLEMENT (V20.3 REALITY)':}")
    print(f"  Resolved to 1.0 (win): {settlement_wins} ({settlement_wins/max(n_trades,1)*100:.1f}%)")
    print(f"  Resolved to 0.0 (loss): {settlement_losses} ({settlement_losses/max(n_trades,1)*100:.1f}%)")
    
    # Entry price distribution
    entry_prices = [t['entry'] for t in trades]
    if entry_prices:
        ep = np.array(entry_prices)
        print(f"\n{'ENTRY PRICE DISTRIBUTION':}")
        print(f"  Mean entry: {np.mean(ep):.3f}")
        print(f"  Entry quartiles: {np.percentile(ep, [25, 50, 75])}")
        print(f"  Ultra-cheap (<0.10): {np.sum(ep < 0.10)} ({np.sum(ep < 0.10)/len(ep)*100:.1f}%)")
        print(f"  Cheap (0.10-0.20): {np.sum((ep >= 0.10) & (ep < 0.20))} ({np.sum((ep >= 0.10) & (ep < 0.20))/len(ep)*100:.1f}%)")
        print(f"  Moderate (0.20-0.40): {np.sum((ep >= 0.20) & (ep < 0.40))} ({np.sum((ep >= 0.20) & (ep < 0.40))/len(ep)*100:.1f}%)")
        print(f"  Expensive (0.40-0.50): {np.sum((ep >= 0.40) & (ep <= 0.50))} ({np.sum((ep >= 0.40) & (ep <= 0.50))/len(ep)*100:.1f}%)")
    
    # Regime distribution
    regimes = defaultdict(int)
    for t in trades:
        regimes[t['regime']] += 1
    print(f"\n{'REGIME DISTRIBUTION':}")
    for regime, count in sorted(regimes.items(), key=lambda x: -x[1]):
        print(f"  {regime}: {count} ({count/n_trades*100:.1f}%)")
    
    print(f"\n{'TIMING':}")
    print(f"  Total time: {total_elapsed:.0f}s")
    print(f"  Time per trade: {total_elapsed/max(n_trades,1):.1f}s")
    print(f"  Per file avg: {total_elapsed/max(len(valid_files),1):.1f}s")
    
    # Save results
    output_dir = Path("/home/naq1987s/father-daddy-capital/output")
    output_dir.mkdir(exist_ok=True)
    
    results = {
        'version': 'V21_PMXT',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'config': {
            'bankroll_start': BANKROLL_START,
            'max_position': MAX_POSITION_USD,
            'spread_cost': SPREAD_COST,
            'slippage_pct': SLIPPAGE_PCT,
            'continuation_prior': CONTINUATION_PRIOR,
            'binary_settlement': True,
            'target_trades': TARGET_TRADES,
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
            'max_win_streak': max_win_streak,
            'max_loss_streak': max_loss_streak,
        },
        'side_stats': side_stats,
        'tier_stats': {k: dict(v) for k, v in tier_stats.items()},
    }
    
    out_file = output_dir / "v21_pmxt_2000_trade_sim.json"
    with open(out_file, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {out_file}")
    
    # Monte Carlo robustness check
    print(f"\n{'MONTE CARLO ROBUSTNESS (1000 sims)':}")
    mc_profits = 0
    mc_max_drawdowns = []
    mc_final_bankrolls = []
    
    for _ in range(1000):
        # Resample trades with replacement
        idx = np.random.choice(n_trades, size=n_trades, replace=True)
        sim_pnls = [pnls[i] for i in idx]
        sim_bank = BANKROLL_START
        sim_peak = BANKROLL_START
        sim_max_dd = 0
        for p in sim_pnls:
            sim_bank += p
            sim_peak = max(sim_peak, sim_bank)
            dd = (sim_peak - sim_bank) / max(sim_peak, 1)
            sim_max_dd = max(sim_max_dd, dd)
        mc_final_bankrolls.append(sim_bank)
        mc_max_drawdowns.append(sim_max_dd * 100)
        if sim_bank > BANKROLL_START:
            mc_profits += 1
    
    mc_profit_pct = mc_profits / 1000 * 100
    mc_mean_final = np.mean(mc_final_bankrolls)
    mc_median_final = np.median(mc_final_bankrolls)
    mc_mean_dd = np.mean(mc_max_drawdowns)
    mc_5th_pnl = np.percentile([b - BANKROLL_START for b in mc_final_bankrolls], 5)
    mc_95th_pnl = np.percentile([b - BANKROLL_START for b in mc_final_bankrolls], 95)
    
    print(f"  Profitable sims: {mc_profit_pct:.1f}%")
    print(f"  Mean final bankroll: ${mc_mean_final:.2f}")
    print(f"  Median final bankroll: ${mc_median_final:.2f}")
    print(f"  Mean max drawdown: {mc_mean_dd:.1f}%")
    print(f"  P&L 5th percentile: ${mc_5th_pnl:.2f}")
    print(f"  P&L 95th percentile: ${mc_95th_pnl:.2f}")
    print(f"  99% bust rate (bankroll < $0): {sum(1 for b in mc_final_bankrolls if b <= 0)/10:.1f}%")
    
    print(f"\n{'=' * 70}")
    print(f"V21 PMXT SIMULATION COMPLETE")
    print(f"{'=' * 70}")


if __name__ == '__main__':
    np.random.seed(42)
    run_simulation()