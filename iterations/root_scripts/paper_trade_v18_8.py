#!/usr/bin/env python3
"""
V18.7 Paper Trading Scanner — 3-Tier Entry
==============================================
Tier 1: Severe RSI (80%+ WR) → 10% position, ≤50¢ entry (daily strikes)
Tier 2: Moderate RSI + confirmations (67-72% WR) → 5-6% position, ≤20¢ entry
Tier 3: Direction + cheap-side (55-58% WR @ ≤12¢) → 3% position, ≤12¢ entry

Cheap-side asymmetry: at 5¢, need 6% WR to break even.
"""

import json, os, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_engine_v18_8 import (
    INITIAL_BANKROLL, MIN_CONFIDENCE, RSI_OVERSOLD_SEVERE, RSI_OVERBOUGHT_SEVERE,
    WIN_PROB_BASE, CONFIDENCE_MAP, TIER_SIZE, TIER_MAX_PRICE, SCAN_SECONDS,
    MAX_OPEN_POSITIONS, MIN_BET,
    compute_rsi, detect_btc_direction, generate_signal_v188,
    fetch_btc_candles, fetch_btc_updown_markets, fetch_clob_price,
    find_market_for_signal, fetch_btc_price_markets,
    compute_win_probability, kelly_size, TradeJournal,
    get_regime, is_bear_market, is_uptrend, is_downtrend
)

OUTPUT = Path(__file__).parent / "output"
OUTPUT.mkdir(exist_ok=True)
STATE_FILE = OUTPUT / "v188_paper_state.json"
LOG_FILE = Path(__file__).parent / "paper_trades" / "scanner_v188.log"
LOG_FILE.parent.mkdir(exist_ok=True)


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
        "daily_pnl": 0.0,
        "updates": 0,
        "trades": [],
        "last_scan": None,
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def tier_label(strategy):
    """Get tier label and parameters for a strategy."""
    if strategy.startswith('severe_'):
        return 1, TIER_SIZE.get(strategy, 0.10), TIER_MAX_PRICE.get(strategy, 0.30)
    elif strategy in ('oversold_down', 'overbought_up'):
        return 2, TIER_SIZE.get(strategy, 0.06), TIER_MAX_PRICE.get(strategy, 0.15)
    elif strategy.startswith('direction_'):
        return 3, TIER_SIZE.get(strategy, 0.03), TIER_MAX_PRICE.get(strategy, 0.10)
    else:
        return 2, 0.06, 0.15  # default to moderate


