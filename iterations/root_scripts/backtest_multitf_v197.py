#!/usr/bin/env python3
"""
V19.7 MULTI-ASSET MULTI-TF — V3 FIXED
- Bankroll resets per strategy (not per asset — compound within strategy)
- Proper RSI zone logic from V19.7 live config
- Fixed compounding: sequential across assets with bankroll reset
"""
import json, math, random, time, sys
from pathlib import Path
import numpy as np

SEED = 42
random.seed(SEED)
np.random.seed(SEED)

DAYS = 180
DATA_DIR = Path("output")
INITIAL_BANKROLL = 320.0
NUM_TRADES_CAP = 5000
PM_FEE = 0.02

ASSETS = {'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'SOL': 'SOLUSDT', 'XRP': 'XRPUSDT'}
INTERVALS = ['5m', '15m']

STRATEGIES = {
    'V19.7 (live)': {'min_conf': 8.0, 'ny_only': True, 'max_price': 0.08, 'block_up_cheap': True, 'allow_overbought': True},
    'V19.7 RSI-only': {'min_conf': 7.5, 'ny_only': True, 'max_price': 0.08, 'block_up_cheap': True, 'allow_overbought': False},
    'Cheap ≤5¢': {'min_conf': 7.5, 'ny_only': True, 'max_price': 0.05, 'block_up_cheap': True, 'allow_overbought': True},
    'Whale 8-30¢': {'min_conf': 8.0, 'ny_only': True, 'min_price': 0.08, 'max_price': 0.30, 'block_up_cheap': False, 'allow_overbought': True},
    '24/7 No sess': {'min_conf': 8.0, 'ny_only': False, 'max_price': 0.08, 'block_up_cheap': True, 'allow_overbought': True},
    'Aggressive ≤15¢': {'min_conf': 7.0, 'ny_only': True, 'max_price': 0.15, 'block_up_cheap': True, 'allow_overbought': True},
}

def download_klines(symbol, interval, days):
    fn = DATA_DIR / f"{symbol}_{interval}_{days}d.json"
    if fn.exists():
        with open(fn) as f:
            return json.load(f)
    import urllib.request
    all_candles = []
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    current = start_ms
    while current < end_ms:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&startTime={current}&limit=1000"
        try:
            resp = urllib.request.urlopen(url, timeout=30)
            data = json.loads(resp.read())
        except:
            break
        if not data: break
        for d in data:
            all_candles.append({'open_time': d[0], 'open': float(d[1]), 'high': float(d[2]),
                'low': float(d[3]), 'close': float(d[4]), 'volume': float(d[5]),
                'close_time': d[6], 'quote_volume': float(d[7])})
        current = data[-1][0] + 1
        if len(data) < 1000: break
        time.sleep(0.1)
    fn.parent.mkdir(parents=True, exist_ok=True)
    with open(fn, 'w') as f:
        json.dump(all_candles, f)
    return all_candles

def vec_rsi(prices, period=14):
    n = len(prices)
    rsi = np.full(n, 50.0)
    if n < period + 1: return rsi
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0: rsi[i + 1] = 100.0
        else: rsi[i + 1] = min(100, max(0, 100 - 100 / (1 + avg_gain / avg_loss)))
    return rsi

def vec_ema(prices, period):
    n = len(prices)
    ema = np.zeros(n)
    if n < period: return np.full(n, prices[0])
    mult = 2.0 / (period + 1)
    ema[period - 1] = np.mean(prices[:period])
    for i in range(period, n):
        ema[i] = (prices[i] - ema[i - 1]) * mult + ema[i - 1]
    ema[:period - 1] = ema[period - 1]
    return ema

def precompute_all(prices, highs, lows, volumes, close_times):
    n = len(prices)
    rsi = vec_rsi(prices, 14)
    ema21 = vec_ema(prices, 21)
    ema50 = vec_ema(prices, 50)
    ema200 = vec_ema(prices, 200)
    ema12 = vec_ema(prices, 12)
    ema26 = vec_ema(prices, 26)
    macd_line = ema12 - ema26
    macd_signal = vec_ema(macd_line, 9)
    # VWAP rolling 20
    vwap = np.zeros(n)
    for i in range(n):
        s = max(0, i - 19)
        typical = (highs[s:i+1] + lows[s:i+1] + prices[s:i+1]) / 3
        vol_seg = volumes[s:i+1]
        vwap[i] = np.sum(typical * vol_seg) / max(np.sum(vol_seg), 1)
    # Session
    from datetime import datetime, timezone
    sessions = np.zeros(n, dtype=int)
    for i, ts in enumerate(close_times):
        h = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).hour
        if 13 <= h < 20: sessions[i] = 1
        elif 20 <= h < 24: sessions[i] = 2
        elif 7 <= h < 9: sessions[i] = 3
    return {'rsi': rsi, 'ema21': ema21, 'ema50': ema50, 'ema200': ema200,
            'macd_line': macd_line, 'macd_signal': macd_signal,
            'vwap': vwap, 'sessions': sessions}

