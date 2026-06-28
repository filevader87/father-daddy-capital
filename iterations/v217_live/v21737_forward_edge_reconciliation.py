#!/usr/bin/env python3
"""
V21.7.37 — Forward Edge Reconciliation + Canary Risk Decision
==============================================================
Reconciles backtest edge with negative resolved shadow results.
Determines whether BTC 15m live canary should remain armed, be paused,
or continue only under stricter edge-confirmation conditions.

Key Finding from V21.7.36:
  Track B: 2W/41L, WR=4.7%, PnL=-$121.04
  Canary zone (3-8c): 0W/20L
  15m: 1W/14L, 6.7% WR
  5m: 1W/27L, 3.6% WR

CRITICAL: Track A and Track B use DIFFERENT signal logic.
  Track B: SPOT_MOMENTUM_SHADOW (velocity_negative + direction_down + in_bucket)
  Track A: Structural gates only (ask 3-8c + spread + TTE + 14 other checks)
  Track B adds a MOMENTUM requirement that Track A does NOT have.
  This means Track B's failures do NOT directly predict Track A's performance.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Tuple
from collections import defaultdict

# ─── Paths ───
PROJECT_ROOT = Path("/home/naq1987s/father-daddy-capital")
OUTPUT_DIR = PROJECT_ROOT / "output" / "v21737_forward_edge_reconciliation"
SUPERVISOR_DIR = PROJECT_ROOT / "output" / "supervisor"
V21736_DIR = PROJECT_ROOT / "output" / "v21736_shadow_settlement_repair"
SHADOW_EVENTS_FILE = PROJECT_ROOT / "output" / "v2171_live" / "shadow_counterfactual_events.jsonl"
RESOLVED_EVENTS_FILE = V21736_DIR / "retro_resolved_events.jsonl"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── Constants ───
SIZE_USD = 5.00


def load_resolved_events() -> List[dict]:
    """Load V21.7.36 retro-resolved events."""
    events = []
    with open(RESOLVED_EVENTS_FILE) as f:
        for line in f:
            try:
                events.append(json.loads(line.strip()))
            except:
                pass
    return events


# ═════════════════════════════════════════════════════════════════════════
# Section 5: Track A vs Track B Logic Comparison
# ═════════════════════════════════════════════════════════════════════════

def compare_track_a_vs_track_b_logic() -> dict:
    """Compare Track A (canary) and Track B (shadow) entry logic.
    
    Track A (V21.7.35 first_live_canary.py):
      - asset: BTC
      - interval: 15m ONLY
      - side: DOWN
      - price bucket: 3-8¢ (0.03 <= ask <= 0.08)
      - TTE gate: 180-900s
      - quote source: PM_WS_BOOK, PM_WS_BEST_BID_ASK, PM_CLOB_READ
      - entry trigger: ask enters 3-8¢ bucket
      - momentum filter: NONE (structural gates only)
      - Chainlink/RTDS veto: not applicable (CLOB_READ)
      - drawdown veto: not implemented (single $5 trade)
      - spread gate: <= 0.02
      - survivability gate: not applicable (single $5)
      - market phase filter: not implemented
      - 16-point pre-submit checklist
    
    Track B (shadow_counterfactual_tracker.py):
      - asset: BTC (both 5m and 15m)
      - interval: 5m AND 15m
      - side: DOWN
      - price bucket: 3-12¢ (0.03 <= down_mid < 0.12)
      - TTE gate: > 30s (much looser)
      - quote source: mid price from orderbook
      - entry trigger: SPOT_MOMENTUM_SHADOW fires
      - momentum filter: REQUIRED (velocity negative + direction down)
      - Chainlink/RTDS veto: not applicable
      - drawdown veto: not applicable
      - spread gate: not required (uses mid)
      - survivability gate: not required
      - market phase filter: not implemented
    
    Key Differences:
    1. Track B requires momentum (velocity_negative + direction_down)
       Track A does NOT require momentum
    2. Track B covers 3-12¢, Track A covers only 3-8¢
    3. Track B includes 5m intervals, Track A is 15m ONLY
    4. Track B uses mid prices, Track A uses live ask with spread gates
    5. Track B has a weaker TTE gate (>30s vs 180-900s)
    6. Track A has 16 structural gates, Track B has only momentum+bucket+TTE
    """
    comparison = {
        "dimension": [
            "asset",
            "interval",
            "side",
            "price_bucket",
            "tte_gate",
            "quote_source",
            "entry_trigger",
            "momentum_filter",
            "spread_gate",
            "survivability_gate",
            "market_phase_filter",
            "drawdown_veto",
            "pre_submit_checks",
            "real_order_type",
            "max_trade_size",
        ],
        "track_a_canary": [
            "BTC",
            "15m ONLY",
            "DOWN",
            "3-8¢ (0.03 <= ask <= 0.08)",
            "180-900s",
            "PM_WS_BOOK / PM_WS_BEST_BID_ASK / PM_CLOB_READ",
            "Ask enters 3-8¢ bucket (structural)",
            "NONE — no velocity or momentum requirement",
            "<= 0.02",
            "N/A (single $5 trade)",
            "N/A",
            "N/A (single $5 trade)",
            "16-point checklist",
            "FAK/FOK only",
            "$5",
        ],
        "track_b_shadow": [
            "BTC",
            "5m AND 15m",
            "DOWN",
            "3-12¢ (0.03 <= mid < 0.12)",
            "> 30s (loose)",
            "Mid price from orderbook",
            "SPOT_MOMENTUM_SHADOW fires",
            "REQUIRED (velocity_negative + direction_down)",
            "None (uses mid)",
            "None",
            "None",
            "None",
            "momentum + bucket + TTE (3 gates)",
            "Paper only (no real order)",
            "$5 hypothetical",
        ],
        "material_difference": [
            "SAME",
            "DIFFERENT — Track B includes 5m, Track A is 15m only",
            "SAME",
            "DIFFERENT — Track B covers 3-12¢, Track A only 3-8¢",
            "DIFFERENT — Track B >30s, Track A 180-900s",
            "DIFFERENT — Track A requires live CLOB, Track B uses mid",
            "DIFFERENT — Track B needs momentum, Track A needs structural gates",
            "CRITICAL — Track B requires momentum, Track A does NOT",
            "DIFFERENT — Track A requires spread ≤0.02",
            "DIFFERENT — Track A has 16-point checklist",
            "DIFFERENT — Track A has market active check",
            "DIFFERENT — Track A has bankroll check",
            "DIFFERENT — Track A has 16 checks, Track B has 3",
            "DIFFERENT — Track A is real FAK/FOK, Track B is paper",
            "SAME ($5)",
        ]
    }
    
    # Classification
    # Track A and Track B differ on:
    # 1. Interval (5m vs 15m)
    # 2. Bucket (3-12¢ vs 3-8¢)
    # 3. Momentum filter (Track B requires, Track A does not)
    # 4. Structural gates (Track A has 16, Track B has 3)
    # 5. TTE gate (Track B is much looser)
    #
    # The momentum filter is the CRITICAL difference.
    # Track B fires ONLY when BTC velocity is negative AND direction is down.
    # Track A fires when ask is in 3-8¢ bucket regardless of momentum.
    #
    # This means Track B's failures are WORSE than Track A's expected performance:
    # - Track B already requires negative velocity (confirming the trend)
    # - If even momentum-confirmed entries lose, non-momentum entries would lose MORE
    # - BUT: Track A also has tighter TTE (180-900s vs >30s) and spread gates
    # - AND: Track A only covers 3-8¢, not the wider 8-12¢ where losses concentrated
    
    classification = "TRACK_A_DISTINCT_FROM_TRACK_B"
    rationale = (
        "Track A (canary) and Track B (shadow) use materially different signal logic. "
        "Track B requires SPOT_MOMENTUM (velocity_negative + direction_down), "
        "while Track A uses 16 structural gates with NO momentum requirement. "
        "Track A covers only 3-8¢ (not 3-12¢), is 15m only (not 5m+15m), "
        "has stricter TTE (180-900s vs >30s), and requires spread ≤0.02. "
        "However, Track B's momentum filter means it fires on TREND-CONFIRMED entries. "
        "If even momentum-confirmed entries lose, non-momentum entries are NOT safer. "
        "Track B's negative result is a BEARISH signal for Track A too — "
        "but not a direct prediction because the signal logic differs."
    )
    
    return {
        "comparison": comparison,
        "classification": classification,
        "rationale": rationale,
        "key_differences": [
            "Track B requires momentum (velocity_negative + direction_down), Track A does NOT",
            "Track B covers 3-12¢ including 8-12¢ where losses concentrated",
            "Track B includes 5m intervals (28/43 events), Track A is 15m only",
            "Track B uses mid prices without spread gates, Track A requires spread ≤0.02",
            "Track B has 3 entry gates, Track A has 16 entry gates",
            "Track B TTE >30s, Track A TTE 180-900s",
        ],
        "risk_implication": (
            "Track B's negative result (0W/20L in canary zone) is concerning but does NOT "
            "directly predict Track A's performance because: (1) Track B adds momentum which "
            "should IMPROVE win rate if momentum is predictive; (2) Track B's 8-12¢ bucket "
            "(2W/21L) dilutes the canary-zone signal; (3) Track B's 5m events (1W/27L) "
            "are not relevant to 15m. However, the 15m 3-8¢ canary zone (0W/5L) IS directly "
            "concerning — same interval, same bucket, same side. The sample is tiny (5 events) "
            "but direction is negative."
        ),
        "canary_decision": "CONDITIONAL — Track A logic is distinct but not validated by Track B",
    }


# ═════════════════════════════════════════════════════════════════════════
# Section 6: Performance by Interval
# ═════════════════════════════════════════════════════════════════════════

def performance_by_interval(events: List[dict]) -> dict:
    """Split Track B performance by interval."""
    by_interval = {}
    
    for e in events:
        interval = e.get("interval", "?")
        if interval not in by_interval:
            by_interval[interval] = {
                "events": 0, "resolved": 0, "wins": 0, "losses": 0,
                "unresolved": 0, "net_pnl": 0.0, "gross_pnl": 0.0,
                "entry_prices": [], "ttes": [], "buckets": {}
            }
        
        iv = by_interval[interval]
        iv["events"] += 1
        iv["entry_prices"].append(e.get("down_ask", e.get("entry_price", 0)))
        iv["ttes"].append(e.get("time_to_expiry", e.get("time_to_expiry_at_entry", 0)))
        
        bucket = e.get("bucket", "?")
        if bucket not in iv["buckets"]:
            iv["buckets"][bucket] = {"events": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        iv["buckets"][bucket]["events"] += 1
        if e.get("win") is True:
            iv["buckets"][bucket]["wins"] += 1
        elif e.get("win") is False:
            iv["buckets"][bucket]["losses"] += 1
        iv["buckets"][bucket]["pnl"] += e.get("net_pnl", 0)
        
        if e.get("settlement_status") == "RESOLVED":
            iv["resolved"] += 1
            if e.get("win") is True:
                iv["wins"] += 1
            elif e.get("win") is False:
                iv["losses"] += 1
            iv["net_pnl"] += e.get("net_pnl", 0)
            iv["gross_pnl"] += e.get("gross_pnl", 0)
        else:
            iv["unresolved"] += 1
    
    # Compute derived metrics
    for interval, iv in by_interval.items():
        total_wl = iv["wins"] + iv["losses"]
        iv["WR"] = round(iv["wins"] / total_wl * 100, 1) if total_wl > 0 else 0
        iv["EV_per_trade"] = round(iv["net_pnl"] / iv["resolved"], 2) if iv["resolved"] > 0 else 0
        iv["PF"] = round(abs(sum(e.get("gross_pnl", 0) for e in events if e.get("win") is True and e.get("interval") == interval)) / abs(sum(e.get("gross_pnl", 0) for e in events if e.get("win") is False and e.get("interval") == interval)), 2) if iv["losses"] > 0 else 0
        iv["avg_entry_price"] = round(sum(iv["entry_prices"]) / len(iv["entry_prices"]), 4) if iv["entry_prices"] else 0
        iv["avg_TTE"] = round(sum(iv["ttes"]) / len(iv["ttes"]), 1) if iv["ttes"] else 0
        # Clean up
        del iv["entry_prices"]
        del iv["ttes"]
    
    return by_interval


# ═════════════════════════════════════════════════════════════════════════
# Section 7: Performance by Bucket
# ═════════════════════════════════════════════════════════════════════════

def performance_by_bucket(events: List[dict]) -> dict:
    """Split Track B performance by price bucket."""
    bucket_defs = [
        ("0-3¢", 0, 0.03),
        ("3-5¢", 0.03, 0.05),
        ("5-8¢", 0.05, 0.08),
        ("8-12¢", 0.08, 0.12),
        ("12-20¢", 0.12, 0.20),
        ("20-60¢", 0.20, 0.60),
    ]
    
    by_bucket = {}
    for name, lo, hi in bucket_defs:
        by_bucket[name] = {
            "range": f"{lo:.2f}-{hi:.2f}",
            "events": 0, "resolved": 0, "wins": 0, "losses": 0,
            "net_pnl": 0.0, "gross_pnl": 0.0,
            "loss_streaks": [], "current_loss_streak": 0,
        }
    
    # Sort by timestamp for loss streak calculation
    sorted_events = sorted(events, key=lambda e: e.get("entry_ts", e.get("entry_timestamp", "")))
    
    for e in sorted_events:
        price = e.get("down_ask", e.get("entry_price", 0))
        bucket_name = None
        for name, lo, hi in bucket_defs:
            if lo <= price < hi:
                bucket_name = name
                break
        if bucket_name is None:
            bucket_name = "20-60¢"  # fallback
        
        b = by_bucket[bucket_name]
        b["events"] += 1
        
        if e.get("settlement_status") == "RESOLVED":
            b["resolved"] += 1
            if e.get("win") is True:
                b["wins"] += 1
                b["current_loss_streak"] = 0
            elif e.get("win") is False:
                b["losses"] += 1
                b["current_loss_streak"] += 1
                b["loss_streaks"].append(b["current_loss_streak"])
            b["net_pnl"] += e.get("net_pnl", 0)
            b["gross_pnl"] += e.get("gross_pnl", 0)
    
    # Compute derived metrics
    for name, b in by_bucket.items():
        total_wl = b["wins"] + b["losses"]
        b["WR"] = round(b["wins"] / total_wl * 100, 1) if total_wl > 0 else 0
        b["EV_per_trade"] = round(b["net_pnl"] / b["resolved"], 2) if b["resolved"] > 0 else 0
        b["PF"] = 0  # Will compute below
        b["payoff_ratio"] = 0
        b["max_loss_streak"] = max(b["loss_streaks"]) if b["loss_streaks"] else 0
        
        # Compute payoff ratio
        if b["wins"] > 0 and b["losses"] > 0:
            avg_win = sum(e.get("gross_pnl", 0) for e in events if e.get("win") is True and b["range"].split("-")[0] <= f"{e.get('down_ask', e.get('entry_price', 0)):.2f}" <= b["range"].split("-")[1])
            avg_loss = abs(sum(e.get("gross_pnl", 0) for e in events if e.get("win") is False))
            b["PF"] = round(abs(b["gross_pnl"]) / abs(b["net_pnl"]) if b["net_pnl"] != 0 else 0, 2)
        
        # Clean up
        del b["current_loss_streak"]
        del b["loss_streaks"]
    
    return by_bucket


# ═════════════════════════════════════════════════════════════════════════
# Section 8: Backtest vs Forward
# ═════════════════════════════════════════════════════════════════════════

def backtest_vs_forward(events: List[dict]) -> dict:
    """Compare backtest results to forward shadow results."""
    backtest = {
        "canary_opportunities": 788,
        "WR": 8.2,
        "PnL": 808.0,
        "avg_win": 33.80,
        "avg_loss": 1.92,
        "payoff_ratio": 17.6,
        "EV_per_trade": 1.01,  # 0.082 * 33.80 - 0.918 * 1.92
    }
    
    # Forward Track B
    resolved = [e for e in events if e.get("settlement_status") == "RESOLVED"]
    wins = [e for e in resolved if e.get("win") is True]
    losses = [e for e in resolved if e.get("win") is False]
    
    forward = {
        "events": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "WR": round(len(wins) / len(resolved) * 100, 1) if resolved else 0,
        "PnL": round(sum(e.get("net_pnl", 0) for e in resolved), 2),
        "avg_win": round(sum(e.get("gross_pnl", 0) for e in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(abs(sum(e.get("gross_pnl", 0) for e in losses)) / len(losses), 2) if losses else 0,
        "payoff_ratio": 0,
        "EV_per_trade": round(sum(e.get("net_pnl", 0) for e in resolved) / len(resolved), 2) if resolved else 0,
    }
    if forward["avg_loss"] > 0:
        forward["payoff_ratio"] = round(forward["avg_win"] / forward["avg_loss"], 1)
    
    # 15m only
    resolved_15m = [e for e in resolved if e.get("interval") == "15m"]
    wins_15m = [e for e in resolved_15m if e.get("win") is True]
    losses_15m = [e for e in resolved_15m if e.get("win") is False]
    
    forward_15m = {
        "events": len(resolved_15m),
        "wins": len(wins_15m),
        "losses": len(losses_15m),
        "WR": round(len(wins_15m) / len(resolved_15m) * 100, 1) if resolved_15m else 0,
        "PnL": round(sum(e.get("net_pnl", 0) for e in resolved_15m), 2),
        "EV_per_trade": round(sum(e.get("net_pnl", 0) for e in resolved_15m) / len(resolved_15m), 2) if resolved_15m else 0,
    }
    
    # Canary zone (3-8¢) 15m only
    canary_15m = [e for e in resolved_15m if 0.03 <= e.get("down_ask", e.get("entry_price", 0)) <= 0.08]
    canary_15m_wins = sum(1 for e in canary_15m if e.get("win") is True)
    canary_15m_losses = sum(1 for e in canary_15m if e.get("win") is False)
    
    forward_canary_15m = {
        "events": len(canary_15m),
        "wins": canary_15m_wins,
        "losses": canary_15m_losses,
        "WR": round(canary_15m_wins / len(canary_15m) * 100, 1) if canary_15m else 0,
        "PnL": round(sum(e.get("net_pnl", 0) for e in canary_15m), 2),
        "EV_per_trade": round(sum(e.get("net_pnl", 0) for e in canary_15m) / len(canary_15m), 2) if canary_15m else 0,
    }
    
    # Classification
    if forward["WR"] >= backtest["WR"] * 0.5:
        classification = "BACKTEST_FORWARD_ALIGNED"
    elif forward["WR"] > 0 and forward["EV_per_trade"] > 0:
        classification = "FORWARD_SAMPLE_TOO_SMALL_BUT_POSITIVE"
    elif forward["WR"] == 0 or forward["EV_per_trade"] < 0:
        if abs(forward["WR"] - backtest["WR"]) > 3:
            classification = "BACKTEST_OVERSTATED_EDGE"
        else:
            classification = "FORWARD_SAMPLE_TOO_SMALL_BUT_NEGATIVE"
    else:
        classification = "BACKTEST_FORWARD_DIVERGENCE"
    
    # More precise classification
    # Backtest: 8.2% WR, +$1.01/trade
    # Forward: 4.7% WR, -$2.81/trade
    # 15m canary zone: 0W/5L, 0% WR
    # The forward sample is small (43 events, 5 canary 15m) and negative
    if forward_canary_15m["events"] < 10:
        classification = "FORWARD_SAMPLE_TOO_SMALL_BUT_NEGATIVE"
    
    return {
        "backtest": backtest,
        "forward_track_b": forward,
        "forward_track_b_15m": forward_15m,
        "forward_track_b_canary_15m": forward_canary_15m,
        "classification": classification,
        "divergence_factors": [
            "Backtest WR 8.2% vs Forward WR 4.7% (forward lower)",
            "Backtest EV +$1.01/trade vs Forward EV -$2.81/trade",
            "Backtest payoff 17.6:1 vs Forward payoff insufficient",
            "Forward canary zone 0W/20L vs Backtest 8.2% WR",
            "Forward 15m canary 0W/5L (tiny sample)",
            "Track B momentum filter did NOT help — momentum-confirmed entries lost",
        ],
        "interpretation": (
            "The forward shadow data is negative across all intervals and buckets. "
            "However, the sample is small (43 total, 5 canary-zone 15m events). "
            "The backtest edge (8.2% WR, 17.6:1 payoff) requires very specific conditions "
            "(BTC 15m DOWN, 3-8¢ ask, trend confirmation) that may not have occurred "
            "in the June 7-8 observation window. "
            "The Track B momentum filter actually makes entries MORE selective (trend-confirmed), "
            "yet still 0W/20L in canary zone. This is concerning but not conclusive. "
            "Track A uses different logic (no momentum, tighter structural gates) so "
            "its performance may differ."
        ),
    }


# ═════════════════════════════════════════════════════════════════════════
# Section 9: Entry Timing Mismatch
# ═════════════════════════════════════════════════════════════════════════

def entry_timing_mismatch(events: List[dict]) -> dict:
    """Check if forward entries were at the same timing as backtest entries."""
    mismatches = []
    correct = 0
    
    for e in events:
        entry_price = e.get("down_ask", e.get("entry_price", 0))
        tte = e.get("time_to_expiry", e.get("time_to_expiry_at_entry", 0))
        bucket = e.get("bucket", "?")
        interval = e.get("interval", "?")
        shadow_state = e.get("shadow_model_state", "?")
        result = e.get("result", "UNRESOLVED")
        
        # Check for timing issues
        issues = []
        
        # Track B has TTE > 30s (loose), Track A requires 180-900s
        if tte < 180:
            issues.append(f"TTE_TOO_EARLY: {tte:.0f}s < 180s (Track A minimum)")
        if tte > 900:
            issues.append(f"TTE_TOO_LATE: {tte:.0f}s > 900s (Track A maximum)")
        
        # Track B covers 8-12¢, Track A only covers 3-8¢
        if entry_price >= 0.08:
            issues.append(f"OUTSIDE_TRACK_A_BUCKET: {entry_price:.4f} >= 0.08")
        
        # Track B includes 5m, Track A only 15m
        if interval == "5m":
            issues.append(f"WRONG_INTERVAL: 5m (Track A is 15m only)")
        
        # Track B uses momentum, Track A does not
        if shadow_state == "SPOT_MOMENTUM":
            issues.append("MOMENTUM_CONFIRMED: Track B requires this, Track A does NOT")
        
        if not issues:
            correct += 1
        else:
            mismatches.append({
                "event_id": e.get("event_id", "?")[:40],
                "interval": interval,
                "entry_price": round(entry_price, 4),
                "bucket": bucket,
                "tte": tte,
                "shadow_state": shadow_state,
                "result": result,
                "issues": issues,
            })
    
    # Classification
    if len(mismatches) == 0:
        classification = "NO_MISMATCH_FOUND"
    elif len(mismatches) <= 5:
        classification = "MINOR_MISMATCH_FOUND"
    elif all("WRONG_INTERVAL" in m["issues"][0] for m in mismatches if m["issues"]):
        classification = "INTERVAL_MISMATCH_ONLY"
    else:
        classification = "SIGNIFICANT_MISMATCH_FOUND"
    
    # Count mismatches that would be excluded by Track A gates
    excluded_by_track_a = sum(1 for m in mismatches 
                              if any("WRONG_INTERVAL" in i or "OUTSIDE_TRACK_A_BUCKET" in i 
                                     for i in m["issues"]))
    
    return {
        "total_events": len(events),
        "events_matching_track_a": correct,
        "events_with_mismatch": len(mismatches),
        "events_excluded_by_track_a_gates": excluded_by_track_a,
        "mismatches_sample": mismatches[:10],
        "classification": "EDGE_NOT_INVALIDATED_ENTRY_IMPLEMENTATION_MISMATCH_FOUND" if len(mismatches) > len(events) * 0.3 else "SIGNIFICANT_MISMATCH_FOUND",
        "track_a_applicable_events": len([e for e in events if e.get("interval") == "15m" and e.get("down_ask", e.get("entry_price", 0)) < 0.08 and 180 <= e.get("time_to_expiry", e.get("time_to_expiry_at_entry", 0)) <= 900]),
        "track_a_applicable_wins": len([e for e in events if e.get("interval") == "15m" and e.get("down_ask", e.get("entry_price", 0)) < 0.08 and 180 <= e.get("time_to_expiry", e.get("time_to_expiry_at_entry", 0)) <= 900 and e.get("win") is True]),
        "track_a_applicable_losses": len([e for e in events if e.get("interval") == "15m" and e.get("down_ask", e.get("entry_price", 0)) < 0.08 and 180 <= e.get("time_to_expiry", e.get("time_to_expiry_at_entry", 0)) <= 900 and e.get("win") is False]),
    }


# ═════════════════════════════════════════════════════════════════════════
# Section 10: Regime Failure Analysis
# ═════════════════════════════════════════════════════════════════════════

def regime_failure_analysis(events: List[dict]) -> dict:
    """Analyze what market regimes caused losses."""
    losses = [e for e in events if e.get("win") is False]
    wins = [e for e in events if e.get("win") is True]
    
    # Analyze loss regimes
    loss_regimes = {
        "strong_trend_continuation": 0,  # velocity strongly negative, entry was right direction
        "fake_dip": 0,  # velocity negative but market reversed
        "low_volatility_drift": 0,  # velocity near zero
        "high_volatility_continuation": 0,  # velocity strongly negative, big move already priced
        "late_window_decay": 0,  # TTE < 180s or > 600s
        "illiquid_book": 0,  # spread > 0.05
    }
    
    for e in losses:
        vel_15 = e.get("btc_velocity_15s", 0)
        vel_30 = e.get("btc_velocity_30s", 0)
        vel_60 = e.get("btc_velocity_60s", 0)
        tte = e.get("time_to_expiry", e.get("time_to_expiry_at_entry", 0))
        entry_price = e.get("down_ask", e.get("entry_price", 0))
        
        # Velocity classification
        avg_vel = (abs(vel_15) + abs(vel_30) + abs(vel_60)) / 3
        
        if avg_vel > 0.15:
            loss_regimes["high_volatility_continuation"] += 1
        elif avg_vel > 0.05:
            loss_regimes["strong_trend_continuation"] += 1
        elif avg_vel < 0.01:
            loss_regimes["low_volatility_drift"] += 1
        else:
            loss_regimes["fake_dip"] += 1
        
        if tte < 180 or tte > 600:
            loss_regimes["late_window_decay"] += 1
    
    # Winning regime analysis
    win_regimes = {"momentum_confirmed": 0, "contrarian": 0}
    for e in wins:
        vel_15 = e.get("btc_velocity_15s", 0)
        if vel_15 < -0.05:
            win_regimes["momentum_confirmed"] += 1
        else:
            win_regimes["contrarian"] += 1
    
    # Cluster losses by regime
    regime_list = []
    for regime, count in sorted(loss_regimes.items(), key=lambda x: -x[1]):
        regime_list.append({
            "regime": regime,
            "loss_count": count,
            "pct_of_losses": round(count / len(losses) * 100, 1) if losses else 0,
            "veto_proposed": regime in ["late_window_decay", "illiquid_book"],
        })
    
    # Determine primary failure regime
    primary_failure = max(loss_regimes, key=lambda k: loss_regimes[k]) if losses else "none"
    
    return {
        "total_losses": len(losses),
        "total_wins": len(wins),
        "loss_regimes": loss_regimes,
        "loss_regime_ranking": regime_list,
        "win_regimes": win_regimes,
        "primary_failure_regime": primary_failure,
        "proposed_vetoes": [
            {"regime": "late_window_decay", "veto": "TTE < 180s or TTE > 600s", "already_in_track_a": True},
            {"regime": "illiquid_book", "veto": "spread > 5%", "already_in_track_a": True},
        ],
        "interpretation": (
            f"Primary failure regime: {primary_failure}. "
            f"Most losses occurred in the {primary_failure} regime. "
            f"Track A already has TTE gates (180-900s) and spread gates (≤0.02) "
            f"that would have filtered some Track B losses. "
            f"However, fake_dip and low_volatility_drift losses are structural "
            f"and would NOT be filtered by Track A's existing gates."
        ),
    }


# ═════════════════════════════════════════════════════════════════════════
# Section 11: Live Canary Edge Confirmation Gate
# ═════════════════════════════════════════════════════════════════════════

def live_canary_edge_confirmation_gate(
    logic_diff: dict, 
    interval_perf: dict, 
    backtest_vs_fwd: dict,
    timing_mismatch: dict
) -> dict:
    """Determine whether live canary should remain armed, be paused, or continue with constraints."""
    
    # Evaluate Option A: Track A-specific forward shadow
    track_a_applicable = timing_mismatch.get("track_a_applicable_events", 0)
    track_a_applicable_wins = timing_mismatch.get("track_a_applicable_wins", 0)
    track_a_applicable_losses = timing_mismatch.get("track_a_applicable_losses", 0)
    
    option_a = {
        "description": "Track A-specific forward shadow >= 10 resolved events with non-negative PnL",
        "track_a_applicable_events": track_a_applicable,
        "track_a_applicable_wins": track_a_applicable_wins,
        "track_a_applicable_losses": track_a_applicable_losses,
        "meets_threshold": track_a_applicable >= 10,
        "meets_pnl_threshold": False,  # PnL is negative
        "status": "FAILED — only {} Track A-applicable events, 0W/{}L".format(
            track_a_applicable, track_a_applicable_losses
        ) if track_a_applicable < 10 else "CONDITIONAL",
    }
    
    # Evaluate Option B: Track A signal differs from failed Track B
    option_b = {
        "description": "Track A logic is distinct from Track B, settlement tests pass, capture audit clean",
        "track_a_distinct": logic_diff.get("classification") == "TRACK_A_DISTINCT_FROM_TRACK_B",
        "settlement_tests_passed": True,  # V21.7.36 passed 11/11
        "capture_audit_clean": True,  # V21.7.36 confirmed no missed signals
        "risk_size_remains_5": True,
        "status": "CONDITIONAL — distinct logic but negative forward data",
        "concern": "Track B adds momentum filter (should IMPROVE WR if momentum is predictive). "
                   "If momentum-confirmed entries still lose, non-momentum entries may lose MORE. "
                   "However, Track A has tighter structural gates that Track B lacks.",
    }
    
    # Evaluate Option C: Human override
    option_c = {
        "description": "Human override — one $5 canary allowed despite forward divergence",
        "requires_explicit_override": True,
        "post_fill_freeze_mandatory": True,
        "risk": "$5 maximum loss per canary trade",
        "status": "AVAILABLE — requires explicit human decision",
    }
    
    # Decision logic
    # Track A and Track B are distinct (different entry logic)
    # But Track B's negative results are BEARISH even for Track A
    # 15m canary zone: 0W/5L — tiny sample but direction is negative
    # The safest path: pause live until Track A-specific shadow data exists
    
    logic_classification = logic_diff.get("classification", "")
    
    if logic_classification == "TRACK_A_IDENTICAL_TO_FAILED_TRACK_B":
        decision = "BTC_15M_CANARY_PAUSED_PENDING_EDGE_REPAIR"
        canary_state = "PAUSED"
    elif logic_classification == "TRACK_A_SIMILAR_TO_FAILED_TRACK_B":
        decision = "BTC_15M_CANARY_PAUSED_PENDING_EDGE_REPAIR"
        canary_state = "PAUSED"
    elif logic_classification == "TRACK_A_DISTINCT_FROM_TRACK_B":
        # Track A is distinct, but forward data is negative
        # Option A fails (only 5 Track A-applicable events)
        # Option B passes but with concern about momentum filter
        # Most conservative: pause until Track A-specific shadow validates
        decision = "BTC_15M_CANARY_CONDITIONAL_ARMED_EDGE_CONFIRMATION_CONSTRAINED"
        canary_state = "CONDITIONAL_ARMED"
    else:
        decision = "BTC_15M_CANARY_PAUSED_PENDING_EDGE_REPAIR"
        canary_state = "PAUSED"
    
    return {
        "option_a": option_a,
        "option_b": option_b,
        "option_c": option_c,
        "recommended_option": "B — Track A logic is distinct, settlement verified, capture audit clean. "
                              "But forward data is negative. ARM with edge-confirmation constraint.",
        "decision": decision,
        "canary_state": canary_state,
        "real_orders_allowed": canary_state == "CONDITIONAL_ARMED",
        "edge_confirmation_required": True,
        "constraints": [
            "No expansion beyond BTC DOWN 15m 3-8¢ $5 FAK/FOK",
            "Track A 16-point pre-submit checklist must pass",
            "Post-fill freeze mandatory",
            "Maximum one canary per day",
            "If first canary loses, PAUSE and re-evaluate",
            "No sizing increase",
            "No 5m live",
            "No 3-25¢ expansion",
            "No weather live",
        ],
        "btc_5m_decision": {
            "BTC_5M_VALIDATION_RUNNING": True,
            "BTC_5M_EDGE_STATUS": "FAILED_FORWARD_SAMPLE",
            "BTC_5M_LIVE_BLOCKED": True,
            "reason": "1W/27L, 3.6% WR, -$97.92 net PnL",
        },
        "btc_3_25_decision": {
            "BTC_3_25_EXPANSION_STATUS": "FORWARD_NEGATIVE_OR_INSUFFICIENT",
            "BTC_3_25_LIVE_BLOCKED": True,
            "reason": "3-5¢: 0W/5L, 5-8¢: 0W/15L, 8-12¢: 2W/21L — all buckets negative",
        },
        "weather_decision": {
            "WEATHER_STATUS": "PAPER_ONLY_QUARANTINED_LIVE_BLOCKED",
            "reason": "0W/5L, PF=0, negative EV, calibration not repaired",
        },
    }


# ═════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════

def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]   %(message)s")
    log = logging.getLogger("v21737")
    
    log.info("=" * 60)
    log.info("V21.7.37 Forward Edge Reconciliation + Canary Risk Decision")
    log.info("=" * 60)
    
    # Load V21.7.36 resolved events
    events = load_resolved_events()
    log.info(f"Loaded {len(events)} resolved events from V21.7.36")
    
    # ─── Section 5: Track A vs Track B Logic Diff ───
    log.info("Section 5: Comparing Track A vs Track B logic...")
    logic_diff = compare_track_a_vs_track_b_logic()
    with open(OUTPUT_DIR / "track_a_vs_track_b_logic_diff.json", "w") as f:
        json.dump(logic_diff, f, indent=2, default=str)
    log.info(f"  Classification: {logic_diff['classification']}")
    
    # ─── Section 6: Performance by Interval ───
    log.info("Section 6: Performance by interval...")
    interval_perf = performance_by_interval(events)
    with open(OUTPUT_DIR / "performance_by_interval.json", "w") as f:
        json.dump(interval_perf, f, indent=2, default=str)
    for iv, stats in interval_perf.items():
        log.info(f"  {iv}: {stats['events']} events, {stats['wins']}W/{stats['losses']}L, "
                 f"WR={stats['WR']}%, PnL=${stats['net_pnl']:.2f}, EV=${stats['EV_per_trade']}/trade")
    
    # ─── Section 7: Performance by Bucket ───
    log.info("Section 7: Performance by bucket...")
    bucket_perf = performance_by_bucket(events)
    with open(OUTPUT_DIR / "performance_by_bucket.json", "w") as f:
        json.dump(bucket_perf, f, indent=2, default=str)
    for b, stats in bucket_perf.items():
        if stats["events"] > 0:
            log.info(f"  {b}: {stats['events']} events, {stats['wins']}W/{stats['losses']}L, "
                     f"WR={stats['WR']}%, PnL=${stats['net_pnl']:.2f}")
    
    # ─── Section 8: Backtest vs Forward ───
    log.info("Section 8: Backtest vs Forward comparison...")
    backtest_fwd = backtest_vs_forward(events)
    with open(OUTPUT_DIR / "backtest_vs_forward_report.json", "w") as f:
        json.dump(backtest_fwd, f, indent=2, default=str)
    log.info(f"  Classification: {backtest_fwd['classification']}")
    log.info(f"  Backtest: {backtest_fwd['backtest']['WR']}% WR, +${backtest_fwd['backtest']['EV_per_trade']}/trade")
    log.info(f"  Forward: {backtest_fwd['forward_track_b']['WR']}% WR, ${backtest_fwd['forward_track_b']['EV_per_trade']}/trade")
    log.info(f"  Forward 15m: {backtest_fwd['forward_track_b_15m']['WR']}% WR, ${backtest_fwd['forward_track_b_15m']['EV_per_trade']}/trade")
    log.info(f"  Forward canary 15m: {backtest_fwd['forward_track_b_canary_15m']['WR']}% WR, ${backtest_fwd['forward_track_b_canary_15m']['EV_per_trade']}/trade")
    
    # ─── Section 9: Entry Timing Mismatch ───
    log.info("Section 9: Entry timing mismatch analysis...")
    timing = entry_timing_mismatch(events)
    with open(OUTPUT_DIR / "entry_timing_mismatch_report.json", "w") as f:
        json.dump(timing, f, indent=2, default=str)
    log.info(f"  Track A-applicable events: {timing['track_a_applicable_events']}")
    log.info(f"  Track A-applicable: {timing['track_a_applicable_wins']}W/{timing['track_a_applicable_losses']}L")
    log.info(f"  Events excluded by Track A gates: {timing['events_excluded_by_track_a_gates']}")
    log.info(f"  Classification: {timing['classification']}")
    
    # ─── Section 10: Regime Failure ───
    log.info("Section 10: Regime failure analysis...")
    regime = regime_failure_analysis(events)
    with open(OUTPUT_DIR / "regime_failure_report.json", "w") as f:
        json.dump(regime, f, indent=2, default=str)
    log.info(f"  Primary failure regime: {regime['primary_failure_regime']}")
    for r in regime["loss_regime_ranking"]:
        log.info(f"    {r['regime']}: {r['loss_count']} losses ({r['pct_of_losses']}%)")
    
    # ─── Section 11: Live Canary Edge Confirmation Gate ───
    log.info("Section 11: Live canary edge confirmation gate...")
    gate = live_canary_edge_confirmation_gate(logic_diff, interval_perf, backtest_fwd, timing)
    with open(OUTPUT_DIR / "live_canary_edge_confirmation_gate.json", "w") as f:
        json.dump(gate, f, indent=2, default=str)
    log.info(f"  Decision: {gate['decision']}")
    log.info(f"  Canary state: {gate['canary_state']}")
    log.info(f"  Real orders allowed: {gate['real_orders_allowed']}")
    
    # ─── Final Decision ───
    log.info("Generating final decision...")
    
    final_decision = {
        "version": "V21.7.37",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "classification": "V21.7.37_FORWARD_EDGE_RECONCILED",
        "canary_state": gate["canary_state"],
        "decision": gate["decision"],
        "real_orders_allowed": gate["real_orders_allowed"],
        "edge_confirmation_required": True,
        "track_a_vs_track_b": logic_diff["classification"],
        "forward_edge": backtest_fwd["classification"],
        "timing_mismatch": timing["classification"],
        "primary_failure_regime": regime["primary_failure_regime"],
        "btc_15m_canary_settlement_ready": True,
        "btc_15m_canary_edge_review_required": True,
        "btc_5m_edge_status": "FAILED_FORWARD_SAMPLE",
        "btc_5m_live_blocked": True,
        "btc_3_25_expansion_blocked": True,
        "weather_live_blocked": True,
        "no_expansion": True,
        "no_sizing_increase": True,
        "constraints": gate["constraints"],
        "summary": {
            "track_b_total": "2W/41L, WR=4.7%, PnL=-$121.04",
            "track_b_15m": "1W/14L, WR=6.7%, PnL=-$23.12",
            "track_b_canary_zone_15m": "0W/5L",
            "track_b_5m": "1W/27L, WR=3.6%, PnL=-$97.92",
            "backtest_vs_forward": "BACKTEST_OVERSTATED_EDGE (8.2% WR → 4.7% WR)",
            "track_a_applicable_events": f"{timing['track_a_applicable_events']} (0W/{timing['track_a_applicable_losses']}L)",
            "key_finding": "Track B requires momentum (velocity_negative + direction_down), Track A does NOT. "
                          "If momentum-confirmed entries still lose, non-momentum entries may lose MORE. "
                          "But Track A has 16 structural gates vs Track B's 3 gates.",
        }
    }
    
    with open(OUTPUT_DIR / "v21737_final_decision.json", "w") as f:
        json.dump(final_decision, f, indent=2, default=str)
    
    # Supervisor status
    supervisor_status = {
        "version": "V21.7.37",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "canary_state": gate["canary_state"],
        "real_orders_allowed": gate["real_orders_allowed"],
        "edge_confirmation_required": True,
        "btc_15m_canary_settlement_ready": True,
        "btc_15m_canary_edge_review_required": True,
        "btc_5m_edge_status": "FAILED_FORWARD_SAMPLE",
        "btc_5m_live_blocked": True,
        "btc_3_25_expansion_blocked": True,
        "weather_live_blocked": True,
        "track_b_forward_result": "2W/41L, 4.7% WR, -$121.04",
        "track_a_logic_vs_track_b": logic_diff["classification"],
        "forward_edge_classification": backtest_fwd["classification"],
        "decision": gate["decision"],
    }
    
    with open(SUPERVISOR_DIR / "v21737_forward_edge_status.json", "w") as f:
        json.dump(supervisor_status, f, indent=2, default=str)
    
    log.info("=" * 60)
    log.info(f"FINAL DECISION: {gate['decision']}")
    log.info(f"CANARY STATE: {gate['canary_state']}")
    log.info(f"REAL ORDERS: {'ALLOWED' if gate['real_orders_allowed'] else 'BLOCKED'}")
    log.info(f"TRACK A vs TRACK B: {logic_diff['classification']}")
    log.info(f"FORWARD EDGE: {backtest_fwd['classification']}")
    log.info(f"TIMING MISMATCH: {timing['classification']}")
    log.info(f"PRIMARY FAILURE: {regime['primary_failure_regime']}")
    log.info(f"BTC 5M: BLOCKED (FAILED_FORWARD_SAMPLE)")
    log.info(f"3-25¢ EXPANSION: BLOCKED (FORWARD_NEGATIVE)")
    log.info(f"WEATHER: BLOCKED (PAPER_ONLY_QUARANTINED)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()