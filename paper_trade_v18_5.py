#!/usr/bin/env python3
"""
V18.5 Paper Trading Engine

Records paper trades using real Binance + Polymarket data.
Detects RSI + direction signals, finds cheap tokens, tracks P&L.

Usage:
  python3 paper_trade_v18_5.py              # Single scan
  python3 paper_trade_v18_5.py --loop       # Continuous (every 60s)
  python3 paper_trade_v18_5.py --report     # Show P&L report
  python3 paper_trade_v18_5.py --reset       # Reset paper trades
"""

import json
import time
import traceback
import sys
import os
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import urllib.request

# ============================================================
# CONFIG
# ============================================================
BANKROLL_START = 100.0
BET_PCT = 0.10          # 10% of bankroll per trade
MAX_BET = 5.0           # max $5 per trade
CHEAP_THRESHOLD = 0.20  # max entry price for cheap token
SCAN_INTERVAL = 60      # seconds between scans in loop mode
MIN_CONFIDENCE = 0.80   # only trade 80%+ WR signals

DATA_DIR = Path(__file__).parent / 'paper_trades'
TRADES_FILE = DATA_DIR / 'trades.json'
STATE_FILE = DATA_DIR / 'state.json'
LOG_FILE = DATA_DIR / 'scanner.log'

# RSI thresholds (from Binance backtest validation)
RSI_OVERSOLD_SEVERE = 25
RSI_OVERSOLD = 30
RSI_OVERBOUGHT_SEVERE = 75
RSI_OVERBOUGHT = 70

# Direction thresholds
MIN_CHANGE_PCT = 0.03
LOOKBACK = 3

# ============================================================
# STATE MANAGEMENT
# ============================================================
def load_state():
    """Load paper trading state (bankroll, open positions)."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        'bankroll': BANKROLL_START,
        'total_trades': 0,
        'total_wins': 0,
        'open_positions': [],
        'closed_positions': [],
        'created_at': datetime.now(timezone.utc).isoformat(),
        'last_scan': None,
    }


def save_state(state):
    """Save paper trading state."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)


def log(msg):
    """Log to file and stdout."""
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    line = f"[{ts}] {msg}"
    print(line)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


# ============================================================
# MARKET DATA
# ============================================================
def fetch_btc_prices(interval='5m', limit=100):
    """Fetch BTC 5m candles from Binance."""
    try:
        url = f'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}'
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC/18.5'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        candles = []
        for c in data:
            candles.append({
                'ts': int(c[0]) / 1000,
                'open': float(c[1]),
                'high': float(c[2]),
                'low': float(c[3]),
                'close': float(c[4]),
                'volume': float(c[5]),
            })
        return candles
    except Exception as e:
        log(f"ERROR fetching Binance: {e}")
        return []


def compute_rsi(prices, period=14):
    """Compute RSI from price array."""
    if len(prices) < period + 2:
        return np.full(len(prices), 50.0)
    
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    
    n = len(deltas)
    avg_gains = np.zeros(n, dtype=float)
    avg_losses = np.zeros(n, dtype=float)
    
    avg_gains[period] = np.mean(gains[1:period+1])
    avg_losses[period] = np.mean(losses[1:period+1])
    
    for i in range(period+1, n):
        avg_gains[i] = (avg_gains[i-1] * (period-1) + gains[i]) / period
        avg_losses[i] = (avg_losses[i-1] * (period-1) + losses[i]) / period
    
    rsi = np.full(len(prices), 50.0)
    for i in range(period+1, len(prices)):
        idx = min(i, n-1)
        if avg_losses[idx] == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gains[idx] / avg_losses[idx]
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    
    return rsi


def detect_direction(candles, idx, lookback=3, min_change=0.03):
    """Detect BTC direction from recent candles."""
    if idx < lookback:
        return 'FLAT', 0.0
    current = candles[idx]['close']
    prev = candles[idx - lookback]['close']
    change_pct = (current - prev) / prev * 100
    if change_pct > min_change:
        return 'UP', abs(change_pct)
    elif change_pct < -min_change:
        return 'DOWN', abs(change_pct)
    return 'FLAT', abs(change_pct)


