#!/usr/bin/env python3
"""
V19.9 — Whale-Inspired Strategy
=================================
Based on analysis of @bonereaper/0xe1D6 whale:
  - Down-side 8-51¢ = 488% ROI
  - Plays DIRECTIONAL (not cheap-side flip)
  - Higher price = higher confidence needed, but higher WR
  
Key changes from V19.7:
  1. EXPAND price range to 3-60¢ (not just ≤8¢)
  2. Use TIERED WR requirements: cheap (3-8¢) needs any signal, 
     mid (8-30¢) needs conf≥8 + oversold/overbought, 
     expensive (30-60¢) needs conf≥9 + severe oversold/overbought
  3. Play DIRECTIONAL at mid/expensive prices (Down token when DOWN signal)
  4. Keep cheap-side flip at ≤8¢
  5. NY-only sessions, conf≥8

Backtested against 365 days of BTC 5m data.
"""
import json, os, sys, math, time, random
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
import numpy as np

try:
    import urllib.request
except:
    pass

OUTPUT = Path(__file__).parent / "output"
OUTPUT.mkdir(exist_ok=True)

# V19.9 Config — Whale-inspired
BANKROLL_START = 320.0
MIN_CONFLUENCE = 8.0
CHEAP_MAX = 0.08
STOP_LOSS_PCT = 0.50
MAX_BET_SIZE = 20.0
BASE_SIZE = 0.04

# Price tiers and WR requirements
TIER_CONFIG = {
    # price_range: (min_conf, max_bet_pct, direction_mode)
    'ultra_cheap': (0.02, 0.05, 6.0, 0.06, 'flip'),     # 2-5¢: cheap-side flip, any signal
    'cheap':       (0.05, 0.08, 7.0, 0.06, 'flip'),       # 5-8¢: cheap-side flip, conf≥7
    'mid':         (0.08, 0.30, 8.0, 0.04, 'directional'), # 8-30¢: DIRECTIONAL, conf≥8
    'expensive':   (0.30, 0.60, 9.0, 0.03, 'directional'), # 30-60¢: DIRECTIONAL, conf≥9
}

FLIP_TO_CHEAP_SIDE = True  # For ultra-cheap and cheap tiers

# Sessions: NY only
BLOCKED_SESSIONS = {'london', 'london_open', 'london_close', 'off_hours', 'asia'}

def download_klines(symbol='BTCUSDT', interval='5m', days=365):
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
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC/19.9'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"  Batch {batch} error: {e}")
            time.sleep(2)
            continue
        if not data: break
        for k in data:
            all_candles.append({'time': k[0], 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5])})
        current_start = data[-1][0] + 1
        if batch % 20 == 0: print(f"  Batch {batch}: {len(all_candles)} candles total")
        time.sleep(0.3)
    print(f"Total {interval} candles: {len(all_candles)}")
    return all_candles

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
    if len(prices) < period: return prices[-1]
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

def classify_volatility(atr, price):
    if price <= 0: return ('low_vol', 0.20)
    pct = atr / price
    if pct > 0.005: return ('high_vol', 0.08)
    elif pct > 0.002: return ('medium_vol', 0.15)
    return ('low_vol', 0.20)

