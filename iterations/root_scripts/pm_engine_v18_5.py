#!/usr/bin/env python3
"""
V18.5 — Live BTC Up/Down Direction Trading Engine

Strategy:
  1. Scan Gamma API for active BTC 5-min Up/Down markets
  2. Check BTC 5-min price direction from Binance
  3. When BTC trending UP: buy UP token if cheap (≤20¢)
  4. When BTC trending DOWN: buy DOWN token if cheap (≤20¢)
  5. Target: 80%+ WR with direction labels

Key breakthrough: PMXT data can't provide direction labels,
but the live Gamma API gives us UP/DOWN token IDs for every
Bitcoin 5-minute binary market.
"""

import json
import time
import sys
import urllib.request
from datetime import datetime, timezone
from collections import defaultdict
import numpy as np

# ============================================================
# CONFIG
# ============================================================
INITIAL_BANKROLL = 100
MAX_POSITION_PCT = 0.10  # max 10% of bankroll per trade
CHEAP_THRESHOLD = 0.20  # tokens ≤20¢ are "cheap"
MIN_DIRECTION_CHANGE = 0.0001  # min BTC % change for direction signal
SCAN_INTERVAL = 30  # seconds between scans
MARKET_DURATION = '5m'  # only trade 5-min markets

# ============================================================
# GAMMA API — Market Scanner
# ============================================================
def fetch_btc_updown_markets(duration=None, active_only=True):
    """Fetch BTC Up/Down markets from Gamma API.
    
    Args:
        duration: Filter by duration ('5m', '15m', etc.). None = all.
        active_only: If True, only return active markets.
    
    Returns:
        dict of condition_id -> {up_aid, down_aid, question, slug, duration, active, expires}
    """
    cid_map = {}
    
    for offset in range(0, 2000, 100):
        url = 'https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false&offset={}'.format(offset)
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'FDC/1.0',
                'Accept': 'application/json'
            })
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
            
            cid = m.get('conditionId', m.get('condition_id', ''))
            if not cid:
                continue
            cid_norm = cid.lower() if cid.startswith('0x') else '0x' + cid.lower()
            
            # Parse outcomes
            outcomes_raw = m.get('outcomes', '[]')
            if isinstance(outcomes_raw, str):
                try:
                    outcomes = json.loads(outcomes_raw)
                except json.JSONDecodeError:
                    outcomes = []
            else:
                outcomes = outcomes_raw
            
            # Parse clobTokenIds
            clob_ids_raw = m.get('clobTokenIds', '[]')
            if isinstance(clob_ids_raw, str):
                try:
                    clob_ids = json.loads(clob_ids_raw)
                except json.JSONDecodeError:
                    clob_ids = []
            elif isinstance(clob_ids_raw, list):
                clob_ids = [str(x) for x in clob_ids_raw]
            else:
                clob_ids = []
            
            if outcomes != ['Up', 'Down'] or len(clob_ids) < 2:
                continue
            
            # Parse slug for duration
            slug = m.get('slug', '')
            dur = 'unknown'
            if '5m' in slug:
                dur = '5m'
            elif '15m' in slug:
                dur = '15m'
            elif '1h' in slug:
                dur = '1h'
            elif '4h' in slug:
                dur = '4h'
            elif any(x in slug for x in ['daily', 'on-may', 'on-jun', 'on-jul']):
                dur = 'daily'
            
            if duration and dur != duration:
                continue
            
            end_date = m.get('endDate', m.get('end_date_iso', ''))
            end_ts = m.get('end_date_iso', '')
            
            cid_map[cid_norm] = {
                'up_aid': str(clob_ids[0]),
                'down_aid': str(clob_ids[1]),
                'question': m.get('question', ''),
                'slug': slug,
                'duration': dur,
                'active': True,
                'end_date': end_date,
                'condition_id': cid_norm,
            }
        
        if len(data) < 100:
            break
    
    return cid_map


# ============================================================
# BINANCE — BTC Price Feed
# ============================================================
def fetch_btc_prices(interval='5m', limit=12):
    """Fetch recent BTC/USDT prices from Binance.
    
    Args:
        interval: Candle interval ('1m', '5m', '15m', '1h')
        limit: Number of candles to fetch
    
    Returns:
        list of {ts, open, high, low, close, volume}
    """
    try:
        url = f'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}'
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC/1.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        prices = []
        for candle in data:
            prices.append({
                'ts': int(candle[0]) / 1000,
                'open': float(candle[1]),
                'high': float(candle[2]),
                'low': float(candle[3]),
                'close': float(candle[4]),
                'volume': float(candle[5]),
            })
        return prices
    except Exception as e:
        print(f"  [ERROR] Binance fetch: {e}")
        return []


