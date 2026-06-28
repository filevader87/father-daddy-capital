#!/usr/bin/env python3
"""
V18.9 Binance Backtest — 5m/15m BTC Up/Down with Exit Strategies
=================================================================
Backtests V18.8 signals on historical Binance 5m candles,
simulating Polymarket 5m/15m Up/Down market entries with full exit logic.

Entry types:
- Direct (cheap ≤8¢): Buy when signal aligns with cheap side
- Fair-price (35-65¢): Directional bet at fair odds, requires CONF ≥ 0.70

Exit strategies:
- Stop-loss: Token drops 50% from entry
- Take-profit: Token reaches 90¢
- Trailing stop: 40% drop from peak after 2 min
- Time-decay: <1 min left, losing, price >3¢
- Expiry: Binary resolution at end of window
"""

import json, os, sys, time, math
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
from pm_engine_v18_8 import (
    compute_rsi, detect_btc_direction, generate_signal_v188, get_regime,
    MIN_CONFIDENCE, RSI_OVERSOLD_SEVERE, RSI_OVERBOUGHT_SEVERE,
    WIN_PROB_BASE, CONFIDENCE_MAP, TIER_SIZE, TIER_MAX_PRICE,
)

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
BANKROLL = 400.0
STOP_LOSS_PCT = 0.50
TAKE_PROFIT_PRICE = 0.90
TRAILING_STOP_PCT = 0.40
TRAILING_ACTIVATE_MINS = 2.0
TIME_DECAY_SELL_MINS = 1.0
TIME_DECAY_MIN_PRICE = 0.03
MAX_OPEN = 3
POSITION_SIZE_PCT = 0.03
MIN_CONFIDENCE_FAIR = 0.70
TIER_CONFIG = {
    "severe_oversold":    {"size": 0.10, "max_price": 0.30},
    "severe_overbought":  {"size": 0.10, "max_price": 0.30},
    "oversold_down":      {"size": 0.06, "max_price": 0.15},
    "overbought_up":      {"size": 0.06, "max_price": 0.15},
    "direction_down_cheap": {"size": 0.03, "max_price": 0.08},
    "direction_up_cheap":   {"size": 0.03, "max_price": 0.08},
}

# Fetch Binance data
def fetch_binance_candles(interval="5m", limit=1500):
    """Fetch BTC/USDT candles from Binance."""
    import urllib.request
    url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={'User-Agent': 'FDC-Backtest/1.0'})
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read())
    candles = []
    for k in data:
        candles.append({
            'open_time': k[0],
            'open': float(k[1]),
            'high': float(k[2]),
            'low': float(k[3]),
            'close': float(k[4]),
            'volume': float(k[5]),
            'close_time': k[6],
        })
    return candles


