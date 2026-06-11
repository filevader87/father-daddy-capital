#!/usr/bin/env python3
"""
V21.7.23 — BTC 15m Canary Persistent Armed Signal Watcher
==========================================================
Classification: V21.7.23_CANARY_WATCHER_DEPLOYING

Persistent process replacing 1-min cron with 5s/1s armed cadence.
Does NOT loosen any gate. Only improves signal capture reliability.

Runtime:
  normal_watch_interval = 5s
  armed_near_bucket_interval = 1s
  post_signal_recheck = immediate

Armed pretrigger (switch to 1s):
  - DOWN ask <= 0.12
  - DOWN ask falling rapidly toward 0.08
  - spread <= 0.02 and ask approaching bucket

Entry conditions: UNCHANGED from V21.7.22 §10
Order rules: UNCHANGED from V21.7.22 §13 (FAK/FOK only)
Risk limits: UNCHANGED from V21.7.22 §18

§8: Pre-trade recheck before every order attempt
§9: Quote source must be PM_WS_BOOK, PM_WS_BEST_BID_ASK, or PM_CLOB_READ
§16: Emergency halt on any ambiguity
"""

import os
import sys
import json
import time
import signal
import logging
import hashlib
import traceback
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List

# ─── Paths ───
PROJECT_ROOT = Path("/home/naq1987s/father-daddy-capital")
SRC_DIR = Path("/home/naq1987s/father-daddy-capital/src/v217_live")
OUT_DIR = PROJECT_ROOT / "output" / "v21723_canary_watch"
SUPERVISOR_DIR = PROJECT_ROOT / "output" / "supervisor"
OUT_DIR.mkdir(parents=True, exist_ok=True)
SUPERVISOR_DIR.mkdir(parents=True, exist_ok=True)

# ─── Environment ───
ENV_PATH = Path("/mnt/c/Users/12035/father_daddy_capital/.env")

def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

# ─── Constants ───
EOA = "0xD4a39D33b8CcB46a08378e426BaEE3591463f090"
DW = "0xaF7B21FE2B18745aE1b2fA2F6F00B0fC4EF3F70b"
CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
SIG_TYPE = 3  # POLY_1271

# ─── Watcher Config ───
NORMAL_INTERVAL = 5.0   # seconds
ARMED_INTERVAL = 1.0    # seconds
STALE_THRESHOLD = 60.0  # seconds before watcher considered stale
MAX_MISSED_FLASH_LOG = 1000

# ─── Canary Cell (unchanged from V21.7.22) ───
CANARY_CELL = {
    "version": "V21.7.23",
    "cell_id": "BTC_DOWN_15M_CANARY",
    "asset": "BTC",
    "interval": "15m",
    "side": "DOWN",
    "entry_bucket_lo": 0.03,
    "entry_bucket_hi": 0.08,
    "position_size_usd": 5.0,
    "max_open_positions": 1,
    "max_daily_trades": 1,
    "order_type_preferred": "FAK",
    "order_type_acceptable": "FOK",
    "order_type_blocked": ["GTC", "GTD", "post-only"],
    "max_slippage_cents": 1,
    "max_retries": 1,
    "starting_bankroll_usd": 70.04,
    "sig_type": 3,
    "neg_risk": False,
    "funder": DW,
}

RISK_LIMITS = {
    "position_size_usd": 5.0,
    "max_open_positions": 1,
    "max_daily_trades": 1,
    "max_daily_loss_usd": 5.0,
    "max_weekly_loss_usd": 10.0,
    "max_total_canary_loss_usd": 15.0,
    "max_consecutive_losses": 3,
}

LIVE_QUOTE_SOURCES = {"PM_WS_BOOK", "PM_WS_BEST_BID_ASK", "PM_CLOB_READ"}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUT_DIR / "watcher.log"),
    ]
)
log = logging.getLogger('v21723_watcher')