def compute_confluence(rsi, price, ema21, ema50, ema200, direction, vwap, volume, avg_volume,
                       macd_line, macd_signal, session, regime, vol_band, mtf_rsi, price_struct):
    score = 0.0
    # 1. RSI extremity
    if direction == 'DOWN':
        if rsi < 15: score += 1.0
        elif rsi < 20: score += 0.85
        elif rsi < 25: score += 0.7
        elif rsi < 28: score += 0.55
        elif rsi < 35: score += 0.35
        else: score += 0.2
    else:
        if rsi > 85: score += 1.0
        elif rsi > 80: score += 0.85
        elif rsi > 75: score += 0.7
        elif rsi > 72: score += 0.55
        elif rsi > 65: score += 0.35
        else: score += 0.2
    # 2. Direction
    if direction == 'DOWN' and price < ema21: score += 1.0
    elif direction == 'DOWN' and price < ema50: score += 0.7
    elif direction == 'UP' and price > ema21: score += 1.0
    elif direction == 'UP' and price > ema50: score += 0.7
    # 3. EMA alignment
    if ema21 > ema50 > ema200: score += 1.0 if direction == 'UP' else 0.3
    elif ema21 < ema50 < ema200: score += 1.0 if direction == 'DOWN' else 0.3
    else: score += 0.5
    # 4. VWAP
    below = price < vwap
    if below and direction == 'DOWN': score += 1.0
    elif not below and direction == 'UP': score += 1.0
    else: score += 0.3
    # 5. MACD
    if macd_line < macd_signal and direction == 'DOWN': score += 1.0
    elif macd_line > macd_signal and direction == 'UP': score += 1.0
    else: score += 0.3
    # 6. Session
    session_scores = {0: 0.35, 1: 0.7, 2: 0.63, 3: 0.6}
    score += session_scores.get(session, 0.35)
    # 7. Volatility
    if vol_band == 'low_vol': score += 0.7
    elif vol_band == 'medium_vol': score += 1.0
    elif vol_band == 'high_vol': score += 0.5
    else: score += 0.7
    # 8. Regime
    if regime == 'ranging' and direction == 'DOWN': score += 1.0
    elif regime == 'ranging' and direction == 'UP': score += 0.7
    elif regime == 'trending_down' and direction == 'DOWN': score += 1.0
    elif regime == 'trending_up' and direction == 'UP': score += 1.0
    else: score += 0.3
    # 9. Multi-TF RSI
    score += mtf_rsi
    # 10. Price structure
    score += price_struct * 0.7
    return score

