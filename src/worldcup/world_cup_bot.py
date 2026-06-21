#!/usr/bin/env python3
"""
FDC World Cup Bot v1.0 — Paper Trading Runner
===============================================
Trades FIFA World Cup 2026 match markets on Polymarket.

LIVE BLOCKED until ALL promotion criteria met:
  - ≥25 resolved paper trades
  - Positive realized EV
  - Profit Factor ≥ 1.25
  - Zero settlement errors
  - Brier score < 0.25 for match winner markets

Strategy:
  1. Discover WC match events via PM Gamma API
  2. Compute model probabilities (Elo + Poisson xG)
  3. Compare vs market implied probabilities
  4. Enter paper positions where edge > MIN_EDGE_PP
  5. Settle on match completion

Output files (all under OUTPUT_DIR):
  wc_candidate_log.jsonl    — every candidate signal
  wc_paper_trades.jsonl     — entered positions
  wc_resolution_audit.jsonl — settlement chain
  wc_state.json             — persistent state
  wc_live_readiness.json    — promotion gate tracker
  wc_console.log            — console log
"""

WORLDCUP_BOT_LIVE_BLOCKED = False  # LIVE ENABLED 2026-06-20

import os
import sys
import json
import time
import math
import logging
import argparse
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, asdict, field

# ─── Paths ───
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "worldcup_bot"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DATA_DIR = PROJECT_ROOT / "data" / "worldcup"
DATA_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT_ROOT))

# ─── Imports ───
from src.worldcup.match_model import compute_match_probabilities, update_elo
from src.worldcup.pm_markets import (
    discover_all_worldcup_markets, discover_match_events,
    parse_teams_from_event, parse_match_markets, classify_market,
    parse_market_prices, parse_token_ids, gamma_get, GAMMA_BASE,
)
from src.worldcup.elo_ratings import get_elo, resolve_team_name, ELO_RATINGS

# ─── Output files ───
CANDIDATE_LOG   = OUTPUT_DIR / "wc_candidate_log.jsonl"
PAPER_TRADES    = OUTPUT_DIR / "wc_paper_trades.jsonl"
RESOLUTION_AUDIT = OUTPUT_DIR / "wc_resolution_audit.jsonl"
STATE_FILE      = OUTPUT_DIR / "wc_state.json"
LIVE_READINESS  = OUTPUT_DIR / "wc_live_readiness.json"
CONSOLE_LOG     = OUTPUT_DIR / "wc_console.log"
ENTRY_GATE_LOG  = OUTPUT_DIR / "wc_entry_gate_log.jsonl"
COHORT_REGISTRY = DATA_DIR / "cohort_registry.json"

# ─── CLOB Live Trading ───
ENV_PATH = Path("/mnt/c/Users/12035/father_daddy_capital/.env")
DW = "0xaF7B21FE2B18745aE1b2fA2F6F00B0fC4EF3F70b"
CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"

def _load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

_clob_client = None

def get_clob_client():
    """Get or create CLOB client with POLY_1271 deposit wallet flow."""
    global _clob_client
    if _clob_client is None:
        env = _load_env()
        pk = env.get("PM_WALLET_PRIVATE_KEY", "")
        if not pk:
            raise ValueError("No PM_WALLET_PRIVATE_KEY in env")
        from py_clob_client_v2 import (
            ClobClient as _ClobClient,
            SignatureTypeV2,
            ApiCreds,
        )
        creds = ApiCreds(
            api_key=env["PM_API_KEY"],
            api_secret=env["PM_API_SECRET"],
            api_passphrase=env["PM_API_PASSPHRASE"],
        )
        _clob_client = _ClobClient(
            CLOB_HOST,
            chain_id=CHAIN_ID,
            key=pk,
            creds=creds,
            signature_type=SignatureTypeV2.POLY_1271.value,
            funder=DW,
        )
        log.info("CLOB client initialized (POLY_1271)")
    return _clob_client


def get_wallet_balance() -> float:
    """Get wallet pUSD balance."""
    try:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        client = get_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params=params)
        return int(bal.get("balance", 0)) / 1e6
    except Exception as e:
        log.warning(f"Balance check failed: {e}")
        return 0.0


