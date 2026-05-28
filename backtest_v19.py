#!/usr/bin/env python3
"""
V19 Binance Backtest — Krajekis-Enhanced with Confluence Scoring
==================================================================
V18.9 + VWAP + EMA21/50 + ATR + session logic + confluence + daily loss limit
"""

import json, os, sys, time, math
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

# Import V19 indicators directly
sys.path.insert(0, str(REPO))
from paper_trade_v19 import (
    compute_ema, compute_atr, compute_vwap, compute_macd,
    get_session, classify_volatility, compute_confluence, MIN_CONFLUENCE,
    BANKROLL, STOP_LOSS_PCT, TAKE_PROFIT_PRICE, TRAILING_STOP_PCT,
    TIER_CONFIG, DAILY_LOSS_LIMIT, DAILY_LOSS_PCT,
)
from pm_engine_v18_8 import (
    compute_rsi, detect_btc_direction, generate_signal_v188, get_regime,
    MIN_CONFIDENCE, MIN_BET,
)

BANKROLL_BT = 400.0
STOP_LOSS_PCT = 0.50
TAKE_PROFIT_PRICE = 0.90
TRAILING_STOP_PCT = 0.40
TRAILING_ACTIVATE_MINS = 2.0
MAX_OPEN = 3
MIN_CONFIDENCE_FAIR_PRICE = 0.70


def fetch_binance_candles(interval="5m", limit=1500):
    import urllib.request
    url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V19/1.0'})
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read())
    candles = []
    for k in data:
        candles.append({
            'open_time': k[0], 'open': float(k[1]), 'high': float(k[2]),
            'low': float(k[3]), 'close': float(k[4]), 'volume': float(k[5]),
            'close_time': k[6],
        })
    return candles


