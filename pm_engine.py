#!/usr/bin/env python3
"""
Father Daddy Capital — Polymarket Engine (Standalone)
======================================================
Dedicated 2-minute scan loop. Finds ALL BTC time-bound contracts.
Asymmetric entry sizing: Kelly × 1.5 for high-conviction, compounding.

Strategy:
  Every 2 minutes: fetch BTC 5m candles, run RSI(7)/MACD/volume stack.
  Query Polymarket for active BTC contracts. Find strikes within 4%.
  Enter when edge > 0.03. Compound all profits. No stop-loss — binaries
  are full-risk by default, so only enter when edge is real.

All trades simulated paper. Zero real USDC until live gates met.
"""

import json
import urllib.request
import urllib.parse
import re
import time
import sys
import yfinance as yf
from datetime import datetime, timedelta
from pathlib import Path

GAMMA  = "https://gamma-api.polymarket.com"
CLOB   = "https://clob.polymarket.com"
OUTPUT = Path("/mnt/c/Users/12035/father_daddy_capital/output")
STATE  = Path("/mnt/c/Users/12035/father_daddy_capital/output/pm_state.json")

# ─── Configuration ──────────────────────────────────────────────────────────

SCAN_SECONDS = 120               # 2-minute scan — Polymarket is fast
INITIAL_BANKROLL = 250.0         # Live starting capital
PAPER_BANKROLL = 250.0           # Paper mode mirror

MAX_DISTANCE_PCT = 4.0           # Strike within 4% of current price
MIN_EDGE = 0.03                  # Lower bar — more trades, volume validates
MAX_YES_PRICE = 0.85             # Max contract price to buy
MAX_POSITIONS = 3                # Concurrent open positions
MAX_BET_PCT = 0.25               # 25% of bankroll per bet (aggressive)
KELLY_MULTIPLIER = 1.5           # Amplify Kelly for small accounts

# BTC signal thresholds (permissive — we want entries)
RSI_OVERSOLD = 38
RSI_OVERBOUGHT = 62
VOL_RANGE_MULT = 1.8             # 1.8x prior range = volume expansion


# ─── API Helpers ────────────────────────────────────────────────────────────

