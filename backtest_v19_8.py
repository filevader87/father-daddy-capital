#!/usr/bin/env python3
"""
V19.8 — 15m Resolution + Ultra-Selective Filters
==================================================
Only trade in NY sessions, only oversold UP or overbought DOWN flips,
conf≥8, RSI 28-36 for DOWN, RSI 65+ for UP.
Uses 15m candles for longer resolution and stronger signal.
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

# V19.8 Config — Ultra-selective
BANKROLL_START = 320.0
MIN_CONFLUENCE = 8.0
CHEAP_MAX = 0.08
STOP_LOSS_PCT = 0.50
MAX_BET_SIZE = 20.0
CONFLUENCE_SIZING = {8: 0.05, 9: 0.06, 10: 0.07}
BASE_SIZE = 0.04
FLIP_TO_CHEAP_SIDE = True

# RSI zones: ONLY trade in sweet spots
RSI_DOWN_MIN = 28    # DOWN signal: RSI must be 28-36
RSI_DOWN_MAX = 36
RSI_UP_MIN = 65      # UP signal: RSI must be 65+
RSI_UP_MAX = 100

# Block dead zones: RSI 36-50, RSI 50-58 UP
BLOCKED_RSI_ZONES = [(36, 50)]  # Dead zone

# Sessions: NY only
BLOCKED_SESSIONS = {'london', 'london_open', 'london_close', 'off_hours', 'asia'}

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
    
    # V19.8: RSI sweet-spot bonuses (28-36 DOWN, 65+ UP get max points)
    if signal_dir == 'DOWN' and rsi < 25: score += 1.5; details.append('RSI<25_severe')
    elif signal_dir == 'DOWN' and RSI_DOWN_MIN <= rsi < RSI_DOWN_MAX: score += 1.5; details.append(f'RSI_sweet_{rsi:.0f}')
    elif signal_dir == 'DOWN' and rsi < 40: score += 0.5; details.append(f'RSI_ok_{rsi:.0f}')
    elif signal_dir == 'UP' and rsi >= RSI_UP_MIN: score += 1.5; details.append(f'RSI_sweet_{rsi:.0f}')
    elif signal_dir == 'UP' and rsi > 55: score += 0.5; details.append(f'RSI_ok_{rsi:.0f}')
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
            # Triple alignment bonus
            if ((signal_dir == 'DOWN' and rsi_15m < 40 and rsi_1h < 45) or
                (signal_dir == 'UP' and rsi_15m > 60 and rsi_1h > 55)):
                score += 0.5; details.append('3TF_BONUS')
    
    return min(10.0, score), details

def simulate_polymarket_price(rsi, direction, sig_dir, confluence, regime, dir_pct):
    if sig_dir == 'DOWN':
        strength = (RSI_DOWN_MAX - min(rsi, RSI_DOWN_MAX)) / (RSI_DOWN_MAX - 20)
        strength *= (confluence / 10.0)
        cheap_price = 0.03 + (1.0 - strength) * 0.05
    else:
        strength = (min(rsi, 100) - RSI_UP_MIN) / (100 - RSI_UP_MIN)
        strength *= (confluence / 10.0)
        cheap_price = 0.03 + (1.0 - strength) * 0.05
    
    noise = random.gauss(0, 0.01)
    cheap_price += noise
    cheap_price = max(0.02, min(0.08, cheap_price))
    return (cheap_price, 'cheap_side', 'direct')

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
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC/19.8'})
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
    
    skipped_rsi = 0
    skipped_session = 0
    skipped_confluence = 0
    
    i = 50
    while len(closed) < target_trades and i < len(prices) - 6:  # 6 candles = 30min for 5m
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
        
        # Resolve open trades (3 candles = 15min resolution)
        for t in trades[:]:
            candles_ahead = i - t['candle_idx']
            if candles_ahead >= 3:
                entry_idx = t['candle_idx']
                price_at_entry = prices[entry_idx]
                price_at_exit = prices[min(entry_idx + 3, len(prices)-1)]
                price_went_up = price_at_exit > price_at_entry
                
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
                closed.append(t)
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
        
        # V19.8: RSI ZONE GATING — only trade in sweet spots
        if sig_dir == 'DOWN' and not (RSI_DOWN_MIN <= rsi <= RSI_DOWN_MAX or rsi < 25):
            # DOWN: only in oversold sweet spot (28-36) or severe oversold (<25)
            skipped_rsi += 1
            i += 1
            continue
        elif sig_dir == 'UP' and rsi < RSI_UP_MIN:
            # UP: only in overbought zone (65+)
            skipped_rsi += 1
            i += 1
            continue
        
        # Block RSI dead zone (36-50)
        if 36 <= rsi < 50:
            skipped_rsi += 1
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
        
        # Strategy
        if rsi < 25 and sig_dir == 'DOWN':
            strategy = 'severe_oversold_down'
        elif rsi >= 65 and sig_dir == 'UP':
            strategy = 'overbought_up' if rsi < 75 else 'severe_overbought_up'
        elif RSI_DOWN_MIN <= rsi <= RSI_DOWN_MAX and sig_dir == 'DOWN':
            strategy = 'oversold_down'
        else:
            strategy = 'other'
        
        # Price simulation
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
        
        cheap_direction = 'UP' if sig_dir == 'DOWN' else 'DOWN'
        
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
        i += 1
    
    print(f"\n  SKIPPED: RSI zone={skipped_rsi}, Session={skipped_session}, Confluence={skipped_confluence}")
    return closed, bankroll

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
    print(f"V19.8 ULTRA-SELECTIVE BACKTEST")
    print(f"{'='*70}")
    print(f"  Bankroll: ${BANKROLL_START}")
    print(f"  RSI sweet spots: DOWN={RSI_DOWN_MIN}-{RSI_DOWN_MAX}, UP≥{RSI_UP_MIN}")
    print(f"  Blocked RSI zones: {BLOCKED_RSI_ZONES}")
    print(f"  Sessions: NY only (block {BLOCKED_SESSIONS})")
    print(f"  MIN_CONFLUENCE: {MIN_CONFLUENCE}")
    print(f"  ≤8¢ ONLY, cheap-side flip")
    print(f"  Target: {args.trades} trades over {args.days} days")
    print(f"{'='*70}\n")
    
    closed, final_bankroll = run_backtest(candles, target_trades=args.trades)
    
    wins = [t for t in closed if t['outcome'] == 'win']
    losses = [t for t in closed if t['outcome'] == 'loss']
    
    wr = len(wins) / len(closed) * 100 if closed else 0
    pnl = final_bankroll - BANKROLL_START
    roi = pnl / BANKROLL_START * 100
    
    # Analysis
    by_strategy = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_session = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_rsi_zone = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_confluence = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    by_price = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    
    for t in closed:
        by_strategy[t['strategy']]['wins'] += t['outcome'] == 'win'
        by_strategy[t['strategy']]['total'] += 1
        by_strategy[t['strategy']]['pnl'] += t['pnl']
        
        by_session[t['session']]['wins'] += t['outcome'] == 'win'
        by_session[t['session']]['total'] += 1
        by_session[t['session']]['pnl'] += t['pnl']
        
        rsi = t['rsi']
        if rsi < 25: rsi_zone = 'RSI<25'
        elif rsi < 30: rsi_zone = 'RSI28-30'
        elif rsi < 36: rsi_zone = 'RSI30-36'
        elif rsi < 45: rsi_zone = 'RSI36-45'
        elif rsi < 55: rsi_zone = 'RSI45-55'
        elif rsi < 65: rsi_zone = 'RSI55-65'
        elif rsi < 75: rsi_zone = 'RSI65-75'
        else: rsi_zone = 'RSI75+'
        by_rsi_zone[rsi_zone]['wins'] += t['outcome'] == 'win'
        by_rsi_zone[rsi_zone]['total'] += 1
        by_rsi_zone[rsi_zone]['pnl'] += t['pnl']
        
        cf = int(t['confluence'])
        by_confluence[cf]['wins'] += t['outcome'] == 'win'
        by_confluence[cf]['total'] += 1
        by_confluence[cf]['pnl'] += t['pnl']
        
        ep = t['entry_price']
        price_bucket = f"{int(ep*100)}c"
        by_price[price_bucket]['wins'] += t['outcome'] == 'win'
        by_price[price_bucket]['total'] += 1
        by_price[price_bucket]['pnl'] += t['pnl']
    
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
    print(f"V19.8 RESULTS")
    print(f"{'='*70}")
    print(f"  Trades: {len(closed)}")
    print(f"  Win rate: {len(wins)}/{len(closed)} = {wr:.1f}%")
    print(f"  Bankroll: ${final_bankroll:,.0f} (start ${BANKROLL_START:,.0f})")
    print(f"  ROI: {roi:.1f}%")
    print(f"  Max DD: {max_dd:.1f}%")
    print(f"  Avg entry: {avg_entry*100:.1f}¢")
    print(f"  Breakeven WR: {breakeven_wr:.1f}%")
    print(f"  Edge: {wr - breakeven_wr:.1f}pp")
    
    print(f"\n  BY STRATEGY:")
    for k, v in sorted(by_strategy.items(), key=lambda x: -x[1]['total']):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k:30s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:,.0f}")
    
    print(f"\n  BY RSI ZONE:")
    for k in ['RSI<25', 'RSI28-30', 'RSI30-36', 'RSI36-45', 'RSI45-55', 'RSI55-65', 'RSI65-75', 'RSI75+']:
        if k in by_rsi_zone:
            v = by_rsi_zone[k]
            v_wr = v['wins']/v['total']*100 if v['total'] else 0
            print(f"    {k:15s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:,.0f}")
    
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
    for k in sorted(by_price.keys(), key=lambda x: int(x.rstrip('c'))):
        v = by_price[k]
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:,.0f}")
    
    # Save
    results = {
        'version': 'V19.8',
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
        'by_rsi_zone': {k: dict(v) for k, v in by_rsi_zone.items()},
        'by_confluence': {k: dict(v) for k, v in by_confluence.items()},
        'by_price': {k: dict(v) for k, v in by_price.items()},
        'trades': closed,
    }
    
    with open(OUTPUT / 'backtest_v19_8_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\nResults saved to {OUTPUT / 'backtest_v19_8_results.json'}")