def simulate_5m_window(signal_dir, entry_price, window_candles, entry_type, tier_size):
    """
    Simulate a 5m Up/Down market trade with exit strategies.
    
    signal_dir: 'UP' or 'DOWN'
    entry_price: token price at entry (0.01-0.99)
    window_candles: list of 1m candles within the 5m window (or we approximate from 5m)
    entry_type: 'direct' or 'fair_price'
    tier_size: fraction of bankroll
    
    Returns: (exit_type, exit_price, pnl_pct, duration_mins)
    """
    # For 5m markets, we simulate minute-by-minute price movement
    # within the 5m window using the 5m candle's range
    
    # The token price tracks probability of winning
    # If signal_dir = 'UP' and BTC goes up, token → $1
    # If signal_dir = 'UP' and BTC goes down, token → $0
    
    # We approximate intrabar movement by dividing the 5m candle into 5 sub-periods
    # Each sub-period's price interpolates from open to close through the high/low
    
    close = window_candles[-1]['close'] if isinstance(window_candles, list) else window_candles['close']
    open_price = window_candles[0]['open'] if isinstance(window_candles, list) else window_candles['open']
    high = max(c['high'] for c in window_candles) if isinstance(window_candles, list) else window_candles['high']
    low = min(c['low'] for c in window_candles) if isinstance(window_candles, list) else window_candles['low']
    
    # Did we win? (direction matches price movement)
    price_went_up = close >= open_price
    won = (signal_dir == 'UP' and price_went_up) or (signal_dir == 'DOWN' and not price_went_up)
    
    # Simulate token price at each minute
    # Token price = probability of winning, updates as price moves
    n_steps = 5  # 5 one-minute intervals
    peak_price = entry_price
    duration = 0
    
    for step in range(n_steps):
        duration += 1  # 1 minute per step
        
        # Progress through the window (0.0 to 1.0)
        progress = (step + 1) / n_steps
        
        # Approximate BTC price at this point
        # Interpolate from open through high/low to close
        if progress < 0.5:
            # First half: open → high or low (depending on direction)
            if price_went_up:
                btc_at_step = open_price + (high - open_price) * (progress / 0.5) * 0.6
            else:
                btc_at_step = open_price + (low - open_price) * (progress / 0.5) * 0.6
        else:
            # Second half: high/low → close
            remaining = (progress - 0.5) / 0.5
            if price_went_up:
                mid = high * 0.6 + open_price * 0.4
                btc_at_step = mid + (close - mid) * remaining
            else:
                mid = low * 0.6 + open_price * 0.4
                btc_at_step = mid + (close - mid) * remaining
        
        # Token price at this step
        # If winning: price → 1.0
        # If losing: price → 0.0
        # Interpolate based on how far we are and the price move
        price_move_pct = (btc_at_step - open_price) / open_price if open_price > 0 else 0
        
        if signal_dir == 'UP':
            # Up token: price increases as BTC goes up
            cur_price = entry_price + (1.0 - entry_price) * max(0, min(1, progress * 2 * max(0, price_move_pct * 1000)))
            cur_price = max(0.01, min(0.99, cur_price))
            if won:
                cur_price = entry_price + (1.0 - entry_price) * progress
            else:
                cur_price = entry_price * (1.0 - progress * 0.8)
        else:
            # Down token: price increases as BTC goes down
            cur_price = entry_price + (1.0 - entry_price) * max(0, min(1, progress * 2 * max(0, -price_move_pct * 1000)))
            cur_price = max(0.01, min(0.99, cur_price))
            if won:
                cur_price = entry_price + (1.0 - entry_price) * progress
            else:
                cur_price = entry_price * (1.0 - progress * 0.8)
        
        cur_price = max(0.01, min(0.99, cur_price))
        peak_price = max(peak_price, cur_price)
        
        # ── Exit checks ──
        
        # Stop-loss: token drops 50% from entry
        price_drop = (entry_price - cur_price) / entry_price if entry_price > 0 else 0
        if price_drop >= STOP_LOSS_PCT:
            exit_value = cur_price / entry_price  # fraction of bet recovered
            return "stop_loss", cur_price, exit_value - 1.0, duration
        
        # Take-profit: token reaches 90¢
        if cur_price >= TAKE_PROFIT_PRICE:
            exit_value = cur_price / entry_price
            return "take_profit", cur_price, exit_value - 1.0, duration
        
        # Trailing stop: after 2 minutes, if price dropped 40% from peak
        if duration >= TRAILING_ACTIVATE_MINS and peak_price > entry_price:
            drop_from_peak = (peak_price - cur_price) / peak_price if peak_price > 0 else 0
            if drop_from_peak >= TRAILING_STOP_PCT:
                exit_value = cur_price / entry_price
                return "trailing_stop", cur_price, exit_value - 1.0, duration
    
    # ── Time-decay: losing with <1 min left and price > 3¢ ──
    remaining_mins = n_steps - duration
    if remaining_mins < TIME_DECAY_SELL_MINS and not won and cur_price > TIME_DECAY_MIN_PRICE:
        exit_value = cur_price / entry_price
        return "time_decay", cur_price, exit_value - 1.0, duration
    
    # ── Expiry: binary resolution ──
    if won:
        payout = 1.0 / entry_price  # bet * (1/entry_price)
        return "expiry_win", 1.0, payout - 1.0, duration
    else:
        return "expiry_loss", 0.0, -1.0, duration  # lose entire bet


