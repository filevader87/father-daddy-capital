#!/usr/bin/env python3
"""
V19.7 Historical Backtest — Multi-TF + Candle Confirmation + Session Filters
===============================================================================
V19.6 base (≤8¢ ONLY, cheap-side flip) PLUS:
1. Multi-TF RSI: 5m RSI + 15m RSI + 1h RSI alignment check
2. Candle-body confirmation: signal candle body must confirm direction
3. Session filter: NY + Asia only (block London 51.3%, off_hours 42.9%)
4. Strategy filter: block overbought_up + direction_up_cheap (53.3%, 51.9% WR)
5. Bankroll $320 (new funding round)
"""
import json, os, sys, math, time, random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
import numpy as np

try:
    import urllib.request
    HAS_URLLIB = True
except:
    HAS_URLLIB = False

OUTPUT = Path(__file__).parent / "output"
OUTPUT.mkdir(exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════
# V19.7 CONFIG
# ═══════════════════════════════════════════════════════════════════════
BANKROLL_START = 320.0   # New funding round
MIN_CONFLUENCE = 7.0

# V19.6: ≤8¢ ONLY
CHEAP_MAX = 0.08
ULTRA_CHEAP_MAX = 0.08
MID_CHEAP_MAX = 0.08

STOP_LOSS_PCT = 0.50
TAKE_PROFIT_PRICE = 0.90
TRAILING_STOP_PCT = 0.40
TRAILING_ACTIVATE_MINS = 2.0
MAX_OPEN_POSITIONS = 3
MAX_SAME_DIRECTION = 2
DAILY_LOSS_LIMIT = 3
DAILY_LOSS_PCT = 0.07
COOLDOWN_MINS = 15
MAX_BET_SIZE = 20.0

CONFLUENCE_SIZING = {7: 0.04, 8: 0.05, 9: 0.06}
BASE_SIZE = 0.03

FLIP_TO_CHEAP_SIDE = True

# V19.7c: NY hours only (block Asia 50.3%, London, off_hours)
BLOCKED_SESSIONS = {'london', 'london_open', 'london_close', 'off_hours', 'asia'}

# V19.7c: Allow all strategies — blocking hurt WR. Confluence ≥8 is the filter.
BLOCKED_STRATEGIES = set()

# V19.7d: MIN_CONF=8 (conf 8/10 = 58.7%, conf 9 = 57.2%)
# Higher conf doesn't help — conf 9 barely better. Keep at 8.
MIN_CONFLUENCE = 8.0

# V19.7: Multi-TF RSI confirmation
MULTI_TF_BONUS = True

# V19.7: Candle-body confirmation
# For cheap-side flips: if we buy UP (signal DOWN, buy cheap UP),
# we DON'T need a green candle — the red candle IS the signal.
# Only reject if candle body is strongly OPPOSITE to our CHEAP side direction.
CANDLE_BODY_CONFIRM = True
CANDLE_BODY_MIN_PCT = 0.10  # Only reject if opposing body > 0.10% (BTC typical body is 0.1-0.5%)

# ═══════════════════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════════════════
def compute_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    deltas = np.diff(prices[-(period+1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def compute_ema(prices, period):
    if len(prices) < period: return prices[-1] if len(prices) > 0 else 0
    multiplier = 2 / (period + 1)
    ema = np.mean(prices[:period])
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def compute_vwap(candles, n=20):
    recent = candles[-n:] if len(candles) >= n else candles
    total_vol = sum(c.get('volume', 1) for c in recent)
    if total_vol == 0: return candles[-1]['close']
    total_vp = sum(c.get('volume', 1) * (c.get('high', c['close']) + c.get('low', c['close']) + c['close']) / 3 for c in recent)
    return total_vp / total_vol

def compute_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal: return 0.0
    ema_fast = prices[0]
    ema_slow = prices[0]
    k_fast = 2 / (fast + 1)
    k_slow = 2 / (slow + 1)
    macd_line = []
    for p in prices:
        ema_fast = (p - ema_fast) * k_fast + ema_fast
        ema_slow = (p - ema_slow) * k_slow + ema_slow
        macd_line.append(ema_fast - ema_slow)
    sig = np.mean(macd_line[-signal:])
    hist = macd_line[-1] - sig
    return hist / prices[-1] if prices[-1] > 0 else 0

def get_regime(prices):
    if len(prices) < 20: return 'ranging'
    rets = np.diff(prices[-21:]) / prices[-21:-1]
    mu = np.mean(rets)
    sigma = np.std(rets)
    if sigma < 0.0005: return 'ranging'
    if mu > 2 * sigma: return 'trending_up'
    if mu < -2 * sigma: return 'trending_down'
    if sigma > 0.003: return 'volatile'
    return 'ranging'

def classify_volatility(atr, price):
    if price <= 0: return ('low_vol', 0.20)
    pct = atr / price
    if pct > 0.005: return ('high_vol', 0.08)
    elif pct > 0.002: return ('medium_vol', 0.15)
    return ('low_vol', 0.20)

def get_session(utc_hour):
    est_hour = (utc_hour - 5) % 24
    if 19 <= est_hour or est_hour < 3:   return ('asia', 0.7)
    elif 2 <= est_hour < 5:               return ('london_open', 0.8)
    elif 7 <= est_hour < 12:              return ('new_york', 1.0)
    elif 10 <= est_hour < 12:             return ('london_close', 0.8)
    elif 3 <= est_hour < 7:               return ('off_hours', 0.5)
    elif 12 <= est_hour < 19:             return ('ny_afternoon', 0.6)
    return ('off_hours', 0.5)

def detect_direction(prices, lookback=3, min_change_pct=0.10):
    if len(prices) < lookback + 1: return 'FLAT', 0.0
    recent = prices[-(lookback+1):-1]
    if len(recent) < 2: return 'FLAT', 0.0
    change_pct = (recent[-1] - recent[0]) / recent[0] * 100
    if change_pct > min_change_pct: return 'UP', change_pct
    elif change_pct < -min_change_pct: return 'DOWN', change_pct
    return 'FLAT', change_pct

def compute_confluence(rsi, direction, regime, ema21, ema50, vwap, price, macd_hist, session, atr_vol, signal_dir,
                      rsi_15m=None, rsi_1h=None):
    score = 0.0
    details = []
    
    # 1. RSI extreme
    if signal_dir == 'DOWN' and rsi < 25: score += 1.0; details.append('RSI<25')
    elif signal_dir == 'DOWN' and rsi < 30: score += 0.8; details.append('RSI<30')
    elif signal_dir == 'DOWN' and rsi < 38: score += 0.3; details.append('RSI<38')
    elif signal_dir == 'UP' and rsi > 75: score += 1.0; details.append('RSI>75')
    elif signal_dir == 'UP' and rsi > 65: score += 0.8; details.append('RSI>65')
    elif signal_dir == 'UP' and rsi > 55: score += 0.3; details.append('RSI>55')
    else: details.append('RSI_meh')
    
    # 2. Direction alignment
    if direction in ('UP', 'DOWN'): score += 1.0; details.append(f'Dir={direction}')
    else: details.append('Dir=FLAT')
    
    # 3. EMA alignment
    ema_diff = abs(ema21 - ema50) / ema50 if ema50 > 0 else 0
    if ema21 > ema50 and signal_dir == 'UP': score += 1.0; details.append('EMA_bullish')
    elif ema21 < ema50 and signal_dir == 'DOWN': score += 1.0; details.append('EMA_bearish')
    elif ema_diff < 0.001: score += 0.3; details.append('EMA_flat')
    
    # 4. VWAP alignment
    if signal_dir == 'UP' and price > vwap: score += 1.0; details.append('Above_VWAP')
    elif signal_dir == 'DOWN' and price < vwap: score += 1.0; details.append('Below_VWAP')
    elif signal_dir == 'UP' and price < vwap * 1.002: score += 0.5; details.append('Near_VWAP')
    
    # 5. MACD
    if signal_dir == 'UP' and macd_hist > 0: score += 1.0; details.append('MACD_expanding')
    elif signal_dir == 'DOWN' and macd_hist < 0: score += 1.0; details.append('MACD_declining')
    elif abs(macd_hist) < 0.0001: score += 0.3
    
    # 6. Session weight
    session_name, session_weight = session
    score += session_weight
    details.append(f'S={session_name[:3]}({session_weight:.1f})')
    
    # 7. Volatility bonus
    vol_regime, max_entry = atr_vol
    if vol_regime == 'high_vol' and signal_dir in ('UP', 'DOWN'): score += 1.0
    elif vol_regime == 'low_vol' and regime in ('trending_up', 'trending_down'): score += 0.8
    elif vol_regime == 'medium_vol': score += 0.5
    
    # 8. Regime alignment
    if regime == 'trending_up' and signal_dir == 'UP': score += 1.0; details.append('Trend_aligned')
    elif regime == 'trending_down' and signal_dir == 'DOWN': score += 1.0; details.append('Trend_aligned')
    
    # 9. RSI zone bonus
    if signal_dir == 'DOWN' and rsi < 40: score += 0.5
    elif signal_dir == 'UP' and rsi > 60: score += 0.5
    
    # 10. Price vs VWAP
    if signal_dir == 'DOWN' and price < vwap: score += 1.0
    elif signal_dir == 'UP' and price > vwap: score += 1.0
    else: score += 0.3
    
    # ═══ V19.7 NEW: Multi-TF RSI confirmation ═══
    if MULTI_TF_BONUS and rsi_15m is not None:
        aligned_15m = False
        if signal_dir == 'DOWN' and rsi_15m < 40:
            score += 1.0
            details.append('RSI15m aligned')
            aligned_15m = True
        elif signal_dir == 'UP' and rsi_15m > 60:
            score += 1.0
            details.append('RSI15m aligned')
            aligned_15m = True
        
        if rsi_1h is not None:
            aligned_1h = False
            if signal_dir == 'DOWN' and rsi_1h < 45:
                score += 1.0
                details.append('RSI1h aligned')
                aligned_1h = True
            elif signal_dir == 'UP' and rsi_1h > 55:
                score += 1.0
                details.append('RSI1h aligned')
                aligned_1h = True
            
            # Triple alignment bonus
            if aligned_15m and aligned_1h:
                score += 0.5
                details.append('3TF_BONUS')
    
    return min(10.0, score), details

def compute_atr(candles, period=14):
    if len(candles) < period + 1: return 0
    trs = []
    for i in range(1, min(period+1, len(candles))):
        c = candles[-(i+1)]
        p = candles[-i]
        high = c.get('high', c['close'])
        low = c.get('low', c['close'])
        prev_close = p.get('close', p['close'])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0

# ═══════════════════════════════════════════════════════════════════════
# V19.7: SIMULATE POLYMARKET PRICING (≤8¢ only)
# ═══════════════════════════════════════════════════════════════════════
def simulate_polymarket_price(rsi, direction, sig_dir, confluence, regime, price_change_pct):
    """Simulate Polymarket Up/Down pricing. V19.7: ≤8¢ entries only."""
    if sig_dir == 'DOWN':
        strength = (30 - min(rsi, 30)) / 30.0
        strength *= (confluence / 10.0)
        cheap_price = 0.03 + (1.0 - strength) * 0.05  # 3-8¢
    else:
        strength = (min(rsi, 100) - 70) / 30.0
        strength *= (confluence / 10.0)
        cheap_price = 0.03 + (1.0 - strength) * 0.05  # 3-8¢
    
    noise = random.gauss(0, 0.01)
    cheap_price += noise
    cheap_price = max(0.02, min(0.08, cheap_price))  # V19.6: hard cap 8¢
    
    return (cheap_price, 'cheap_side', 'direct')

# ═══════════════════════════════════════════════════════════════════════
# DOWNLOAD BINANCE DATA — multi-TF: 5m, 15m, 1h
# ═══════════════════════════════════════════════════════════════════════
def download_klines(symbol='BTCUSDT', interval='5m', days=180):
    all_candles = []
    end_time = int(time.time() * 1000)
    start_time = end_time - days * 24 * 60 * 60 * 1000
    
    print(f"Downloading {symbol} {interval} klines for last {days} days...")
    
    current_start = start_time
    batch = 0
    while current_start < end_time:
        batch += 1
        url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&startTime={current_start}&limit=1000'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC/19.7'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"  Batch {batch} error: {e}")
            time.sleep(2)
            continue
        
        if not data:
            break
        
        for k in data:
            all_candles.append({
                'time': k[0],
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5]),
            })
        
        current_start = data[-1][0] + 1
        print(f"  Batch {batch}: {len(data)} candles, total {len(all_candles)}")
        time.sleep(0.5)
    
    print(f"Total {interval} candles downloaded: {len(all_candles)}")
    return all_candles

# ═══════════════════════════════════════════════════════════════════════
# BUILD MULTI-TF RSI FROM 5m DATA
# ═══════════════════════════════════════════════════════════════════════
def build_multi_tf_rsi(candles_5m):
    """Build 15m and 1h RSI from 5m candles by aggregating."""
    rsi_15m = {}
    rsi_1h = {}
    
    prices_5m = [c['close'] for c in candles_5m]
    
    # Build 15m close prices by sampling every 3rd candle
    # Build 1h close prices by sampling every 12th candle
    prices_15m_all = prices_5m[::3]  # Every 3rd 5m candle ≈ 15m
    prices_1h_all = prices_5m[::12]  # Every 12th 5m candle ≈ 1h
    
    for i in range(50, len(candles_5m)):
        # Map 5m index i to 15m index (i // 3)
        idx_15m = i // 3
        idx_1h = i // 12
        
        # 15m RSI: need at least 15 data points
        if idx_15m >= 15:
            window = prices_15m_all[max(0, idx_15m-15):idx_15m+1]
            if len(window) >= 15:
                rsi_15m[i] = compute_rsi(window, 14)
        
        # 1h RSI: need at least 15 data points
        if idx_1h >= 15:
            window = prices_1h_all[max(0, idx_1h-15):idx_1h+1]
            if len(window) >= 15:
                rsi_1h[i] = compute_rsi(window, 14)
    
    return rsi_15m, rsi_1h

# ═══════════════════════════════════════════════════════════════════════
# SIMULATION
# ═══════════════════════════════════════════════════════════════════════
def run_backtest(candles, target_trades=500):
    prices = [c['close'] for c in candles]
    
    # Build multi-TF RSI
    print("Building multi-TF RSI indicators...")
    rsi_15m_map, rsi_1h_map = build_multi_tf_rsi(candles)
    print(f"  15m RSI available for {len(rsi_15m_map)} candles")
    print(f"  1h RSI available for {len(rsi_1h_map)} candles")
    
    bankroll = BANKROLL_START
    trades = []
    closed = []
    daily_losses = 0
    daily_loss_amt = 0.0
    current_date = None
    cooldown_until = 0
    n_direction_same = {'UP': 0, 'DOWN': 0}
    
    skipped_session = 0
    skipped_strategy = 0
    skipped_candle_body = 0
    skipped_confluence = 0
    multi_tf_aligned = 0
    triple_tf_aligned = 0
    
    i = 50  # Start after warmup
    
    while len(closed) < target_trades and i < len(prices) - 3:
        c = candles[i]
        ts = c.get('time', 0)
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        today = dt.strftime('%Y-%m-%d')
        
        # Daily reset
        if today != current_date:
            daily_losses = 0
            daily_loss_amt = 0.0
            current_date = today
        
        # Check daily loss limits
        if daily_losses >= DAILY_LOSS_LIMIT or daily_loss_amt >= bankroll * DAILY_LOSS_PCT:
            i += 3
            continue
        
        # Resolve open trades
        for t in trades[:]:
            candles_ahead = i - t['candle_idx']
            mins_elapsed = candles_ahead * 5
            
            if mins_elapsed >= 15 or candles_ahead >= 3:
                entry_idx = t['candle_idx']
                price_at_entry = prices[entry_idx]
                price_at_exit = prices[min(entry_idx + 3, len(prices)-1)]
                price_went_up = price_at_exit > price_at_entry
                
                if t['side'] == 'cheap_side':
                    won = (t['cheap_direction'] == 'UP' and price_went_up) or \
                          (t['cheap_direction'] == 'DOWN' and not price_went_up)
                else:
                    won = (t['direction'] == 'UP' and price_went_up) or \
                          (t['direction'] == 'DOWN' and not price_went_up)
                
                if won:
                    payout = (1.0 / t['entry_price'] - 1.0) * t['bet']
                    pnl = payout
                else:
                    if random.random() < 0.35:
                        pnl = -t['bet'] * STOP_LOSS_PCT
                    else:
                        pnl = -t['bet']
                
                bankroll += pnl
                t['outcome'] = 'win' if won else 'loss'
                t['pnl'] = round(pnl, 2)
                t['actual_price_change'] = round((price_at_exit - price_at_entry) / price_at_entry * 100, 3)
                t['price_went_up'] = price_went_up
                closed.append(t)
                
                if pnl < 0:
                    daily_losses += 1
                    daily_loss_amt += abs(pnl)
                    if abs(pnl) > t['bet'] * STOP_LOSS_PCT * 0.5:
                        cooldown_until = i + int(COOLDOWN_MINS / 5)
                
                n_direction_same[t['direction']] = max(0, n_direction_same.get(t['direction'], 0) - 1)
                trades.remove(t)
        
        # Skip if in cooldown
        if i < cooldown_until:
            i += 1
            continue
        
        # ═══ V19.7: SESSION FILTER ═══
        session = get_session(dt.hour)
        if session[0] in BLOCKED_SESSIONS:
            skipped_session += 1
            i += 1
            continue
        
        # Generate signal
        window = prices[:i+1]
        rsi = compute_rsi(window)
        direction, dir_pct = detect_direction(window)
        regime = get_regime(window)
        ema21 = compute_ema(window, 21)
        ema50 = compute_ema(window, 50)
        vwap = compute_vwap(candles[:i+1])
        macd_hist = compute_macd(window)
        atr_val = compute_atr(candles[:i+1])
        vol_regime, max_entry = classify_volatility(atr_val, prices[i])
        
        # Multi-TF RSI
        rsi_15m_val = rsi_15m_map.get(i)
        rsi_1h_val = rsi_1h_map.get(i)
        
        # Determine signal direction
        if direction in ('UP', 'DOWN'):
            sig_dir = direction
        else:
            if regime == 'trending_down' and rsi < 60:
                sig_dir = 'DOWN'
            elif regime == 'trending_up' and rsi > 40:
                sig_dir = 'UP'
            else:
                i += 1
                continue
        
        # ═══ V19.7: Candle-body confirmation (for cheap-side direction) ═══
        # When we buy the CHEAP SIDE, we're buying the OPPOSITE of signal direction.
        # Signal DOWN → buy cheap UP. Signal UP → buy cheap DOWN.
        # The candle body should NOT strongly oppose our CHEAP SIDE direction.
        # E.g., if we're buying cheap UP, a small red candle is fine (it IS the oversold signal).
        # But a large green candle against a DOWN signal is suspicious.
        if CANDLE_BODY_CONFIRM:
            candle_open = c.get('open', c['close'])
            candle_close = c['close']
            candle_body = candle_close - candle_open
            body_pct = abs(candle_body) / c['close'] * 100 if c['close'] > 0 else 0
            
            # Determine CHEAP SIDE direction (what we're actually buying)
            cheap_dir = 'UP' if sig_dir == 'DOWN' else 'DOWN'
            
            # Only reject if candle body STRONGLY opposes cheap-side direction
            # If cheap side is UP but candle is large red → suspicious
            # If cheap side is DOWN but candle is large green → suspicious
            # But small opposing bodies are fine (they're the oversold signal)
            if cheap_dir == 'UP' and candle_body < 0 and body_pct > 0.05:
                # Buying UP but large red candle — only reject if body > 0.05%
                if body_pct > CANDLE_BODY_MIN_PCT:
                    skipped_candle_body += 1
                    i += 1
                    continue
            elif cheap_dir == 'DOWN' and candle_body > 0 and body_pct > 0.05:
                # Buying DOWN but large green candle — only reject if body > 0.05%
                if body_pct > CANDLE_BODY_MIN_PCT:
                    skipped_candle_body += 1
                    i += 1
                    continue
        
        # Confluence override for FLAT direction
        confluence, details = compute_confluence(
            rsi, direction, regime, ema21, ema50, vwap, prices[i],
            macd_hist, session, (vol_regime, max_entry), sig_dir,
            rsi_15m=rsi_15m_val, rsi_1h=rsi_1h_val
        )
        
        # Confluence override for FLAT direction
        if direction == 'FLAT' and confluence >= 7.5:
            pass
        elif confluence < MIN_CONFLUENCE:
            skipped_confluence += 1
            i += 1
            continue
        
        # Count multi-TF alignment
        if rsi_15m_val is not None:
            aligned_15m = (sig_dir == 'DOWN' and rsi_15m_val < 40) or (sig_dir == 'UP' and rsi_15m_val > 60)
            if aligned_15m:
                multi_tf_aligned += 1
            if rsi_1h_val is not None:
                aligned_1h = (sig_dir == 'DOWN' and rsi_1h_val < 45) or (sig_dir == 'UP' and rsi_1h_val > 55)
                if aligned_15m and aligned_1h:
                    triple_tf_aligned += 1
        
        # ═══ V19.7: STRATEGY FILTER ═══
        if rsi < 25 and sig_dir == 'DOWN':
            strategy = 'severe_oversold_down'
        elif rsi > 75 and sig_dir == 'UP':
            strategy = 'severe_overbought_up'
        elif rsi < 30 and sig_dir == 'DOWN':
            strategy = 'oversold_down'
        elif rsi > 65 and sig_dir == 'UP':
            strategy = 'overbought_up'
        elif sig_dir == 'DOWN':
            strategy = 'direction_down_cheap'
        else:
            strategy = 'direction_up_cheap'
        
        # Block weak strategies
        if strategy in BLOCKED_STRATEGIES:
            skipped_strategy += 1
            i += 1
            continue
        
        # Simulate Polymarket pricing
        price_result = simulate_polymarket_price(rsi, direction, sig_dir, confluence, regime, dir_pct)
        if price_result is None:
            i += 1
            continue
        
        entry_price, side, entry_type = price_result
        
        # Position sizing
        tier_size = CONFLUENCE_SIZING.get(int(confluence), BASE_SIZE)
        if vol_regime == 'low_vol' and confluence >= 8:
            tier_size *= 1.3
        elif vol_regime == 'high_vol':
            tier_size *= 0.7
        tier_size = min(tier_size, 0.08)
        
        bet = bankroll * tier_size
        bet = max(0.50, min(bet, MAX_BET_SIZE, bankroll * 0.08))
        
        if bankroll - bet < 10:
            i += 1
            continue
        
        # Determine which side is cheap
        if sig_dir == 'DOWN':
            cheap_direction = 'UP'
        else:
            cheap_direction = 'DOWN'
        
        trade = {
            'strategy': strategy,
            'direction': sig_dir,
            'cheap_direction': cheap_direction,
            'side': side,
            'entry_type': entry_type,
            'rsi': round(rsi, 1),
            'rsi_15m': round(rsi_15m_val, 1) if rsi_15m_val else None,
            'rsi_1h': round(rsi_1h_val, 1) if rsi_1h_val else None,
            'confluence': round(confluence, 1),
            'details': details,
            'entry_price': round(entry_price, 3),
            'win_prob': round(1.0 / entry_price if entry_price > 0 else 0, 4),
            'tier_size': tier_size,
            'bet': round(bet, 2),
            'bankroll_at_entry': round(bankroll, 2),
            'vol_regime': vol_regime,
            'session': session[0],
            'regime': regime,
            'candle_idx': i,
            'time': c.get('time', 0),
            'entry_time': dt.isoformat(),
        }
        trades.append(trade)
        n_direction_same[sig_dir] = n_direction_same.get(sig_dir, 0) + 1
        
        i += 1
    
    print(f"\n  SKIPPED STATS:")
    print(f"    Session filter (London/off_hours): {skipped_session}")
    print(f"    Strategy filter (overbought_up/direction_up_cheap): {skipped_strategy}")
    print(f"    Candle-body filter: {skipped_candle_body}")
    print(f"    Confluence <7: {skipped_confluence}")
    print(f"    Multi-TF aligned (15m): {multi_tf_aligned}")
    print(f"    Triple-TF aligned (15m+1h): {triple_tf_aligned}")
    
    return closed, bankroll

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=180, help='Days of history to backtest')
    parser.add_argument('--trades', type=int, default=500, help='Target number of trades')
    args = parser.parse_args()
    
    # Download data
    candles = download_klines('BTCUSDT', '5m', days=args.days)
    
    if len(candles) < 100:
        print("ERROR: Not enough candle data")
        sys.exit(1)
    
    print(f"\n{'='*70}")
    print(f"V19.7 HISTORICAL BACKTEST")
    print(f"{'='*70}")
    print(f"  Bankroll: ${BANKROLL_START}")
    print(f"  MIN_CONFLUENCE: {MIN_CONFLUENCE}")
    print(f"  Price gate: ≤8¢ ONLY")
    print(f"  FLIP_TO_CHEAP_SIDE: {FLIP_TO_CHEAP_SIDE}")
    print(f"  Session filter: Block {BLOCKED_SESSIONS}")
    print(f"  Strategy filter: Block {BLOCKED_STRATEGIES}")
    print(f"  Multi-TF RSI: {MULTI_TF_BONUS}")
    print(f"  Candle-body confirm: {CANDLE_BODY_CONFIRM}")
    print(f"  Target: {args.trades} trades over {args.days} days")
    print(f"{'='*70}\n")
    
    closed, final_bankroll = run_backtest(candles, target_trades=args.trades)
    
    # Analysis
    wins = [t for t in closed if t['outcome'] == 'win']
    losses = [t for t in closed if t['outcome'] == 'loss']
    
    wr = len(wins) / len(closed) * 100 if closed else 0
    pnl = final_bankroll - BANKROLL_START
    roi = pnl / BANKROLL_START * 100
    
    # By strategy
    by_strategy = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_direction = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_confluence = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_price = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_session = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_cheap_dir = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_multi_tf = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    
    for t in closed:
        by_strategy[t['strategy']]['wins'] += t['outcome'] == 'win'
        by_strategy[t['strategy']]['total'] += 1
        by_strategy[t['strategy']]['pnl'] += t['pnl']
        
        by_direction[t['direction']]['wins'] += t['outcome'] == 'win'
        by_direction[t['direction']]['total'] += 1
        by_direction[t['direction']]['pnl'] += t['pnl']
        
        cf = int(t['confluence'])
        by_confluence[cf]['wins'] += t['outcome'] == 'win'
        by_confluence[cf]['total'] += 1
        by_confluence[cf]['pnl'] += t['pnl']
        
        ep = t['entry_price']
        price_bucket = f"{int(ep*100)}c"
        by_price[price_bucket]['wins'] += t['outcome'] == 'win'
        by_price[price_bucket]['total'] += 1
        by_price[price_bucket]['pnl'] += t['pnl']
        
        by_session[t['session']]['wins'] += t['outcome'] == 'win'
        by_session[t['session']]['total'] += 1
        by_session[t['session']]['pnl'] += t['pnl']
        
        by_cheap_dir[t['cheap_direction']]['wins'] += t['outcome'] == 'win'
        by_cheap_dir[t['cheap_direction']]['total'] += 1
        by_cheap_dir[t['cheap_direction']]['pnl'] += t['pnl']
        
        # Multi-TF analysis
        has_15m = t.get('rsi_15m') is not None
        has_1h = t.get('rsi_1h') is not None
        key = '5m_only'
        if has_15m and has_1h:
            key = '3TF'
        elif has_15m:
            key = '2TF'
        by_multi_tf[key]['wins'] += t['outcome'] == 'win'
        by_multi_tf[key]['total'] += 1
        by_multi_tf[key]['pnl'] += t['pnl']
    
    # Max drawdown
    peak = BANKROLL_START
    max_dd = 0
    running_pnl = BANKROLL_START
    for t in closed:
        running_pnl += t['pnl']
        if running_pnl > peak: peak = running_pnl
        dd = (peak - running_pnl) / peak * 100
        if dd > max_dd: max_dd = dd
    
    # Avg entry price
    avg_entry = np.mean([t['entry_price'] for t in closed]) if closed else 0
    
    # Expected value
    if closed:
        avg_payout_on_win = np.mean([1.0/t['entry_price'] - 1.0 for t in closed if t['outcome'] == 'win'])
        ev_per_dollar = (wr/100 * avg_payout_on_win - (1-wr/100) * 1)
    
    print(f"\n{'='*70}")
    print(f"V19.7 BACKTEST RESULTS")
    print(f"{'='*70}")
    print(f"  Total trades: {len(closed)}")
    print(f"  Win rate: {len(wins)}/{len(closed)} = {wr:.1f}%")
    print(f"  Final bankroll: ${final_bankroll:.2f} (start ${BANKROLL_START})")
    print(f"  Net PnL: ${pnl:.2f}")
    print(f"  ROI: {roi:.1f}%")
    print(f"  Max drawdown: {max_dd:.1f}%")
    print(f"  Avg entry price: {avg_entry*100:.1f}¢")
    if closed:
        print(f"  Avg payout per $1 on win: {avg_payout_on_win:.2f}x")
        print(f"  EV per $1: ${ev_per_dollar:.3f}")
    
    print(f"\n  BY STRATEGY:")
    for k, v in sorted(by_strategy.items(), key=lambda x: -x[1]['total']):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k:30s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY SIGNAL DIRECTION:")
    for k, v in sorted(by_direction.items()):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY CHEAP SIDE DIRECTION (which side we bought):")
    for k, v in sorted(by_cheap_dir.items()):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY MULTI-TF ALIGNMENT:")
    for k in ['5m_only', '2TF', '3TF']:
        if k in by_multi_tf:
            v = by_multi_tf[k]
            v_wr = v['wins']/v['total']*100 if v['total'] else 0
            print(f"    {k:10s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY CONFLUENCE:")
    for k in sorted(by_confluence.keys()):
        v = by_confluence[k]
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}/10: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY SESSION:")
    for k, v in sorted(by_session.items(), key=lambda x: -x[1]['total']):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k:15s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY PRICE TIER:")
    for k in sorted(by_price.keys(), key=lambda x: int(x.rstrip('c'))):
        v = by_price[k]
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    # Breakeven
    breakeven_wr = avg_entry * 100 if avg_entry > 0 else 0
    print(f"\n  BREAKEVEN ANALYSIS:")
    print(f"    Avg entry price: {avg_entry*100:.1f}¢")
    print(f"    Breakeven WR: {breakeven_wr:.1f}%")
    print(f"    Actual WR: {wr:.1f}%")
    print(f"    Edge: {wr - breakeven_wr:.1f}pp above breakeven")
    
    # Save
    results = {
        'version': 'V19.7',
        'total_trades': len(closed),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': wr,
        'final_bankroll': final_bankroll,
        'pnl': pnl,
        'roi_pct': roi,
        'max_drawdown_pct': max_dd,
        'avg_entry_price': avg_entry,
        'breakeven_wr': breakeven_wr,
        'edge_pp': wr - breakeven_wr,
        'by_strategy': {k: dict(v) for k, v in by_strategy.items()},
        'by_direction': {k: dict(v) for k, v in by_direction.items()},
        'by_cheap_direction': {k: dict(v) for k, v in by_cheap_dir.items()},
        'by_confluence': {k: dict(v) for k, v in by_confluence.items()},
        'by_price': {k: dict(v) for k, v in by_price.items()},
        'by_session': {k: dict(v) for k, v in by_session.items()},
        'by_multi_tf': {k: dict(v) for k, v in by_multi_tf.items()},
        'trades': closed[:50],
    }
    
    with open(OUTPUT / 'backtest_v19_7_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\nResults saved to {OUTPUT / 'backtest_v19_7_results.json'}")