def fetch_btc_updown_markets(duration='5m'):
    """Fetch active BTC Up/Down markets from Gamma API."""
    cid_map = {}
    for offset in range(0, 2000, 100):
        url = f'https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false&offset={offset}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC/18.5', 'Accept': 'application/json'})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            if not data:
                break
        except Exception:
            break
        
        for m in data:
            q = m.get('question', '').lower()
            if 'bitcoin' not in q and 'btc' not in q:
                continue
            if 'up' not in q or 'down' not in q:
                continue
            
            cid = m.get('conditionId', '')
            if not cid or len(cid) < 10:
                continue
            cid_norm = cid.lower() if cid.startswith('0x') else '0x' + cid.lower()
            
            outcomes_raw = m.get('outcomes', '[]')
            if isinstance(outcomes_raw, str):
                try:
                    outcomes = json.loads(outcomes_raw)
                except json.JSONDecodeError:
                    continue
            else:
                outcomes = outcomes_raw
            
            clob_ids_raw = m.get('clobTokenIds', '[]')
            if isinstance(clob_ids_raw, str):
                try:
                    clob_ids = json.loads(clob_ids_raw)
                except json.JSONDecodeError:
                    continue
            elif isinstance(clob_ids_raw, list):
                clob_ids = [str(x) for x in clob_ids_raw]
            else:
                continue
            
            if outcomes != ['Up', 'Down'] or len(clob_ids) < 2:
                continue
            
            slug = m.get('slug', '')
            dur = 'unknown'
            if '5m' in slug:
                dur = '5m'
            elif '15m' in slug:
                dur = '15m'
            elif '1h' in slug:
                dur = '1h'
            
            if duration and dur != duration:
                continue
            
            cid_map[cid_norm] = {
                'up_aid': str(clob_ids[0]),
                'down_aid': str(clob_ids[1]),
                'question': m.get('question', ''),
                'slug': slug,
                'duration': dur,
                'end_date': m.get('endDateIso', ''),
            }
    
    return cid_map


def fetch_clob_price(token_id):
    """Fetch CLOB price for a token."""
    try:
        url = f'https://clob.polymarket.com/price?token_id={token_id}&side=buy'
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC/18.5', 'Accept': 'application/json'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return float(data.get('price', 0))
    except Exception:
        return None


# ============================================================
# PAPER TRADING LOGIC
# ============================================================
def generate_signal(rsi_value, direction, strength):
    """Generate trade signal based on RSI + direction combination."""
    signals = []
    
    # SEVERE strategies (80%+ WR, validated)
    if rsi_value < RSI_OVERSOLD_SEVERE and direction == 'DOWN':
        signals.append(('BUY_DOWN', 0.806, 'severe_oversold_down'))
    if rsi_value > RSI_OVERBOUGHT_SEVERE and direction == 'UP':
        signals.append(('BUY_UP', 0.871, 'severe_overbought_up'))
    
    # MODERATE strategies (70%+ WR)
    if RSI_OVERSOLD <= rsi_value < RSI_OVERSOLD_SEVERE and direction == 'DOWN':
        signals.append(('BUY_DOWN', 0.739, 'oversold_down'))
    if RSI_OVERBOUGHT <= rsi_value < RSI_OVERBOUGHT_SEVERE and direction == 'UP':
        signals.append(('BUY_UP', 0.691, 'overbought_up'))
    
    if not signals:
        return None
    return max(signals, key=lambda x: x[1])