def detect_btc_direction(prices_5m, prices_1m=None):
    """Detect BTC direction from price data.
    
    Returns:
        dict with 'direction', 'confidence', 'change_pct', 'current_price'
    """
    if not prices_5m:
        return {'direction': 'FLAT', 'confidence': 0, 'change_pct': 0, 'current_price': 0}
    
    current = prices_5m[-1]['close']
    
    # 5-minute trend
    if len(prices_5m) >= 2:
        prev_5m = prices_5m[-2]['close']
        change_5m = (current - prev_5m) / prev_5m
    else:
        change_5m = 0
    
    # 15-minute trend (3 candles ago)
    if len(prices_5m) >= 4:
        prev_15m = prices_5m[-4]['close']
        change_15m = (current - prev_15m) / prev_15m
    else:
        change_15m = change_5m
    
    # 1-hour trend (12 candles ago)
    if len(prices_5m) >= 12:
        prev_1h = prices_5m[0]['close']
        change_1h = (current - prev_1h) / prev_1h
    else:
        change_1h = change_15m
    
    # Direction signal
    if change_5m > MIN_DIRECTION_CHANGE and change_15m > 0:
        direction = 'UP'
        confidence = min(abs(change_15m) * 50 + abs(change_5m) * 20, 0.95)
    elif change_5m < -MIN_DIRECTION_CHANGE and change_15m < 0:
        direction = 'DOWN'
        confidence = min(abs(change_15m) * 50 + abs(change_5m) * 20, 0.95)
    else:
        direction = 'FLAT'
        confidence = 0.1
    
    return {
        'direction': direction,
        'confidence': confidence,
        'change_5m': change_5m * 100,
        'change_15m': change_15m * 100,
        'change_1h': change_1h * 100,
        'current_price': current,
    }


# ============================================================
# CLOB — Price Fetcher
# ============================================================
def fetch_clob_price(token_id):
    """Fetch current mid-price for a token from Polymarket CLOB.
    
    Returns:
        float: mid-price (0-1) or None on error
    """
    try:
        url = f'https://clob.polymarket.com/price?token_id={token_id}&side=buy'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'FDC/1.0',
            'Accept': 'application/json'
        })
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return float(data.get('price', 0))
    except Exception:
        return None


def fetch_clob_orderbook(token_id, side='buy'):
    """Fetch orderbook for a token from Polymarket CLOB.
    
    Returns:
        dict: {bids: [...], asks: [...]} or None
    """
    try:
        url = f'https://clob.polymarket.com/book?token_id={token_id}'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'FDC/1.0',
            'Accept': 'application/json'
        })
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data
    except Exception:
        return None


