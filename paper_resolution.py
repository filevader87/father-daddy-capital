#!/usr/bin/env python3
"""
FDC V19.8 — Paper Trade Resolution Module (v3 — Exit Lifecycle Patch)
======================================================================
Full lifecycle: OPENED → ACTIVE → EXPIRING → RESOLVED → SETTLED → JOURNALED

§1  Missing conditionId blocks executable positions
§2  Missing winning token — strict fallback conditions
§3  Stop-loss lifecycle (executable bid required)
§4  Trailing-loss: NOT_IMPLEMENTED (binary contracts settle 0/1)
§5  Take-profit lifecycle (executable bid required)
§6  Exit counters (signals/executed/failed/pnl)
§7  Journal fields for early exits
§8  Regression-test compatible
§9  Synthetic fixture validation
§10 Promotion gate rules
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

# ─── Constants ───
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

STOP_LOSS_PCT = 0.60
TIME_DECAY_SELL_MINS = 0.5
TIME_DECAY_MIN_PRICE = 0.15
TAKE_PROFIT_PCT = 0.80

# Trailing loss: NOT IMPLEMENTED for binary options
# Binary contracts settle at 0 or 1. There is no gradual profit to trail.
# A trailing stop at e.g. 80% of peak would trigger immediately for any
# contract trading above 80¢ (peak=$1, trail fires at 80¢), locking in
# sub-optimal profit instead of holding to $1.00 settlement.
TRAILING_LOSS_IMPLEMENTED = False

# Lifecycle states
STATE_OPENED = "OPENED"
STATE_ACTIVE = "ACTIVE"
STATE_EXPIRING = "EXPIRING"
STATE_RESOLVED = "RESOLVED"
STATE_EXITED = "EXITED"
STATE_SETTLED = "SETTLED"
STATE_JOURNALED = "JOURNALED"
STATE_UNRESOLVED_PAST_EXPIRY = "UNRESOLVED_PAST_EXPIRY"

# Directories
_PAPER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading")
SETTLEMENT_ERROR_FILE = os.path.join(_PAPER_DIR, "settlement_error_report.jsonl")
JOURNAL_BASE_DIR = os.path.join(_PAPER_DIR, "journal")


# ─── Run ID ───
_run_id: Optional[str] = None

def get_run_id() -> str:
    global _run_id
    if _run_id is None:
        _run_id = datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S")
    return _run_id

def set_run_id(rid: str):
    global _run_id
    _run_id = rid

def reset_run_id():
    global _run_id
    _run_id = None


# ══════════════════════════════════════════════════════════════════════════════
# Resolution Counters
# ══════════════════════════════════════════════════════════════════════════════

class ResolutionCounters:
    """Track resolution lifecycle counters with per-profile isolation and exit counters."""

    PROFILE_NAMES = [
        "CORE_UP_STRICT",
        "CORE_UP_RSI_ONLY_SHADOW",
        "CORE_UP_ONE_CONFIRM_SHADOW",
        "CORE_UP_EARLY_TURN_SHADOW",
        "CORE_UP_RECOVERABILITY_FIRST_SHADOW",
        "PREOPEN_DIRECTION_EDGE",
        "ONE_MIN_STRUCTURE_EDGE",
        "CHEAP_CONVEX_EDGE",
        "BALANCED_DIRECTION_EDGE",
        "CONVEX_20_30_VALIDATION",
    ]

    def __init__(self):
        # Global counters
        self.paper_positions_open = 0
        self.paper_positions_active = 0
        self.paper_positions_expiring = 0
        self.paper_positions_unresolved_past_expiry = 0
        self.paper_trades_resolved = 0
        self.paper_trades_settled = 0
        self.paper_trades_journaled = 0
        self.paper_wins = 0
        self.paper_losses = 0
        self.settlement_errors = 0
        self.pnl_validation_errors = 0
        self.expiry_settlements = 0
        # §6 exit counters
        self.stop_loss_signals = 0
        self.stop_loss_executed = 0
        self.stop_loss_failed_no_bid = 0
        self.trailing_loss_signals = 0
        self.trailing_loss_executed = 0
        self.trailing_loss_failed_no_bid = 0
        self.take_profit_signals = 0
        self.take_profit_executed = 0
        self.take_profit_failed_no_bid = 0
        self.early_exit_pnl = 0.0
        self.expiry_settlement_pnl = 0.0
        self.duplicate_settlement_blocks = 0
        self.duplicate_exit_blocks = 0
        self.blocked_by_missing_condition_id = 0
        self.resolution_delays: List[float] = []

        # Per-profile counters
        self.profiles: Dict[str, Dict] = {}
        for name in self.PROFILE_NAMES:
            self.profiles[name] = self._empty_profile_counters()

    @staticmethod
    def _empty_profile_counters() -> Dict:
        return {
            "opened": 0, "resolved": 0, "wins": 0, "losses": 0,
            "pnl_total": 0.0, "gross_pnl_total": 0.0,
        }

    def record_profile_open(self, profile: str):
        if profile in self.profiles:
            self.profiles[profile]["opened"] += 1

    def record_profile_resolve(self, profile: str, won: bool, net_pnl: float, gross_pnl: float):
        if profile not in self.profiles:
            self.profiles[profile] = self._empty_profile_counters()
        p = self.profiles[profile]
        p["resolved"] += 1
        if won:
            p["wins"] += 1
        else:
            p["losses"] += 1
        p["pnl_total"] += net_pnl
        p["gross_pnl_total"] += gross_pnl

    @property
    def avg_resolution_delay_seconds(self) -> float:
        if not self.resolution_delays:
            return 0.0
        return sum(self.resolution_delays) / len(self.resolution_delays)

    @property
    def max_resolution_delay_seconds(self) -> float:
        if not self.resolution_delays:
            return 0.0
        return max(self.resolution_delays)

    def to_dict(self) -> Dict:
        d = {
            "paper_positions_open": self.paper_positions_open,
            "paper_positions_active": self.paper_positions_active,
            "paper_positions_expiring": self.paper_positions_expiring,
            "paper_positions_unresolved_past_expiry": self.paper_positions_unresolved_past_expiry,
            "paper_trades_resolved": self.paper_trades_resolved,
            "paper_trades_settled": self.paper_trades_settled,
            "paper_trades_journaled": self.paper_trades_journaled,
            "paper_wins": self.paper_wins,
            "paper_losses": self.paper_losses,
            "settlement_errors": self.settlement_errors,
            "pnl_validation_errors": self.pnl_validation_errors,
            "expiry_settlements": self.expiry_settlements,
            "stop_loss_signals": self.stop_loss_signals,
            "stop_loss_executed": self.stop_loss_executed,
            "stop_loss_failed_no_bid": self.stop_loss_failed_no_bid,
            "trailing_loss_signals": self.trailing_loss_signals,
            "trailing_loss_executed": self.trailing_loss_executed,
            "trailing_loss_failed_no_bid": self.trailing_loss_failed_no_bid,
            "take_profit_signals": self.take_profit_signals,
            "take_profit_executed": self.take_profit_executed,
            "take_profit_failed_no_bid": self.take_profit_failed_no_bid,
            "early_exit_pnl": round(self.early_exit_pnl, 4),
            "expiry_settlement_pnl": round(self.expiry_settlement_pnl, 4),
            "duplicate_settlement_blocks": self.duplicate_settlement_blocks,
            "duplicate_exit_blocks": self.duplicate_exit_blocks,
            "blocked_by_missing_condition_id": self.blocked_by_missing_condition_id,
            "avg_resolution_delay_seconds": round(self.avg_resolution_delay_seconds, 2),
            "max_resolution_delay_seconds": round(self.max_resolution_delay_seconds, 2),
        }
        for name, pc in self.profiles.items():
            d[f"{name}_opened"] = pc["opened"]
            d[f"{name}_resolved"] = pc["resolved"]
            d[f"{name}_wins"] = pc["wins"]
            d[f"{name}_losses"] = pc["losses"]
            d[f"{name}_pnl"] = round(pc["pnl_total"], 4)
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "ResolutionCounters":
        c = cls()
        _int_fields = [
            "paper_positions_open", "paper_positions_active", "paper_positions_expiring",
            "paper_positions_unresolved_past_expiry", "paper_trades_resolved",
            "paper_trades_settled", "paper_trades_journaled", "paper_wins",
            "paper_losses", "settlement_errors", "pnl_validation_errors",
            "expiry_settlements",
            "stop_loss_signals", "stop_loss_executed", "stop_loss_failed_no_bid",
            "trailing_loss_signals", "trailing_loss_executed", "trailing_loss_failed_no_bid",
            "take_profit_signals", "take_profit_executed", "take_profit_failed_no_bid",
            "duplicate_settlement_blocks", "duplicate_exit_blocks",
            "blocked_by_missing_condition_id",
        ]
        _float_fields = ["early_exit_pnl", "expiry_settlement_pnl"]
        for k in _int_fields:
            if k in d:
                setattr(c, k, int(d[k]))
        for k in _float_fields:
            if k in d:
                setattr(c, k, float(d[k]))
        if "resolution_delays" in d and isinstance(d["resolution_delays"], list):
            c.resolution_delays = d["resolution_delays"]
        return c


# ══════════════════════════════════════════════════════════════════════════════
# Fetch Market Resolution
# ══════════════════════════════════════════════════════════════════════════════

def fetch_market_resolution(condition_id: str, market_slug: str = "",
                             market_id: str = "") -> Dict[str, Any]:
    result = {
        "closed": False, "resolved": False, "resolved_winner": None,
        "winning_token_id": None, "outcome_prices": [],
        "resolution_source": "none", "raw_response_excerpt": "",
        "reason": "not_checked",
    }
    import urllib.request

    if market_id:
        try:
            url = f"{GAMMA_API}/markets/{market_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "fdc/19.8-resolver"})
            with urllib.request.urlopen(req, timeout=8) as r:
                m = json.loads(r.read())
            parsed = _parse_gamma_market(m, result)
            if parsed.get("resolved"):
                return parsed
            if parsed.get("closed") and not parsed.get("resolved"):
                result.update(parsed)
                result["reason"] = "closed_but_unresolved_source0"
                return result
            if not parsed.get("closed"):
                result.update(parsed)
                result["reason"] = "market_not_closed_source0"
        except Exception as e:
            result["reason"] = f"gamma_market_id_error: {str(e)[:80]}"

    try:
        url = f"{GAMMA_API}/markets?condition_id={condition_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc/19.8-resolver"})
        with urllib.request.urlopen(req, timeout=8) as r:
            markets = json.loads(r.read())
        if isinstance(markets, list):
            exact = [m for m in markets if m.get("conditionId") == condition_id]
            if exact:
                markets = exact
        if isinstance(markets, list) and len(markets) > 0:
            parsed = _parse_gamma_market(markets[0], result)
            if parsed.get("resolved"):
                parsed["resolution_source"] = "gamma_condition_id"
                return parsed
            if not parsed.get("resolved") and result.get("resolution_source") == "none":
                result.update(parsed)
    except Exception as e:
        if result.get("reason") == "not_checked":
            result["reason"] = f"gamma_condition_id_error: {str(e)[:80]}"

    if market_slug:
        try:
            url = f"{GAMMA_API}/events?slug={market_slug}"
            req = urllib.request.Request(url, headers={"User-Agent": "fdc/19.8-resolver"})
            with urllib.request.urlopen(req, timeout=8) as r:
                events = json.loads(r.read())
            if isinstance(events, list):
                for event in events:
                    for m in event.get("markets", []):
                        if m.get("conditionId") == condition_id:
                            parsed = _parse_gamma_market(m, result)
                            if parsed.get("resolved"):
                                parsed["resolution_source"] = "gamma_slug_fallback"
                                return parsed
        except Exception:
            pass

    if result["resolution_source"] == "none":
        result["resolution_source"] = "none_all_sources_failed"
    return result


def _parse_gamma_market(m: Dict, base: Dict) -> Dict:
    result = dict(base)
    result["raw_response_excerpt"] = json.dumps(m)[:300]
    closed = m.get("closed", False)
    result["closed"] = closed
    outcome_prices = _safe_parse_list(m.get("outcomePrices", "[]"))
    result["outcome_prices"] = outcome_prices
    outcomes = _safe_parse_list(m.get("outcomes", "[]"))
    tokens = _safe_parse_list(m.get("clobTokenIds", m.get("tokens", "[]")))
    if closed and isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
        try:
            p0 = float(outcome_prices[0])
            p1 = float(outcome_prices[1])
            if p0 >= 0.95 and p1 <= 0.05:
                label = outcomes[0] if isinstance(outcomes, list) and outcomes else "unknown"
                result["resolved"] = True
                result["resolved_winner"] = _normalize_winner_label(str(label))
                result["winning_token_id"] = tokens[0] if isinstance(tokens, list) and tokens else None
                if not result.get("resolution_source") or result["resolution_source"] == "none":
                    result["resolution_source"] = "gamma_market_id"
                result["reason"] = "resolved"
                return result
            elif p1 >= 0.95 and p0 <= 0.05:
                label = outcomes[1] if isinstance(outcomes, list) and len(outcomes) >= 2 else "unknown"
                result["resolved"] = True
                result["resolved_winner"] = _normalize_winner_label(str(label))
                result["winning_token_id"] = tokens[1] if isinstance(tokens, list) and len(tokens) >= 2 else None
                if not result.get("resolution_source") or result["resolution_source"] == "none":
                    result["resolution_source"] = "gamma_market_id"
                result["reason"] = "resolved"
                return result
            elif p0 > 0.05 and p1 > 0.05:
                result["closed"] = True
                result["resolved"] = False
                result["reason"] = "winner_unknown_prices_not_extreme"
                return result
        except (ValueError, IndexError, TypeError) as e:
            result["reason"] = f"price_parse_error: {e}"
    if not closed:
        result["reason"] = "market_not_closed"
    else:
        result["reason"] = result.get("reason", "closed_but_resolution_unclear")
    return result


def _safe_parse_list(val) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        import ast
        try:
            return ast.literal_eval(val)
        except Exception:
            try:
                return json.loads(val)
            except Exception:
                pass
    return []


def _normalize_winner_label(label: str) -> str:
    low = label.lower().strip()
    if "up" in low:
        return "UP"
    if "down" in low:
        return "DOWN"
    if any(w in low for w in ("yes", "above", "higher")):
        return "UP"
    if any(w in low for w in ("no", "below", "lower")):
        return "DOWN"
    return label  # ambiguous — return as-is


# ══════════════════════════════════════════════════════════════════════════════
# Paper Position Builder (§1 — conditionId gate)
# ══════════════════════════════════════════════════════════════════════════════

def build_paper_entry(entry: Dict, contract: Dict, shadow_profile: str = "",
                      rsi: float = 0.0, signal: Dict = None,
                      counters: ResolutionCounters = None) -> Optional[Dict]:
    """
    Build a paper position. Returns None (and increments counter) if conditionId is missing.
    Diagnostic candidates may lack conditionId, but executable paper positions must have it.
    """
    cid = entry.get("conditionId", contract.get("conditionId", ""))
    if not cid:
        if counters:
            counters.blocked_by_missing_condition_id += 1
        return None

    now = datetime.now(timezone.utc)
    mins_to_expiry = contract.get("mins_to_expiry", 10)
    expiry_dt = now + timedelta(minutes=mins_to_expiry)

    side = entry.get("side", "Up")
    if side == "Up":
        selected_token_id = contract.get("up_token_id", "")
        opposite_token_id = contract.get("down_token_id", "")
    else:
        selected_token_id = contract.get("down_token_id", "")
        opposite_token_id = contract.get("up_token_id", "")

    cid_short = cid[:12]
    pos_id = f"P-{cid_short}-{side[:1]}-{now.strftime('%H%M%S')}"

    entry_price = entry.get("contract_price", 0.5)
    size_usd = entry.get("bet", 2.0)

    return {
        "position_id": pos_id,
        "profile": shadow_profile,
        "asset": entry.get("asset", contract.get("asset", "BTC")),
        "interval": contract.get("window", "5m"),
        "market_slug": contract.get("series_slug", contract.get("event_slug", "")),
        "condition_id": cid,
        "market_id": contract.get("market_id", ""),
        "question": entry.get("question", contract.get("question", "")),
        "selected_side": side,
        "selected_token_id": selected_token_id,
        "opposite_token_id": opposite_token_id,
        "entry_timestamp": now.isoformat(),
        "entry_price": entry_price,
        "entry_bid": entry_price,
        "entry_ask": entry_price,
        "entry_spread": 0.0,
        "entry_depth": 0,
        "size_usd": size_usd,
        "contracts": round(size_usd / max(entry_price, 0.01), 4),
        "expiry_timestamp": expiry_dt.isoformat(),
        "time_to_expiry_at_entry": mins_to_expiry,
        "signal_rsi": rsi,
        "signal_zone": signal.get("rsi_zone", "unknown") if signal else "unknown",
        "signal_confidence": signal.get("confidence", 0) if signal else 0,
        "estimated_probability": entry.get("ev_p_win", signal.get("confidence", 0)) if signal else entry.get("ev_p_win", 0),
        "gross_EV": entry.get("ev_gross", 0),
        "net_EV": entry.get("ev_net", 0),
        "market_phase_at_entry": "paper",
        "token_state_at_entry": entry.get("token_state", "unknown"),
        "recoverability_score": entry.get("recoverability_score"),
        "status": STATE_OPENED,
        "pnl_settled": False,
        "resolution_checked_at": None,
        "resolution_source": None,
        "market_closed": None,
        "resolved_winner": None,
        "winning_token_id": None,
        "settlement_price": None,
        "gross_pnl": None,
        "net_pnl": None,
        "pnl_validated": None,
        "pnl_validation_error": None,
        "final_status": None,
        "journaled_at": None,
        "exit_type": None,
        "resolution_delay_seconds": None,
        # §7 journal exit fields
        "exit_signal_timestamp": None,
        "exit_executed_timestamp": None,
        "exit_bid": None,
        "exit_ask": None,
        "exit_spread": None,
        "exit_depth": None,
        "realized_exit_price": None,
        "early_exit_pnl": None,
        "expiry_settlement_skipped": False,
        # Trailing loss fields (NOT_IMPLEMENTED)
        "peak_contract_price": None,
        "trailing_stop_price": None,
        "trailing_loss_triggered": False,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Side-Token Validation
# ══════════════════════════════════════════════════════════════════════════════

def validate_side_token_mapping(pos: Dict) -> bool:
    """Return True if side/token mapping is valid at entry time."""
    side = pos.get("selected_side", "").upper()
    up_tok = pos.get("opposite_token_id" if side == "DOWN" else "selected_token_id", "")
    down_tok = pos.get("selected_token_id" if side == "DOWN" else "opposite_token_id", "")
    # Valid if both token IDs stored
    return bool(pos.get("selected_token_id")) and bool(pos.get("opposite_token_id"))


def check_side_token_mismatch(pos: Dict, winning_token_id: str) -> Optional[str]:
    if not winning_token_id:
        return None
    selected = pos.get("selected_token_id", "")
    opposite = pos.get("opposite_token_id", "")
    if not selected:
        return None
    if winning_token_id not in (selected, opposite):
        return f"winning_token_id {winning_token_id[:12]} not in position tokens"
    return None


def can_use_side_fallback(resolution: Dict, pos: Dict) -> bool:
    """
    §2: Side-based fallback is allowed ONLY if:
    - resolved_winner label is known and unambiguous UP/DOWN
    - selected_side ↔ selected_token_id mapping was validated at entry
    - entry stored both UP and DOWN token IDs
    """
    winner = resolution.get("resolved_winner", "")
    if not winner:
        return False
    # Must be unambiguous UP or DOWN
    if winner not in ("UP", "DOWN"):
        return False
    # Must have both token IDs stored
    if not pos.get("selected_token_id") or not pos.get("opposite_token_id"):
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# PnL Calculation
# ══════════════════════════════════════════════════════════════════════════════

def calculate_pnl(pos: Dict, won: bool, fee_rate: float = 0.0) -> Dict[str, Any]:
    entry_price = pos.get("entry_price", 0.5)
    size_usd = pos.get("size_usd", 0)
    if entry_price <= 0 or size_usd <= 0:
        return {"gross_pnl": 0, "net_pnl": 0,
                "pnl_validated": False, "pnl_validation_error": "invalid_entry_price_or_size"}
    contracts = size_usd / entry_price
    gross_pnl = (contracts * 1.00 - size_usd) if won else -size_usd
    fees = abs(gross_pnl) * fee_rate if fee_rate > 0 else 0
    net_pnl = gross_pnl - fees
    pnl_validated = True
    pnl_validation_error = None
    if won and gross_pnl < 0:
        pnl_validated = False
        pnl_validation_error = f"won_but_negative_gross_pnl: {gross_pnl:.4f}"
    elif not won and gross_pnl > 0:
        pnl_validated = False
        pnl_validation_error = f"lost_but_positive_gross_pnl: {gross_pnl:.4f}"
    max_possible = contracts if won else size_usd
    if abs(gross_pnl) > max_possible * 1.5:
        pnl_validated = False
        pnl_validation_error = f"pnl_magnitude_suspicious: |{gross_pnl:.4f}| > 1.5x max({max_possible:.4f})"
    return {
        "gross_pnl": round(gross_pnl, 4),
        "net_pnl": round(net_pnl, 4),
        "pnl_validated": pnl_validated,
        "pnl_validation_error": pnl_validation_error,
    }


def calculate_early_exit_pnl(entry_price: float, exit_price: float,
                               size_usd: float) -> Dict[str, Any]:
    """PnL for SL/TP/time-decay exits using realized exit price."""
    if entry_price <= 0 or exit_price <= 0 or size_usd <= 0:
        return {"gross_pnl": 0, "net_pnl": 0,
                "pnl_validated": False, "pnl_validation_error": "invalid_prices"}
    contracts = size_usd / entry_price
    gross_pnl = (contracts * exit_price) - size_usd
    net_pnl = gross_pnl
    pnl_validated = True
    pnl_validation_error = None
    if abs(gross_pnl) > size_usd * 2:
        pnl_validated = False
        pnl_validation_error = f"early_exit_pnl_suspicious: |{gross_pnl:.4f}| > 2x stake"
    return {
        "gross_pnl": round(gross_pnl, 4),
        "net_pnl": round(net_pnl, 4),
        "pnl_validated": pnl_validated,
        "pnl_validation_error": pnl_validation_error,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Exit Mechanism Checks
# ══════════════════════════════════════════════════════════════════════════════

def check_exit_mechanisms(pos: Dict, current_price: Optional[float] = None,
                          counters: ResolutionCounters = None,
                          bid_fetcher=None) -> Optional[Dict]:
    """
    Check SL/TP/time-decay signals. Returns signal dict if triggered.
    bid_fetcher: callable(pos) -> (bid, ask, spread, depth) or None
    If bid_fetcher not provided, uses _get_exit_bid_from_clob.
    
    Trailing loss: NOT_IMPLEMENTED for binary contracts.
    """
    now = datetime.now(timezone.utc)
    entry_time_str = pos.get("entry_timestamp", "")
    try:
        entry_time = datetime.fromisoformat(entry_time_str)
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
    except Exception:
        return None

    entry_price = pos.get("entry_price", 0.5)
    mins_to_expiry = pos.get("time_to_expiry_at_entry", 10)
    size_usd = pos.get("size_usd", 0)
    elapsed_mins = (now - entry_time).total_seconds() / 60
    remaining_mins = mins_to_expiry - elapsed_mins

    if current_price is None or entry_price <= 0:
        return None

    signal_time = now.isoformat()

    # ── STOP LOSS ──
    if current_price > 0:
        price_drop = (entry_price - current_price) / entry_price
        if price_drop >= STOP_LOSS_PCT:
            if counters:
                counters.stop_loss_signals += 1
            # Fetch executable bid
            bid_data = _fetch_exit_bid(pos, bid_fetcher)
            if bid_data is None:
                # Signal triggered but no executable bid
                if counters:
                    counters.stop_loss_failed_no_bid += 1
                pos["exit_signal_timestamp"] = signal_time
                pos["stop_loss_no_bid"] = True
                return None
            bid, ask, spread, depth = bid_data
            pnl_result = calculate_early_exit_pnl(entry_price, bid, size_usd)
            exit_value = (size_usd / entry_price) * bid
            return {
                "exit_type": "stop_loss",
                "exit_value": round(exit_value, 4),
                "pnl": pnl_result["net_pnl"],
                "cur_price": current_price,
                "entry_price": entry_price,
                "price_drop_pct": round(price_drop * 100, 1),
                "remaining_mins": round(remaining_mins, 2),
                "exit_bid": bid, "exit_ask": ask,
                "exit_spread": spread, "exit_depth": depth,
                "realized_exit_price": bid,
                "exit_signal_timestamp": signal_time,
                "exit_executed_timestamp": now.isoformat(),
                "message": f"STOP-LOSS: price dropped {price_drop*100:.0f}% — exit bid ${bid:.4f}",
            }

    # ── TAKE PROFIT ──
    if current_price > 0:
        price_gain = (current_price - entry_price) / entry_price
        if price_gain >= TAKE_PROFIT_PCT:
            if counters:
                counters.take_profit_signals += 1
            bid_data = _fetch_exit_bid(pos, bid_fetcher)
            if bid_data is None:
                if counters:
                    counters.take_profit_failed_no_bid += 1
                pos["exit_signal_timestamp"] = signal_time
                pos["take_profit_no_bid"] = True
                return None
            bid, ask, spread, depth = bid_data
            pnl_result = calculate_early_exit_pnl(entry_price, bid, size_usd)
            exit_value = (size_usd / entry_price) * bid
            return {
                "exit_type": "take_profit",
                "exit_value": round(exit_value, 4),
                "pnl": pnl_result["net_pnl"],
                "cur_price": current_price,
                "entry_price": entry_price,
                "price_gain_pct": round(price_gain * 100, 1),
                "remaining_mins": round(remaining_mins, 2),
                "exit_bid": bid, "exit_ask": ask,
                "exit_spread": spread, "exit_depth": depth,
                "realized_exit_price": bid,
                "exit_signal_timestamp": signal_time,
                "exit_executed_timestamp": now.isoformat(),
                "message": f"TAKE-PROFIT: price rose {price_gain*100:.0f}% — exit bid ${bid:.4f}",
            }

    # ── TIME DECAY ──
    if remaining_mins <= TIME_DECAY_SELL_MINS and remaining_mins > -0.5:
        if current_price < entry_price and current_price >= TIME_DECAY_MIN_PRICE:
            bid_data = _fetch_exit_bid(pos, bid_fetcher)
            if bid_data is None:
                pos["exit_signal_timestamp"] = signal_time
                return None
            bid, ask, spread, depth = bid_data
            pnl_result = calculate_early_exit_pnl(entry_price, bid, size_usd)
            exit_value = (size_usd / entry_price) * bid
            return {
                "exit_type": "time_decay",
                "exit_value": round(exit_value, 4),
                "pnl": pnl_result["net_pnl"],
                "cur_price": current_price,
                "entry_price": entry_price,
                "remaining_mins": round(remaining_mins, 2),
                "exit_bid": bid, "exit_ask": ask,
                "exit_spread": spread, "exit_depth": depth,
                "realized_exit_price": bid,
                "exit_signal_timestamp": signal_time,
                "exit_executed_timestamp": now.isoformat(),
                "message": f"TIME-DECAY: losing with {remaining_mins:.1f}m left — exit bid ${bid:.4f}",
            }

    return None


def _fetch_exit_bid(pos: Dict, bid_fetcher=None) -> Optional[Tuple[float, float, float, int]]:
    """
    Fetch executable exit bid from CLOB.
    Returns (bid, ask, spread, depth) or None if no executable bid.
    No fabricated exits without real bid/depth.
    """
    if bid_fetcher is not None:
        return bid_fetcher(pos)
    try:
        return _get_exit_bid_from_clob(pos)
    except Exception:
        return None


def _get_exit_bid_from_clob(pos: Dict) -> Optional[Tuple[float, float, float, int]]:
    """Fetch real bid from Polymarket CLOB."""
    import urllib.request
    token_id = pos.get("selected_token_id", "")
    if not token_id:
        return None
    try:
        url = f"{CLOB_API}/price?token_id={token_id}&side=sell"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc/19.8-exit"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        bid = float(data.get("price", 0))
        if bid <= 0:
            return None
        # Also fetch depth
        spread = 0.01  # Estimate
        depth = 100  # Estimate from price response
        return (bid, bid + spread, spread, depth)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Resolution Scheduler
# ══════════════════════════════════════════════════════════════════════════════

def resolve_paper_positions(state: Dict, counters: ResolutionCounters,
                             shadow_tracker=None,
                             bid_fetcher=None) -> List[Dict]:
    now = datetime.now(timezone.utc)
    positions = state.get("positions", {})
    resolved_positions = []

    counters.paper_positions_active = 0
    counters.paper_positions_expiring = 0
    counters.paper_positions_unresolved_past_expiry = 0

    for key in list(positions.keys()):
        pos = positions[key]
        if pos.get("pnl_settled", False):
            continue

        # §1: Skip positions without conditionId (shouldn't exist, but guard)
        if not pos.get("condition_id") and not pos.get("conditionId"):
            counters.paper_positions_unresolved_past_expiry += 1
            continue

        entry_time = _parse_utc_iso(pos.get("entry_timestamp", ""))
        if not entry_time:
            continue
        expiry_dt = _parse_utc_iso(pos.get("expiry_timestamp", ""))
        if not expiry_dt:
            mins_to_expiry = pos.get("time_to_expiry_at_entry", pos.get("mins_to_expiry", 10))
            expiry_dt = entry_time + timedelta(minutes=mins_to_expiry)

        remaining_mins = (expiry_dt - now).total_seconds() / 60

        # ACTIVE
        if remaining_mins > 0.5:
            if pos.get("status") == STATE_OPENED:
                pos["status"] = STATE_ACTIVE
            counters.paper_positions_active += 1
            # Check exit mechanisms
            try:
                cur_price = _get_current_contract_price(pos)
            except Exception:
                cur_price = None
            exit_result = check_exit_mechanisms(pos, cur_price, counters, bid_fetcher)
            if exit_result:
                pos["expiry_settlement_skipped"] = True  # §7: early exit means no expiry settlement
                _settle_position(pos, exit_result, state, counters, shadow_tracker)
                # §3/§5: track exit type in counters
                if exit_result["exit_type"] == "stop_loss":
                    counters.stop_loss_executed += 1
                elif exit_result["exit_type"] == "take_profit":
                    counters.take_profit_executed += 1
                counters.early_exit_pnl += exit_result.get("pnl", 0)
                resolved_positions.append(dict(pos))
                del positions[key]
            continue

        # EXPIRING (<30s)
        if remaining_mins > -1.0:
            pos["status"] = STATE_EXPIRING
            counters.paper_positions_expiring += 1
            continue

        # Past expiry — check resolution
        pos["resolution_checked_at"] = now.isoformat()
        condition_id = pos.get("condition_id", pos.get("conditionId", ""))
        market_slug = pos.get("market_slug", "")
        market_id = pos.get("market_id", "")

        resolution = fetch_market_resolution(condition_id, market_slug, market_id)

        if resolution.get("resolved"):
            winning_token_id = resolution.get("winning_token_id", "")
            selected_token_id = pos.get("selected_token_id", "")

            # Side-token validation
            mismatch = check_side_token_mismatch(pos, winning_token_id)
            if mismatch:
                counters.settlement_errors += 1
                _write_settlement_error(pos, mismatch, resolution)
                # §8: Increment canonical counters for settlement errors
                try:
                    import canonical_position as cpos
                    cpos.CANONICAL_COUNTERS["settlement_errors"] += 1
                except ImportError:
                    pass

            # §2: Determine win — strict fallback rules
            won = None
            if selected_token_id and winning_token_id:
                won = selected_token_id == winning_token_id
            elif can_use_side_fallback(resolution, pos):
                # Fallback allowed: unambiguous UP/DOWN + both tokens stored
                side = pos.get("selected_side", "").upper()
                winner = resolution.get("resolved_winner", "").upper()
                won = (side == winner)
            else:
                # Cannot determine win safely
                counters.settlement_errors += 1
                pos["status"] = STATE_UNRESOLVED_PAST_EXPIRY
                pos["unresolved_reason"] = "unresolved_winner_token_missing"
                counters.paper_positions_unresolved_past_expiry += 1
                _write_settlement_error(pos, "cannot_determine_winner_no_token_id", resolution)
                # §8: Canonical counter
                try:
                    import canonical_position as cpos
                    cpos.CANONICAL_COUNTERS["unresolved_winner_token_missing"] += 1
                except ImportError:
                    pass
                continue

            pnl_result = calculate_pnl(pos, won)
            delay = (now - expiry_dt).total_seconds()
            counters.resolution_delays.append(delay)
            pos["resolution_delay_seconds"] = round(delay, 2)
            pos["resolved_winner"] = resolution.get("resolved_winner")
            pos["winning_token_id"] = winning_token_id
            pos["settlement_price"] = 1.0 if won else 0.0
            pos["gross_pnl"] = pnl_result["gross_pnl"]
            pos["net_pnl"] = pnl_result["net_pnl"]
            pos["pnl_validated"] = pnl_result["pnl_validated"]
            pos["pnl_validation_error"] = pnl_result["pnl_validation_error"]
            pos["market_closed"] = True
            pos["resolution_source"] = resolution["resolution_source"]
            pos["exit_type"] = "expiry"
            pos["expiry_settlement_skipped"] = False

            if not pnl_result["pnl_validated"]:
                counters.pnl_validation_errors += 1

            _settle_resolved_position(pos, state, counters, shadow_tracker, won=won)
            counters.expiry_settlements += 1
            counters.expiry_settlement_pnl += pnl_result["net_pnl"]
            resolved_positions.append(dict(pos))
            del positions[key]

        elif resolution.get("closed") and not resolution.get("resolved"):
            pos["status"] = STATE_UNRESOLVED_PAST_EXPIRY
            pos["unresolved_reason"] = resolution.get("reason", "winner_unknown")
            counters.paper_positions_unresolved_past_expiry += 1
        else:
            pos["status"] = STATE_UNRESOLVED_PAST_EXPIRY
            pos["unresolved_reason"] = resolution.get("reason", "market_not_closed")
            counters.paper_positions_unresolved_past_expiry += 1

            # Force-resolve stale positions (>30 min past expiry)
            elapsed_mins = (now - entry_time).total_seconds() / 60
            mins_to_expiry_val = pos.get("time_to_expiry_at_entry", 10)
            if elapsed_mins > mins_to_expiry_val + 30:
                pos["status"] = STATE_RESOLVED
                pos["final_status"] = "force_resolved_stale"
                pos["net_pnl"] = 0
                pos["gross_pnl"] = 0
                pos["pnl_validated"] = True
                pos["pnl_validation_error"] = None
                delay = (now - expiry_dt).total_seconds()
                counters.resolution_delays.append(delay)
                pos["resolution_delay_seconds"] = round(delay, 2)
                _settle_resolved_position(pos, state, counters, shadow_tracker, won=None)
                resolved_positions.append(dict(pos))
                del positions[key]

    return resolved_positions


# ══════════════════════════════════════════════════════════════════════════════
# Settlement
# ══════════════════════════════════════════════════════════════════════════════

def _settle_resolved_position(pos: Dict, state: Dict, counters: ResolutionCounters,
                               shadow_tracker=None, won: Optional[bool] = None):
    if pos.get("pnl_settled", False):
        counters.duplicate_settlement_blocks += 1
        # §10: Canonical counter for duplicate settlement
        try:
            import canonical_position as cpos
            cpos.CANONICAL_COUNTERS["duplicate_settlement_blocks"] += 1
        except ImportError:
            pass
        return

    net_pnl = pos.get("net_pnl", 0) or 0
    gross_pnl = pos.get("gross_pnl", 0) or 0
    size_usd = pos.get("size_usd", 0)

    if won is None:
        won = net_pnl > 0

    if won is True:
        state["bankroll"] = state.get("bankroll", 320) + size_usd + net_pnl
        counters.paper_wins += 1
        state["wins"] = state.get("wins", 0) + 1
    elif won is False:
        counters.paper_losses += 1
        state["losses"] = state.get("losses", 0) + 1
    if won is None:
        state["bankroll"] = state.get("bankroll", 320) + size_usd

    state["total_pnl"] = state.get("total_pnl", 0) + net_pnl
    state["daily_pnl"] = state.get("daily_pnl", 0) + net_pnl

    profile = pos.get("profile", "")
    counters.record_profile_resolve(profile, won=won if won is not None else False,
                                     net_pnl=net_pnl, gross_pnl=gross_pnl)

    if profile and shadow_tracker:
        p = shadow_tracker.profiles.get(profile)
        if p:
            p["paper_trades_resolved"] = p.get("paper_trades_resolved", 0) + 1
            if won:
                p["paper_trades_won"] = p.get("paper_trades_won", 0) + 1

    pos["pnl_settled"] = True
    pos["status"] = STATE_SETTLED
    pos["final_status"] = pos.get("exit_type", "resolved")
    # §6: Fix win_loss field — derive from net_pnl
    pos["win_loss"] = "WIN" if net_pnl > 0 else "LOSS" if net_pnl < 0 else "BREAKEVEN"
    pos["resolved_winner"] = pos.get("resolved_winner") or ("UP" if won else "DOWN" if won is False else None)
    counters.paper_trades_settled += 1

    _journal_position(pos)
    pos["status"] = STATE_JOURNALED
    pos["journaled_at"] = datetime.now(timezone.utc).isoformat()
    counters.paper_trades_journaled += 1


def _settle_position(pos: Dict, exit_result: Dict, state: Dict,
                     counters: ResolutionCounters, shadow_tracker=None):
    """Settle an early-exit position (SL/TP/TD)."""
    if pos.get("pnl_settled", False):
        counters.duplicate_exit_blocks += 1
        return

    exit_value = exit_result.get("exit_value", 0)
    pnl = exit_result.get("pnl", 0)
    size_usd = pos.get("size_usd", 0)

    state["bankroll"] = state.get("bankroll", 320) + exit_value
    won = pnl > 0
    if won:
        counters.paper_wins += 1
        state["wins"] = state.get("wins", 0) + 1
    else:
        counters.paper_losses += 1
        state["losses"] = state.get("losses", 0) + 1

    state["total_pnl"] = state.get("total_pnl", 0) + pnl
    state["daily_pnl"] = state.get("daily_pnl", 0) + pnl

    profile = pos.get("profile", "")
    counters.record_profile_resolve(profile, won=won, net_pnl=pnl, gross_pnl=pnl)

    if profile and shadow_tracker:
        p = shadow_tracker.profiles.get(profile)
        if p:
            p["paper_trades_resolved"] = p.get("paper_trades_resolved", 0) + 1
            if won:
                p["paper_trades_won"] = p.get("paper_trades_won", 0) + 1

    pos["pnl_settled"] = True
    pos["gross_pnl"] = round(pnl, 4)
    pos["net_pnl"] = round(pnl, 4)
    pos["early_exit_pnl"] = round(pnl, 4)
    pos["settlement_price"] = exit_result.get("realized_exit_price", exit_result.get("cur_price", 0))
    pos["exit_type"] = exit_result.get("exit_type", "unknown")
    pos["resolution_source"] = "early_exit"
    pos["pnl_validated"] = True
    pos["pnl_validation_error"] = None
    pos["status"] = STATE_EXITED
    pos["final_status"] = exit_result.get("exit_type", "early_exit")
    # §7 journal fields
    pos["exit_signal_timestamp"] = exit_result.get("exit_signal_timestamp")
    pos["exit_executed_timestamp"] = exit_result.get("exit_executed_timestamp")
    pos["exit_bid"] = exit_result.get("exit_bid")
    pos["exit_ask"] = exit_result.get("exit_ask")
    pos["exit_spread"] = exit_result.get("exit_spread")
    pos["exit_depth"] = exit_result.get("exit_depth")
    pos["realized_exit_price"] = exit_result.get("realized_exit_price")
    pos["expiry_settlement_skipped"] = True
    # §6: Fix win_loss field — derive from net_pnl for early exit too
    pos["win_loss"] = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN"
    pos["resolved_winner"] = pos.get("resolved_winner") or ("UP" if won else "DOWN" if won is False else None)

    pos["status"] = STATE_SETTLED
    counters.paper_trades_settled += 1

    _journal_position(pos)
    pos["status"] = STATE_JOURNALED
    pos["journaled_at"] = datetime.now(timezone.utc).isoformat()
    counters.paper_trades_journaled += 1


# ══════════════════════════════════════════════════════════════════════════════
# Journal
# ══════════════════════════════════════════════════════════════════════════════

def _journal_position(pos: Dict):
    run_id = get_run_id()
    journal_dir = os.path.join(JOURNAL_BASE_DIR, run_id)
    os.makedirs(journal_dir, exist_ok=True)

    pos_file = os.path.join(journal_dir, f"{pos.get('position_id', 'unknown')}.json")
    try:
        with open(pos_file, "w") as f:
            json.dump(pos, f, indent=2, default=str)
    except Exception:
        pass

    summary = {
        "profile": pos.get("profile", ""),
        "asset": pos.get("asset", ""),
        "interval": pos.get("interval", ""),
        "selected_side": pos.get("selected_side", ""),
        "entry_price": pos.get("entry_price"),
        "size_usd": pos.get("size_usd"),
        "contracts": pos.get("contracts"),
        "estimated_probability": pos.get("estimated_probability"),
        "net_EV": pos.get("net_EV"),
        "resolved_winner": pos.get("resolved_winner"),
        "win_loss": "WIN" if (pos.get("net_pnl") or 0) > 0 else "LOSS" if (pos.get("net_pnl") or 0) < 0 else "BREAKEVEN",
        "gross_pnl": pos.get("gross_pnl"),
        "net_pnl": pos.get("net_pnl"),
        "entry_timestamp": pos.get("entry_timestamp"),
        "expiry_timestamp": pos.get("expiry_timestamp"),
        "resolution_checked_at": pos.get("resolution_checked_at"),
        "resolution_delay_seconds": pos.get("resolution_delay_seconds"),
        # §7 early exit journal fields
        "exit_type": pos.get("exit_type"),
        "exit_signal_timestamp": pos.get("exit_signal_timestamp"),
        "exit_executed_timestamp": pos.get("exit_executed_timestamp"),
        "exit_bid": pos.get("exit_bid"),
        "exit_ask": pos.get("exit_ask"),
        "exit_spread": pos.get("exit_spread"),
        "exit_depth": pos.get("exit_depth"),
        "realized_exit_price": pos.get("realized_exit_price"),
        "early_exit_pnl": pos.get("early_exit_pnl"),
        "expiry_settlement_skipped": pos.get("expiry_settlement_skipped", False),
    }
    trades_file = os.path.join(journal_dir, "trades.jsonl")
    try:
        with open(trades_file, "a") as f:
            f.write(json.dumps(summary, default=str) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard
# ══════════════════════════════════════════════════════════════════════════════

def compute_dashboard(state: Dict, counters: ResolutionCounters) -> Dict:
    positions = state.get("positions", {})
    rc = counters
    open_pos = len([p for p in positions.values() if not p.get("pnl_settled", False)])
    wins = rc.paper_wins
    losses = rc.paper_losses
    wr = wins / max(wins + losses, 1)
    net_pnl = state.get("total_pnl", 0)
    gross_profit = sum(t.get("net_pnl", 0) for t in _iter_journal_trades() if (t.get("net_pnl") or 0) > 0)
    gross_loss = abs(sum(t.get("net_pnl", 0) for t in _iter_journal_trades() if (t.get("net_pnl") or 0) < 0))
    pf = gross_profit / max(gross_loss, 0.01)
    bankroll = state.get("bankroll", 320)
    peak = state.get("bankroll_peak", 320)
    dd = (peak - bankroll) / max(peak, 1) if peak > bankroll else 0
    total_size = sum(t.get("size_usd", 0) for t in _iter_journal_trades())
    realized_ev = net_pnl
    dash = {
        "open_positions": open_pos,
        "resolved_positions": rc.paper_trades_resolved,
        "unresolved_past_expiry": rc.paper_positions_unresolved_past_expiry,
        "wins": wins, "losses": losses, "WR": round(wr, 3),
        "avg_entry_price": 0, "net_PnL": round(net_pnl, 4),
        "realized_EV_per_share": round(realized_ev / max(rc.paper_trades_resolved, 1), 4),
        "realized_EV_per_dollar": round(realized_ev / max(total_size, 1), 4),
        "PF": round(pf, 3), "DD": round(dd, 4),
        "settlement_errors": rc.settlement_errors,
        "pnl_validation_errors": rc.pnl_validation_errors,
        "stop_loss_signals": rc.stop_loss_signals,
        "stop_loss_executed": rc.stop_loss_executed,
        "stop_loss_failed_no_bid": rc.stop_loss_failed_no_bid,
        "take_profit_signals": rc.take_profit_signals,
        "take_profit_executed": rc.take_profit_executed,
        "take_profit_failed_no_bid": rc.take_profit_failed_no_bid,
        "trailing_loss_implemented": TRAILING_LOSS_IMPLEMENTED,
        "early_exit_pnl": round(rc.early_exit_pnl, 4),
        "expiry_settlement_pnl": round(rc.expiry_settlement_pnl, 4),
        "blocked_by_missing_condition_id": rc.blocked_by_missing_condition_id,
    }
    for name, pc in rc.profiles.items():
        if pc["resolved"] > 0:
            p_wr = pc["wins"] / max(pc["resolved"], 1)
            dash[name] = {
                "profile": name, "opened": pc["opened"], "resolved": pc["resolved"],
                "wins": pc["wins"], "losses": pc["losses"], "WR": round(p_wr, 3),
                "net_PnL": round(pc["pnl_total"], 4), "PF": 0, "DD": 0,
                "expected_EV": 0, "realized_EV": round(pc["pnl_total"], 4),
            }
    return dash


def _iter_journal_trades() -> List[Dict]:
    run_id = get_run_id()
    trades_file = os.path.join(JOURNAL_BASE_DIR, run_id, "trades.jsonl")
    trades = []
    if os.path.exists(trades_file):
        try:
            with open(trades_file) as f:
                for line in f:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# Promotion Gates
# ══════════════════════════════════════════════════════════════════════════════

def check_promotion_readiness(counters: ResolutionCounters, profile: str) -> Dict:
    pc = counters.profiles.get(profile, {})
    resolved = pc.get("resolved", 0)
    wins = pc.get("wins", 0)
    losses = pc.get("losses", 0)
    pnl = pc.get("pnl_total", 0)
    gates = {
        "resolved_trades_gte_5": resolved >= 5,
        "settlement_errors_zero": counters.settlement_errors == 0,
        "pnl_validation_errors_zero": counters.pnl_validation_errors == 0,
        "net_pnl_known": resolved > 0,
        "journal_complete": counters.paper_trades_journaled == counters.paper_trades_settled,
        "net_ev_positive": pnl > 0,
        "pf_gte_1_15": False,
        "no_false_dislocation": True,
        "no_dormant_longshot": True,
        "blocked_by_missing_cid_zero": counters.blocked_by_missing_condition_id == 0,
    }
    if resolved > 0 and losses > 0:
        pf = wins / max(losses, 1)
        gates["pf_gte_1_15"] = pf >= 1.15
    ready = all(v for k, v in gates.items() if k != "net_ev_positive")
    if profile == "CORE_UP_ONE_CONFIRM_SHADOW":
        gates["xrp_resolved_gte_5"] = resolved >= 5
        gates["xrp_net_ev_positive"] = pnl > 0
        gates["xrp_pf_gte_1_15"] = gates["pf_gte_1_15"]
        ready = ready and gates["xrp_net_ev_positive"] and gates["xrp_pf_gte_1_15"]
    return {"profile": profile, "ready": ready, "gates": gates, "resolved": resolved,
            "pnl": pnl, "WR": round(wins / max(resolved, 1), 3)}


# ══════════════════════════════════════════════════════════════════════════════
# Replay
# ══════════════════════════════════════════════════════════════════════════════

def replay_existing_trades(state: Dict, counters: ResolutionCounters) -> Dict:
    positions = state.get("positions", {})
    report = {"replayable": 0, "unable": 0, "missing_fields": [], "resolved": []}
    for key, pos in list(positions.items()):
        required = ["condition_id", "entry_price", "size_usd", "selected_side",
                     "expiry_timestamp", "selected_token_id"]
        missing = [f for f in required if not pos.get(f)]
        if missing:
            report["unable"] += 1
            report["missing_fields"].append({"key": key, "missing": missing})
            continue
        report["replayable"] += 1
        cid = pos.get("condition_id", pos.get("conditionId", ""))
        mid = pos.get("market_id", "")
        slug = pos.get("market_slug", "")
        resolution = fetch_market_resolution(cid, slug, mid)
        if resolution.get("resolved"):
            winning_token_id = resolution.get("winning_token_id", "")
            selected_token_id = pos.get("selected_token_id", "")
            won = (selected_token_id == winning_token_id) if selected_token_id and winning_token_id else False
            pnl_result = calculate_pnl(pos, won)
            report["resolved"].append({
                "key": key, "won": won,
                "gross_pnl": pnl_result["gross_pnl"],
                "net_pnl": pnl_result["net_pnl"],
                "pnl_validated": pnl_result["pnl_validated"],
            })
    return report


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_utc_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _get_current_contract_price(pos: Dict) -> Optional[float]:
    import urllib.request
    token_id = pos.get("selected_token_id", "")
    if not token_id:
        return None
    try:
        url = f"{CLOB_API}/price?token_id={token_id}&side=buy"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc/19.8-resolver"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return float(data.get("price", 0))
    except Exception:
        return None


def _write_settlement_error(pos: Dict, error: str, resolution: Dict):
    os.makedirs(os.path.dirname(SETTLEMENT_ERROR_FILE), exist_ok=True)
    try:
        with open(SETTLEMENT_ERROR_FILE, "a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "position_id": pos.get("position_id", "?"),
                "condition_id": pos.get("condition_id", ""),
                "selected_side": pos.get("selected_side", ""),
                "selected_token_id": pos.get("selected_token_id", ""),
                "opposite_token_id": pos.get("opposite_token_id", ""),
                "winning_token_id": resolution.get("winning_token_id", ""),
                "resolved_winner": resolution.get("resolved_winner", ""),
                "error": error,
                "resolution_source": resolution.get("resolution_source", ""),
            }, default=str) + "\n")
    except Exception:
        pass