def run_strategy_on_asset(key, prices, volumes, ind, strat, bankroll_start=None):
    """Run strategy on a single asset/tf. Returns trades list with FIXED PnL per trade."""
    n = len(prices)
    rsi = ind['rsi']; ema21 = ind['ema21']; ema50 = ind['ema50']; ema200 = ind['ema200']
    macd_line = ind['macd_line']; macd_signal = ind['macd_signal']
    vwap = ind['vwap']; sessions = ind['sessions']

    bankroll = bankroll_start or INITIAL_BANKROLL
    peak = bankroll
    max_dd = 0.0
    trades = []
    i = 200

    while i < n - 20:
        i += 1
        price = prices[i]
        r = rsi[i]
        s = sessions[i]

        # Session filter
        if strat['ny_only'] and s not in (1, 2, 3): continue
        if s == 0: continue

        # Direction (V19.7 logic)
        if r < 28:
            sig_dir = 'DOWN'; sig_type = 'oversold_down'
        elif r > 72 and strat.get('allow_overbought', True):
            sig_dir = 'UP'; sig_type = 'overbought_up'
        elif r < 40 and price < vwap[i]:
            sig_dir = 'DOWN'; sig_type = 'direction_down_cheap'
        elif r > 60 and price > vwap[i]:
            if not strat.get('block_up_cheap', True) or price > 0.08:
                sig_dir = 'UP'; sig_type = 'direction_up'
            else:
                continue
        else:
            continue

        # Regime
        sma50_v = np.mean(prices[i-50:i+1])
        sma200_v = np.mean(prices[i-200:i+1])
        if sma50_v > sma200_v * 1.01: regime = 'trending_up'
        elif sma50_v < sma200_v * 0.99: regime = 'trending_down'
        else: regime = 'ranging'

        vol_mean = np.mean(volumes[i-200:i+1])
        vol_recent = np.mean(volumes[i-20:i+1])
        vol_band = 'low_vol' if vol_recent < vol_mean * 0.7 else 'high_vol' if vol_recent > vol_mean * 1.5 else 'medium_vol'

        # Entry price
        short_ret = (price - prices[max(0,i-6)]) / prices[max(0,i-6)] if i >= 6 else 0
        if sig_dir == 'DOWN':
            entry_price = max(0.01, min(0.50, 0.03 + short_ret * 0.5 + random.uniform(-0.01, 0.01)))
        else:
            entry_price = max(0.50, min(0.95, 0.97 - abs(short_ret) * 0.5 + random.uniform(-0.01, 0.01)))

        if entry_price < strat.get('min_price', 0.0): continue
        if entry_price > strat.get('max_price', 0.5): continue

        # Multi-TF RSI
        mtf_rsi = 0.5
        if i >= 15:
            rsi_short = vec_rsi(prices[max(0,i-15):i+1], 14)[-1]
            if sig_dir == 'DOWN' and rsi_short < 40: mtf_rsi = 1.0
            elif sig_dir == 'DOWN' and rsi_short < 50: mtf_rsi = 0.7
            elif sig_dir == 'UP' and rsi_short > 60: mtf_rsi = 1.0
            elif sig_dir == 'UP' and rsi_short > 50: mtf_rsi = 0.7

        price_struct = 0.3
        if i >= 2:
            if sig_dir == 'DOWN' and prices[i] < prices[i-1] < prices[i-2]: price_struct = 1.0
            elif sig_dir == 'UP' and prices[i] > prices[i-1] > prices[i-2]: price_struct = 1.0

        confluence = compute_confluence(
            r, price, ema21[i], ema50[i], ema200[i], sig_dir,
            vwap[i], volumes[i], vol_mean,
            macd_line[i], macd_signal[i], s, regime, vol_band,
            mtf_rsi, price_struct
        )
        confluence *= random.uniform(0.93, 1.0)  # execution noise
        if confluence < strat['min_conf']: continue

        # Resolve trade
        future = prices[i+1:i+4]
        if len(future) < 3: break
        won = (min(future) < price) if sig_dir == 'DOWN' else (max(future) > price)

        # Sizing: 3% of bankroll, min $1, max 6%
        sizing_pct = min(0.06, max(0.03, 0.01 / entry_price))
        bet = max(bankroll * sizing_pct, 1.0)
        bet = min(bet, bankroll * 0.06)

        if sig_dir == 'DOWN':
            pnl = bet * (1/entry_price - 1) * (1 - PM_FEE) if won else -bet
        else:
            pnl = bet * ((1 - entry_price) / entry_price) * (1 - PM_FEE) if won else -bet

        # Cap to avoid compounding explosion
        pnl = max(min(pnl, bankroll * 0.5), -bankroll * 0.06)

        bankroll += pnl
        if bankroll < 1: bankroll = 1.0
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak * 100
        max_dd = max(max_dd, dd)

        trades.append({
            'asset': key.split('_')[0], 'tf': key.split('_')[1],
            'dir': sig_dir, 'type': sig_type, 'price': round(entry_price, 4),
            'won': won, 'pnl': round(pnl, 2), 'confluence': round(confluence, 2),
            'rsi': round(r, 1), 'bankroll': round(bankroll, 2),
        })
        i += random.randint(1, 3)

    return trades, bankroll, max_dd

