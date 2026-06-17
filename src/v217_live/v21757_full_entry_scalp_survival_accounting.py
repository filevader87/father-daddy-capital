#!/usr/bin/env python3
"""
V21.7.57 Full-Entry Scalp Survival Accounting

Purpose: Reconstruct every 5m paper entry from V21.7.55 and V21.7.56,
classify each with exactly one final strategy outcome, separate actual
strategy PnL from counterfactual hold PnL, measure no-exit losses,
stress-test scalp profitability under slippage, and determine whether
XRP 5m DOWN is truly positive expectancy.

LIVE AUTHORIZATION REMAINS SUSPENDED. ZERO REAL ORDERS. ZERO WALLET SPEND.
"""

import json
import os
import sys
from datetime import datetime, timezone
from collections import defaultdict, Counter
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent.parent
OUT_DIR = BASE_DIR / "output" / "v21757_full_entry_scalp_survival"
SUPERVISOR_DIR = BASE_DIR / "output" / "supervisor"

SWARM_POS = BASE_DIR / "output/v21755_true_forward_paper_5m_swarm/paper_positions.jsonl"
SWARM_SCALP = BASE_DIR / "output/v21755_true_forward_paper_5m_swarm/paper_scalp_exits.jsonl"
SWARM_EXPIRY = BASE_DIR / "output/v21755_true_forward_paper_5m_swarm/paper_expiry_settlements.jsonl"
SWARM_SETTLEMENT_AUDIT = BASE_DIR / "output/v21755_true_forward_paper_5m_swarm/paper_settlement_audit.jsonl"
REPAIRED_PNL = BASE_DIR / "output/v21756_scalp_accounting_xrp_focus/repaired_strategy_pnl.jsonl"
BUCKET_CLASS = BASE_DIR / "output/v21756_scalp_accounting_xrp_focus/entry_bucket_classification.jsonl"
XRP_POS = BASE_DIR / "output/v21756_scalp_accounting_xrp_focus/xrp_5m_down_focused_positions.jsonl"
XRP_SCALP = BASE_DIR / "output/v21756_scalp_accounting_xrp_focus/xrp_5m_down_scalp_exits.jsonl"

SCALP_THRESHOLD = 0.03  # +3¢ executable bid target
REAL_ORDERS_ALLOWED = False
LIVE_AUTHORIZATION_SUSPENDED = True
WALLET_SPEND_ALLOWED = False

# ── Helpers ────────────────────────────────────────────────────────────

def load_jsonl(path):
    """Load a JSONL file, return list of dicts."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out

def write_jsonl(path, data):
    """Write list of dicts to JSONL."""
    with open(path, "w") as f:
        for row in data:
            f.write(json.dumps(row, default=str) + "\n")

def write_json(path, data):
    """Write JSON object."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

def safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except (ValueError, TypeError):
        return default

def parse_ts(ts):
    """Parse ISO timestamp to datetime."""
    if ts is None:
        return None
    try:
        if isinstance(ts, str):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return ts
    except Exception:
        return None

def calc_pnl(entry_price, exit_price, contracts, size_usd, side="BUY"):
    """
    Calculate PnL for a position.
    For binary options: pnl = (exit_price - entry_price) * contracts
    For expiry win: pnl = contracts * 1.0 - size_usd
    For expiry loss: pnl = -size_usd
    """
    if exit_price is None or entry_price is None:
        return None
    ep = safe_float(entry_price)
    xp = safe_float(exit_price)
    c = safe_float(contracts)
    su = safe_float(size_usd)
    # If exit is 0 or 1 (expiry), compute from size
    if xp == 0.0:
        return -su
    if xp == 1.0:
        return c * 1.0 - su
    # Mid-trade scalp: profit from bid repricing
    return (xp - ep) * c

def classify_tte_band(tte):
    """Classify time-to-expiry band."""
    t = safe_float(tte)
    if t <= 30:
        return "0-30s"
    elif t <= 60:
        return "31-60s"
    elif t <= 120:
        return "61-120s"
    elif t <= 180:
        return "121-180s"
    elif t <= 240:
        return "181-240s"
    else:
        return "241s+"

def classify_spread_band(spread):
    """Classify spread band."""
    s = safe_float(spread)
    if s <= 0.01:
        return "0-1c"
    elif s <= 0.02:
        return "1-2c"
    elif s <= 0.03:
        return "2-3c"
    elif s <= 0.05:
        return "3-5c"
    else:
        return "5c+"

def classify_bucket(price):
    """Classify entry price bucket."""
    p = safe_float(price)
    if p < 0.10:
        return "0-10c"
    elif p < 0.20:
        return "10-20c"
    elif p < 0.30:
        return "20-30c"
    elif p < 0.40:
        return "30-40c"
    elif p < 0.50:
        return "40-50c"
    elif p < 0.60:
        return "50-60c"
    elif p < 0.70:
        return "60-70c"
    elif p < 0.80:
        return "70-80c"
    elif p < 0.90:
        return "80-90c"
    else:
        return "90-100c"

# ── Step 5: Canonical Entry Universe Reconstruction ────────────────────

