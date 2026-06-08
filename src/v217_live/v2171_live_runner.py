#!/usr/bin/env python3
"""
V21.7.1 LIVE DEPLOYMENT — DOWN_MOMENTUM EXECUTION SURVIVABLE
=============================================================
Controlled live asymmetric extraction. No research. No iteration.
Reality is now the optimizer.

LIVE_PROFILE:
  asset: BTC
  intervals: 5m, 15m
  side: DOWN
  state: MOMENTUM
  route: TAKER
  timing: MOMENTUM_ONLY
  bucket_primary: 0.03-0.12
  bucket_preferred: 0.05-0.08
  position_size: $1.00

Kill switches:
  MAX_DAILY_LOSS = $15.00
  MAX_WEEKLY_LOSS = $50.00
  MAX_CONSECUTIVE_LOSSES = 60
  MAX_TOTAL_LIVE_TRADES = 100

Hard revert to PAPER if:
  - realized_ev < 0 over rolling 100 trades
  - PF < 1.0
  - settlement_errors > 0
  - accounting_errors > 0
"""

import json, os, time, sys, logging, traceback
import numpy as np
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple

sys.path.insert(0, "/home/naq1987s/father-daddy-capital")
from fdc_pm_live import (
    check_wallet, get_tick_size, get_neg_risk, validate_price, round_to_tick,
    derive_api_credentials, get_clob_client, build_dry_run_order,
    submit_tracked_order, read_orderbook, parse_slug, compute_next_slug,
    discover_active_contract, KillSwitch,
    CLOB_URL, GAMMA_URL, CHAIN_ID, FUNDER,
)
import urllib.request
import csv

# ═══════════════════════════════════════════════════════════════════════
# V21.7.1 CONFIGURATION — LOCKED PER DIRECTIVE
# ═══════════════════════════════════════════════════════════════════════

BANKROLL_ACTUAL = 70.00  # User-confirmed tradeable bankroll

LIVE_PROFILE = {
    "asset": ["BTC"],
    "intervals": ["5m", "15m"],
    "side": "DOWN",
    "state": "MOMENTUM",
    "route": "TAKER",
    "timing": "MOMENTUM_ONLY",
    "bucket_primary": (0.03, 0.12),
    "bucket_preferred": (0.05, 0.08),
    "position_size": 1.00,
    "version": "V21.7.1",
}

# §4: Revised kill switches
MAX_DAILY_LOSS = 15.00
MAX_WEEKLY_LOSS = 50.00
MAX_CONSECUTIVE_LOSSES = 60
MAX_TOTAL_LIVE_TRADES = 100
MAX_DAILY_TRADES = 30
MAX_CONCURRENT = 1
POSITION_SIZE = 1.00

# §3: Bucket weighting
BUCKET_WEIGHTS = {
    (0.05, 0.08): 1.00,   # preferred — survivable
    (0.03, 0.05): 0.85,   # ultra-cheap
    (0.08, 0.10): 0.65,   # mid-cheap
    (0.10, 0.12): 0.40,   # upper PRIMARY
}

# Signal weights — DOWN_MOMENTUM emphasis
SIGNAL_WEIGHTS = {
    'persist': 0.30, 'accel': 0.25, 'lag': 0.15,
    'vol': 0.15, 'tte': 0.10, 'exec': 0.05, 'rsi': 0.05,
}

# Direction priority — DOWN only
DIRECTION_PRIORITY = {
    'DOWN_MOMENTUM': 1.60,
    'DOWN_CONTINUATION': 1.40,
    'UP_REVERSAL': 0.00,
    'UP_CONTINUATION': 0.00,
    'FLAT': 0.00,
}

# Timing — MOMENTUM only
TIMING_LO = 0.40
TIMING_HI = 0.80

# Scan intervals (§9)
BASE_SCAN_INTERVAL = 5.0
FAST_SCAN_INTERVAL = 2.0

# ═══════════════════════════════════════════════════════════════════════
# §3-7: TELEMETRY CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

NOTRADE_REASONS = [
    "bucket_below_floor",      # down_mid < 0.03
    "bucket_above_cap",        # down_mid >= 0.12
    "wrong_state",             # signal not DOWN_MOMENTUM/DOWN_CONTINUATION
    "low_survivability",       # survivability < 0.05
    "too_near_expiry",         # expires_in < 30s
    "duplicate_position",      # already have position on this condition
    "stale_quote",             # no orderbook data
    "no_book",                 # no bids/asks in orderbook
    "no_active_market",        # no contract found
    "execution_rejected",      # kill switch or order failed
    "risk_limit_block",        # daily/weekly/consecutive loss limit hit
    "spread_too_wide",         # spread > 25% of price
    "no_momentum",             # vol_imbalance not bearish enough
]

BUCKET_RANGES = {
    "0_3c":      (0.000, 0.030),
    "3_5c":      (0.030, 0.050),
    "5_8c":      (0.050, 0.080),
    "8_12c":     (0.080, 0.120),
    "12_20c":    (0.120, 0.200),
    "20_40c":    (0.200, 0.400),
    "above_40c": (0.400, 999.0),
}

SCARCITY_REPORT_INTERVAL = 1800  # 30 minutes

# Output paths
OUTPUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v2171_live")
LOG_FILE = OUTPUT_DIR / "v2171_live.log"
TRADES_FILE = OUTPUT_DIR / "trades.jsonl"
INCIDENT_FILE = OUTPUT_DIR / "incident_report.json"
STATE_FILE = OUTPUT_DIR / "state.json"
FORENSICS_FILE = OUTPUT_DIR / "state_gate_forensics.jsonl"
ELIGIBLE_AUDIT_CSV = OUTPUT_DIR / "eligible_bucket_state_audit.csv"
SHADOW_COUNTERFACTUAL_FILE = OUTPUT_DIR / "spot_momentum_shadow_counterfactual.json"
LATENCY_TELEMETRY_FILE = OUTPUT_DIR / "latency_telemetry.jsonl"
LATENCY_REPORT_FILE = OUTPUT_DIR / "latency_report.json"
STATE_FORENSICS_REPORT_FILE = OUTPUT_DIR / "state_gate_forensics_report.json"
ARMED_SCANNER_REPORT_FILE = OUTPUT_DIR / "armed_scanner_report.json"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE)),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("v2171_live")
# Reduce HTTP noise
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """§10: Required trade logging."""
    timestamp: str
    asset: str
    interval: str
    slug: str
    condition_id: str
    side: str
    entry_price: float
    bucket: str
    timing_phase: str
    route: str
    signal_score: float
    expected_probability: float
    expected_ev: float
    expected_slippage: float
    actual_fill_price: float
    fill_latency_ms: float
    slippage_bps: float
    settlement_result: str  # "PENDING" | "WON" | "LOST" | "ERROR"
    win_loss: str           # "WIN" | "LOSS" | "PENDING"
    realized_pnl: float
    bankroll_before: float
    bankroll_after: float
    running_pf: float
    running_ev: float
    running_drawdown: float
    fill_quality: str       # "FULL" | "PARTIAL" | "REJECTED" | "STALE"

@dataclass
class LiveState:
    """Persistent runner state."""
    live_enabled: bool = True
    paper_only: bool = False
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    bankroll: float = 0.0  # set at startup
    consecutive_losses: int = 0
    daily_loss: float = 0.0
    weekly_loss: float = 0.0
    daily_trades: int = 0
    settlement_errors: int = 0
    accounting_errors: int = 0
    stale_fill_errors: int = 0
    api_execution_errors: int = 0
    duplicate_position_errors: int = 0
    last_daily_reset: str = ""
    last_weekly_reset: str = ""
    running_pnl_list: list = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""


# ═══════════════════════════════════════════════════════════════════════
# MARKET DATA — CLOB ORDERBOOK
# ═══════════════════════════════════════════════════════════════════════