def run_scan():
    """Single scan iteration."""
    state = load_state()
    journal = TradeJournal()
    
    # Load existing trades into journal
    for t in state.get("trades", []):
        if t.get("outcome") == "win":
            journal.total_wins += 1
            journal.total_trades += 1
        elif t.get("outcome") == "loss":
            journal.total_trades += 1
    
    log(f"📊 Bankroll: ${state['bankroll']:.2f} | Trades: {journal.total_trades} | WR: {journal.get_wr():.1%}")
    
    # 1. Fetch BTC candles
    candles = fetch_btc_candles('5m', 100)
    if not candles:
        log("❌ Could not fetch BTC candles")
        return
    
    prices = [c['close'] for c in candles]
    log(f"  BTC: ${prices[-1]:,.0f} | {len(candles)} candles")
    
    # 2. Compute RSI
    rsi_arr = compute_rsi(prices)
    current_rsi = rsi_arr[-1]
    
    # 3. Direction
    direction, strength = detect_btc_direction(candles, len(candles)-1)
    
    # 4. Regime
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
    
    # 5. Generate signal
    signal = generate_signal_v188(prices, candles, len(candles)-1)
    
    if signal['direction'] == 'neutral':
        reason = signal.get('strategy', 'no_signal')
        if 'blacklist' in reason:
            reason = f"{reason}: {signal.get('blacklist_reason', '')}"
        log(f"  ⏸️ No signal — {reason} (conf={signal.get('confidence',0):.2f})")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # 6. SIGNAL DETECTED
    sig_dir = signal['direction'].upper()
    sig_conf = signal['confidence']
    sig_strategy = signal['strategy']
    tier, tier_size, tier_max_price = tier_label(sig_strategy)
    
    log(f"  ⭐ SIGNAL: BUY_{sig_dir} | Tier {tier} | Strategy: {sig_strategy} | Conf: {sig_conf:.1%}")
    log(f"     RSI={current_rsi:.1f} Dir={direction} Regime={regime} | Size: {tier_size:.0%} bankroll, max price: {tier_max_price*100:.0f}¢")
    
    # 7. Market discovery — use BTC price-above markets
    btc_price = signal['price']
    market_result = find_market_for_signal(sig_dir, btc_price, tier, tier_max_price)
    
    if market_result is None:
        log(f"  ❌ No suitable {sig_dir} market found (≤{tier_max_price*100:.0f}¢ for BTC@${btc_price:,.0f})")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    best_market = market_result['market']
    best_price = market_result['price']
    side = market_result['side']  # 'YES' or 'NO'
    strike = market_result['strike']
    question = market_result['question']
    
    log(f"  📈 Market: {side} @ {best_price*100:.1f}¢ | Strike=${strike:,}")
    log(f"     \"{question[:65]}\"")
    log(f"     Volume: ${market_result['volume']:,.0f} | Expires in {market_result['hours_left']:.0f}h")
    
    # Check price — upgrade tier if market price exceeds tier max but fits a higher tier
    if best_price > tier_max_price * 1.5:
        # Try upgrading to a higher tier with larger max price
        for upgrade_tier, upgrade_max in [('1', 0.50), ('2', 0.20)]:
            if best_price <= upgrade_max * 1.5:
                old_tier = tier
                old_size = tier_size
                old_max = tier_max_price
                tier = int(upgrade_tier)
                tier_size = {'1': 0.10, '2': 0.055}[upgrade_tier]
                tier_max_price = upgrade_max
                log(f"  ⬆️ Tier upgrade: T{old_tier}→T{tier} at {best_price*100:.1f}¢ (max {old_max*100:.0f}¢→{tier_max_price*100:.0f}¢)")
                break
        else:
            log(f"  ❌ Price too high: {best_price*100:.1f}¢ — no tier fits (max T1=50¢×1.5=75¢)")
            state["last_scan"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            return
    
    # 8. Compute trade — use tier sizing
    win_prob = compute_win_probability(sig_strategy, best_price)
    edge = win_prob - best_price
    odds = 1.0 - best_price
    
    if edge < 0.03:  # Tier 3 lower edge threshold (3% vs 5%)
        log(f"  ❌ Edge too small: {edge:.3f}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # Position sizing: tier-based, not pure Kelly
    # Kelly would give 10-12% for severe, but we cap by tier
    cal_factor = journal.get_calibration_factor()
    kelly_bet = kelly_size(edge, odds, state['bankroll'], cal_factor, sig_conf, state.get('updates', 0))
    
    # Tier sizing: min of Kelly and tier limit
    max_bet = state['bankroll'] * tier_size
    bet = min(kelly_bet, max_bet)
    bet = max(MIN_BET, min(bet, state['bankroll'] * 0.15))  # absolute cap at 15%
    
    # Position limit
    open_positions = sum(1 for p in state.get('positions', {}).values() if p.get('status') == 'open')
    if open_positions >= MAX_OPEN_POSITIONS:
        log(f"  ⚠️ Max positions: {open_positions}/{MAX_OPEN_POSITIONS}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # Kill switch
    if state['bankroll'] < 5.0:
        log(f"  🛑 Kill switch: bankroll ${state['bankroll']:.2f} < minimum")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return
    
    # 9. RECORD TRADE (paper)
    # token_id already resolved by find_market_for_signal
    token_id = market_result.get('token_id', best_market.get('yes_token_id' if side == 'YES' else 'no_token_id', ''))
    
    log(f"  📝 TRADE: BUY_{side} @ {best_price*100:.1f}¢ | Bet: ${bet:.2f} ({tier_size:.0%} tier)")
    log(f"     Win prob: {win_prob:.1%} | Edge: {edge:.3f} | Odds: {odds:.2f}:1")
    log(f"     Market: {best_market['question'][:60]}")
    log(f"     Strategy: {sig_strategy} (Tier {tier}) | Kelly: ${kelly_bet:.2f}")
    
    trade = {
        "id": f"T{len(state.get('trades', []))+1:04d}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": f"BUY_{side}",
        "strategy": sig_strategy,
        "tier": tier,
        "condition_id": best_market.get('condition_id', ''),
        "token_id": token_id,
        "contract_price": best_price,
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
        "status": "open",
        "outcome": "pending",
    }
    
    state['trades'].append(trade)
    state['updates'] = state.get('updates', 0) + 1
    state['positions'][trade['id']] = trade
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    
    log(f"  💰 Bankroll: ${state['bankroll']:.2f} | Open: {open_positions + 1}/{MAX_OPEN_POSITIONS}")


def main_loop():
    log("=" * 70)
    log("V18.8 PAPER TRADING SCANNER — ALWAYS-ON 3-TIER")
    log(f"Bankroll: ${INITIAL_BANKROLL} | Min Confidence: {MIN_CONFIDENCE}")
    log(f"T1: RSI<{RSI_OVERSOLD_SEVERE}/>{RSI_OVERBOUGHT_SEVERE} → 10% pos, ≤50¢ | T2: moderate → 5-6%, ≤20¢ | T3: direction+cheap → 3%, ≤12¢")
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
    args = parser.parse_args()
    
    if args.once:
        run_scan()
    else:
        main_loop()