def open_paper_trade(state, signal_type, confidence, strategy, btc_price, rsi_value, 
                      direction, strength, token_price, market_info):
    """Open a paper trade position."""
    bet = min(state['bankroll'] * BET_PCT, MAX_BET)
    if bet < 1.0:
        log(f"Bankroll too low for trade: ${state['bankroll']:.2f}")
        return state
    
    shares = bet / token_price
    
    trade = {
        'id': state['total_trades'] + 1,
        'opened_at': datetime.now(timezone.utc).isoformat(),
        'signal_type': signal_type,
        'strategy': strategy,
        'confidence': confidence,
        'btc_price_entry': btc_price,
        'rsi_entry': rsi_value,
        'direction': direction,
        'direction_strength': strength,
        'token_price_entry': token_price,
        'token_side': 'UP' if signal_type == 'BUY_UP' else 'DOWN',
        'bet_amount': round(bet, 2),
        'shares': round(shares, 4),
        'market_question': market_info.get('question', ''),
        'market_slug': market_info.get('slug', ''),
        'market_end': market_info.get('end_date', ''),
        'status': 'OPEN',
    }
    
    state['bankroll'] -= bet
    state['total_trades'] += 1
    state['open_positions'].append(trade)
    
    log(f"📝 OPEN #{trade['id']} {signal_type} @ ${token_price:.4f} | "
        f"RSI={rsi_value:.1f} {direction} | bet=${bet:.2f} shares={shares:.1f} | "
        f"{strategy} ({confidence:.0%}) | BTC=${btc_price:,.0f}")
    
    return state


def close_paper_trade(state, position_idx, won, btc_price_exit=None):
    """Close a paper trade position."""
    pos = state['open_positions'][position_idx]
    
    if won:
        payout = pos['shares'] * (1.0 - pos['token_price_entry'])
        state['bankroll'] += pos['shares'] * 1.0  # settle at $1
        state['total_wins'] += 1
        result = 'WIN'
    else:
        # settle at $0, already deducted from bankroll
        payout = -pos['bet_amount']
        result = 'LOSE'
    
    pos['closed_at'] = datetime.now(timezone.utc).isoformat()
    pos['status'] = result
    pos['won'] = won
    pos['pnl'] = round(payout, 2) if won else round(-pos['bet_amount'], 2)
    pos['btc_price_exit'] = btc_price_exit
    
    state['closed_positions'].append(pos)
    state['open_positions'].pop(position_idx)
    
    log(f"{'✅' if won else '❌'} CLOSE #{pos['id']} {result} | "
        f"PnL=${pos['pnl']:.2f} | bankroll=${state['bankroll']:.2f} | "
        f"WR={state['total_wins']}/{state['total_trades']} "
        f"({state['total_wins']/max(state['total_wins']+len(state['open_positions'])-(state['total_trades']-state['total_wins']),1)*100:.1f}%)")
    
    return state


def check_open_positions(state, candles):
    """Check if any open positions should be closed (5-min binary expiry)."""
    if not state['open_positions']:
        return state
    
    current_price = candles[-1]['close']
    
    for i in range(len(state['open_positions']) - 1, -1, -1):
        pos = state['open_positions'][i]
        
        # Check if the position has been open for >= 5 minutes
        opened = datetime.fromisoformat(pos['opened_at'].replace('Z', '+00:00'))
        elapsed = (datetime.now(timezone.utc) - opened).total_seconds()
        
        if elapsed < 300:  # Less than 5 minutes
            continue
        
        # Determine outcome based on BTC price movement since entry
        entry_price = pos['btc_price_entry']
        change_pct = (current_price - entry_price) / entry_price * 100
        
        # For 5-min binary: did BTC go UP or DOWN from our entry?
        if pos['signal_type'] == 'BUY_DOWN':
            # We bet DOWN. Win if BTC went DOWN.
            won = current_price < entry_price
        elif pos['signal_type'] == 'BUY_UP':
            # We bet UP. Win if BTC went UP.
            won = current_price > entry_price
        else:
            won = False
        
        state = close_paper_trade(state, i, won, current_price)
    
    return state


