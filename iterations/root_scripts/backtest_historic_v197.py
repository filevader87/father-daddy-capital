#!/usr/bin/env python3
"""
V19.7 HISTORICAL BACKTEST — OPTIMIZED
Real BTC 5m data from Binance, 6 strategies, $320 bankroll.
Precomputes indicators for speed.
"""
import json, math, random, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path
import urllib.request
import numpy as np

random.seed(42)
np.random.seed(42)
OUTPUT = Path(__file__).parent / "output"
OUTPUT.mkdir(exist_ok=True)

def download_klines(symbol='BTCUSDT', interval='5m', days=180):
    cache_file = OUTPUT / f"btc_5m_{days}d.json"
    if cache_file.exists():
        with open(cache_file) as f: return json.load(f)
    all_candles = []
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    current_ms = start_ms
    while current_ms < end_ms:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&startTime={current_ms}&limit=1000"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as e: time.sleep(2); continue
        for k in data:
            all_candles.append({'open_time': k[0], 'open': float(k[1]), 'high': float(k[2]),
                'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5]), 'close_time': k[6]})
        if not data: break
        current_ms = data[-1][6] + 1
        time.sleep(0.1)
    with open(cache_file, 'w') as f: json.dump(all_candles, f)
    return all_candles

def compute_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    deltas = np.diff(prices[-(period+1):])
    gains = np.where(deltas > 0, deltas, 0); losses = np.where(deltas < 0, -deltas, 0)
    ag = np.mean(gains); al = np.mean(losses)
    if al < 1e-10: return 100.0
    return min(100, max(0, 100 - 100 / (1 + ag/al)))

def compute_ema(prices, period):
    if len(prices) < period: return prices[-1] if prices else 0
    ema = float(np.mean(prices[:period]))
    k = 2 / (period + 1)
    for p in prices[period:]: ema = (p - ema) * k + ema
    return ema

def get_session(utc_hour):
    est_hour = (utc_hour - 5) % 24
    if 7 <= est_hour < 11: return ('ny_open', 1.0)
    elif 10 <= est_hour < 12: return ('london_close', 0.8)
    elif 12 <= est_hour < 16: return ('ny_afternoon', 0.7)
    elif 2 <= est_hour < 5: return ('london_open', 0.8)
    elif 5 <= est_hour < 7: return ('london', 0.8)
    elif 19 <= est_hour or est_hour < 3: return ('asia', 0.7)
    return ('off_hours', 0.5)

def compute_confluence(rsi, direction, regime, ema21, ema50, vwap, price, macd_hist, session, atr_vol, signal_dir, rsi_15m=None, rsi_1h=None, candle_body_bullish=False):
    score = 0.0
    if signal_dir == "DOWN" and rsi < 25: score += 1.0
    elif signal_dir == "DOWN" and rsi < 30: score += 0.8
    elif signal_dir == "DOWN" and rsi < 38: score += 0.3
    elif signal_dir == "UP" and rsi > 75: score += 1.0
    elif signal_dir == "UP" and rsi > 65: score += 0.8
    elif signal_dir == "UP" and rsi > 55: score += 0.3
    if direction in ("UP", "DOWN"): score += 1.0
    if ema21 > ema50 and signal_dir == "UP": score += 1.0
    elif ema21 < ema50 and signal_dir == "DOWN": score += 1.0
    elif abs(ema21 - ema50) / max(ema50, 0.001) < 0.001: score += 0.3
    if signal_dir == "UP" and price > vwap: score += 1.0
    elif signal_dir == "DOWN" and price < vwap: score += 1.0
    elif signal_dir == "UP" and price < vwap * 1.002: score += 0.5
    if signal_dir == "UP" and macd_hist > 0: score += 1.0
    elif signal_dir == "DOWN" and macd_hist < 0: score += 1.0
    elif abs(macd_hist) < 0.0001: score += 0.3
    session_name, session_weight = session
    score += session_weight
    vol_regime, _ = atr_vol
    if vol_regime == "high_vol" and signal_dir in ("UP", "DOWN"): score += 1.0
    elif vol_regime == "low_vol" and regime in ("trending_up", "trending_down"): score += 0.8
    elif vol_regime == "medium_vol": score += 0.5
    if regime == "trending_up" and signal_dir == "UP": score += 1.0
    elif regime == "trending_down" and signal_dir == "DOWN": score += 1.0
    elif regime == "ranging": score += 0.3
    mtf = 0.0
    if rsi_15m is not None:
        if signal_dir == "DOWN" and rsi_15m < 40: mtf += 0.5
        elif signal_dir == "UP" and rsi_15m > 60: mtf += 0.5
        if rsi_1h is not None:
            if signal_dir == "DOWN" and rsi_1h < 45: mtf += 0.5
            elif signal_dir == "UP" and rsi_1h > 55: mtf += 0.5
    if mtf == 0: mtf = 0.3
    score += mtf
    structure = 0.0
    if signal_dir == "UP" and price > ema21: structure += 0.7
    elif signal_dir == "DOWN" and price < ema21: structure += 0.7
    else: structure += 0.3
    if candle_body_bullish: structure += 0.3
    score += min(1.0, structure)
    return min(10.0, score)

STRATEGIES = {
    "V19.7 (live)": {"max_entry": 0.08, "ny_only": True, "min_conf": 8.0, "block_up_cheap": True, "block_overbought": False, "position_pct": 0.03, "min_bet": 1.0, "max_bet": 100, "max_pos_pct": 0.06, "daily_loss_limit": 3},
    "V19.7 RSI-only": {"max_entry": 0.08, "ny_only": True, "min_conf": 8.0, "block_up_cheap": True, "block_overbought": True, "position_pct": 0.03, "min_bet": 1.0, "max_bet": 100, "max_pos_pct": 0.06, "daily_loss_limit": 3},
    "Cheap <=5c": {"max_entry": 0.05, "ny_only": True, "min_conf": 8.0, "block_up_cheap": True, "block_overbought": False, "position_pct": 0.04, "min_bet": 1.0, "max_bet": 100, "max_pos_pct": 0.08, "daily_loss_limit": 3},
    "Whale 8-30c": {"max_entry": 0.30, "ny_only": True, "min_conf": 8.0, "block_up_cheap": False, "block_overbought": False, "position_pct": 0.03, "min_bet": 1.0, "max_bet": 100, "max_pos_pct": 0.06, "daily_loss_limit": 3},
    "Aggressive <=15c": {"max_entry": 0.15, "ny_only": True, "min_conf": 7.0, "block_up_cheap": False, "block_overbought": False, "position_pct": 0.04, "min_bet": 1.0, "max_bet": 100, "max_pos_pct": 0.08, "daily_loss_limit": 4},
    "24/7 No sess": {"max_entry": 0.08, "ny_only": False, "min_conf": 8.0, "block_up_cheap": True, "block_overbought": False, "position_pct": 0.03, "min_bet": 1.0, "max_bet": 100, "max_pos_pct": 0.06, "daily_loss_limit": 3},
}

def run_backtest(strategy, candles, prices, rsi_arr, ema21_arr, ema50_arr, vwap_arr, macd_arr, regime_arr, atr_arr, vol_arr, num_trades=5000):
    bankroll = 320.0
    date_str = ""
    trades = []
    daily_losses = 0
    current_date = None
    max_bankroll = bankroll
    max_dd = 0.0
    open_positions = []
    n = len(prices)
    
    i = 100
    while len(trades) < num_trades and i < n - 20:
        c = candles[i]
        dt = datetime.fromtimestamp(c['open_time'] / 1000, tz=timezone.utc)
        date_str = dt.strftime('%Y-%m-%d')
        if date_str != current_date:
            current_date = date_str
            daily_losses = 0
        
        # Resolve open positions
        for pos in open_positions[:]:
            if i - pos['idx'] >= 3:
                p_entry = prices[pos['idx']]
                p_exit = prices[min(pos['idx'] + 3, n-1)]
                went_up = p_exit > p_entry
                won = (pos['cheap_dir'] == 'UP' and went_up) or (pos['cheap_dir'] == 'DOWN' and not went_up)
                if won:
                    pnl = pos['bet'] * (1/pos['entry_price'] - 1) * 0.98
                else:
                    pnl = -pos['bet'] * random.uniform(0.4, 1.0) if random.random() < 0.30 else -pos['bet']
                bankroll += pnl
                trades.append({
                    'direction': pos['sig_dir'], 'strategy': pos['strategy_name'],
                    'entry_price': pos['entry_price'], 'cheap_dir': pos['cheap_dir'],
                    'confluence': pos['confluence'], 'rsi': pos['rsi'],
                    'session': pos['session'][0], 'regime': pos['regime'],
                    'won': won, 'bet': pos['bet'], 'pnl': pnl,
                    'bankroll': bankroll, 'rsi_zone': pos['rsi_zone'],
                    'price_tier': pos['price_tier'], 'date': date_str,
                })
                max_bankroll = max(max_bankroll, bankroll)
                dd = (max_bankroll - bankroll) / max_bankroll if max_bankroll > 0 else 0
                max_dd = max(max_dd, dd)
                if not won: daily_losses += 1
                open_positions.remove(pos)
                if bankroll < 5: return trades, bankroll, max_dd
        
        if daily_losses >= strategy['daily_loss_limit']:
            i += 1; continue
        
        session = get_session(dt.hour)
        if strategy['ny_only'] and session[0] not in ('ny_open', 'ny_afternoon', 'london_close'):
            i += 1; continue
        
        # Use precomputed indicators
        rsi = rsi_arr[i]
        ema21_val = ema21_arr[i]
        ema50_val = ema50_arr[i]
        vwap_val = vwap_arr[i]
        macd_val = macd_arr[i]
        regime = regime_arr[i]
        atr_v = atr_arr[i]
        vol_regime = vol_arr[i]
        atr_vol = (vol_regime, 0.08 if vol_regime == 'high_vol' else 0.15 if vol_regime == 'medium_vol' else 0.20)
        
        # Multi-TF RSI
        prices_15m = prices[:i+1:3]
        prices_1h = prices[:i+1:12]
        rsi_15m = compute_rsi(prices_15m) if len(prices_15m) >= 15 else None
        rsi_1h = compute_rsi(prices_1h) if len(prices_1h) >= 15 else None
        
        # Signal direction
        short_ret = (prices[i] - prices[max(0, i-6)]) / prices[max(0, i-6)] if i >= 6 else 0
        if rsi < 28: sig_dir = 'DOWN'; strategy_name = 'oversold_down'
        elif rsi > 72: sig_dir = 'UP'; strategy_name = 'overbought_up'
        elif rsi < 42 and short_ret < -0.001: sig_dir = 'DOWN'; strategy_name = 'direction_down_cheap'
        elif rsi > 58 and short_ret > 0.001: sig_dir = 'UP'; strategy_name = 'direction_up_cheap'
        else: i += 1; continue
        
        if strategy['block_up_cheap'] and strategy_name == 'direction_up_cheap': i += 1; continue
        if strategy['block_overbought'] and strategy_name == 'overbought_up': i += 1; continue
        
        candle_body = c['close'] - c['open']
        cheap_dir = 'UP' if sig_dir == 'DOWN' else 'DOWN'
        candle_body_bullish = (cheap_dir == 'UP' and candle_body > 0) or (cheap_dir == 'DOWN' and candle_body < 0)
        
        direction = sig_dir
        confluence = compute_confluence(rsi, direction, regime, ema21_val, ema50_val, vwap_val, prices[i], macd_val, session, atr_vol, sig_dir, rsi_15m, rsi_1h, candle_body_bullish)
        
        # Execution risk noise
        confluence *= random.uniform(0.93, 1.0)
        
        if confluence < strategy['min_conf']: i += 1; continue
        
        # Entry price (cheap side)
        if strategy_name == 'oversold_down' and rsi < 20:
            base_price = 0.02 + (0.03 * (1 - confluence/10))
        elif strategy_name in ('oversold_down', 'overbought_up'):
            base_price = 0.03 + (0.05 * (1 - confluence/10))
        elif strategy_name in ('direction_down_cheap', 'direction_up_cheap'):
            base_price = 0.04 + (0.04 * (1 - confluence/10))
        else:
            base_price = 0.05 + (0.03 * (1 - confluence/10))
        
        entry_price = base_price + random.gauss(0, 0.005)
        entry_price = max(0.01, min(strategy['max_entry'], entry_price))
        
        size = strategy['position_pct']
        if confluence >= 9.0: size *= 1.3
        elif confluence >= 8.5: size *= 1.15
        size = min(size, strategy['max_pos_pct'])
        bet = max(bankroll * size, strategy['min_bet'])
        bet = min(bet, strategy['max_bet'], bankroll * 0.10)
        if bet < 0.50 or bankroll < 10: i += 1; continue
        
        if entry_price <= 0.03: price_tier = "1-3c"
        elif entry_price <= 0.05: price_tier = "4-5c"
        elif entry_price <= 0.08: price_tier = "6-8c"
        elif entry_price <= 0.15: price_tier = "9-15c"
        else: price_tier = "16-30c"
        
        if rsi < 20: rsi_zone = "<20"
        elif rsi < 25: rsi_zone = "20-25"
        elif rsi < 30: rsi_zone = "25-30"
        elif rsi < 40: rsi_zone = "30-40"
        elif rsi < 50: rsi_zone = "40-50"
        elif rsi < 60: rsi_zone = "50-60"
        elif rsi < 70: rsi_zone = "60-70"
        elif rsi < 75: rsi_zone = "70-75"
        else: rsi_zone = "75+"
        
        open_positions.append({
            'idx': i, 'sig_dir': sig_dir, 'strategy_name': strategy_name,
            'entry_price': entry_price, 'cheap_dir': cheap_dir,
            'confluence': confluence, 'rsi': rsi, 'rsi_zone': rsi_zone,
            'session': session, 'regime': regime, 'bet': bet,
            'price_tier': price_tier,
        })
        i += random.randint(1, 3)
    
    # Resolve remaining
    for pos in open_positions[:]:
        p_entry = prices[pos['idx']]; p_exit = prices[min(pos['idx'] + 3, n-1)]
        went_up = p_exit > p_entry
        won = (pos['cheap_dir'] == 'UP' and went_up) or (pos['cheap_dir'] == 'DOWN' and not went_up)
        pnl = pos['bet'] * (1/pos['entry_price'] - 1) * 0.98 if won else -pos['bet']
        bankroll += pnl
        trades.append({
            'direction': pos['sig_dir'], 'strategy': pos['strategy_name'],
            'entry_price': pos['entry_price'], 'cheap_dir': pos['cheap_dir'],
            'confluence': pos['confluence'], 'rsi': pos['rsi'],
            'session': pos['session'][0], 'regime': pos['regime'],
            'won': won, 'bet': pos['bet'], 'pnl': pnl,
            'bankroll': bankroll, 'rsi_zone': pos['rsi_zone'],
            'price_tier': pos['price_tier'], 'date': date_str if current_date else '',
        })
    return trades, bankroll, max_dd

def main():
    candles = download_klines('BTCUSDT', '5m', 180)
    prices = [c['close'] for c in candles]
    n = len(prices)
    print(f"Loaded {n} candles, ${min(prices):,.0f}-${max(prices):,.0f}")
    
    # Precompute indicators
    print("Precomputing indicators...")
    rsi_arr = np.full(n, 50.0)
    ema21_arr = np.zeros(n)
    ema50_arr = np.zeros(n)
    vwap_arr = np.zeros(n)
    macd_arr = np.zeros(n)
    regime_arr = ['ranging'] * n
    atr_arr = np.zeros(n)
    vol_arr = ['medium_vol'] * n
    
    for i in range(100, n):
        window = prices[:i+1]
        rsi_arr[i] = compute_rsi(window)
        ema21_arr[i] = compute_ema(window, 21)
        ema50_arr[i] = compute_ema(window, 50) if len(window) >= 50 else ema21_arr[i]
        # VWAP (simplified: volume-weighted mean)
        recent = candles[max(0, i-20):i+1]
        tvol = sum(c.get('volume', 1) for c in recent)
        if tvol > 0:
            vwap_arr[i] = sum(c.get('volume', 1) * (c['high'] + c['low'] + c['close']) / 3 for c in recent) / tvol
        else:
            vwap_arr[i] = prices[i]
        # MACD
        if len(window) >= 35:
            f = 2/(12+1); s = 2/(26+1)
            ef = np.mean(window[:12]); es = np.mean(window[:26])
            for p in window[12:]: ef = (p-ef)*f + ef
            for p in window[26:]: es = (p-es)*s + es
            macd_arr[i] = (ef - es) / prices[i] if prices[i] > 0 else 0
        # ATR
        recent_c = candles[max(0, i-15):i+1]
        if len(recent_c) >= 2:
            trs = []
            for j in range(1, len(recent_c)):
                c = recent_c[j]; p = recent_c[j-1]
                tr = max(c['high']-c['low'], abs(c['high']-p['close']), abs(c['low']-p['close']))
                trs.append(tr)
            atr_arr[i] = sum(trs[-14:]) / min(14, len(trs)) if trs else 0
        
        # Regime
        if len(window) >= 21:
            rets = np.diff(window[-21:]) / window[-21:-1]
            mu = np.mean(rets); sigma = np.std(rets)
            if sigma < 0.0005: regime_arr[i] = 'ranging'
            elif mu > 2*sigma: regime_arr[i] = 'trending_up'
            elif mu < -2*sigma: regime_arr[i] = 'trending_down'
            elif sigma > 0.003: regime_arr[i] = 'volatile'
            else: regime_arr[i] = 'ranging'
        
        # Vol classification
        if atr_arr[i] / prices[i] > 0.005: vol_arr[i] = 'high_vol'
        elif atr_arr[i] / prices[i] > 0.002: vol_arr[i] = 'medium_vol'
        else: vol_arr[i] = 'low_vol'
    
    print(f"Precomputed indicators for {n} candles\n")
    
    results = {}
    for name, strat in STRATEGIES.items():
        t0 = time.time()
        random.seed(42); np.random.seed(42)
        trades, bankroll, max_dd = run_backtest(strat, candles, prices, rsi_arr, ema21_arr, ema50_arr, vwap_arr, macd_arr, regime_arr, atr_arr, vol_arr, 5000)
        elapsed = time.time() - t0
        
        if not trades:
            results[name] = {'trades': 0}
            print(f"  {name}: 0 trades ({elapsed:.1f}s)")
            continue
        
        wins = sum(1 for t in trades if t['won'])
        pt = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
        st = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
        reg = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
        ses = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
        rz = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
        
        for t in trades:
            pt[t['price_tier']]["n"] += 1; pt[t['price_tier']]["w"] += (1 if t['won'] else 0); pt[t['price_tier']]["pnl"] += t['pnl']
            st[t['strategy']]["n"] += 1; st[t['strategy']]["w"] += (1 if t['won'] else 0); st[t['strategy']]["pnl"] += t['pnl']
            reg[t['regime']]["n"] += 1; reg[t['regime']]["w"] += (1 if t['won'] else 0); reg[t['regime']]["pnl"] += t['pnl']
            ses[t['session']]["n"] += 1; ses[t['session']]["w"] += (1 if t['won'] else 0); ses[t['session']]["pnl"] += t['pnl']
            rz[t['rsi_zone']]["n"] += 1; rz[t['rsi_zone']]["w"] += (1 if t['won'] else 0); rz[t['rsi_zone']]["pnl"] += t['pnl']
        
        for d in [pt, st, reg, ses, rz]:
            for k, v in d.items(): v['wr'] = v['w'] / v['n'] * 100 if v['n'] > 0 else 0
        
        ms = streak = 0
        for t in trades:
            if not t['won']: streak += 1; ms = max(ms, streak)
            else: streak = 0
        f50 = sum(1 for t in trades[-50:] if t['won']) / min(50, len(trades)) * 100
        
        milestones = []
        for m in [0, 10, 25, 50, 100, 250, 500, 1000, 2000, min(4999, len(trades)-1)]:
            if m < len(trades):
                t = trades[m]
                wr = sum(1 for tr in trades[:m+1] if tr['won']) / (m+1) * 100
                milestones.append({'trade': m, 'bankroll': t['bankroll'], 'wr': wr})
        
        results[name] = {
            'trades': len(trades), 'wins': wins, 'wr': wins/len(trades)*100,
            'total_pnl': sum(t['pnl'] for t in trades), 'final_bankroll': bankroll,
            'max_dd': max_dd * 100, 'worst_streak': ms, 'last50_wr': f50,
            'price_tiers': dict(pt), 'sig_types': dict(st),
            'regimes': dict(reg), 'sessions': dict(ses),
            'rsi_zones': dict(rz), 'milestones': milestones,
        }
        print(f"  {name}: {len(trades)} trades, {wins/len(trades)*100:.1f}% WR, ${bankroll:,.0f}, +${sum(t['pnl'] for t in trades):,.0f} PnL ({elapsed:.1f}s)")
    
    # Print report
    print("\n" + "=" * 90)
    print("V19.7 HISTORICAL BACKTEST — REAL BTC 5M DATA (180 DAYS) × 6 STRATEGIES")
    print("$320 start | $100 max bet | 2% PM fee | 30% chance partial loss")
    print("=" * 90)
    
    print("\n## STRATEGY COMPARISON (ranked by total PnL)")
    print("-" * 90)
    print("{:<20} {:>5} {:>5} {:>6} {:>11} {:>9} {:>5} {:>4} {:>5}".format(
        "Strategy", "N", "Win", "WR%", "Total PnL", "Final$", "DD%", "Strk", "50WR"))
    print("-" * 90)
    for name, r in sorted(results.items(), key=lambda x: x[1].get('total_pnl', -1e99), reverse=True):
        if r['trades'] == 0: print(f"  {name}: 0 trades"); continue
        print("{:<20} {:>5d} {:>5d} {:>5.1f}% {:>+10,.0f} {:>9,.0f} {:>5.1f}% {:>4d} {:>5.1f}%".format(
            name[:20], r['trades'], r['wins'], r['wr'], r['total_pnl'], r['final_bankroll'], r['max_dd'], r['worst_streak'], r['last50_wr']))
    
    v197 = results.get("V19.7 (live)", {})
    if v197.get('trades', 0) > 0:
        print("\n## V19.7 PRICE TIER")
        for tier in ["1-3c", "4-5c", "6-8c", "9-15c", "16-30c"]:
            d = v197.get('price_tiers', {}).get(tier, {})
            if d and d.get('n', 0) > 0:
                print(f"  {tier:<10} {d['n']:>5d} | {d['wr']:>5.1f}% WR | {d['pnl']:>+10,.0f} PnL")
        
        print("\n## V19.7 SIGNAL TYPE")
        for sig in ['oversold_down', 'overbought_up', 'direction_down_cheap', 'direction_up_cheap']:
            d = v197.get('sig_types', {}).get(sig, {})
            if d and d.get('n', 0) > 0:
                print(f"  {sig:<25} {d['n']:>5d} | {d['wr']:>5.1f}% WR | {d['pnl']:>+10,.0f} PnL")
        
        print("\n## V19.7 REGIME")
        for regime in ['trending_down', 'trending_up', 'ranging', 'volatile']:
            d = v197.get('regimes', {}).get(regime, {})
            if d and d.get('n', 0) > 0:
                print(f"  {regime:<15} {d['n']:>5d} | {d['wr']:>5.1f}% WR | {d['pnl']:>+10,.0f} PnL")
        
        print("\n## V19.7 RSI ZONE")
        for zone in ["<20", "20-25", "25-30", "30-40", "40-50", "50-60", "60-70", "70-75", "75+"]:
            d = v197.get('rsi_zones', {}).get(zone, {})
            if d and d.get('n', 0) > 0:
                print(f"  RSI {zone:<8} {d['n']:>5d} | {d['wr']:>5.1f}% WR | {d['pnl']:>+10,.0f} PnL")
        
        print("\n## V19.7 SESSION")
        for sess in ['ny_open', 'ny_afternoon', 'london_close']:
            d = v197.get('sessions', {}).get(sess, {})
            if d and d.get('n', 0) > 0:
                print(f"  {sess:<15} {d['n']:>5d} | {d['wr']:>5.1f}% WR | {d['pnl']:>+10,.0f} PnL")
        
        print("\n## V19.7 BANKROLL EVOLUTION")
        for m in v197.get('milestones', []):
            print(f"  Trade {m['trade']:>5d}: ${m['bankroll']:>12,.2f} | WR: {m['wr']:>5.1f}%")
    
    print("\n## KEY FINDINGS")
    print("=" * 60)
    valid = {k: v for k, v in results.items() if v.get('trades', 0) > 0}
    if valid:
        best = max(valid.items(), key=lambda x: x[1]['total_pnl'])
        safest = min(valid.items(), key=lambda x: x[1]['max_dd'])
        best_wr = max(valid.items(), key=lambda x: x[1]['wr'])
        print(f"  Best PnL:     {best[0]} — {best[1]['total_pnl']:>+,.0f}, {best[1]['wr']:.1f}% WR")
        print(f"  Safest:       {safest[0]} — {safest[1]['max_dd']:.1f}% DD")
        print(f"  Best WR:      {best_wr[0]} — {best_wr[1]['wr']:.1f}% WR, {best_wr[1]['trades']} trades")
        
        v = valid.get("V19.7 (live)", {})
        if v:
            print(f"\n  V19.7: {v['trades']} trades, ${320:.0f}→${v['final_bankroll']:,.0f} ({(v['final_bankroll']-320)/320*100:.0f}% ROI)")
            print(f"  WR: {v['wr']:.1f}% | DD: {v['max_dd']:.1f}% | Worst streak: {v['worst_streak']} | Last 50: {v['last50_wr']:.1f}%")

if __name__ == "__main__":
    main()