def run_backtest(candles, window_mins=5):
    """Run V18.9 backtest on Binance 5m candles."""
    
    results = []
    bankroll = BANKROLL
    peak_bankroll = BANKROLL
    max_drawdown = 0
    
    # We need at least 100 candles for RSI
    LOOKBACK = 100
    
    for i in range(LOOKBACK, len(candles) - 1):
        window_candles = candles[:i+1]
        prices = [c['close'] for c in window_candles]
        
        # Skip if we don't have enough data
        if len(prices) < LOOKBACK:
            continue
        
        # Generate signal
        rsi_arr = compute_rsi(prices)
        current_rsi = rsi_arr[-1]
        direction, strength = detect_btc_direction(window_candles, len(window_candles) - 1)
        regime = get_regime(prices)
        
        signal = generate_signal_v188(prices, window_candles, len(window_candles) - 1)
        
        if signal['direction'] == 'neutral':
            continue
        
        sig_dir = signal['direction'].upper()
        sig_conf = signal['confidence']
        sig_strategy = signal['strategy']
        
        tier_cfg = TIER_CONFIG.get(sig_strategy, {"size": 0.03, "max_price": 0.08})
        tier_size = tier_cfg["size"]
        tier_max_price = tier_cfg["max_price"]
        tier_num = 1 if tier_size >= 0.10 else (2 if tier_size >= 0.05 else 3)
        
        # Determine what price we'd enter at
        # For 5m markets, simulate token prices based on candle
        close_now = candles[i]['close']
        open_now = candles[i]['open']
        went_up = close_now >= open_now
        
        # Simulate entry price based on where in the window we enter
        # For cheap entries: 3-8¢, for fair-price: 45-55¢
        
        # Determine if cheap side is available
        # In a 5m market, if BTC is going UP, Up token is expensive, Down is cheap
        # If BTC is going DOWN, Down is expensive, Up is cheap
        
        # Entry models:
        # 1. Direct cheap: signal aligns with cheap token (e.g., UP signal + Up ≤8¢)
        # 2. Fair-price: both sides near 50¢, directional bet
        
        entry_type = None
        entry_price = None
        
        # Check direction alignment with cheap side
        # When BTC hasn't moved much in the current window, both sides are ~50¢
        # When BTC is trending down, Up becomes cheap and Down expensive
        price_move = (close_now - candles[max(0,i-5)]['close']) / candles[max(0,i-5)]['close'] if i > 0 else 0
        
        if abs(price_move) < 0.002:  # <0.2% move — both sides near 50¢
            # Fair-price entry if confidence high enough
            if sig_conf >= MIN_CONFIDENCE_FAIR:
                entry_price = 0.48 + np.random.uniform(-0.03, 0.07)  # 45-55¢
                entry_type = "fair_price"
        elif (sig_dir == 'UP' and price_move < -0.003) or (sig_dir == 'DOWN' and price_move > 0.003):
            # Signal aligns with cheap side
            # e.g., UP signal when BTC is dropping → Up is cheap
            cheap_price = max(0.02, min(tier_max_price * 1.5, 0.03 + abs(price_move) * 5))
            cheap_price = min(cheap_price, 0.15)  # Cap at 15¢
            if cheap_price <= tier_max_price * 1.5:
                entry_price = cheap_price + np.random.uniform(-0.01, 0.02)
                entry_price = max(0.02, min(0.15, entry_price))
                entry_type = "direct"
        elif (sig_dir == 'UP' and price_move > 0.003) or (sig_dir == 'DOWN' and price_move < -0.003):
            # Signal opposes cheap side (e.g., UP when BTC is already up → Up is expensive)
            # Skip — contrarian not supported
            pass
        
        if entry_type is None or entry_price is None:
            continue
        
        # Position sizing
        bet = bankroll * tier_size
        bet = max(0.25, min(bet, bankroll * 0.08))
        
        if bankroll < 5 or bet > bankroll:
            continue
        
        if len([r for r in results if r.get('open')]) >= MAX_OPEN:
            continue
        
        # Determine win/loss
        went_up = candles[i]['close'] >= candles[i]['open']
        won = (sig_dir == 'UP' and went_up) or (sig_dir == 'DOWN' and not went_up)
        
        # Simulate exit
        exit_type, exit_price, pnl_pct, duration = simulate_5m_window(
            sig_dir, entry_price, candles[i], entry_type, tier_size
        )
        
        # Calculate PnL
        pnl = bet * pnl_pct
        bankroll += pnl
        peak_bankroll = max(peak_bankroll, bankroll)
        drawdown = (peak_bankroll - bankroll) / peak_bankroll
        max_drawdown = max(max_drawdown, drawdown)
        
        results.append({
            'candle_idx': i,
            'timestamp': candles[i].get('open_time', i),
            'direction': sig_dir,
            'strategy': sig_strategy,
            'tier': tier_num,
            'entry_type': entry_type,
            'entry_price': entry_price,
            'exit_type': exit_type,
            'exit_price': exit_price,
            'won': won,
            'bet': round(bet, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct * 100, 2),
            'bankroll': round(bankroll, 2),
            'rsi': round(current_rsi, 1),
            'confidence': round(sig_conf, 3),
            'regime': regime,
            'duration_mins': duration,
        })
    
    return results, bankroll, max_drawdown