# ============================================================
# TRADE LOGIC
# ============================================================
def evaluate_market(market_info, btc_direction, bankroll):
    """Evaluate whether to trade a market.
    
    Args:
        market_info: dict from cid_map (up_aid, down_aid, etc.)
        btc_direction: dict from detect_btc_direction()
        bankroll: current bankroll (float)
    
    Returns:
        dict: {signal, token_to_buy, entry_price, direction, reason}
    """
    direction = btc_direction['direction']
    confidence = btc_direction['confidence']
    
    if direction == 'FLAT':
        return {'signal': 'SKIP', 'reason': 'BTC flat, no direction'}
    
    if confidence < 0.15:
        return {'signal': 'SKIP', 'reason': f'Low confidence ({confidence:.2f})'}
    
    # Determine which token to buy
    if direction == 'UP':
        token_to_buy = market_info['up_aid']
        token_label = 'UP'
    else:  # DOWN
        token_to_buy = market_info['down_aid']
        token_label = 'DOWN'
    
    # Fetch current price
    price = fetch_clob_price(token_to_buy)
    if price is None:
        return {'signal': 'ERROR', 'reason': 'Could not fetch price'}
    
    # Only buy cheap tokens (≤20¢)
    if price > CHEAP_THRESHOLD:
        return {
            'signal': 'SKIP',
            'reason': f'{token_label} token too expensive ({price:.4f} > {CHEAP_THRESHOLD})',
            'price': price,
            'direction': direction,
        }
    
    # Skip if price is too cheap (≤1¢ = essentially zero)
    if price < 0.01:
        return {
            'signal': 'SKIP',
            'reason': f'{token_label} token too cheap ({price:.4f} < 0.01)',
            'price': price,
            'direction': direction,
        }
    
    # Calculate position size
    position_size = min(bankroll * MAX_POSITION_PCT, 5.0)  # max $5 per trade
    shares = position_size / price
    
    # Expected value calculation
    # If BTC direction is correct: prob ≈ 0.60-0.70 (base rate for 5-min binaries)
    # Cheap token at 5-15¢ with correct direction: settles at $1
    # EV = prob * (1 - price) - (1 - prob) * price
    # At price=0.10, prob=0.65: EV = 0.65*0.90 - 0.35*0.10 = 0.585 - 0.035 = 0.55
    
    win_prob = min(0.55 + confidence * 0.30, 0.85)  # base + direction confidence
    ev = win_prob * (1 - price) - (1 - win_prob) * price
    ev_pct = ev / price * 100  # EV as % of investment
    
    return {
        'signal': 'BUY',
        'token_to_buy': token_to_buy,
        'token_label': token_label,
        'entry_price': price,
        'direction': direction,
        'confidence': confidence,
        'win_prob': win_prob,
        'ev': ev,
        'ev_pct': ev_pct,
        'position_size': position_size,
        'shares': shares,
        'market': market_info['question'],
        'reason': f'{token_label} @ {price:.4f}, BTC {direction}, prob={win_prob:.1%}, EV={ev:.3f} ({ev_pct:.0f}%)',
    }


