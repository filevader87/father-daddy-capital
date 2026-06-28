#!/usr/bin/env python3
"""
V18.9 5-Minute BTC Up/Down Paper Trader
=========================================
Trades Polymarket 5-minute and 15-minute "BTC Up or Down" markets
using V18.8 RSI/direction signals with proper exit strategies.

Market discovery:
- Gamma API series: btc-up-or-down-5m, btc-up-or-down-15m
- Each market resolves in 5 or 15 minutes based on Chainlink BTC/USD
- New windows appear continuously (288/day for 5m, 96/day for 15m)

Exit strategies:
1. Stop-loss: Sell if token price drops 50% from entry
2. Take-profit: Sell if token price reaches 90¢ (near-guaranteed win)
3. Trailing stop: Lock in 60% of max gain after 2+ minutes
4. Time-decay: Exit if losing with <1 min left and price > 3¢
5. Expiry: Hold to settlement for binary resolution

Entry types:
- Direct: signal aligns with cheap side (≤8¢) — PMXT validated
- Fair-price: both sides near 50¢, directional bet — requires CONF ≥ 0.70
"""

import json, os, sys, time, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_engine_v18_8 import (
    MIN_CONFIDENCE, RSI_OVERSOLD_SEVERE, RSI_OVERBOUGHT_SEVERE,
    SCAN_SECONDS, MIN_BET,
    compute_rsi, detect_btc_direction, generate_signal_v188,
    fetch_btc_candles, compute_win_probability, kelly_size, TradeJournal,
    get_regime,
)

import urllib.request

OUTPUT = Path(__file__).parent / "output"
OUTPUT.mkdir(exist_ok=True)
STATE_FILE = OUTPUT / "v189_5m_paper_state.json"
LOG_FILE = Path(__file__).parent / "paper_trades" / "scanner_v189_5m.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
BANKROLL = 400.0                    # Starting bankroll
MAX_OPEN_POSITIONS = 3              # Max concurrent positions
POSITION_SIZE_PCT = 0.03            # 3% of bankroll per trade
MAX_POSITION_PCT = 0.08            # 8% max position size
MIN_CONFIDENCE_FAIR_PRICE = 0.70    # Higher threshold for fair-price entries

# Timeframes to scan (in priority order: 5m first for faster resolution)
SERIES_CONFIG = [
    {"slug": "btc-up-or-down-5m", "label": "5m", "window_mins": 5, "min_remaining": 1.5, "max_remaining": 4.5},
    {"slug": "btc-up-or-down-15m", "label": "15m", "window_mins": 15, "min_remaining": 3, "max_remaining": 13},
]

# Exit strategy thresholds
STOP_LOSS_PCT = 0.50        # Sell if token drops 50% from entry (e.g., 8¢ → 4¢)
TAKE_PROFIT_PRICE = 0.90    # Sell if token reaches 90¢ (near-guaranteed win)
TRAILING_STOP_PCT = 0.40    # Lock in 60% of gain (sell if drops 40% from peak)
TRAILING_ACTIVATE_MINS = 2.0 # Start trailing after 2 minutes
TIME_DECAY_SELL_MINS = 1.0  # Consider time-decay exit if <1 min left
TIME_DECAY_MIN_PRICE = 0.03 # Don't time-decay sell if price < 3¢ (not worth gas)

# Cheap-side thresholds per tier
TIER_CONFIG = {
    "severe_oversold":  {"size": 0.10, "max_price": 0.30},
    "severe_overbought": {"size": 0.10, "max_price": 0.30},
    "oversold_down":    {"size": 0.06, "max_price": 0.15},
    "overbought_up":    {"size": 0.06, "max_price": 0.15},
    "direction_down_cheap": {"size": 0.03, "max_price": 0.08},
    "direction_up_cheap":   {"size": 0.03, "max_price": 0.08},
}


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
        "bankroll": BANKROLL,
        "positions": {},
        "total_pnl": 0.0,
        "trades": [],
        "resolutions": [],
        "last_scan": None,
        "version": "v189",
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def get_clob_price(token_id):
    """Get live mid-price from CLOB for a token."""
    try:
        url = f"{CLOB_API}/price?token_id={token_id}&side=buy"
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V189/1.0'})
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        if isinstance(data, dict) and 'price' in data:
            return float(data['price'])
    except:
        pass
    return None