def main():
    print("=" * 70, flush=True)
    print("V19.7 MULTI-ASSET MULTI-TF HISTORICAL BACKTEST (V3)", flush=True)
    print(f"BTC, ETH, SOL, XRP × 5m,15m | $320 | cap 5000 trades/strat", flush=True)
    print("=" * 70, flush=True)

    # Load and precompute
    all_data = {}
    indicators = {}
    for aname, symbol in ASSETS.items():
        for interval in INTERVALS:
            key = f"{aname}_{interval}"
            candles = download_klines(symbol, interval, DAYS)
            prices = np.array([c['close'] for c in candles], dtype=np.float64)
            highs = np.array([c['high'] for c in candles], dtype=np.float64)
            lows = np.array([c['low'] for c in candles], dtype=np.float64)
            volumes = np.array([c['volume'] for c in candles], dtype=np.float64)
            opens = np.array([c['open'] for c in candles], dtype=np.float64)
            close_times = np.array([c['close_time'] for c in candles])
            print(f"  {key}: {len(candles)} candles ${min(prices):,.2f}-${max(prices):,.2f}", flush=True)
            all_data[key] = {'prices': prices, 'highs': highs, 'lows': lows,
                             'volumes': volumes, 'opens': opens, 'close_times': close_times, 'n': len(candles)}
            indicators[key] = precompute_all(prices, highs, lows, volumes, close_times)

    print("\nRunning 6 strategies...", flush=True)
    results = {}
    for sname, strat in STRATEGIES.items():
        all_trades = []
        max_dd_all = 0.0
        t0 = time.time()
        # Each strategy runs across ALL assets with BANKROLL CARRIED OVER
        bankroll = INITIAL_BANKROLL
        for key, data in all_data.items():
            trades, bankroll, dd = run_strategy_on_asset(
                key, data['prices'], data['volumes'], indicators[key], strat, bankroll
            )
            all_trades.extend(trades)
            max_dd_all = max(max_dd_all, dd)
            # Cap
            if len(all_trades) >= NUM_TRADES_CAP:
                break

        all_trades = all_trades[:NUM_TRADES_CAP]
        elapsed = time.time() - t0
        if not all_trades:
            results[sname] = {'trades': 0}
            print(f"  {sname}: 0 trades ({elapsed:.1f}s)", flush=True)
            continue

        wins = sum(1 for t in all_trades if t['won'])
        total_pnl = sum(t['pnl'] for t in all_trades)
        final = 320 + total_pnl
        wr = wins / len(all_trades) * 100
        results[sname] = {
            'trades': len(all_trades), 'wins': wins, 'wr': wr,
            'pnl': total_pnl, 'final': final, 'dd': max_dd_all, 'elapsed': elapsed,
        }
        print(f"  {sname}: {len(all_trades)} trades, {wr:.1f}% WR, ${final:,.0f}, +${total_pnl:+,.0f} PnL, {max_dd_all:.1f}% DD ({elapsed:.1f}s)", flush=True)

    # Asset breakdown for V19.7
    print("\n" + "=" * 70, flush=True)
    print("ASSET BREAKDOWN — V19.7 (live)", flush=True)
    print("=" * 70, flush=True)
    strat = STRATEGIES['V19.7 (live)']
    bankroll = INITIAL_BANKROLL
    for key, data in all_data.items():
        trades, bankroll, _ = run_strategy_on_asset(
            key, data['prices'], data['volumes'], indicators[key], strat, bankroll
        )
        if trades:
            wins = sum(1 for t in trades if t['won'])
            wr = wins / len(trades) * 100
            pnl = sum(t['pnl'] for t in trades)
            print(f"  {key}: {len(trades)} trades, {wr:.1f}% WR, ${pnl:+,.0f}", flush=True)

    # RSI zone breakdown
    print("\nRSI ZONE BREAKDOWN — V19.7 (live):", flush=True)
    strat = STRATEGIES['V19.7 (live)']
    all_t = []
    bankroll = INITIAL_BANKROLL
    for key, data in all_data.items():
        trades, bankroll, _ = run_strategy_on_asset(
            key, data['prices'], data['volumes'], indicators[key], strat, bankroll
        )
        all_t.extend(trades)
    all_t = all_t[:NUM_TRADES_CAP]

    zones = {'<15': [], '15-20': [], '20-25': [], '25-28': [], '28-35': [],
             '35-45': [], '65-72': [], '72-80': [], '>80': []}
    for t in all_t:
        r = t['rsi']
        if r < 15: zone = '<15'
        elif r < 20: zone = '15-20'
        elif r < 25: zone = '20-25'
        elif r < 28: zone = '25-28'
        elif r < 35: zone = '28-35'
        elif r < 45: zone = '35-45'
        elif r < 65: zone = '35-45'  # midzone
        elif r < 72: zone = '65-72'
        elif r < 80: zone = '72-80'
        else: zone = '>80'
        zones[zone].append(t)

    for zone, tl in zones.items():
        if tl:
            w = sum(1 for t in tl if t['won'])
            wr = w / len(tl) * 100
            pnl = sum(t['pnl'] for t in tl)
            print(f"  RSI {zone:>6s}: {len(tl):4d} trades, {wr:5.1f}% WR, ${pnl:+10,.0f}", flush=True)

    # Direction breakdown
    print("\nDIRECTION BREAKDOWN — V19.7 (live):", flush=True)
    down_t = [t for t in all_t if t['dir'] == 'DOWN']
    up_t = [t for t in all_t if t['dir'] == 'UP']
    if down_t:
        dw = sum(1 for t in down_t if t['won'])
        print(f"  DOWN: {len(down_t)} trades, {dw/len(down_t)*100:.1f}% WR, ${sum(t['pnl'] for t in down_t):+,.0f}", flush=True)
    if up_t:
        uw = sum(1 for t in up_t if t['won'])
        print(f"  UP:   {len(up_t)} trades, {uw/len(up_t)*100:.1f}% WR, ${sum(t['pnl'] for t in up_t):+,.0f}", flush=True)

    print("\nDone.", flush=True)

if __name__ == "__main__":
    main()