# ============================================================
# MAIN LOOP
# ============================================================
def run_scan(state):
    """Run a single scan cycle."""
    # 1. Check open positions first
    candles = fetch_btc_prices('5m', 100)
    if not candles:
        log("No candles data, skipping")
        return state
    
    closes = np.array([c['close'] for c in candles])
    current_price = closes[-1]
    rsi = compute_rsi(closes)
    current_rsi = rsi[-1]
    
    # Close expired positions
    state = check_open_positions(state, candles)
    
    # 2. Get direction
    direction, strength = detect_direction(candles, len(candles)-1, LOOKBACK, MIN_CHANGE_PCT)
    
    # RSI zone label
    if current_rsi < 25:
        rsi_zone = "SEVERE OVERSOLD"
    elif current_rsi < 30:
        rsi_zone = "OVERSOLD"
    elif current_rsi < 35:
        rsi_zone = "NEAR OVERSOLD"
    elif current_rsi > 75:
        rsi_zone = "SEVERE OVERBOUGHT"
    elif current_rsi > 70:
        rsi_zone = "OVERBOUGHT"
    elif current_rsi > 65:
        rsi_zone = "NEAR OVERBOUGHT"
    else:
        rsi_zone = "NEUTRAL"
    
    # 3. Generate signal
    signal = generate_signal(current_rsi, direction, strength)
    
    # 4. If signal and confidence meets threshold, try to open trade
    if signal and signal[1] >= MIN_CONFIDENCE and not state['open_positions']:
        signal_type, confidence, strategy = signal
        
        # Fetch Polymarket markets
        cid_map = fetch_btc_updown_markets(duration='5m')
        
        if cid_map:
            # Try to find a cheap token
            for cid, info in sorted(cid_map.items(), key=lambda x: x[1].get('question', '')):
                if signal_type == 'BUY_UP':
                    token_id = info['up_aid']
                    token_label = 'UP'
                else:
                    token_id = info['down_aid']
                    token_label = 'DOWN'
                
                price = fetch_clob_price(token_id)
                if price and 0.01 <= price <= CHEAP_THRESHOLD:
                    log(f"🎯 SIGNAL: {signal_type} ({strategy}) {confidence:.0%} | "
                        f"BTC=${current_price:,.0f} RSI={current_rsi:.1f} {direction} | "
                        f"{token_label} token @ ${price:.4f}")
                    
                    state = open_paper_trade(
                        state, signal_type, confidence, strategy,
                        current_price, current_rsi, direction, strength,
                        price, info
                    )
                    break
                elif price and price > CHEAP_THRESHOLD:
                    log(f"⏭️ Token {token_label} @ ${price:.4f} (too expensive, max ${CHEAP_THRESHOLD})")
        else:
            log(f"🎯 SIGNAL: {signal_type} ({strategy}) {confidence:.0%} | "
                f"BTC=${current_price:,.0f} RSI={current_rsi:.1f} {direction} | "
                f"No 5m markets available")
    
    elif signal and signal[1] >= MIN_CONFIDENCE and state['open_positions']:
        log(f"⏳ Signal active ({signal[2]} {signal[1]:.0%}) but position already open, waiting")
    elif signal:
        log(f"⚠️ Signal {signal[2]} @ {signal[1]:.0%} below MIN_CONFIDENCE {MIN_CONFIDENCE:.0%}")
    else:
        log(f"📊 BTC=${current_price:,.0f} RSI={current_rsi:.1f} ({rsi_zone}) {direction} | No signal")
    
    # 5. Save state
    state['last_scan'] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    
    # 6. Print summary
    wr = state['total_wins'] / max(state['total_trades'] - len(state['open_positions']), 1) * 100
    open_count = len(state['open_positions'])
    log(f"💰 Bankroll: ${state['bankroll']:.2f} | WR: {state['total_wins']}/{state['total_trades'] - open_count} ({wr:.1f}%) | Open: {open_count}")
    
    return state


