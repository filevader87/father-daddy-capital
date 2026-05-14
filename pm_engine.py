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
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

# ─── Neural & Bayesian Import ────────────────────────────────────────────
REPO = Path(__file__).parent
sys.path.insert(0, str(REPO / "src" / "neural"))
try:
    import plastic_network as pn
    _NEURAL_AVAILABLE = True
except ImportError:
    _NEURAL_AVAILABLE = False

try:
    import bayesian_layer as bl
    import feature_encoder as fe
    _BAYESIAN_AVAILABLE = True
except ImportError:
    _BAYESIAN_AVAILABLE = False

GAMMA  = "https://gamma-api.polymarket.com"
CLOB   = "https://clob.polymarket.com"
OUTPUT = Path("/mnt/c/Users/12035/father_daddy_capital/output")
STATE  = Path("/mnt/c/Users/12035/father_daddy_capital/output/pm_state.json")

# ─── Configuration ──────────────────────────────────────────────────────────

SCAN_SECONDS = 120               # 2-minute scan
INITIAL_BANKROLL = 250.0         # Live starting capital
PAPER_BANKROLL = 250.0           # Paper mode mirror

MAX_DISTANCE_PCT = 4.0           # Strike within 4% of current price
MIN_EDGE = 0.02                  # Edge threshold (lowered for more entries)
MAX_YES_PRICE = 0.85             # Max contract price to buy
MIN_CONTRACT_PRICE = 0.05        # Filter extreme longshots
MAX_POSITIONS = 3                # Concurrent open positions
KELLY_MULTIPLIER = 1.5           # Amplify Kelly for small accounts

# Signal thresholds — tuned for 2-3 trades/day
RSI_OVERSOLD = 48                # Was 38 — too sterile
RSI_OVERBOUGHT = 52              # Was 62 — same problem
MIN_VOLUME_USD = 10000           # Minimum volume for any contract

# Neural plasticity blending
NEURAL_BLEND_MAX = 0.30        # Max weight for neural score in blended signal
NEURAL_BLEND_UPDATES = 200     # Updates to reach full blend (0→NEURAL_BLEND_MAX)
NEURAL_CONSOLIDATE_EVERY = 50  # EWC consolidation frequency

# Neural engine instance (initialized lazily)
_neural_engine = None
_bayesian_engine = None
_feature_encoder = None


def pm_encode_signal(signal: dict) -> np.ndarray:
    """Convert PM signal dict to neural input vector [8,]."""
    direction = signal.get("direction", "neutral")
    conf = signal.get("confidence", 0.0)
    rsi = signal.get("rsi", 50.0)
    macd = signal.get("macd", 0.0)
    mom = signal.get("momentum", 2)

    # Map direction to signals
    if direction == "up":
        trend_sig = 0.5 + conf * 0.3
        mom_sig = min(1.0, mom / 3.0)
        mean_rev = max(0.0, (50 - rsi) / 25)
    elif direction == "down":
        trend_sig = -0.5 - conf * 0.3
        mom_sig = -min(1.0, (3 - mom) / 3.0)
        mean_rev = -max(0.0, (rsi - 50) / 25)
    else:
        trend_sig = 0.0
        mom_sig = 0.0
        mean_rev = 0.0

    # Normalize RSI to roughly [-1, 1]
    rsi_norm = (rsi - 50) / 25

    # MACD normalized
    macd_norm = float(np.clip(macd / 500, -1.0, 1.0))

    # Volatility proxy from RSI extremes
    vol = abs(rsi - 50) / 25

    return np.array([
        float(np.clip(rsi_norm, -1.0, 1.0)),
        float(np.clip(macd_norm, -1.0, 1.0)),
        float(np.clip(trend_sig, -1.0, 1.0)),
        float(np.clip(mom_sig, -1.0, 1.0)),
        float(np.clip(mean_rev, -1.0, 1.0)),
        float(np.clip(vol, 0.0, 1.0)),
        0.0,  # asset_class: BTC=crypto, but PM is all crypto so neutral
        float(np.clip(conf, 0.0, 1.0)),
    ], dtype=float)


