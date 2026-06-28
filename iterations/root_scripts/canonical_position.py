#!/usr/bin/env python3
"""
V19.9 Canonical Paper Position Validation + Child-Market Enforcement
=====================================================================
Every paper position must go through validate_canonical_paper_position()
before it is opened. Monthly parent markets are rejected. Manual position
dict construction is forbidden.

Author: Hugh (3rd of 5) + Riker
Date: 2026-06-02
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
import paper_resolution as pres

REJECT_LOG = REPO / "paper_trading" / "canonical_position_reject.jsonl"

REQUIRED_CANONICAL_FIELDS = [
    "position_id", "profile", "asset", "interval", "market_slug",
    "condition_id", "conditionId", "market_id", "question",
    "selected_side", "opposite_side",
    "selected_token_id", "opposite_token_id",
    "up_token_id", "down_token_id",
    "entry_timestamp", "entry_price", "entry_bid", "entry_ask",
    "entry_spread", "entry_depth",
    "size_usd", "contracts",
    "expiry_timestamp", "expected_market_end_time",
    "time_to_expiry_at_entry",
    "signal_rsi", "signal_zone", "signal_confidence",
    "estimated_probability", "adjusted_probability",
    "gross_EV", "net_EV",
    "market_phase_at_entry", "token_state_at_entry",
    "reference_price", "current_price_at_entry",
    "recoverability_score",
    "position_builder_source", "status",
]

# ─── Expiry tolerances ───
EXPIRY_TOLERANCE_SECONDS = {
    "5m": 90,    # 5-minute markets: ±90 seconds
    "15m": 180,  # 15-minute markets: ±180 seconds
}

# ─── Allowed intervals ───
ALLOWED_INTERVALS = {"5m", "15m"}

# ─── Reject log path ───
REJECT_LOG = REPO / "paper_trading" / "canonical_position_reject.jsonl"

# ─── Counters ───
CANONICAL_COUNTERS = {
    "positions_built_with_pres_build_paper_entry": 0,
    "manual_position_build_attempts": 0,
    "positions_rejected_missing_required_fields": 0,
    "canonical_position_validation_passed": 0,
    "canonical_position_validation_failed": 0,
    "parent_market_mismatch_rejects": 0,
    "end_date_window_rejects": 0,
    "expiry_mismatch_rejects": 0,
    "missing_condition_id_rejects": 0,
    "missing_market_slug_rejects": 0,
    "missing_token_id_rejects": 0,
    "settlement_errors": 0,
    "unresolved_ambiguous_market": 0,
    "unresolved_winner_token_missing": 0,
    "duplicate_settlement_blocks": 0,
    "accounting_invariant_failures": 0,
}


def reset_counters():
    """Reset all canonical counters to zero."""
    for k in CANONICAL_COUNTERS:
        CANONICAL_COUNTERS[k] = 0


def _log_reject(reason: str, details: dict):
    """Append a rejection to canonical_position_reject.jsonl."""
    os.makedirs(REJECT_LOG.parent, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        **details,
    }
    with open(REJECT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def validate_canonical_paper_position(pos: Dict) -> Tuple[bool, List[str]]:
    """
    §2 + §3: Validate that a paper position dict has all required canonical
    fields and that they are consistent.

    Returns (is_valid, list_of_errors).
    """
    errors = []

    # §1: Must be built through pres.build_paper_entry
    builder = pos.get("position_builder_source", "")
    if builder != "pres.build_paper_entry":
        errors.append(
            f"position_builder_source='{builder}' != 'pres.build_paper_entry' "
            f"— manual position build forbidden"
        )
        CANONICAL_COUNTERS["manual_position_build_attempts"] += 1

    # §2: Check all required fields exist and are not empty/None
    for field in REQUIRED_CANONICAL_FIELDS:
        val = pos.get(field)
        if val is None or val == "" or val == "MISSING":
            errors.append(f"missing_required_field: {field}={val!r}")

    # §3: interval must be 5m or 15m
    interval = pos.get("interval", "")
    if interval not in ALLOWED_INTERVALS:
        errors.append(f"invalid_interval: {interval!r} not in {ALLOWED_INTERVALS}")

    # §3: selected_side must be UP or DOWN
    side = pos.get("selected_side", "")
    if side not in ("UP", "DOWN"):
        errors.append(f"invalid_selected_side: {side!r}")

    # §3: selected_token_id must match selected_side
    up_tid = pos.get("up_token_id", "")
    down_tid = pos.get("down_token_id", "")
    sel_tid = pos.get("selected_token_id", "")
    opp_tid = pos.get("opposite_token_id", "")
    if side == "UP" and sel_tid and up_tid and sel_tid != up_tid:
        errors.append(
            f"side_token_mismatch: selected_side=UP but selected_token_id != up_token_id "
            f"({sel_tid[:20]}... vs {up_tid[:20]}...)"
        )
    if side == "DOWN" and sel_tid and down_tid and sel_tid != down_tid:
        errors.append(
            f"side_token_mismatch: selected_side=DOWN but selected_token_id != down_token_id "
            f"({sel_tid[:20]}... vs {down_tid[:20]}...)"
        )
    # opposite must match the other side
    if side == "UP" and opp_tid and down_tid and opp_tid != down_tid:
        errors.append(
            f"opposite_token_mismatch: selected_side=UP but opposite_token_id != down_token_id"
        )
    if side == "DOWN" and opp_tid and up_tid and opp_tid != up_tid:
        errors.append(
            f"opposite_token_mismatch: selected_side=DOWN but opposite_token_id != up_token_id"
        )

    # §5: Expiry timestamp invariant
    expiry_str = pos.get("expiry_timestamp", "")
    expected_end_str = pos.get("expected_market_end_time", "")
    if expiry_str and expected_end_str:
        try:
            expiry_dt = _parse_utc_iso(expiry_str)
            expected_dt = _parse_utc_iso(expected_end_str)
            if expiry_dt and expected_dt:
                diff_seconds = abs((expiry_dt - expected_dt).total_seconds())
                tol = EXPIRY_TOLERANCE_SECONDS.get(interval, 180)
                if diff_seconds > tol:
                    errors.append(
                        f"expiry_mismatch: {diff_seconds:.0f}s > {tol}s tolerance "
                        f"(expiry={expiry_str[:19]}, expected={expected_end_str[:19]})"
                    )
                    CANONICAL_COUNTERS["expiry_mismatch_rejects"] += 1
        except Exception as e:
            errors.append(f"expiry_parse_error: {e}")

    if errors:
        CANONICAL_COUNTERS["canonical_position_validation_failed"] += 1
        CANONICAL_COUNTERS["positions_rejected_missing_required_fields"] += 0  # counted below
        for e in errors:
            if e.startswith("missing_required_field"):
                CANONICAL_COUNTERS["positions_rejected_missing_required_fields"] += 1
            elif "position_builder_source" in e:
                pass  # already counted in manual_position_build_attempts
        _log_reject("canonical_validation_failed", {"errors": errors, "position_id": pos.get("position_id", "unknown")})
    else:
        CANONICAL_COUNTERS["canonical_position_validation_passed"] += 1

    return len(errors) == 0, errors


def validate_child_market(contract: Dict) -> Tuple[bool, str]:
    """
    §4 + §6: Validate that a contract dict represents a 5m/15m child market,
    not a monthly/weekly parent or stale market.

    Returns (is_valid_child, reason).
    """
    now = datetime.now(timezone.utc)

    # §4: Interval must be 5m or 15m
    interval = contract.get("window", contract.get("interval", ""))
    if interval not in ALLOWED_INTERVALS:
        CANONICAL_COUNTERS["parent_market_mismatch_rejects"] += 1
        return False, f"blocked_by_parent_market_mismatch: interval={interval!r}"

    # §4: endDate must be within expected window (now to now+30min)
    end_str = contract.get("end_date", contract.get("endDate", ""))
    if end_str:
        try:
            end_dt = _parse_utc_iso(end_str)
            if end_dt:
                # must not be monthly/weekly/daily
                mins_to_end = (end_dt - now).total_seconds() / 60
                if mins_to_end > 30:
                    CANONICAL_COUNTERS["parent_market_mismatch_rejects"] += 1
                    return False, f"blocked_by_parent_market_mismatch: end_date={end_str[:19]} is {mins_to_end:.0f}min away (>30min)"
                if mins_to_end < -5:  # already expired
                    CANONICAL_COUNTERS["end_date_window_rejects"] += 1
                    return False, f"blocked_by_expired_market: end_date={end_str[:19]} expired {-mins_to_end:.0f}min ago"
        except Exception:
            pass  # If parse fails, trust the contract but log

    # §4: Slug must match deterministic child-market format
    # Expected: btc-updown-{5m|15m}-{timestamp} or similar
    slug = contract.get("series_slug", contract.get("event_slug", ""))
    asset = contract.get("asset", "")
    valid_prefixes = []
    for a in ["btc", "eth", "sol", "xrp"]:
        for w in ["5m", "15m"]:
            valid_prefixes.append(f"{a}-updown-{w}")
            valid_prefixes.append(f"{a}-up-or-down-{w}")
    # Allow if slug contains updown/up-or-down + interval
    slug_ok = False
    if slug:
        slug_lower = slug.lower()
        for prefix in valid_prefixes:
            if prefix in slug_lower:
                slug_ok = True
                break
    # If slug doesn't match child format, check question for time window
    if not slug_ok:
        question = contract.get("question", "")
        # Must contain a specific time window like "7:30AM-7:45AM"
        import re
        if not re.search(r'\d{1,2}:\d{2}[AP]M', question, re.IGNORECASE):
            CANONICAL_COUNTERS["parent_market_mismatch_rejects"] += 1
            return False, f"blocked_by_parent_market_mismatch: slug={slug!r} question={question[:60]}"

    # §4: conditionId must belong to child market (not monthly parent)
    # Monthly parents have end_date far in the future or match daily/weekly patterns
    question = contract.get("question", "")
    if "below" in question.lower() and ("daily" in question.lower() or "weekly" in question.lower()):
        CANONICAL_COUNTERS["parent_market_mismatch_rejects"] += 1
        return False, f"blocked_by_parent_market_mismatch: question mentions daily/weekly"

    # §6: Validate conditionId exists
    cid = contract.get("conditionId", "")
    if not cid:
        CANONICAL_COUNTERS["missing_condition_id_rejects"] += 1
        return False, "blocked_by_missing_condition_id"

    # §6: Validate market_slug exists
    if not slug and not contract.get("event_slug"):
        CANONICAL_COUNTERS["missing_market_slug_rejects"] += 1
        return False, "blocked_by_missing_market_slug"

    # §6: Validate token IDs exist
    has_up = bool(contract.get("up_token_id"))
    has_down = bool(contract.get("down_token_id"))
    if not has_up or not has_down:
        # Try clobTokenIds as fallback
        clob = contract.get("clobTokenIds", [])
        if isinstance(clob, str):
            try:
                import json as _json
                clob = _json.loads(clob)
            except Exception:
                clob = []
        if len(clob) < 2:
            CANONICAL_COUNTERS["missing_token_id_rejects"] += 1
            return False, f"blocked_by_missing_token_id: up={has_up} down={has_down} clob={len(clob) if isinstance(clob, list) else 0}"

    return True, ""


def enrich_contract_with_expected_end(contract: Dict) -> Dict:
    """
    Calculate expected_market_end_time from contract's end_date or mins_to_expiry.
    Returns contract dict with 'expected_market_end_time' added.
    """
    now = datetime.now(timezone.utc)

    # Try end_date first
    end_str = contract.get("end_date", contract.get("endDate", ""))
    if end_str:
        end_dt = _parse_utc_iso(end_str)
        if end_dt:
            contract["expected_market_end_time"] = end_dt.isoformat()
            return contract

    # Fallback: entry_time + mins_to_expiry
    mins = contract.get("mins_to_expiry", contract.get("window_mins", 10))
    expected = now + timedelta(minutes=mins)
    contract["expected_market_end_time"] = expected.isoformat()
    return contract


def build_canonical_paper_entry(entry: Dict, contract: Dict, shadow_profile: str,
                                 rsi: float, signal: Dict) -> Optional[Dict]:
    """
    §1: Build a canonical paper position through pres.build_paper_entry,
    then validate it. Returns None if validation fails.

    This is the ONLY way to create paper positions.
    """
    # Enrich contract with expected_market_end_time
    contract = enrich_contract_with_expected_end(contract)

    # §4: Validate child market first
    is_child, child_reason = validate_child_market(contract)
    if not is_child:
        _log_reject(child_reason, {"contract_question": contract.get("question", ""),
                                   "contract_slug": contract.get("series_slug", "")})
        return None

    # §1: Build through pres.build_paper_entry
    pos = pres.build_paper_entry(
        entry=entry,
        contract=contract,
        shadow_profile=shadow_profile,
        rsi=rsi,
        signal=signal,
    )
    if pos is None:
        CANONICAL_COUNTERS["manual_position_build_attempts"] += 1
        _log_reject("build_paper_entry_returned_none", {"entry_action": entry.get("action", "")})
        return None

    # Tag as canonical
    pos["position_builder_source"] = "pres.build_paper_entry"

    # Enrich with required canonical fields from contract and entry
    # (pres.build_paper_entry doesn't set all required fields)
    # Normalize side to uppercase
    side_raw = pos.get("selected_side", entry.get("side", "UP"))
    side = side_raw.upper() if isinstance(side_raw, str) else "UP"
    pos["selected_side"] = side
    pos["opposite_side"] = "DOWN" if side == "UP" else "UP"

    # Fill condition IDs (pres.build_paper_entry uses conditionId lowercase key)
    pos["conditionId"] = pos.get("conditionId") or pos.get("condition_id") or entry.get("conditionId", "")
    pos["condition_id"] = pos.get("condition_id") or pos.get("conditionId")
    pos["market_id"] = pos.get("market_id") or contract.get("market_id", "")
    pos["up_token_id"] = pos.get("up_token_id") or contract.get("up_token_id", "")
    pos["down_token_id"] = pos.get("down_token_id") or contract.get("down_token_id", "")
    pos["expected_market_end_time"] = contract.get("expected_market_end_time", "")
    pos["adjusted_probability"] = entry.get("adjusted_p", entry.get("estimated_probability", 0))
    pos["reference_price"] = entry.get("reference_price", entry.get("current_price", 0))
    pos["current_price_at_entry"] = entry.get("current_price_at_entry", entry.get("reference_price", 0))
    pos["recoverability_score"] = entry.get("recoverability_score", 0)

    # Set selected/opposite token IDs from side if not already set by build_paper_entry
    # NOTE: build_paper_entry maps "Up" → up_token_id, "Down" → down_token_id.
    # If side was "UP" (all caps), build_paper_entry falls to else branch and
    # reverses the tokens. Only overwrite if tokens are missing or clearly wrong.
    sel_tid = pos.get("selected_token_id", "")
    opp_tid = pos.get("opposite_token_id", "")
    up_tid = pos["up_token_id"]
    down_tid = pos["down_token_id"]

    # If selected_token_id doesn't match the side, fix it
    if side == "UP" and sel_tid != up_tid and up_tid:
        pos["selected_token_id"] = up_tid
    elif side == "DOWN" and sel_tid != down_tid and down_tid:
        pos["selected_token_id"] = down_tid

    if side == "UP" and opp_tid != down_tid and down_tid:
        pos["opposite_token_id"] = down_tid
    elif side == "DOWN" and opp_tid != up_tid and up_tid:
        pos["opposite_token_id"] = up_tid

    # §2 + §3: Validate canonical fields
    is_valid, errors = validate_canonical_paper_position(pos)
    if not is_valid:
        CANONICAL_COUNTERS["positions_rejected_missing_required_fields"] += 1
        _log_reject("canonical_validation_failed", {
            "errors": errors,
            "position_id": pos.get("position_id", "unknown"),
        })
        return None

    CANONICAL_COUNTERS["positions_built_with_pres_build_paper_entry"] += 1
    return pos


def validate_settlement_side_token_mapping(pos: Dict, winning_token_id: str) -> Tuple[str, bool]:
    """
    §8: Validate that settlement uses token-based resolution, not side labels.

    Returns (result, is_valid):
      result = "win" or "loss"
      is_valid = True if side-token mapping is consistent, False if mismatch

    settlement_error is incremented internally if mismatch.
    """
    up_tid = pos.get("up_token_id", "")
    down_tid = pos.get("down_token_id", "")
    sel_tid = pos.get("selected_token_id", "")
    side = pos.get("selected_side", "")

    # Check side-token consistency at entry
    if side == "UP" and sel_tid and up_tid and sel_tid != up_tid:
        CANONICAL_COUNTERS["settlement_errors"] += 1
        return "error", False
    if side == "DOWN" and sel_tid and down_tid and sel_tid != down_tid:
        CANONICAL_COUNTERS["settlement_errors"] += 1
        return "error", False

    # Token-based resolution
    if not winning_token_id:
        CANONICAL_COUNTERS["unresolved_winner_token_missing"] += 1
        return "unresolved", False

    if sel_tid == winning_token_id:
        return "win", True
    elif up_tid == winning_token_id or down_tid == winning_token_id:
        return "loss", True
    else:
        CANONICAL_COUNTERS["unresolved_ambiguous_market"] += 1
        return "unresolved", False


def _parse_utc_iso(s: str) -> Optional[datetime]:
    """Parse ISO datetime string to UTC-aware datetime."""
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_counter_report() -> Dict:
    """Return current counter values as a dict."""
    return dict(CANONICAL_COUNTERS)