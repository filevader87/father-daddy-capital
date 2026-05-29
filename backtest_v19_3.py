#!/usr/bin/env python3
"""
V19.3 Backtest — 500 trades on real Binance BTC 5m data.
Uses actual RSI, EMA, direction, regime from historical candles.
Simulates Polymarket Up/Down binary contract resolution.
"""
import json, os, sys, math, time
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
# V19.3 CONFIG
# ═══════════════════════════════════════════════════════════════════════
BANKROLL_START = 400.0
MIN_CONFLUENCE = 7.0
CHEAP_MAX = 0.08  # Only enter at ≤8¢
STOP_LOSS_PCT = 0.50
TAKE_PROFIT_PRICE = 0.90
TRAILING_STOP_PCT = 0.40
TRAILING_ACTIVATE_MINS = 2.0
MAX_OPEN_POSITIONS = 3
MAX_SAME_DIRECTION = 2
DAILY_LOSS_LIMIT = 3
DAILY_LOSS_PCT = 0.07
COOLDOWN_MINS = 15
CONFLUENCE_SIZING = {7: 0.04, 8: 0.05, 9: 0.06}
BASE_SIZE = 0.03
MAX_BET_SIZE = 20.0  # Max $20 per bet (liquidity cap for cheap-side)

# Win probability tiers (from pm_engine_v18_8)
WIN_PROB_BASE = {
    'severe_oversold_down': 0.806,
    'severe_overbought_up': 0.871,
    'oversold_down': 0.74,
    'overbought_up': 0.71,
    'direction_down_cheap': 0.68,
    'direction_up_cheap': 0.70,
    'confluence_down': 0.72,
    'confluence_up': 0.72,
    'direction_down': 0.62,
    'direction_up': 0.64,
}

# Becker longshot bias
def calibrate_longshot(prob, price):
    if price <= 0.05: return prob * 0.836
    elif price <= 0.10: return prob * 0.90
    elif price <= 0.15: return prob * 0.95
    return prob

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
    # Signal line
    sig = np.mean(macd_line[-signal:])
    hist = macd_line[-1] - sig
    # Normalize by price
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
    
    # 1. RSI zone
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
    
    # 4. VWAP position
    if signal_dir == 'UP' and price > vwap: score += 1.0; details.append('Above_VWAP')
    elif signal_dir == 'DOWN' and price < vwap: score += 1.0; details.append('Below_VWAP')
    elif signal_dir == 'UP' and price < vwap * 1.002: score += 0.5; details.append('Near_VWAP')
    
    # 5. MACD
    if signal_dir == 'UP' and macd_hist > 0: score += 1.0; details.append('MACD_expanding')
    elif signal_dir == 'DOWN' and macd_hist < 0: score += 1.0; details.append('MACD_declining')
    elif abs(macd_hist) < 0.0001: score += 0.3
    
    # 6. Session
    session_name, session_weight = session
    score += session_weight
    details.append(f'S={session_name[:3]}({session_weight:.1f})')
    
    # 7. Vol regime
    vol_regime, max_entry = atr_vol
    if vol_regime == 'high_vol' and signal_dir in ('UP', 'DOWN'): score += 1.0
    elif vol_regime == 'low_vol' and regime in ('trending_up', 'trending_down'): score += 0.8
    elif vol_regime == 'medium_vol': score += 0.5
    
    # 8. Trend consistency
    if regime in ('trending_up',) and signal_dir == 'UP': score += 1.0; details.append('Trend_aligned')
    elif regime in ('trending_down',) and signal_dir == 'DOWN': score += 1.0; details.append('Trend_aligned')
    
    # 9. RSI divergence (simplified)
    if signal_dir == 'DOWN' and rsi < 40: score += 0.5
    elif signal_dir == 'UP' and rsi > 60: score += 0.5
    
    # 10. Price structure
    if signal_dir == 'DOWN' and price < vwap: score += 1.0
    elif signal_dir == 'UP' and price > vwap: score += 1.0
    else: score += 0.3
    
    return min(10.0, score), details