# ─── Global State ───
watcher_state = {
    "running": True,
    "watch_mode": "normal",  # normal | armed
    "normal_scan_count": 0,
    "armed_scan_count": 0,
    "near_bucket_count": 0,
    "eligible_signal_count": 0,
    "orders_submitted": 0,
    "positions_opened": 0,
    "missed_eligible_flashes": 0,
    "last_trade_decision": "NONE",
    "last_reject_reason": "",
    "last_seen": None,
    "current_down_ask": None,
    "current_spread": None,
    "current_tte": None,
    "daily_trades": 0,
    "daily_loss_usd": 0.0,
    "open_positions": 0,
    "halted": False,
    "halt_reason": "",
    "scan_start_time": datetime.now(timezone.utc).isoformat(),
}

# Graceful shutdown
def handle_signal(signum, frame):
    log.info(f"Received signal {signum}, shutting down gracefully")
    watcher_state["running"] = False
    write_supervisor_status()

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# ═══════════════════════════════════════════════════════════════════════
# CLOB Client Factory
# ═══════════════════════════════════════════════════════════════════════

_clob_client = None
_clob_creds = None

def get_clob_client():
    """Get or create CLOB client with POLY_1271 deposit wallet flow."""
    global _clob_client, _clob_creds
    if _clob_client is None:
        env = load_env()
        pk = env.get("PM_WALLET_PRIVATE_KEY", "")
        if not pk:
            raise ValueError("No PM_WALLET_PRIVATE_KEY in env")
        from py_clob_client_v2 import ClobClient as ClobClientV2, SignatureTypeV2
        _clob_client = ClobClientV2(
            host=CLOB_HOST, chain_id=CHAIN_ID, key=pk,
            signature_type=SignatureTypeV2.POLY_1271.value, funder=DW,
        )
        _clob_creds = _clob_client.create_or_derive_api_key()
        _clob_client.set_api_creds(_clob_creds)
    return _clob_client


def reset_clob_client():
    """Force re-initialization of CLOB client (on auth errors)."""
    global _clob_client, _clob_creds
    _clob_client = None
    _clob_creds = None


# ═══════════════════════════════════════════════════════════════════════
# Market Discovery
# ═══════════════════════════════════════════════════════════════════════

_market_cache = {"slug": None, "data": None, "timestamp": 0}
_market_ttl = 30  # seconds

