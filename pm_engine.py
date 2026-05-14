#!/usr/bin/env python3
"""
Father Daddy Capital — Polymarket Engine v2 (Multi-Asset + Short-Duration)
===========================================================================
Overhauled based on successful bot analysis (@ohanism: 80% WR, +$31K).

Strategy — three changes from v1:
  1. SHORT-DURATION CONTRACTS: 5-min "Up or Down" windows instead of daily
     above/below. Edge decays over hours — capture it in minutes.
  2. MULTI-ASSET: BTC (50%), ETH (30%), SOL (15%), XRP (5%).
     Different vol profiles = uncorrelated losers. No single-asset bleed.
  3. VARIABLE SIZING: Conviction-based three-tier system.
     Low edge (<0.08) → probe bet (1% bankroll)
     Medium edge (0.08-0.15) → confidence bet (3%)
     High edge (>0.15) → conviction bet (5%, cap $50)

Pipeline per asset per scan:
  fetch 5m candles → RSI(7)/MACD(6/13) → direction + confidence
  → discover active "Up or Down" contracts expiring in next 10 min
  → match signal to outcome → Bayesian + neural edge → variable size

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
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Neural & Bayesian Import ────────────────────────────────────────────────
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

GAMMA   = "https://gamma-api.polymarket.com"
CLOB    = "https://clob.polymarket.com"
OUTPUT  = Path("/mnt/c/Users/12035/father_daddy_capital/output")
STATE   = OUTPUT / "pm_state.json"

# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

SCAN_SECONDS = 120
INITIAL_BANKROLL = 250.0
PAPER_BANKROLL = 250.0

# Multi-asset roster — allocation weights match @ohanism pattern
ASSETS = {
    "BTC":  {"yf": "BTC-USD",  "name": "Bitcoin",  "alloc": 0.50},
    "ETH":  {"yf": "ETH-USD",  "name": "Ethereum",  "alloc": 0.30},
    "SOL":  {"yf": "SOL-USD",  "name": "Solana",    "alloc": 0.15},
    "XRP":  {"yf": "XRP-USD",  "name": "XRP",       "alloc": 0.05},
}

# ── Variable sizing tiers (conviction-based) ───────────────────────────────
# Replaces the old fixed 2% Kelly. Small probes, big convictions.
SIZING = {
    "probe_threshold":      0.03,   # edge > this → probe bet
    "confidence_threshold": 0.08,   # edge > this → confidence bet
    "conviction_threshold": 0.15,   # edge > this → conviction bet
    "probe_pct":            0.01,   # 1% of bankroll
    "confidence_pct":       0.03,   # 3%
    "conviction_pct":       0.05,   # 5%
    "max_conviction_dollar": 25.0,  # Cap per conviction bet (seed phase: aggressive)
    "min_bet":               1.0,   # Minimum $1 (allow micro-probes)
}

# ── Drawdown Guardrails ──────────────────────────────────────────────────
# Progressive DD-based sizing reduction. During losing streaks, the engine
# automatically downgrades bet sizes. Prevents the 20% DD spirals seen
# in 9/20 multi-asset seeds. Three tiers:
#   DD > 5%:  conviction→confidence, confidence→probe (downgrade one tier)
#   DD > 8%:  all entries → probe only
#   DD > 12%: halt all new entries, wait for recovery
DD_GUARD = {
    "track_peak": True,       # Track session peak capital for DD calc
    "downgrade_dd": 0.05,     # >5% DD → downgrade conviction to confidence tier
    "probe_only_dd": 0.08,    # >8% DD → probe-only, no confidence/conviction
    "halt_dd": 0.12,          # >12% DD → no new entries at all
}

# ── Mid-Window Stop-Loss ─────────────────────────────────────────────────
# If a position is still open past half its expiry window and the price
# has moved AGAINST the prediction, close it early. Binaries shouldn't
# ride to zero when edge has clearly evaporated mid-window.
MID_WINDOW_STOP = {
    "enabled": True,
    "check_at_pct": 0.50,     # Check at 50% of window elapsed
    "loss_threshold_pct": -0.015,  # -1.5% move against prediction → trigger
}

# Contract filters
MAX_POSITIONS_PER_ASSET = 2     # Max concurrent positions per asset
MAX_TOTAL_POSITIONS = 8         # Across all assets
MIN_VOLUME_USD = 5000           # Lower for short-duration contracts
MIN_CONTRACT_PRICE = 0.03       # Lower floor — 5-min contracts have wilder prices
MAX_CONTRACT_PRICE = 0.90
MAX_WINDOW_MINUTES = 15         # Only trade contracts expiring within this window
MIN_SIGNAL_CONF = 0.12          # Minimum signal confidence to enter

# Signal thresholds
RSI_OVERSOLD = 48
RSI_OVERBOUGHT = 52

# Regime guard
BEAR_SKIP = True                # BTC < 20-SMA AND MACD < 0 → skip all entries

# Neural
NEURAL_BLEND_MAX = 0.30
NEURAL_BLEND_UPDATES = 200
NEURAL_CONSOLIDATE_EVERY = 50

_neural_engine  = None
_bayesian_engine = None
_feature_encoder = None


# ══════════════════════════════════════════════════════════════════════════════
# Neural / Bayesian helpers (unchanged from v1)
# ══════════════════════════════════════════════════════════════════════════════

def pm_encode_signal(sig: dict, asset_class: float = 0.0) -> np.ndarray:
    d = sig.get("direction", "neutral"); conf = sig.get("confidence", 0.0)
    rsi = sig.get("rsi", 50.0); macd = sig.get("macd", 0.0); mom = sig.get("momentum", 2)
    if d == "up":
        trend_sig = 0.5 + conf * 0.3; mom_sig = min(1.0, mom / 3.0)
        mean_rev = max(0.0, (50 - rsi) / 25)
    elif d == "down":
        trend_sig = -0.5 - conf * 0.3; mom_sig = -min(1.0, (3 - mom) / 3.0)
        mean_rev = -max(0.0, (rsi - 50) / 25)
    else:
        trend_sig = mom_sig = mean_rev = 0.0
    rsi_norm = (rsi - 50) / 25; macd_norm = float(np.clip(macd / 500, -1.0, 1.0))
    vol = abs(rsi - 50) / 25
    return np.array([
        float(np.clip(rsi_norm, -1.0, 1.0)), float(np.clip(macd_norm, -1.0, 1.0)),
        float(np.clip(trend_sig, -1.0, 1.0)), float(np.clip(mom_sig, -1.0, 1.0)),
        float(np.clip(mean_rev, -1.0, 1.0)), float(np.clip(vol, 0.0, 1.0)),
        float(np.clip(asset_class, -1.0, 1.0)), float(np.clip(conf, 0.0, 1.0)),
    ], dtype=float)

def scale_pm_pnl(pnl_pct: float) -> float:
    return float(np.clip(pnl_pct / 1.25, -1.0, 1.0))

def _get_neural():
    global _neural_engine
    if not _NEURAL_AVAILABLE: return None
    if _neural_engine is None: _neural_engine = pn.NeuralPlasticityEngine()
    return _neural_engine

def _get_bayesian():
    global _bayesian_engine
    if not _BAYESIAN_AVAILABLE: return None
    if _bayesian_engine is None: _bayesian_engine = bl.BayesianCalibrator()
    return _bayesian_engine

def _get_feature_encoder():
    global _feature_encoder
    if _feature_encoder is None:
        _feature_encoder = fe.FeatureEncoder(calibrator=_get_bayesian())
    return _feature_encoder

def _neural_blend_weight() -> float:
    neural = _get_neural()
    if neural is None: return 0.0
    return NEURAL_BLEND_MAX * min(1.0, neural.network.updates / NEURAL_BLEND_UPDATES)


# ══════════════════════════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════════════════════════

def _get(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-fdc/2.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def _parse(val):
    if isinstance(val, str):
        try: return json.loads(val)
        except: return val
    return val


# ══════════════════════════════════════════════════════════════════════════════
# Multi-Asset Price Fetching (parallel)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_5m(symbol: str) -> list[float]:
    try:
        import yfinance as yf
        h = yf.Ticker(symbol).history(period="1d", interval="5m")
        if len(h) < 14: return []
        return h['Close'].tolist()[-60:]
    except:
        return []

def fetch_all_asset_prices() -> dict[str, list[float]]:
    """Parallel fetch of 5m candle data for all assets."""
    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fetch_5m, a["yf"]): sym
                   for sym, a in ASSETS.items()}
        for f in as_completed(futures):
            sym = futures[f]
            try:
                results[sym] = f.result()
            except Exception:
                results[sym] = []
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Signal Stack (applied to any asset's price array)
# ══════════════════════════════════════════════════════════════════════════════

def asset_signal(prices: list[float]) -> dict:
    """RSI(7) + MACD(6/13) + momentum on any price array."""
    if len(prices) < 14:
        return {"direction": "neutral", "confidence": 0.0, "rsi": 50, "price": 0}
    current = prices[-1]
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = sum(max(d, 0) for d in deltas[-7:]) / 7
    losses = sum(max(-d, 0) for d in deltas[-7:]) / 7
    rsi = 100 - (100 / (1 + gains / max(losses, 1e-9)))

    def ema(vals, span):
        a = 2/(span+1); r = vals[0]
        for v in vals[1:]: r = a*v + (1-a)*r
        return r
    macd = ema(prices, 6) - ema(prices, 13)
    up = sum(1 for i in range(1, min(4, len(prices))) if prices[-i] > prices[-i-1])

    direction, confidence = "neutral", 0.0
    if rsi < RSI_OVERSOLD:
        direction = "up"; confidence = min(0.80, (RSI_OVERSOLD - rsi) / 15)
        if up >= 2: confidence += 0.10
    elif rsi > RSI_OVERBOUGHT:
        direction = "down"; confidence = min(0.80, (rsi - RSI_OVERBOUGHT) / 15)
        if up < 2: confidence += 0.10
    else:
        direction = "up" if up >= 2 else "down"
        confidence = 0.20

    sma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else current
    return {
        "direction": direction, "confidence": min(0.90, confidence),
        "rsi": round(rsi, 1), "macd": round(macd, 2), "momentum": up,
        "price": current, "sma20": sma20, "_prices": prices,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Regime Guard
# ══════════════════════════════════════════════════════════════════════════════

def is_bear_market(prices: list[float]) -> bool:
    if len(prices) < 20: return False
    sma20 = sum(prices[-20:]) / 20
    def ema(vals, span):
        a = 2/(span+1); r = vals[0]
        for v in vals[1:]: r = a*v + (1-a)*r
        return r
    return prices[-1] < sma20 and (ema(prices, 6) - ema(prices, 13)) < 0


# ══════════════════════════════════════════════════════════════════════════════
# Contract Discovery — "Up or Down" short-duration contracts
# ══════════════════════════════════════════════════════════════════════════════

def discover_short_contracts() -> dict[str, list[dict]]:
    """Find active 'Up or Down' contracts for each asset, expiring soon.
    Returns {asset_symbol: [contract_dict, ...]}"""
    today = datetime.now()
    month = today.strftime("%B")
    day = today.day
    by_asset: dict[str, list[dict]] = {sym: [] for sym in ASSETS}
    seen = set()

    for sym, a in ASSETS.items():
        queries = [
            f"{a['name']} Up or Down",
            f"{a['name']} Up or Down - {month} {day}",
        ]
        for q in queries:
            try:
                data = _get(f"{GAMMA}/public-search?q={urllib.parse.quote(q)}")
                for evt in data.get("events", []):
                    for m in evt.get("markets", []):
                        cid = m.get("conditionId", "")
                        if cid in seen or m.get("closed", False): continue
                        vol = float(m.get("volume", 0))
                        if vol < MIN_VOLUME_USD: continue
                        seen.add(cid)
                        question = m.get("question", "")
                        prices = _parse(m.get("outcomePrices", []))
                        if not isinstance(prices, list) or len(prices) < 2: continue
                        outcomes = _parse(m.get("outcomes", []))
                        if not isinstance(outcomes, list) or len(outcomes) < 2: continue

                        # Extract time window from question
                        window = extract_time_window(question)
                        if not window: continue

                        end_dt = parse_end_time(m.get("endDate", ""), window)
                        if not end_dt: continue
                        mins_to_expiry = (end_dt - datetime.now()).total_seconds() / 60
                        if mins_to_expiry < 0 or mins_to_expiry > MAX_WINDOW_MINUTES:
                            continue  # too far out or already expired

                        # Determine which outcome is "Up" and "Down"
                        up_idx, down_idx = 0, 1
                        if "Down" in (outcomes[0] or ""):
                            down_idx, up_idx = 0, 1

                        by_asset[sym].append({
                            "question": question, "conditionId": cid,
                            "up_price": float(prices[up_idx]), "down_price": float(prices[down_idx]),
                            "volume": vol, "slug": evt.get("slug", ""),
                            "outcomes": outcomes, "end_date": m.get("endDate", ""),
                            "window": window, "mins_to_expiry": round(mins_to_expiry, 1),
                            "asset": sym,
                        })
            except Exception:
                continue
    return by_asset


def extract_time_window(question: str) -> str | None:
    """Extract time window from question.
    'Bitcoin Up or Down - May 14, 8:15AM-8:20AM ET' → '8:15AM-8:20AM ET'"""
    m = re.search(r'(\d{1,2}:\d{2}(AM|PM)\s*-\s*\d{1,2}:\d{2}(AM|PM)\s*(ET|UTC))',
                  question, re.IGNORECASE)
    if m: return m.group(1).replace(" ", "")
    # Also match: "Bitcoin Up or Down - May 14, 7AM ET"
    m = re.search(r'(\d{1,2}(AM|PM)\s*(ET|UTC))', question, re.IGNORECASE)
    if m: return m.group(1).replace(" ", "")
    return None


def parse_end_time(end_date: str, window: str) -> datetime | None:
    """Parse the contract end time. Use end_date if valid, otherwise extract from window."""
    # Try end_date first (Gamma provides ISO timestamps)
    if end_date:
        try:
            return datetime.fromisoformat(end_date.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            pass
    # Fallback: extract end time from window string
    # "8:15AM-8:20AMET" → end = 8:20AM
    m_end = re.search(r'-(\d{1,2}:\d{2})(AM|PM)', window, re.IGNORECASE)
    if m_end:
        t_str = f"{m_end.group(1)}{m_end.group(2).upper()}"
        try:
            t = datetime.strptime(t_str, "%I:%M%p").time()
            now = datetime.now()
            return datetime.combine(now.date(), t)
        except ValueError:
            pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Variable Sizing
# ══════════════════════════════════════════════════════════════════════════════

def size_conviction(edge: float, bankroll: float, drawdown_pct: float = 0.0):
    """Conviction-based sizing with drawdown guard. Returns (bet_amount, tier_label)."""
    if edge <= SIZING["probe_threshold"] or bankroll <= 0:
        return 0.0, "skip"

    # ── Drawdown guardrail ──────────────────────────────────────────────────
    if DD_GUARD["track_peak"] and drawdown_pct >= DD_GUARD["halt_dd"]:
        return 0.0, "halted"  # >12% DD → no new entries
    force_probe = drawdown_pct >= DD_GUARD["probe_only_dd"]  # >8% → probe only
    downgrade = drawdown_pct >= DD_GUARD["downgrade_dd"]     # >5% → downgrade one tier

    if force_probe:
        bet = bankroll * SIZING["probe_pct"]
        return round(max(bet, SIZING["min_bet"]), 2), "probe"

    if edge >= SIZING["conviction_threshold"]:
        if downgrade:
            bet = bankroll * SIZING["confidence_pct"]
            return round(max(bet, SIZING["min_bet"]), 2), "confidence"
        bet = bankroll * SIZING["conviction_pct"]
        bet = min(bet, SIZING["max_conviction_dollar"])
        return round(max(bet, SIZING["min_bet"]), 2), "conviction"
    elif edge >= SIZING["confidence_threshold"]:
        if downgrade:
            bet = bankroll * SIZING["probe_pct"]
            return round(max(bet, SIZING["min_bet"]), 2), "probe"
        bet = bankroll * SIZING["confidence_pct"]
        return round(max(bet, SIZING["min_bet"]), 2), "confidence"
    else:
        bet = bankroll * SIZING["probe_pct"]
        return round(max(bet, SIZING["min_bet"]), 2), "probe"


def compute_drawdown(state: dict) -> float:
    """Compute current drawdown from peak capital. Peak is tracked in state."""
    bankroll = state.get("bankroll", PAPER_BANKROLL)
    peak = state.get("peak_capital", bankroll)
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - bankroll) / peak)


# ══════════════════════════════════════════════════════════════════════════════
# Trade Decision
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_entries_for_asset(
    sym: str, sig: dict, contracts: list[dict], state: dict
) -> list[dict]:
    """Find entry opportunities for one asset."""
    direction = sig["direction"]; conf = sig["confidence"]
    price = sig["price"]

    if direction == "neutral" or conf < MIN_SIGNAL_CONF:
        return []

    # ── Regime-aware direction filter ─────────────────────────────────────
    # Don't fade the trend: use 20-SMA to detect regime and suppress contrarian signals.
    # trending_up (BTC > 20-SMA AND MACD > 0) → suppress "down" signals
    # trending_down (BTC < 20-SMA AND MACD < 0) → suppress "up" signals (already caught by bear guard)
    # ranging/volatile → allow both directions
    sma20 = sig.get("sma20", price)
    macd = sig.get("macd", 0)
    in_uptrend = price > sma20 and macd > 0
    in_downtrend = price < sma20 and macd < 0

    if in_uptrend and direction == "down":
        return []  # Don't short an uptrend on RSI micro-pullbacks
    if in_downtrend and direction == "up":
        return []  # Don't buy a downtrend on oversold bounces (bear guard misses some)

    # Neural blend
    neural_pred = None; signal_vector = None
    blend_w = _neural_blend_weight()
    neural = _get_neural()
    asset_class_map = {"BTC": -0.5, "ETH": -0.2, "SOL": 0.3, "XRP": 0.5}
    if neural and blend_w > 0.0:
        signal_vector = pm_encode_signal(sig, asset_class=asset_class_map.get(sym, 0.0))
        neural_pred = neural.network.predict(signal_vector)
        neural_conf_val = (neural_pred + 1.0) / 2.0 if direction == "up" else (1.0 - neural_pred) / 2.0
        neural_conf_val = max(0.0, min(1.0, neural_conf_val))
        conf = conf * (1.0 - blend_w) + neural_conf_val * blend_w
        conf = round(min(0.95, conf), 3)

    candidates = []
    for c in contracts:
        if direction == "up":
            if MIN_CONTRACT_PRICE < c["up_price"] < MAX_CONTRACT_PRICE:
                candidates.append({"contract": c, "side": "Up", "price": c["up_price"]})
        else:
            if MIN_CONTRACT_PRICE < c["down_price"] < MAX_CONTRACT_PRICE:
                candidates.append({"contract": c, "side": "Down", "price": c["down_price"]})

    if not candidates:
        return []

    bankroll = state.get("bankroll", PAPER_BANKROLL)
    positions = state.get("positions", {})
    invested = sum(p.get("bet", 0) for p in positions.values())
    available = max(0.0, bankroll - invested)
    asset_positions = sum(1 for k in positions if k.startswith(sym))

    entries = []
    for cand in sorted(candidates, key=lambda x: conf - x["price"], reverse=True):
        edge = conf - cand["price"]
        if edge < SIZING["probe_threshold"]:
            continue

        key = f"{sym}_{cand['contract']['conditionId'][:16]}_{cand['side']}"
        if key in positions: continue
        if asset_positions + len(entries) >= MAX_POSITIONS_PER_ASSET: break
        if len(positions) + len(entries) >= MAX_TOTAL_POSITIONS: break
        if available < SIZING["min_bet"]: break

        # Bayesian calibration
        cal = _get_bayesian(); encoder = _get_feature_encoder()
        mins_to_expiry = cand["contract"].get("mins_to_expiry", 10)
        hours_to_res = mins_to_expiry / 60.0

        fv = encoder.encode(
            sig.get("_prices", []),
            cand["contract"]["up_price"], cand["contract"]["down_price"],
            cand["contract"]["volume"], hours_to_res,
        )
        cal_result = cal.predict(fv, market_price=cand["price"]) if cal else None

        if cal_result:
            cal_prob = cal_result["probability"]
            calibrated_edge = (cal_prob - cand["price"]) if cand["side"] == "Up" else ((1 - cal_prob) - cand["price"])
            if calibrated_edge > edge:
                edge = calibrated_edge

        # Variable sizing (with drawdown guard)
        dd = compute_drawdown(state)
        bet, tier = size_conviction(edge, bankroll, drawdown_pct=dd)
        if bet < SIZING["min_bet"] or bet > available:
            continue

        sv = signal_vector.tolist() if signal_vector is not None else None

        entries.append({
            "asset": sym, "action": f"BUY_{cand['side']}",
            "question": cand["contract"]["question"],
            "conditionId": cand["contract"]["conditionId"],
            "contract_price": cand["price"], "bet": bet,
            "edge": round(edge, 4), "sizing_tier": tier,
            "price_at_entry": round(price, 2),
            "signal_conf": conf, "signal_rsi": sig["rsi"],
            "mins_to_expiry": mins_to_expiry,
            "entry_time": datetime.now().isoformat(),
            "side": cand["side"],
            "bayesian_features": fv.tolist() if cal_result else None,
            "cal_prob": round(cal_result["probability"], 4) if cal_result else None,
            "cal_ci_low": round(cal_result["probability_ci_low"], 4) if cal_result else None,
            "cal_ci_high": round(cal_result["probability_ci_high"], 4) if cal_result else None,
            "cal_certainty": round(cal_result["certainty"], 4) if cal_result else None,
            "cal_entropy": round(cal_result["entropy"], 4) if cal_result else None,
            "kl_divergence": round(cal_result["kl_divergence"], 6) if cal_result and "kl_divergence" in cal_result else None,
            "kl_edge_score": round(cal_result["kl_edge_score"], 6) if cal_result and "kl_edge_score" in cal_result else None,
            "signal_vector": sv,
            "neural_pred": round(neural_pred, 4) if neural_pred is not None else None,
        })
        available -= bet

    return entries


# ══════════════════════════════════════════════════════════════════════════════
# Settlement
# ══════════════════════════════════════════════════════════════════════════════

def check_settlements(state: dict, prices_by_asset: dict[str, float]) -> list[dict]:
    """Settle positions where contract window has expired (time-based).
    For short-duration 'Up or Down' contracts: settled = price moved in predicted direction."""
    positions = state.get("positions", {})
    settled = []
    now = datetime.now()

    for key, pos in list(positions.items()):
        # Time-based: check if mins_to_expiry has elapsed since entry
        entry_time_str = pos.get("entry_time", "")
        mins_to_expiry = pos.get("mins_to_expiry", 10)
        try:
            entry_dt = datetime.fromisoformat(entry_time_str)
            if (now - entry_dt).total_seconds() / 60 < mins_to_expiry:
                continue  # Not yet expired
        except (ValueError, TypeError):
            continue

        sym = pos.get("asset", "BTC")
        entry_price = pos.get("price_at_entry", 0)
        current_price = prices_by_asset.get(sym, entry_price)
        side = pos["side"]

        # Simple settlement: did price move in predicted direction?
        moved_up = current_price > entry_price
        won = (side == "Up" and moved_up) or (side == "Down" and not moved_up)
        # Edge case: flat price → lose (Up must go up, Down must go down)

        bet = pos["bet"]
        if won:
            payout = bet / pos["contract_price"]
            profit = payout - bet
        else:
            profit = -bet

        settled.append({**pos, "pnl": round(profit, 2),
                       "settle_price": round(current_price, 2),
                       "settle_time": now.isoformat()})
        del positions[key]

    return settled


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

def summary(state: dict, entries: list, settled: list) -> str:
    br = state.get("bankroll", PAPER_BANKROLL); pnl = state.get("total_pnl", 0)
    wins = state.get("wins", 0); losses = state.get("losses", 0)
    trades = wins + losses; positions = state.get("positions", {})

    lines = ["", "🎲 POLYMARKET ENGINE v2 (multi-asset • short-duration)"]
    lines.append(f"   Bankroll: ${br:,.2f} | P&L: ${pnl:+,.2f} | Trades: {trades}")
    if trades:
        lines.append(f"   Wins: {wins} | Losses: {losses} | Rate: {wins/max(1,trades)*100:.0f}%")

    if settled:
        lines.append("   ── Settled ──")
        for s in settled[-5:]:
            e = "🟢" if s["pnl"] > 0 else "🔴"
            lines.append(f"   {e} [{s.get('asset','?')}] {s['action']} — ${s['pnl']:+,.2f} ({s.get('sizing_tier','?')})")

    if entries:
        lines.append("   ── New entries ──")
        for e in entries:
            lines.append(f"   ⚡ [{e['asset']}] {e['action']} ${e['bet']} @ {e['contract_price']:.3f} "
                        f"(edge={e['edge']:.3f}, {e['sizing_tier']}, {e['mins_to_expiry']}m to expiry)")

    if positions:
        lines.append(f"   ── Open ({len(positions)}) ──")
        for k, p in list(positions.items())[-8:]:
            lines.append(f"   📌 [{p.get('asset','?')}] {p['side']} ${p['bet']} | "
                        f"edge={p.get('edge',0):.3f} | {p.get('sizing_tier','?')}")

    if not positions and not entries and not settled:
        lines.append("   Idle — no signals meeting criteria.")

    # Neural + Bayesian status
    neural = _get_neural()
    if neural:
        st = neural.stats(); bw = _neural_blend_weight()
        lines.append(f"   🧠 Neural: {st['updates']} updates | Acc={st['rolling_accuracy']:.0%} | Blend={bw:.0%}")
    cal = _get_bayesian()
    if cal and cal.updates > 0:
        lines.append(f"   📐 Bayesian: {cal.updates} updates | Brier={cal.brier_score:.4f} | Cal={cal.calibration_factor:.2%}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Main Loop
# ══════════════════════════════════════════════════════════════════════════════

def load_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        st = json.loads(STATE.read_text())
        # Migrate v1 state if needed
        if "asset_pnl" not in st:
            st["asset_pnl"] = {}
        return st
    return {"bankroll": PAPER_BANKROLL, "total_pnl": 0, "wins": 0, "losses": 0,
            "positions": {}, "journal": [], "scans": 0, "asset_pnl": {}}

def save_state(state):
    state["scans"] = state.get("scans", 0) + 1
    STATE.write_text(json.dumps(state, indent=2, default=str))


def run_once(state):
    # ── 1. Fetch all asset prices ─────────────────────────────────────────
    all_prices = fetch_all_asset_prices()

    # ── 2. Regime guard (BTC) ─────────────────────────────────────────────
    btc_prices = all_prices.get("BTC", [])
    if not btc_prices:
        return [], [], {}
    if BEAR_SKIP and is_bear_market(btc_prices):
        return [], [], {"BTC": "bear_skip", "note": "bear market guard active"}

    # ── 3. Run signals on all assets ─────────────────────────────────────
    signals = {}
    for sym in ASSETS:
        prices = all_prices.get(sym, [])
        if prices:
            signals[sym] = asset_signal(prices)

    # ── 4. Discover contracts ────────────────────────────────────────────
    contracts_by_asset = discover_short_contracts()

    # ── 5. Build price map for settlement ────────────────────────────────
    price_map = {sym: sig["price"] for sym, sig in signals.items() if sig["price"]}

    # ── 6. Settle expired contracts ──────────────────────────────────────
    settled = check_settlements(state, price_map)
    for s in settled:
        pnl = s["pnl"]; sym = s.get("asset", "?")
        state["total_pnl"] += pnl; state["bankroll"] += pnl
        if pnl > 0: state["wins"] = state.get("wins", 0) + 1
        else: state["losses"] = state.get("losses", 0) + 1
        state["asset_pnl"][sym] = state["asset_pnl"].get(sym, 0.0) + pnl
        state.setdefault("journal", []).append({
            "ts": datetime.now().isoformat(), "type": "settle", "pnl": pnl,
            "asset": sym, "question": s.get("question", ""),
        })

        # Bayesian learn
        cal = _get_bayesian()
        if cal:
            sv_b = s.get("bayesian_features")
            if sv_b:
                cal.update(np.array(sv_b, dtype=float), 1 if pnl > 0 else 0)

        # Neural learn
        neural = _get_neural()
        sv = s.get("signal_vector"); n_pred = s.get("neural_pred")
        if neural and sv and n_pred is not None:
            bet = s.get("bet", 1.0)
            pnl_pct = pnl / max(bet, 0.01)
            sv_arr = np.array(sv, dtype=float)
            neural.network.learn_from_trade(sv_arr, n_pred, scale_pm_pnl(pnl_pct))
            neural.network.add_to_replay(sv_arr, scale_pm_pnl(pnl_pct))
            if neural.network.updates % 5 == 0: neural.network.replay()
            if neural.network.updates > 0 and neural.network.updates % NEURAL_CONSOLIDATE_EVERY == 0:
                neural.network.consolidate()
            neural.network.save(); neural.performance.save()

    # ── 7. Evaluate new entries per asset ────────────────────────────────
    all_entries = []
    for sym in ASSETS:
        sig = signals.get(sym)
        contracts = contracts_by_asset.get(sym, [])
        if sig and sig["direction"] != "neutral":
            entries = evaluate_entries_for_asset(sym, sig, contracts, state)
            all_entries.extend(entries)

    for e in all_entries:
        key = f"{e['asset']}_{e['conditionId'][:16]}_{e['side']}"
        state["positions"][key] = e

    save_state(state)
    print(summary(state, all_entries, settled))
    return all_entries, settled, signals


def run_continuous():
    state = load_state()
    print(f"🎲 FDC POLYMARKET v2 — {SCAN_SECONDS}s scan | ${state['bankroll']:,.2f}")
    print(f"   Assets: {', '.join(ASSETS)} | Short-duration 'Up or Down'\n")

    last_summary = 0
    while True:
        try:
            entries, settled, signals = run_once(state)
            now = time.time()
            if entries or settled or now - last_summary > 600:
                last_summary = now
            time.sleep(SCAN_SECONDS)
        except KeyboardInterrupt:
            print(f"\n👋 Stopped. ${state['bankroll']:,.2f} | P&L: ${state.get('total_pnl',0):+,.2f}")
            break
        except Exception as e:
            print(f"❌ {e}", file=sys.stderr)
            time.sleep(30)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--once" in sys.argv:
        state = load_state()
        e, s, sigs = run_once(state)
        btc_sig = sigs.get("BTC", {})
        if btc_sig and btc_sig.get("price"):
            print(f"\nBTC: {btc_sig['direction']} @ {btc_sig['confidence']:.2f} "
                  f"(RSI={btc_sig['rsi']}, ${btc_sig['price']:,.2f})")
            for sym in ["ETH", "SOL", "XRP"]:
                s2 = sigs.get(sym, {})
                if s2.get("price"):
                    print(f"{sym}: {s2['direction']} @ {s2['confidence']:.2f} "
                          f"(RSI={s2['rsi']}, ${s2['price']:,.2f})")
            neural = _get_neural()
            if neural:
                st = neural.stats()
                print(f"🧠 Neural: {st['updates']} updates | Acc={st['rolling_accuracy']:.0%} | "
                      f"Blend={_neural_blend_weight():.0%}")
        else:
            print("\n⚠ No price data available.")
    elif "--discover" in sys.argv:
        cba = discover_short_contracts()
        for sym, cs in cba.items():
            print(f"\n{sym} ({len(cs)} contracts):")
            for c in sorted(cs, key=lambda x: x["mins_to_expiry"])[:8]:
                print(f"  {c['question']} — Up {c['up_price']*100:.0f}% | "
                      f"Down {c['down_price']*100:.0f}% | ${c['volume']:,.0f} | "
                      f"Expires in {c['mins_to_expiry']}m")
    elif "--reset" in sys.argv:
        STATE.unlink(missing_ok=True)
        print("State reset.")
    elif "--continuous" in sys.argv or "-c" in sys.argv:
        run_continuous()
    else:
        print(__doc__)