def generate_signal(prices, candles, idx):
    """Generate V19.3 signal at candle index idx. Returns None or signal dict."""
    if idx < 50 or idx >= len(prices) - 3:
        return None
    
    window = prices[:idx+1]
    rsi = compute_rsi(window)
    direction, dir_pct = detect_direction(window)
    regime = get_regime(window)
    ema21 = compute_ema(window, 21)
    ema50 = compute_ema(window, 50)
    vwap = compute_vwap(candles[:idx+1])
    macd_hist = compute_macd(window)
    atr_val = compute_atr(candles[:idx+1])
    vol_regime, max_entry = classify_volatility(atr_val, prices[idx])
    session = get_session(datetime.fromtimestamp(candles[idx]['time']/1000, tz=timezone.utc).hour)
    
    # Determine signal direction
    if direction in ('UP', 'DOWN'):
        sig_dir = direction
    else:
        # Confluence override: derive from regime + RSI
        if regime == 'trending_down' and rsi < 60:
            sig_dir = 'DOWN'
        elif regime == 'trending_up' and rsi > 40:
            sig_dir = 'UP'
        else:
            return None
    
    confluence, details = compute_confluence(
        rsi, direction, regime, ema21, ema50, vwap, prices[idx],
        macd_hist, session, (vol_regime, max_entry), sig_dir
    )
    
    if confluence < MIN_CONFLUENCE:
        return None
    
    # Determine strategy
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
    
    # Simulate cheap-side entry price (2-8¢)
    # Higher RSI for DOWN = higher price (market agrees DOWN more likely)
    # Lower RSI for DOWN = lower price (market disagrees, cheap side)
    if sig_dir == 'DOWN':
        # DOWN cheap side = DOWN token
        base_prob = 1.0 - rsi / 100.0  # Low RSI = market thinks DOWN likely = higher DOWN price
        entry_price = max(0.02, min(0.08, base_prob * 0.10 + np.random.uniform(0.01, 0.03)))
    else:
        base_prob = rsi / 100.0
        entry_price = max(0.02, min(0.08, base_prob * 0.10 + np.random.uniform(0.01, 0.03)))
    
    if entry_price > CHEAP_MAX:
        return None
    
    # Win probability
    raw_prob = WIN_PROB_BASE.get(strategy, 0.68)
    prob = calibrate_longshot(raw_prob, entry_price)
    confluence_boost = (confluence - 5) * 0.02
    prob = min(0.95, prob + confluence_boost)
    
    # Position sizing
    tier_size = CONFLUENCE_SIZING.get(int(confluence), BASE_SIZE)
    
    return {
        'strategy': strategy,
        'direction': sig_dir,
        'rsi': round(rsi, 1),
        'confluence': confluence,
        'details': details,
        'entry_price': round(entry_price, 3),
        'win_prob': round(prob, 4),
        'tier_size': tier_size,
        'vol_regime': vol_regime,
        'session': session[0],
        'regime': regime,
        'candle_idx': idx,
        'time': candles[idx].get('time', 0),
    }

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
# DOWNLOAD BINANCE DATA
# ═══════════════════════════════════════════════════════════════════════
def download_klines(symbol='BTCUSDT', interval='5m', days=45):
    """Download last N days of 5m klines from Binance."""
    all_candles = []
    end_time = int(time.time() * 1000)
    start_time = end_time - days * 24 * 60 * 60 * 1000
    
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&startTime={start_time}&endTime={end_time}&limit=1000'
    
    print(f"Downloading {symbol} {interval} klines for last {days} days...")
    
    current_start = start_time
    batch = 0
    while current_start < end_time:
        batch += 1
        url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&startTime={current_start}&limit=1000'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC/19.3'})
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
        time.sleep(0.5)  # Rate limit
    
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
            i += 3  # Skip to next 5m candle
            continue
        
        # Resolve open trades (check every 3 candles = 15 min windows)
        # Simulate 5m/15m resolution
        for t in trades[:]:
            candles_ahead = i - t['candle_idx']
            mins_elapsed = candles_ahead * 5
            
            # Resolution: after 15 min or at end of window
            if mins_elapsed >= 15 or candles_ahead >= 3:
                # Resolve based on ACTUAL historical price direction (no random override)
                entry_idx = t['candle_idx']
                # Check if price went in our predicted direction
                if t['direction'] == 'UP':
                    won = prices[min(entry_idx + 3, len(prices)-1)] > prices[entry_idx]
                else:
                    won = prices[min(entry_idx + 3, len(prices)-1)] < prices[entry_idx]
                
                if won:
                    # Win: payout = (1/entry_price - 1) * bet
                    payout = (1.0 / t['entry_price'] - 1.0) * t['bet']
                    pnl = payout
                else:
                    # Loss: model stop-loss (35% chance of 50% loss, 65% chance of full loss)
                    sl_chance = 0.35
                    if np.random.random() < sl_chance:
                        pnl = -t['bet'] * STOP_LOSS_PCT
                    else:
                        pnl = -t['bet']
                
                bankroll += pnl
                t['outcome'] = 'win' if won else 'loss'
                t['pnl'] = round(pnl, 2)
                t['exit_price'] = round(1.0 - t['entry_price'] if won else 0.0, 3)
                closed.append(t)
                
                if pnl < 0:
                    daily_losses += 1
                    daily_loss_amt += abs(pnl)
                    if abs(pnl) > t['bet'] * STOP_LOSS_PCT * 0.5:
                        cooldown_until = i + int(COOLDOWN_MINS / 5)  # Convert to candles
                
                n_direction_same[t['direction']] = max(0, n_direction_same.get(t['direction'], 0) - 1)
                trades.remove(t)
        
        # Skip if in cooldown
        if i < cooldown_until:
            i += 1
            continue
        
        # Try to generate a new signal
        sig = generate_signal(prices, candles, i)
        if sig is None:
            i += 1
            continue
        
        # Check too many open positions
        if len(trades) >= MAX_OPEN_POSITIONS:
            i += 1
            continue
        
        # Check same direction limit
        same_dir = sum(1 for t in trades if t['direction'] == sig['direction'])
        if same_dir >= MAX_SAME_DIRECTION:
            i += 1
            continue
        
        # Compute bet size (capped for liquidity on cheap-side)
        bet = bankroll * sig['tier_size']
        bet = max(0.50, min(bet, MAX_BET_SIZE, bankroll * 0.08))  # Cap at $20 and 8%
        
        if bankroll - bet < 10:  # Keep minimum reserve
            i += 1
            continue
        
        # Enter trade
        trade = {
            **sig,
            'bet': round(bet, 2),
            'bankroll_at_entry': round(bankroll, 2),
            'entry_time': dt.isoformat(),
        }
        trades.append(trade)
        # Don't deduct bet here — bankroll changes only on resolution
        n_direction_same[sig['direction']] = n_direction_same.get(sig['direction'], 0) + 1
        
        i += 1  # Move to next candle
    
    return closed, bankroll

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    candles = download_klines('BTCUSDT', '5m', days=45)
    
    if len(candles) < 100:
        print("ERROR: Not enough candle data")
        sys.exit(1)
    
    print(f"\n{'='*70}")
    print(f"V19.3 HISTORICAL BACKTEST — {len(candles)} candles, 5m BTC")
    print(f"{'='*70}")
    print(f"  Bankroll: ${BANKROLL_START}")
    print(f"  MIN_CONFLUENCE: {MIN_CONFLUENCE}")
    print(f"  Price gate: ≤{CHEAP_MAX*100:.0f}¢ only (cheap-side)")
    print(f"  Target: 500 trades")
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
    
    for t in closed:
        k = t['strategy']
        by_strategy[k]['wins'] += t['outcome'] == 'win'
        by_strategy[k]['total'] += 1
        by_strategy[k]['pnl'] += t['pnl']
        
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
    
    print(f"\n{'='*70}")
    print(f"V19.3 BACKTEST RESULTS")
    print(f"{'='*70}")
    print(f"  Total trades: {len(closed)}")
    print(f"  Win rate: {len(wins)}/{len(closed)} = {wr:.1f}%")
    print(f"  Final bankroll: ${final_bankroll:.2f} (start ${BANKROLL_START})")
    print(f"  Net PnL: ${pnl:.2f}")
    print(f"  ROI: {roi:.1f}%")
    print(f"  Max drawdown: {max_dd:.1f}%")
    print(f"\n  BY STRATEGY:")
    for k, v in sorted(by_strategy.items(), key=lambda x: -x[1]['total']):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k:30s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY DIRECTION:")
    for k, v in sorted(by_direction.items()):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY CONFLUENCE:")
    for k in sorted(by_confluence.keys()):
        v = by_confluence[k]
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}/10: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY PRICE TIER:")
    for k in sorted(by_price.keys()):
        v = by_price[k]
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    print(f"\n  BY SESSION:")
    for k, v in sorted(by_session.items(), key=lambda x: -x[1]['total']):
        v_wr = v['wins']/v['total']*100 if v['total'] else 0
        print(f"    {k:15s}: {v['wins']}/{v['total']} WR={v_wr:.1f}% PnL=${v['pnl']:.2f}")
    
    # Save results
    results = {
        'total_trades': len(closed),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': wr,
        'final_bankroll': final_bankroll,
        'pnl': pnl,
        'roi_pct': roi,
        'max_drawdown_pct': max_dd,
        'by_strategy': {k: dict(v) for k, v in by_strategy.items()},
        'by_direction': {k: dict(v) for k, v in by_direction.items()},
        'by_confluence': {k: dict(v) for k, v in by_confluence.items()},
        'by_price': {k: dict(v) for k, v in by_price.items()},
        'by_session': {k: dict(v) for k, v in by_session.items()},
        'trades': closed[:50],  # First 50 trades for inspection
    }
    
    with open(OUTPUT / 'backtest_v19_3_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\n  Results saved to {OUTPUT / 'backtest_v19_3_results.json'}")