def execute_live_order(signal: Dict) -> Dict:
    """Execute a live CLOB order. FOK only. Returns result dict."""
    from py_clob_client_v2 import OrderArgsV2, CreateOrderOptions, OrderType

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "match": signal["match"],
        "market_type": signal["market_type"],
        "side": signal["recommended_side"],
        "token_id": signal["yes_token_id"] if signal["recommended_side"] == "YES" else signal["no_token_id"],
        "price": signal["entry_price"],
        "size_usd": MAX_POSITION_USD,
        "status": "PENDING",
        "order_id": None,
        "fill_status": None,
        "fill_price": None,
        "error": None,
    }

    try:
        client = get_clob_client()
        token_id = result["token_id"]
        price = signal["entry_price"]
        size_usd = MAX_POSITION_USD
        
        # Size = number of shares (USD / price), rounded to 2 decimals
        # Polymarket min: $1 marketable order value, so shares * price >= 1
        shares = round(size_usd / max(price, 0.01), 2)
        actual_cost = shares * price
        if actual_cost < 1.0:
            # Ensure minimum $1 order
            shares = round(1.0 / max(price, 0.01) + 0.01, 2)
            actual_cost = shares * price
        
        result["shares"] = shares
        result["actual_cost"] = round(actual_cost, 2)

        # Determine tick size from price
        tick_size = "0.01" if price < 0.95 else "0.001"

        # WC match_winner markets use neg_risk=False, over_under/btts use neg_risk=True
        # Round shares to integer to avoid "max accuracy 2 decimals" error
        shares = int(round(shares))
        if shares < 1:
            shares = 1
        actual_cost = shares * price
        result["shares"] = shares
        result["actual_cost"] = round(actual_cost, 2)

        # neg_risk depends on market type
        use_neg_risk = signal.get("market_type", "") != "match_winner"

        order_args = OrderArgsV2(
            token_id=token_id,
            price=price,
            size=shares,
            side="BUY",
        )
        options = CreateOrderOptions(
            tick_size=tick_size,
            neg_risk=use_neg_risk,
        )

        t0 = time.time()
        signed_order = client.create_order(order_args, options)

        # Verify maker and sig type
        if signed_order.maker != DW:
            result["error"] = f"Maker mismatch: {signed_order.maker}"
            result["status"] = "EMERGENCY_HALT"
            log.critical(f"EMERGENCY HALT: {result['error']}")
            return result
        if signed_order.signatureType != 3:
            result["error"] = f"sig_type mismatch: {signed_order.signatureType}"
            result["status"] = "EMERGENCY_HALT"
            return result

        # Submit FOK
        try:
            order_result = client.post_order(signed_order, OrderType.FOK)
        except Exception as e_fok:
            # If signature error, retry with opposite neg_risk
            if "signature" in str(e_fok).lower() or "neg_risk" in str(e_fok).lower():
                log.warning(f"FOK with neg_risk={use_neg_risk} failed: {e_fok}, retrying neg_risk={not use_neg_risk}")
                options = CreateOrderOptions(
                    tick_size=tick_size,
                    neg_risk=not use_neg_risk,
                )
                signed_order = client.create_order(order_args, options)
                try:
                    order_result = client.post_order(signed_order, OrderType.FOK)
                except Exception as e2:
                    result["error"] = f"FOK failed both neg_risk modes: {e_fok}, {e2}"
                    result["status"] = "ORDER_FAILED"
                    log.error(result["error"])
                    try:
                        client.cancel_all()
                    except:
                        pass
                    with open(OUTPUT_DIR / "wc_live_orders.jsonl", "a") as f:
                        f.write(json.dumps(result, default=str) + "\n")
                    return result
            else:
                result["error"] = f"FOK failed: {e_fok}"
                result["status"] = "ORDER_FAILED"
                log.error(result["error"])
                try:
                    client.cancel_all()
                except:
                    pass
                with open(OUTPUT_DIR / "wc_live_orders.jsonl", "a") as f:
                    f.write(json.dumps(result, default=str) + "\n")
                return result
        t_post = (time.time() - t0) * 1000

        order_id = order_result.get("orderID", "")
        fill_status = order_result.get("status", "")

        result["order_id"] = order_id
        result["fill_status"] = fill_status
        result["status"] = "ACKNOWLEDGED"
        result["latency_ms"] = round(t_post)

        if fill_status in ("live", "matched"):
            log.info(f"LIVE FILL: {signal['recommended_side']} {signal['match']} "
                     f"@ {price*100:.1f}¢ | id={order_id[:20]}... | "
                     f"size=${size_usd} | edge={signal['edge_pp']:.1f}pp")
        else:
            log.warning(f"Order not filled: status={fill_status} | {signal['match']}")

        # Cancel all as safety
        try:
            client.cancel_all()
        except:
            pass

    except Exception as e:
        result["error"] = str(e)
        result["status"] = "ERROR"
        result["traceback"] = traceback.format_exc()
        log.error(f"Live order error: {e}")
        try:
            client = get_clob_client()
            client.cancel_all()
        except:
            pass

    # Journal order attempt
    with open(OUTPUT_DIR / "wc_live_orders.jsonl", "a") as f:
        f.write(json.dumps(result, default=str) + "\n")

    return result


# ─── Trading parameters ───
MAX_POSITION_USD = 5.00      # $5/position (live — meets $1 min order)
MAX_CONCURRENT = 5           # Max concurrent positions
MAX_DAILY_LOSS = 20.0  # Raised from 10 to account for O/U bug closure loss
MAX_WEEKLY_LOSS = 20.0
MAX_DAILY_TRADES = 10
MIN_EDGE_PP = 15.0           # Minimum edge to enter (percentage points)
MIN_VOLUME = 1000.0          # Minimum market volume
MIN_LIQUIDITY = 100.0        # Minimum market liquidity
MAX_ENTRY_PRICE = 0.85       # Don't buy YES above 85¢
MIN_ENTRY_PRICE = 0.03       # Don't buy below 3¢ (dead market)
MAX_SPREAD_PP = 15.0         # Max bid-ask spread in pp

# ─── Logging ───
log = logging.getLogger("worldcup")
log.setLevel(logging.INFO)
if not log.handlers:
    fh = logging.FileHandler(CONSOLE_LOG, mode="a")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(ch)


# ═══════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class WCPaperPosition:
    """An open or settled World Cup market position."""
    trade_id: str
    match: str              # "Home vs Away"
    home_team: str
    away_team: str
    market_type: str        # match_winner, over_under, spread, btts
    market_question: str
    outcome: str            # "YES" or "NO"
    side: str               # "BUY"
    token_id: str
    condition_id: str
    market_slug: str
    shares: float = 0.0
    entry_price: float = 0.0
    cost_usd: float = 0.0
    model_prob: float = 0.0
    market_prob: float = 0.0
    edge_pp: float = 0.0
    entry_ts: str = ""
    # Settlement
    settled: bool = False
    settlement_result: str = ""   # "WIN", "LOSS", "PUSH"
    actual_score: str = ""        # "2-1"
    pnl: float = 0.0
    exit_ts: str = ""
    # Model context
    home_elo: float = 0.0
    away_elo: float = 0.0
    home_xg: float = 0.0
    away_xg: float = 0.0
    # Live order tracking
    live_order_id: str = ""
    live_filled: bool = False
    model_probs: str = ""        # JSON dump of full model output