def get_session(utc_hour):
    est_hour = (utc_hour - 5) % 24
    if 19 <= est_hour or est_hour < 3: return ('asia', 0.7)
    elif 2 <= est_hour < 5: return ('london_open', 0.8)
    elif 7 <= est_hour < 12: return ('new_york', 1.0)
    elif 10 <= est_hour < 12: return ('london_close', 0.8)
    elif 3 <= est_hour < 7: return ('off_hours', 0.5)
    elif 12 <= est_hour < 19: return ('ny_afternoon', 0.6)
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
    
    # RSI scoring — sweet spot bonuses
    if signal_dir == 'DOWN':
        if rsi < 25: score += 1.5; details.append('RSI<25')
        elif 28 <= rsi < 36: score += 1.5; details.append(f'RSI_sweet_{rsi:.0f}')
        elif rsi < 45: score += 0.5; details.append(f'RSI_ok_{rsi:.0f}')
        else: details.append(f'RSI_meh_{rsi:.0f}')
    elif signal_dir == 'UP':
        if rsi >= 75: score += 1.5; details.append('RSI>75')
        elif rsi >= 65: score += 1.5; details.append(f'RSI_sweet_{rsi:.0f}')
        elif rsi > 55: score += 0.5; details.append(f'RSI_ok_{rsi:.0f}')
        else: details.append(f'RSI_meh_{rsi:.0f}')
    
    if direction in ('UP', 'DOWN'): score += 1.0; details.append(f'Dir={direction}')
    else: details.append('Dir=FLAT')
    
    ema_diff = abs(ema21 - ema50) / ema50 if ema50 > 0 else 0
    if ema21 > ema50 and signal_dir == 'UP': score += 1.0; details.append('EMA_bull')
    elif ema21 < ema50 and signal_dir == 'DOWN': score += 1.0; details.append('EMA_bear')
    elif ema_diff < 0.001: score += 0.3; details.append('EMA_flat')
    
    if signal_dir == 'UP' and price > vwap: score += 1.0; details.append('Above_VWAP')
    elif signal_dir == 'DOWN' and price < vwap: score += 1.0; details.append('Below_VWAP')
    elif signal_dir == 'UP' and price < vwap * 1.002: score += 0.5; details.append('Near_VWAP')
    
    if signal_dir == 'UP' and macd_hist > 0: score += 1.0; details.append('MACD_exp')
    elif signal_dir == 'DOWN' and macd_hist < 0: score += 1.0; details.append('MACD_dec')
    elif abs(macd_hist) < 0.0001: score += 0.3
    
    session_name, session_weight = session
    score += session_weight
    details.append(f'S={session_name[:3]}({session_weight:.1f})')
    
    vol_regime, max_entry = atr_vol
    if vol_regime in ('high_vol', 'medium_vol'): score += 0.5
    if regime == 'trending_up' and signal_dir == 'UP': score += 1.0; details.append('Trend_aligned')
    elif regime == 'trending_down' and signal_dir == 'DOWN': score += 1.0; details.append('Trend_aligned')
    
    # Multi-TF RSI
    if rsi_15m is not None:
        if signal_dir == 'DOWN' and rsi_15m < 40: score += 1.0; details.append('RSI15m_align')
        elif signal_dir == 'UP' and rsi_15m > 60: score += 1.0; details.append('RSI15m_align')
        if rsi_1h is not None:
            if signal_dir == 'DOWN' and rsi_1h < 45: score += 1.0; details.append('RSI1h_align')
            elif signal_dir == 'UP' and rsi_1h > 55: score += 1.0; details.append('RSI1h_align')
            if ((signal_dir == 'DOWN' and rsi_15m < 40 and rsi_1h < 45) or
                (signal_dir == 'UP' and rsi_15m > 60 and rsi_1h > 55)):
                score += 0.5; details.append('3TF_BONUS')
    
    return min(10.0, score), details

def simulate_polymarket_price(rsi, direction, sig_dir, confluence, regime, dir_pct, price_tier):
    """Simulate entry price based on signal strength and tier config."""
    if price_tier == 'directional':
        # Directional: buy the SIDE that matches our signal
        # If DOWN signal, buy DOWN token at moderate price
        # If UP signal, buy UP token at moderate price
        # Price reflects confidence in the direction
        if sig_dir == 'DOWN':
            strength = (50 - min(rsi, 50)) / 50  # Lower RSI = stronger DOWN signal
        else:
            strength = (min(rsi, 100) - 50) / 50  # Higher RSI = stronger UP signal
        
        strength *= (confluence / 10.0)
        
        # Moderate price range: 8-30¢ for mid tier
        base_price = 0.15  # Start at 15¢ (moderate confidence)
        # Strong signal → higher price (market agrees with us)
        # Weak signal → lower price (market disagrees, better odds)
        price = base_price + (strength - 0.5) * 0.20
        price = max(0.08, min(0.30, price))
        
        noise = random.gauss(0, 0.02)
        price = max(0.08, min(0.30, price + noise))
        return (price, 'directional', 'directional')
    
    else:
        # Cheap-side flip: buy the cheaper token
        if sig_dir == 'DOWN':
            cheap_price = 0.03 + random.random() * 0.05  # 3-8¢ UP token (opposite direction)
        else:
            cheap_price = 0.03 + random.random() * 0.05  # 3-8¢ DOWN token (opposite direction)
        
        cheap_price = max(0.02, min(0.08, cheap_price))
        return (cheap_price, 'cheap_side', 'flip')

def build_multi_tf_rsi(candles_5m):
    rsi_15m = {}
    rsi_1h = {}
    prices_5m = [c['close'] for c in candles_5m]
    prices_15m_all = prices_5m[::3]
    prices_1h_all = prices_5m[::12]
    for i in range(50, len(candles_5m)):
        idx_15m = i // 3
        idx_1h = i // 12
        if idx_15m >= 15:
            window = prices_15m_all[max(0, idx_15m-15):idx_15m+1]
            if len(window) >= 15:
                rsi_15m[i] = compute_rsi(window, 14)
        if idx_1h >= 15:
            window = prices_1h_all[max(0, idx_1h-15):idx_1h+1]
            if len(window) >= 15:
                rsi_1h[i] = compute_rsi(window, 14)
    return rsi_15m, rsi_1h

