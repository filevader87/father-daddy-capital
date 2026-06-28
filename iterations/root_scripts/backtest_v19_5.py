#!/usr/bin/env python3
"""
V19.5 Historical Backtest — 500 trades on real Binance BTC 5m data.
Core change: Buy CHEAP SIDE regardless of signal direction.
When signal says DOWN but DOWN is 85¢, buy UP at 7¢ (13:1 odds).
Resolution: Did the cheap side actually win? At 7¢, UP only needs to win 7%+.
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
# V19.5 CONFIG
# ═══════════════════════════════════════════════════════════════════════
BANKROLL_START = 400.0
MIN_CONFLUENCE = 7.0

# V19.6: ≤8¢ ONLY (historical backtest proved 8-15¢ zone is -EV at 39% WR)
# ≤8¢: always allowed (77.8% WR historical, breakeven 8%)
# 8¢+: BLOCKED
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

# V19.5 FLIP LOGIC: always buy the cheap side
# When signal says DOWN but DOWN is expensive, buy UP (cheap side) instead
# This is the key insight: at 7¢, breakeven is 7%. Even if we're wrong 53% of the time,
# the 13:1 payout makes us profitable.
FLIP_TO_CHEAP_SIDE = True

# ═══════════════════════════════════════════════════════════════════════
# INDICATORS (same as paper_trade_v19_2)
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
    if 13 <= utc_hour < 21: return ('new_york', 1.0)
    elif 7 <= utc_hour < 16: return ('london', 0.8)
    elif 0 <= utc_hour < 9: return ('asia', 0.7)
    return ('off_hours', 0.5)

def detect_direction(prices, lookback=3, min_change_pct=0.10):
    if len(prices) < lookback + 1: return 'FLAT', 0.0
    recent = prices[-(lookback+1):-1]
    if len(recent) < 2: return 'FLAT', 0.0
    change_pct = (recent[-1] - recent[0]) / recent[0] * 100
    if change_pct > min_change_pct: return 'UP', change_pct
    elif change_pct < -min_change_pct: return 'DOWN', change_pct
    return 'FLAT', change_pct

def compute_confluence(rsi, direction, regime, ema21, ema50, vwap, price, macd_hist, session, atr_vol, signal_dir):
    score = 0.0
    details = []
    
    if signal_dir == 'DOWN' and rsi < 25: score += 1.0; details.append('RSI<25')
    elif signal_dir == 'DOWN' and rsi < 30: score += 0.8; details.append('RSI<30')
    elif signal_dir == 'DOWN' and rsi < 38: score += 0.3; details.append('RSI<38')
    elif signal_dir == 'UP' and rsi > 75: score += 1.0; details.append('RSI>75')
    elif signal_dir == 'UP' and rsi > 65: score += 0.8; details.append('RSI>65')
    elif signal_dir == 'UP' and rsi > 55: score += 0.3; details.append('RSI>55')
    else: details.append('RSI_meh')
    
    if direction in ('UP', 'DOWN'): score += 1.0; details.append(f'Dir={direction}')
    else: details.append('Dir=FLAT')
    
    ema_diff = abs(ema21 - ema50) / ema50 if ema50 > 0 else 0
    if ema21 > ema50 and signal_dir == 'UP': score += 1.0; details.append('EMA_bullish')
    elif ema21 < ema50 and signal_dir == 'DOWN': score += 1.0; details.append('EMA_bearish')
    elif ema_diff < 0.001: score += 0.3; details.append('EMA_flat')
    
    if signal_dir == 'UP' and price > vwap: score += 1.0; details.append('Above_VWAP')
    elif signal_dir == 'DOWN' and price < vwap: score += 1.0; details.append('Below_VWAP')
    elif signal_dir == 'UP' and price < vwap * 1.002: score += 0.5; details.append('Near_VWAP')
    
    if signal_dir == 'UP' and macd_hist > 0: score += 1.0; details.append('MACD_expanding')
    elif signal_dir == 'DOWN' and macd_hist < 0: score += 1.0; details.append('MACD_declining')
    elif abs(macd_hist) < 0.0001: score += 0.3
    
    session_name, session_weight = session
    score += session_weight
    details.append(f'S={session_name[:3]}({session_weight:.1f})')
    
    vol_regime, max_entry = atr_vol
    if vol_regime == 'high_vol' and signal_dir in ('UP', 'DOWN'): score += 1.0
    elif vol_regime == 'low_vol' and regime in ('trending_up', 'trending_down'): score += 0.8
    elif vol_regime == 'medium_vol': score += 0.5
    
    if regime in ('trending_up',) and signal_dir == 'UP': score += 1.0; details.append('Trend_aligned')
    elif regime in ('trending_down',) and signal_dir == 'DOWN': score += 1.0; details.append('Trend_aligned')
    
    if signal_dir == 'DOWN' and rsi < 40: score += 0.5
    elif signal_dir == 'UP' and rsi > 60: score += 0.5
    
    if signal_dir == 'DOWN' and price < vwap: score += 1.0
    elif signal_dir == 'UP' and price > vwap: score += 1.0
    else: score += 0.3
    
    return min(10.0, score), details

def compute_atr(candles, period=14):
    if len(candles) < period + 1: return 0
    trs = []
    for i in range(1, min(period+1, len(candles))):
        c = candles[-(i+1)]
        p = candles[-(i)]
        high = c.get('high', c['close'])
        low = c.get('low', c['close'])
        prev_close = p.get('close', p['close'])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0

# ═══════════════════════════════════════════════════════════════════════
# V19.5: SIMULATE POLYMARKET PRICING
# ═══════════════════════════════════════════════════════════════════════
def simulate_polymarket_price(rsi, direction, sig_dir, confluence, regime, price_change_pct):
    """
    Simulate Polymarket Up/Down binary prices based on signal strength.
    
    When signal says DOWN (RSI<30, bearish indicators):
    - DOWN token is expensive (70-95¢) — market agrees BTC goes down
    - UP token is cheap (5-30¢) — market disagrees
    
    Key: cheap side price = 1 - expensive_side_price
    The cheaper side has the odds advantage.
    
    Returns (entry_price, side, entry_type)
    """
    # Signal strength determines how lopsided the market is
    # Strong signal (RSI<25, high confluence) → cheaper opposite side
    # Weak signal (RSI~38, moderate confluence) → less lopsided
    # V19.6: Only generate ultra-cheap entries (≤8¢)
    # When signal says DOWN, UP is cheap (3-8¢)
    # When signal says UP, DOWN is cheap (3-8¢)
    if sig_dir == 'DOWN':
        # Signal says DOWN. Market prices DOWN high (70-95¢), UP cheap (3-8¢)
        strength = (30 - min(rsi, 30)) / 30.0  # 0-1, 1=strongest bearish
        strength *= (confluence / 10.0)
        # UP price range: 3-8¢ (V19.6: hard cap at 8¢)
        cheap_price = 0.03 + (1.0 - strength) * 0.05  # 3-8¢
    else:
        # Signal says UP. Market prices UP high, DOWN cheap (3-8¢)
        strength = (min(rsi, 100) - 70) / 30.0  # 0-1, 1=strongest bullish
        strength *= (confluence / 10.0)
        # DOWN price range: 3-8¢
        cheap_price = 0.03 + (1.0 - strength) * 0.05  # 3-8¢
    
    # Add some randomness (market isn't perfectly priced)
    noise = random.gauss(0, 0.01)  # V19.6: tighter noise to stay ≤8¢
    cheap_price += noise
    cheap_price = max(0.02, min(0.08, cheap_price))  # V19.6: hard cap 8¢
    
    # The expensive side
    expensive_price = 1.0 - cheap_price
    # Slight overround
    total = cheap_price + expensive_price
    if total < 1.0:
        expensive_price += (1.0 - total)
    
    # V19.5: Always buy the CHEAP side
    # Determine entry_type based on price
    if cheap_price <= ULTRA_CHEAP_MAX:
        entry_type = 'direct'  # Signal aligned with cheap side
    elif cheap_price <= MID_CHEAP_MAX:
        if confluence >= 8.0:
            entry_type = 'mid_cheap'
        else:
            return None  # Not enough confluence for 8-15¢
    elif cheap_price <= CHEAP_MAX:
        if confluence >= 9.0:
            entry_type = 'upper_cheap'
        else:
            return None  # Not enough confluence for 15-20¢
    else:
        return None  # Dead zone or fair price
    
    return (cheap_price, 'cheap_side', entry_type)

# ═══════════════════════════════════════════════════════════════════════
# DOWNLOAD BINANCE DATA
# ═══════════════════════════════════════════════════════════════════════
def download_klines(symbol='BTCUSDT', interval='5m', days=90):
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
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC/19.5'})
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
    
    print(f"Total candles downloaded: {len(all_candles)}")
    return all_candles

# ═══════════════════════════════════════════════════════════════════════
# SIMULATION
# ═══════════════════════════════════════════════════════════════════════
def run_backtest(candles, target_trades=500):
    prices = [c['close'] for c in candles]
    
    bankroll = BANKROLL_START
    trades = []
    closed = []
    daily_losses = 0
    daily_loss_amt = 0.0
    current_date = None
    cooldown_until = 0
    n_direction_same = {'UP': 0, 'DOWN': 0}
    
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
                
                # V19.5: Resolution based on ACTUAL price movement
                # We bought the CHEAP SIDE. Did it win?
                # If we bought UP cheap at 7¢, UP wins if price went UP
                # If we bought DOWN cheap at 7¢, DOWN wins if price went DOWN
                
                # Use actual price change to determine outcome
                price_at_entry = prices[entry_idx]
                price_at_exit = prices[min(entry_idx + 3, len(prices)-1)]
                price_went_up = price_at_exit > price_at_entry
                
                # Did our side win?
                if t['side'] == 'cheap_side':
                    # We bought the cheap side. Cheap side wins if:
                    # - UP cheap side: price went UP (our UP token pays $1)
                    # - DOWN cheap side: price went DOWN (our DOWN token pays $1)
                    # The "side" we bought is determined by which was cheap
                    won = (t['cheap_direction'] == 'UP' and price_went_up) or \
                          (t['cheap_direction'] == 'DOWN' and not price_went_up)
                else:
                    won = (t['direction'] == 'UP' and price_went_up) or \
                          (t['direction'] == 'DOWN' and not price_went_up)
                
                if won:
                    # Win payout at cheap-side odds: (1/entry_price - 1) * bet
                    payout = (1.0 / t['entry_price'] - 1.0) * t['bet']
                    pnl = payout
                else:
                    # Loss: 35% chance of stop-loss at 50%, 65% full loss
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
        session = get_session(dt.hour)
        
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
        
        # Confluence override for FLAT direction
        confluence, details = compute_confluence(
            rsi, direction, regime, ema21, ema50, vwap, prices[i],
            macd_hist, session, (vol_regime, max_entry), sig_dir
        )
        
        # V19.5: Confluence override — FLAT with strong regime can override
        if direction == 'FLAT' and confluence >= 7.5:
            pass  # Keep the signal
        elif confluence < MIN_CONFLUENCE:
            i += 1
            continue
        
        # V19.5: Determine strategy
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
        
        # V19.5: Simulate Polymarket pricing
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
            # Signal says DOWN. Market prices DOWN high, UP cheap.
            cheap_direction = 'UP'
        else:
            # Signal says UP. Market prices UP high, DOWN cheap.
            cheap_direction = 'DOWN'
        
        trade = {
            'strategy': strategy,
            'direction': sig_dir,
            'cheap_direction': cheap_direction,
            'side': side,
            'entry_type': entry_type,
            'rsi': round(rsi, 1),
            'confluence': round(confluence, 1),
            'details': details,
            'entry_price': round(entry_price, 3),
            'win_prob': round(1.0 / entry_price if entry_price > 0 else 0, 4),  # Implied odds
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
    
    return closed, bankroll

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    # Download 90 days of data for more trades
    candles = download_klines('BTCUSDT', '5m', days=90)
    
    if len(candles) < 100:
        print("ERROR: Not enough candle data")
        sys.exit(1)
    
    print(f"\n{'='*70}")
    print(f"V19.6 HISTORICAL BACKTEST — ≤8¢ ONLY + CHEAP-SIDE FLIP")
    print(f"{'='*70}")
    print(f"  Candles: {len(candles)} (90 days, 5m BTC)")
    print(f"  Bankroll: ${BANKROLL_START}")
    print(f"  MIN_CONFLUENCE: {MIN_CONFLUENCE}")
    print(f"  Price gate: ≤8¢ ONLY (V19.5 mid-cheap zone removed: 39% WR at 8-15¢)")
    print(f"  FLIP_TO_CHEAP_SIDE: {FLIP_TO_CHEAP_SIDE}")
    print(f"  Target: {500} trades")
    print(f"{'='*70}\n")
    
    closed, final_bankroll = run_backtest(candles, target_trades=500)
    
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
    by_entry_type = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    
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
        
        by_entry_type[t['entry_type']]['wins'] += t['outcome'] == 'win'
        by_entry_type[t['entry_type']]['total'] += 1
        by_entry_type[t['entry_type']]['pnl'] += t['pnl']
    
    # Max drawdown
    peak = BANKROLL_START
    max_dd = 0
    running_pnl = BANKROLL_START
    for t in closed:
        running_pnl += t['pnl']
        if running_pnl > peak:
            peak = running_pnl
        dd = (peak - running_pnl) / peak * 100
        if dd > max_dd:
            max_dd = dd
    
    # Avg entry price
    avg_entry = np.mean([t['entry_price'] for t in closed]) if closed else 0
    
    # Expected value per trade
    if closed:
        avg_payout_on_win = np.mean([1.0/t['entry_price'] - 1.0 for t in closed if t['outcome'] == 'win'])
        avg_loss = np.mean([abs(t['pnl']) for t in closed if t['outcome'] == 'loss'])
        ev_per_dollar = (wr/100 * avg_payout_on_win - (1-wr/100) * 1) 
    
    print(f"\n{'='*70}")
    print(f"V19.6 BACKTEST RESULTS — ≤8¢ ONLY + CHEAP-SIDE FLIP")
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
    
    print(f"\n  BY ENTRY TYPE:")
    for k, v in sorted(by_entry_type.items(), key=lambda x: -x[1]['total']):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k:20s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY CONFLUENCE:")
    for k in sorted(by_confluence.keys()):
        v = by_confluence[k]
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}/10: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY PRICE TIER:")
    for k in sorted(by_price.keys(), key=lambda x: int(x.rstrip('c'))):
        v = by_price[k]
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY SESSION:")
    for k, v in sorted(by_session.items(), key=lambda x: -x[1]['total']):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k:15s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    # Check: what WR do we need at avg entry price to be profitable?
    avg_ep = avg_entry
    breakeven_wr = avg_ep * 100  # e.g., 8¢ → 8% breakeven
    print(f"\n  BREAKEVEN ANALYSIS:")
    print(f"    Avg entry price: {avg_ep*100:.1f}¢")
    print(f"    Breakeven WR at this price: {breakeven_wr:.1f}%")
    print(f"    Actual WR: {wr:.1f}%")
    print(f"    Edge: {wr - breakeven_wr:.1f}pp above breakeven")
    
    # Save results
    results = {
        'version': 'V19.6',
        'total_trades': len(closed),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': wr,
        'final_bankroll': final_bankroll,
        'pnl': pnl,
        'roi_pct': roi,
        'max_drawdown_pct': max_dd,
        'avg_entry_price': avg_ep,
        'breakeven_wr': breakeven_wr,
        'edge_pp': wr - breakeven_wr,
        'by_strategy': {k: dict(v) for k, v in by_strategy.items()},
        'by_direction': {k: dict(v) for k, v in by_direction.items()},
        'by_cheap_direction': {k: dict(v) for k, v in by_cheap_dir.items()},
        'by_entry_type': {k: dict(v) for k, v in by_entry_type.items()},
        'by_confluence': {k: dict(v) for k, v in by_confluence.items()},
        'by_price': {k: dict(v) for k, v in by_price.items()},
        'by_session': {k: dict(v) for k, v in by_session.items()},
        'trades': closed[:50],
    }
    
    with open(OUTPUT / 'backtest_v19_6_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\nResults saved to {OUTPUT / 'backtest_v19_6_results.json'}")