def fetch_orderbook_depth(token_id: str, depth: int = 10) -> Optional[dict]:
    """Fetch real orderbook from Polymarket CLOB."""
    try:
        url = f"{CLOB_URL}/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        bids = sorted(
            [(float(e["price"]), float(e["size"])) for e in data.get("bids", [])],
            key=lambda x: -x[0]
        )[:depth]
        asks = sorted(
            [(float(e["price"]), float(e["size"])) for e in data.get("asks", [])],
            key=lambda x: x[0]
        )[:depth]
        return {
            "bids": bids,
            "asks": asks,
            "best_bid": bids[0][0] if bids else 0.0,
            "best_ask": asks[0][0] if asks else 1.0,
            "spread": asks[0][0] - bids[0][0] if bids and asks else 1.0,
            "tick_size": data.get("tick_size", "0.01"),
        }
    except Exception as e:
        log.warning(f"Orderbook fetch failed for {token_id[:16]}...: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# BTC SPOT PRICE FEED — Binance API
# ═══════════════════════════════════════════════════════════════════════

SPOT_PRICE_BUFFER: List[dict] = []  # [{timestamp, price}, ...]
TOKEN_ASK_BUFFER: Dict[str, List[dict]] = {}  # token_id -> [{timestamp, ask}, ...]
SPOT_BUFFER_MAX = 120  # Keep last 120 readings (~10 min at 5s intervals)
TOKEN_BUFFER_MAX = 60  # Keep last 60 token ask readings (~5 min)


def fetch_btc_spot() -> Optional[float]:
    """Fetch current BTC/USDT price from Binance."""
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc/2.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return float(data["price"])
    except Exception as e:
        log.debug(f"BTC spot fetch failed: {e}")
        return None


def fetch_btc_perp_price() -> Optional[float]:
    """Fetch BTC/USDT perpetual futures price from Binance."""
    try:
        url = "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc/2.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return float(data["price"])
    except Exception as e:
        log.debug(f"BTC perp fetch failed: {e}")
        return None


def record_spot(price: float):
    """Record spot price with timestamp."""
    now = time.time()
    SPOT_PRICE_BUFFER.append({"timestamp": now, "price": price})
    # Trim to max
    while len(SPOT_PRICE_BUFFER) > SPOT_BUFFER_MAX:
        SPOT_PRICE_BUFFER.pop(0)


def record_token_ask(token_id: str, ask: float):
    """Record token ask price for delta tracking."""
    now = time.time()
    if token_id not in TOKEN_ASK_BUFFER:
        TOKEN_ASK_BUFFER[token_id] = []
    TOKEN_ASK_BUFFER[token_id].append({"timestamp": now, "ask": ask})
    while len(TOKEN_ASK_BUFFER[token_id]) > TOKEN_BUFFER_MAX:
        TOKEN_ASK_BUFFER[token_id].pop(0)


def compute_token_ask_delta(token_id: str) -> dict:
    """Compute token ask price deltas over 15s and 30s horizons."""
    result = {"ask_delta_15s": 0.0, "ask_delta_30s": 0.0, "has_delta": False}
    buf = TOKEN_ASK_BUFFER.get(token_id, [])
    if len(buf) < 2:
        return result
    now = buf[-1]
    now_t = now["timestamp"]
    now_ask = now["ask"]
    for horizon, key in [(15, "ask_delta_15s"), (30, "ask_delta_30s")]:
        target_t = now_t - horizon
        best = min(buf, key=lambda e: abs(e["timestamp"] - target_t))
        if abs(best["timestamp"] - target_t) < horizon * 0.5:
            result[key] = now_ask - best["ask"]
    result["has_delta"] = True
    return result


def compute_spot_velocity() -> dict:
    """Compute BTC spot velocities from historical buffer."""
    result = {
        "spot_now": 0.0,
        "spot_15s": 0.0,
        "spot_30s": 0.0,
        "spot_60s": 0.0,
        "velocity_15s": 0.0,
        "velocity_30s": 0.0,
        "velocity_60s": 0.0,
        "has_spot": False,
        "perp_now": 0.0,
        "perp_velocity_15s": 0.0,
        "perp_velocity_30s": 0.0,
        "perp_velocity_60s": 0.0,
        "has_perp": False,
    }
    if len(SPOT_PRICE_BUFFER) < 2:
        return result

    now = SPOT_PRICE_BUFFER[-1]
    result["spot_now"] = now["price"]
    result["has_spot"] = True
    now_t = now["timestamp"]

    # Find closest reading at each horizon
    for horizon, key in [(15, "spot_15s"), (30, "spot_30s"), (60, "spot_60s")]:
        target_t = now_t - horizon
        best = None
        best_delta = float('inf')
        for entry in SPOT_PRICE_BUFFER:
            delta = abs(entry["timestamp"] - target_t)
            if delta < best_delta:
                best = entry
                best_delta = delta
        if best and best_delta < horizon * 0.5:
            result[key] = best["price"]
            velocity = (now["price"] - best["price"]) / best["price"] * 100
            vel_key = f"velocity_{horizon}s"
            result[vel_key] = round(velocity, 6)

    # Reference price: oldest reading in buffer (up to 60s ago)
    ref_candidates = [e for e in SPOT_PRICE_BUFFER if now_t - e["timestamp"] >= 30]
    if ref_candidates:
        result["reference_price"] = ref_candidates[0]["price"]
        current_dist = (now["price"] - ref_candidates[0]["price"]) / ref_candidates[0]["price"] * 100
        result["distance_from_ref"] = round(current_dist, 6)

        # Distance deltas: how distance to reference is changing over 15s/30s
        for delta_key, horizon in [("distance_delta_15s", 15), ("distance_delta_30s", 30)]:
            target_t = now_t - horizon
            best = None
            best_delta = float('inf')
            for entry in SPOT_PRICE_BUFFER:
                delta = abs(entry["timestamp"] - target_t)
                if delta < best_delta:
                    best = entry
                    best_delta = delta
            if best and best_delta < horizon * 0.5:
                past_dist = (best["price"] - ref_candidates[0]["price"]) / ref_candidates[0]["price"] * 100
                result[delta_key] = round(current_dist - past_dist, 6)

    # §6: Compute perp velocity from buffer entries with perp field
    perp_entries = [e for e in SPOT_PRICE_BUFFER if "perp" in e and e["perp"] > 0]
    if len(perp_entries) >= 2:
        now_perp = perp_entries[-1]
        result["perp_now"] = now_perp.get("perp", 0)
        now_pt = now_perp["timestamp"]
        for horizon, key in [(15, "perp_velocity_15s"), (30, "perp_velocity_30s"), (60, "perp_velocity_60s")]:
            target_t = now_pt - horizon
            best = None
            best_delta = float('inf')
            for entry in perp_entries:
                delta = abs(entry["timestamp"] - target_t)
                if delta < best_delta:
                    best = entry
                    best_delta = delta
            if best and best_delta < horizon * 0.5 and "perp" in best:
                v = (now_perp.get("perp", 0) - best.get("perp", 0)) / best.get("perp", 1) * 100
                result[key] = round(v, 6)
        result["has_perp"] = result["perp_now"] > 0

    return result


def compute_spot_momentum_shadow(spot_vel: dict, down_mid: float, expires_in: float,
                                    sig_info: dict = None, token_delta: dict = None) -> dict:
    """
    §3+§7: SPOT_MOMENTUM_SHADOW — shadow state model.
    Classifies MOMENTUM if:
      - BTC spot velocity is negative over 15s/30s/60s (price declining)
      - Price is moving away from reference in DOWN direction
      - time_to_expiry > 30s
      - DOWN ask is 3-12¢
    Strengthening features (§7):
      - Perp velocity confirms spot velocity
      - Distance to reference is worsening for UP
      - Token ask delta is rising (DOWN token getting more expensive = market pricing in decline)
    PAPER-ONLY. Never trades live.
    """
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "shadow_state": "NO_SIGNAL",
        "shadow_momentum": False,
        "reason": "",
        "checks": {},
        "shadow_entry_price": 0.0,
        "shadow_expected_ev": 0.0,
        "shadow_block_reason": "",
        "strengthening": {},
    }

    # Check each criterion
    vel_15_neg = spot_vel.get("velocity_15s", 0) < 0
    vel_30_neg = spot_vel.get("velocity_30s", 0) < 0
    vel_60_neg = spot_vel.get("velocity_60s", 0) < 0
    perp_vel_15_neg = spot_vel.get("perp_velocity_15s", 0) < 0
    perp_vel_30_neg = spot_vel.get("perp_velocity_30s", 0) < 0
    perp_confirms = (perp_vel_15_neg or perp_vel_30_neg) if spot_vel.get("has_perp", False) else None
    direction_down = spot_vel.get("distance_from_ref", 0) < 0  # price dropping = negative
    in_bucket = 0.03 <= down_mid < 0.12
    not_expired = expires_in > 30

    # Token ask delta: rising ask = market pricing in decline (strengthening)
    ask_rising_15 = token_delta and token_delta.get("ask_delta_15s", 0) > 0
    ask_rising_30 = token_delta and token_delta.get("ask_delta_30s", 0) > 0

    # Vol expansion check (strengthening)
    spread_pct = sig_info.get("spread_pct", 1.0) if sig_info else 1.0
    vol_expanding = spread_pct > 0.05  # spread > 5% = volatility expansion

    result["checks"] = {
        "vel_15s_negative": vel_15_neg,
        "vel_30s_negative": vel_30_neg,
        "vel_60s_negative": vel_60_neg,
        "perp_vel_15s_negative": perp_vel_15_neg,
        "perp_vel_30s_negative": perp_vel_30_neg,
        "perp_confirms_spot": perp_confirms,
        "direction_from_ref_down": direction_down,
        "in_primary_bucket": in_bucket,
        "not_expired": not_expired,
        "any_velocity_negative": vel_15_neg or vel_30_neg or vel_60_neg,
        "ask_rising_15s": ask_rising_15,
        "ask_rising_30s": ask_rising_30,
        "vol_expanding": vol_expanding,
    }

    result["strengthening"] = {
        "perp_confirms": perp_confirms,
        "ask_rising": ask_rising_15 or ask_rising_30,
        "vol_expanding": vol_expanding,
        "strengthening_count": int(sum([
            1 if perp_confirms else 0,
            1 if (ask_rising_15 or ask_rising_30) else 0,
            1 if vol_expanding else 0,
        ])),
    }

    # Gate sequence
    if not spot_vel.get("has_spot", False):
        result["shadow_state"] = "NO_SPOT_DATA"
        result["reason"] = "no_btc_spot_feed"
        result["shadow_block_reason"] = "no_spot"
        return result

    if not in_bucket:
        result["shadow_state"] = "OUTSIDE_BUCKET"
        result["reason"] = f"down_mid={down_mid:.4f} outside 0.03-0.12"
        result["shadow_block_reason"] = "bucket"
        return result

    if not not_expired:
        result["shadow_state"] = "EXPIRING"
        result["reason"] = f"expires_in={expires_in:.0f}s < 30s"
        result["shadow_block_reason"] = "expiry"
        return result

    if not (vel_15_neg or vel_30_neg or vel_60_neg):
        result["shadow_state"] = "NO_DECLINE"
        result["reason"] = "no_negative_velocity_horizon"
        result["shadow_momentum"] = False
        result["shadow_block_reason"] = "no_decline_velocity"
        return result

    if not direction_down:
        result["shadow_state"] = "RALLYING"
        result["reason"] = "spot_rallying_from_reference"
        result["shadow_momentum"] = False
        result["shadow_block_reason"] = "rallying"
        return result

    # All shadow criteria met
    result["shadow_state"] = "SPOT_MOMENTUM"
    result["shadow_momentum"] = True
    result["reason"] = "velocity_negative_and_declining_and_in_bucket"
    result["shadow_block_reason"] = ""
    # Shadow would trade at current ask
    result["shadow_entry_price"] = down_mid
    # Shadow EV: probability of DOWN = 1 - down_mid, payout = 1 - down_mid, loss = down_mid
    result["shadow_expected_ev"] = round((1 - down_mid) * (1 - down_mid) - down_mid * down_mid, 6)
    return result


def compute_continuation_from_orderbook(ob: dict, side: str = "DOWN") -> Tuple[str, float, dict]:
    """
    §12: Derive continuation signal from live orderbook.
    DOWN_MOMENTUM = heavy bid/ask imbalance + cheap token price in bucket.
    """
    if not ob or not ob["bids"] or not ob["asks"]:
        return "NO_SIGNAL", 0.0, {}

    best_bid = ob["best_bid"]
    best_ask = ob["best_ask"]
    spread = ob["spread"]
    mid = (best_bid + best_ask) / 2

    # Compute bid/ask volume imbalance
    total_bid_vol = sum(s for _, s in ob["bids"][:5])
    total_ask_vol = sum(s for _, s in ob["asks"][:5])
    vol_imbalance = (total_bid_vol - total_ask_vol) / max(total_bid_vol + total_ask_vol, 1)

    # Spread relative to price
    spread_pct = spread / max(mid, 0.001)

    # For DOWN token (cheap side): high ask volume = selling pressure = DOWN continuation
    # Token price in preferred bucket?
    in_preferred = 0.05 <= mid < 0.08
    in_primary = 0.03 <= mid < 0.12
    bucket_weight = 1.00 if in_preferred else (
        0.85 if 0.03 <= mid < 0.05 else (
            0.65 if 0.08 <= mid < 0.10 else (
                0.40 if 0.10 <= mid < 0.12 else 0.0
            )
        )
    )

    # Simple continuation score from orderbook
    # Heavy ask volume on DOWN token = market expects it to go to 0 = DOWN_MOMENTUM
    ask_heavy = total_ask_vol > total_bid_vol * 1.2
    reasonable_spread = spread_pct < 0.25  # spread < 25% of price

    # Expected probability: cheap token losing = settling to 0
    # Price near 0 = high probability of losing, high payout if correct
    expected_prob_down = 1.0 - mid  # DOWN token at 7¢ → 93% chance it settles to 0

    # Expected EV for buying DOWN token at mid price
    # Win: token settles to 1.0, payout = (1.0 - mid) per share
    # Lose: token settles to 0.0, loss = mid per share
    expected_ev = expected_prob_down * mid - (1 - expected_prob_down) * (1.0 - mid)

    signal_info = {
        "mid": mid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_pct": spread_pct,
        "total_bid_vol": total_bid_vol,
        "total_ask_vol": total_ask_vol,
        "vol_imbalance": vol_imbalance,
        "bucket_weight": bucket_weight,
        "expected_prob_down": expected_prob_down,
        "expected_ev": expected_ev,
        "in_preferred": in_preferred,
        "in_primary": in_primary,
    }

    # Signal validation
    if not in_primary:
        return "NO_SIGNAL", 0.0, signal_info

    if not reasonable_spread:
        return "SPREAD_TOO_WIDE", 0.0, signal_info

    if not ask_heavy and vol_imbalance > -0.1:
        return "NO_MOMENTUM", 0.0, signal_info

    # Survivability score (§7): realized_ev × fill_prob × slippage_survival
    fill_probability = min(1.0, total_ask_vol / max(POSITION_SIZE / max(mid, 0.01), 1))
    slippage_survival = max(0, 1.0 - spread_pct * 3)  # wider spread = less survivable
    payout_asymmetry = (1 - mid) / max(mid, 0.01)  # e.g., 0.07 → 13.3x

    survivability = expected_ev * fill_probability * slippage_survival * payout_asymmetry * bucket_weight

    if survivability < 0.05:
        return "LOW_SURVIVABILITY", survivability, signal_info

    state = "DOWN_MOMENTUM" if ask_heavy and vol_imbalance < -0.15 else "DOWN_CONTINUATION"
    return state, survivability, signal_info


# ═══════════════════════════════════════════════════════════════════════
# LIVE TRADING ENGINE
# ═══════════════════════════════════════════════════════════════════════