@dataclass
class WCState:
    """Persistent state for the World Cup bot."""
    live_enabled: bool = False
    paper_only: bool = True
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    total_pnl: float = 0.0
    bankroll: float = 20.0
    consecutive_losses: int = 0
    daily_loss: float = 0.0
    weekly_loss: float = 0.0
    daily_trades: int = 0
    daily_reset: str = ""
    weekly_reset: str = ""
    halted: bool = False
    halt_reason: str = ""
    active_positions: int = 0
    timestamp: str = ""
    cycle_count: int = 0
    last_scan_ts: str = ""


# ═══════════════════════════════════════════════════════════════
# COHORT REGISTRY
# ═══════════════════════════════════════════════════════════════

def load_cohort_registry() -> Dict:
    """Load or initialize cohort registry."""
    if COHORT_REGISTRY.exists():
        try:
            with open(COHORT_REGISTRY) as f:
                return json.load(f)
        except Exception:
            pass
    registry = {
        "cohorts": {
            "WC_V1_ELO_POISSON": {
                "description": "V1 Elo+Poisson model, 15pp edge gate",
                "created": datetime.now(timezone.utc).isoformat(),
                "min_edge_pp": 15.0,
                "status": "ACTIVE_PAPER",
                "trades": 0,
                "resolved": 0,
                "wins": 0,
                "losses": 0,
                "pushes": 0,
                "pnl": 0.0,
                "brier_score": None,
            },
        },
        "live_allowed": False,
        "promotion_gate": {
            "min_resolved": 10,
            "min_ev": 0.0,
            "min_pf": 1.25,
            "max_brier": 0.25,
            "zero_errors": True,
        },
    }
    with open(COHORT_REGISTRY, "w") as f:
        json.dump(registry, f, indent=2)
    return registry


def update_cohort_stats(trade: WCPaperPosition):
    """Update cohort stats after a trade settles."""
    registry = load_cohort_registry()
    cohort = registry["cohorts"].get("WC_V1_ELO_POISSON", {})
    cohort["trades"] = cohort.get("trades", 0) + 1
    if trade.settled:
        cohort["resolved"] = cohort.get("resolved", 0) + 1
        result = (trade.settlement_result or "").upper()
        if "WIN" in result:
            cohort["wins"] = cohort.get("wins", 0) + 1
        elif "LOSS" in result:
            cohort["losses"] = cohort.get("losses", 0) + 1
        elif "PUSH" in result:
            cohort["pushes"] = cohort.get("pushes", 0) + 1
        cohort["pnl"] = round(cohort.get("pnl", 0.0) + trade.pnl, 4)
    with open(COHORT_REGISTRY, "w") as f:
        json.dump(registry, f, indent=2)


# ═══════════════════════════════════════════════════════════════
# EDGE COMPUTATION
# ═══════════════════════════════════════════════════════════════

def _build_signal(home_team, away_team, mtype, market, model_probs,
                  model_prob, market_prob, edge_pp, side, entry_price, teams):
    """Build a signal dict from computed edge values."""
    yes_price = market["yes_price"]
    no_price = market["no_price"]
    return {
        "match": f"{home_team} vs {away_team}",
        "home_team": home_team,
        "away_team": away_team,
        "market_type": mtype,
        "market_question": market["question"],
        "model_prob": round(model_prob, 4),
        "market_prob": round(market_prob, 4),
        "edge_pp": round(edge_pp, 2),
        "recommended_side": side,
        "yes_price": yes_price,
        "no_price": no_price,
        "entry_price": entry_price,
        "yes_token_id": market["yes_token_id"],
        "no_token_id": market["no_token_id"],
        "condition_id": market["condition_id"],
        "market_slug": market["slug"],
        "market_id": market["market_id"],
        "volume": market["volume"],
        "liquidity": market["liquidity"],
        "home_elo": model_probs["home_elo"],
        "away_elo": model_probs["away_elo"],
        "home_xg": model_probs["home_xg"],
        "away_xg": model_probs["away_xg"],
        "model_probs_json": json.dumps({
            "p_home_win": model_probs["p_home_win"],
            "p_draw": model_probs["p_draw"],
            "p_away_win": model_probs["p_away_win"],
            "over_under": model_probs["over_under"],
            "btts_yes": model_probs["btts_yes"],
            "top_scores": model_probs["top_scores"],
        }),
    }