# ============================================================
# LIVE SCANNER
# ============================================================
def run_scanner(duration='5m', dry_run=True):
    """Run continuous scanner for BTC Up/Down trading opportunities.
    
    Args:
        duration: Market duration to scan ('5m', '15m', etc.)
        dry_run: If True, only print signals without executing
    """
    print("=" * 70)
    print(f"V18.5 Live Scanner — BTC Up/Down {duration} Markets")
    print("=" * 70)
    print(f"Bankroll: ${INITIAL_BANKROLL} | Max position: {MAX_POSITION_PCT*100}%")
    print(f"Cheap threshold: ≤{CHEAP_THRESHOLD*100:.0f}¢ | Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print()
    
    # Step 1: Fetch market metadata
    print("[1] Fetching BTC Up/Down markets from Gamma API...")
    cid_map = fetch_btc_updown_markets(duration=duration)
    
    by_dur = defaultdict(int)
    for v in cid_map.values():
        by_dur[v['duration']] += 1
    print(f"    Found {len(cid_map)} BTC Up/Down markets:")
    for d, c in sorted(by_dur.items()):
        print(f"      {d}: {c}")
    print()
    
    if not cid_map:
        print("No active markets found. Exiting.")
        return
    
    # Step 2: Fetch BTC prices
    print("[2] Fetching BTC prices from Binance...")
    prices_5m = fetch_btc_prices('5m', 12)
    prices_1m = fetch_btc_prices('1m', 5)
    
    btc_dir = detect_btc_direction(prices_5m, prices_1m)
    print(f"    BTC/USDT: ${btc_dir['current_price']:,.2f}")
    print(f"    5m: {btc_dir['change_5m']:+.3f}% | 15m: {btc_dir['change_15m']:+.3f}% | 1h: {btc_dir['change_1h']:+.3f}%")
    print(f"    Direction: {btc_dir['direction']} (confidence: {btc_dir['confidence']:.2f})")
    print()
    
    # Step 3: Evaluate each market
    print(f"[3] Evaluating {len(cid_map)} markets...")
    bankroll = INITIAL_BANKROLL
    signals = []
    
    for cid, info in sorted(cid_map.items(), key=lambda x: x[1].get('question', '')):
        result = evaluate_market(info, btc_dir, bankroll)
        
        if result['signal'] == 'BUY':
            signals.append(result)
            print(f"    ✅ SIGNAL: {result['reason']}")
            print(f"       Market: {info['question'][:70]}")
            print(f"       Position: ${result['position_size']:.2f} ({result['shares']:.1f} shares)")
        elif result['signal'] == 'SKIP' and 'too expensive' in result.get('reason', ''):
            pass  # Silent skip for expensive tokens (normal)
        elif result['signal'] != 'ERROR':
            print(f"    ⏭️  {info['question'][:50]}: {result.get('reason', 'skip')}")
    
    # Step 4: Summary
    print()
    print("=" * 70)
    print(f"SCAN COMPLETE: {len(signals)} BUY signals out of {len(cid_map)} markets")
    if signals:
        print("\n actionable signals:")
        for s in signals:
            print(f"  {s['token_label']:4s} @ ${s['entry_price']:.4f} | EV=${s['ev']:.3f} ({s['ev_pct']:.0f}%) | {s['market'][:50]}")
    print()
    
    return signals


def run_live_loop(duration='5m', dry_run=True):
    """Run continuous scanning loop.
    
    Args:
        duration: Market duration to scan
        dry_run: If True, only print signals without executing
    """
    iteration = 0
    trade_log = []
    
    print("=" * 70)
    print(f"V18.5 Live Trading Loop — BTC Up/Down {duration} Markets")
    print("=" * 70)
    print(f"Bankroll: ${INITIAL_BANKROLL} | Max position: {MAX_POSITION_PCT*100}%")
    print(f"Cheap threshold: ≤{CHEAP_THRESHOLD*100:.0f}¢ | Interval: {SCAN_INTERVAL}s")
    print(f"Mode: {'DRY RUN (no execution)' if dry_run else '⚠️ LIVE TRADING'}")
    print()
    
    while True:
        iteration += 1
        now = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
        print(f"\n--- [{now}] Scan #{iteration} ---")
        
        try:
            # Fetch markets
            cid_map = fetch_btc_updown_markets(duration=duration)
            
            # Fetch BTC direction
            prices_5m = fetch_btc_prices('5m', 12)
            btc_dir = detect_btc_direction(prices_5m)
            
            print(f"  BTC: ${btc_dir['current_price']:,.2f} | 5m: {btc_dir['change_5m']:+.3f}% | Dir: {btc_dir['direction']} ({btc_dir['confidence']:.2f})")
            
            # Evaluate markets
            for cid, info in cid_map.items():
                result = evaluate_market(info, btc_dir, INITIAL_BANKROLL)
                
                if result['signal'] == 'BUY':
                    print(f"  >>> BUY {result['token_label']} @ {result['entry_price']:.4f} | {result['market'][:50]}")
                    trade_log.append({
                        'iteration': iteration,
                        'timestamp': now,
                        'market': info['question'],
                        'direction': result['direction'],
                        'token_label': result['token_label'],
                        'entry_price': result['entry_price'],
                        'win_prob': result['win_prob'],
                        'ev': result['ev'],
                        'ev_pct': result['ev_pct'],
                    })
            
            # Rate limit
            time.sleep(SCAN_INTERVAL)
            
        except KeyboardInterrupt:
            print("\n\nStopped by user.")
            break
        except Exception as e:
            print(f"  [ERROR] {e}")
            time.sleep(5)
    
    # Print trade log
    if trade_log:
        print(f"\n\n{'='*70}")
        print(f"TRADE LOG: {len(trade_log)} signals generated")
        print(f"{'='*70}")
        for t in trade_log:
            print(f"  [{t['timestamp']}] {t['direction']:4s} | {t['token_label']} @ {t['entry_price']:.4f} | EV={t['ev']:.3f} ({t['ev_pct']:.0f}%) | {t['market'][:50]}")


# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='V18.5 Live BTC Up/Down Scanner')
    p.add_argument('--scan', action='store_true', help='Single scan for opportunities')
    p.add_argument('--live', action='store_true', help='Continuous live scanning loop')
    p.add_argument('--duration', default='5m', help='Market duration filter (5m, 15m, all)')
    p.add_argument('--dry-run', action='store_true', default=True, help='Dry run mode (no execution)')
    p.add_argument('--execute', action='store_true', help='Enable live execution (DEPRECATED - use wallet)')
    args = p.parse_args()
    
    if args.live:
        run_live_loop(duration=args.duration, dry_run=not args.execute)
    else:
        # Default: single scan
        run_scanner(duration=args.duration, dry_run=not args.execute)