#!/usr/bin/env python3
"""
Father Daddy Capital — Polymarket Paper Trading Module
Daily EOD BTC above/below binary contracts. Edge: 5-min signal enters before
price crosses the strike boundary. All trades simulated, no real USDC.

v2: Integrated adversarial debate, scaled entries, orderbook analysis,
    smart money filter, Bayesian calibration, and 3-perspective risk sizing.
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

GAMMA  = "https://gamma-api.polymarket.com"
CLOB   = "https://clob.polymarket.com"
OUTPUT = Path("/mnt/c/Users/12035/father_daddy_capital/output")

# ─── Dependency imports (graceful fallback if missing) ──
try:
    from fdc_debate import debate, debate_summary, DebateConfig
    HAS_DEBATE = True
except ImportError:
    HAS_DEBATE = False

try:
    from fdc_pm_scaled_entries import evaluate_scaled_entries, ScaledEntryConfig
    HAS_SCALED_ENTRIES = True
except ImportError:
    HAS_SCALED_ENTRIES = False

try:
    from fdc_orderbook import analyze_orderbook, simulate_fill, get_trade_params
    HAS_ORDERBOOK = True
except ImportError:
    HAS_ORDERBOOK = False

try:
    from fdc_smart_money_api import run_smart_money_cycle, smart_money_summary
    HAS_SMART_MONEY = True
except ImportError:
    HAS_SMART_MONEY = False

try:
    from fdc_calibrate import run_calibration
    HAS_CALIBRATION = True
except ImportError:
    HAS_CALIBRATION = False


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
    today = datetime.now()
    month_name = today.strftime("%B")
    day = today.day

    for day_offset in [0, 1, 2, 3]:
        target_day = day + day_offset
        query = f"Bitcoin above on {month_name} {target_day}"
        q = urllib.parse.quote(query)
        url = f"{GAMMA}/public-search?q={q}"
        data = _get(url)

        for evt in data.get("events", []):
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

    # ── SMA20 ──
    sma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else sum(prices) / len(prices)
    price = prices[-1]

    # ── Aggregate ──
    signals = {"rsi": round(rsi, 1), "macd": round(macd, 2), "vol_expanding": vol_expanding,
               "momentum_3_candles": momentum_up, "sma20": round(sma20, 2), "price": price}

    direction = "neutral"
    confidence = 0.0

    # Oversold + volume spike + momentum turning = strong UP signal
    if rsi < 35 and vol_expanding and momentum_up:
        direction = "up"
        confidence = min(0.80, (40 - rsi) / 20)
    # Overbought + volume spike + momentum fading = DOWN signal
    elif rsi > 65 and vol_expanding and not momentum_up:
        direction = "down"
        confidence = min(0.80, (rsi - 60) / 20)
    # Strong trend detection: price above/below SMA
    elif price > sma20 and momentum_up and rsi < 55:
        direction = "up"
        confidence = min(0.65, (sma20 / price - 1) * -50 + 0.35)
    elif price < sma20 and not momentum_up and rsi > 45:
        direction = "down"
        confidence = min(0.65, (price / sma20 - 1) * -50 + 0.35)
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

    # ── Adaptation: Check recent P&L and loss streak ──
    trade_journal = portfolio.get("trade_journal", [])
    recent_pm = [t for t in trade_journal if t.get("type") == "polymarket"][-20:]
    recent_losses = sum(1 for t in recent_pm if t.get("pnl", 0) < 0)
    recent_wins = sum(1 for t in recent_pm if t.get("pnl", 0) > 0)
    loss_streak = 0
    for t in reversed(recent_pm):
        if t.get("pnl", 0) < 0:
            loss_streak += 1
        else:
            break
    consecutive_same_dir_losses = 0
    last_dir = None
    for t in reversed(recent_pm):
        t_dir = "up" if "BUY_YES" in t.get("action", "") else "down" if "BUY_NO" in t.get("action", "") else None
        if t_dir and t.get("pnl", 0) < 0:
            if last_dir is None:
                last_dir = t_dir
            if t_dir == last_dir:
                consecutive_same_dir_losses += 1
            else:
                break
        else:
            break

    # Kill switch: pause PM after 5+ consecutive losses
    if loss_streak >= 5:
        return trades

    # Reduce bet size based on recent performance
    pnl_history = [t.get("pnl", 0) for t in recent_pm]
    recent_pnl = sum(pnl_history)
    max_bet = 50.0
    if recent_pnl < -200:
        max_bet = 15.0
    elif recent_pnl < -100:
        max_bet = 25.0
    elif recent_pnl < -50:
        max_bet = 35.0

    # ── Neural calibration factor ──
    cal_factor = 0.5  # default: no calibration data
    if HAS_CALIBRATION:
        try:
            cal_report = run_calibration()
            composite = cal_report.get("composite_score", 0.5)
            # Map calibration [0,1] → confidence multiplier [0.3, 1.0]
            cal_factor = 0.3 + 0.7 * composite
        except Exception:
            pass

    # Direction boost: if same direction lost 3+ times consecutively, flip bias
    direction_override = None
    if consecutive_same_dir_losses >= 3 and direction == "up":
        direction_override = "down"
    elif consecutive_same_dir_losses >= 3 and direction == "down":
        direction_override = "up"

    effective_direction = direction_override or direction

    # Dedup: track ALL strikes that have been entered in recent journal
    recent_strikes = set()
    for t in recent_pm[-10:]:
        key = t.get("key", "")
        if key:
            recent_strikes.add(key)

    # ── Build candidate contracts for debate ──
    candidates = []

    if effective_direction == "up" and nearest_above:
        na = nearest_above
        distance_pct = (na["strike"] - btc_price) / btc_price * 100
        yes_price = na["yes_price"]

        if distance_pct < 3.0 and yes_price < 0.80:
            edge = confidence - yes_price
            if edge > 0.05:
                candidates.append({
                    "direction": "up", "strike": na["strike"],
                    "market": na["market"], "yes_price": yes_price,
                    "no_price": na["no_price"], "edge": edge,
                    "distance_pct": distance_pct, "confidence": confidence,
                })

    if effective_direction == "down" and nearest_below:
        nb = nearest_below
        distance_pct = (btc_price - nb["strike"]) / btc_price * 100
        no_price = nb["no_price"]

        if distance_pct < 1.0 and no_price < 0.75:
            edge = confidence - no_price
            if edge > 0.05:
                candidates.append({
                    "direction": "down", "strike": nb["strike"],
                    "market": nb["market"], "yes_price": nb["yes_price"],
                    "no_price": no_price, "edge": edge,
                    "distance_pct": distance_pct, "confidence": confidence,
                })

    # ── Apply FDC feature stack to each candidate ──
    for cand in candidates:
        is_up = cand["direction"] == "up"
        mkt = cand["market"]

        # ── 1. Adversarial Debate ──
        debate_verdict = "ENTER"
        debate_net = 0.0
        debate_conf_mod = 0.0
        debate_reasons_bull = []
        debate_reasons_bear = []

        if HAS_DEBATE:
            try:
                sig_for_debate = {
                    "direction": cand["direction"],
                    "confidence": cand["confidence"],
                    "rsi": signal["signals"].get("rsi", 50),
                    "macd": signal["signals"].get("macd", 0),
                    "momentum": signal["signals"].get("momentum_3_candles", False),
                    "price": btc_price,
                    "prices": signal["signals"].get("sma20", btc_price),
                }
                contract_for_debate = {
                    "up_price": cand["yes_price"] if is_up else 1 - cand["no_price"],
                    "down_price": cand["no_price"] if not is_up else 1 - cand["yes_price"],
                    "mins_to_expiry": 120,  # approximate for daily contracts
                    "volume": mkt.get("volume", 0),
                }
                dr = debate(sig_for_debate, contract_for_debate)
                debate_verdict = dr.verdict
                debate_net = dr.net_score
                debate_conf_mod = dr.confidence_modifier
                debate_reasons_bull = dr.bull_reasons
                debate_reasons_bear = dr.bear_reasons
            except Exception:
                pass  # Degrade gracefully

        # Skip if debate says SKIP
        if debate_verdict == "SKIP":
            continue

        # ── 2. Apply debate confidence modifier + calibration ──
        adjusted_confidence = cand["confidence"] + debate_conf_mod
        adjusted_confidence *= cal_factor  # Scale by calibration
        adjusted_confidence = max(0.1, min(0.95, adjusted_confidence))

        # ── 3. Recalculate edge with adjusted confidence ──
        entry_price = cand["yes_price"] if is_up else cand["no_price"]
        adjusted_edge = adjusted_confidence - entry_price
        if adjusted_edge < 0.03:  # Higher bar after debate + calibration
            continue

        # ── 4. 3-Perspective Risk Sizing ──
        # Perspective 1: Kelly
        kelly_fraction = adjusted_edge / (1 - entry_price)
        # Perspective 2: Loss-streak adjusted (reduce on drawdown)
        streak_factor = max(0.2, 1.0 - loss_streak * 0.15)
        # Perspective 3: Debate conviction (ENTER = 1.0, REDUCE = 0.5)
        conviction_factor = 1.0 if debate_verdict == "ENTER" else 0.5

        bet_size = min(max_bet, max(5.0, kelly_fraction * 200 * streak_factor * conviction_factor))

        # ── 5. Scaled Entries (if available) ──
        tiers = None
        if HAS_SCALED_ENTRIES:
            try:
                sec = ScaledEntryConfig(
                    max_entries_per_contract=3,
                    base_entry_size=bet_size / 3,
                    size_multiplier=1.0,
                    max_position_usd=max_bet * 2,
                    min_contract_price=0.05,
                    max_contract_price=0.95,
                    force_entry=False,
                    min_edge_override=max(0.03, adjusted_edge),
                    preferred_assets=["BTC"],
                    allow_fallback=True,
                    max_concurrent_positions=5,
                    min_time_between_entries=15,
                )
                tiers = evaluate_scaled_entries(
                    signal={**signal, "confidence": adjusted_confidence, "edge": adjusted_edge},
                    contracts=[cand],
                    state=portfolio,
                    config=sec,
                )
            except Exception:
                tiers = None

        # ── 6. Orderbook analysis (if available) ──
        ob_quality = None
        if HAS_ORDERBOOK:
            try:
                # Simulated orderbook: use market prices as depth proxy
                # Full orderbook requires CLOB auth — degrade gracefully
                depth_yes = cand["yes_price"] * 1000  # rough volume proxy
                depth_no = cand["no_price"] * 1000
                ob_analysis = analyze_orderbook(
                    market=cand["market"].get("conditionId", ""),
                    bids={cand["yes_price"]: depth_yes},
                    asks={cand["no_price"]: depth_no},
                    target_size=bet_size,
                    is_no_token=not is_up,
                )
                ob_quality = ob_analysis.get("quality", "unknown")
                # Reduce size if orderbook is thin
                if ob_quality in ("thin", "very_thin"):
                    bet_size *= 0.5
            except Exception:
                pass

        # ── 7. Smart Money filter (if available) ──
        smart_money_signal = None
        if HAS_SMART_MONEY:
            try:
                sm_entries, sm_signals = run_smart_money_cycle(portfolio)
                # Check if smart money aligns with our direction
                for sm in sm_signals[:3]:
                    if sm.get("direction") == cand["direction"]:
                        smart_money_signal = "aligned"
                        break
                    elif sm.get("direction") != cand["direction"]:
                        smart_money_signal = "contradicts"
                        break
                if smart_money_signal == "contradicts":
                    bet_size *= 0.5  # Halve position when smart money disagrees
            except Exception:
                pass

        # ── Build final trade ──
        strike_key = f"BTC>{cand['strike']}" if is_up else f"BTC<{cand['strike']}"
        if strike_key in positions or strike_key in recent_strikes:
            continue

        action = "BUY_YES" if is_up else "BUY_NO"
        trades.append({
            "action": action,
            "market_question": mkt["question"],
            "strike": cand["strike"],
            "conditionId": mkt["conditionId"],
            "yes_price": cand["yes_price"],
            "no_price": cand["no_price"],
            "bet_size": round(bet_size, 2),
            "edge": round(adjusted_edge, 3),
            "btc_price": btc_price,
            "distance_pct": round(cand["distance_pct"], 2),
            "signal_confidence": adjusted_confidence,
            "signal_rsi": signal["signals"]["rsi"],
            "timestamp": datetime.now().isoformat(),
            "settle_date": mkt.get("settle_date", ""),
            "adaptation": f"loss_streak={loss_streak},max_bet={max_bet},dir_override={direction_override}",
            # ── FDC feature metadata ──
            "debate_verdict": debate_verdict,
            "debate_net": round(debate_net, 3),
            "debate_confidence_mod": round(debate_conf_mod, 3),
            "debate_bull": debate_reasons_bull[:3] if debate_reasons_bull else [],
            "debate_bear": debate_reasons_bear[:3] if debate_reasons_bear else [],
            "calibration_factor": round(cal_factor, 3),
            "risk_posture": "CONSERVATIVE" if recent_pnl < -50 else "NORMAL",
            "risk_regime": "trending_down" if loss_streak > 2 else "normal",
            "risk_sizes": f"R=${bet_size*2:.1f}/S=${bet_size*0.3:.1f}/N=${bet_size:.1f}",
            "ob_quality": ob_quality or "n/a",
            "smart_money": smart_money_signal or "n/a",
            "scaled_tiers": len(tiers) if tiers else 1,
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

        # Fallback: if no settle_date, derive from entry timestamp (settle after 24h)
        if settle_dt is None:
            entry_ts = pos.get("timestamp", pos.get("entry_time", ""))
            if entry_ts:
                try:
                    entry_dt = datetime.fromisoformat(entry_ts)
                    settle_dt = entry_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                except (ValueError, TypeError):
                    pass

        now = datetime.utcnow()
        # Settlement: if settle date is in the past (in UTC) or price is clearly resolved
        target_reached = False

        if pos["action"] == "BUY_YES":
            target_reached = btc_price >= pos["strike"]
        else:  # BUY_NO
            target_reached = btc_price <= pos["strike"]

        # Settle when past target date OR price crossed strike
        should_settle = False
        if settle_dt and now > settle_dt:
            should_settle = True
        elif target_reached:
            should_settle = True

        if should_settle:
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
            debate_info = ""
            if pos.get("debate_verdict"):
                debate_info = f" | debate={pos['debate_verdict']} net={pos.get('debate_net', 0):.2f}"
            cal_info = ""
            if pos.get("calibration_factor"):
                cal_info = f" | cal={pos['calibration_factor']:.2f}"
            lines.append(
                f"    {pos['action']} {pos.get('market_question', '?')[:50]} — "
                f"${pos['bet_size']} @ {pos['yes_price']:.3f} | "
                f"edge: {pos['edge']:.3f}{debate_info}{cal_info}"
            )
    elif not settled:
        lines.append("  No positions. Waiting for signal + strike proximity.")

    lines.append(f"  Total P&L: ${total_pnl:+,.2f}")
    return "\n".join(lines)


# ─── Main Cycle (called from paper_engine.py) ─────────────────────────────────

def run_polymarket_cycle(state: dict, btc_prices_5m: list[float]) -> tuple[list, list]:
    """
    Full Polymarket cycle: signal → debate → calibration → sizing → settlement.
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

    # ── 3. Evaluate new trades (with full FDC stack) ──
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

    elif "--features" in sys.argv:
        print("FDC Feature Integration Status:")
        print(f"  Adversarial Debate:  {'✅' if HAS_DEBATE else '❌'}")
        print(f"  Scaled Entries:       {'✅' if HAS_SCALED_ENTRIES else '❌'}")
        print(f"  Orderbook Analysis:   {'✅' if HAS_ORDERBOOK else '❌'}")
        print(f"  Smart Money Filter:   {'✅' if HAS_SMART_MONEY else '❌'}")
        print(f"  Bayesian Calibration: {'✅' if HAS_CALIBRATION else '❌'}")