#!/usr/bin/env python3
"""
Father Daddy Capital — Polymarket Paper Trading Module
Daily EOD BTC above/below binary contracts. Edge: 5-min signal enters before
price crosses the strike boundary. All trades simulated, no real USDC.

BTC is currently ~$79,600. The $80K May 8 contract is 47.5% YES / 52.5% NO.
Our signals (RSI divergence, MACD cross, volume spike on 5-min candles) tell us
which direction BTC is likely to move in the next few candles. If we're confident
BTC touches $80K, we buy YES at 0.475 (implied 47.5% → true probability higher).
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

GAMMA  = "https://gamma-api.polymarket.com"
CLOB   = "https://clob.polymarket.com"
OUTPUT = Path("/mnt/c/Users/12035/father_daddy_capital/output")


def _get(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-fdc/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _parse_field(val):
    """Parse double-encoded JSON fields."""
    if isinstance(val, str):
        try: return json.loads(val)
        except (json.JSONDecodeError, TypeError): return val
    return val


# ─── Market Discovery ─────────────────────────────────────────────────────────

def find_btc_daily_markets() -> list[dict]:
    """Discover active daily BTC above/below contracts. Returns nearest-strike markets."""
    # Search for today/tomorrow BTC daily contracts
    today = datetime.now()
    # Try "Bitcoin above ___ on May N" pattern
    month_name = today.strftime("%B")
    day = today.day

    # Search gamma for today
    for day_offset in [0, 1, 2, 3]:  # today through +3 days
        target_day = day + day_offset
        query = f"Bitcoin above on {month_name} {target_day}"
        q = urllib.parse.quote(query)
        url = f"{GAMMA}/public-search?q={q}"
        data = _get(url)

        for evt in data.get("events", []):
            # Skip closed events
            markets = []
            for m in evt.get("markets", []):
                if not m.get("closed", False):
                    markets.append({
                        "question": m.get("question", "?"),
                        "conditionId": m.get("conditionId", ""),
                        "outcomePrices": _parse_field(m.get("outcomePrices", [])),
                        "clobTokenIds": _parse_field(m.get("clobTokenIds", [])),
                        "volume": float(m.get("volume", 0)),
                        "slug": evt.get("slug", ""),
                        "event_title": evt.get("title", ""),
                        "settle_date": f"{today.strftime('%Y-%m')}-{target_day:02d}",
                    })

            if markets:
                return markets
    return []


def extract_strike(question: str) -> float | None:
    """Extract BTC price strike from market question. E.g. 'above $80,000' → 80000.0"""
    import re
    m = re.search(r'\$([\d,]+)', question)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


# ─── Signal Integration ───────────────────────────────────────────────────────

def btc_signal_5m(prices: list[float]) -> dict:
    """
    Run our signal stack on 5-minute BTC candles. Returns direction + confidence.
    Uses yfinance 5m data passed in from paper_engine.py.
    """
    if len(prices) < 14:
        return {"direction": "neutral", "confidence": 0.0, "signals": {}}

    # ── RSI(7) on 5m candles ──
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    avg_gain = sum(gains[-7:]) / 7
    avg_loss = sum(losses[-7:]) / 7
    rs = avg_gain / max(avg_loss, 1e-9)
    rsi = 100 - (100 / (1 + rs))

    # ── MACD ──
    def ema(vals, span):
        alpha = 2 / (span + 1)
        result = vals[0]
        for v in vals[1:]:
            result = alpha * v + (1 - alpha) * result
        return result

    ema12 = ema(prices, 12)
    ema26 = ema(prices, 26)
    macd = ema12 - ema26

    # Simplified: just recent direction
    last_3 = sum(1 for i in range(1, min(4, len(prices))) if prices[-i] > prices[-i-1])
    momentum_up = last_3 >= 2

    # ── Volume proxy: price range expansion ──
    recent_range = max(prices[-5:]) - min(prices[-5:])
    prior_range = max(prices[-10:-5]) - min(prices[-10:-5]) if len(prices) >= 10 else recent_range
    vol_expanding = recent_range > prior_range * 1.5

    # ── Aggregate ──
    signals = {"rsi": round(rsi, 1), "macd": round(macd, 2), "vol_expanding": vol_expanding,
               "momentum_3_candles": momentum_up}

    direction = "neutral"
    confidence = 0.0

    # Oversold + volume spike + momentum turning = strong UP signal
    if rsi < 35 and vol_expanding and momentum_up:
        direction = "up"
        confidence = min(0.75, (40 - rsi) / 25)
    # Overbought + volume spike + momentum fading = DOWN signal
    elif rsi > 65 and vol_expanding and not momentum_up:
        direction = "down"
        confidence = min(0.75, (rsi - 60) / 25)
    # Mild signal
    elif rsi < 40 and momentum_up:
        direction = "up"
        confidence = 0.35
    elif rsi > 60 and not momentum_up:
        direction = "down"
        confidence = 0.35

    return {"direction": direction, "confidence": round(confidence, 3), "signals": signals,
            "current_price": prices[-1]}


# ─── Trade Decision ───────────────────────────────────────────────────────────

def evaluate_polymarket_trades(btc_price: float, signal: dict,
                                portfolio: dict) -> list[dict]:
    """
    Check Polymarket BTC daily contracts against our 5-min signal.
    Returns recommended paper trades.

    portfolio = {"polymarket_positions": {...}, "polymarket_pnl": 0.0}
    """
    markets = find_btc_daily_markets()
    if not markets:
        return []

    trades = []
    direction = signal["direction"]
    confidence = signal["confidence"]

    if direction == "neutral" or confidence < 0.30:
        return trades

    # Find the nearest strike above and below current price
    nearest_above = None
    nearest_below = None

    for mkt in markets:
        strike = extract_strike(mkt["question"])
        if strike is None:
            continue

        prices = mkt["outcomePrices"]
        if not isinstance(prices, list) or len(prices) < 2:
            continue

        if strike > btc_price:
            if nearest_above is None or strike < nearest_above["strike"]:
                nearest_above = {"strike": strike, "market": mkt,
                                 "yes_price": float(prices[0]), "no_price": float(prices[1])}
        elif strike < btc_price:
            if nearest_below is None or strike > nearest_below["strike"]:
                nearest_below = {"strike": strike, "market": mkt,
                                 "yes_price": float(prices[0]), "no_price": float(prices[1])}

    # ── Trade logic ──
    positions = portfolio.get("polymarket_positions", {})

    if direction == "up" and nearest_above:
        na = nearest_above
        distance_pct = (na["strike"] - btc_price) / btc_price * 100
        yes_price = na["yes_price"]

        # Enter if strike is within 3% and YES is cheap enough (< 0.80)
        if distance_pct < 3.0 and yes_price < 0.80:
            # Kelly-inspired: bet size proportional to edge
            edge = confidence - yes_price  # how much we beat the market
            if edge > 0.05:
                kelly_fraction = edge / (1 - yes_price)  # simplified
                bet_size = min(50.0, max(10.0, kelly_fraction * 200))

                # Don't double-enter the same strike
                strike_key = f"BTC>{na['strike']}"
                if strike_key not in positions:
                    trades.append({
                        "action": "BUY_YES",
                        "market_question": na["market"]["question"],
                        "strike": na["strike"],
                        "conditionId": na["market"]["conditionId"],
                        "yes_price": yes_price,
                        "no_price": na["no_price"],
                        "bet_size": round(bet_size, 2),
                        "edge": round(edge, 3),
                        "btc_price": btc_price,
                        "distance_pct": round(distance_pct, 2),
                        "signal_confidence": confidence,
                        "signal_rsi": signal["signals"]["rsi"],
                        "timestamp": datetime.now().isoformat(),
                    })

    if direction == "down" and nearest_below:
        nb = nearest_below
        distance_pct = (btc_price - nb["strike"]) / btc_price * 100
        no_price = nb["no_price"]

        # Enter when signal says down and NO is cheap (< 0.75) — we're betting BTC drops
        if distance_pct < 1.0 and no_price < 0.75:
            edge = confidence - no_price
            if edge > 0.05:
                kelly_fraction = edge / (1 - no_price)
                bet_size = min(50.0, max(10.0, kelly_fraction * 200))

                strike_key = f"BTC<{nb['strike']}"
                if strike_key not in positions:
                    trades.append({
                        "action": "BUY_NO",
                        "market_question": nb["market"]["question"],
                        "strike": nb["strike"],
                        "conditionId": nb["market"]["conditionId"],
                        "yes_price": nb["yes_price"],
                        "no_price": no_price,
                        "bet_size": round(bet_size, 2),
                        "edge": round(edge, 3),
                        "btc_price": btc_price,
                        "distance_pct": round(distance_pct, 2),
                        "signal_confidence": confidence,
                        "signal_rsi": signal["signals"]["rsi"],
                        "timestamp": datetime.now().isoformat(),
                    })

    return trades


# ─── Settlement Check ─────────────────────────────────────────────────────────

def check_settlements(portfolio: dict, btc_price: float) -> list[dict]:
    """
    Check open Polymarket positions — have any daily contracts settled?
    Daily contracts settle at midnight UTC on the target date.

    Returns list of settled positions with P&L.
    """
    positions = portfolio.get("polymarket_positions", {})
    settled = []

    for key, pos in list(positions.items()):
        settle_date_str = pos.get("settle_date", "")
        settle_dt = None
        if settle_date_str:
            try:
                settle_dt = datetime.strptime(settle_date_str, "%Y-%m-%d")
            except ValueError:
                pass

        now = datetime.utcnow()
        # Settlement: if settle date is in the past (in UTC) or price is clearly resolved
        target_reached = False

        if pos["action"] == "BUY_YES":
            target_reached = btc_price >= pos["strike"]
        else:  # BUY_NO
            target_reached = btc_price <= pos["strike"]

        # Settle if target date passed OR target reached
        should_settle = False
        if settle_dt and now > settle_dt:
            should_settle = True
        if target_reached and pos.get("btc_price", 0) != btc_price:
            # Price crossed the strike since we entered — mark for settlement
            # But only if the market is settled on Polymarket (we can't check easily, so use date)
            pass

        if should_settle or target_reached:
            # Calculate P&L
            bet = pos["bet_size"]
            if pos["action"] == "BUY_YES":
                if target_reached:
                    # YES wins → get $1 per share, paid (1 - yes_price) * bet
                    payout = bet / pos["yes_price"]  # shares bought
                    cost = bet
                    profit = payout - cost
                else:
                    profit = -bet  # lost entire bet
            else:  # BUY_NO
                if target_reached:
                    payout = bet / pos["no_price"]
                    cost = bet
                    profit = payout - cost
                else:
                    profit = -bet

            settled.append({
                "key": key,
                "action": pos["action"],
                "strike": pos["strike"],
                "bet_size": bet,
                "pnl": round(profit, 2),
                "settled": target_reached,
                "btc_final": btc_price,
                "timestamp": now.isoformat(),
            })

            del positions[key]

    return settled


# ─── Summary ──────────────────────────────────────────────────────────────────

def polymarket_summary(portfolio: dict, settled: list[dict]) -> str:
    """Generate Polymarket section for the report."""
    lines = ["\n🎲 Polymarket BTC Contracts"]

    positions = portfolio.get("polymarket_positions", {})
    total_pnl = portfolio.get("polymarket_pnl", 0.0)

    if settled:
        lines.append("  Settled:")
        for s in settled:
            emoji = "🟢" if s["pnl"] > 0 else "🔴"
            lines.append(
                f"    {emoji} {s['action']} @ ${s['strike']:,.0f} — "
                f"${s['pnl']:+,.2f} (bet ${s['bet_size']})"
            )

    if positions:
        lines.append(f"  Open ({len(positions)}):")
        for key, pos in positions.items():
            lines.append(
                f"    {pos['action']} {pos['market_question']} — "
                f"${pos['bet_size']} @ {pos['yes_price']:.3f} | "
                f"edge: {pos['edge']:.3f}"
            )
    elif not settled:
        lines.append("  No positions. Waiting for signal + strike proximity.")

    lines.append(f"  Total P&L: ${total_pnl:+,.2f}")
    return "\n".join(lines)


# ─── Main Cycle (called from paper_engine.py) ─────────────────────────────────

def run_polymarket_cycle(state: dict, btc_prices_5m: list[float]) -> tuple[list, list]:
    """
    Full Polymarket cycle: signal → trade → settlement.
    Called by paper_engine.py each scan.

    Returns (new_entries, settlements)
    """
    state.setdefault("polymarket_positions", {})
    state.setdefault("polymarket_pnl", 0.0)

    # ── 1. Generate BTC 5-min signal ──
    signal = btc_signal_5m(btc_prices_5m)
    btc_price = signal.get("current_price", btc_prices_5m[-1] if btc_prices_5m else 0)
    if btc_price == 0:
        return [], []

    # ── 2. Check settlements ──
    settlements = check_settlements(state, btc_price)
    for s in settlements:
        state["polymarket_pnl"] += s["pnl"]
        # Record to trade journal
        state.setdefault("trade_journal", [])
        state["trade_journal"].append({
            "type": "polymarket",
            **s,
        })

    # ── 3. Evaluate new trades ──
    entries = evaluate_polymarket_trades(btc_price, signal, state)
    for e in entries:
        key = f"BTC>{e['strike']}" if e["action"] == "BUY_YES" else f"BTC<{e['strike']}"
        state["polymarket_positions"][key] = e

    return entries, settlements


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--discover" in sys.argv:
        markets = find_btc_daily_markets()
        print(f"Found {len(markets)} active BTC daily markets:\n")
        for m in markets:
            print(f"  {m['question']}")
            prices = m["outcomePrices"]
            if isinstance(prices, list) and len(prices) >= 2:
                print(f"    YES: {float(prices[0])*100:.1f}% / NO: {float(prices[1])*100:.1f}%")
            print(f"    conditionId: {m['conditionId']}")
            print(f"    volume: ${m['volume']:,.0f}")
            print()

    elif "--signal" in sys.argv:
        # Quick demo signal with placeholder prices
        import numpy as np
        demo = list(np.random.normal(79500, 200, 20))
        signal = btc_signal_5m(demo)
        print(json.dumps(signal, indent=2))

    else:
        print(__doc__)
        print("Usage: python fdc_polymarket.py --discover   (list active BTC contracts)")
        print("       python fdc_polymarket.py --signal     (demo signal generation)")