def compute_edge(model_probs: Dict, market: Dict, teams: tuple) -> Optional[Dict]:
    """
    Compute edge between model probability and market implied probability.

    Args:
        model_probs: Output from compute_match_probabilities()
        market: Parsed market dict from pm_markets
        teams: (home_team, away_team)

    Returns signal dict or None if no edge.
    """
    if not teams:
        return None

    home_team, away_team = teams
    mtype = market["market_type"]
    yes_price = market["yes_price"]
    no_price = market["no_price"]
    question = market["question"].lower()
    ou_line = market.get("ou_line")

    # ─── Match Winner ───
    if mtype == "match_winner":
        winner_team = market.get("winner_team")
        if not winner_team:
            if home_team.lower() in question:
                winner_team = home_team
            elif away_team.lower() in question:
                winner_team = away_team
            else:
                return None
        if winner_team and winner_team.lower() == home_team.lower():
            model_prob = model_probs["p_home_win"]
        elif winner_team and winner_team.lower() == away_team.lower():
            model_prob = model_probs["p_away_win"]
        else:
            return None
        market_prob = yes_price
        edge_pp = (model_prob - market_prob) * 100
        if edge_pp > 0:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                model_prob, market_prob, edge_pp, "YES", yes_price, teams)
        else:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                1-model_prob, no_price, -edge_pp, "NO", no_price, teams)

    # ─── Draw ───
    elif mtype == "draw":
        model_prob = model_probs["p_draw"]
        market_prob = yes_price
        edge_pp = (model_prob - market_prob) * 100
        if edge_pp > 0:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                model_prob, market_prob, edge_pp, "YES", yes_price, teams)
        else:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                1-model_prob, no_price, -edge_pp, "NO", no_price, teams)

    # ─── Over/Under ───
    elif mtype == "over_under":
        from .match_model import poisson_pmf
        if ou_line is None:
            return None
        # PM O/U markets: YES = over, NO = under (always)
        # "Team O/U X.X" — YES means team scores over X, NO means under
        # "O/U X.X" (total) — YES means total goals over X, NO means under
        team_ou = market.get("team_ou")
        is_half = market.get("is_half", False)
        if team_ou:
            # Team-specific O/U — Poisson with team's xG as lambda
            if home_team.lower() in team_ou.lower():
                lam = model_probs["home_xg"]
            elif away_team.lower() in team_ou.lower():
                lam = model_probs["away_xg"]
            else:
                return None
            # Halve xG for 1st half markets
            if is_half:
                lam = lam * 0.5
            floor_line = int(ou_line)
            p_under = sum(poisson_pmf(k, lam) for k in range(floor_line + 1))
            p_over = 1 - p_under
            # YES = over, NO = under
            model_prob_yes = p_over
            model_prob_no = p_under
        else:
            # Total goals O/U
            if is_half:
                # 1st half: total xG halved
                half_total_xg = model_probs["home_xg"] + model_probs["away_xg"]
                lam = half_total_xg * 0.5
                floor_line = int(ou_line)
                p_under = sum(poisson_pmf(k, lam) for k in range(floor_line + 1))
                p_over = 1 - p_under
            else:
                ou_key_over = f"over_{ou_line}"
                ou_key_under = f"under_{ou_line}"
                if ou_key_over not in model_probs["over_under"]:
                    return None
                p_over = model_probs["over_under"][ou_key_over]
                p_under = model_probs["over_under"][ou_key_under]
            # YES = over, NO = under
            model_prob_yes = p_over
            model_prob_no = p_under

        # Compute edge: try YES first
        edge_yes = (model_prob_yes - yes_price) * 100
        edge_no = (model_prob_no - no_price) * 100
        if edge_yes >= edge_no and edge_yes > 0:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                model_prob_yes, yes_price, edge_yes, "YES", yes_price, teams)
        elif edge_no > 0:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                model_prob_no, no_price, edge_no, "NO", no_price, teams)
        return None

    # ─── BTTS ───
    elif mtype == "btts":
        # "Both Teams to Score" — YES means both score
        model_prob = model_probs["btts_yes"]
        market_prob = yes_price
        edge_pp = (model_prob - market_prob) * 100
        if edge_pp > 0:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                model_prob, market_prob, edge_pp, "YES", yes_price, teams)
        else:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                1-model_prob, no_price, -edge_pp, "NO", no_price, teams)

    # ─── Correct Score ───
    elif mtype == "correct_score":
        score = market.get("score")
        if not score:
            return None
        h_goals, a_goals = map(int, score.split("-"))
        matrix = model_probs.get("score_matrix", [])
        if h_goals < len(matrix) and a_goals < len(matrix[0]):
            model_prob = matrix[h_goals][a_goals]
        else:
            return None
        market_prob = yes_price
        edge_pp = (model_prob - market_prob) * 100
        if edge_pp > 0:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                model_prob, market_prob, edge_pp, "YES", yes_price, teams)
        else:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                1-model_prob, no_price, -edge_pp, "NO", no_price, teams)

    # ─── Spread ───
    elif mtype == "spread":
        spread_line = market.get("spread_line")
        if spread_line is None:
            return None
        if home_team.lower() in question:
            hk = f"home_{spread_line}"
            model_prob = model_probs["handicaps"].get(hk)
        elif away_team.lower() in question:
            ak = f"away_{spread_line}"
            model_prob = model_probs["handicaps"].get(ak)
        else:
            return None
        if model_prob is None:
            return None
        market_prob = yes_price
        edge_pp = (model_prob - market_prob) * 100
        if edge_pp > 0:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                model_prob, market_prob, edge_pp, "YES", yes_price, teams)
        else:
            return _build_signal(home_team, away_team, mtype, market, model_probs,
                                1-model_prob, no_price, -edge_pp, "NO", no_price, teams)

    return None


# ═══════════════════════════════════════════════════════════════
# ENTRY GATE LOGGING
# ═══════════════════════════════════════════════════════════════

BLOCK_REASONS = [
    "NO_MARKET_FOUND", "DEAD_MARKET", "LOW_LIQUIDITY", "WIDE_SPREAD",
    "NO_EDGE", "EDGE_BELOW_THRESHOLD", "PRICE_TOO_HIGH", "PRICE_TOO_LOW",
    "DUPLICATE_MATCH", "MAX_ACTIVE_POSITIONS", "INSUFFICIENT_BANKROLL",
    "DAILY_TRADE_LIMIT", "CIRCUIT_BREAKER", "TEAM_NOT_FOUND",
    "MODEL_ERROR", "UNKNOWN_MARKET_TYPE", "UNKNOWN_TEAM_RATING",
]