def run_backtest(candles, bankroll=BANKROLL_BT):
    LOOKBACK = 100
    results = []
    bd = bankroll
    peak = bankroll
    max_dd = 0
    daily_losses = 0
    daily_loss_amt = 0
    current_day = None

    for i in range(LOOKBACK, len(candles) - 1):
        window = candles[:i+1]
        prices = [c['close'] for c in window]

        # Daily reset
        candle_day = datetime.fromtimestamp(candles[i]['open_time'] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if current_day != candle_day:
            current_day = candle_day
            daily_losses = 0
            daily_loss_amt = 0

        # Daily loss limit
        if daily_losses >= DAILY_LOSS_LIMIT or daily_loss_amt >= bankroll * DAILY_LOSS_PCT:
            continue

        # Indicators
        rsi_arr = compute_rsi(prices)
        rsi = rsi_arr[-1]
        direction, strength = detect_btc_direction(window, len(window) - 1)
        regime = get_regime(prices)
        ema21 = compute_ema(prices, 21)
        ema50 = compute_ema(prices, 50)
        vwap = compute_vwap(window[-20:] if len(window) >= 20 else window)
        atr = compute_atr(window, 14)
        _, _, macd_hist = compute_macd(prices)
        session = get_session(datetime.fromtimestamp(candles[i]['open_time'] / 1000, tz=timezone.utc).hour)
        vol_regime, vol_max_price = classify_volatility(atr, prices[-1])

        # Signal
        signal = generate_signal_v188(prices, window, len(window) - 1)
        if signal['direction'] == 'neutral':
            continue

        sig_dir = signal['direction'].upper()
        sig_conf = signal['confidence']
        sig_strategy = signal['strategy']

        # V19: Confluence check
        confluence, details = compute_confluence(
            rsi, direction, regime, ema21, ema50, vwap, prices[-1],
            macd_hist, session, (vol_regime, vol_max_price), sig_dir
        )

        if confluence < MIN_CONFLUENCE:
            continue

        # Tier
        tier_cfg = TIER_CONFIG.get(sig_strategy, {"size": 0.03, "max_price": 0.08})
        tier_size = tier_cfg["size"]
        tier_max_price = min(tier_cfg["max_price"], vol_max_price)

        # Vol-adaptive sizing
        if vol_regime == "low_vol" and confluence >= 8:
            tier_size *= 1.3
        elif vol_regime == "high_vol":
            tier_size *= 0.7

        # Entry type
        close_now = candles[i]['close']
        open_now = candles[i]['open']
        prev_close = candles[max(0, i-5)]['close'] if i > 0 else open_now
        price_move = (close_now - prev_close) / prev_close if prev_close > 0 else 0

        entry_type = None
        entry_price = None

        if abs(price_move) < 0.002:
            if sig_conf >= MIN_CONFIDENCE_FAIR_PRICE:
                entry_price = 0.48 + np.random.uniform(-0.03, 0.07)
                entry_type = "fair_price"
        elif (sig_dir == 'UP' and price_move < -0.003) or (sig_dir == 'DOWN' and price_move > 0.003):
            cheap_price = max(0.02, min(tier_max_price * 1.5, abs(price_move) * 8 + np.random.uniform(0.01, 0.04)))
            cheap_price = min(cheap_price, 0.15)
            if cheap_price <= tier_max_price * 1.5:
                entry_price = cheap_price + np.random.uniform(-0.01, 0.02)
                entry_price = max(0.02, min(0.15, entry_price))
                entry_type = "direct"

        if entry_type is None:
            continue

        bet = bd * tier_size
        bet = max(0.25, min(bet, bd * 0.08))
        if bd < 5 or bet > bd:
            continue

        went_up = close_now >= open_now
        won = (sig_dir == 'UP' and went_up) or (sig_dir == 'DOWN' and not went_up)

        # Simple exit simulation (5 bars = 5 min)
        peak_price = entry_price
        cur_price = entry_price
        exit_type = None
        exit_price = None
        duration = 0
        n_steps = 5

        for step in range(n_steps):
            duration += 1
            progress = (step + 1) / n_steps

            if won:
                cur_price = entry_price + (1.0 - entry_price) * progress * np.random.uniform(0.7, 1.0)
            else:
                cur_price = entry_price * (1 - progress * np.random.uniform(0.5, 0.9))
            cur_price = max(0.01, min(0.99, cur_price))
            peak_price = max(peak_price, cur_price)

            # Stop-loss
            if (entry_price - cur_price) / entry_price >= STOP_LOSS_PCT:
                exit_type = "stop_loss"
                exit_price = cur_price
                break
            # Take-profit
            if cur_price >= TAKE_PROFIT_PRICE:
                exit_type = "take_profit"
                exit_price = cur_price
                break
            # Trailing stop
            if duration >= 2 and peak_price > entry_price:
                if (peak_price - cur_price) / peak_price >= TRAILING_STOP_PCT:
                    exit_type = "trailing_stop"
                    exit_price = cur_price
                    break

        if exit_type is None:
            if won:
                exit_type = "expiry_win"
                exit_price = 1.0
            else:
                exit_type = "expiry_loss"
                exit_price = 0.0

        # PnL
        if exit_price > 0 and exit_type != "expiry_loss":
            pnl = bet * ((exit_price / entry_price) - 1)
        elif exit_type == "expiry_loss":
            pnl = -bet
        else:
            pnl = -bet

        bd += pnl
        peak = max(peak, bd)
        dd = (peak - bd) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

        if pnl < 0:
            daily_losses += 1
            daily_loss_amt += abs(pnl)

        results.append({
            'idx': i, 'direction': sig_dir, 'strategy': sig_strategy,
            'tier': 1 if tier_size >= 0.10 else (2 if tier_size >= 0.05 else 3),
            'entry_type': entry_type, 'entry_price': entry_price,
            'exit_type': exit_type, 'exit_price': exit_price,
            'won': won, 'bet': round(bet, 2), 'pnl': round(pnl, 2),
            'bankroll': round(bd, 2), 'rsi': round(rsi, 1),
            'confluence': round(confluence, 1),
            'session': session[0], 'vol_regime': vol_regime,
            'regime': regime, 'confidence': round(sig_conf, 3),
        })

    return results, bd, max_dd


def main():
    print("=" * 70)
    print("V19 BACKTEST — Krajekis-Enhanced with Confluence")
    print("=" * 70)

    print("\n1. Fetching 5m candles from Binance...")
    candles = fetch_binance_candles("5m", 1500)
    print(f"   Got {len(candles)} candles")

    print("\n2. Running V19 backtest...")
    results, final_bankroll, max_dd = run_backtest(candles)

    if not results:
        print("No trades generated.")
        return

    total = len(results)
    wins = [r for r in results if r['won']]
    losses = [r for r in results if not r['won']]
    total_pnl = sum(r['pnl'] for r in results)

    direct = [r for r in results if r['entry_type'] == 'direct']
    fair = [r for r in results if r['entry_type'] == 'fair_price']

    exit_types = {}
    for r in results:
        et = r['exit_type']
        exit_types.setdefault(et, {'count': 0, 'pnl': 0, 'wins': 0})
        exit_types[et]['count'] += 1
        exit_types[et]['pnl'] += r['pnl']
        if r['won']: exit_types[et]['wins'] += 1

    session_types = {}
    for r in results:
        s = r['session']
        session_types.setdefault(s, {'count': 0, 'pnl': 0, 'wins': 0})
        session_types[s]['count'] += 1
        session_types[s]['pnl'] += r['pnl']
        if r['won']: session_types[s]['wins'] += 1

    vol_types = {}
    for r in results:
        v = r['vol_regime']
        vol_types.setdefault(v, {'count': 0, 'pnl': 0, 'wins': 0})
        vol_types[v]['count'] += 1
        vol_types[v]['pnl'] += r['pnl']
        if r['won']: vol_types[v]['wins'] += 1

    conf_high = [r for r in results if r['confluence'] >= 8]
    conf_low = [r for r in results if r['confluence'] < 7]

    print(f"\n{'='*70}")
    print(f"V19 BACKTEST RESULTS")
    print(f"{'='*70}")
    print(f"  Candles: {len(candles)} | Total trades: {total}")
    print(f"  Win rate: {len(wins)}/{total} = {len(wins)/total*100:.1f}%")
    print(f"  Total PnL: ${total_pnl:+.2f} | Avg: ${total_pnl/total:+.2f}/trade")
    print(f"  Final bankroll: ${final_bankroll:.2f} (started ${BANKROLL_BT})")
    print(f"  Max drawdown: {max_dd*100:.1f}%")
    print(f"  Return: {(final_bankroll/BANKROLL_BT - 1)*100:+.1f}%")

    print(f"\n  BY ENTRY TYPE:")
    print(f"    Direct: {len(direct)} trades, {len([r for r in direct if r['won']])/max(1,len(direct))*100:.1f}% WR, ${sum(r['pnl'] for r in direct):+.2f}")
    print(f"    Fair-price: {len(fair)} trades, {len([r for r in fair if r['won']])/max(1,len(fair))*100:.1f}% WR, ${sum(r['pnl'] for r in fair):+.2f}")

    print(f"\n  BY EXIT TYPE:")
    for et, s in sorted(exit_types.items()):
        wr = s['wins']/s['count']*100 if s['count'] > 0 else 0
        print(f"    {et}: {s['count']} trades, {wr:.0f}% WR, ${s['pnl']:+.2f}")

    print(f"\n  BY SESSION (Krajekis):")
    for s, d in sorted(session_types.items(), key=lambda x: x[1]['pnl'], reverse=True):
        wr = d['wins']/d['count']*100 if d['count'] > 0 else 0
        print(f"    {s}: {d['count']} trades, {wr:.0f}% WR, ${d['pnl']:+.2f}")

    print(f"\n  BY VOLATILITY:")
    for v, d in sorted(vol_types.items(), key=lambda x: x[1]['pnl'], reverse=True):
        wr = d['wins']/d['count']*100 if d['count'] > 0 else 0
        print(f"    {v}: {d['count']} trades, {wr:.0f}% WR, ${d['pnl']:+.2f}")

    print(f"\n  BY CONFLUENCE:")
    ch_wr = len([r for r in conf_high if r['won']])/max(1,len(conf_high))*100
    cl_wr = len([r for r in conf_low if r['won']])/max(1,len(conf_low))*100
    print(f"    High conf (≥8): {len(conf_high)} trades, {ch_wr:.1f}% WR, ${sum(r['pnl'] for r in conf_high):+.2f}")
    print(f"    Low conf (<7): {len(conf_low)} trades, {cl_wr:.1f}% WR, ${sum(r['pnl'] for r in conf_low):+.2f}")

    out = REPO / "output" / "backtest_v19.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "version": "v19", "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_trades": total, "win_rate": len(wins)/total*100,
        "total_pnl": total_pnl, "final_bankroll": final_bankroll,
        "max_drawdown": max_dd*100, "return_pct": (final_bankroll/BANKROLL_BT-1)*100,
        "trades": results,
    }, indent=2, default=str))
    print(f"\n  Results saved to {out}")


if __name__ == "__main__":
    main()