def build_canonical_entry_universe():
    """
    Build canonical entry universe from all V21.7.55 and V21.7.56 files.
    Dedup by position_id, keeping the most informative row.
    """
    # Load all positions
    swarm_positions = load_jsonl(SWARM_POS)
    xrp_positions = load_jsonl(XRP_POS)
    
    # Load supplementary data for enrichment
    swarm_scalp = load_jsonl(SWARM_SCALP)
    xrp_scalp = load_jsonl(XRP_SCALP)
    swarm_expiry = load_jsonl(SWARM_EXPIRY)
    settlement_audit = load_jsonl(SWARM_SETTLEMENT_AUDIT)
    bucket_class = load_jsonl(BUCKET_CLASS)
    
    # Build lookup dicts
    scalp_lookup = {}  # position_id -> scalp exit data
    for s in swarm_scalp:
        scalp_lookup[s["position_id"]] = s
    for s in xrp_scalp:
        scalp_lookup[s["position_id"]] = s
    
    expiry_lookup = {}  # position_id -> expiry settlement
    for e in swarm_expiry:
        expiry_lookup[e["position_id"]] = e
    
    audit_lookup = {}  # position_id -> settlement audit
    for a in settlement_audit:
        audit_lookup[a["position_id"]] = a
    
    bucket_lookup = {}  # position_id -> bucket classification
    for b in bucket_class:
        bucket_lookup[b["position_id"]] = b
    
    # Dedup by position_id: keep the row with most complete exit data
    # Priority: PAPER_SETTLED > PAPER_RESOLVED > PAPER_OPENED (for swarm)
    # Also merge in data from expiry_settlements and scalp_exits
    all_entries = {}
    
    def merge_entry(row, source):
        pid = row.get("position_id")
        if not pid:
            return
        
        existing = all_entries.get(pid)
        if existing is None:
            all_entries[pid] = {**row, "_source": source}
        else:
            # Merge: prefer non-null, prefer more settled status
            status_priority = {"PAPER_SETTLED": 3, "PAPER_RESOLVED": 2, "PAPER_OPENED": 1}
            existing_pri = status_priority.get(existing.get("status", ""), 0)
            new_pri = status_priority.get(row.get("status", ""), 0)
            
            if new_pri > existing_pri:
                # New row has better status - use it as base but preserve existing fields
                merged = {**existing}
                merged.update({k: v for k, v in row.items() if v is not None and v != ""})
                merged["_source"] = source
                all_entries[pid] = merged
            else:
                # Existing has better or equal status - just fill in missing fields
                for k, v in row.items():
                    if k not in existing or existing[k] is None or existing[k] == "":
                        if v is not None and v != "":
                            existing[k] = v
    
    for row in swarm_positions:
        merge_entry(row, "v21755_swarm")
    for row in xrp_positions:
        merge_entry(row, "v21756_xrp_focus")
    
    # Infer missing asset/side/interval/cell_id from position_id and market_slug
    for pid, entry in all_entries.items():
        slug = entry.get("market_slug", "") or ""
        # Infer from market_slug: e.g. "xrp-updown-5m-1781691900"
        if not entry.get("asset"):
            if "btc" in slug.lower():
                entry["asset"] = "BTC"
            elif "eth" in slug.lower():
                entry["asset"] = "ETH"
            elif "sol" in slug.lower():
                entry["asset"] = "SOL"
            elif "xrp" in slug.lower():
                entry["asset"] = "XRP"
        if not entry.get("interval"):
            if "-5m-" in slug:
                entry["interval"] = "5m"
            elif "-15m-" in slug:
                entry["interval"] = "15m"
        # XRP-FOCUS entries are all DOWN by definition (filename: xrp_5m_down_focused)
        if pid.startswith("XRP-FOCUS") and not entry.get("side"):
            entry["side"] = "DOWN"
        if not entry.get("cell_id"):
            a = entry.get("asset", "?")
            i = entry.get("interval", "?")
            s = entry.get("side", "?")
            entry["cell_id"] = f"{a}_{i}_{s}"
    
    # Enrich with expiry data for positions that have PAPER_OPENED status but actually settled
    for pid, entry in all_entries.items():
        if pid in expiry_lookup:
            exp = expiry_lookup[pid]
            # If the position is in expiry_settlements, it actually settled (not open)
            if entry.get("status") == "PAPER_OPENED":
                entry["status"] = exp.get("status", "PAPER_SETTLED")
            # Fill in settlement fields
            for k in ["exit_price", "exit_reason", "net_pnl", "gross_pnl", "selected_token_won",
                       "winning_token_id", "resolved_winner", "settlement_source", "exit_timestamp"]:
                if k in exp and (entry.get(k) is None or entry.get(k) == "" or entry.get(k) == 0.0):
                    entry[k] = exp[k]
    
    # Enrich with scalp exit data
    for pid, entry in all_entries.items():
        if pid in scalp_lookup:
            sx = scalp_lookup[pid]
            if entry.get("exit_reason") != "SCALP_EXIT_3C":
                entry["exit_reason"] = sx.get("exit_reason", "SCALP_EXIT_3C")
                entry["exit_price"] = sx.get("exit_price")
                entry["exit_timestamp"] = sx.get("exit_timestamp")
                entry["net_pnl"] = sx.get("net_pnl")
                entry["gross_pnl"] = sx.get("gross_pnl")
                if entry.get("status") in ("PAPER_OPENED", "PAPER_SETTLED"):
                    entry["status"] = "PAPER_RESOLVED"
    
    # Enrich with settlement audit
    for pid, entry in all_entries.items():
        if pid in audit_lookup:
            audit = audit_lookup[pid]
            for k in ["selected_token_won", "winning_token_id", "resolved_winner",
                       "settlement_validated", "settlement_error"]:
                if k in audit:
                    entry[k] = audit[k]
    
    # Enrich with bucket classification
    for pid, entry in all_entries.items():
        if pid in bucket_lookup:
            bc = bucket_lookup[pid]
            entry["actual_bucket"] = bc.get("actual_bucket", classify_bucket(entry.get("entry_price")))
            entry["mislabeled"] = bc.get("mislabeled", False)
        else:
            entry["actual_bucket"] = classify_bucket(entry.get("entry_price"))
            entry["mislabeled"] = False
    
    # Build canonical universe with required fields
    REQUIRED_FIELDS = [
        "position_id", "paper_order_id", "asset", "interval", "side",
        "market_slug", "condition_id", "selected_token_id",
        "entry_timestamp", "entry_price", "entry_bid", "entry_ask",
        "entry_spread", "entry_quote_source", "entry_quote_age_ms",
        "entry_book_depth", "size_usd", "contracts",
        "time_to_expiry_at_entry", "status"
    ]
    
    canonical = []
    hard_failures = []
    
    for pid, entry in all_entries.items():
        # Hard fail checks
        if not entry.get("position_id"):
            hard_failures.append(f"Missing position_id: {pid}")
            continue
        if not entry.get("selected_token_id"):
            hard_failures.append(f"Missing selected_token_id: {pid}")
            continue
        if not entry.get("entry_quote_source"):
            hard_failures.append(f"Missing entry_quote_source: {pid}")
            continue
        if entry.get("entry_price") is None:
            hard_failures.append(f"Missing entry_price: {pid}")
            continue
        
        # Build canonical row
        row = {}
        for field in REQUIRED_FIELDS:
            row[field] = entry.get(field)
        
        # Add enrichment fields
        row["actual_bucket"] = entry.get("actual_bucket", classify_bucket(entry.get("entry_price")))
        row["cell_id"] = entry.get("cell_id", f"{entry.get('asset','?')}_{entry.get('interval','?')}_{entry.get('side','?')}")
        row["max_bid_after_entry"] = entry.get("max_bid_after_entry")
        row["min_bid_after_entry"] = entry.get("min_bid_after_entry")
        row["exit_price"] = entry.get("exit_price")
        row["exit_reason"] = entry.get("exit_reason")
        row["exit_timestamp"] = entry.get("exit_timestamp")
        row["net_pnl"] = entry.get("net_pnl")
        row["gross_pnl"] = entry.get("gross_pnl")
        row["selected_token_won"] = entry.get("selected_token_won")
        row["winning_token_id"] = entry.get("winning_token_id")
        row["resolved_winner"] = entry.get("resolved_winner")
        row["settlement_source"] = entry.get("settlement_source")
        row["mislabeled"] = entry.get("mislabeled", False)
        row["_source"] = entry.get("_source", "unknown")
        
        canonical.append(row)
    
    # Sort by entry_timestamp
    canonical.sort(key=lambda r: r.get("entry_timestamp", ""))
    
    return canonical, hard_failures

# ── Step 6: Final Outcome Classification ────────────────────────────────

def classify_final_outcome(entry):
    """
    Classify every entry with exactly one final strategy outcome.
    """
    exit_reason = entry.get("exit_reason", "")
    status = entry.get("status", "")
    selected_token_id = entry.get("selected_token_id")
    winning_token_id = entry.get("winning_token_id")
    selected_token_won = entry.get("selected_token_won")
    exit_price = entry.get("exit_price")
    
    # SCALP_EXIT: executable bid scalp exit occurred
    if exit_reason == "SCALP_EXIT_3C" or (exit_price is not None and 
                                            safe_float(exit_price) > safe_float(entry.get("entry_price", 0)) and
                                            exit_reason and "SCALP" in str(exit_reason).upper()):
        return "SCALP_EXIT"
    
    # If still open (no exit, no settlement)
    if status in ("PAPER_OPENED",) and exit_price is None:
        # Check if it's in expiry_settlements (would mean it actually settled)
        # If exit_price is 0.0 and no exit reason, could be expiry loss
        if exit_price is None:
            return "OPEN_UNRESOLVED"
    
    # If settled/resolved with no scalp exit, check if it's expiry
    if status in ("PAPER_SETTLED", "PAPER_RESOLVED"):
        # If exit_price is 0 or 1, it's expiry resolution
        ep = safe_float(exit_price)
        if ep == 0.0 and (not exit_reason or exit_reason == "HOLD_TO_EXPIRY"):
            return "EXPIRY_LOSS"
        if ep == 1.0 and (not exit_reason or exit_reason == "HOLD_TO_EXPIRY"):
            return "EXPIRY_WIN"
        
        # Check if selected_token_won
        if selected_token_won is True:
            return "EXPIRY_WIN"
        if selected_token_won is False:
            return "EXPIRY_LOSS"
        
        # Check token ID match
        if winning_token_id is not None and selected_token_id is not None:
            if str(selected_token_id) == str(winning_token_id):
                return "EXPIRY_WIN"
            else:
                return "EXPIRY_LOSS"
        
        # If exit_price is 0 and it's settled, it's an expiry loss
        if ep == 0.0:
            return "EXPIRY_LOSS"
        if ep >= 1.0:
            return "EXPIRY_WIN"
        
        # If we have a non-zero, non-1 exit price but no scalp reason, it could be time stop or stop loss
        # Check exit_reason for hints
        if exit_reason and "TIME_STOP" in str(exit_reason).upper():
            return "TIME_STOP"
        if exit_reason and "STOP_LOSS" in str(exit_reason).upper():
            return "STOP_LOSS"
        
        # If we can't determine and exit_price exists but no bid liquidity info
        if exit_price is not None and ep > 0 and ep < 1:
            return "NO_EXIT_LIQUIDITY"
    
    # If still PAPER_OPENED with exit_price=0, might be open with no exit
    if status == "PAPER_OPENED":
        return "OPEN_UNRESOLVED"
    
    return "INVALID"