def select_tier(rsi, confluence, sig_dir):
    """Select which price tier to enter based on signal confidence."""
    # Ultra-cheap (2-5¢): cheap-side flip, conf≥6
    # Cheap (5-8¢): cheap-side flip, conf≥7
    # Mid (8-30¢): DIRECTIONAL, conf≥8 — THE WHALE ZONE
    # Expensive (30-60¢): DIRECTIONAL, conf≥9 — HIGH CONVICTION ONLY
    
    if confluence >= 9.0 and (
        (sig_dir == 'DOWN' and rsi < 28) or (sig_dir == 'UP' and rsi > 72)
    ):
        # Severe signal: play directional up to 30¢
        return 'mid', 'directional'
    
    if confluence >= 8.0 and (
        (sig_dir == 'DOWN' and rsi < 35) or (sig_dir == 'UP' and rsi > 65)
    ):
        # Moderate signal: play directional at 8-30¢
        return 'mid', 'directional'
    
    if confluence >= 7.0:
        # Any signal at conf≥7: cheap-side flip at 5-8¢
        return 'cheap', 'flip'
    
    if confluence >= 6.0:
        # Weak signal: ultra-cheap flip at 2-5¢
        return 'ultra_cheap', 'flip'
    
    return None, None

def run_backtest(candles, target_trades=500):
    prices = [c['close'] for c in candles]
    rsi_15m_map, rsi_1h_map = build_multi_tf_rsi(candles)
    print(f"  15m RSI: {len(rsi_15m_map)} candles, 1h RSI: {len(rsi_1h_map)} candles")
    
    bankroll = BANKROLL_START
    trades = []
    closed = []
    daily_losses = 0
    daily_loss_amt = 0.0
    current_date = None
    cooldown_until = 0
    
    skipped_session = 0
    skipped_confluence = 0
    skipped_dead_zone = 0
    
    tier_stats = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    
    i = 50
    while len(closed) < target_trades and i < len(prices) - 6:
        c = candles[i]
        ts = c.get('time', 0)
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        today = dt.strftime('%Y-%m-%d')
        
        if today != current_date:
            daily_losses = 0
            daily_loss_amt = 0.0
            current_date = today
        
        if daily_losses >= 3 or daily_loss_amt >= bankroll * 0.07:
            i += 3
            continue
        
        # Resolve open trades (3 candles = 15min for 5m data)
        for t in trades[:]:
            candles_ahead = i - t['candle_idx']
            if candles_ahead >= 3:
                entry_idx = t['candle_idx']
                price_at_entry = prices[entry_idx]
                price_at_exit = prices[min(entry_idx + 3, len(prices)-1)]
                price_went_up = price_at_exit > price_at_entry
                
                # Resolution depends on mode
                if t['entry_type'] == 'directional':
                    # DIRECTIONAL: we predicted a direction, check if it happened
                    if t['direction'] == 'DOWN':
                        won = not price_went_up  # DOWN signal → price must go DOWN
                    else:
                        won = price_went_up  # UP signal → price must go UP
                else:
                    # CHEAP-SIDE FLIP: we bought the opposite of signal
                    if t['cheap_direction'] == 'UP':
                        won = price_went_up
                    else:
                        won = not price_went_up
                
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
                closed.append(t)
                tier_stats[t['tier']]['wins'] += won
                tier_stats[t['tier']]['total'] += 1
                tier_stats[t['tier']]['pnl'] += pnl
                if pnl < 0:
                    daily_losses += 1
                    daily_loss_amt += abs(pnl)
                    if abs(pnl) > t['bet'] * STOP_LOSS_PCT * 0.5:
                        cooldown_until = i + 3
                trades.remove(t)
        
        if i < cooldown_until:
            i += 1
            continue
        
        # Session filter
        session = get_session(dt.hour)
        if session[0] in BLOCKED_SESSIONS:
            skipped_session += 1
            i += 1
            continue
        
        # Indicators
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
        rsi_15m_val = rsi_15m_map.get(i)
        rsi_1h_val = rsi_1h_map.get(i)
        
        # Signal direction
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
        
        # Dead zone: RSI 36-50
        if 36 <= rsi < 50:
            skipped_dead_zone += 1
            i += 1
            continue
        
        # Confluence
        confluence, details = compute_confluence(
            rsi, direction, regime, ema21, ema50, vwap, prices[i],
            macd_hist, session, (vol_regime, max_entry), sig_dir,
            rsi_15m=rsi_15m_val, rsi_1h=rsi_1h_val
        )
        
        if confluence < MIN_CONFLUENCE:
            skipped_confluence += 1
            i += 1
            continue
        
        # Select price tier
        tier, mode = select_tier(rsi, confluence, sig_dir)
        if tier is None:
            i += 1
            continue
        
        # Strategy name
        if rsi < 25 and sig_dir == 'DOWN':
            strategy = 'severe_oversold_down'
        elif rsi >= 75 and sig_dir == 'UP':
            strategy = 'severe_overbought_up'
        elif rsi < 35 and sig_dir == 'DOWN':
            strategy = 'oversold_down'
        elif rsi >= 65 and sig_dir == 'UP':
            strategy = 'overbought_up'
        else:
            strategy = 'directional'
        
        # Price simulation
        price_result = simulate_polymarket_price(rsi, direction, sig_dir, confluence, regime, dir_pct, mode)
        if price_result is None:
            i += 1
            continue
        entry_price, side, entry_type = price_result
        
        # Position sizing by tier
        tier_config = TIER_CONFIG[tier]
        min_price, max_price, min_conf, max_pct, tier_mode = tier_config
        
        if tier in ('mid', 'expensive'):
            # Directional: smaller bets
            bet_pct = max_pct * (confluence / 10.0)
        else:
            # Cheap: normal sizing
            bet_pct = max_pct
        
        if vol_regime == 'low_vol' and confluence >= 8:
            bet_pct *= 1.3
        elif vol_regime == 'high_vol':
            bet_pct *= 0.7
        
        bet = bankroll * bet_pct
        bet = max(0.50, min(bet, MAX_BET_SIZE, bankroll * 0.08))
        if bankroll - bet < 10:
            i += 1
            continue
        
        # For directional: cheap_direction = signal direction (not flipped)
        # For flip: cheap_direction = opposite
        if mode == 'directional':
            cheap_direction = sig_dir  # We buy the side matching our signal
            side_description = f'directional_{sig_dir}'
        else:
            cheap_direction = 'UP' if sig_dir == 'DOWN' else 'DOWN'
            side_description = f'flip_{cheap_direction}'
        
        trade = {
            'strategy': strategy,
            'direction': sig_dir,
            'cheap_direction': cheap_direction,
            'side': side_description,
            'entry_type': entry_type,
            'tier': tier,
            'rsi': round(rsi, 1),
            'rsi_15m': round(rsi_15m_val, 1) if rsi_15m_val else None,
            'rsi_1h': round(rsi_1h_val, 1) if rsi_1h_val else None,
            'confluence': round(confluence, 1),
            'details': details,
            'entry_price': round(entry_price, 3),
            'win_prob': round(1.0 / entry_price if entry_price > 0 else 0, 4),
            'bet_pct': round(bet_pct, 3),
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
        i += 1
    
    print(f"\n  SKIPPED: Session={skipped_session}, Confluence={skipped_confluence}, Dead zone={skipped_dead_zone}")
    return closed, bankroll, tier_stats

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=365)
    parser.add_argument('--trades', type=int, default=500)
    args = parser.parse_args()
    
    candles = download_klines('BTCUSDT', '5m', days=args.days)
    if len(candles) < 100:
        print("ERROR: Not enough data")
        sys.exit(1)
    
    print(f"\n{'='*70}")
    print(f"V19.9 WHALE-INSPIRED BACKTEST")
    print(f"{'='*70}")
    print(f"  Bankroll: ${BANKROLL_START}")
    print(f"  Tiers: ultra-cheap(2-5¢ flip), cheap(5-8¢ flip), mid(8-30¢ directional), expensive(30-60¢ directional)")
    print(f"  Sessions: NY only (block {BLOCKED_SESSIONS})")
    print(f"  MIN_CONFLUENCE: {MIN_CONFLUENCE}")
    print(f"  Dead zone: RSI 36-50 blocked")
    print(f"  Target: {args.trades} trades over {args.days} days")
    print(f"{'='*70}\n")
    
    closed, final_bankroll, tier_stats = run_backtest(candles, target_trades=args.trades)
    
    wins = [t for t in closed if t['outcome'] == 'win']
    losses = [t for t in closed if t['outcome'] == 'loss']
    
    wr = len(wins) / len(closed) * 100 if closed else 0
    pnl = final_bankroll - BANKROLL_START
    roi = pnl / BANKROLL_START * 100
    
    # Analysis
    by_strategy = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_session = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_tier = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_mode = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_confluence = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_price = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    
    for t in closed:
        by_strategy[t['strategy']]['wins'] += t['outcome'] == 'win'
        by_strategy[t['strategy']]['total'] += 1
        by_strategy[t['strategy']]['pnl'] += t['pnl']
        
        by_session[t['session']]['wins'] += t['outcome'] == 'win'
        by_session[t['session']]['total'] += 1
        by_session[t['session']]['pnl'] += t['pnl']
        
        by_tier[t['tier']]['wins'] += t['outcome'] == 'win'
        by_tier[t['tier']]['total'] += 1
        by_tier[t['tier']]['pnl'] += t['pnl']
        
        by_mode[t['entry_type']]['wins'] += t['outcome'] == 'win'
        by_mode[t['entry_type']]['total'] += 1
        by_mode[t['entry_type']]['pnl'] += t['pnl']
        
        cf = int(t['confluence'])
        by_confluence[cf]['wins'] += t['outcome'] == 'win'
        by_confluence[cf]['total'] += 1
        by_confluence[cf]['pnl'] += t['pnl']
        
        ep = t['entry_price']
        if ep <= 0.05: pb = '2-5c'
        elif ep <= 0.08: pb = '5-8c'
        elif ep <= 0.15: pb = '8-15c'
        elif ep <= 0.30: pb = '15-30c'
        else: pb = '30+c'
        by_price[pb]['wins'] += t['outcome'] == 'win'
        by_price[pb]['total'] += 1
        by_price[pb]['pnl'] += t['pnl']
    
    # Max drawdown
    peak = BANKROLL_START
    max_dd = 0
    running_pnl = BANKROLL_START
    for t in closed:
        running_pnl += t['pnl']
        if running_pnl > peak: peak = running_pnl
        dd = (peak - running_pnl) / peak * 100
        if dd > max_dd: max_dd = dd
    
    avg_entry = np.mean([t['entry_price'] for t in closed]) if closed else 0
    breakeven_wr = avg_entry * 100 if avg_entry > 0 else 0
    
    print(f"\n{'='*70}")
    print(f"V19.9 RESULTS")
    print(f"{'='*70}")
    print(f"  Trades: {len(closed)}")
    print(f"  Win rate: {len(wins)}/{len(closed)} = {wr:.1f}%")
    print(f"  Bankroll: ${final_bankroll:,.0f} (start ${BANKROLL_START:,.0f})")
    print(f"  ROI: {roi:.1f}%")
    print(f"  Max DD: {max_dd:.1f}%")
    print(f"  Avg entry: {avg_entry*100:.1f}¢")
    print(f"  Breakeven WR: {breakeven_wr:.1f}%")
    print(f"  Edge: {wr - breakeven_wr:.1f}pp")
    
    print(f"\n  BY TIER (KEY METRIC):")
    for k in ['ultra_cheap', 'cheap', 'mid', 'expensive']:
        if k in by_tier:
            v = by_tier[k]
            v_wr = v['wins']/v['total']*100 if v['total'] else 0
            print(f"    {k:15s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:,.0f}")
    
    print(f"\n  BY MODE (flip vs directional):")
    for k, v in sorted(by_mode.items(), key=lambda x: -x[1]['total']):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k:15s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:,.0f}")
    
    print(f"\n  BY STRATEGY:")
    for k, v in sorted(by_strategy.items(), key=lambda x: -x[1]['total']):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k:30s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:,.0f}")
    
    print(f"\n  BY SESSION:")
    for k, v in sorted(by_session.items(), key=lambda x: -x[1]['total']):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k:15s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:,.0f}")
    
    print(f"\n  BY CONFLUENCE:")
    for k in sorted(by_confluence.keys()):
        v = by_confluence[k]
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}/10: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:,.0f}")
    
    print(f"\n  BY PRICE TIER:")
    for k in ['2-5c', '5-8c', '8-15c', '15-30c', '30+c']:
        if k in by_price:
            v = by_price[k]
            v_wr = v['wins']/v['total']*100 if v['total'] else 0
            print(f"    {k:10s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:,.0f}")
    
    # Save
    results = {
        'version': 'V19.9',
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
        'by_session': {k: dict(v) for k, v in by_session.items()},
        'by_tier': {k: dict(v) for k, v in by_tier.items()},
        'by_mode': {k: dict(v) for k, v in by_mode.items()},
        'by_confluence': {k: dict(v) for k, v in by_confluence.items()},
        'by_price': {k: dict(v) for k, v in by_price.items()},
        'trades': closed,
    }
    
    with open(OUTPUT / 'backtest_v19_9_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\nResults saved to {OUTPUT / 'backtest_v19_9_results.json'}")