def _get(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-fdc/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def _parse(val):
    if isinstance(val, str):
        try: return json.loads(val)
        except: return val
    return val


# ─── Market Discovery — ALL BTC contracts ────────────────────────────────────

def discover_btc_contracts() -> list[dict]:
    """Find ALL active BTC above/below contracts from Polymarket.
    Searches multiple date ranges and contract types."""
    contracts = []
    today = datetime.now()
    month = today.strftime("%B")
    day = today.day

    queries = [
        f"Bitcoin above on {month} {day}",
        f"Bitcoin above on {month} {day+1}",
        f"Bitcoin above on {month} {day+2}",
        "bitcoin above",       # catch any time-specific contracts
    ]

    seen_ids = set()
    for query in queries:
        try:
            q = urllib.parse.quote(query)
            data = _get(f"{GAMMA}/public-search?q={q}")
            for evt in data.get("events", []):
                for m in evt.get("markets", []):
                    cid = m.get("conditionId", "")
                    if cid in seen_ids or m.get("closed", False):
                        continue
                    seen_ids.add(cid)

                    question = m.get("question", "")
                    prices = _parse(m.get("outcomePrices", []))
                    if not isinstance(prices, list) or len(prices) < 2:
                        continue

                    contracts.append({
                        "question": question,
                        "conditionId": cid,
                        "yes_price": float(prices[0]),
                        "no_price": float(prices[1]),
                        "volume": float(m.get("volume", 0)),
                        "slug": evt.get("slug", ""),
                        "end_date": m.get("endDate", ""),
                    })
        except Exception:
            continue

    return contracts


def extract_strike(question: str) -> float | None:
    m = re.search(r'\$([\d,]+)', question)
    return float(m.group(1).replace(",", "")) if m else None

def extract_time(question: str) -> str | None:
    """Extract time from contract question, e.g. '8PM ET', '2PM ET'."""
    m = re.search(r'(\d{1,2})(AM|PM)\s*(ET|UTC)', question, re.IGNORECASE)
    if m:
        return f"{m.group(1)}{m.group(2).upper()} {m.group(3).upper()}"
    return None


# ─── BTC 5-minute Signal Stack ──────────────────────────────────────────────

def btc_signal(prices: list[float]) -> dict:
    """
    RSI(7) + MACD(6/13) + range expansion on 5-min candles.
    Returns direction, confidence, RSI.
    """
    if len(prices) < 14:
        return {"direction": "neutral", "confidence": 0, "rsi": 0, "price": 0}

    current = prices[-1]

    # RSI(7)
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = sum(max(d, 0) for d in deltas[-7:]) / 7
    losses = sum(max(-d, 0) for d in deltas[-7:]) / 7
    rs = gains / max(losses, 1e-9)
    rsi = 100 - (100 / (1 + rs))

    # MACD(6/13)
    def smooth(vals, span):
        a = 2/(span+1); r = vals[0]
        for v in vals[1:]: r = a*v+(1-a)*r
        return r
    macd = smooth(prices, 6) - smooth(prices, 13)

    # Momentum (last 3 candles)
    up = sum(1 for i in range(1, min(4, len(prices))) if prices[-i] > prices[-i-1])

    # Range expansion (volume proxy on yfinance 5m data)
    r5 = max(prices[-5:]) - min(prices[-5:])
    r10 = max(prices[-10:-5]) - min(prices[-10:-5]) if len(prices) >= 10 else r5
    expanding = r5 > r10 * VOL_RANGE_MULT

    direction = "neutral"
    confidence = 0.0

    if rsi < RSI_OVERSOLD:
        direction = "up"
        confidence = min(0.80, (RSI_OVERSOLD - rsi) / 20)
        if up >= 2: confidence += 0.10
        if expanding: confidence += 0.10
    elif rsi > RSI_OVERBOUGHT:
        direction = "down"
        confidence = min(0.80, (rsi - RSI_OVERBOUGHT) / 20)
        if up < 2: confidence += 0.10
        if expanding: confidence += 0.10
    elif expanding:
        direction = "up" if up >= 2 else "down"
        confidence = 0.25

    confidence = min(0.90, confidence)

    return {
        "direction": direction,
        "confidence": round(confidence, 3),
        "rsi": round(rsi, 1),
        "macd": round(macd, 2),
        "momentum": up,
        "expanding": expanding,
        "price": current,
    }


def fetch_btc_5min() -> list[float]:
    try:
        h = yf.Ticker("BTC-USD").history(period="1d", interval="5m")
        if len(h) < 14:
            return []
        return h['Close'].tolist()[-60:]
    except:
        return []


# ─── Trade Decision ─────────────────────────────────────────────────────────

def evaluate_entries(signal: dict, contracts: list[dict],
                     state: dict) -> list[dict]:
    """Find contracts where signal direction aligns with strike proximity."""
    direction = signal["direction"]
    conf = signal["confidence"]
    btc = signal["price"]

    if direction == "neutral" or conf < 0.20:
        return []

    # Filter: strike within MAX_DISTANCE_PCT%, active contract
    candidates = []
    for c in contracts:
        strike = extract_strike(c["question"])
        if strike is None:
            continue

        dist = (strike - btc) / btc * 100

        if direction == "up":
            if strike > btc and dist < MAX_DISTANCE_PCT and c["yes_price"] < MAX_YES_PRICE:
                candidates.append({"contract": c, "strike": strike, "distance": dist,
                                   "side": "YES", "price": c["yes_price"]})
        else:  # down
            if strike < btc and abs(dist) < MAX_DISTANCE_PCT and c["no_price"] < MAX_YES_PRICE:
                candidates.append({"contract": c, "strike": strike, "distance": abs(dist),
                                   "side": "NO", "price": c["no_price"]})

    if not candidates:
        return []

    # Pick the best one (largest edge)
    bankroll = state.get("bankroll", PAPER_BANKROLL)
    positions = state.get("positions", {})

    entries = []
    for cand in sorted(candidates, key=lambda x: conf - x["price"], reverse=True):
        edge = conf - cand["price"]
        if edge < MIN_EDGE:
            continue

        key = f"{cand['contract']['conditionId'][:16]}_{cand['side']}"
        if key in positions:
            continue
        if len(positions) + len(entries) >= MAX_POSITIONS:
            break

        # Kelly sizing: f* = edge / (1 - price) × KELLY_MULTIPLIER
        kelly = (edge / max(0.01, 1 - cand["price"])) * KELLY_MULTIPLIER
        kelly = min(0.4, max(0.05, kelly))  # clamp 5-40%
        bet = round(bankroll * kelly, 2)

        entries.append({
            "action": "BUY_" + cand["side"],
            "question": cand["contract"]["question"],
            "conditionId": cand["contract"]["conditionId"],
            "strike": cand["strike"],
            "contract_price": cand["price"],
            "bet": bet,
            "kelly_pct": round(kelly * 100, 1),
            "edge": round(edge, 3),
            "btc_at_entry": round(btc, 2),
            "distance_pct": round(cand["distance"], 2),
            "signal_conf": conf,
            "signal_rsi": signal["rsi"],
            "entry_time": datetime.now().isoformat(),
            "side": cand["side"],
        })

    return entries


# ─── Settlement Check ───────────────────────────────────────────────────────

def check_settlements(state: dict, btc_price: float) -> list[dict]:
    """Settle positions where strike has been crossed."""
    positions = state.get("positions", {})
    settled = []

    for key, pos in list(positions.items()):
        strike = pos["strike"]
        side = pos["side"]
        crossed = (side == "YES" and btc_price >= strike) or \
                  (side == "NO" and btc_price <= strike)

        if crossed:
            bet = pos["bet"]
            if side == "YES":
                payout = bet / pos["contract_price"]
                profit = payout - bet
            else:
                payout = bet / pos["contract_price"]
                profit = payout - bet

            settled.append({**pos, "pnl": round(profit, 2),
                           "btc_settle": round(btc_price, 2),
                           "settle_time": datetime.now().isoformat()})
            del positions[key]

    return settled


# ─── Journal / Reporting ───────────────────────────────────────────────────

def summary(state: dict, entries: list, settled: list) -> str:
    bankroll = state.get("bankroll", PAPER_BANKROLL)
    positions = state.get("positions", {})
    pnl = state.get("total_pnl", 0)
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    trades = wins + losses

    lines = [
        "",
        "🎲 POLYMARKET ENGINE",
        f"   Bankroll: ${bankroll:,.2f} | P&L: ${pnl:+,.2f} | Trades: {trades}",
        f"   Wins: {wins} | Losses: {losses} | "
        f"Rate: {wins/max(1,trades)*100:.0f}%",
    ]

    if settled:
        for s in settled[-5:]:
            e = "🟢" if s["pnl"] > 0 else "🔴"
            lines.append(f"   {e} {s['action']} {s['question']} — ${s['pnl']:+,.2f}")

    if entries:
        for e in entries:
            lines.append(f"   ⚡ NEW: {e['action']} {e['question'][:60]} — "
                        f"${e['bet']} @ {e['contract_price']:.3f} "
                        f"(edge={e['edge']:.3f}, RSI={e['signal_rsi']})")

    if positions:
        for k, p in positions.items():
            price = p.get("contract_price", 0)
            payout = round(p["bet"] / max(price, 0.01), 2) if price > 0 else 0
            lines.append(f"   📌 {p['side']} {p['question'][:50]} — "
                        f"${p['bet']} → ${payout} if right")

    if not positions and not entries and not settled:
        lines.append("   Idle — waiting for signal + strike proximity.")

    return "\n".join(lines)


# ─── Main Loop ──────────────────────────────────────────────────────────────

def load_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"bankroll": PAPER_BANKROLL, "total_pnl": 0, "wins": 0, "losses": 0,
            "positions": {}, "journal": [], "scans": 0}

