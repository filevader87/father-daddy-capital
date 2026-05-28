#!/usr/bin/env python3
"""
V18.8 5-Minute BTC Up/Down Paper Trader
==========================================
Trades Polymarket hourly "Bitcoin Up or Down" markets using V18.8 signals.
The scanner runs every 60 seconds, detects BTC RSI/direction signals,
and buys the cheap side of the nearest hourly Up/Down market.

Signal → Market mapping:
- BUY_UP: BTC trending up + oversold → buy "Up" token when ≤15¢
- BUY_DOWN: BTC trending down + overbought → buy "Down" token when ≤15¢

Market discovery:
- Gamma API: bitcoin-up-or-down-{date}-{hour}{am/pm}-et
- Resolves every hour: "Will BTC price go Up or Down during this hour?"
- Uses Chainlink BTC/USD price feed
"""

import json, os, sys, time, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_engine_v18_8 import (
    INITIAL_BANKROLL, MIN_CONFIDENCE, RSI_OVERSOLD_SEVERE, RSI_OVERBOUGHT_SEVERE,
    WIN_PROB_BASE, CONFIDENCE_MAP, TIER_SIZE, TIER_MAX_PRICE, SCAN_SECONDS,
    MAX_OPEN_POSITIONS, MIN_BET,
    compute_rsi, detect_btc_direction, generate_signal_v188,
    fetch_btc_candles, compute_win_probability, kelly_size, TradeJournal,
    get_regime,
)
import urllib.request