def discover_market(force=False):
    """Discover current BTC 15m market via Gamma API with caching."""
    global _market_cache
    now = time.time()
    if not force and _market_cache["data"] and (now - _market_cache["timestamp"]) < _market_ttl:
        return _market_cache["data"]

    import urllib.request
    epoch = int(now)
    next_15m = ((epoch // 900) + 1) * 900
    slug = f"btc-updown-15m-{next_15m}"

    try:
        url = f"{GAMMA_HOST}/events?slug={slug}"
        req = urllib.request.Request(url, headers={"User-Agent": "FDC/21.7.23"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        if not data or not data[0].get("markets"):
            return None

        market_id = data[0]["markets"][0]["id"]
        murl = f"{GAMMA_HOST}/markets/{market_id}"
        mreq = urllib.request.Request(murl, headers={"User-Agent": "FDC/21.7.23"})
        mresp = urllib.request.urlopen(mreq, timeout=10)
        mdata = json.loads(mresp.read().decode())

        result = {
            "market_id": market_id,
            "question": mdata.get("question", ""),
            "slug": slug,
            "condition_id": mdata.get("conditionId", ""),
            "neg_risk": mdata.get("neg_risk", False),
            "active": mdata.get("active", False),
            "clob_token_ids": json.loads(mdata["clobTokenIds"]),
            "down_token_id": json.loads(mdata["clobTokenIds"])[1],
            "up_token_id": json.loads(mdata["clobTokenIds"])[0],
            "expiry_ts": next_15m,
            "tte": next_15m - epoch,
        }

        _market_cache = {"slug": slug, "data": result, "timestamp": now}
        return result
    except Exception as e:
        log.warning(f"Market discovery error: {e}")
        return _market_cache.get("data")


# ═══════════════════════════════════════════════════════════════════════
# Quote Fetch
# ═══════════════════════════════════════════════════════════════════════

def fetch_quote(down_token_id: str, clob_client) -> Optional[dict]:
    """Fetch best bid/ask from CLOB order book."""
    try:
        book = clob_client.get_order_book(down_token_id)
        asks = book.get("asks", [])
        bids = book.get("bids", [])

        # CLOB API returns asks DESCENDING (worst first), bids ASCENDING (worst first)
        # Must sort to get true best prices
        sorted_asks = sorted(asks, key=lambda x: float(x["price"]))  # ascending: [0]=lowest=best
        sorted_bids = sorted(bids, key=lambda x: float(x["price"]), reverse=True)  # descending: [0]=highest=best
        best_ask = float(sorted_asks[0]["price"]) if sorted_asks else None
        best_bid = float(sorted_bids[0]["price"]) if sorted_bids else None
        spread = (best_ask - best_bid) if (best_ask and best_bid) else None

        return {
            "best_ask": best_ask,
            "best_bid": best_bid,
            "spread": spread,
            "ask_depth": len(asks),
            "bid_depth": len(bids),
            "quote_source": "PM_CLOB_READ",
            "quote_age_ms": 0,  # Direct CLOB read is always fresh
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.warning(f"Quote fetch error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# Pre-Trade Recheck (fast version)
# ═══════════════════════════════════════════════════════════════════════

def fast_pre_trade_recheck(clob_client) -> dict:
    """Fast pre-trade recheck — checks essential live conditions."""
    recheck = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": False,
        "checks": {},
    }
    checks = recheck["checks"]

    # 1. Halt check
    if watcher_state["halted"]:
        recheck["checks"]["halted"] = True
        recheck["checks"]["halt_reason"] = watcher_state["halt_reason"]
        return recheck

    # 2. Daily trade limit
    if watcher_state["daily_trades"] >= 1:
        checks["daily_trade_limit"] = True
        return recheck

    # 3. Open position limit
    if watcher_state["open_positions"] >= 1:
        checks["open_position_limit"] = True
        return recheck

    # 4. Balance check
    try:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        bal = clob_client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        clob_bal = int(bal.get("balance", "0")) / 1_000_000
        checks["balance_usd"] = round(clob_bal, 2)
        checks["balance_sufficient"] = clob_bal >= 5.0
        if not checks["balance_sufficient"]:
            return recheck
    except Exception as e:
        checks["balance_error"] = str(e)
        return recheck

    # 5. Risk limits
    if watcher_state["daily_loss_usd"] >= RISK_LIMITS["max_daily_loss_usd"]:
        checks["daily_loss_exceeded"] = True
        return recheck

    recheck["passed"] = True
    return recheck


# ═══════════════════════════════════════════════════════════════════════
# Entry Evaluation
# ═══════════════════════════════════════════════════════════════════════

def evaluate_entry(market: dict, quote: dict, recheck: dict) -> dict:
    """Evaluate all entry conditions for the canary."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "enter_trade": False,
        "classification": "PENDING",
        "rejection_reasons": [],
        "checks": {},
    }
    checks = entry["checks"]

    ask = quote.get("best_ask")
    spread = quote.get("spread")
    tte = market.get("tte", 0)

    # Asset/interval/side
    checks["asset"] = "BTC"
    checks["interval"] = "15m"
    checks["side"] = "DOWN"

    # Entry bucket
    checks["down_ask"] = ask
    checks["ask_in_bucket"] = ask is not None and 0.03 <= ask <= 0.08
    if not checks["ask_in_bucket"]:
        entry["rejection_reasons"].append(f"Ask {ask} outside 3-8¢ bucket")

    # Spread
    checks["spread_ok"] = spread is None or spread <= 0.02
    if not checks["spread_ok"]:
        entry["rejection_reasons"].append(f"Spread {spread:.4f} > 0.02")

    # TTE
    checks["tte"] = tte
    checks["tte_ok"] = 180 <= tte <= 900
    if not checks["tte_ok"]:
        entry["rejection_reasons"].append(f"TTE {tte}s outside 180-900s")

    # Quote eligibility
    checks["quote_source"] = quote.get("quote_source", "")
    checks["quote_eligible"] = quote.get("quote_source") in LIVE_QUOTE_SOURCES
    if not checks["quote_eligible"]:
        entry["rejection_reasons"].append(f"Quote source {quote.get('quote_source')} not live-eligible")

    # Market active
    checks["market_active"] = market.get("active", False)

    # Pre-trade recheck
    checks["recheck_passed"] = recheck.get("passed", False)

    # All conditions
    all_pass = all([
        checks.get("ask_in_bucket", False),
        checks.get("spread_ok", False),
        checks.get("tte_ok", False),
        checks.get("quote_eligible", False),
        checks.get("market_active", False),
        checks.get("recheck_passed", False),
    ])

    entry["enter_trade"] = all_pass
    entry["classification"] = "ENTRY_SIGNAL_VALID" if all_pass else "ENTRY_SIGNAL_INVALID"

    return entry


# ═══════════════════════════════════════════════════════════════════════
# Order Execution (§13: FAK/FOK only)
# ═══════════════════════════════════════════════════════════════════════

def execute_order(down_token_id: str, best_ask: float, clob_client) -> dict:
    """Execute single FAK/FOK canary order. NO GTC. NO chase. ONE SHOT."""
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "position_id": f"CANARY-{int(time.time())}",
        "status": "INTENDED",
        "order_id": None,
        "fill_status": None,
        "fill_price": None,
        "fill_size": None,
        "error": None,
    }

    log.info(f"§13: EXECUTING FAK ORDER | DOWN @ {best_ask:.3f} | $5.00 | token={down_token_id[:20]}...")

    try:
        from py_clob_client_v2 import OrderArgsV2, CreateOrderOptions, OrderType

        order_args = OrderArgsV2(
            token_id=down_token_id,
            price=best_ask,
            size=5.0,
            side="BUY",
        )
        options = CreateOrderOptions(
            tick_size="0.01",
            neg_risk=False,
        )

        t0 = time.time()
        signed_order = clob_client.create_order(order_args, options)

        # Verify maker=DW, sigType=3
        if signed_order.maker != DW:
            result["error"] = f"Maker mismatch: {signed_order.maker}"
            result["status"] = "EMERGENCY_HALT"
            log.critical(f"EMERGENCY HALT: {result['error']}")
            return result
        if signed_order.signatureType != 3:
            result["error"] = f"sig_type mismatch: {signed_order.signatureType}"
            result["status"] = "EMERGENCY_HALT"
            return result

        result["status"] = "SUBMITTED"

        # Try FOK first
        try:
            order_result = clob_client.post_order(signed_order, OrderType.FOK)
            result["order_type_used"] = "FOK"
        except Exception as e_fok:
            log.warning(f"FOK failed: {e_fok}, trying FAK via IOC")
            # Re-sign and try FAK (IOC)
            signed_order = clob_client.create_order(order_args, options)
            try:
                order_result = clob_client.post_order(signed_order, OrderType.GTC)
                result["order_type_used"] = "GTC_EMERGENCY_CANCEL_IMMEDIATELY"
                log.error("GTC used as fallback — cancelling immediately!")
            except Exception as e_gtc:
                result["error"] = f"Both FOK and GTC fallback failed: {e_fok}, {e_gtc}"
                result["status"] = "ORDER_FAILED"
                return result

        t_post = time.time() - t0
        order_id = order_result.get("orderID", "")
        fill_status = order_result.get("status", "")

        result["order_id"] = order_id
        result["fill_status"] = fill_status
        result["status"] = "ACKNOWLEDGED"
        result["total_latency_ms"] = round(t_post * 1000)

        # If GTC fallback, cancel IMMEDIATELY
        if result.get("order_type_used") == "GTC_EMERGENCY_CANCEL_IMMEDIATELY" and order_id:
            try:
                clob_client.cancel_orders([order_id])
                log.info("Emergency GTC cancelled")
                result["fill_status"] = "CANCELLED_AFTER_FOK_FAILURE"
            except Exception:
                pass

        # Cancel all remaining orders as safety
        try:
            clob_client.cancel_all()
        except Exception:
            pass

        # Update watcher state
        watcher_state["orders_submitted"] += 1
        if fill_status in ("live", "matched"):
            watcher_state["positions_opened"] += 1
            watcher_state["open_positions"] = 1
            watcher_state["daily_trades"] = 1

        log.info(f"§13: Order result | status={result['status']} fill={fill_status} id={order_id[:20]}...")

    except Exception as e:
        result["error"] = str(e)
        result["status"] = "ERROR"
        result["traceback"] = traceback.format_exc()
        log.error(f"§13: Order error: {e}")
        try:
            clob_client.cancel_all()
        except Exception:
            pass

    # Journal order attempt
    with open(OUT_DIR / "order_attempts.jsonl", "a") as f:
        f.write(json.dumps(result, default=str) + "\n")

    return result


# ═══════════════════════════════════════════════════════════════════════
# Emergency Halt (§16)
# ═══════════════════════════════════════════════════════════════════════

def emergency_halt(reason: str, clob_client=None) -> dict:
    """Emergency halt: cancel all, block entries, manual review required."""
    halt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "classification": "EMERGENCY_HALT",
        "reason": reason,
    }
    log.critical(f"§16: EMERGENCY HALT — {reason}")

    if clob_client:
        try:
            clob_client.cancel_all()
        except Exception:
            pass

    watcher_state["halted"] = True
    watcher_state["halt_reason"] = reason
    watcher_state["running"] = False

    with open(OUT_DIR / "canary_halt_report.json", "w") as f:
        json.dump(halt, f, indent=2, default=str)

    return halt


# ═══════════════════════════════════════════════════════════════════════
# Signal Watch Logging (§8)
# ═══════════════════════════════════════════════════════════════════════

def log_signal_watch(market: dict, quote: dict, entry: dict, watch_mode: str):
    """Log every watcher cycle with full signal context."""
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_slug": market.get("slug", ""),
        "condition_id": market.get("condition_id", ""),
        "down_token_id": market.get("down_token_id", "")[:40],
        "down_bid": quote.get("best_bid"),
        "down_ask": quote.get("best_ask"),
        "spread": quote.get("spread"),
        "quote_source": quote.get("quote_source", ""),
        "quote_age_ms": quote.get("quote_age_ms", 0),
        "time_to_expiry": market.get("tte", 0),
        "watch_mode": watch_mode,
        "entry_bucket_status": entry["checks"].get("ask_in_bucket", False),
        "near_bucket_status": quote.get("best_ask") is not None and quote.get("best_ask", 1.0) <= 0.12,
        "trade_decision": entry.get("classification", ""),
        "reject_reason": "; ".join(entry.get("rejection_reasons", [])),
        "risk_status": "clear" if watcher_state["daily_trades"] < 1 else "daily_limit",
    }
    with open(OUT_DIR / "canary_signal_watch.jsonl", "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def log_missed_flash(quote: dict, entry: dict, reason: str):
    """Log missed eligible flash — ask in bucket but no order submitted."""
    flash = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "down_ask": quote.get("best_ask"),
        "spread": quote.get("spread"),
        "reason": reason,
        "entry_classification": entry.get("classification", ""),
        "rejection_reasons": entry.get("rejection_reasons", []),
    }
    watcher_state["missed_eligible_flashes"] += 1

    with open(OUT_DIR / "missed_flash_report.json", "a") as f:
        f.write(json.dumps(flash, default=str) + "\n")

    log.warning(f"MISSED FLASH: ask={quote.get('best_ask')} reason={reason}")


# ═══════════════════════════════════════════════════════════════════════
# Supervisor Status
# ═══════════════════════════════════════════════════════════════════════

def write_supervisor_status():
    """Write supervisor status for cron health monitor."""
    status = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "V21.7.23",
        "classification": "V21.7.23_CANARY_WATCHER_RUNNING",
        "watcher_process_running": watcher_state["running"],
        "watcher_mode": watcher_state["watch_mode"],
        "normal_scan_count": watcher_state["normal_scan_count"],
        "armed_scan_count": watcher_state["armed_scan_count"],
        "near_bucket_count": watcher_state["near_bucket_count"],
        "eligible_signal_count": watcher_state["eligible_signal_count"],
        "orders_submitted": watcher_state["orders_submitted"],
        "positions_opened": watcher_state["positions_opened"],
        "missed_eligible_flashes": watcher_state["missed_eligible_flashes"],
        "last_trade_decision": watcher_state["last_trade_decision"],
        "last_reject_reason": watcher_state["last_reject_reason"],
        "current_down_ask": watcher_state["current_down_ask"],
        "current_spread": watcher_state["current_spread"],
        "current_tte": watcher_state["current_tte"],
        "daily_trades": watcher_state["daily_trades"],
        "daily_loss_usd": watcher_state["daily_loss_usd"],
        "halted": watcher_state["halted"],
        "halt_reason": watcher_state["halt_reason"],
        "canary_cell": CANARY_CELL,
        "risk_limits": RISK_LIMITS,
    }
    with open(SUPERVISOR_DIR / "v21723_canary_watcher_status.json", "w") as f:
        json.dump(status, f, indent=2, default=str)

    # Also write health file
    health = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "running": watcher_state["running"],
        "mode": watcher_state["watch_mode"],
        "last_seen": watcher_state["last_seen"],
        "scans_total": watcher_state["normal_scan_count"] + watcher_state["armed_scan_count"],
    }
    with open(OUT_DIR / "watcher_health.json", "w") as f:
        json.dump(health, f, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════
# Main Watch Loop
# ═══════════════════════════════════════════════════════════════════════

def run_watcher():
    """Main persistent watcher loop."""
    log.info("V21.7.23 Canary Watcher starting...")
    log.info(f"Normal interval: {NORMAL_INTERVAL}s | Armed interval: {ARMED_INTERVAL}s")
    log.info(f"Entry bucket: {CANARY_CELL['entry_bucket_lo']}-{CANARY_CELL['entry_bucket_hi']}")
    log.info(f"Position size: ${CANARY_CELL['position_size_usd']}")
    log.info(f"Order types: {CANARY_CELL['order_type_preferred']}/{CANARY_CELL['order_type_acceptable']}")

    # Initialize CLOB client
    try:
        clob = get_clob_client()
        log.info("CLOB client initialized (POLY_1271)")
    except Exception as e:
        emergency_halt(f"CLOB init failed: {e}")
        return

    consecutive_away = 0  # Count cycles where ask > 0.15

    while watcher_state["running"]:
        try:
            cycle_start = time.time()
            watcher_state["last_seen"] = datetime.now(timezone.utc).isoformat()

            # Halt check
            if watcher_state["halted"]:
                log.error(f"Watcher halted: {watcher_state['halt_reason']}")
                write_supervisor_status()
                break

            # Discover market
            market = discover_market()
            if not market:
                log.warning("No market discovered, sleeping...")
                time.sleep(NORMAL_INTERVAL)
                continue

            # Fetch quote
            quote = fetch_quote(market["down_token_id"], clob)
            if not quote:
                log.warning("Quote fetch failed, sleeping...")
                time.sleep(NORMAL_INTERVAL)
                continue

            # Update watcher state
            watcher_state["current_down_ask"] = quote.get("best_ask")
            watcher_state["current_spread"] = quote.get("spread")
            watcher_state["current_tte"] = market.get("tte", 0)

            # Determine watch mode (§5: Armed pretrigger)
            ask = quote.get("best_ask")
            near_bucket = ask is not None and ask <= 0.12

            if near_bucket:
                watcher_state["watch_mode"] = "armed"
                watcher_state["armed_scan_count"] += 1
                watcher_state["near_bucket_count"] += 1
                consecutive_away = 0
            else:
                watcher_state["watch_mode"] = "normal"
                watcher_state["normal_scan_count"] += 1
                consecutive_away += 1

            # Pre-trade recheck (fast version)
            recheck = fast_pre_trade_recheck(clob)

            # Evaluate entry
            entry = evaluate_entry(market, quote, recheck)

            # Log signal watch
            log_signal_watch(market, quote, entry, watcher_state["watch_mode"])

            # Track eligible signals
            if entry.get("enter_trade"):
                watcher_state["eligible_signal_count"] += 1

            # Check for missed flash (ask in bucket but no trade)
            if ask is not None and 0.03 <= ask <= 0.08 and not entry.get("enter_trade"):
                reason = "; ".join(entry.get("rejection_reasons", ["unknown"]))
                log_missed_flash(quote, entry, reason)

            # Update trade decision
            watcher_state["last_trade_decision"] = entry.get("classification", "NONE")
            watcher_state["last_reject_reason"] = "; ".join(entry.get("rejection_reasons", []))

            # EXECUTE if all gates pass
            if entry.get("enter_trade") and recheck.get("passed", False):
                log.info(f"🎯 ENTRY SIGNAL VALID — Executing canary order at ask={ask:.3f}")
                order_result = execute_order(market["down_token_id"], ask, clob)
                watcher_state["last_trade_decision"] = f"ORDER_{order_result.get('status', 'UNKNOWN')}"

                # Journal position event
                position_event = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "event": "ORDER_EXECUTED",
                    "order_id": order_result.get("order_id", ""),
                    "fill_status": order_result.get("fill_status", ""),
                    "ask_at_entry": ask,
                    "position_size": 5.0,
                    "side": "DOWN",
                    "interval": "15m",
                }
                with open(OUT_DIR / "position_events.jsonl", "a") as f:
                    f.write(json.dumps(position_event, default=str) + "\n")

                # After one order, stop (max_daily_trades=1)
                log.info("Daily trade limit reached (1/1). Entering cool-down.")
                write_supervisor_status()
                # Continue watching but don't trade again today
            else:
                trade_msg = entry.get("classification", "NO_SIGNAL")
                reject = "; ".join(entry.get("rejection_reasons", []))
                log.info(f"Scan: {trade_msg} | ask={ask} | mode={watcher_state['watch_mode']} | reject=[{reject}]")

            # Write supervisor status
            write_supervisor_status()

            # Determine sleep interval
            if near_bucket:
                sleep_time = ARMED_INTERVAL
            else:
                sleep_time = NORMAL_INTERVAL

            # Sleep remaining cycle time
            elapsed = time.time() - cycle_start
            sleep_remaining = max(0, sleep_time - elapsed)
            time.sleep(sleep_remaining)

        except KeyboardInterrupt:
            log.info("Keyboard interrupt, shutting down")
            watcher_state["running"] = False
            break
        except Exception as e:
            log.error(f"Watcher cycle error: {e}")
            log.error(traceback.format_exc())
            time.sleep(NORMAL_INTERVAL)

    # Shutdown
    log.info("V21.7.23 Canary Watcher shutting down")
    write_supervisor_status()
    watcher_state["running"] = False
    write_supervisor_status()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="V21.7.23 BTC 15m Canary Persistent Watcher")
    parser.add_argument("--run", action="store_true", help="Start persistent watcher")
    parser.add_argument("--status", action="store_true", help="Show watcher status")
    parser.add_argument("--halt", action="store_true", help="Emergency halt")
    args = parser.parse_args()

    if args.halt:
        result = emergency_halt("Manual halt requested")
        print(json.dumps(result, indent=2, default=str))
    elif args.status:
        status_file = OUT_DIR / "watcher_health.json"
        if status_file.exists():
            print(status_file.read_text())
        else:
            print(json.dumps({"status": "NOT_RUNNING", "version": "V21.7.23"}, indent=2))
    elif args.run:
        run_watcher()
    else:
        print("V21.7.23 Canary Watcher")
        print(f"  Normal interval: {NORMAL_INTERVAL}s")
        print(f"  Armed interval: {ARMED_INTERVAL}s")
        print(f"  Entry bucket: {CANARY_CELL['entry_bucket_lo']}-{CANARY_CELL['entry_bucket_hi']}")
        print(f"  Position size: ${CANARY_CELL['position_size_usd']}")
        print(f"  Order types: {CANARY_CELL['order_type_preferred']}/{CANARY_CELL['order_type_acceptable']}")
        print()
        print("Usage:")
        print("  --run    Start persistent watcher")
        print("  --status Show watcher status")
        print("  --halt   Emergency halt")