class V2171LiveRunner:
    """V21.7.1 Live Deployment Runner — DOWN_MOMENTUM EXECUTION SURVIVABLE."""

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self.state = LiveState()
        self.trades: List[dict] = []
        self.wallet_info: dict = {}
        self.active_contracts: Dict[str, dict] = {}  # slug → contract info
        self.active_positions: Dict[str, dict] = {}   # condition_id → position info
        self.scan_interval = BASE_SCAN_INTERVAL
        self.running = True
        self.start_time = datetime.now(timezone.utc)

        # ═══════════════════════════════════════════════════════════════
        # §3-7: TELEMETRY — No-Trade Reason, Bucket Occupancy, Near-Miss, Scarcity
        # ═══════════════════════════════════════════════════════════════
        self.cycle_id = 0
        self.last_scarcity_report = time.time()

        # §3: No-trade reason counts
        self.notrade_reason_counts: Dict[str, int] = {r: 0 for r in NOTRADE_REASONS}

        # §4: Bucket occupancy tracking
        self.bucket_occupancy: Dict[str, dict] = {
            name: {"seconds_observed": 0, "scan_count": 0, "momentum_scans": 0,
                    "survivability_passes": 0, "trades": 0}
            for name in BUCKET_RANGES
        }

        # §5: Eligible/preferred bucket seconds
        self.eligible_bucket_seconds = 0.0
        self.preferred_bucket_seconds = 0.0

        # §6: Near-miss tracking
        self.near_miss_count = 0
        self.protective_gate_blocks = 0
        self.protective_gate_log = []
        # §V21.7.3: Adjacent bucket shadow diagnostics
        self.adjacent_bucket_shadow_count = 0
        self.adjacent_bucket_resolved = 0
        self.adjacent_bucket_ev = {}
        self.bucket_flash_log = []  # track near-primary bucket flash durations
        self.current_bucket_flash = {}  # active flash tracking per slug
        self.bucket_flash_missed_latency = 0
        self.near_miss_log_path = OUTPUT_DIR / "near_miss_log.jsonl"

        # §7: Scarcity report output
        self.scarcity_report_path = OUTPUT_DIR / "bucket_scarcity_report.json"

        # ═══════════════════════════════════════════════════════════════
        # §2-6: SPOT FEED + SHADOW FORENSICS
        # ═══════════════════════════════════════════════════════════════
        self.shadow_counterfactual = {
            "total_eligible_scans": 0,
            "current_state_momentum": 0,
            "shadow_momentum": 0,
            "both_momentum": 0,
            "current_only": 0,
            "shadow_only": 0,
            "neither": 0,
            "disagreement_rate": 0.0,
        }
        self.forensics_log_path = FORENSICS_FILE
        self.audit_csv_path = ELIGIBLE_AUDIT_CSV
        self.shadow_cf_path = SHADOW_COUNTERFACTUAL_FILE

        # Initialize CSV audit file with header
        try:
            with open(self.audit_csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "market_slug", "interval", "down_ask", "down_bid", "down_mid",
                    "bucket", "state_current", "state_shadow", "survivability_score",
                    "spot_now", "spot_15s_ago", "spot_30s_ago", "spot_60s_ago",
                    "velocity_15s_pct", "velocity_30s_pct", "velocity_60s_pct",
                    "perp_velocity_15s", "perp_velocity_30s", "perp_velocity_60s",
                    "reference_price", "distance_from_ref_pct",
                    "distance_delta_15s", "distance_delta_30s",
                    "token_down_ask_delta_15s", "token_down_ask_delta_30s",
                    "vol_imbalance", "ask_heavy", "spread_pct",
                    "time_to_expiry", "near_miss_criteria_met",
                    "would_trade_current", "would_trade_shadow",
                ])
            log.info(f"Audit CSV initialized: {self.audit_csv_path}")
        except Exception as e:
            log.warning(f"Failed to init audit CSV: {e}")

        # ═══════════════════════════════════════════════════════════════
        # §4: ARMED SCANNER STATE
        # ═══════════════════════════════════════════════════════════════
        self.armed = False
        self.armed_since = 0.0
        self.armed_expire = 0.0
        self.armed_activations = 0
        self.armed_total_seconds = 0.0
        self.armed_scans = 0
        self.near_entry_events = 0
        self.eligible_flashes_seen = 0
        self.eligible_flashes_missed = 0

        # ═══════════════════════════════════════════════════════════════
        # §4: LATENCY TELEMETRY
        # ═══════════════════════════════════════════════════════════════
        self.latency_records: List[dict] = []
        self.latency_report_path = LATENCY_REPORT_FILE
        self.last_latency_report = time.time()
        self.last_forensics_report = time.time()
        self.last_armed_report = time.time()

    def initialize(self) -> bool:
        """Check wallet, auth, and discover markets."""
        log.info("=" * 60)
        log.info("V21.7.1 LIVE DEPLOYMENT — INITIALIZATION")
        log.info("=" * 60)

        # Wallet check
        self.wallet_info = check_wallet()
        log.info(f"Wallet: {self.wallet_info.get('address', 'UNKNOWN')}")
        log.info(f"  USDC: ${self.wallet_info.get('usdc_total', 0):.2f}")
        log.info(f"  On-chain USDC: ${self.wallet_info.get('usdc_total', 0):.2f}")
        log.info(f"  Tradeable bankroll: ${BANKROLL_ACTUAL:.2f}")
        log.info(f"  Position size: ${POSITION_SIZE:.2f}")

        if not self.wallet_info.get('collateral_ready', False):
            log.warning("⚠️  Collateral NOT ready — running in PAPER mode")
            self.paper_mode = True

        # Auth check
        creds = derive_api_credentials()
        if "error" in creds:
            log.error(f"Auth FAILED: {creds['error']}")
            log.warning("⚠️  Running in PAPER mode — no CLOB access")
            self.paper_mode = True
        else:
            log.info(f"Auth: {creds.get('mode')} wallet={creds.get('wallet', '')[:10]}...")

        self.state.bankroll = BANKROLL_ACTUAL  # User-confirmed tradeable amount
        self.state.paper_only = self.paper_mode

        log.info(f"\nConfiguration:")
        log.info(f"  Mode: {'PAPER' if self.paper_mode else 'LIVE'}")
        log.info(f"  Side: {LIVE_PROFILE['side']}")
        log.info(f"  State: {LIVE_PROFILE['state']}")
        log.info(f"  Route: {LIVE_PROFILE['route']}")
        log.info(f"  Bucket: ${LIVE_PROFILE['bucket_primary'][0]:.2f}-${LIVE_PROFILE['bucket_primary'][1]:.2f}")
        log.info(f"  Preferred: ${LIVE_PROFILE['bucket_preferred'][0]:.2f}-${LIVE_PROFILE['bucket_preferred'][1]:.2f}")
        log.info(f"  Position: ${POSITION_SIZE:.2f}")
        log.info(f"  Max trades: {MAX_TOTAL_LIVE_TRADES}")
        log.info(f"  Max daily loss: ${MAX_DAILY_LOSS:.2f}")
        log.info(f"  Max weekly loss: ${MAX_WEEKLY_LOSS:.2f}")
        log.info(f"  Max consecutive losses: {MAX_CONSECUTIVE_LOSSES}")

        # Save initial state
        self._save_state()
        log.info("✓ Initialization complete")
        return True

    def discover_markets(self) -> Dict[str, dict]:
        """Discover active BTC 5m and 15m markets."""
        discovered = {}
        for asset in LIVE_PROFILE["asset"]:
            for interval in LIVE_PROFILE["intervals"]:
                slug_key = f"{asset}_{interval}"
                log.info(f"Discovering {asset} {interval} market...")
                contract = discover_active_contract(asset, interval)
                if contract:
                    log.info(f"  Found: {contract.get('slug', 'unknown')}")
                    log.info(f"  Expires in: {contract.get('expires_in_sec', 0):.0f}s")
                    log.info(f"  NegRisk: {contract.get('negRisk', False)}")
                    discovered[slug_key] = contract
                    self.active_contracts[slug_key] = contract
                else:
                    log.warning(f"  No active contract for {asset} {interval}")
        return discovered

    def check_kill_switches(self, proposed_pnl: float = 0.0) -> Tuple[bool, str]:
        """§7: Check all kill switch conditions. Returns (allowed, reason)."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

        # Reset daily counters
        if self.state.last_daily_reset != today:
            self.state.daily_loss = 0.0
            self.state.daily_trades = 0
            self.state.last_daily_reset = today

        # §11: Hard failure conditions — immediate PAPER reversion
        if self.state.settlement_errors > 0:
            self.state.halted = True
            self.state.halt_reason = f"SETTLEMENT_ERROR_COUNT={self.state.settlement_errors}"
            self._write_incident("settlement_error", self.state.halt_reason)
            return False, self.state.halt_reason

        if self.state.accounting_errors > 0:
            self.state.halted = True
            self.state.halt_reason = f"ACCOUNTING_ERROR_COUNT={self.state.accounting_errors}"
            self._write_incident("accounting_error", self.state.halt_reason)
            return False, self.state.halt_reason

        if self.state.stale_fill_errors >= 3:
            self.state.halted = True
            self.state.halt_reason = f"STALE_FILL_COUNT={self.state.stale_fill_errors}"
            self._write_incident("stale_fill", self.state.halt_reason)
            return False, self.state.halt_reason

        if self.state.api_execution_errors >= 3:
            self.state.halted = True
            self.state.halt_reason = f"API_EXECUTION_ERROR_COUNT={self.state.api_execution_errors}"
            self._write_incident("api_execution_error", self.state.halt_reason)
            return False, self.state.halt_reason

        if self.state.duplicate_position_errors > 0:
            self.state.halted = True
            self.state.halt_reason = f"DUPLICATE_POSITION_ERROR={self.state.duplicate_position_errors}"
            self._write_incident("duplicate_position", self.state.halt_reason)
            return False, self.state.halt_reason

        # §4: Loss limits
        if self.state.daily_loss + proposed_pnl <= -MAX_DAILY_LOSS:
            return False, f"DAILY_LOSS_LIMIT ${self.state.daily_loss:.2f}"

        if self.state.weekly_loss + proposed_pnl <= -MAX_WEEKLY_LOSS:
            return False, f"WEEKLY_LOSS_LIMIT ${self.state.weekly_loss:.2f}"

        if self.state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.state.halted = True
            self.state.halt_reason = f"CONSECUTIVE_LOSSES_{self.state.consecutive_losses}"
            self._write_incident("max_consecutive_losses", self.state.halt_reason)
            return False, self.state.halt_reason

        if self.state.total_trades >= MAX_TOTAL_LIVE_TRADES:
            return False, f"MAX_TRADES_REACHED_{self.state.total_trades}"

        if self.state.daily_trades >= MAX_DAILY_TRADES:
            return False, "MAX_DAILY_TRADES"

        if len(self.active_positions) >= MAX_CONCURRENT:
            return False, "MAX_CONCURRENT_POSITIONS"

        # §11: Rolling EV check
        if len(self.state.running_pnl_list) >= 100:
            last_100 = self.state.running_pnl_list[-100:]
            rolling_ev = sum(last_100) / len(last_100)
            if rolling_ev < 0:
                self.state.halted = True
                self.state.halt_reason = f"ROLLING_EV_NEGATIVE_{rolling_ev:.4f}"
                self._write_incident("negative_rolling_ev", self.state.halt_reason)
                return False, self.state.halt_reason

            # PF check on last 100
            gross_profit = sum(p for p in last_100 if p > 0)
            gross_loss = abs(sum(p for p in last_100 if p < 0))
            rolling_pf = gross_profit / max(gross_loss, 0.01)
            if rolling_pf < 1.0:
                self.state.halted = True
                self.state.halt_reason = f"ROLLING_PF_BELOW_1_{rolling_pf:.2f}"
                self._write_incident("pf_below_1", self.state.halt_reason)
                return False, self.state.halt_reason

        return True, "OK"

    def _write_eligible_forensics(self, slug_key, contract, down_mid, down_ob,
                                    state, survivability, sig_info, spot_vel,
                                    shadow_result, expires_in, has_position,
                                    token_delta=None, latency_info=None):
        """§2+§6: Write full state computation forensics when DOWN ask is in eligible bucket (3–12¢)."""
        try:
            ts = datetime.now(timezone.utc).isoformat()
            interval = slug_key.split("_")[-1] if "_" in slug_key else "unknown"

            # Full forensics JSON (append)
            forensics_entry = {
                "timestamp": ts,
                "market_slug": slug_key,
                "interval": interval,
                "down_ask": down_ob.get("best_ask", 0),
                "down_bid": down_ob.get("best_bid", 0),
                "down_mid": down_mid,
                "bucket": f"{down_mid:.3f}",
                "state_current": state,
                "survivability_score": survivability,
                "survivability_components": {
                    "expected_ev": sig_info.get("expected_ev", 0),
                    "fill_probability": sig_info.get("fill_probability", 0),
                    "slippage_survival": sig_info.get("slippage_survival", 0),
                    "payout_asymmetry": sig_info.get("payout_asymmetry", 0),
                    "bucket_weight": sig_info.get("bucket_weight", 0),
                },
                "why_momentum_failed": "",
                "orderbook_signal": {
                    "vol_imbalance": sig_info.get("vol_imbalance", 0),
                    "ask_heavy": sig_info.get("total_ask_vol", 0) > sig_info.get("total_bid_vol", 0) * 1.2,
                    "spread_pct": sig_info.get("spread_pct", 0),
                    "total_bid_vol": sig_info.get("total_bid_vol", 0),
                    "total_ask_vol": sig_info.get("total_ask_vol", 0),
                },
                "spot_data": {
                    "btc_spot_now": spot_vel.get("spot_now", 0),
                    "btc_spot_15s": spot_vel.get("spot_15s", 0),
                    "btc_spot_30s": spot_vel.get("spot_30s", 0),
                    "btc_spot_60s": spot_vel.get("spot_60s", 0),
                    "velocity_15s_pct": spot_vel.get("velocity_15s", 0),
                    "velocity_30s_pct": spot_vel.get("velocity_30s", 0),
                    "velocity_60s_pct": spot_vel.get("velocity_60s", 0),
                    "perp_velocity_15s": spot_vel.get("perp_velocity_15s", 0),
                    "perp_velocity_30s": spot_vel.get("perp_velocity_30s", 0),
                    "perp_velocity_60s": spot_vel.get("perp_velocity_60s", 0),
                    "reference_price": spot_vel.get("reference_price", 0),
                    "distance_from_ref_pct": spot_vel.get("distance_from_ref", 0),
                    "distance_delta_15s": spot_vel.get("distance_delta_15s", 0),
                    "distance_delta_30s": spot_vel.get("distance_delta_30s", 0),
                },
                "token_delta": token_delta or {},
                "latency_ms": latency_info or {},
                "shadow_state": shadow_result.get("shadow_state", ""),
                "shadow_momentum": shadow_result.get("shadow_momentum", False),
                "shadow_reason": shadow_result.get("reason", ""),
                "shadow_checks": shadow_result.get("checks", {}),
                "time_to_expiry": expires_in,
                "near_miss_criteria_met": self._count_near_miss_criteria(
                    down_mid, state, survivability, expires_in,
                    down_ob is not None, has_position
                ),
                "would_trade_current": state in ("DOWN_MOMENTUM", "DOWN_CONTINUATION") and survivability >= 0.05,
                "would_trade_shadow": shadow_result.get("shadow_momentum", False),
            }

            # Determine why MOMENTUM failed
            if state not in ("DOWN_MOMENTUM", "DOWN_CONTINUATION"):
                reasons = []
                if not (sig_info.get("in_primary", False)):
                    reasons.append("not_in_primary_bucket")
                if sig_info.get("spread_pct", 1) >= 0.25:
                    reasons.append("spread_too_wide")
                ask_vol = sig_info.get("total_ask_vol", 0)
                bid_vol = sig_info.get("total_bid_vol", 0)
                if not (ask_vol > bid_vol * 1.2):
                    reasons.append("ask_not_heavy")
                if sig_info.get("vol_imbalance", 0) > -0.1:
                    reasons.append("vol_imbalance_not_bearish")
                if survivability < 0.05:
                    reasons.append(f"low_survivability={survivability:.4f}")
                forensics_entry["why_momentum_failed"] = " | ".join(reasons) if reasons else "unknown"

            with open(self.forensics_log_path, "a") as f:
                f.write(json.dumps(forensics_entry) + "\n")

            # CSV audit (append)
            try:
                with open(self.audit_csv_path, "a", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        ts, slug_key, interval,
                        f"{down_ob.get('best_ask', 0):.4f}",
                        f"{down_ob.get('best_bid', 0):.4f}",
                        f"{down_mid:.4f}",
                        f"{down_mid:.3f}",
                        state, shadow_result.get("shadow_state", ""),
                        f"{survivability:.4f}",
                        spot_vel.get("spot_now", 0),
                        spot_vel.get("spot_15s", 0),
                        spot_vel.get("spot_30s", 0),
                        spot_vel.get("spot_60s", 0),
                        f"{spot_vel.get('velocity_15s', 0):.6f}",
                        f"{spot_vel.get('velocity_30s', 0):.6f}",
                        f"{spot_vel.get('velocity_60s', 0):.6f}",
                        f"{spot_vel.get('perp_velocity_15s', 0):.6f}",
                        f"{spot_vel.get('perp_velocity_30s', 0):.6f}",
                        f"{spot_vel.get('perp_velocity_60s', 0):.6f}",
                        spot_vel.get("reference_price", 0),
                        f"{spot_vel.get('distance_from_ref', 0):.6f}",
                        f"{spot_vel.get('distance_delta_15s', 0):.6f}",
                        f"{spot_vel.get('distance_delta_30s', 0):.6f}",
                        f"{(token_delta or {}).get('ask_delta_15s', 0):.6f}",
                        f"{(token_delta or {}).get('ask_delta_30s', 0):.6f}",
                        f"{sig_info.get('vol_imbalance', 0):.4f}",
                        sig_info.get("total_ask_vol", 0) > sig_info.get("total_bid_vol", 0) * 1.2,
                        f"{sig_info.get('spread_pct', 0):.4f}",
                        f"{expires_in:.0f}",
                        forensics_entry["near_miss_criteria_met"],
                        forensics_entry["would_trade_current"],
                        forensics_entry["would_trade_shadow"],
                    ])
            except Exception as e:
                log.debug(f"CSV write failed: {e}")

        except Exception as e:
            log.warning(f"Forensics write failed: {e}")

    def _classify_no_trade(self, slug_key: str, contract: dict, down_mid: float,
                            state: str, survivability: float, expires_in: float,
                            has_orderbook: bool, has_position: bool, kill_allowed: bool,
                            kill_reason: str) -> Tuple[str, List[str]]:
        """§3: Classify why no trade fired this scan cycle."""
        primary = "no_active_market"
        secondary = []

        if not contract:
            primary = "no_active_market"
        elif not has_orderbook:
            primary = "stale_quote"
            secondary.append("no_book")
        elif down_mid < 0.03:
            primary = "bucket_below_floor"
        elif down_mid >= 0.12:
            primary = "bucket_above_cap"
        elif expires_in < 30:
            primary = "too_near_expiry"
        elif has_position:
            primary = "duplicate_position"
        elif not kill_allowed:
            primary = "risk_limit_block"
            secondary.append(kill_reason)
        elif state not in ("DOWN_MOMENTUM", "DOWN_CONTINUATION"):
            if state == "NO_SIGNAL":
                primary = "wrong_state"
            elif state == "SPREAD_TOO_WIDE":
                primary = "spread_too_wide"
            elif state == "NO_MOMENTUM":
                primary = "no_momentum"
            else:
                primary = "wrong_state"
                secondary.append(state)
        elif survivability < 0.05:
            primary = "low_survivability"

        self.notrade_reason_counts[primary] = self.notrade_reason_counts.get(primary, 0) + 1
        for s in secondary:
            self.notrade_reason_counts[s] = self.notrade_reason_counts.get(s, 0) + 1

        return primary, secondary

    def _record_protective_gate(self, slug_key: str, contract: dict, down_mid: float,
                                block_reason: str, spot_vel: dict, regime: str,
                                expires_in: float):
        """§V21.7.2: Record protective gate events for accounting.
        
        Tracks every blocked eligible event that the state gate vetoes,
        enabling measurement of protective gate value over time.
        Settlement tracking: after market expires, resolve blocked events
        to compute protective gate value (§7).
        """
        self.protective_gate_blocks += 1
        
        # Extract interval from slug (e.g., btc-updown-5m-xxx → 5m)
        interval = "unknown"
        slug = contract.get("slug", slug_key) if contract else slug_key
        if "-5m-" in slug:
            interval = "5m"
        elif "-15m-" in slug:
            interval = "15m"
        
        # Extract down ask from orderbook if available
        down_ask = 0.0
        
        # Bucket classification
        if 0.05 <= down_mid < 0.08:
            entry_bucket = "PREFERRED"
        elif 0.03 <= down_mid < 0.12:
            entry_bucket = "PRIMARY"
        else:
            entry_bucket = "OUTSIDE"
        
        cid = contract.get("conditionId", "") if contract else ""
        expiry_ts = time.time() + expires_in if expires_in > 0 else 0
        
        entry = {
            "blocked_event_id": f"PG-{slug_key}-{int(time.time())}",
            "market_slug": slug,
            "interval": interval,
            "condition_id": cid,
            "down_ask": down_ask,
            "entry_price": down_mid,
            "entry_bucket": entry_bucket,
            "blocked_reason": block_reason,
            "would_have_entered_side": "DOWN",
            "v15": spot_vel.get("velocity_15s", 0),
            "v30": spot_vel.get("velocity_30s", 0),
            "v60": spot_vel.get("velocity_60s", 0),
            "perp_v15": spot_vel.get("perp_velocity_15s", 0),
            "perp_v30": spot_vel.get("perp_velocity_30s", 0),
            "higher_timeframe_regime": regime,
            "time_to_expiry": expires_in,
            "expiry_timestamp": expiry_ts,
            "resolved": False,
            "would_have_won": None,
            "hypothetical_pnl": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.protective_gate_log.append(entry)
        # Write to JSONL file
        gate_path = OUTPUT_DIR / "protective_gate_accounting.jsonl"
        with open(gate_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _resolve_protective_gate_events(self):
        """§7: Resolve expired blocked events via CLOB market API.
        
        For each unresolved protective gate event whose market has expired,
        query the market outcome (binary settlement) and compute
        whether the bot would have won or lost.
        """
        now = time.time()
        resolved_count = 0
        for entry in self.protective_gate_log:
            if entry.get("resolved", False):
                continue
            expiry_ts = entry.get("expiry_timestamp", 0)
            if expiry_ts <= 0 or now < expiry_ts:
                continue  # not yet expired
            
            slug = entry.get("market_slug", "")
            cid = entry.get("condition_id", "")
            entry_price = entry.get("entry_price", 0)
            
            # Query CLOB/Gamma API for market outcome
            winner = None
            try:
                url = f"{GAMMA_URL}/markets?limit=1&slug={slug}"
                req = urllib.request.Request(url, headers={"User-Agent": "FDC-V21.7.2"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read())
                if data and len(data) > 0:
                    market = data[0]
                    outcome = market.get("outcome", "")
                    outcomePrices = market.get("outcomePrices", "")
                    if outcome == "down" or (outcomePrices and isinstance(outcomePrices, str)):
                        prices = outcomePrices.split(",") if isinstance(outcomePrices, str) else []
                        if len(prices) >= 2:
                            # DOWN is index 1 (0=UP, 1=DOWN typically)
                            down_final = float(prices[1]) if prices[1] else 0.0
                            winner = "DOWN" if down_final > 0.5 else "UP"
                    if not winner:
                        # Fallback: check if market is closed
                        closed = market.get("closed", False)
                        active = market.get("active", True)
                        if closed or not active:
                            # Check resolution from outcome field
                            if outcome.lower() == "down":
                                winner = "DOWN"
                            elif outcome.lower() == "up":
                                winner = "UP"
            except Exception as e:
                log.warning(f"Protective gate resolution failed for {slug}: {e}")
                continue
            
            if winner is None:
                # Market might not be resolved yet, try condition_id fallback
                continue
            
            # Binary settlement
            would_have_won = winner == "DOWN"
            if would_have_won:
                hypothetical_pnl = (1.0 - entry_price) * 1.0  # $1 position, win payout
            else:
                hypothetical_pnl = -entry_price * 1.0  # $1 position, lose
            
            entry["resolved"] = True
            entry["resolved_winner"] = winner
            entry["would_have_won"] = would_have_won
            entry["hypothetical_pnl"] = round(hypothetical_pnl, 4)
            entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
            resolved_count += 1
        
        if resolved_count > 0:
            log.info(f"✓ Protective gate: resolved {resolved_count} blocked events")
            # Rewrite the JSONL with updated entries
            gate_path = OUTPUT_DIR / "protective_gate_accounting.jsonl"
            with open(gate_path, "w") as f:
                for entry in self.protective_gate_log:
                    f.write(json.dumps(entry) + "\n")
    
    def _generate_protective_gate_summary(self):
        """§8: Rolling protective gate summary every 30 min."""
        resolved = [e for e in self.protective_gate_log if e.get("resolved", False)]
        unresolved = [e for e in self.protective_gate_log if not e.get("resolved", False)]
        
        protected_losses = sum(1 for e in resolved if not e.get("would_have_won", True))
        missed_wins = sum(1 for e in resolved if e.get("would_have_won", False))
        
        # PnL avoided: sum of hypothetical losses avoided (positive = good)
        loss_avoided = sum(-e["hypothetical_pnl"] for e in resolved 
                         if not e.get("would_have_won", True) and e.get("hypothetical_pnl") is not None)
        win_missed = sum(e["hypothetical_pnl"] for e in resolved 
                        if e.get("would_have_won", False) and e.get("hypothetical_pnl") is not None)
        net_avoided_pnl = loss_avoided - win_missed
        
        # Block reason counts
        reason_counts = {}
        for e in self.protective_gate_log:
            reason = e.get("blocked_reason", "unknown")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        
        top_reason = max(reason_counts, key=reason_counts.get) if reason_counts else "none"
        
        runtime_s = (time.time() - self.start_time.timestamp()) if hasattr(self, 'start_time') else 0
        
        summary = {
            "runtime_minutes": round(runtime_s / 60, 1),
            "eligible_bucket_flashes": self.eligible_flashes_seen,
            "protective_gate_blocks": self.protective_gate_blocks,
            "protected_losses": protected_losses,
            "missed_wins": missed_wins,
            "pending_blocked_events": len(unresolved),
            "net_avoided_pnl": round(net_avoided_pnl, 4),
            "top_block_reason": top_reason,
            "uptrend_regime_filter_count": reason_counts.get("uptrend_regime_filter", 0),
            "fake_short_term_dip_count": reason_counts.get("fake_short_term_dip_no_sustained_downtrend", 0),
            "insufficient_sustained_velocity_count": reason_counts.get("insufficient_sustained_down_velocity", 0),
            "low_survivability_count": reason_counts.get("low_survivability", 0),
            "spread_block_count": reason_counts.get("spread_too_wide", 0),
            "classification": "STATE_GATE_PROTECTIVE_SHADOW_REJECTED",
        }
        
        summary_path = OUTPUT_DIR / "protective_gate_summary.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        
        log.info(f"🛡️ Protective Gate Summary: {self.protective_gate_blocks} blocked | "
                 f"{protected_losses}L/{missed_wins}W resolved | "
                 f"net_avoided=${net_avoided_pnl:.2f} | top={top_reason}")
    
    ADJACENT_BUCKET_RANGES = {
        "SUB_FLOOR":       (0.00,  0.03),
        "PRIMARY_LOW":     (0.03,  0.05),
        "PRIMARY_PREFERRED": (0.05, 0.08),
        "PRIMARY_HIGH":    (0.08,  0.12),
        "ADJACENT_HIGH":   (0.12,  0.20),
        "MIDRANGE_BLOCKED": (0.20, 0.40),
        "CONVEXITY_GONE":  (0.40,  1.01),
    }

    def _classify_bucket_v2173(self, down_mid: float) -> str:
        """§4: Classify into V21.7.3 adjacent bucket labels."""
        for label, (lo, hi) in self.ADJACENT_BUCKET_RANGES.items():
            if lo <= down_mid < hi:
                return label
        return "UNKNOWN"

    def _log_adjacent_bucket_shadow(self, slug_key, contract, down_mid, down_ob,
                                     spot_vel, state, survivability, expires_in,
                                     has_position, phase_timings):
        """§4: Log shadow observations across ALL bucket ranges.
        
        Samples 1-in-10 to limit log volume. Logs full details for
        near-primary (0-20¢) buckets; minimal for others.
        """
        bucket_label = self._classify_bucket_v2173(down_mid)
        
        # Log near-primary (0-20¢) every scan; sample others 1-in-5
        if down_mid >= 0.20 and self.adjacent_bucket_shadow_count % 5 != 0:
            return
        
        self.adjacent_bucket_shadow_count += 1
        
        # Determine live decision for this observation
        if 0.03 <= down_mid < 0.12:
            current_live_decision = "ELIGIBLE"
        else:
            current_live_decision = "BLOCKED_BUCKET"
        
        # Would shadow trade this? (relaxed criteria)
        would_trade_shadow = False
        blocked_reason = ""
        v15 = spot_vel.get("velocity_15s", 0)
        v60 = spot_vel.get("velocity_60s", 0)
        
        if down_mid < 0.005:
            blocked_reason = "dust"
        elif down_mid >= 0.60:
            blocked_reason = "convexity_gone"
        elif not (down_mid < 0.20):  # above adjacent high
            blocked_reason = "outside_adjacent_range"
        else:
            # Shadow would consider: any price 0-20¢ with negative velocity
            if v15 < 0 or v60 < 0:
                would_trade_shadow = True
            else:
                blocked_reason = "no_negative_velocity"
        
        # Regime from spot buffer
        regime = "unknown"
        if len(SPOT_PRICE_BUFFER) >= 20:
            recent = [e["price"] for e in SPOT_PRICE_BUFFER[-20:]]
            trend = (recent[-1] - recent[0]) / max(abs(recent[0]), 1e-9)
            if trend > 0.02:
                regime = "trending_up"
            elif trend < -0.02:
                regime = "trending_down"
            else:
                regime = "ranging"
        
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_slug": contract.get("slug", slug_key) if contract else slug_key,
            "interval": "5m" if "5m" in slug_key else ("15m" if "15m" in slug_key else "unknown"),
            "condition_id": contract.get("conditionId", "") if contract else "",
            "down_bid": down_ob.get("best_bid", 0) if down_ob else 0,
            "down_ask": down_ob.get("best_ask", 0) if down_ob else 0,
            "bucket": bucket_label,
            "time_to_expiry": expires_in,
            "btc_spot": spot_vel.get("spot_now", 0),
            "v15": v15,
            "v30": spot_vel.get("velocity_30s", 0),
            "v60": v60,
            "higher_timeframe_regime": regime,
            "current_live_decision": current_live_decision,
            "blocked_reason": blocked_reason,
            "would_trade_shadow": would_trade_shadow,
            "shadow_bucket_label": bucket_label,
            "expiry_timestamp": time.time() + expires_in if expires_in > 0 else 0,
        }
        
        shadow_log_path = OUTPUT_DIR / "adjacent_bucket_shadow_log.jsonl"
        with open(shadow_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _track_bucket_flash_latency(self, slug_key, contract, down_mid, down_ob,
                                     spot_vel, state, survivability, expires_in,
                                     phase_timings):
        """§7: Track how long DOWN price stays near/in PRIMARY bucket.
        
        Detects when price crosses into 0-20¢ zone, measures duration,
        and flags if the bot is too slow to catch the flash.
        """
        now_ts = time.time()
        near_primary = down_mid < 0.20  # 0-20¢ zone
        
        slug = slug_key
        
        if near_primary:
            if slug not in self.current_bucket_flash:
                # Entering near-primary zone
                self.current_bucket_flash[slug] = {
                    "first_seen_timestamp": now_ts,
                    "bucket_cross_timestamp": now_ts,
                    "last_seen_timestamp": now_ts,
                    "entry_price_at_cross": down_mid,
                    "bucket_label": self._classify_bucket_v2173(down_mid),
                }
            else:
                # Still in near-primary zone — update last_seen
                self.current_bucket_flash[slug]["last_seen_timestamp"] = now_ts
                self.current_bucket_flash[slug]["current_price"] = down_mid
                self.current_bucket_flash[slug]["current_bucket"] = self._classify_bucket_v2173(down_mid)
        else:
            # Left near-primary zone
            if slug in self.current_bucket_flash:
                flash = self.current_bucket_flash.pop(slug)
                duration_ms = (now_ts - flash["first_seen_timestamp"]) * 1000
                
                # Was this a valid live state that was missed?
                missed = False
                if flash["entry_price_at_cross"] >= 0.03 and flash["entry_price_at_cross"] < 0.12:
                    # PRIMARY bucket flash — would live trade if state passed
                    book_lat = phase_timings.get("book_fetch_latency_ms", 0)
                    spot_lat = phase_timings.get("spot_fetch_latency_ms", 0)
                    scan_lat = phase_timings.get("scan_latency_ms", 0)
                    if duration_ms < self.scan_interval * 1000:
                        missed = True
                        self.bucket_flash_missed_latency += 1
                
                flash_entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market_slug": contract.get("slug", slug_key) if contract else slug_key,
                    "bucket_cross_timestamp": flash["bucket_cross_timestamp"],
                    "first_seen_timestamp": flash["first_seen_timestamp"],
                    "last_seen_timestamp": flash["last_seen_timestamp"],
                    "duration_in_bucket_ms": round(duration_ms, 1),
                    "entry_price_at_cross": flash["entry_price_at_cross"],
                    "bucket_label": flash["bucket_label"],
                    "scan_interval_ms": self.scan_interval * 1000,
                    "book_fetch_latency_ms": phase_timings.get("book_fetch_latency_ms", 0),
                    "spot_fetch_latency_ms": phase_timings.get("spot_fetch_latency_ms", 0),
                    "decision_latency_ms": phase_timings.get("scan_latency_ms", 0),
                    "quote_age_ms": phase_timings.get("quote_age_ms", 0),
                    "would_live_trade_if_state_passed": flash["entry_price_at_cross"] >= 0.03 and flash["entry_price_at_cross"] < 0.12,
                    "missed_due_to_latency": missed,
                }
                self.bucket_flash_log.append(flash_entry)
                
                # Write to file
                flash_path = OUTPUT_DIR / "bucket_flash_latency_log.jsonl"
                with open(flash_path, "a") as f:
                    f.write(json.dumps(flash_entry) + "\n")

    def _resolve_adjacent_bucket_settlements(self):
        """§5: Resolve expired adjacent bucket shadow events via Gamma API.
        
        Binary settlement only. No midpoint. No synthetic close.
        """
        now = time.time()
        shadow_log_path = OUTPUT_DIR / "adjacent_bucket_shadow_log.jsonl"
        if not shadow_log_path.exists():
            return
        
        # Load existing settlements
        settlement_path = OUTPUT_DIR / "adjacent_bucket_shadow_settlements.jsonl"
        settled_ids = set()
        if settlement_path.exists():
            for line in open(settlement_path):
                s = json.loads(line)
                settled_ids.add(s.get("event_id", ""))
        
        # Read shadow log entries that need resolution
        to_resolve = []
        all_entries = []
        for line in open(shadow_log_path):
            e = json.loads(line)
            all_entries.append(e)
            eid = e.get("timestamp", "") + e.get("market_slug", "")
            if eid in settled_ids:
                continue
            exp_ts = e.get("expiry_timestamp", 0)
            if exp_ts <= 0 or now < exp_ts:
                continue
            # Only settle if shadow would have traded
            if not e.get("would_trade_shadow", False):
                continue
            to_resolve.append(e)
        
        if not to_resolve:
            return
        
        resolved_count = 0
        for entry in to_resolve:
            slug = entry.get("market_slug", "")
            entry_price = entry.get("down_ask", 0) or entry.get("down_bid", 0)
            if entry_price <= 0:
                entry_price = 0.10  # fallback estimate
            bucket = entry.get("bucket", "UNKNOWN")
            eid = entry.get("timestamp", "") + slug
            
            winner = None
            try:
                url = f"{GAMMA_URL}/markets?limit=1&slug={slug}"
                req = urllib.request.Request(url, headers={"User-Agent": "FDC-V21.7.3"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read())
                if data and len(data) > 0:
                    m = data[0]
                    outcome = m.get("outcome", "").lower()
                    prices_str = m.get("outcomePrices", "")
                    if prices_str and isinstance(prices_str, str):
                        prices = prices_str.split(",")
                        if len(prices) >= 2:
                            down_final = float(prices[1]) if prices[1] else 0.0
                            winner = "DOWN" if down_final > 0.5 else "UP"
                    if not winner and (m.get("closed", False) or not m.get("active", True)):
                        if outcome == "down":
                            winner = "DOWN"
                        elif outcome == "up":
                            winner = "UP"
            except Exception:
                continue
            
            if winner is None:
                continue
            
            win_loss = "WIN" if winner == "DOWN" else "LOSS"
            gross_pnl = (1.0 - entry_price) * 1.0 if winner == "DOWN" else -entry_price * 1.0
            slip_adj = gross_pnl * 0.98 if gross_pnl > 0 else gross_pnl  # 2% slippage on wins
            
            settlement = {
                "event_id": eid,
                "market_slug": slug,
                "condition_id": entry.get("condition_id", ""),
                "interval": entry.get("interval", ""),
                "entry_bucket": bucket,
                "entry_price": entry_price,
                "selected_side": "DOWN",
                "expiry_timestamp": entry.get("expiry_timestamp", 0),
                "resolved_winner": winner,
                "win_loss": win_loss,
                "gross_pnl": round(gross_pnl, 4),
                "slippage_adjusted_pnl": round(slip_adj, 4),
                "settlement_source": "gamma_api",
                "settlement_error": "",
            }
            
            with open(settlement_path, "a") as f:
                f.write(json.dumps(settlement) + "\n")
            
            # Track EV per bucket
            if bucket not in self.adjacent_bucket_ev:
                self.adjacent_bucket_ev[bucket] = {"wins": 0, "losses": 0, "pnl": 0.0}
            if winner == "DOWN":
                self.adjacent_bucket_ev[bucket]["wins"] += 1
            else:
                self.adjacent_bucket_ev[bucket]["losses"] += 1
            self.adjacent_bucket_ev[bucket]["pnl"] += slip_adj
            
            resolved_count += 1
        
        if resolved_count > 0:
            self.adjacent_bucket_resolved += resolved_count
            log.info(f"✓ Adjacent bucket: resolved {resolved_count} shadow settlements")

    def _generate_v2173_report(self):
        """§11: Rolling V21.7.3 restrictiveness + speed report."""
        now = time.time()
        runtime_s = now - self.start_time.timestamp()
        
        # Latency stats
        book_lats = [r.get("book_fetch_latency_ms", 0) for r in self.latency_records if r.get("book_fetch_latency_ms")]
        quote_ages = [r.get("quote_age_at_submit_ms", 0) for r in self.latency_records if r.get("quote_age_at_submit_ms")]
        scan_lats = [r.get("scan_latency_ms", 0) for r in self.latency_records if r.get("scan_latency_ms")]
        
        def p95(data):
            if not data: return 0
            s = sorted(data)
            idx = int(len(s) * 0.95)
            return round(s[min(idx, len(s)-1)], 1)
        
        # Bucket flash stats
        flash_durations = [f.get("duration_in_bucket_ms", 0) for f in self.bucket_flash_log]
        median_flash = 0
        if flash_durations:
            sf = sorted(flash_durations)
            median_flash = sf[len(sf)//2]
        
        # Bucket distribution
        bucket_dist = {}
        for bname, bdata in self.bucket_occupancy.items():
            bucket_dist[bname] = bdata.get("scan_count", 0)
        
        # Adjacent bucket EV
        bucket_ev_summary = {}
        for bucket, stats in self.adjacent_bucket_ev.items():
            total = stats["wins"] + stats["losses"]
            ev = stats["pnl"] / max(total, 1)
            bucket_ev_summary[bucket] = {
                "total": total,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "pnl": round(stats["pnl"], 4),
                "ev_per_trade": round(ev, 4),
            }
        
        report = {
            "runtime_minutes": round(runtime_s / 60, 1),
            "scans": sum(bucket_dist.values()),
            "live_trades": self.state.total_trades,
            "bankroll": round(self.state.bankroll, 2),
            "bucket_distribution": bucket_dist,
            "primary_bucket_seconds": round(self.eligible_bucket_seconds, 0),
            "adjacent_bucket_seconds": round(
                sum(bd.get("seconds_observed", 0) for bd in self.bucket_occupancy.values()
                    if bd.get("seconds_observed", 0) > 0), 0),
            "bucket_flash_count": len(self.bucket_flash_log),
            "median_bucket_flash_duration_ms": round(median_flash, 1),
            "p95_book_fetch_latency_ms": p95(book_lats),
            "p95_quote_age_ms": p95(quote_ages),
            "p95_decision_latency_ms": p95(scan_lats),
            "live_bucket_missed_due_to_latency_count": self.bucket_flash_missed_latency,
            "adjacent_bucket_shadow_count": self.adjacent_bucket_shadow_count,
            "adjacent_bucket_resolved_count": self.adjacent_bucket_resolved,
            "adjacent_bucket_EV": bucket_ev_summary,
            "protective_gate_net_value": round(
                sum(-e.get("hypothetical_pnl", 0) for e in self.protective_gate_log
                    if e.get("resolved") and not e.get("would_have_won", True) 
                    and e.get("hypothetical_pnl") is not None), 4),
            "classification": "V21.7.3_RESTRICTIVENESS_SPEED_DIAGNOSTIC_RUNNING",
        }
        
        report_path = OUTPUT_DIR / "v2173_restrictiveness_speed_report.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        
        log.info(f"📊 V21.7.3 Report: {report['runtime_minutes']:.0f}min | "
                 f"flashes={report['bucket_flash_count']} | "
                 f"median_flash={report['median_bucket_flash_duration_ms']:.0f}ms | "
                 f"missed_latency={report['live_bucket_missed_due_to_latency_count']} | "
                 f"adj_resolved={report['adjacent_bucket_resolved_count']}")
    
    def _update_bucket_occupancy(self, down_mid: float, state: str, survivability: float):
        """§4: Track how much time BTC DOWN spends in each bucket."""
        for bucket_name, (lo, hi) in BUCKET_RANGES.items():
            if lo <= down_mid < hi:
                self.bucket_occupancy[bucket_name]["scan_count"] += 1
                self.bucket_occupancy[bucket_name]["seconds_observed"] += self.scan_interval
                if state in ("DOWN_MOMENTUM", "DOWN_CONTINUATION"):
                    self.bucket_occupancy[bucket_name]["momentum_scans"] += 1
                if survivability >= 0.05:
                    self.bucket_occupancy[bucket_name]["survivability_passes"] += 1
                break

        # §5: Eligible and preferred bucket seconds
        if 0.03 <= down_mid < 0.12:
            self.eligible_bucket_seconds += self.scan_interval
            if 0.05 <= down_mid < 0.08:
                self.preferred_bucket_seconds += self.scan_interval

    def _count_near_miss_criteria(self, down_mid, state, survivability, expires_in,
                                    has_orderbook, has_position) -> int:
        """Count how many near-miss criteria are met (for forensics)."""
        criteria = 0
        if True: criteria += 1  # BTC
        if state in ("DOWN_MOMENTUM", "DOWN_CONTINUATION", "NO_MOMENTUM"): criteria += 1
        if 0.03 <= down_mid < 0.12: criteria += 1
        if survivability >= 0.25 * 0.80: criteria += 1  # within 20% of threshold
        if expires_in > 30: criteria += 1
        if has_orderbook: criteria += 1
        if not has_position: criteria += 1
        return criteria

    def _check_near_miss(self, slug_key: str, contract: dict, down_mid: float,
                          state: str, survivability: float, expires_in: float,
                          has_orderbook: bool, has_position: bool) -> bool:
        """§6: Check if this scan is a near-miss (3+ of 8 criteria met)."""
        criteria_met = 0
        missing = []

        # 1. asset = BTC (always true for V21.7.1)
        if True:
            criteria_met += 1
        else:
            missing.append("wrong_asset")

        # 2. side = DOWN (always true)
        if True:
            criteria_met += 1
        else:
            missing.append("wrong_side")

        # 3. bucket within 0.03-0.12
        if 0.03 <= down_mid < 0.12:
            criteria_met += 1
        else:
            missing.append("outside_bucket")

        # 4. state = MOMENTUM or near-MOMENTUM
        if state in ("DOWN_MOMENTUM", "DOWN_CONTINUATION"):
            criteria_met += 1
        elif state == "NO_MOMENTUM":
            # near-momentum: vol_imbalance exists but not quite threshold
            criteria_met += 0.5  # partial credit
            missing.append("near_momentum")
        else:
            missing.append(f"wrong_state_{state}")

        # 5. survivability within 20% of threshold (>= 0.04)
        if survivability >= 0.04:
            criteria_met += 1
        else:
            missing.append("low_survivability")

        # 6. time_to_expiry > 30s
        if expires_in > 30:
            criteria_met += 1
        else:
            missing.append("too_near_expiry")

        # 7. book is fresh
        if has_orderbook:
            criteria_met += 1
        else:
            missing.append("stale_book")

        # 8. no duplicate position
        if not has_position:
            criteria_met += 1
        else:
            missing.append("duplicate_position")

        is_near_miss = criteria_met >= 3

        if is_near_miss:
            self.near_miss_count += 1
            near_miss_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market_slug": slug_key,
                "interval": contract.get("slug", "").split("-")[-2] if contract else "unknown",
                "down_ask": down_mid,
                "bucket": f"{down_mid:.3f}",
                "state": state,
                "survivability_score": survivability,
                "missing_requirements": missing,
                "criteria_met": criteria_met,
                "would_trade_if_relaxed": "bucket" not in missing and "state" not in missing,
            }
            with open(self.near_miss_log_path, 'a') as f:
                f.write(json.dumps(near_miss_entry, default=str) + "\n")

        return is_near_miss

    def _generate_scarcity_report(self) -> dict:
        """§7: Generate rolling scarcity report."""
        runtime_minutes = (time.time() - self.start_time.timestamp()) / 60
        total_scans = self.cycle_id

        # Determine primary bottleneck
        top_reason = max(self.notrade_reason_counts.keys(), key=lambda k: self.notrade_reason_counts[k])
        if top_reason in ("bucket_below_floor", "bucket_above_cap"):
            primary_bottleneck = "BUCKET_SCARCITY"
        elif top_reason in ("wrong_state", "no_momentum"):
            primary_bottleneck = "STATE_SCARCITY"
        elif top_reason == "low_survivability":
            primary_bottleneck = "SURVIVABILITY_SCARCITY"
        elif top_reason in ("stale_quote", "no_book"):
            primary_bottleneck = "EXECUTION_SCARCITY"
        elif top_reason in ("risk_limit_block", "execution_rejected"):
            primary_bottleneck = "RISK_BLOCK"
        else:
            primary_bottleneck = "UNKNOWN"

        # Bucket distribution percentages
        total_bucket_scans = max(sum(b["scan_count"] for b in self.bucket_occupancy.values()), 1)
        bucket_distribution = {
            name: {
                "percent": round(b["scan_count"] / total_bucket_scans * 100, 2),
                "seconds": b["seconds_observed"],
                "momentum_scans": b["momentum_scans"],
                "survivability_passes": b["survivability_passes"],
                "trades": b["trades"],
            }
            for name, b in self.bucket_occupancy.items()
        }

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "runtime_minutes": round(runtime_minutes, 1),
            "total_scans": total_scans,
            "markets_scanned": len(self.active_contracts),
            "eligible_bucket_seconds": round(self.eligible_bucket_seconds, 1),
            "preferred_bucket_seconds": round(self.preferred_bucket_seconds, 1),
            "bucket_distribution": bucket_distribution,
            "no_trade_reason_counts": dict(self.notrade_reason_counts),
            "near_miss_count": self.near_miss_count,
            "protective_gate_blocks": self.protective_gate_blocks,
            "trade_count": self.state.total_trades,
            "primary_bottleneck": primary_bottleneck,
            "status": "LIVE_RUNNING",
            "scan_frequency": f"{self.scan_interval}s",
            "classification": "STATE_GATE_PROTECTIVE_SHADOW_REJECTED"
                if self.eligible_bucket_seconds < 300 else "MONITORING",
            "shadow_counterfactual": dict(self.shadow_counterfactual),
        }

        with open(self.scarcity_report_path, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        # Save shadow counterfactual separately
        with open(self.shadow_cf_path, 'w') as f:
            json.dump(self.shadow_counterfactual, f, indent=2, default=str)

        log.info(f"📊 Scarcity Report: {runtime_minutes:.0f}min | {total_scans} scans | "
                 f"eligible={self.eligible_bucket_seconds:.0f}s | preferred={self.preferred_bucket_seconds:.0f}s | "
                 f"near_misses={self.near_miss_count} | trades={self.state.total_trades} | "
                 f"bottleneck={primary_bottleneck}")

        return report

    # ═══════════════════════════════════════════════════════════════════
    # §3-4: ARMED SCANNER + LATENCY + ROLLING REPORTS
    # ═══════════════════════════════════════════════════════════════════

    def _check_armed_mode(self, down_mid: float, spot_vel: dict, expires_in: float,
                           shadow_result: dict) -> bool:
        """§3: Check if armed scanner mode should activate.
        Armed mode triggers when any near-entry condition is met:
        - DOWN ask enters 2-15¢ (within 20% of bucket)
        - BTC spot velocity turns sharply negative
        - time_to_expiry < 120s
        - Near-miss cluster detected (3+ near-misses in last 60s)
        """
        now = time.time()
        should_arm = False

        # 1. DOWN ask within 20% of entry bucket
        if 0.02 <= down_mid <= 0.15:
            should_arm = True

        # 2. BTC spot velocity sharply negative (< -0.05% on any horizon)
        if spot_vel.get("has_spot", False):
            for key in ("velocity_15s", "velocity_30s", "velocity_60s"):
                if spot_vel.get(key, 0) < -0.05:
                    should_arm = True
                    break

        # 3. Time to expiry < 120s
        if 0 < expires_in < 120:
            should_arm = True

        # 4. Shadow model signals momentum
        if shadow_result.get("shadow_momentum", False):
            should_arm = True

        if should_arm and not self.armed:
            self.armed = True
            self.armed_since = now
            self.armed_activations += 1
            log.info(f"🔴 ARMED mode activated: down_mid={down_mid:.4f} spot_vel={spot_vel.get('velocity_15s',0):.6f}")
        elif should_arm:
            # Renew armed mode
            self.armed_expire = now + 60  # Extend by 60s
        elif self.armed and now > self.armed_expire + 60:
            # Expire armed mode after 60s without near-entry
            armed_duration = now - self.armed_since
            self.armed_total_seconds += armed_duration
            self.armed = False
            log.info(f"⚪ ARMED mode expired: lasted {armed_duration:.0f}s")

        if self.armed:
            self.armed_scans += 1
            self.near_entry_events += 1

        return self.armed

    def _record_latency(self, phase_timings: dict):
        """§4: Record latency timings for a scan cycle."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **phase_timings,
        }

        # Write to JSONL
        try:
            with open(LATENCY_TELEMETRY_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

        # Keep in-memory for rolling report (last 3600 entries = ~5h at 5s)
        self.latency_records.append(entry)
        if len(self.latency_records) > 3600:
            self.latency_records.pop(0)

    def _generate_latency_report(self) -> dict:
        """§10: Generate rolling latency report."""
        if not self.latency_records:
            return {}

        def pct(data, p):
            if not data:
                return 0
            s = sorted(data)
            idx = int(len(s) * p / 100)
            return round(s[min(idx, len(s)-1)], 1)

        scan_times = [r.get("scan_latency_ms", 0) for r in self.latency_records if r.get("scan_latency_ms")]
        signal_times = [r.get("signal_to_submit_ms", 0) for r in self.latency_records if r.get("signal_to_submit_ms")]
        quote_ages = [r.get("quote_age_at_submit_ms", 0) for r in self.latency_records if r.get("quote_age_at_submit_ms")]
        book_times = [r.get("book_fetch_latency_ms", 0) for r in self.latency_records if r.get("book_fetch_latency_ms")]
        spot_times = [r.get("spot_fetch_latency_ms", 0) for r in self.latency_records if r.get("spot_fetch_latency_ms")]
        signal_compute = [r.get("signal_compute_latency_ms", 0) for r in self.latency_records if r.get("signal_compute_latency_ms")]

        slow_count = sum(1 for r in self.latency_records if r.get("execution_too_slow", False))

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_records": len(self.latency_records),
            "avg_scan_latency_ms": round(sum(scan_times) / max(len(scan_times), 1), 1),
            "p50_scan_latency_ms": pct(scan_times, 50),
            "p95_scan_latency_ms": pct(scan_times, 95),
            "avg_book_fetch_latency_ms": round(sum(book_times) / max(len(book_times), 1), 1),
            "avg_spot_fetch_latency_ms": round(sum(spot_times) / max(len(spot_times), 1), 1),
            "avg_signal_compute_latency_ms": round(sum(signal_compute) / max(len(signal_compute), 1), 1),
            "avg_signal_to_submit_ms": round(sum(signal_times) / max(len(signal_times), 1), 1),
            "p95_signal_to_submit_ms": pct(signal_times, 95),
            "avg_quote_age_at_submit_ms": round(sum(quote_ages) / max(len(quote_ages), 1), 1),
            "p95_quote_age_at_submit_ms": pct(quote_ages, 95),
            "execution_too_slow_count": slow_count,
        }

        with open(LATENCY_REPORT_FILE, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        return report

    def _generate_forensics_report(self) -> dict:
        """§10: Generate rolling state forensics report."""
        total_eligible = self.shadow_counterfactual.get("total_eligible_scans", 0)
        current_mom = self.shadow_counterfactual.get("current_state_momentum", 0)
        shadow_mom = self.shadow_counterfactual.get("shadow_momentum", 0)
        both = self.shadow_counterfactual.get("both_momentum", 0)
        current_only = self.shadow_counterfactual.get("current_only", 0)
        shadow_only = self.shadow_counterfactual.get("shadow_only", 0)
        neither = self.shadow_counterfactual.get("neither", 0)

        # Build momentum failure top reasons from recent no-trade counts
        momentum_failures = {
            "no_momentum": self.notrade_reason_counts.get("no_momentum", 0),
            "wrong_state": self.notrade_reason_counts.get("wrong_state", 0),
            "low_survivability": self.notrade_reason_counts.get("low_survivability", 0),
            "spread_too_wide": self.notrade_reason_counts.get("spread_too_wide", 0),
            "ask_not_heavy": 0,  # tracked in forensics logs
        }

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "eligible_bucket_scans": total_eligible,
            "current_model_momentum_count": current_mom,
            "spot_shadow_momentum_count": shadow_mom,
            "state_model_disagreement_count": shadow_only,
            "state_model_agreement_count": both,
            "neither_model_count": neither,
            "current_only_count": current_only,
            "shadow_only_count": shadow_only,
            "disagreement_rate": round(shadow_only / max(total_eligible, 1) * 100, 2),
            "top_momentum_failed_reasons": momentum_failures,
        }

        with open(STATE_FORENSICS_REPORT_FILE, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        return report

    def _generate_armed_report(self) -> dict:
        """§10: Generate rolling armed scanner report."""
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "armed_mode_active": self.armed,
            "armed_mode_activations": self.armed_activations,
            "armed_mode_total_seconds": round(self.armed_total_seconds, 1),
            "armed_mode_scans": self.armed_scans,
            "near_entry_events_seen": self.near_entry_events,
            "eligible_bucket_flashes_seen": self.eligible_flashes_seen,
            "eligible_bucket_flashes_missed": self.eligible_flashes_missed,
            "trade_count": self.state.total_trades,
        }

        with open(ARMED_SCANNER_REPORT_FILE, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        return report

    def evaluate_and_trade(self, slug_key: str, contract: dict) -> Optional[dict]:
        """§6: Evaluate a market for DOWN_MOMENTUM entry with full telemetry + latency."""
        self.cycle_id += 1
        scan_started_at = time.time()
        phase_timings = {}

        expires_in = contract.get("expires_in_sec", 0) if contract else 0
        has_position = False

        # ─── §4: Latency Phase — Book Fetch ───
        book_request_started_at = time.time()

        # Get both tokens, find DOWN (cheaper) token
        tokens = contract.get("tokens", []) if contract else []
        if not tokens or len(tokens) < 2:
            log.info(f"No tokens for {slug_key}")
            self._classify_no_trade(slug_key, contract, 0.0, "NO_SIGNAL", 0.0,
                                     expires_in, False, has_position, True, "")
            return None

        # Fetch orderbook for both tokens to identify cheap (DOWN) side
        token_orderbooks = {}
        for token_info in tokens:
            tid = token_info.get("token_id", "")
            if not tid:
                continue
            ob = fetch_orderbook_depth(tid)
            if not ob or ob.get("best_bid", 0) == 0 or ob.get("best_ask", 0) == 0:
                continue
            mid = (ob["best_bid"] + ob["best_ask"]) / 2
            ob["mid"] = mid
            token_orderbooks[tid] = (mid, ob)

        phase_timings["book_fetch_latency_ms"] = round((time.time() - book_request_started_at) * 1000, 1)

        if len(token_orderbooks) < 1:
            log.info(f"No orderbook data for {slug_key}")
            self._classify_no_trade(slug_key, contract, 0.0, "NO_SIGNAL", 0.0,
                                     expires_in, False, has_position, True, "")
            return None

        # Find the cheaper token (DOWN side)
        sorted_tokens = sorted(token_orderbooks.items(), key=lambda x: x[1][0])
        down_tid = sorted_tokens[0][0]  # cheapest token
        down_mid = sorted_tokens[0][1][0]
        down_ob = sorted_tokens[0][1][1]
        has_orderbook = True

        # ─── §4: Record token ask for delta tracking ───
        record_token_ask(down_tid, down_ob.get("best_ask", 0))

        # ─── §4: Latency Phase — Spot Fetch ───
        spot_request_started_at = time.time()

        # Compute continuation signal ALWAYS (for telemetry even when outside bucket)
        state, survivability, sig_info = compute_continuation_from_orderbook(down_ob, side="DOWN")

        phase_timings["signal_compute_latency_ms"] = round((time.time() - spot_request_started_at) * 1000, 1)

        # ─── §2: Fetch BTC spot price and compute velocities ───
        btc_spot = fetch_btc_spot()
        btc_perp = fetch_btc_perp_price()
        if btc_spot:
            record_spot(btc_spot)
            # §6: Also record perp price alongside spot
            if btc_perp:
                # Append perp to the latest spot buffer entry
                if SPOT_PRICE_BUFFER:
                    SPOT_PRICE_BUFFER[-1]["perp"] = btc_perp
        spot_vel = compute_spot_velocity()

        phase_timings["spot_fetch_latency_ms"] = round((time.time() - spot_request_started_at) * 1000, 1)

        # ─── §6: Compute token ask delta ───
        token_delta = compute_token_ask_delta(down_tid)

        # ─── §3+§7: Compute SPOT_MOMENTUM_SHADOW ───
        shadow_result = compute_spot_momentum_shadow(spot_vel, down_mid, expires_in,
                                                        sig_info=sig_info, token_delta=token_delta)
        shadow_state = shadow_result["shadow_state"]
        shadow_momentum = shadow_result["shadow_momentum"]

        # ─── §4: Check armed mode ───
        self._check_armed_mode(down_mid, spot_vel, expires_in, shadow_result)

        # ─── §4: Complete latency timings ───
        phase_timings["scan_latency_ms"] = round((time.time() - scan_started_at) * 1000, 1)
        self._record_latency(phase_timings)

        # ─── §2: Write forensics when in eligible bucket (3–12¢) ───
        in_eligible = 0.03 <= down_mid < 0.12
        if in_eligible:
            self.eligible_flashes_seen += 1
            self._write_eligible_forensics(
                slug_key, contract, down_mid, down_ob, state, survivability,
                sig_info, spot_vel, shadow_result, expires_in, has_position,
                token_delta=token_delta, latency_info=phase_timings
            )

        # ─── §3: Update shadow counterfactual ───
        if in_eligible and spot_vel.get("has_spot", False):
            current_momentum = state in ("DOWN_MOMENTUM", "DOWN_CONTINUATION")
            self.shadow_counterfactual["total_eligible_scans"] += 1
            if current_momentum:
                self.shadow_counterfactual["current_state_momentum"] += 1
            if shadow_momentum:
                self.shadow_counterfactual["shadow_momentum"] += 1
            if current_momentum and shadow_momentum:
                self.shadow_counterfactual["both_momentum"] += 1
            elif current_momentum and not shadow_momentum:
                self.shadow_counterfactual["current_only"] += 1
            elif not current_momentum and shadow_momentum:
                self.shadow_counterfactual["shadow_only"] += 1
            else:
                self.shadow_counterfactual["neither"] += 1
            total = max(self.shadow_counterfactual["total_eligible_scans"], 1)
            self.shadow_counterfactual["disagreement_rate"] = round(
                (self.shadow_counterfactual["current_only"] + self.shadow_counterfactual["shadow_only"])
                / total, 4
            )

        # Check for duplicate position
        cid = contract.get("conditionId", "")
        has_position = cid in self.active_positions

        # ─── §4: Bucket occupancy tracking (ALWAYS) ───
        self._update_bucket_occupancy(down_mid, state, survivability)

        # ─── §V21.7.3: Adjacent-bucket shadow diagnostics (ALWAYS) ───
        self._log_adjacent_bucket_shadow(
            slug_key, contract, down_mid, down_ob, spot_vel,
            state, survivability, expires_in, has_position, phase_timings
        )

        # ─── §V21.7.3: Bucket flash latency tracking (near-primary) ───
        self._track_bucket_flash_latency(
            slug_key, contract, down_mid, down_ob, spot_vel,
            state, survivability, expires_in, phase_timings
        )

        # ═══════════════════════════════════════════════════════════════
        # GATE SEQUENCE — each gate has telemetry
        # ═══════════════════════════════════════════════════════════════

        # Gate 1: PRIMARY bucket (§2) — DO NOT MODIFY PER DIRECTIVE §2
        if not (0.03 <= down_mid < 0.12):
            log.info(f"DOWN price {down_mid:.4f} outside PRIMARY bucket for {slug_key}")
            self._classify_no_trade(slug_key, contract, down_mid, state, survivability,
                                     expires_in, has_orderbook, has_position, True, "")
            # §6: Near-miss check (bucket outside range but other criteria might pass)
            self._check_near_miss(slug_key, contract, down_mid, state, survivability,
                                   expires_in, has_orderbook, has_position)
            return None

        # Gate 2: Signal state
        if state not in ("DOWN_MOMENTUM", "DOWN_CONTINUATION"):
            log.info(f"No DOWN_MOMENTUM signal for {slug_key}: {state}")
            self._classify_no_trade(slug_key, contract, down_mid, state, survivability,
                                     expires_in, has_orderbook, has_position, True, "")
            self._check_near_miss(slug_key, contract, down_mid, state, survivability,
                                   expires_in, has_orderbook, has_position)
            return None

        # ─── §V21.7.2: Gate 2a — Uptrend Regime Filter ───
        # Block fake short-term DOWN velocity inside a broader BTC uptrend.
        # Shadow CF showed 95.3% of blocked DOWN entries resolved UP during
        # an uptrend. This gate prevents entering contra-trend during trending_up.
        regime = "unknown"
        if len(SPOT_PRICE_BUFFER) >= 20:
            recent_prices = [e["price"] for e in SPOT_PRICE_BUFFER[-20:]]
            recent_std = np.std(recent_prices)
            recent_mean = abs(np.mean(recent_prices))
            trend = (recent_prices[-1] - recent_prices[0]) / max(abs(recent_prices[0]), 1e-9)
            if trend > 0.02:
                regime = "trending_up"
            elif trend < -0.02:
                regime = "trending_down"
            elif recent_std / max(recent_mean, 1e-9) > 0.05:
                regime = "volatile"
            else:
                regime = "ranging"

        if regime == "trending_up":
            log.info(f"UPTREND_REGIME_FILTER blocks {slug_key}: regime={regime} v15={spot_vel.get('velocity_15s',0):.6f} v60={spot_vel.get('velocity_60s',0):.6f}")
            self._classify_no_trade(slug_key, contract, down_mid, state, survivability,
                                     expires_in, has_orderbook, has_position, True, "")
            self._record_protective_gate(slug_key, contract, down_mid, "uptrend_regime_filter",
                                          spot_vel, regime, expires_in)
            return None

        # ─── §V21.7.2: Gate 2b — Fake Short-Term Dip Veto ───
        # v15 < 0 (brief dip) AND |v60| < 0.05 (no sustained move) = fake DOWN.
        # 70.7% of shadow losses had |v60| < 0.05. This pattern is noise inside
        # an uptrend, not genuine continuation.
        v15 = spot_vel.get("velocity_15s", 0)
        v30 = spot_vel.get("velocity_30s", 0)
        v60 = spot_vel.get("velocity_60s", 0)

        if v15 < 0 and abs(v60) < 0.05:
            log.info(f"FAKE_DIP veto blocks {slug_key}: v15={v15:.6f} v60={v60:.6f} — no sustained downtrend")
            self._classify_no_trade(slug_key, contract, down_mid, state, survivability,
                                     expires_in, has_orderbook, has_position, True, "")
            self._record_protective_gate(slug_key, contract, down_mid, "fake_short_term_dip_no_sustained_downtrend",
                                          spot_vel, regime, expires_in)
            return None

        # ─── §V21.7.2: Gate 2c — Insufficient Sustained Down-Velocity ───
        # PMXT DOWN_MOMENTUM required |v60| > 0.3. Live shadow had |v60| mean = 0.03.
        # Zero shadow events had |v60| > 0.3. Require minimum |v60| > threshold.
        MIN_SUSTAINED_VELOCITY = 0.03  # conservative floor — PMXT used 0.3 but live v60 is raw % change
        if abs(v60) <= MIN_SUSTAINED_VELOCITY:
            log.info(f"INSUFFICIENT_VELOCITY blocks {slug_key}: |v60|={abs(v60):.6f} <= {MIN_SUSTAINED_VELOCITY}")
            self._classify_no_trade(slug_key, contract, down_mid, state, survivability,
                                     expires_in, has_orderbook, has_position, True, "")
            self._record_protective_gate(slug_key, contract, down_mid, "insufficient_sustained_down_velocity",
                                          spot_vel, regime, expires_in)
            return None

        # Gate 3: Survivability
        if survivability < 0.05:
            log.info(f"Low survivability {survivability:.4f} for {slug_key}")
            self._classify_no_trade(slug_key, contract, down_mid, state, survivability,
                                     expires_in, has_orderbook, has_position, True, "")
            self._check_near_miss(slug_key, contract, down_mid, state, survivability,
                                   expires_in, has_orderbook, has_position)
            return None

        # Gate 4: Expiry
        if expires_in < 30:
            log.info(f"Market {slug_key} expiring in {expires_in:.0f}s — skipping")
            self._classify_no_trade(slug_key, contract, down_mid, state, survivability,
                                     expires_in, has_orderbook, has_position, True, "")
            return None

        # Gate 5: Duplicate position
        if has_position:
            self._classify_no_trade(slug_key, contract, down_mid, state, survivability,
                                     expires_in, has_orderbook, has_position, True, "")
            return None

        # Gate 6: Kill switches
        proposed_loss = POSITION_SIZE  # worst case
        kill_allowed, kill_reason = self.check_kill_switches(proposed_loss)
        if not kill_allowed:
            log.info(f"Kill switch blocked trade: {kill_reason}")
            self._classify_no_trade(slug_key, contract, down_mid, state, survivability,
                                     expires_in, has_orderbook, has_position, False, kill_reason)
            return None

        # Estimate time_pct
        slug_parts = contract.get("slug", "").split("-")
        interval_str = slug_parts[-2] if len(slug_parts) >= 2 else "5m"
        interval_sec = int(interval_str.replace("m", "")) * 60 if interval_str.endswith("m") else 300
        total_lifetime = interval_sec * 2
        time_pct = 1.0 - (expires_in / max(total_lifetime, 1))

        log.info(f"  {slug_key}: expires_in={expires_in:.0f}s interval={interval_sec}s "
                 f"time_pct={time_pct:.2f} down_mid={down_mid:.4f}")

        # Timing preference (not hard gate per §5)
        if 0 < time_pct < TIMING_LO:
            log.info(f"Too early for MOMENTUM window: time_pct={time_pct:.2f}")
        elif time_pct >= TIMING_HI:
            log.info(f"Too late for MOMENTUM window: time_pct={time_pct:.2f}")

        # §6: All gates passed — execute trade
        price = down_ob["best_ask"]  # TAKER buys at ask
        tick_size = down_ob.get("tick_size", "0.01")
        rounded_price = round_to_tick(price, tick_size)

        if not validate_price(rounded_price, tick_size):
            log.warning(f"Price {rounded_price} doesn't conform to tick {tick_size}")
            self._classify_no_trade(slug_key, contract, down_mid, state, survivability,
                                     expires_in, has_orderbook, has_position, True,
                                     "execution_rejected")
            return None

        # Determine bucket weight
        bucket_weight = 1.0
        for (lo, hi), w in BUCKET_WEIGHTS.items():
            if lo <= down_mid < hi:
                bucket_weight = w
                break

        adjusted_size = POSITION_SIZE * bucket_weight

        log.info(f"🔴 SIGNAL: {state} | price={down_mid:.4f} | survivability={survivability:.4f} | {slug_key}")

        # Build trade record
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "asset": contract.get("slug", "").split("-")[0].upper() if "-" in contract.get("slug", "") else "BTC",
            "interval": contract.get("slug", "").split("-")[-2] if "-" in contract.get("slug", "") else "5m",
            "slug": contract.get("slug", ""),
            "condition_id": cid,
            "side": "DOWN",
            "entry_price": rounded_price,
            "bucket": f"{down_mid:.3f}",
            "timing_phase": "MOMENTUM" if TIMING_LO <= time_pct < TIMING_HI else "UNKNOWN",
            "route": "TAKER",
            "signal_score": survivability,
            "expected_probability": sig_info.get("expected_prob_down", 0),
            "expected_ev": sig_info.get("expected_ev", 0),
            "expected_slippage": sig_info.get("spread_pct", 0),
            "actual_fill_price": rounded_price,  # updated after fill
            "fill_latency_ms": 0,
            "slippage_bps": 0,
            "settlement_result": "PENDING",
            "win_loss": "PENDING",
            "realized_pnl": 0.0,
            "bankroll_before": self.state.bankroll,
            "bankroll_after": self.state.bankroll,  # updated after settlement
            "running_pf": 0.0,
            "running_ev": 0.0,
            "running_drawdown": 0.0,
            "fill_quality": "PENDING",
            "mode": "PAPER" if self.paper_mode else "LIVE",
            "bucket_weight": bucket_weight,
            "adjusted_size": adjusted_size,
        }

        # Execute trade
        if self.paper_mode:
            trade["fill_quality"] = "PAPER_FILL"
            trade["actual_fill_price"] = rounded_price
            # Paper P&L: worst case for now, settled at expiry
            trade["realized_pnl"] = 0.0  # PENDING settlement
        else:
            # Live execution via CLOB
            spec = build_dry_run_order(down_tid, "BUY", rounded_price, adjusted_size)
            if spec.valid:
                result = submit_tracked_order(spec)
                if "error" in result:
                    log.error(f"Order failed: {result['error']}")
                    self.state.api_execution_errors += 1
                    trade["fill_quality"] = "ERROR"
                    trade["settlement_result"] = "ERROR"
                    return None
                trade["actual_fill_price"] = rounded_price
                trade["fill_quality"] = "FULL"
            else:
                log.warning(f"Invalid order spec: {spec.errors}")
                return None

        # Update state
        self.state.total_trades += 1
        self.state.daily_trades += 1
        self.active_positions[cid] = trade

        # Log trade
        self._save_trade(trade)
        log.info(f"  Trade #{self.state.total_trades}: DOWN @ ${rounded_price:.4f} | size=${adjusted_size:.2f} | {trade['fill_quality']}")

        # Update bucket occupancy trade count
        for bucket_name, (lo, hi) in BUCKET_RANGES.items():
            if lo <= down_mid < hi:
                self.bucket_occupancy[bucket_name]["trades"] += 1
                break

        return trade

    def run_loop(self, max_iterations: int = None):
        """Main scan loop."""
        log.info("=" * 60)
        log.info("V21.7.1 LIVE DEPLOYMENT — STARTING SCAN LOOP")
        log.info(f"  Mode: {'PAPER' if self.paper_mode else 'LIVE'}")
        log.info(f"  Max trades: {MAX_TOTAL_LIVE_TRADES}")
        log.info("=" * 60)

        iteration = 0
        last_discovery = 0

        while self.running:
            if max_iterations and iteration >= max_iterations:
                log.info(f"Max iterations ({max_iterations}) reached")
                break

            if self.state.halted:
                log.warning(f"⚠️  HALTED: {self.state.halt_reason}")
                break

            if self.state.total_trades >= MAX_TOTAL_LIVE_TRADES:
                log.info(f"✓ Max trades reached: {self.state.total_trades}")
                break

            now = time.time()

            # Rediscover markets every 60 seconds
            if now - last_discovery > 60:
                self.discover_markets()
                last_discovery = now

            # Check each market for trade opportunity
            for slug_key, contract in self.active_contracts.items():
                try:
                    trade = self.evaluate_and_trade(slug_key, contract)
                    if trade:
                        # Trade executed — skip rest of cycle
                        break
                except Exception as e:
                    log.error(f"Trade evaluation error: {e}")
                    traceback.print_exc()

            # Save state
            self._save_state()

            # §7: Scarcity report every 30 minutes
            if now - self.last_scarcity_report >= SCARCITY_REPORT_INTERVAL:
                self._generate_scarcity_report()
                self._resolve_protective_gate_events()
                self._generate_protective_gate_summary()
                self._resolve_adjacent_bucket_settlements()
                self._generate_v2173_report()
                self.last_scarcity_report = now

            # §10: Rolling reports every 30 minutes
            if now - self.last_latency_report >= SCARCITY_REPORT_INTERVAL:
                self._generate_latency_report()
                self.last_latency_report = now

            if now - self.last_forensics_report >= SCARCITY_REPORT_INTERVAL:
                self._generate_forensics_report()
                self.last_forensics_report = now

            if now - self.last_armed_report >= SCARCITY_REPORT_INTERVAL:
                self._generate_armed_report()
                self.last_armed_report = now

            # §3: Armed mode → faster scan interval
            if self.armed:
                sleep_time = 1.0  # §3: Armed scan at 1s
            else:
                sleep_time = self.scan_interval
            time.sleep(sleep_time)
            iteration += 1

            # Periodic status (every ~1 minute at 5s interval)
            if iteration % 12 == 0:
                log.info(
                    f"Status: {self.state.total_trades} trades | "
                    f"${self.state.bankroll:.2f} bank | "
                    f"P&L: ${self.state.total_pnl:.2f} | "
                    f"Consec losses: {self.state.consecutive_losses} | "
                    f"Active positions: {len(self.active_positions)} | "
                    f"Eligible: {self.eligible_bucket_seconds:.0f}s | "
                    f"Near-misses: {self.near_miss_count}"
                )

        log.info("=" * 60)
        log.info("V21.7.1 DEPLOYMENT COMPLETE")
        log.info(f"  Total trades: {self.state.total_trades}")
        log.info(f"  Total P&L: ${self.state.total_pnl:.2f}")
        log.info(f"  Bankroll: ${self.state.bankroll:.2f}")
        log.info("=" * 60)

        # Final scarcity report
        self._generate_scarcity_report()
        self._generate_latency_report()
        self._generate_forensics_report()
        self._generate_armed_report()
        self._save_state()

    def _save_trade(self, trade: dict):
        """§10: Persist trade record."""
        with open(TRADES_FILE, 'a') as f:
            f.write(json.dumps(trade, default=str) + "\n")

    def _save_state(self):
        """Save runner state."""
        state_dict = {
            "live_enabled": self.state.live_enabled,
            "paper_only": self.state.paper_only,
            "total_trades": self.state.total_trades,
            "wins": self.state.wins,
            "losses": self.state.losses,
            "total_pnl": self.state.total_pnl,
            "bankroll": self.state.bankroll,
            "consecutive_losses": self.state.consecutive_losses,
            "daily_loss": self.state.daily_loss,
            "weekly_loss": self.state.weekly_loss,
            "daily_trades": self.state.daily_trades,
            "settlement_errors": self.state.settlement_errors,
            "accounting_errors": self.state.accounting_errors,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "active_positions": len(self.active_positions),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state_dict, f, indent=2, default=str)

    def _write_incident(self, incident_type: str, detail: str):
        """Write incident report on kill switch trigger."""
        incident = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": incident_type,
            "detail": detail,
            "state": asdict(self.state),
            "trades_so_far": self.state.total_trades,
            "pnl": self.state.total_pnl,
        }
        with open(INCIDENT_FILE, 'w') as f:
            json.dump(incident, f, indent=2, default=str)
        log.critical(f"INCIDENT REPORT: {incident_type} — {detail}")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="V21.7.1 Live Deployment")
    parser.add_argument("--live", action="store_true", help="Enable live execution (default: paper)")
    parser.add_argument("--max-iterations", type=int, default=None, help="Max scan iterations")
    parser.add_argument("--scan-interval", type=float, default=5.0, help="Scan interval in seconds")
    args = parser.parse_args()

    runner = V2171LiveRunner(paper_mode=not args.live)

    if args.scan_interval:
        BASE_SCAN_INTERVAL = args.scan_interval

    runner.initialize()

    log.info("\nStarting V21.7.1 deployment loop...")
    if not runner.wallet_info.get('collateral_ready', False):
        log.warning("⚠️  Wallet not collateral-ready — PAPER MODE FORCED")
        log.warning("To enable live: ensure USDC balance > $10 and allowances set")

    runner.run_loop(max_iterations=args.max_iterations)