def scale_pm_pnl(actual_pnl_pct: float) -> float:
    """Scale P&L % to [-1, 1] target. Binary = full 100% or -100% payoff."""
    return float(np.clip(actual_pnl_pct / 1.25, -1.0, 1.0))


def _get_neural() -> "pn.NeuralPlasticityEngine | None":
    """Lazy-init the neural engine. Returns None if unavailable."""
    global _neural_engine
    if not _NEURAL_AVAILABLE:
        return None
    if _neural_engine is None:
        _neural_engine = pn.NeuralPlasticityEngine()
    return _neural_engine


def _get_bayesian() -> "bl.BayesianCalibrator | None":
    """Lazy-init the Bayesian calibration layer."""
    global _bayesian_engine
    if not _BAYESIAN_AVAILABLE:
        return None
    if _bayesian_engine is None:
        _bayesian_engine = bl.BayesianCalibrator()
    return _bayesian_engine


def _get_feature_encoder() -> "fe.FeatureEncoder":
    """Lazy-init the feature encoder, wired to Bayesian calibrator."""
    global _feature_encoder
    if _feature_encoder is None:
        _feature_encoder = fe.FeatureEncoder(calibrator=_get_bayesian())
    return _feature_encoder


def _neural_blend_weight() -> float:
    """Blend weight: 0→NEURAL_BLEND_MAX over NEURAL_BLEND_UPDATES updates."""
    neural = _get_neural()
    if neural is None:
        return 0.0
    updates = neural.network.updates
    return NEURAL_BLEND_MAX * min(1.0, updates / NEURAL_BLEND_UPDATES)


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


# ─── Market Discovery — ALL crypto contracts ────────────────────────────────

CRYPTO_QUERIES = [
    "bitcoin above", "ethereum above", "solana above",
    "bitcoin below", "ethereum below", "solana below",
    "btc", "eth", "sol",
]

def discover_contracts() -> list[dict]:
    """Find ALL active crypto above/below contracts from Polymarket.
    Cross-searches multiple asset names to catch everything."""
    contracts = []
    today = datetime.now()
    month = today.strftime("%B")
    day = today.day

    # Date-specific queries for the next 3 days per asset
    assets = ["Bitcoin", "Ethereum", "Solana"]
    queries = CRYPTO_QUERIES.copy()
    for asset in assets:
        for offset in [0, 1, 2, 3]:
            queries.append(f"{asset} above on {month} {day+offset}")
            queries.append(f"{asset} below on {month} {day+offset}")

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
                    volume = float(m.get("volume", 0))
                    if volume < MIN_VOLUME_USD:
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
                        "volume": volume,
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

    direction = "neutral"
    confidence = 0.0

    if rsi < RSI_OVERSOLD:
        direction = "up"
        confidence = min(0.80, (RSI_OVERSOLD - rsi) / 15)
        if up >= 2: confidence += 0.10
    elif rsi > RSI_OVERBOUGHT:
        direction = "down"
        confidence = min(0.80, (rsi - RSI_OVERBOUGHT) / 15)
        if up < 2: confidence += 0.10
    else:
        # RSI between 48-52 — trade the momentum direction
        direction = "up" if up >= 2 else "down"
        confidence = 0.20

    confidence = min(0.90, confidence)

    return {
        "direction": direction,
        "confidence": round(confidence, 3),
        "rsi": round(rsi, 1),
        "macd": round(macd, 2),
        "momentum": up,
        "price": current,
        "_prices": prices,   # Full price array for feature encoder
    }


def fetch_btc_5min() -> list[float]:
    try:
        import yfinance as yf
        h = yf.Ticker("BTC-USD").history(period="1d", interval="5m")
        if len(h) < 14:
            return []
        return h['Close'].tolist()[-60:]
    except:
        return []


# ─── Trade Decision ─────────────────────────────────────────────────────────