def gamma_get(path, params=None):
    """Helper for Gamma API GET requests."""
    try:
        query = ""
        if params:
           query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{GAMMA_API}/{path}{query}"
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC-V189/1.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        log(f"[WARN] Gamma API error: {e}")
        return []


def fetch_5m_15m_markets():
    """Discover active 5m and 15m BTC Up/Down markets via Gamma series API."""
    now = datetime.now(timezone.utc)
    all_markets = []

    for config in SERIES_CONFIG:
        slug = config["slug"]
        label = config["label"]
        window_mins = config["window_mins"]

        # Get active events for this series that close soon (next 30 min)
        events = gamma_get("events", {
            "limit": "10",
            "series_slug": slug,
            "active": "true",
            "closed": "false",
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

        for event in events:
            markets = event.get("markets", [])
            for m in markets:
                if not m.get("active", False) or m.get("closed", False):
                    continue

                question = m.get("question", "")
                if "bitcoin" not in question.lower() and "btc" not in question.lower():
                    continue

                # Get token IDs
                clob_str = m.get("clobTokenIds", "[]")
                if isinstance(clob_str, str):
                    try:
                        clob = json.loads(clob_str)
                    except:
                        clob = []
                else:
                    clob = clob_str
                if len(clob) < 2:
                    continue

                # Get prices
                prices = m.get("outcomePrices", [])
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except:
                        prices = []
                outcomes = m.get("outcomes", [])
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except:
                        outcomes = []

                if len(prices) < 2:
                    continue

                up_idx = 0
                down_idx = 1
                if len(outcomes) >= 2:
                    if outcomes[1].lower() == "up":
                        up_idx = 1
                        down_idx = 0

                up_price = float(prices[up_idx]) if prices[up_idx] else 0.5
                down_price = float(prices[down_idx]) if len(prices) > down_idx and prices[down_idx] else 0.5

                # Try CLOB for live prices
                up_clob = get_clob_price(clob[up_idx]) if up_idx < len(clob) else None
                down_clob = get_clob_price(clob[down_idx]) if down_idx < len(clob) else None
                if up_clob:
                    up_price = up_clob
                if down_clob:
                    down_price = down_clob

                # Calculate time remaining
                end_str = m.get("endDate", event.get("endDate", ""))
                try:
                    if end_str.endswith("Z"):
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    else:
                        end_dt = datetime.fromisoformat(end_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    minutes_left = (end_dt - now).total_seconds() / 60
                except:
                    continue

                # Determine cheap side
                cheap_side = "Up" if up_price <= down_price else "Down"
                cheap_price = min(up_price, down_price)

                # Determine price reference
                price_beat = None
                # Extract price to beat from question or description
                desc = m.get("description", "")

                all_markets.append({
                    "question": question,
                    "slug": m.get("slug", event.get("slug", "")),
                    "condition_id": m.get("conditionId", ""),
                    "event_slug": event.get("slug", ""),
                    "up_token_id": clob[up_idx] if up_idx < len(clob) else "",
                    "down_token_id": clob[down_idx] if down_idx < len(clob) else "",
                    "up_price": up_price,
                    "down_price": down_price,
                    "cheap_side": cheap_side,
                    "cheap_price": cheap_price,
                    "end_date": end_str,
                    "minutes_left": minutes_left,
                    "window_mins": window_mins,
                    "label": label,
                    "volume24hr": m.get("volume24hr", 0),
                    "market_active": m.get("active", False),
                    "series_slug": slug,
                })

    # Sort by time remaining (nearest first)
    all_markets.sort(key=lambda m: m["minutes_left"])
    return all_markets


def evaluate_exits(state):
    """Check open positions for exit conditions using live CLOB prices."""
    now = datetime.now(timezone.utc)
    to_close = []

    for pos_id, pos in list(state.get("positions", {}).items()):
        if pos.get("status") != "open":
            continue

        entry_price = pos.get("entry_price", 0.5)
        entry_time = pos.get("entry_time", pos.get("timestamp", ""))
        try:
            if entry_time.endswith("Z"):
                et = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
            else:
                et = datetime.fromisoformat(entry_time)
            if et.tzinfo is None:
                et = et.replace(tzinfo=timezone.utc)
            elapsed_mins = (now - et).total_seconds() / 60
        except:
            elapsed_mins = 999

        # Get current price from CLOB
        token_id = pos.get("token_id", "")
        cur_price = get_clob_price(token_id) if token_id else None

        # Also check if market has resolved
        slug = pos.get("market_slug", "")
        event_slug = pos.get("event_slug", "")

        if cur_price is None:
            # Can't evaluate without price — check resolution instead
            resolved = check_market_resolution(pos)
            if resolved:
                to_close.append((pos_id, resolved))
            continue

        # Track peak price for trailing stop
        peak_price = pos.get("peak_price", entry_price)
        if cur_price > peak_price:
            pos["peak_price"] = cur_price
            peak_price = cur_price

        # ── Exit 1: Stop-Loss ──
        # Token drops 50% from entry (e.g., bought at 8¢, now 4¢)
        price_drop = (entry_price - cur_price) / entry_price if entry_price > 0 else 0
        if price_drop >= STOP_LOSS_PCT and cur_price > 0:
            exit_value = pos["bet"] * (cur_price / entry_price)
            to_close.append((pos_id, {
                "exit_type": "stop_loss",
                "exit_price": cur_price,
                "exit_value": round(exit_value, 2),
                "pnl": round(exit_value - pos["bet"], 2),
                "price_drop_pct": round(price_drop * 100, 1),
                "elapsed_mins": round(elapsed_mins, 1),
            }))
            continue

        # ── Exit 2: Take-Profit ──
        # Token reaches 90¢ — near-certain win, lock it in
        if cur_price >= TAKE_PROFIT_PRICE:
            exit_value = pos["bet"] * (cur_price / entry_price)
            to_close.append((pos_id, {
                "exit_type": "take_profit",
                "exit_price": cur_price,
                "exit_value": round(exit_value, 2),
                "pnl": round(exit_value - pos["bet"], 2),
                "elapsed_mins": round(elapsed_mins, 1),
            }))
            continue

        # ── Exit 3: Trailing Stop ──
        # After 2+ minutes, if we had a peak and price dropped 40% from that peak
        if elapsed_mins >= TRAILING_ACTIVATE_MINS and peak_price > entry_price:
            drop_from_peak = (peak_price - cur_price) / peak_price if peak_price > 0 else 0
            if drop_from_peak >= TRAILING_STOP_PCT:
                exit_value = pos["bet"] * (cur_price / entry_price)
                gain_from_entry = (cur_price - entry_price) / entry_price if entry_price > 0 else 0
                to_close.append((pos_id, {
                    "exit_type": "trailing_stop",
                    "exit_price": cur_price,
                    "exit_value": round(exit_value, 2),
                    "pnl": round(exit_value - pos["bet"], 2),
                    "peak_price": peak_price,
                    "drop_from_peak_pct": round(drop_from_peak * 100, 1),
                    "gain_from_entry_pct": round(gain_from_entry * 100, 1),
                    "elapsed_mins": round(elapsed_mins, 1),
                }))
                continue

        # ── Exit 4: Time-Decay ──
        # If losing and <1 min left, exit for scraps if price > 3¢
        end_str = pos.get("market_end_date", "")
        try:
            if end_str.endswith("Z"):
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            else:
                end_dt = datetime.fromisoformat(end_str)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            remaining = (end_dt - now).total_seconds() / 60
        except:
            remaining = 999

        if remaining < TIME_DECAY_SELL_MINS and cur_price < entry_price * 0.5:
            if cur_price > TIME_DECAY_MIN_PRICE:
                exit_value = pos["bet"] * (cur_price / entry_price)
                to_close.append((pos_id, {
                    "exit_type": "time_decay",
                    "exit_price": cur_price,
                    "exit_value": round(exit_value, 2),
                    "pnl": round(exit_value - pos["bet"], 2),
                    "remaining_mins": round(remaining, 2),
                    "elapsed_mins": round(elapsed_mins, 1),
                }))
                continue

        # Update position with current price for logging
        pos["last_price"] = cur_price
        pos["last_check"] = now.isoformat()

    # Process exits
    for pos_id, exit_info in to_close:
        pos = state["positions"][pos_id]
        exit_value = exit_info.get("exit_value", 0)
        pnl = exit_info.get("pnl", exit_value - pos["bet"])

        state["bankroll"] += exit_value
        pos["status"] = "closed"
        pos["outcome"] = exit_info["exit_type"]
        pos["pnl"] = pnl
        pos["exit_price"] = exit_info.get("exit_price", 0)
        pos["exit_type"] = exit_info["exit_type"]
        pos["closed_at"] = now.isoformat()

        # Update trade record
        for t in state.get("trades", []):
            if t.get("id") == pos_id:
                t.update({
                    "status": "closed",
                    "outcome": exit_info["exit_type"],
                    "pnl": pnl,
                    "exit_price": exit_info.get("exit_price", 0),
                })

        state["resolutions"].append({**pos, **exit_info})

        emoji = "🎯" if exit_info["exit_type"] == "take_profit" else "🛑" if exit_info["exit_type"] == "stop_loss" else "📉" if exit_info["exit_type"] == "trailing_stop" else "⏰"
        log(f"{emoji} EXIT {exit_info['exit_type']}: {pos_id} | {pos['side']} @ {pos['entry_price']*100:.1f}¢ → {exit_info.get('exit_price',0)*100:.1f}¢ | PnL: ${pnl:+.2f} | Bankroll: ${state['bankroll']:.2f}")
        del state["positions"][pos_id]

    save_state(state)
    return len(to_close) > 0


def check_market_resolution(pos):
    """Check if a market has resolved on Polymarket."""
    slug = pos.get("market_slug", "")
    if not slug:
        return None

    try:
        data = gamma_get("markets", {"slug": slug})
        for m in data:
            if m.get("closed", False) or m.get("resolved", False):
                # Determine winner
                outcomes = m.get("outcomes", [])
                prices = m.get("outcomePrices", [])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if isinstance(prices, str):
                    prices = json.loads(prices)

                if len(outcomes) >= 2 and len(prices) >= 2:
                    winning_idx = 0 if float(prices[0]) > float(prices[1]) else 1
                    winning_side = outcomes[winning_idx]

                    side = pos.get("side", "")
                    if side.lower() == winning_side.lower():
                        # WIN — full payout at $1 per share
                        payout = pos["bet"] / pos["entry_price"]
                        profit = payout - pos["bet"]
                        return {
                            "exit_type": "expiry_win",
                            "exit_price": 1.0,
                            "exit_value": round(payout, 2),
                            "pnl": round(profit, 2),
                            "winning_side": winning_side,
                        }
                    else:
                        # LOSS — token goes to $0
                        return {
                            "exit_type": "expiry_loss",
                            "exit_price": 0.0,
                            "exit_value": 0.0,
                            "pnl": round(-pos["bet"], 2),
                            "winning_side": winning_side,
                        }
    except Exception as e:
        log(f"[WARN] Resolution check failed for {slug}: {e}")

    return None


def resolve_positions():
    """Check if any open positions have resolved via market expiry."""
    state = load_state()
    to_remove = []

    for trade_id, trade in list(state.get("positions", {}).items()):
        if trade.get("status") != "open":
            continue

        result = check_market_resolution(trade)
        if result:
            if result["exit_type"] == "expiry_win":
                state["bankroll"] += result["exit_value"]
                emoji = "✅"
            else:
                state["bankroll"] += result["exit_value"]
                emoji = "❌"

            trade["status"] = "resolved"
            trade["outcome"] = result["exit_type"]
            trade["pnl"] = result["pnl"]
            trade["exit_price"] = result.get("exit_price", 0)
            trade["resolved_at"] = datetime.now(timezone.utc).isoformat()

            state["resolutions"].append({**trade, **result})
            to_remove.append(trade_id)

            log(f"{emoji} {result['exit_type'].upper()}: {trade_id} | {trade['side']} @ {trade['entry_price']*100:.1f}¢ | PnL: ${result['pnl']:+.2f} | Bankroll: ${state['bankroll']:.2f}")

            for t in state.get("trades", []):
                if t.get("id") == trade_id:
                    t.update({"status": "resolved", "outcome": result["exit_type"], "pnl": result["pnl"]})

    for trade_id in to_remove:
        if trade_id in state.get("positions", {}):
            del state["positions"][trade_id]

    save_state(state)


def run_scan():
    """Single scan iteration for 5m/15m BTC Up/Down markets."""
    state = load_state()
    journal = TradeJournal()

    for t in state.get("trades", []):
        if t.get("outcome") == "win" or t.get("outcome") == "expiry_win":
            journal.total_wins += 1
            journal.total_trades += 1
        elif t.get("outcome") in ("loss", "expiry_loss", "stop_loss", "trailing_stop", "time_decay"):
            journal.total_trades += 1

    resolved = len([t for t in state.get("trades", []) if t.get("status") == "resolved" or t.get("status") == "closed"])
    wins = len([t for t in state.get("trades", []) if t.get("outcome") in ("win", "expiry_win", "take_profit")])
    wr = wins / resolved * 100 if resolved > 0 else 0
    total_pnl = sum(r.get("pnl", 0) for r in state.get("resolutions", []))

    log(f"📊 Bankroll: ${state['bankroll']:.2f} | Trades: {resolved} (WR: {wr:.1f}%) | P&L: ${total_pnl:+.2f}")

    # 1. Check exits on open positions
    evaluate_exits(state)

    # 2. Resolve any expired positions
    resolve_positions()
    state = load_state()

    # 3. Fetch BTC candles
    candles = fetch_btc_candles('5m', 100)
    if not candles:
        log("❌ Could not fetch BTC candles")
        return

    prices = [c['close'] for c in candles]
    log(f"  BTC: ${prices[-1]:,.0f} | {len(candles)} candles")

    # 4. Compute RSI
    rsi_arr = compute_rsi(prices)
    current_rsi = rsi_arr[-1]

    # 5. Direction
    direction, strength = detect_btc_direction(candles, len(candles) - 1)

    # 6. Regime
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

    # 7. Generate signal
    signal = generate_signal_v188(prices, candles, len(candles) - 1)

    if signal['direction'] == 'neutral':
        reason = signal.get('strategy', 'no_signal')
        log(f"  ⏸️ No signal — {reason} (conf={signal.get('confidence', 0):.2f})")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # 8. SIGNAL DETECTED
    sig_dir = signal['direction'].upper()
    sig_conf = signal['confidence']
    sig_strategy = signal['strategy']

    tier_cfg = TIER_CONFIG.get(sig_strategy, {"size": 0.03, "max_price": 0.08})
    tier_size = tier_cfg["size"]
    tier_max_price = tier_cfg["max_price"]

    tier_num = 1 if tier_size >= 0.10 else (2 if tier_size >= 0.05 else 3)

    log(f"  ⭐ SIGNAL: BUY_{sig_dir} | Tier {tier_num} | Strategy: {sig_strategy} | Conf: {sig_conf:.1%}")
    log(f"     RSI={current_rsi:.1f} Dir={direction} Regime={regime} | Size: {tier_size:.0%} bankroll, max price: {tier_max_price*100:.0f}¢")

    # 9. Find matching 5m/15m market
    markets = fetch_5m_15m_markets()
    if not markets:
        log("  ❌ No active 5m/15m markets found")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # Filter markets by time remaining and price
    now = datetime.now(timezone.utc)
    viable = []
    for m in markets:
        mins_left = m["minutes_left"]
        window = m.get("window_mins", 5)
        config = None
        for c in SERIES_CONFIG:
            if c["slug"] == m.get("series_slug", ""):
                config = c
                break
        if config:
            if not (config["min_remaining"] <= mins_left <= config["max_remaining"]):
                continue
        else:
            if not (1 <= mins_left <= 30):
                continue
        viable.append(m)

    if not viable:
        log(f"  ❌ No viable markets (found {len(markets)} total, none with right time window)")
        for m in markets[:3]:
            log(f"     {m['label']}: {m['question'][:60]} | Up={m['up_price']*100:.1f}¢ Down={m['down_price']*100:.1f}¢ | {m['minutes_left']:.1f}min left")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # 10. Find best entry — direct (cheap side) or fair-price
    best_market = None
    best_price = None
    best_side = None
    best_entry_type = None

    for m in viable:
        up_price = m["up_price"]
        down_price = m["down_price"]
        cheap = m["cheap_side"]
        cheap_p = m["cheap_price"]

        # Direct: signal aligns with cheap side AND price ≤ max
        if sig_dir == "UP" and cheap == "Up" and cheap_p <= tier_max_price * 1.5:
            best_market = m
            best_price = up_price
            best_side = "Up"
            best_entry_type = "direct"
            break
        elif sig_dir == "DOWN" and cheap == "Down" and cheap_p <= tier_max_price * 1.5:
            best_market = m
            best_price = down_price
            best_side = "Down"
            best_entry_type = "direct"
            break

        # Signal opposite side is cheap (e.g., DOWN signal, Up is cheap at ≤8¢)
        # This is a REVERSION play — skip for now
        # Direct alignment only

    # Fair-price fallback: both sides near 50¢, directional bet at fair odds
    if best_market is None:
        for m in viable:
            up_price = m["up_price"]
            down_price = m["down_price"]
            if min(up_price, down_price) >= 0.35 and max(up_price, down_price) <= 0.65:
                if sig_conf >= MIN_CONFIDENCE_FAIR_PRICE:
                    if best_market is None or m["minutes_left"] < best_market.get("minutes_left", 999):
                        best_market = m
                        best_price = up_price if sig_dir == "UP" else down_price
                        best_side = sig_dir.capitalize()
                        best_entry_type = "fair_price"

    if best_market is None:
        log(f"  ❌ No market for BUY_{sig_dir} — no cheap side or fair-price available")
        for m in viable[:3]:
            log(f"     {m['label']}: {m['question'][:60]} | Up={m['up_price']*100:.1f}¢ Down={m['down_price']*100:.1f}¢ | {m['minutes_left']:.1f}min left")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # 11. Determine trade side and price
    if best_side == "Up":
        token_id = best_market["up_token_id"]
    else:
        token_id = best_market["down_token_id"]

    entry_price = best_price
    question = best_market["question"]
    end_date = best_market["end_date"]
    minutes_left = best_market["minutes_left"]
    window_label = best_market["label"]

    log(f"  📈 Market: {question[:70]}")
    log(f"     Side: {best_side} @ {entry_price*100:.1f}¢ ({best_entry_type}) | {window_label} window | Expires in {minutes_left:.1f}min")

    # 12. Compute trade
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
    kelly_bet = kelly_size(edge, odds, state["bankroll"], cal_factor, sig_conf, state.get("updates", 0))
    max_bet = state["bankroll"] * tier_size
    max_cap = state["bankroll"] * MAX_POSITION_PCT
    bet = min(kelly_bet, max_bet, max_cap)
    bet = max(MIN_BET, bet)

    # Position limit
    open_positions = sum(1 for p in state.get("positions", {}).values() if p.get("status") == "open")
    if open_positions >= MAX_OPEN_POSITIONS:
        log(f"  ⚠️ Max positions: {open_positions}/{MAX_OPEN_POSITIONS}")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # Dedup: don't enter the same market twice
    market_slug = best_market.get("slug", best_market.get("event_slug", ""))
    existing = [p for p in state.get("positions", {}).values()
                if p.get("status") == "open" and (p.get("market_slug") == market_slug or p.get("event_slug") == market_slug)]
    if existing:
        log(f"  ⚠️ Already have position in {market_slug}, skipping")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # Kill switch
    if state["bankroll"] < 5.0:
        log(f"  🛑 Kill switch: bankroll ${state['bankroll']:.2f} < minimum")
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # 13. RECORD TRADE
    log(f"  📝 TRADE: BUY_{best_side} @ {entry_price*100:.1f}¢ ({best_entry_type}) | Bet: ${bet:.2f} ({tier_size:.0%} tier)")
    log(f"     Win prob: {win_prob:.1%} | Edge: {edge:.3f} | Odds: {odds:.2f}:1")
    log(f"     Strategy: {sig_strategy} (Tier {tier_num}) | Kelly: ${kelly_bet:.2f}")
    log(f"     Market: {question[:60]} ({window_label})")

    trade_id = f"T5M{len(state.get('trades', [])) + 1:04d}"
    trade = {
        "id": trade_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "action": f"BUY_{best_side}",
        "strategy": sig_strategy,
        "tier": tier_num,
        "market_type": f"{window_label}_updown",
        "entry_type": best_entry_type,
        "condition_id": best_market.get("condition_id", ""),
        "token_id": token_id,
        "side": best_side,
        "entry_price": entry_price,
        "peak_price": entry_price,  # Track for trailing stop
        "bet": round(bet, 2),
        "tier_pct": tier_size,
        "edge": round(edge, 4),
        "win_prob": round(win_prob, 4),
        "confidence": round(sig_conf, 3),
        "rsi": round(current_rsi, 1),
        "direction": direction,
        "regime": regime,
        "btc_price": prices[-1],
        "bankroll_at_entry": round(state["bankroll"], 2),
        "market_slug": market_slug,
        "event_slug": best_market.get("event_slug", ""),
        "market_question": question,
        "market_end_date": end_date,
        "series_slug": best_market.get("series_slug", ""),
        "window_label": window_label,
        "minutes_left": round(minutes_left, 1),
        "status": "open",
        "outcome": "pending",
    }

    state["bankroll"] -= bet
    state["trades"].append(trade)
    state["positions"][trade_id] = trade
    state["updates"] = state.get("updates", 0) + 1
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    log(f"  💰 Bankroll: ${state['bankroll']:.2f} | Open: {open_positions + 1}/{MAX_OPEN_POSITIONS}")
    log(f"     Market expires: {end_date}")


def main_loop():
    log("=" * 70)
    log("V18.9 5M/15M BTC UP/DOWN PAPER TRADER")
    log(f"Bankroll: ${BANKROLL} | Min Confidence: {MIN_CONFIDENCE}")
    log(f"T1: RSI<{RSI_OVERSOLD_SEVERE}/>{RSI_OVERBOUGHT_SEVERE} → 10% pos, ≤50¢")
    log(f"T2: moderate → 6%, ≤20¢ | T3: direction+cheap → 3%, ≤8¢")
    log(f"Exits: SL@{STOP_LOSS_PCT:.0%} | TP@{TAKE_PROFIT_PRICE*100:.0f}¢ | Trail@{TRAILING_STOP_PCT:.0%} after {TRAILING_ACTIVATE_MINS}min")
    log(f"Markets: 5m (btc-up-or-down-5m) + 15m (btc-up-or-down-15m)")
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
    parser.add_argument("--exits", action="store_true", help="Only evaluate exits")
    args = parser.parse_args()

    if args.resolve:
        resolve_positions()
    elif args.exits:
        state = load_state()
        evaluate_exits(state)
    elif args.once:
        run_scan()
    else:
        main_loop()