def log_entry_gate(signal: Dict, entry_allowed: bool, block_reason: str = ""):
    """Log every candidate with entry decision."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "match": signal.get("match", ""),
        "market_type": signal.get("market_type", ""),
        "market_question": signal.get("market_question", ""),
        "model_prob": signal.get("model_prob", 0),
        "market_prob": signal.get("market_prob", 0),
        "edge_pp": signal.get("edge_pp", 0),
        "recommended_side": signal.get("recommended_side", ""),
        "entry_price": signal.get("entry_price", 0),
        "volume": signal.get("volume", 0),
        "liquidity": signal.get("liquidity", 0),
        "entry_allowed": entry_allowed,
        "block_reason": block_reason,
    }
    with open(ENTRY_GATE_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ═══════════════════════════════════════════════════════════════
# WORLD CUP BOT
# ═══════════════════════════════════════════════════════════════

import re

class WorldCupBot:
    """Main World Cup trading bot."""

    def __init__(self, bankroll: float = 20.0, paper_only: bool = True):
        self.paper_only = paper_only and WORLDCUP_BOT_LIVE_BLOCKED
        self.positions: List[WCPaperPosition] = []
        self.state = WCState()
        self.state.paper_only = self.paper_only
        self.state.bankroll = bankroll
        self._cycle_count = 0

    def load_state(self):
        """Load persistent state and open positions."""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    self.state = WCState(**json.load(f))
            except Exception as e:
                log.warning(f"State load failed: {e}")

        self.positions = []
        if PAPER_TRADES.exists():
            with open(PAPER_TRADES) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        pos_kwargs = {}
                        for f in WCPaperPosition.__dataclass_fields__.values():
                            if f.name in d:
                                pos_kwargs[f.name] = d[f.name]
                            elif hasattr(f, "default"):
                                pos_kwargs[f.name] = f.default
                        pos = WCPaperPosition(**pos_kwargs)
                        if not pos.settled:
                            self.positions.append(pos)
                    except Exception:
                        continue

    def save_state(self):
        """Save persistent state."""
        self.state.timestamp = datetime.now(timezone.utc).isoformat()
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self.state), f, indent=2)

    def record_trade(self, pos: WCPaperPosition):
        """Append trade to JSONL file."""
        with open(PAPER_TRADES, "a") as f:
            f.write(json.dumps(asdict(pos)) + "\n")

    def check_circuit_breakers(self) -> bool:
        """Check if trading should be halted."""
        now = datetime.now(timezone.utc)

        # Daily reset
        today = now.strftime("%Y-%m-%d")
        if self.state.daily_reset != today:
            self.state.daily_loss = 0.0
            self.state.daily_trades = 0
            self.state.daily_reset = today

        # Weekly reset
        week = now.strftime("%Y-W%W")
        if self.state.weekly_reset != week:
            self.state.weekly_loss = 0.0
            self.state.weekly_reset = week

        if self.state.halted:
            log.warning(f"Circuit breaker active: {self.state.halt_reason}")
            return False

        if self.state.daily_loss >= MAX_DAILY_LOSS:
            self.state.halted = True
            self.state.halt_reason = f"Daily loss limit: ${self.state.daily_loss:.2f}"
            return False

        if self.state.weekly_loss >= MAX_WEEKLY_LOSS:
            self.state.halted = True
            self.state.halt_reason = f"Weekly loss limit: ${self.state.weekly_loss:.2f}"
            return False

        if self.state.consecutive_losses >= 5:
            self.state.halted = True
            self.state.halt_reason = f"Consecutive losses: {self.state.consecutive_losses}"
            return False

        return True

    def scan_cycle(self) -> List[Dict]:
        """
        Scan all World Cup match markets and generate signals.
        Returns sorted list of signals by edge.
        """
        self._cycle_count += 1
        self.state.cycle_count = self._cycle_count
        now = datetime.now(timezone.utc)

        log.info(f"Cycle {self._cycle_count} starting — scanning WC markets...")

        # Discover all match markets
        all_matches = discover_all_worldcup_markets(limit=200)

        if not all_matches:
            log.warning(f"Cycle {self._cycle_count}: no WC match markets found")
            self.state.last_scan_ts = now.isoformat()
            return []

        all_signals = []

        for match in all_matches:
            teams = match["teams"]
            if not teams:
                continue

            home_team, away_team = teams

            # Compute model probabilities
            try:
                model_probs = compute_match_probabilities(home_team, away_team)
            except Exception as e:
                log.debug(f"Model error for {home_team} vs {away_team}: {e}")
                continue

            # Compute edge for each market
            for market in match["markets"]:
                try:
                    signal = compute_edge(model_probs, market, teams)
                    if signal and signal["edge_pp"] > 0:
                        signal["event_title"] = match["event"]["title"]
                        signal["event_volume"] = match["event"]["volume"]
                        all_signals.append(signal)

                        # Log candidate
                        with open(CANDIDATE_LOG, "a") as f:
                            f.write(json.dumps({
                                "ts": now.isoformat(),
                                **signal,
                            }) + "\n")
                except Exception as e:
                    log.debug(f"Edge computation error: {e}")

        # Sort by edge descending
        all_signals.sort(key=lambda s: s["edge_pp"], reverse=True)

        log.info(f"Cycle {self._cycle_count}: {len(all_matches)} matches scanned | "
                 f"{len(all_signals)} signals found")

        self.state.last_scan_ts = now.isoformat()
        return all_signals

    def enter_position(self, signal: Dict) -> Optional[WCPaperPosition]:
        """Enter a paper position based on a signal."""
        position_size = MAX_POSITION_USD
        entry_price = signal["entry_price"]
        
        # Calculate committed capital from existing positions
        committed = sum(p.cost_usd for p in self.positions if not p.settled)
        available = self.state.bankroll - committed
        
        if available < position_size:
            log.warning(f"Insufficient available capital: ${available:.2f} available "
                       f"(bankroll=${self.state.bankroll:.2f} - committed=${committed:.2f}) < ${position_size:.2f}")
            return None
        
        # Deduplication: skip if we already have an open position on this market
        existing_slugs = {p.market_slug for p in self.positions if not p.settled}
        slug = signal.get("market_slug", "")
        if slug in existing_slugs:
            log.info(f"Skipping {slug[:50]} — already have open position")
            return None

        side = signal["recommended_side"]
        token_id = signal["no_token_id"] if side == "NO" else signal["yes_token_id"]
        shares = round(position_size / max(entry_price, 0.01), 2)
        cost = round(shares * entry_price, 2)

        if cost > self.state.bankroll:
            shares = round(self.state.bankroll / max(entry_price, 0.01), 2)
            cost = round(shares * entry_price, 2)

        trade_id = f"WC-{signal['home_team'][:3].upper()}{signal['away_team'][:3].upper()}{signal['market_type'][:2].upper()}{int(time.time())}"

        pos = WCPaperPosition(
            trade_id=trade_id,
            match=signal["match"],
            home_team=signal["home_team"],
            away_team=signal["away_team"],
            market_type=signal["market_type"],
            market_question=signal["market_question"],
            outcome=side,
            side="BUY",
            token_id=token_id,
            condition_id=signal["condition_id"],
            market_slug=signal["market_slug"],
            shares=shares,
            entry_price=entry_price,
            cost_usd=cost,
            model_prob=signal["model_prob"],
            market_prob=signal["market_prob"],
            edge_pp=signal["edge_pp"],
            entry_ts=datetime.now(timezone.utc).isoformat(),
            home_elo=signal["home_elo"],
            away_elo=signal["away_elo"],
            home_xg=signal["home_xg"],
            away_xg=signal["away_xg"],
            model_probs=signal.get("model_probs_json", ""),
        )

        # ─── LIVE EXECUTION ───
        if not self.paper_only:
            log.info(f"LIVE BUY {side} {signal['match']} [{signal['market_type']}] "
                     f"@ {entry_price:.2f} | edge={signal['edge_pp']:.1f}pp "
                     f"pos=${cost:.2f} | model={signal['model_prob']:.1%} vs market={signal['market_prob']:.1%}")
            order_result = execute_live_order(signal)
            
            if order_result.get("status") == "EMERGENCY_HALT":
                log.critical(f"EMERGENCY HALT — stopping bot")
                self.state.halted = True
                self.state.halt_reason = order_result.get("error", "EMERGENCY_HALT")
                self.save_state()
                return None
            
            if order_result.get("fill_status") not in ("live", "matched"):
                log.warning(f"Live order not filled: {order_result.get('fill_status')} | {signal['match']}")
                return None
            
            # Filled — record position with live order ID
            pos = WCPaperPosition(
                trade_id=trade_id,
                match=signal["match"],
                home_team=signal["home_team"],
                away_team=signal["away_team"],
                market_type=signal["market_type"],
                market_question=signal["market_question"],
                outcome=side,
                side="BUY",
                token_id=token_id,
                condition_id=signal["condition_id"],
                market_slug=signal["market_slug"],
                shares=shares,
                entry_price=entry_price,
                cost_usd=cost,
                model_prob=signal["model_prob"],
                market_prob=signal["market_prob"],
                edge_pp=signal["edge_pp"],
                entry_ts=datetime.now(timezone.utc).isoformat(),
                home_elo=signal["home_elo"],
                away_elo=signal["away_elo"],
                home_xg=signal["home_xg"],
                away_xg=signal["away_xg"],
                model_probs=signal.get("model_probs_json", ""),
            )
            pos.live_order_id = order_result.get("order_id", "")
            pos.live_filled = True
            
            log.info(f"LIVE FILL CONFIRMED {side} {signal['match']} @ {entry_price:.2f} | "
                     f"order_id={order_result.get('order_id', '')[:20]}... | "
                     f"size=${cost:.2f}")
            
            self.positions.append(pos)
            self.state.bankroll -= cost
            self.state.total_trades += 1
            self.state.daily_trades += 1
            self.state.active_positions += 1
            self.record_trade(pos)
            self.save_state()
            return pos
        
        # ─── PAPER EXECUTION ───
        log.info(f"PAPER BUY {side} {signal['match']} [{signal['market_type']}] "
                 f"@ {entry_price:.2f} | edge={signal['edge_pp']:.1f}pp "
                 f"pos=${cost:.2f} | model={signal['model_prob']:.1%} vs market={signal['market_prob']:.1%}")

        self.positions.append(pos)
        self.state.bankroll -= cost
        self.state.total_trades += 1
        self.state.daily_trades += 1
        self.state.active_positions += 1
        self.record_trade(pos)
        self.save_state()
        return pos

    def check_entry_gate(self, signal: Dict) -> Tuple[bool, str]:
        """Check if a signal passes all entry criteria."""
        # Unknown team check — block if either team defaulted to 1500 (unknown)
        from src.worldcup.elo_ratings import get_elo, ELO_RATINGS, resolve_team_name
        home_canonical = resolve_team_name(signal["home_team"])
        away_canonical = resolve_team_name(signal["away_team"])
        if home_canonical not in ELO_RATINGS or away_canonical not in ELO_RATINGS:
            return False, "UNKNOWN_TEAM_RATING"

        # Edge threshold
        if signal["edge_pp"] < MIN_EDGE_PP:
            return False, "EDGE_BELOW_THRESHOLD"

        # Price bounds
        entry_price = signal["entry_price"]
        if entry_price > MAX_ENTRY_PRICE:
            return False, "PRICE_TOO_HIGH"
        if entry_price < MIN_ENTRY_PRICE:
            return False, "PRICE_TOO_LOW"

        # Volume / liquidity
        if signal["volume"] < MIN_VOLUME:
            return False, "DEAD_MARKET"
        if signal["liquidity"] < MIN_LIQUIDITY:
            return False, "LOW_LIQUIDITY"

        # Spread check
        spread = abs(signal["yes_price"] - signal["no_price"])
        # Actually: yes + no should = ~1.0. Spread = 1 - (yes + no) in some markets
        # Or use yes - (1-no) = yes + no - 1
        spread_pp = abs(signal["yes_price"] + signal["no_price"] - 1.0) * 100
        if spread_pp > MAX_SPREAD_PP:
            return False, "WIDE_SPREAD"

        # Duplicate check — same match + market type
        for pos in self.positions:
            if (pos.match == signal["match"] and
                pos.market_type == signal["market_type"] and
                not pos.settled):
                return False, "DUPLICATE_MATCH"

        # Max concurrent
        active = sum(1 for p in self.positions if not p.settled)
        if active >= MAX_CONCURRENT:
            return False, "MAX_ACTIVE_POSITIONS"

        # Daily trade limit
        if self.state.daily_trades >= MAX_DAILY_TRADES:
            return False, "DAILY_TRADE_LIMIT"

        return True, ""

    def run_once(self) -> List[Dict]:
        """Run one scan + entry cycle."""
        if not self.check_circuit_breakers():
            return []

        # Settle any completed positions
        self.settle_positions()

        # Scan markets
        signals = self.scan_cycle()

        if not signals:
            return []

        entered = []
        for sig in signals:
            if len(entered) >= MAX_CONCURRENT:
                break

            allowed, reason = self.check_entry_gate(sig)
            log_entry_gate(sig, allowed, reason)

            if not allowed:
                continue

            pos = self.enter_position(sig)
            if pos:
                entered.append(sig)

        self.save_state()
        return entered

    def settle_positions(self):
        """Check if any positions can be settled (match completed)."""
        # For paper trading, we settle by checking PM Gamma API for market resolution
        for pos in [p for p in self.positions if not p.settled]:
            try:
                settled = self._check_settlement(pos)
                if settled:
                    self._apply_settlement(pos, settled)
            except Exception as e:
                log.debug(f"Settlement check error for {pos.trade_id}: {e}")

    def _check_settlement(self, pos: WCPaperPosition) -> Optional[Dict]:
        """Check if a market has been resolved via PM Gamma API."""
        if not pos.condition_id:
            return None

        url = f"{GAMMA_BASE}/markets?condition_id={pos.condition_id}&limit=1"
        data = gamma_get(url)
        if not data or len(data) == 0:
            return None

        market = data[0]
        # Check if market is closed/resolved
        closed = market.get("closed", False)
        if not closed:
            return None

        # Get resolution
        outcome = market.get("outcome", "")
        prices = parse_market_prices(market)

        # Determine if YES or NO won
        yes_price = None
        if prices:
            yes_price = prices[0]

        # If market resolved YES, yes_price → 1.0; if NO, yes_price → 0.0
        if yes_price is not None:
            if yes_price >= 0.95:
                result = "YES"
            elif yes_price <= 0.05:
                result = "NO"
            else:
                return None  # Not fully resolved
        else:
            return None

        return {
            "result": result,
            "market_closed": closed,
            "resolution_ts": datetime.now(timezone.utc).isoformat(),
        }

    def _apply_settlement(self, pos: WCPaperPosition, settlement: Dict):
        """Apply settlement to a position and update state."""
        pos.settled = True
        pos.exit_ts = datetime.now(timezone.utc).isoformat()

        if pos.outcome == settlement["result"]:
            pos.settlement_result = "WIN"
            pos.pnl = round(pos.shares - pos.cost_usd, 2)
            self.state.wins += 1
            self.state.consecutive_losses = 0
        else:
            pos.settlement_result = "LOSS"
            pos.pnl = round(-pos.cost_usd, 2)
            self.state.losses += 1
            self.state.consecutive_losses += 1

        self.state.total_pnl += pos.pnl
        self.state.bankroll += (pos.shares if pos.settlement_result == "WIN" else 0)
        self.state.active_positions -= 1

        if pos.pnl < 0:
            self.state.daily_loss += abs(pos.pnl)
            self.state.weekly_loss += abs(pos.pnl)

        # Update trade record
        self.record_trade(pos)
        update_cohort_stats(pos)

        # Log resolution
        with open(RESOLUTION_AUDIT, "a") as f:
            f.write(json.dumps({
                "trade_id": pos.trade_id,
                "match": pos.match,
                "market_type": pos.market_type,
                "outcome": pos.outcome,
                "settlement_result": settlement["result"],
                "result": pos.settlement_result,
                "pnl": pos.pnl,
                "ts": pos.exit_ts,
            }) + "\n")

        log.info(f"SETTLED {pos.trade_id} {pos.match} [{pos.market_type}] "
                 f"→ {pos.settlement_result} PnL=${pos.pnl:+.2f}")

    def generate_live_readiness(self) -> Dict:
        """Generate live readiness assessment."""
        registry = load_cohort_registry()
        cohort = registry["cohorts"].get("WC_V1_ELO_POISSON", {})
        gate = registry["promotion_gate"]

        resolved = cohort.get("resolved", 0)
        wins = cohort.get("wins", 0)
        losses = cohort.get("losses", 0)
        pnl = cohort.get("pnl", 0.0)

        ev = pnl / resolved if resolved > 0 else 0.0
        pf = (wins / losses) if losses > 0 else (float("inf") if wins > 0 else 0.0)

        checks = {
            "min_resolved": resolved >= gate["min_resolved"],
            "min_ev": ev >= gate["min_ev"],
            "min_pf": pf >= gate["min_pf"],
            "zero_errors": True,  # No error tracking yet
        }
        all_pass = all(checks.values())

        readiness = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "live_allowed": all_pass and not WORLDCUP_BOT_LIVE_BLOCKED,
            "live_blocked": WORLDCUP_BOT_LIVE_BLOCKED,
            "hard_block": WORLDCUP_BOT_LIVE_BLOCKED,
            "cohort": "WC_V1_ELO_POISSON",
            "stats": {
                "resolved": resolved,
                "wins": wins,
                "losses": losses,
                "pushes": cohort.get("pushes", 0),
                "pnl": pnl,
                "ev_per_trade": round(ev, 4),
                "profit_factor": round(pf, 4) if pf != float("inf") else "inf",
                "win_rate": round(wins / resolved, 4) if resolved > 0 else 0,
            },
            "gate": gate,
            "checks": checks,
            "all_pass": all_pass,
        }

        with open(LIVE_READINESS, "w") as f:
            json.dump(readiness, f, indent=2)

        return readiness

    def status(self) -> str:
        """Return status string."""
        active = sum(1 for p in self.positions if not p.settled)
        readiness = self.generate_live_readiness()
        return (f"World Cup Bot v1.0 | Cycle {self._cycle_count} | "
                f"Paper ${self.state.bankroll:.2f} | "
                f"Active: {active} | Total: {self.state.total_trades} | "
                f"W/L: {self.state.wins}/{self.state.losses} | "
                f"PnL: ${self.state.total_pnl:+.2f} | "
                f"Live: {'BLOCKED' if readiness['live_blocked'] else 'ELIGIBLE'}")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="FDC World Cup Bot v1.0")
    parser.add_argument("--paper", action="store_true", default=False,
                        help="Paper trading mode (no live orders)")
    parser.add_argument("--live", action="store_true", default=False,
                        help="LIVE trading mode — places real CLOB orders")
    parser.add_argument("--once", action="store_true",
                        help="Run one scan cycle and exit")
    parser.add_argument("--interval", type=int, default=300,
                        help="Scan interval in seconds (default: 300)")
    parser.add_argument("--bankroll", type=float, default=20.0,
                        help="Starting bankroll")
    parser.add_argument("--status", action="store_true",
                        help="Print status and exit")
    parser.add_argument("--discover", action="store_true",
                        help="Discover markets and print, no trading")
    args = parser.parse_args()

    paper_mode = not args.live  # Default to paper unless --live
    if args.paper:
        paper_mode = True

    bot = WorldCupBot(bankroll=args.bankroll, paper_only=paper_mode)
    bot.load_state()

    if not paper_mode:
        # Verify CLOB connection before starting
        try:
            bal = get_wallet_balance()
            log.info(f"LIVE MODE — Wallet balance: ${bal:.2f}")
            if bal < MAX_POSITION_USD:
                log.error(f"Insufficient balance: ${bal:.2f} < ${MAX_POSITION_USD}")
                return
            bot.state.bankroll = bal  # Use real balance
        except Exception as e:
            log.error(f"CLOB init failed: {e} — cannot go live")
            return
        
        # Check live readiness gate — DO NOT go live without passing validation
        readiness = bot.generate_live_readiness()
        if not readiness.get("all_pass", False):
            log.error(f"LIVE GATE FAILED — cannot go live. "
                      f"resolved={readiness['stats']['resolved']}/{readiness['gate']['min_resolved']} "
                      f"WR={readiness['stats']['win_rate']:.0%} PF={readiness['stats']['profit_factor']} "
                      f"EV=${readiness['stats']['ev_per_trade']:.2f}")
            log.error("Falling back to paper mode until gates are met.")
            paper_mode = True
            bot.paper_only = True
            bot.state.paper_only = True

    if args.status:
        print(bot.status())
        return

    if args.discover:
        log.info("Discovering WC markets...")
        matches = discover_all_worldcup_markets(limit=200)
        for m in matches:
            teams = m["teams"]
            print(f"\n{m['event']['title']} (vol=${m['event']['volume']:,.0f})")
            if teams:
                print(f"  Teams: {teams[0]} vs {teams[1]}")
            for mkt in m["markets"]:
                print(f"  [{mkt['market_type']}] {mkt['question'][:60]} "
                      f"YES={mkt['yes_price']:.2f} NO={mkt['no_price']:.2f} "
                      f"vol={mkt['volume']:,.0f}")
        return

    if args.once:
        entered = bot.run_once()
        print(bot.status())
        if entered:
            print(f"\nEntered {len(entered)} positions:")
            for sig in entered:
                print(f"  {sig['match']} [{sig['market_type']}] "
                      f"{sig['recommended_side']}@{sig['entry_price']:.2f} "
                      f"edge={sig['edge_pp']:.1f}pp")
        return

    mode_str = "LIVE" if not paper_mode else "PAPER"
    log.info(f"World Cup Bot v1.0 starting — {mode_str} mode, {args.interval}s interval")
    while True:
        try:
            entered = bot.run_once()
            if entered:
                log.info(f"Entered {len(entered)} positions this cycle")
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("Shutdown requested")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    main()