def evaluate_entries(signal: dict, contracts: list[dict],
                     state: dict) -> list[dict]:
    """Find contracts where signal direction aligns with strike proximity.
    Blends neural plasticity score with traditional confidence."""
    direction = signal["direction"]
    conf = signal["confidence"]
    btc = signal["price"]

    if direction == "neutral" or conf < 0.20:
        return [], None

    # ── Neural plasticity blend ──────────────────────────────────────────
    neural_pred = None
    signal_vector = None
    blend_w = _neural_blend_weight()
    neural = _get_neural()
    if neural is not None and blend_w > 0.0:
        signal_vector = pm_encode_signal(signal)
        neural_pred = neural.network.predict(signal_vector)
        # neural_pred ∈ [-1,1]: +1 = expect BTC up, -1 = expect down
        # Map to confidence: for BUY_YES (direction=up), +neural is good
        #                    for BUY_NO (direction=down), -neural is good
        neural_conf = (neural_pred + 1.0) / 2.0 if direction == "up" else (1.0 - neural_pred) / 2.0
        neural_conf = max(0.0, min(1.0, neural_conf))
        conf = conf * (1.0 - blend_w) + neural_conf * blend_w
        conf = round(min(0.95, conf), 3)

    # Filter: strike within MAX_DISTANCE_PCT%, active contract
    candidates = []
    for c in contracts:
        strike = extract_strike(c["question"])
        if strike is None:
            continue

        dist = (strike - btc) / btc * 100

        if direction == "up":
            if strike > btc and dist < MAX_DISTANCE_PCT and MIN_CONTRACT_PRICE < c["yes_price"] < MAX_YES_PRICE:
                candidates.append({"contract": c, "strike": strike, "distance": dist,
                                   "side": "YES", "price": c["yes_price"]})
        else:  # down
            if strike < btc and abs(dist) < MAX_DISTANCE_PCT and MIN_CONTRACT_PRICE < c["no_price"] < MAX_YES_PRICE:
                candidates.append({"contract": c, "strike": strike, "distance": abs(dist),
                                   "side": "NO", "price": c["no_price"]})

    if not candidates:
        return [], neural_pred

    # Pick the best one (largest edge)
    bankroll = state.get("bankroll", PAPER_BANKROLL)
    # Account for already-invested capital
    positions = state.get("positions", {})
    invested = sum(p.get("bet", 0) for p in positions.values())
    available = max(0.0, bankroll - invested)

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
        if available < 5.0:  # Min bet $5
            break

        # ── Bayesian-calibrated Kelly sizing ────────────────────────────
        cal = _get_bayesian()
        encoder = _get_feature_encoder()

        # Compute feature vector for this specific contract
        hours_to_res = 24.0  # default: unknown → use conservative
        if cand.get("contract", {}).get("end_date"):
            try:
                end_dt = datetime.fromisoformat(
                    str(cand["contract"]["end_date"]).replace("Z", "+00:00"))
                hours_to_res = max(0.0, (end_dt - datetime.now(end_dt.tzinfo)).total_seconds() / 3600)
            except (ValueError, TypeError):
                pass

        features = encoder.encode(
            btc_prices_5m=signal.get("_prices", []),
            contract_yes_price=cand["price"] if cand["side"] == "YES" else 1 - cand["price"],
            contract_no_price=1 - cand["price"] if cand["side"] == "YES" else cand["price"],
            contract_volume=cand["contract"].get("volume", 10000),
            hours_to_resolution=hours_to_res,
        )

        # Get calibrated probability
        cal_result = cal.predict(features) if cal else None
        cal_factor = cal.calibration_factor if cal else 0.5
        certainty = cal_result.get("certainty", 0.5) if cal_result else 0.5

        # Use the calibrated edge if available, otherwise fall back
        if cal_result:
            cal_prob = cal_result["probability"]
            # Edge: our calibrated prob vs market price
            calibrated_edge = cal_prob - cand["price"] if cand["side"] == "YES" else (1 - cal_prob) - cand["price"]
            if calibrated_edge > edge:
                edge = calibrated_edge

        bet = fe.kelly_sizer(
            edge=edge,
            odds=1 - cand["price"],
            bankroll=bankroll,
            calibration_factor=cal_factor,
            certainty=certainty,
            max_bankroll_fraction=0.02,
            min_bet=5.0,
        )

        # Store signal vector for neural learning on settlement
        sv = signal_vector.tolist() if signal_vector is not None else None

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
            # Bayesian fields for later learning
            "bayesian_features": features.tolist() if 'features' in dir() else None,
            "cal_prob": round(cal_result["probability"], 4) if cal_result else None,
            "cal_ci_low": round(cal_result["probability_ci_low"], 4) if cal_result else None,
            "cal_ci_high": round(cal_result["probability_ci_high"], 4) if cal_result else None,
            "cal_certainty": round(certainty, 4),
            # Neural fields for later learning
            "signal_vector": sv,
            "neural_pred": round(neural_pred, 4) if neural_pred is not None else None,
        })
        available -= bet

    return entries, neural_pred


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

    # ── Neural plasticity status ──────────────────────────────────────────
    neural = _get_neural()
    if neural is not None:
        stats = neural.stats()
        bw = _neural_blend_weight()
        lines.append(f"   🧠 Neural: {stats['updates']} updates | "
                     f"LR={stats['learning_rate']:.5f} | "
                     f"Accuracy={stats['rolling_accuracy']:.0%} | "
                     f"Blend={bw:.0%}")

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
    contracts = discover_contracts()
    btc_price = sig["price"]

    # Settle crossed contracts
    settled = check_settlements(state, btc_price)
    for s in settled:
        pnl = s["pnl"]
        state["total_pnl"] += pnl
        state["bankroll"] += pnl
        if pnl > 0:
            state["wins"] = state.get("wins", 0) + 1
        else:
            state["losses"] = state.get("losses", 0) + 1
        state.setdefault("journal", []).append({
            "ts": datetime.now().isoformat(),
            "type": "settle",
            "pnl": pnl,
            "question": s.get("question", ""),
        })

        # ── Bayesian learning from settled trade ────────────────────────
        cal = _get_bayesian()
        if cal is not None:
            sv_bayes = s.get("bayesian_features")
            if sv_bayes is not None:
                outcome = 1 if pnl > 0 else 0  # YES wins = 1
                cal.update(np.array(sv_bayes, dtype=float), outcome)

        # ── Neural learning from settled trade ────────────────────────────
        neural = _get_neural()
        sv = s.get("signal_vector")
        n_pred = s.get("neural_pred")
        if neural is not None and sv is not None and n_pred is not None:
            bet = s.get("bet", 1.0)
            pnl_pct = pnl / max(bet, 0.01)  # Binary: +X% win or -100% loss
            target = scale_pm_pnl(pnl_pct)
            sv_arr = np.array(sv, dtype=float)
            neural.network.learn_from_trade(sv_arr, n_pred, target)
            neural.network.add_to_replay(sv_arr, target)
            # Periodic replay + consolidation
            if neural.network.updates % 5 == 0:
                neural.network.replay()
            if neural.network.updates > 0 and neural.network.updates % NEURAL_CONSOLIDATE_EVERY == 0:
                neural.network.consolidate()
            neural.network.save()
            neural.performance.save()

    # New entries
    entries, neural_pred = evaluate_entries(sig, contracts, state)
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
        if sig is not None:
            print(f"\nSignal: {sig['direction']} @ {sig['confidence']:.2f} "
                  f"(RSI={sig['rsi']}, BTC=${sig['price']:,.2f})")
            neural = _get_neural()
            if neural is not None:
                stats = neural.stats()
                print(f"🧠 Neural: {stats['updates']} updates | "
                      f"Accuracy={stats['rolling_accuracy']:.0%} | "
                      f"Blend={_neural_blend_weight():.0%}")
        else:
            print("\n⚠ No BTC price data available.")
    elif "--discover" in sys.argv:
        cs = discover_contracts()
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