OUTPUT = Path(__file__).parent / "output"
OUTPUT.mkdir(exist_ok=True)
STATE_FILE = OUTPUT / "v188_5min_paper_state.json"
LOG_FILE = Path(__file__).parent / "paper_trades" / "scanner_v188_5min.log"
LOG_FILE.parent.mkdir(exist_ok=True)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            pass
    return {
        "bankroll": INITIAL_BANKROLL,
        "positions": {},
        "total_pnl": 0.0,
        "trades": [],
        "resolutions": [],
        "last_scan": None,
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def fetch_updown_markets():
    """Discover active Bitcoin Up/Down hourly markets from Gamma API."""
    now = datetime.now(timezone.utc)
    markets = []
    
    # Generate slugs for the next 3 hours
    for offset_hours in range(0, 4):
        target = now + timedelta(hours=offset_hours)
        # Convert to ET (UTC-4 or UTC-5)
        et = target - timedelta(hours=4)  # Approximate ET offset (EDT = UTC-4)
        
        hour_12 = et.hour % 12
        if hour_12 == 0:
            hour_12 = 12
        period = "am" if et.hour < 12 else "pm"
        
        # Format: bitcoin-up-or-down-may-28-2026-2pm-et  
        date_str = et.strftime("%B").lower() + f"-{et.day}-{et.year}"
        slug = f"bitcoin-up-or-down-{date_str}-{hour_12}{period}-et"
        
        try:
            url = f"{GAMMA_API}/markets?slug={slug}"
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V18-5min/1.0'})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            
            for m in data:
                if not m.get('active', False) or m.get('closed', False):
                    continue
                    
                question = m.get('question', '')
                if 'bitcoin' not in question.lower():
                    continue
                    
                clob_str = m.get('clobTokenIds', '[]')
                if isinstance(clob_str, str):
                    try:
                        clob = json.loads(clob_str)
                    except:
                        clob = []
                else:
                    clob = clob_str
                
                if len(clob) < 2:
                    continue
                
                end_date = m.get('endDate', '')
                prices = m.get('outcomePrices', ['0.5', '0.5'])
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except:
                        prices = ['0.5', '0.5']
                
                up_price = float(prices[0]) if prices and prices[0] else 0.5
                down_price = float(prices[1]) if len(prices) > 1 and prices[1] else 0.5
                
                # Get CLOB prices for more accurate live pricing
                up_clob = get_clob_price(clob[0])
                down_clob = get_clob_price(clob[1])
                
                if up_clob:
                    up_price = up_clob
                if down_clob:
                    down_price = down_clob
                
                # Determine cheap side
                cheap_side = 'Up' if up_price <= down_price else 'Down'
                cheap_price = min(up_price, down_price)
                expensive_price = max(up_price, down_price)
                
                markets.append({
                    'question': question,
                    'slug': slug,
                    'condition_id': m.get('conditionId', ''),
                    'up_token_id': clob[0],
                    'down_token_id': clob[1],
                    'up_price': up_price,
                    'down_price': down_price,
                    'cheap_side': cheap_side,
                    'cheap_price': cheap_price,
                    'expensive_price': expensive_price,
                    'end_date': end_date,
                    'volume24hr': m.get('volume24hr', 0),
                    'market_active': m.get('active', False),
                })
        except Exception as e:
            log(f"[WARN] Could not fetch slug {slug}: {e}")
    
    return markets


def get_clob_price(token_id):
    """Get live CLOB price for a token."""
    try:
        url = f"{CLOB_API}/price?token_id={token_id}&side=buy"
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V18-5min/1.0'})
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        if isinstance(data, dict) and 'price' in data:
            return float(data['price'])
    except:
        pass
    return None


def tier_label(strategy):
    """Get tier label and parameters for a strategy."""
    if strategy.startswith('severe_'):
        return 1, TIER_SIZE.get(strategy, 0.10), TIER_MAX_PRICE.get(strategy, 0.30)
    elif strategy in ('oversold_down', 'overbought_up'):
        return 2, TIER_SIZE.get(strategy, 0.06), TIER_MAX_PRICE.get(strategy, 0.15)
    elif strategy.startswith('direction_'):
        return 3, TIER_SIZE.get(strategy, 0.03), TIER_MAX_PRICE.get(strategy, 0.08)
    else:
        return 2, 0.06, 0.15


def resolve_positions():
    """Check if any open positions have resolved."""
    state = load_state()
    to_remove = []
    
    for trade_id, trade in list(state.get('positions', {}).items()):
        if trade.get('status') != 'open':
            continue
            
        # Fetch market result
        slug = trade.get('market_slug', '')
        try:
            url = f"{GAMMA_API}/markets?slug={slug}"
            req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V18-5min/1.0'})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            
            for m in data:
                if m.get('closed', False) or m.get('resolved', False):
                    # Market resolved
                    outcomes = m.get('outcomes', [])
                    prices = m.get('outcomePrices', [])
                    if isinstance(prices, str):
                        try:
                            prices = json.loads(prices)
                        except:
                            prices = []
                    
                    if len(outcomes) >= 2 and len(prices) >= 2:
                        up_resolved = float(prices[0]) > 0.5
                        down_resolved = float(prices[1]) > 0.5
                        winning_side = 'Up' if up_resolved else 'Down'
                        
                        if trade.get('side') == winning_side:
                            # WIN
                            payout = trade['bet'] / trade['entry_price']
                            profit = payout - trade['bet']
                            state['bankroll'] += payout
                            trade['outcome'] = 'win'
                            trade['pnl'] = profit
                            trade['payout'] = payout
                            trade['resolved_at'] = datetime.now(timezone.utc).isoformat()
                            log(f"✅ WIN: {trade_id} | {trade['side']} @ {trade['entry_price']*100:.1f}¢ | Payout: ${payout:.2f} | Profit: ${profit:.2f}")
                        else:
                            # LOSS
                            state['bankroll'] -= trade['bet']
                            trade['outcome'] = 'loss'
                            trade['pnl'] = -trade['bet']
                            trade['payout'] = 0
                            trade['resolved_at'] = datetime.now(timezone.utc).isoformat()
                            log(f"❌ LOSS: {trade_id} | {trade['side']} @ {trade['entry_price']*100:.1f}¢ | Lost: ${trade['bet']:.2f}")
                        
                        trade['status'] = 'resolved'
                        state['resolutions'].append(trade)
                        to_remove.append(trade_id)
                        
                        # Update journal
                        for t in state.get('trades', []):
                            if t.get('id') == trade_id:
                                t['outcome'] = trade['outcome']
                                t['pnl'] = trade['pnl']
                                t['status'] = 'resolved'
        except Exception as e:
            log(f"[WARN] Could not check resolution for {trade_id}: {e}")
    
    for trade_id in to_remove:
        if trade_id in state.get('positions', {}):
            del state['positions'][trade_id]
    
    save_state(state)


def run_scan():
    """Single scan iteration for 5-min Up/Down markets."""
    state = load_state()
    journal = TradeJournal()
    
    # Load existing trades into journal
    for t in state.get('trades', []):
        if t.get('outcome') == 'win':
            journal.total_wins += 1
            journal.total_trades += 1
        elif t.get('outcome') == 'loss':
            journal.total_trades += 1
    
    # Count resolved trades
    resolved = len([t for t in state.get('trades', []) if t.get('status') == 'resolved'])
    wins = len([t for t in state.get('trades', []) if t.get('outcome') == 'win'])
    wr = wins / resolved * 100 if resolved > 0 else 0
    
    log(f"📊 Bankroll: ${state['bankroll']:.2f} | Trades: {resolved} (WR: {wr:.1f}%) | P&L: ${state.get('total_pnl', 0):.2f}")
    
    # 1. Resolve any completed positions
    resolve_positions()
    state = load_state()
    
    # 2. Fetch BTC candles
    candles = fetch_btc_candles('5m', 100)
    if not candles:
        log("❌ Could not fetch BTC candles")
        return
    
    prices = [c['close'] for c in candles]
    log(f"  BTC: ${prices[-1]:,.0f} | {len(candles)} candles")
    
    # 3. Compute RSI
    rsi_arr = compute_rsi(prices)
    current_rsi = rsi_arr[-1]
    
    # 4. Direction
    direction, strength = detect_btc_direction(candles, len(candles)-1)
    
    # 5. Regime
    regime = get_regime(prices)
    
    # RSI zone label
    if current_rsi < 25:
        zone = "SEVERE_OVERSOLD"
    elif current_rsi < 30:
        zone = "OVERSOLD"
    elif current_rsi < 35:
        zone = "NEAR_OVERSOLD"
    elif current_rsi > 73:
        zone = "SEVERE_OVERBOUGHT"
    elif current_rsi > 70:
        zone = "OVERBOUGHT"
    elif current_rsi > 65:
        zone = "NEAR_OVERBOUGHT"
    else:
        zone = "NEUTRAL"
    
    log(f"  RSI: {current_rsi:.1f} ({zone}) | Dir: {direction} ({strength:.2f}%) | Regime: {regime}")
    
    # 6. Generate signal
    signal = generate_signal_v188(prices, candles, len(candles)-1)
    
    if signal['direction'] == 'neutral':
        reason = signal.get('strategy', 'no_signal')
        log(f"  ⏸️ No signal — {reason} (conf={signal.get('confidence',0):.2f})")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # 7. SIGNAL DETECTED
    sig_dir = signal['direction'].upper()  # UP or DOWN
    sig_conf = signal['confidence']
    sig_strategy = signal['strategy']
    tier, tier_size, tier_max_price = tier_label(sig_strategy)
    
    log(f"  ⭐ SIGNAL: BUY_{sig_dir} | Tier {tier} | Strategy: {sig_strategy} | Conf: {sig_conf:.1%}")
    log(f"     RSI={current_rsi:.1f} Dir={direction} Regime={regime} | Size: {tier_size:.0%} bankroll, max price: {tier_max_price*100:.0f}¢")
    
    # 8. Find matching Up/Down market
    markets = fetch_updown_markets()
    if not markets:
        log("  ❌ No active Up/Down markets found")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # Filter: must have enough volume and be the nearest future market
    # Sort by end date (nearest first)
    markets.sort(key=lambda m: m['end_date'])
    
    # Skip markets that expire in < 15 minutes (too late to enter)
    now = datetime.now(timezone.utc)
    viable = []
    for m in markets:
        try:
            end_str = m['end_date']
            if end_str.endswith('Z'):
                end = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
            else:
                end = datetime.fromisoformat(end_str)
            # Ensure timezone-aware
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            minutes_left = (end - now).total_seconds() / 60
            m['minutes_left'] = minutes_left
            if minutes_left > 15 and minutes_left < 180:  # 15min-3h window
                viable.append(m)
        except Exception as e:
            log(f"[WARN] Could not parse end_date '{m.get('end_date','')}': {e}")
    
    if not viable:
        log("  ❌ No viable Up/Down markets (need 15-180 min left)")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # Pick the nearest market with the matching side
    # Strategy: 
    # 1. Direct: signal matches cheap side (DOWN signal + Down ≤ 8¢) — momentum entry
    # 2. Reversion: signal opposite to cheap side (DOWN signal + Up ≤ 8¢) — contrarian entry
    # 3. Start-of-hour: both sides near 50¢ — directional bet at fair price
    best_market = None
    best_price = None
    best_side = None
    best_entry_type = None
    
    for m in viable:
        up_price = m['up_price']
        down_price = m['down_price']
        
        # Direct alignment: signal matches the cheap side
        if sig_dir == 'UP' and up_price <= tier_max_price * 1.5:
            best_market = m
            best_price = up_price
            best_side = 'Up'
            best_entry_type = 'direct'
            break
        elif sig_dir == 'DOWN' and down_price <= tier_max_price * 1.5:
            best_market = m
            best_price = down_price
            best_side = 'Down'
            best_entry_type = 'direct'
            break
        
        # Start-of-hour entry: both sides near 50¢, directional bet at fair price
        # This is when the hour just started and the market hasn't moved yet
        if min(up_price, down_price) >= 0.35 and max(up_price, down_price) <= 0.65:
            if best_market is None or m['minutes_left'] < best_market.get('minutes_left', 999):
                best_market = m
                best_price = up_price if sig_dir == 'UP' else down_price
                best_side = sig_dir.capitalize()
                best_entry_type = 'fair_price'
        
        # Note: contrarian entries (buying opposite side of signal) are REMOVED
        # PMXT validation shows direction alignment is key — contrarian flips the edge
        # Only direct (signal=cheap) and fair-price (both~50¢) entries are valid
    
    if best_market is None:
        log(f"  ❌ No market found for BUY_{sig_dir} — no viable market side")
        for m in viable[:3]:
            log(f"     Market: {m['question'][:60]} | Up={m['up_price']*100:.1f}¢ Down={m['down_price']*100:.1f}¢ | {m['minutes_left']:.0f}min left")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # 9. Determine trade side and price
    if best_side == 'Up':
        token_id = best_market['up_token_id']
    else:
        token_id = best_market['down_token_id']
    
    entry_price = best_price
    question = best_market['question']
    end_date = best_market['end_date']
    minutes_left = best_market['minutes_left']
    
    log(f"  📈 Market: {question}")
    log(f"     Side: {best_side} @ {entry_price*100:.1f}¢ ({best_entry_type}) | Expires in {minutes_left:.0f}min")
    log(f"     Volume: ${best_market.get('volume24hr', 0):,.0f}")
    
    # 10. Compute trade
    win_prob = compute_win_probability(sig_strategy, entry_price)
    edge = win_prob - entry_price
    odds = 1.0 - entry_price
    
    if edge < 0.03:
        log(f"  ❌ Edge too small: {edge:.3f}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # Position sizing
    cal_factor = journal.get_calibration_factor()
    kelly_bet = kelly_size(edge, odds, state['bankroll'], cal_factor, sig_conf, state.get('updates', 0))
    max_bet = state['bankroll'] * tier_size
    bet = min(kelly_bet, max_bet)
    bet = max(MIN_BET, min(bet, state['bankroll'] * 0.15))
    
    # Position limit
    open_positions = sum(1 for p in state.get('positions', {}).values() if p.get('status') == 'open')
    if open_positions >= MAX_OPEN_POSITIONS:
        log(f"  ⚠️ Max positions: {open_positions}/{MAX_OPEN_POSITIONS}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # Deduplication: don't enter the same market twice
    market_slug = best_market.get('slug', '')
    existing_in_market = [p for p in state.get('positions', {}).values()
                          if p.get('status') == 'open' and p.get('market_slug') == market_slug]
    if existing_in_market:
        log(f"  ⚠️ Already have position in {market_slug}, skipping")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # Kill switch
    if state['bankroll'] < 5.0:
        log(f"  🛑 Kill switch: bankroll ${state['bankroll']:.2f} < minimum")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # 11. RECORD TRADE (paper)
    log(f"  📝 TRADE: BUY_{best_side} @ {entry_price*100:.1f}¢ ({best_entry_type}) | Bet: ${bet:.2f} ({tier_size:.0%} tier)")
    log(f"     Win prob: {win_prob:.1%} | Edge: {edge:.3f} | Odds: {odds:.2f}:1")
    log(f"     Strategy: {sig_strategy} (Tier {tier}) | Kelly: ${kelly_bet:.2f}")
    log(f"     Market: {question[:60]}")
    
    trade = {
        "id": f"T5M{len(state.get('trades', []))+1:04d}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": f"BUY_{best_side}",
        "strategy": sig_strategy,
        "tier": tier,
        "market_type": "5min_updown",
        "entry_type": best_entry_type,
        "condition_id": best_market.get('condition_id', ''),
        "token_id": token_id,
        "side": best_side,
        "entry_price": entry_price,
        "bet": round(bet, 2),
        "tier_pct": tier_size,
        "edge": round(edge, 4),
        "win_prob": round(win_prob, 4),
        "confidence": round(sig_conf, 3),
        "rsi": round(current_rsi, 1),
        "direction": direction,
        "regime": regime,
        "btc_price": prices[-1],
        "bankroll_at_entry": round(state['bankroll'], 2),
        "market_slug": best_market.get('slug', ''),
        "market_question": question,
        "market_end_date": end_date,
        "minutes_left": round(minutes_left, 1),
        "status": "open",
        "outcome": "pending",
    }
    
    state['bankroll'] -= bet  # Deduct bet from bankroll (paper)
    state['trades'].append(trade)
    state['updates'] = state.get('updates', 0) + 1
    state['positions'][trade['id']] = trade
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    
    log(f"  💰 Bankroll: ${state['bankroll']:.2f} | Open: {open_positions + 1}/{MAX_OPEN_POSITIONS}")
    log(f"     Market expires: {end_date}")


def main_loop():
    log("=" * 70)
    log("V18.8 5-MIN BTC UP/DOWN PAPER TRADER")
    log(f"Bankroll: ${INITIAL_BANKROLL} | Min Confidence: {MIN_CONFIDENCE}")
    log(f"T1: RSI<{RSI_OVERSOLD_SEVERE}/>{RSI_OVERBOUGHT_SEVERE} → 10% pos, ≤50¢ | T2: moderate → 5-6%, ≤20¢ | T3: direction+cheap → 3%, ≤8¢")
    log(f"Scanning every {SCAN_SECONDS}s | Max positions: {MAX_OPEN_POSITIONS}")
    log("=" * 70)
    
    while True:
        try:
            run_scan()
        except Exception as e:
            log(f"❌ Error: {e}")
            traceback.print_exc(file=sys.stderr)
        
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run continuous loop")
    parser.add_argument("--once", action="store_true", help="Single scan only")
    parser.add_argument("--resolve", action="store_true", help="Only resolve open positions")
    args = parser.parse_args()
    
    if args.resolve:
        resolve_positions()
    elif args.once:
        run_scan()
    else:
        main_loop()