def print_report(state):
    """Print P&L report."""
    print("=" * 70)
    print("V18.5 PAPER TRADING REPORT")
    print("=" * 70)
    
    closed = state.get('closed_positions', [])
    open_pos = state.get('open_positions', [])
    total_closed = len(closed)
    total_wins = state.get('total_wins', 0)
    total_trades = state.get('total_trades', 0)
    bankroll = state.get('bankroll', BANKROLL_START)
    
    # Overall stats
    wr = total_wins / max(total_closed, 1) * 100
    pnl = bankroll - BANKROLL_START
    roi = pnl / BANKROLL_START * 100
    
    print(f"\n📊 Overall Performance:")
    print(f"  Starting Bankroll: ${BANKROLL_START:.2f}")
    print(f"  Current Bankroll: ${bankroll:.2f}")
    print(f"  P&L: ${pnl:+.2f} ({roi:+.1f}%)")
    print(f"  Closed Trades: {total_closed}")
    print(f"  Open Positions: {len(open_pos)}")
    print(f"  Win Rate: {total_wins}/{total_closed} ({wr:.1f}%)")
    
    if not closed:
        print("\n  No closed trades yet.")
        return
    
    # By strategy
    print(f"\n📈 By Strategy:")
    strategies = {}
    for t in closed:
        s = t.get('strategy', 'unknown')
        if s not in strategies:
            strategies[s] = {'wins': 0, 'total': 0, 'pnl': 0}
        strategies[s]['total'] += 1
        if t.get('won', False):
            strategies[s]['wins'] += 1
        strategies[s]['pnl'] += t.get('pnl', 0)
    
    print(f"  {'Strategy':<30} {'Trades':>8} {'WR%':>6} {'PnL':>10}")
    print("  " + "-" * 60)
    for s, data in sorted(strategies.items(), key=lambda x: -x[1]['pnl']):
        wr_s = data['wins'] / max(data['total'], 1) * 100
        print(f"  {s:<30} {data['total']:>8} {wr_s:>6.1f} ${data['pnl']:>10.2f}")
    
    # Recent trades
    print(f"\n📋 Recent Trades (last 10):")
    print(f"  {'#':>3} {'Signal':<12} {'Strategy':<25} {'Entry':>8} {'PnL':>8} {'Status':>6}")
    print("  " + "-" * 65)
    for t in closed[-10:]:
        print(f"  {t['id']:>3} {t['signal_type']:<12} {t['strategy']:<25} "
              f"${t['token_price_entry']:.4f} {t['pnl']:>+8.2f} {t['status']:>6}")
    
    # Open positions
    if open_pos:
        print(f"\n⏳ Open Positions:")
        for t in open_pos:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(t['opened_at'].replace('Z', '+00:00'))).total_seconds()
            print(f"  #{t['id']} {t['signal_type']} @ ${t['token_price_entry']:.4f} | "
                  f"RSI={t['rsi_entry']:.1f} | {t['strategy']} | "
                  f"elapsed: {elapsed/60:.1f}min")


def reset_paper_trades():
    """Reset all paper trading data."""
    if DATA_DIR.exists():
        for f in DATA_DIR.glob('*'):
            f.unlink()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("Paper trades reset. Starting fresh.")


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='V18.5 Paper Trading Engine')
    p.add_argument('--loop', action='store_true', help='Continuous scanning (every 60s)')
    p.add_argument('--report', action='store_true', help='Show P&L report')
    p.add_argument('--reset', action='store_true', help='Reset paper trades')
    args = p.parse_args()
    
    if args.reset:
        reset_paper_trades()
        sys.exit(0)
    
    if args.report:
        state = load_state()
        print_report(state)
        sys.exit(0)
    
    state = load_state()
    log(f"V18.5 Paper Trader | Bankroll: ${state['bankroll']:.2f} | "
        f"Trades: {state['total_trades']} | "
        f"Open: {len(state['open_positions'])}")
    
    if args.loop:
        log("Starting continuous scan loop (60s interval)...")
        while True:
            try:
                state = run_scan(state)
                time.sleep(SCAN_INTERVAL)
            except KeyboardInterrupt:
                log("Stopped by user.")
                break
            except Exception as e:
                log(f"ERROR: {e}")
                traceback.print_exc()
                time.sleep(30)
    else:
        state = run_scan(state)