def main():
    print("=" * 70)
    print("V18.9 Binance Backtest — 5m Up/Down with Exit Strategies")
    print("=" * 70)
    
    # Fetch candles
    print(f"\n1. Fetching 5m candles from Binance...")
    candles = fetch_binance_candles("5m", 1500)
    print(f"   Got {len(candles)} candles")
    
    if len(candles) < 200:
        print("ERROR: Not enough candles")
        return
    
    # Also fetch 1m candles for intrabar simulation
    print(f"\n2. Running V18.9 backtest...")
    results, final_bankroll, max_dd = run_backtest(candles)
    
    if not results:
        print("No trades generated. Check signal thresholds.")
        return
    
    # Compute stats
    total_trades = len(results)
    wins = [r for r in results if r['won']]
    losses = [r for r in results if not r['won']]
    total_pnl = sum(r['pnl'] for r in results)
    avg_pnl = total_pnl / total_trades
    
    # By exit type
    exit_stats = {}
    for r in results:
        et = r['exit_type']
        if et not in exit_stats:
            exit_stats[et] = {'count': 0, 'pnl': 0, 'wins': 0}
        exit_stats[et]['count'] += 1
        exit_stats[et]['pnl'] += r['pnl']
        if r['won']:
            exit_stats[et]['wins'] += 1
    
    # By entry type
    direct = [r for r in results if r['entry_type'] == 'direct']
    fair_price = [r for r in results if r['entry_type'] == 'fair_price']
    
    direct_wr = len([r for r in direct if r['won']]) / len(direct) * 100 if direct else 0
    fair_wr = len([r for r in fair_price if r['won']]) / len(fair_price) * 100 if fair_price else 0
    
    # By tier
    tier_stats = {}
    for r in results:
        t = f"T{r['tier']}"
        if t not in tier_stats:
            tier_stats[t] = {'count': 0, 'pnl': 0, 'wins': 0}
        tier_stats[t]['count'] += 1
        tier_stats[t]['pnl'] += r['pnl']
        if r['won']:
            tier_stats[t]['wins'] += 1
    
    # By strategy
    strat_stats = {}
    for r in results:
        s = r['strategy']
        if s not in strat_stats:
            strat_stats[s] = {'count': 0, 'pnl': 0, 'wins': 0}
        strat_stats[s]['count'] += 1
        strat_stats[s]['pnl'] += r['pnl']
        if r['won']:
            strat_stats[s]['wins'] += 1
    
    # Print results
    print(f"\n{'='*70}")
    print(f"BACKTEST RESULTS — V18.9 with Exit Strategies")
    print(f"{'='*70}")
    print(f"  Candles: {len(candles)}")
    print(f"  Total trades: {total_trades}")
    print(f"  Win rate: {len(wins)}/{total_trades} = {len(wins)/total_trades*100:.1f}%")
    print(f"  Total PnL: ${total_pnl:+.2f}")
    print(f"  Avg PnL/trade: ${avg_pnl:+.2f}")
    print(f"  Final bankroll: ${final_bankroll:.2f} (started ${BANKROLL})")
    print(f"  Max drawdown: {max_dd*100:.1f}%")
    print(f"  Return: {(final_bankroll/BANKROLL - 1)*100:+.1f}%")
    
    print(f"\n  BY ENTRY TYPE:")
    print(f"    Direct (cheap ≤8¢): {len(direct)} trades, {direct_wr:.1f}% WR, ${sum(r['pnl'] for r in direct):+.2f} PnL")
    print(f"    Fair-price (45-55¢): {len(fair_price)} trades, {fair_wr:.1f}% WR, ${sum(r['pnl'] for r in fair_price):+.2f} PnL")
    
    print(f"\n  BY EXIT TYPE:")
    for et, stats in sorted(exit_stats.items()):
        wr = stats['wins'] / stats['count'] * 100 if stats['count'] > 0 else 0
        print(f"    {et}: {stats['count']} trades, {wr:.0f}% WR, ${stats['pnl']:+.2f} PnL")
    
    print(f"\n  BY TIER:")
    for t, stats in sorted(tier_stats.items()):
        wr = stats['wins'] / stats['count'] * 100 if stats['count'] > 0 else 0
        print(f"    {t}: {stats['count']} trades, {wr:.0f}% WR, ${stats['pnl']:+.2f} PnL")
    
    print(f"\n  BY STRATEGY:")
    for s, stats in sorted(strat_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
        wr = stats['wins'] / stats['count'] * 100 if stats['count'] > 0 else 0
        print(f"    {s}: {stats['count']} trades, {wr:.0f}% WR, ${stats['pnl']:+.2f} PnL")
    
    # Sample trades
    print(f"\n  LAST 10 TRADES:")
    for r in results[-10:]:
        print(f"    {r['direction']} T{r['tier']} {r['entry_type']} @ {r['entry_price']*100:.1f}¢ → {r['exit_type']} @ {r['exit_price']*100:.1f}¢ | PnL: ${r['pnl']:+.2f} | RSI: {r['rsi']:.1f}")
    
    # Save results
    output_file = REPO / "output" / "backtest_v189_5m.json"
    output_file.parent.mkdir(exist_ok=True)
    output_file.write_text(json.dumps({
        "version": "v189",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "bankroll": BANKROLL,
            "stop_loss_pct": STOP_LOSS_PCT,
            "take_profit_price": TAKE_PROFIT_PRICE,
            "trailing_stop_pct": TRAILING_STOP_PCT,
            "trailing_activate_mins": TRAILING_ACTIVATE_MINS,
            "window_mins": 5,
        },
        "summary": {
            "total_trades": total_trades,
            "win_rate": len(wins) / total_trades * 100,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "final_bankroll": final_bankroll,
            "max_drawdown": max_dd * 100,
            "return_pct": (final_bankroll / BANKROLL - 1) * 100,
            "direct_trades": len(direct),
            "direct_wr": direct_wr,
            "direct_pnl": sum(r['pnl'] for r in direct),
            "fair_trades": len(fair_price),
            "fair_wr": fair_wr,
            "fair_pnl": sum(r['pnl'] for r in fair_price),
        },
        "trades": results,
    }, indent=2, default=str))
    print(f"\n  Results saved to {output_file}")


if __name__ == "__main__":
    main()