# ── Step 7: Actual Strategy PnL ─────────────────────────────────────────

def compute_strategy_pnl(entry, outcome):
    """
    Calculate actual strategy PnL for a given outcome.
    Returns (strategy_exit_price, strategy_pnl).
    """
    entry_price = safe_float(entry.get("entry_price"))
    exit_price = entry.get("exit_price")
    contracts = safe_float(entry.get("contracts"))
    size_usd = safe_float(entry.get("size_usd"))
    net_pnl = entry.get("net_pnl")
    gross_pnl = entry.get("gross_pnl")
    
    if outcome == "SCALP_EXIT":
        # Use actual scalp exit data
        sxl = safe_float(exit_price)
        pnl = safe_float(net_pnl, None) if net_pnl is not None else None
        if pnl is None:
            pnl = (sxl - entry_price) * contracts
        return sxl, pnl
    
    if outcome == "TIME_STOP":
        sxl = safe_float(exit_price)
        pnl = safe_float(net_pnl)
        return sxl, pnl
    
    if outcome == "STOP_LOSS":
        sxl = safe_float(exit_price)
        pnl = safe_float(net_pnl)
        return sxl, pnl
    
    if outcome == "EXPIRY_WIN":
        return 1.0, contracts * 1.0 - size_usd
    
    if outcome == "EXPIRY_LOSS":
        return 0.0, -size_usd
    
    if outcome == "NO_EXIT_LIQUIDITY":
        # Mark as unrealized or expiry pending
        # If it has exit_price 0, it's an expiry loss; if 1, expiry win
        ep = safe_float(exit_price, 0)
        if ep == 0.0:
            return None, -size_usd
        elif ep == 1.0:
            return None, contracts * 1.0 - size_usd
        else:
            return None, None  # Unrealized
    
    if outcome == "OPEN_UNRESOLVED":
        # Unrealized - compute bid exit if possible
        max_bid = entry.get("max_bid_after_entry")
        if max_bid is not None:
            mb = safe_float(max_bid)
            unrealized = (mb - entry_price) * contracts
            return mb, unrealized
        return None, None  # Cannot determine
    
    if outcome == "INVALID":
        return None, None
    
    return None, None

# ── Step 8: Counterfactual Hold PnL ────────────────────────────────────

def compute_counterfactual_hold(entry, actual_outcome, actual_pnl):
    """
    Compute counterfactual hold-to-expiry PnL.
    """
    size_usd = safe_float(entry.get("size_usd"))
    contracts = safe_float(entry.get("contracts"))
    selected_token_won = entry.get("selected_token_won")
    winning_token_id = entry.get("winning_token_id")
    selected_token_id = entry.get("selected_token_id")
    
    # Determine hold outcome
    if selected_token_won is True:
        hold_outcome = "EXPIRY_WIN"
        hold_pnl = contracts * 1.0 - size_usd
    elif selected_token_won is False:
        hold_outcome = "EXPIRY_LOSS"
        hold_pnl = -size_usd
    elif winning_token_id is not None and selected_token_id is not None:
        if str(selected_token_id) == str(winning_token_id):
            hold_outcome = "EXPIRY_WIN"
            hold_pnl = contracts * 1.0 - size_usd
        else:
            hold_outcome = "EXPIRY_LOSS"
            hold_pnl = -size_usd
    else:
        # If position is open, hold is unresolved
        if actual_outcome == "OPEN_UNRESOLVED":
            hold_outcome = "OPEN_UNRESOLVED"
            hold_pnl = None
        else:
            hold_outcome = "UNKNOWN"
            hold_pnl = None
    
    scalp_better = None
    hold_better = None
    if actual_pnl is not None and hold_pnl is not None:
        scalp_better = actual_pnl > hold_pnl
        hold_better = hold_pnl > actual_pnl
    
    return {
        "position_id": entry.get("position_id"),
        "actual_strategy_outcome": actual_outcome,
        "actual_strategy_pnl": actual_pnl,
        "counterfactual_hold_outcome": hold_outcome,
        "counterfactual_hold_pnl": hold_pnl,
        "scalp_better_than_hold": scalp_better,
        "hold_better_than_scalp": hold_better
    }

# ── Step 9: Open Position Risk Accounting ──────────────────────────────

def compute_open_position_risk(canonical, outcomes, strategy_pnls):
    """
    Account for open positions and their risk.
    """
    open_entries = []
    for i, entry in enumerate(canonical):
        if outcomes[i] == "OPEN_UNRESOLVED":
            open_entries.append((entry, strategy_pnls[i]))
    
    open_count = len(open_entries)
    open_notional = sum(safe_float(e.get("size_usd", 0)) for e, _ in open_entries)
    worst_case_loss = open_notional  # If all expire worthless
    current_bid_exit_value = 0.0
    net_pnl_mark_to_bid = 0.0
    net_pnl_if_expire_zero = 0.0
    
    open_details = []
    for entry, (exit_price, pnl) in open_entries:
        size_usd = safe_float(entry.get("size_usd", 0))
        max_bid = safe_float(entry.get("max_bid_after_entry", 0))
        entry_price = safe_float(entry.get("entry_price", 0))
        contracts = safe_float(entry.get("contracts", 0))
        tte = safe_float(entry.get("time_to_expiry_at_entry", 0))
        
        unrealized_bid_pnl = (max_bid - entry_price) * contracts if max_bid > 0 else 0
        current_bid_exit_value += max_bid * contracts if max_bid > 0 else 0
        net_pnl_mark_to_bid += unrealized_bid_pnl
        net_pnl_if_expire_zero += -size_usd
        
        open_details.append({
            "position_id": entry.get("position_id"),
            "entry_price": entry_price,
            "current_bid": max_bid,
            "unrealized_bid_exit_pnl": round(unrealized_bid_pnl, 4),
            "max_loss_if_expires_zero": -size_usd,
            "time_to_expiry_remaining": tte,
            "current_exit_available": max_bid > 0,
            "current_exit_price": max_bid if max_bid > 0 else None,
            "current_exit_pnl": round(unrealized_bid_pnl, 4) if max_bid > 0 else None
        })
    
    return {
        "open_positions_count": open_count,
        "open_notional": round(open_notional, 2),
        "worst_case_open_loss": round(worst_case_loss, 2),
        "current_bid_exit_value": round(current_bid_exit_value, 2),
        "net_strategy_PnL_including_open_mark_to_bid": round(net_pnl_mark_to_bid, 2),
        "net_strategy_PnL_if_all_open_expire_zero": round(net_pnl_if_expire_zero, 2),
        "open_position_details": open_details
    }

# ── Step 10: Scalp Exit Rate ───────────────────────────────────────────