def save_state(state):
    state["scans"] += 1
    STATE.write_text(json.dumps(state, indent=2, default=str))


def run_once(state):
    btc_prices = fetch_btc_5min()
    if not btc_prices:
        return [], [], None

    sig = btc_signal(btc_prices)
    contracts = discover_btc_contracts()
    btc_price = sig["price"]

    # Settle crossed contracts
    settled = check_settlements(state, btc_price)
    for s in settled:
        state["total_pnl"] += s["pnl"]
        state["bankroll"] += s["pnl"]
        if s["pnl"] > 0:
            state["wins"] = state.get("wins", 0) + 1
        else:
            state["losses"] = state.get("losses", 0) + 1
        state.setdefault("journal", []).append({
            "ts": datetime.now().isoformat(),
            "type": "settle",
            "pnl": s["pnl"],
            "question": s.get("question", ""),
        })

    # New entries
    entries = evaluate_entries(sig, contracts, state)
    for e in entries:
        key = f"{e['conditionId'][:16]}_{e['side']}"
        state["positions"][key] = e

    save_state(state)

    # Print on screen
    print(summary(state, entries, settled))
    return entries, settled, sig


def run_continuous():
    state = load_state()
    print(f"🎲 FDC POLYMARKET — {SCAN_SECONDS}s scan | Bankroll: ${state['bankroll']:,.2f}")
    print("   Ctrl+C to stop\n")

    last_summary = 0
    while True:
        try:
            entries, settled, sig = run_once(state)
            now = time.time()
            # Print full summary every 10 iterations (20 min)
            if entries or settled or now - last_summary > 600:
                last_summary = now
            time.sleep(SCAN_SECONDS)
        except KeyboardInterrupt:
            print(f"\n👋 Stopped. Bankroll: ${state['bankroll']:,.2f} | "
                  f"P&L: ${state.get('total_pnl',0):+,.2f}")
            break
        except Exception as e:
            print(f"❌ {e}", file=sys.stderr)
            time.sleep(30)


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--once" in sys.argv:
        state = load_state()
        e, s, sig = run_once(state)
        print(f"\nSignal: {sig['direction']} @ {sig['confidence']:.2f} "
              f"(RSI={sig['rsi']}, BTC=${sig['price']:,.2f})")
    elif "--discover" in sys.argv:
        cs = discover_btc_contracts()
        print(f"{len(cs)} active BTC contracts:")
        for c in sorted(cs, key=lambda x: x["volume"], reverse=True)[:10]:
            print(f"  {c['question']} — YES {c['yes_price']*100:.0f}% | "
                  f"NO {c['no_price']*100:.0f}% | ${c['volume']:,.0f} vol")
    elif "--reset" in sys.argv:
        STATE.unlink(missing_ok=True)
        print("State reset.")
    elif "--continuous" in sys.argv or "-c" in sys.argv:
        run_continuous()
    else:
        print(__doc__)