def compute_scalp_exit_rate(canonical, outcomes):
    """
    Compute scalp exit rate across all entries and by dimension.
    """
    total = len(canonical)
    scalp_exits = sum(1 for o in outcomes if o == "SCALP_EXIT")
    valid_entries = sum(1 for o in outcomes if o != "INVALID")
    
    overall_rate = scalp_exits / valid_entries if valid_entries > 0 else 0
    
    def rate_by(key_func):
        groups = defaultdict(lambda: {"total": 0, "scalp": 0})
        for i, entry in enumerate(canonical):
            if outcomes[i] == "INVALID":
                continue
            k = key_func(entry)
            groups[k]["total"] += 1
            if outcomes[i] == "SCALP_EXIT":
                groups[k]["scalp"] += 1
        return {k: {
            "total": v["total"],
            "scalp_exits": v["scalp"],
            "exit_rate": round(v["scalp"] / v["total"], 4) if v["total"] > 0 else 0
        } for k, v in sorted(groups.items(), key=lambda x: str(x[0]))}
    
    return {
        "total_valid_entries": valid_entries,
        "scalp_exits": scalp_exits,
        "scalp_exit_rate": round(overall_rate, 4),
        "by_asset": rate_by(lambda e: e.get("asset", "unknown")),
        "by_side": rate_by(lambda e: e.get("side", "unknown")),
        "by_bucket": rate_by(lambda e: e.get("actual_bucket", classify_bucket(e.get("entry_price")))),
        "by_TTE_band": rate_by(lambda e: classify_tte_band(e.get("time_to_expiry_at_entry"))),
        "by_spread_band": rate_by(lambda e: classify_spread_band(e.get("entry_spread")))
    }

# ── Step 11: No-Exit Loss Analysis ─────────────────────────────────────

def compute_no_exit_loss_analysis(canonical, outcomes, strategy_pnls):
    """
    For entries that did not scalp-exit, classify why.
    """
    no_exit_entries = []
    for i, entry in enumerate(canonical):
        if outcomes[i] != "SCALP_EXIT":
            no_exit_entries.append((i, entry, outcomes[i], strategy_pnls[i]))
    
    reasons = defaultdict(lambda: {"count": 0, "total_pnl": 0.0, "total_size_usd": 0.0})
    
    for i, entry, outcome, (exit_price, pnl) in no_exit_entries:
        entry_price = safe_float(entry.get("entry_price"))
        max_bid = safe_float(entry.get("max_bid_after_entry", 0))
        spread = safe_float(entry.get("entry_spread", 0))
        tte = safe_float(entry.get("time_to_expiry_at_entry", 0))
        size_usd = safe_float(entry.get("size_usd", 0))
        actual_pnl = safe_float(pnl) if pnl is not None else 0
        
        # Classify reason
        if outcome == "EXPIRY_LOSS":
            if max_bid < entry_price:
                reason = "price_never_repriced_to_target_bid"
            elif max_bid >= entry_price + SCALP_THRESHOLD:
                reason = "scalp_target_reached_but_not_executed"
            elif spread > 0.05:
                reason = "spread_too_wide_after_entry"
            elif tte <= 30:
                reason = "entry_too_late"
            else:
                reason = "market_moved_against_position"
        elif outcome == "EXPIRY_WIN":
            if max_bid >= entry_price + SCALP_THRESHOLD:
                reason = "scalp_target_reached_but_not_executed"
            else:
                reason = "price_never_repriced_to_target_bid"
        elif outcome == "OPEN_UNRESOLVED":
            reason = "bid_liquidity_missing"
        elif outcome == "NO_EXIT_LIQUIDITY":
            reason = "bid_liquidity_missing"
        elif outcome == "INVALID":
            reason = "identity_error"
        else:
            reason = "settlement_loss"
        
        reasons[reason]["count"] += 1
        reasons[reason]["total_pnl"] += actual_pnl
        reasons[reason]["total_size_usd"] += size_usd
    
    # Build avoidability analysis
    avoidable_count = sum(v["count"] for k, v in reasons.items() 
                         if k in ("scalp_target_reached_but_not_executed", "entry_too_late", 
                                   "spread_too_wide_after_entry"))
    avoidable_pnl = sum(v["total_pnl"] for k, v in reasons.items()
                       if k in ("scalp_target_reached_but_not_executed", "entry_too_late",
                                 "spread_too_wide_after_entry"))
    
    return {
        "total_no_exit_entries": len(no_exit_entries),
        "reasons": {k: {
            "count": v["count"],
            "total_pnl": round(v["total_pnl"], 2),
            "total_size_usd": round(v["total_size_usd"], 2)
        } for k, v in sorted(reasons.items(), key=lambda x: -x[1]["count"])},
        "avoidable_by_filtering": {
            "count": avoidable_count,
            "pnl_recoverable": round(avoidable_pnl, 2),
            "filters_that_could_help": [
                "better entry bucket",
                "better TTE filter",
                "asset-side filtering",
                "spread filter",
                "faster exit",
                "lower profit target",
                "time stop",
                "stop loss"
            ]
        }
    }

# ── Step 12: Profit Target Comparison ─────────────────────────────────

def compute_profit_target_comparison(canonical):
    """
    Test profit targets: +1c, +2c, +3c, +5c.
    For each, determine how many entries would have exited based on max_bid_after_entry.
    """
    targets = [0.01, 0.02, 0.03, 0.05]
    results = {}
    
    for target in targets:
        exit_count = 0
        missed_count = 0
        pnls = []
        hold_seconds = []
        
        for entry in canonical:
            entry_price = safe_float(entry.get("entry_price"))
            max_bid = safe_float(entry.get("max_bid_after_entry", 0))
            contracts = safe_float(entry.get("contracts"))
            size_usd = safe_float(entry.get("size_usd"))
            tte = safe_float(entry.get("time_to_expiry_at_entry", 0))
            
            if max_bid >= entry_price + target:
                # Would have exited at this target
                exit_count += 1
                pnl = (max_bid - entry_price) * contracts
                pnls.append(pnl)
                # Approximate hold time as proportional to TTE
                hold_seconds.append(tte // 2)  # rough estimate
            else:
                missed_count += 1
                # If held to expiry, use settlement outcome
                selected_token_won = entry.get("selected_token_won")
                if selected_token_won is True:
                    pnls.append(contracts * 1.0 - size_usd)
                elif selected_token_won is False:
                    pnls.append(-size_usd)
                else:
                    # Open/unresolved
                    pnls.append(0)
                hold_seconds.append(tte)
        
        total_pnl = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
        
        # Max drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        
        avg_hold = sum(hold_seconds) / len(hold_seconds) if hold_seconds else 0
        
        results[f"+{int(target*100)}c"] = {
            "exit_count": exit_count,
            "exit_rate": round(exit_count / len(canonical), 4) if canonical else 0,
            "avg_profit": round(total_pnl / exit_count, 4) if exit_count > 0 else 0,
            "net_strategy_PnL": round(total_pnl, 2),
            "PF": round(pf, 4),
            "max_DD": round(max_dd, 2),
            "avg_hold_seconds": round(avg_hold),
            "missed_exit_count": missed_count
        }
    
    return results

# ── Step 13: Stop-Loss and Time-Stop Simulation ───────────────────────

def compute_stop_time_exit_simulation(canonical):
    """
    Test candidate exits for non-scalpers.
    """
    time_stops = [30, 60, 90, 120]
    tte_exits = [30, 15]  # exit at TTE <= 30s or 15s
    stop_losses = [0.02, 0.03, 0.05]  # bid <= entry - loss
    
    results = {}
    
    # Time stops (hold max N seconds)
    for ts in time_stops:
        label = f"time_stop_{ts}s"
        pnls = []
        exits = 0
        for entry in canonical:
            entry_price = safe_float(entry.get("entry_price"))
            max_bid = safe_float(entry.get("max_bid_after_entry", 0))
            contracts = safe_float(entry.get("contracts"))
            size_usd = safe_float(entry.get("size_usd"))
            tte = safe_float(entry.get("time_to_expiry_at_entry", 0))
            selected_token_won = entry.get("selected_token_won")
            
            # Simulate: if TTE <= ts, would have exited at max_bid
            # If TTE > ts, hold until TTE = ts, then exit at max_bid (approximation)
            # For simplicity: if max_bid reached target, exit at max_bid; else hold to expiry
            if max_bid >= entry_price + SCALP_THRESHOLD:
                # Would have scalp exited before time stop
                exits += 1
                pnls.append((max_bid - entry_price) * contracts)
            elif tte <= ts:
                # Time stop triggers - exit at max_bid
                exits += 1
                pnl = (max_bid - entry_price) * contracts
                pnls.append(pnl)
            else:
                # Would hold to expiry
                if selected_token_won is True:
                    pnls.append(contracts * 1.0 - size_usd)
                elif selected_token_won is False:
                    pnls.append(-size_usd)
                else:
                    pnls.append(0)
        
        total_pnl = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
        
        results[label] = {
            "exits": exits,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(wins / len(pnls), 4) if pnls else 0,
            "PF": round(pf, 4)
        }
    
    # TTE-based exits
    for tte_exit in tte_exits:
        label = f"exit_at_TTE_{tte_exit}s"
        pnls = []
        exits = 0
        for entry in canonical:
            entry_price = safe_float(entry.get("entry_price"))
            max_bid = safe_float(entry.get("max_bid_after_entry", 0))
            contracts = safe_float(entry.get("contracts"))
            size_usd = safe_float(entry.get("size_usd"))
            tte = safe_float(entry.get("time_to_expiry_at_entry", 0))
            selected_token_won = entry.get("selected_token_won")
            
            if max_bid >= entry_price + SCALP_THRESHOLD:
                exits += 1
                pnls.append((max_bid - entry_price) * contracts)
            elif tte <= tte_exit:
                # Exit at TTE threshold
                exits += 1
                pnls.append((max_bid - entry_price) * contracts)
            else:
                if selected_token_won is True:
                    pnls.append(contracts * 1.0 - size_usd)
                elif selected_token_won is False:
                    pnls.append(-size_usd)
                else:
                    pnls.append(0)
        
        total_pnl = sum(pnls)
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
        
        results[label] = {
            "exits": exits,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else 0,
            "PF": round(pf, 4)
        }
    
    # Stop losses
    for sl in stop_losses:
        label = f"stop_loss_{int(sl*100)}c"
        pnls = []
        exits = 0
        for entry in canonical:
            entry_price = safe_float(entry.get("entry_price"))
            max_bid = safe_float(entry.get("max_bid_after_entry", 0))
            min_bid = safe_float(entry.get("min_bid_after_entry", max_bid))
            contracts = safe_float(entry.get("contracts"))
            size_usd = safe_float(entry.get("size_usd"))
            tte = safe_float(entry.get("time_to_expiry_at_entry", 0))
            selected_token_won = entry.get("selected_token_won")
            
            # Check if stop loss triggered (bid dropped below entry - sl)
            if min_bid <= entry_price - sl:
                exits += 1
                # Exit at stop loss price
                pnls.append((entry_price - sl - entry_price) * contracts)  # = -sl * contracts
            elif max_bid >= entry_price + SCALP_THRESHOLD:
                exits += 1
                pnls.append((max_bid - entry_price) * contracts)
            else:
                if selected_token_won is True:
                    pnls.append(contracts * 1.0 - size_usd)
                elif selected_token_won is False:
                    pnls.append(-size_usd)
                else:
                    pnls.append(0)
        
        total_pnl = sum(pnls)
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
        
        results[label] = {
            "exits": exits,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else 0,
            "PF": round(pf, 4)
        }
    
    return results

# ── Step 14: Slippage and Fill Stress ──────────────────────────────────

def compute_slippage_stress(canonical):
    """
    Stress-test every entry and exit.
    Entry: best ask, one tick worse, two ticks worse, depth-adjusted
    Exit: best bid, one tick worse, two ticks worse, depth-adjusted
    """
    TICK = 0.01  # 1¢ tick on Polymarket
    results = {}
    
    for label, entry_adj, exit_adj in [
        ("base_case", 0, 0),
        ("entry_one_tick_worse", TICK, 0),
        ("entry_two_ticks_worse", 2*TICK, 0),
        ("exit_one_tick_worse", 0, -TICK),
        ("exit_two_ticks_worse", 0, -2*TICK),
        ("both_one_tick_worse", TICK, -TICK),
    ]:
        pnls = []
        edge_survives = 0
        edge_fails_one = 0
        edge_fails_two = 0
        
        for entry in canonical:
            entry_price = safe_float(entry.get("entry_price"))
            max_bid = safe_float(entry.get("max_bid_after_entry", 0))
            contracts = safe_float(entry.get("contracts"))
            size_usd = safe_float(entry.get("size_usd"))
            
            stressed_entry = entry_price + entry_adj
            stressed_exit = max_bid + exit_adj
            
            # Check if scalp exit would still trigger
            if max_bid >= entry_price + SCALP_THRESHOLD:
                # Would scalp
                pnl = (stressed_exit - stressed_entry) * contracts
                pnls.append(pnl)
                if pnl > 0:
                    edge_survives += 1
                elif entry_adj == TICK:
                    edge_fails_one += 1
                elif entry_adj == 2*TICK:
                    edge_fails_two += 1
            else:
                # Held to expiry
                selected_token_won = entry.get("selected_token_won")
                if selected_token_won is True:
                    pnls.append(contracts * 1.0 - size_usd)
                elif selected_token_won is False:
                    pnls.append(-size_usd)
                else:
                    pnls.append(0)
        
        total_pnl = sum(pnls)
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
        
        results[label] = {
            "net_pnl": round(total_pnl, 2),
            "PF": round(pf, 4),
            "edge_survives": edge_survives,
            "edge_fails_one_tick": edge_fails_one,
            "edge_fails_two_ticks": edge_fails_two
        }
    
    # Classify
    base_pnl = results["base_case"]["net_pnl"]
    one_tick_pnl = results["exit_one_tick_worse"]["net_pnl"]
    two_tick_pnl = results["exit_two_ticks_worse"]["net_pnl"]
    
    if base_pnl > 0 and one_tick_pnl > 0 and two_tick_pnl > 0:
        classification = "EDGE_SURVIVES_SLIPPAGE"
    elif base_pnl > 0 and one_tick_pnl > 0 and two_tick_pnl <= 0:
        classification = "EDGE_FAILS_TWO_TICKS_WORSE"
    elif base_pnl > 0 and one_tick_pnl <= 0:
        classification = "EDGE_FAILS_ONE_TICK_WORSE"
    else:
        classification = "EDGE_FAILS_DEPTH_ADJUSTMENT"
    
    results["classification"] = classification
    return results

# ── Step 15: XRP 5m DOWN Focus Report ──────────────────────────────────

def compute_xrp_5m_down_report(canonical, outcomes, strategy_pnls, counterfactuals):
    """
    For XRP 5m DOWN only, compute survival report.
    """
    xrp_entries = []
    for i, entry in enumerate(canonical):
        if entry.get("asset") == "XRP" and entry.get("side") == "DOWN" and entry.get("interval") == "5m":
            xrp_entries.append((i, entry, outcomes[i], strategy_pnls[i]))
    
    if not xrp_entries:
        return {
            "classification": "XRP_5M_DOWN_INCONCLUSIVE_OPEN_RISK_TOO_HIGH",
            "total_entries": 0,
            "error": "No XRP 5m DOWN entries found"
        }
    
    total = len(xrp_entries)
    closed = sum(1 for _, _, o, _ in xrp_entries if o not in ("OPEN_UNRESOLVED", "INVALID"))
    open_count = sum(1 for _, _, o, _ in xrp_entries if o == "OPEN_UNRESOLVED")
    scalp_exits = sum(1 for _, _, o, _ in xrp_entries if o == "SCALP_EXIT")
    
    # Strategy PnL
    closed_pnls = []
    for _, _, o, (_, pnl) in xrp_entries:
        if o not in ("OPEN_UNRESOLVED", "INVALID") and pnl is not None:
            closed_pnls.append(pnl)
    
    strategy_pnl = sum(closed_pnls) if closed_pnls else 0
    
    # Counterfactual
    hold_pnls = []
    for i, entry, o, _ in xrp_entries:
        for cf in counterfactuals:
            if cf["position_id"] == entry["position_id"]:
                if cf["counterfactual_hold_pnl"] is not None:
                    hold_pnls.append(cf["counterfactual_hold_pnl"])
                break
    hold_pnl = sum(hold_pnls) if hold_pnls else 0
    
    # PF
    gross_profit = sum(p for p in closed_pnls if p > 0)
    gross_loss = abs(sum(p for p in closed_pnls if p < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
    
    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for p in closed_pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    
    # Max loss streak
    max_streak = 0
    current_streak = 0
    for p in closed_pnls:
        if p < 0:
            current_streak += 1
            if current_streak > max_streak:
                max_streak = current_streak
        else:
            current_streak = 0
    
    # Averages
    entry_prices = [safe_float(e.get("entry_price")) for _, e, _, _ in xrp_entries]
    exit_bids = [safe_float(e.get("max_bid_after_entry", 0)) for _, e, _, _ in xrp_entries]
    
    # TTE bands
    tte_bands = defaultdict(lambda: {"count": 0, "pnl": 0.0, "scalp": 0})
    for _, entry, o, (_, pnl) in xrp_entries:
        band = classify_tte_band(entry.get("time_to_expiry_at_entry"))
        tte_bands[band]["count"] += 1
        if pnl is not None:
            tte_bands[band]["pnl"] += pnl
        if o == "SCALP_EXIT":
            tte_bands[band]["scalp"] += 1
    best_tte = max(tte_bands.items(), key=lambda x: x[1]["scalp"] / x[1]["count"] if x[1]["count"] > 0 else 0)[0] if tte_bands else "unknown"
    
    # Buckets
    bucket_perf = defaultdict(lambda: {"count": 0, "pnl": 0.0, "scalp": 0})
    for _, entry, o, (_, pnl) in xrp_entries:
        bucket = entry.get("actual_bucket", classify_bucket(entry.get("entry_price")))
        bucket_perf[bucket]["count"] += 1
        if pnl is not None:
            bucket_perf[bucket]["pnl"] += pnl
        if o == "SCALP_EXIT":
            bucket_perf[bucket]["scalp"] += 1
    best_bucket = max(bucket_perf.items(), key=lambda x: x[1]["scalp"] / x[1]["count"] if x[1]["count"] > 0 else 0)[0] if bucket_perf else "unknown"
    
    # Worst failure mode
    no_scalp = [(_, e, o, p) for _, e, o, p in xrp_entries if o != "SCALP_EXIT"]
    failure_reasons = Counter()
    for _, _, o, _ in no_scalp:
        failure_reasons[o] += 1
    worst_failure = failure_reasons.most_common(1)[0][0] if failure_reasons else "none"
    
    # Open risk
    open_entries = [e for _, e, o, _ in xrp_entries if o == "OPEN_UNRESOLVED"]
    open_risk = sum(safe_float(e.get("size_usd", 0)) for e in open_entries)
    
    # Classification
    closed_valid = sum(1 for _, _, o, _ in xrp_entries if o not in ("OPEN_UNRESOLVED", "INVALID"))
    if closed_valid < 25:
        classification = "XRP_5M_DOWN_PROMISING_BUT_UNPROVEN"
    elif strategy_pnl > 0 and pf >= 1.25:
        classification = "XRP_5M_DOWN_POSITIVE_AFTER_FULL_ENTRY_ACCOUNTING"
    elif open_risk > abs(strategy_pnl) * 2 and open_risk > 20:
        classification = "XRP_5M_DOWN_INCONCLUSIVE_OPEN_RISK_TOO_HIGH"
    else:
        classification = "XRP_5M_DOWN_FAILS_FULL_ENTRY_ACCOUNTING"
    
    return {
        "total_entries": total,
        "valid_entries": closed,
        "open_positions": open_count,
        "closed_positions": closed,
        "scalp_exits": scalp_exits,
        "scalp_exit_rate": round(scalp_exits / total, 4) if total > 0 else 0,
        "actual_strategy_PnL": round(strategy_pnl, 2),
        "counterfactual_hold_PnL": round(hold_pnl, 2),
        "PF": round(pf, 4),
        "max_DD": round(max_dd, 2),
        "max_loss_streak": max_streak,
        "avg_entry_price": round(sum(entry_prices) / len(entry_prices), 4) if entry_prices else 0,
        "avg_exit_bid": round(sum(exit_bids) / len(exit_bids), 4) if exit_bids else 0,
        "best_TTE_band": best_tte,
        "best_bucket": best_bucket,
        "worst_failure_mode": worst_failure,
        "open_risk": round(open_risk, 2),
        "classification": classification
    }

# ── Step 16: Aggregate Strategy Report ─────────────────────────────────

def compute_aggregate_report(canonical, outcomes, strategy_pnls, counterfactuals):
    """
    Across all cells, compute aggregate strategy survival report.
    """
    total = len(canonical)
    closed = sum(1 for o in outcomes if o not in ("OPEN_UNRESOLVED", "INVALID"))
    open_count = sum(1 for o in outcomes if o == "OPEN_UNRESOLVED")
    scalp_exits = sum(1 for o in outcomes if o == "SCALP_EXIT")
    
    # Closed PnL
    closed_pnls = []
    for i, (exit_price, pnl) in enumerate(strategy_pnls):
        if outcomes[i] not in ("OPEN_UNRESOLVED", "INVALID") and pnl is not None:
            closed_pnls.append(pnl)
    
    closed_pnl = sum(closed_pnls) if closed_pnls else 0
    
    # Open bid mark
    open_pnl_mark = 0.0
    open_pnl_expire_zero = 0.0
    for i, entry in enumerate(canonical):
        if outcomes[i] == "OPEN_UNRESOLVED":
            max_bid = safe_float(entry.get("max_bid_after_entry", 0))
            entry_price = safe_float(entry.get("entry_price", 0))
            contracts = safe_float(entry.get("contracts", 0))
            size_usd = safe_float(entry.get("size_usd", 0))
            open_pnl_mark += (max_bid - entry_price) * contracts
            open_pnl_expire_zero += -size_usd
    
    # PF
    gross_profit = sum(p for p in closed_pnls if p > 0)
    gross_loss = abs(sum(p for p in closed_pnls if p < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
    
    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for p in closed_pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    
    # Best/worst cell
    cell_perf = defaultdict(lambda: {"count": 0, "pnl": 0.0, "scalp": 0})
    for i, entry in enumerate(canonical):
        cell = entry.get("cell_id", f"{entry.get('asset','?')}_{entry.get('interval','?')}_{entry.get('side','?')}")
        cell_perf[cell]["count"] += 1
        _, pnl = strategy_pnls[i]
        if pnl is not None and outcomes[i] not in ("OPEN_UNRESOLVED", "INVALID"):
            cell_perf[cell]["pnl"] += pnl
        if outcomes[i] == "SCALP_EXIT":
            cell_perf[cell]["scalp"] += 1
    
    if cell_perf:
        best_cell = max(cell_perf.items(), key=lambda x: x[1]["pnl"])
        worst_cell = min(cell_perf.items(), key=lambda x: x[1]["pnl"])
        best_cell_name = best_cell[0]
        worst_cell_name = worst_cell[0]
    else:
        best_cell_name = "none"
        worst_cell_name = "none"
    
    cells_positive = sum(1 for _, v in cell_perf.items() if v["pnl"] > 0)
    cells_negative = sum(1 for _, v in cell_perf.items() if v["pnl"] < 0)
    
    return {
        "total_entries": total,
        "closed_entries": closed,
        "open_entries": open_count,
        "scalp_exits": scalp_exits,
        "scalp_exit_rate": round(scalp_exits / total, 4) if total > 0 else 0,
        "strategy_net_PnL_closed_only": round(closed_pnl, 2),
        "strategy_net_PnL_including_open_bid_mark": round(closed_pnl + open_pnl_mark, 2),
        "strategy_net_PnL_if_open_expire_zero": round(closed_pnl + open_pnl_expire_zero, 2),
        "PF": round(pf, 4),
        "max_DD": round(max_dd, 2),
        "best_cell": best_cell_name,
        "worst_cell": worst_cell_name,
        "cells_positive_after_full_accounting": cells_positive,
        "cells_negative_after_full_accounting": cells_negative,
        "cell_breakdown": {k: {
            "count": v["count"],
            "pnl": round(v["pnl"], 2),
            "scalp_exits": v["scalp"],
            "scalp_rate": round(v["scalp"] / v["count"], 4) if v["count"] > 0 else 0
        } for k, v in sorted(cell_perf.items())}
    }

# ── Main ────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("V21.7.57 Full-Entry Scalp Survival Accounting")
    print("LIVE AUTHORIZATION SUSPENDED — PAPER MODE ONLY")
    print("=" * 70)
    
    # Create output directory
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SUPERVISOR_DIR.mkdir(parents=True, exist_ok=True)
    
    # Step 5: Canonical Entry Universe
    print("\n[5] Building canonical entry universe...")
    canonical, hard_failures = build_canonical_entry_universe()
    
    if hard_failures:
        print(f"  ⚠ HARD FAILURES ({len(hard_failures)}):")
        for hf in hard_failures:
            print(f"    - {hf}")
    
    write_jsonl(OUT_DIR / "canonical_entry_universe.jsonl", canonical)
    print(f"  Canonical entries: {len(canonical)}")
    print(f"  Written: canonical_entry_universe.jsonl")
    
    # Step 6: Final Outcome Classification
    print("\n[6] Classifying final outcomes...")
    outcomes = []
    for entry in canonical:
        outcome = classify_final_outcome(entry)
        outcomes.append(outcome)
    
    outcome_dist = Counter(outcomes)
    print(f"  Outcome distribution: {dict(outcome_dist)}")
    
    classification_output = []
    for i, entry in enumerate(canonical):
        classification_output.append({
            "position_id": entry["position_id"],
            "cell_id": entry.get("cell_id"),
            "asset": entry.get("asset"),
            "side": entry.get("side"),
            "interval": entry.get("interval"),
            "entry_price": entry.get("entry_price"),
            "exit_price": entry.get("exit_price"),
            "exit_reason": entry.get("exit_reason"),
            "status": entry.get("status"),
            "selected_token_won": entry.get("selected_token_won"),
            "final_strategy_outcome": outcomes[i]
        })
    write_jsonl(OUT_DIR / "final_outcome_classification.jsonl", classification_output)
    print(f"  Written: final_outcome_classification.jsonl")
    
    # Step 7: Actual Strategy PnL
    print("\n[7] Computing actual strategy PnL...")
    strategy_pnls = []  # list of (exit_price, pnl) tuples
    pnl_output = []
    pnl_count_per_position = Counter()
    
    for i, entry in enumerate(canonical):
        exit_price, pnl = compute_strategy_pnl(entry, outcomes[i])
        strategy_pnls.append((exit_price, pnl))
        pnl_count_per_position[entry["position_id"]] += 1
        pnl_output.append({
            "position_id": entry["position_id"],
            "final_strategy_outcome": outcomes[i],
            "strategy_exit_price": exit_price,
            "strategy_pnl": round(pnl, 4) if pnl is not None else None,
            "entry_price": entry.get("entry_price"),
            "contracts": entry.get("contracts"),
            "size_usd": entry.get("size_usd")
        })
    
    # Hard fail checks
    double_counted = [pid for pid, cnt in pnl_count_per_position.items() if cnt > 1]
    if double_counted:
        print(f"  ⚠ PnL counted more than once for: {double_counted}")
    
    # Check that scalp-exited positions aren't later counted as expiry loss
    for i, entry in enumerate(canonical):
        if outcomes[i] == "SCALP_EXIT" and entry.get("selected_token_won") is False:
            print(f"  ⚠ Scalp exit {entry['position_id']} has selected_token_won=False — ensure no double-count")
    
    write_jsonl(OUT_DIR / "actual_strategy_pnl.jsonl", pnl_output)
    print(f"  Written: actual_strategy_pnl.jsonl")
    
    # Step 8: Counterfactual Hold PnL
    print("\n[8] Computing counterfactual hold PnL...")
    counterfactuals = []
    for i, entry in enumerate(canonical):
        _, actual_pnl = strategy_pnls[i]
        cf = compute_counterfactual_hold(entry, outcomes[i], actual_pnl)
        counterfactuals.append(cf)
    write_jsonl(OUT_DIR / "counterfactual_hold_pnl.jsonl", counterfactuals)
    print(f"  Written: counterfactual_hold_pnl.jsonl")
    
    # Step 9: Open Position Risk
    print("\n[9] Computing open position risk...")
    open_risk = compute_open_position_risk(canonical, outcomes, strategy_pnls)
    write_json(OUT_DIR / "open_position_risk.json", open_risk)
    print(f"  Open positions: {open_risk['open_positions_count']}")
    print(f"  Open notional: ${open_risk['open_notional']}")
    print(f"  Worst case loss: ${open_risk['worst_case_open_loss']}")
    print(f"  Written: open_position_risk.json")
    
    # Step 10: Scalp Exit Rate
    print("\n[10] Computing scalp exit rate...")
    scalp_rate = compute_scalp_exit_rate(canonical, outcomes)
    write_json(OUT_DIR / "scalp_exit_rate_report.json", scalp_rate)
    print(f"  Scalp exit rate: {scalp_rate['scalp_exit_rate']:.2%} ({scalp_rate['scalp_exits']}/{scalp_rate['total_valid_entries']})")
    print(f"  Written: scalp_exit_rate_report.json")
    
    # Step 11: No-Exit Loss Analysis
    print("\n[11] Computing no-exit loss analysis...")
    no_exit = compute_no_exit_loss_analysis(canonical, outcomes, strategy_pnls)
    write_json(OUT_DIR / "no_exit_loss_analysis.json", no_exit)
    print(f"  No-exit entries: {no_exit['total_no_exit_entries']}")
    print(f"  Written: no_exit_loss_analysis.json")
    
    # Step 12: Profit Target Comparison
    print("\n[12] Computing profit target comparison...")
    target_comp = compute_profit_target_comparison(canonical)
    write_json(OUT_DIR / "profit_target_comparison.json", target_comp)
    for target, data in target_comp.items():
        print(f"  {target}: exits={data['exit_count']}, PnL=${data['net_strategy_PnL']}, PF={data['PF']}")
    print(f"  Written: profit_target_comparison.json")
    
    # Step 13: Stop-Loss and Time-Stop Simulation
    print("\n[13] Computing stop/time exit simulation...")
    stop_time = compute_stop_time_exit_simulation(canonical)
    write_json(OUT_DIR / "stop_time_exit_simulation.json", stop_time)
    for label, data in stop_time.items():
        print(f"  {label}: PnL=${data['total_pnl']}, PF={data['PF']}")
    print(f"  Written: stop_time_exit_simulation.json")
    
    # Step 14: Slippage and Fill Stress
    print("\n[14] Computing slippage and fill stress...")
    slippage = compute_slippage_stress(canonical)
    write_json(OUT_DIR / "slippage_fill_stress_report.json", slippage)
    print(f"  Classification: {slippage['classification']}")
    print(f"  Written: slippage_fill_stress_report.json")
    
    # Step 15: XRP 5m DOWN Focus Report
    print("\n[15] Computing XRP 5m DOWN survival report...")
    xrp_report = compute_xrp_5m_down_report(canonical, outcomes, strategy_pnls, counterfactuals)
    write_json(OUT_DIR / "xrp_5m_down_survival_report.json", xrp_report)
    print(f"  Classification: {xrp_report['classification']}")
    print(f"  Total entries: {xrp_report.get('total_entries', 0)}")
    print(f"  Scalp exits: {xrp_report.get('scalp_exits', 0)}")
    print(f"  Strategy PnL: ${xrp_report.get('actual_strategy_PnL', 0)}")
    print(f"  Written: xrp_5m_down_survival_report.json")
    
    # Step 16: Aggregate Strategy Report
    print("\n[16] Computing aggregate strategy survival report...")
    aggregate = compute_aggregate_report(canonical, outcomes, strategy_pnls, counterfactuals)
    write_json(OUT_DIR / "aggregate_strategy_survival_report.json", aggregate)
    print(f"  Total entries: {aggregate['total_entries']}")
    print(f"  Scalp exit rate: {aggregate['scalp_exit_rate']:.2%}")
    print(f"  Closed PnL: ${aggregate['strategy_net_PnL_closed_only']}")
    print(f"  PF: {aggregate['PF']}")
    print(f"  Written: aggregate_strategy_survival_report.json")
    
    # Step 20: Final Report
    print("\n[20] Generating final report...")
    final_report = {
        "module": "V21.7.57",
        "mode": "FULL_ENTRY_SCALP_SURVIVAL_ACCOUNTING",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "real_orders_allowed": REAL_ORDERS_ALLOWED,
        "live_authorization_suspended": LIVE_AUTHORIZATION_SUSPENDED,
        "wallet_spend_allowed": WALLET_SPEND_ALLOWED,
        "canonical_entries": len(canonical),
        "outcome_distribution": dict(outcome_dist),
        "scalp_exits": int(outcome_dist.get("SCALP_EXIT", 0)),
        "scalp_exit_rate": round(outcome_dist.get("SCALP_EXIT", 0) / len(canonical), 4) if canonical else 0,
        "closed_strategy_pnl": aggregate["strategy_net_PnL_closed_only"],
        "open_risk_worst_case": open_risk["worst_case_open_loss"],
        "aggregate_PF": aggregate["PF"],
        "xrp_5m_down_classification": xrp_report["classification"],
        "slippage_classification": slippage["classification"],
        "best_cell": aggregate["best_cell"],
        "worst_cell": aggregate["worst_cell"],
        "profit_target_comparison_summary": {
            k: {"exit_count": v["exit_count"], "net_pnl": v["net_strategy_PnL"], "PF": v["PF"]}
            for k, v in target_comp.items()
        },
        "stop_time_best": {
            k: v for k, v in sorted(stop_time.items(), key=lambda x: -x[1]["total_pnl"])[:3]
        },
        "hard_failures": hard_failures,
        "status": "V21.7.57_FULL_ENTRY_SCALP_SURVIVAL_COMPLETE"
    }
    write_json(OUT_DIR / "v21757_final_report.json", final_report)
    print(f"  Written: v21757_final_report.json")
    
    # Step 19: Supervisor Output
    print("\n[19] Writing supervisor status...")
    
    # Promotion gate check (Step 17)
    closed_valid_entries = aggregate["closed_entries"]
    xrp_closed = xrp_report.get("closed_positions", 0)
    strategy_net_pnl = aggregate["strategy_net_PnL_closed_only"]
    agg_pf = aggregate["PF"]
    max_dd = aggregate["max_DD"]
    
    promotion_review_allowed = (
        closed_valid_entries >= 50 and
        xrp_closed >= 25 and
        strategy_net_pnl > 0 and
        agg_pf >= 1.25 and
        max_dd <= 0.15 * abs(strategy_net_pnl) if strategy_net_pnl != 0 else False and
        open_risk["worst_case_open_loss"] < 50 and
        slippage["classification"] == "EDGE_SURVIVES_SLIPPAGE" and
        len(hard_failures) == 0
    )
    
    # Determine halt reason if applicable
    halt_reason = None
    halted = False
    if not REAL_ORDERS_ALLOWED:
        # This is expected, not a halt
        pass
    if LIVE_AUTHORIZATION_SUSPENDED:
        pass
    if hard_failures:
        halted = True
        halt_reason = f"HARD_FAILURES: {len(hard_failures)} entries failed validation"
    
    supervisor = {
        "real_orders_allowed": REAL_ORDERS_ALLOWED,
        "live_authorization_suspended": LIVE_AUTHORIZATION_SUSPENDED,
        "wallet_spend_allowed": WALLET_SPEND_ALLOWED,
        "total_entries": len(canonical),
        "closed_entries": aggregate["closed_entries"],
        "open_entries": aggregate["open_entries"],
        "scalp_exits": aggregate["scalp_exits"],
        "scalp_exit_rate": aggregate["scalp_exit_rate"],
        "closed_strategy_PnL": aggregate["strategy_net_PnL_closed_only"],
        "open_risk_worst_case": open_risk["worst_case_open_loss"],
        "strategy_PnL_if_open_expire_zero": aggregate["strategy_net_PnL_if_open_expire_zero"],
        "aggregate_PF": aggregate["PF"],
        "xrp_5m_down_classification": xrp_report["classification"],
        "best_cell": aggregate["best_cell"],
        "worst_cell": aggregate["worst_cell"],
        "promotion_review_allowed": promotion_review_allowed,
        "halted": halted,
        "halt_reason": halt_reason,
        "next_action": "accumulate_more_paper_data_until_promotion_gates_pass" if not promotion_review_allowed else "separate_live_review_directive_required",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module": "V21.7.57"
    }
    write_json(SUPERVISOR_DIR / "v21757_full_entry_scalp_survival_status.json", supervisor)
    print(f"  Written: supervisor/v21757_full_entry_scalp_survival_status.json")
    
    # Step 18: Live Scope Protection Assertions
    print("\n[18] Live scope protection assertions...")
    assert REAL_ORDERS_ALLOWED == False, "VIOLATION: real_orders_allowed is True"
    assert LIVE_AUTHORIZATION_SUSPENDED == True, "VIOLATION: live_authorization_suspended is False"
    assert WALLET_SPEND_ALLOWED == False, "VIOLATION: wallet_spend_allowed is True"
    print("  ✓ No live orders submitted")
    print("  ✓ No wallet spend")
    print("  ✓ All positions are paper")
    print("  ✓ Live authorization suspended")
    
    # Summary
    print("\n" + "=" * 70)
    print("V21.7.57 FULL ENTRY SCALP SURVIVAL ACCOUNTING — COMPLETE")
    print("=" * 70)
    print(f"\nCanonical entries: {len(canonical)}")
    print(f"Outcome distribution: {dict(outcome_dist)}")
    print(f"Scalp exit rate: {aggregate['scalp_exit_rate']:.2%}")
    print(f"Closed strategy PnL: ${aggregate['strategy_net_PnL_closed_only']}")
    print(f"PF: {aggregate['PF']}")
    print(f"XRP 5m DOWN: {xrp_report['classification']}")
    print(f"Slippage: {slippage['classification']}")
    print(f"Promotion review allowed: {promotion_review_allowed}")
    print(f"Live authorization: SUSPENDED")
    print(f"\nAll 14 outputs generated successfully.")
    
    return final_report

if __name__ == "__main__":
    main()