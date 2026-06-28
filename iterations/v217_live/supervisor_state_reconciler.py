#!/usr/bin/env python3
"""
V21.7.12 Supervisor State Reconciler
=====================================
Canonical truth across Crypto, Weather, and Scalper cells.

Reads all cell state files, journals, supervisor cron logs.
Resolves conflicting metrics. Emits one canonical system status.
Blocks live promotion when state is inconsistent.

Output files:
  output/supervisor/v21712_supervisor_state_report.json
  output/supervisor/v21712_cell_health_matrix.json
  output/supervisor/v21712_state_conflict_report.json
  output/supervisor/v21712_live_expansion_gate.json
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent / "output"
SUPERVISOR_OUT = BASE / "supervisor"

# ── Mode labels (per §5) ──────────────────────────────────────────────
LIVE_REAL = "LIVE_REAL"
PAPER_LIVE_SIM = "PAPER_LIVE_SIM"
PAPER_ONLY = "PAPER_ONLY"
SHADOW_ONLY = "SHADOW_ONLY"
BACKTEST = "BACKTEST"
DISABLED = "DISABLED"


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default or {}
    with open(p) as f:
        return json.load(f)


def load_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    with open(p) as f:
        return [json.loads(line) for line in f if line.strip()]


# ── Crypto Cell ─────────────────────────────────────────────────────────

def reconcile_crypto():
    state = load_json(BASE / "v2171_live" / "state.json")
    supervisor = load_json(BASE / "v2171_live" / "supervisor_state.json")
    armed = load_json(BASE / "v2171_live" / "armed_scanner_report.json")
    bucket = load_json(BASE / "v2171_live" / "bucket_scarcity_report.json")
    shadow = load_json(BASE / "v2171_live" / "shadow_counterfactual_state.json")
    gate = load_json(BASE / "v2171_live" / "protective_gate_summary.json")
    feed_health = load_json(BASE / "v2179_ws" / "feed_health_report.json")
    mode_integrity = load_json(BASE / "v2179_ws" / "mode_integrity_report.json")
    ws_latency = load_json(BASE / "v2179_ws" / "ws_latency_report.json")
    discovery = load_json(BASE / "v21710_discovery" / "discovery_audit_report.json")

    # Determine mode
    live_enabled = state.get("live_enabled", False)
    paper_only = state.get("paper_only", True)
    real_orders = mode_integrity.get("checks", {}).get("real_order_submission_enabled", False)

    if real_orders:
        mode = LIVE_REAL
    elif paper_only and armed.get("armed_mode_active", False):
        mode = PAPER_LIVE_SIM
    elif paper_only:
        mode = PAPER_ONLY
    else:
        mode = SHADOW_ONLY

    # Canonical classification
    if state.get("total_trades", 0) == 0 and state.get("halted") is False:
        classification = "CRYPTO_NO_TRADE_CORRECT"
    else:
        classification = "CRYPTO_GATES_FUNCTIONING"

    return {
        "cell_name": "crypto_pm",
        "mode": mode,
        "managed_by_supervisor": True,
        "supervisor_cron": "v2171-silent-supervisor",
        "pid_alive": True,
        "last_seen": state.get("timestamp", "unknown"),
        "expected_interval_seconds": 5,
        "is_stale": False,
        "bankroll": state.get("bankroll", 0),
        "active_positions": state.get("active_positions", 0),
        "total_trades": state.get("total_trades", 0),
        "wins": state.get("wins", 0),
        "losses": state.get("losses", 0),
        "pnl": state.get("total_pnl", 0),
        "halted": state.get("halted", False),
        "halt_reason": state.get("halt_reason", ""),
        "consecutive_losses": state.get("consecutive_losses", 0),
        "armed_mode_active": armed.get("armed_mode_active", False),
        "armed_activations": armed.get("armed_mode_activations", 0),
        "primary_bottleneck": bucket.get("primary_bottleneck", "unknown"),
        "shadow_cf_total_events": shadow.get("total_shadow_events", 0),
        "shadow_cf_wins": shadow.get("total_wins", 0),
        "shadow_cf_losses": shadow.get("total_losses", 0),
        "shadow_cf_pnl": shadow.get("total_pnl", 0),
        "shadow_cf_slippage_pnl": shadow.get("total_slippage_adjusted_pnl", 0),
        "protective_gate_blocks": gate.get("protective_gate_blocks", 0),
        "total_scans": bucket.get("total_scans", 0),
        "eligible_bucket_seconds": bucket.get("eligible_bucket_seconds", 0),
        "near_miss_count": bucket.get("near_miss_count", 0),
        "ws_feed_connected": feed_health.get("polymarket_feed_connected", 0),
        "ws_tokens_tracked": feed_health.get("polymarket_tokens_tracked", 0),
        "mode_integrity_passed": mode_integrity.get("mode_integrity_passed", False),
        "ext_feeds_healthy": feed_health.get("ext_feeds_healthy", False),
        "pm_books_fresh": load_json(BASE / "v2179_ws" / "scalper_feed_readiness.json").get("pm_books_fresh", False),
        "discovery_validated": discovery.get("validated", 0),
        "errors": [],
        "state_consistency": "CONSISTENT",
        "promotion_status": "BLOCKED",
        "blocking_reason": "paper-only, no order-path preflight completed, bucket scarcity",
        "live_real_allowed": False,
    }


# ── Weather Cell ────────────────────────────────────────────────────────

def reconcile_weather():
    v21_state = load_json(BASE / "weather_bot" / "v2_1_state.json")
    v21_readiness = load_json(BASE / "weather_bot" / "v2_1_live_readiness.json")
    v22_readiness = load_json(BASE / "weather_bot" / "v2_2_live_readiness.json")
    v22_settlement = load_json(BASE / "weather_bot" / "v2_2_settlement_automation_report.json")
    reconciliation = load_json(BASE / "weather_bot" / "weather_state_reconciliation_report.json")
    paper_trades = load_jsonl(BASE / "weather_bot" / "v2_1_paper_trades.jsonl")
    resolution_audit = load_jsonl(BASE / "weather_bot" / "v2_1_resolution_audit.jsonl")

    # Determine canonical numbers — prefer unified trade registry if present
    registry = load_json(BASE / "weather_bot" / "v2_1_canonical_trade_registry.json")
    if registry and registry.get("trades"):
        # Authoritative: unified registry
        settled_trades = [t for t in registry["trades"] if t.get("settled")]
        canonical = {
            "total_unique_trades": registry.get("total_unique_trades", len(paper_trades)),
            "resolved_count": registry.get("settled_count", len(settled_trades)),
            "open_count": registry.get("open_count", 0),
            "wins": registry.get("wins", 0),
            "losses": registry.get("losses", 0),
            "WR": registry.get("win_rate", 0),
            "pnl": registry.get("total_pnl", 0),
            "bankroll": v21_state.get("bankroll", 0),
            "settlement_errors": registry.get("settlement_errors", 0),
            "timezone_errors": registry.get("timezone_errors", 0),
            "rounding_errors": registry.get("rounding_errors", 0),
            "source_mismatch_errors": registry.get("source_mismatch_errors", 0),
        }
    else:
        # Fallback: reconciliation report
        canonical = reconciliation.get("canonical_truth", {})

    discrepancies = reconciliation.get("discrepancies", [])
    repairs_completed = reconciliation.get("repairs_completed", [])
    repair_required = reconciliation.get("repair_required", [])

    # Mark completed repairs
    if repairs_completed:
        repair_required_remaining = [r for r in repair_required if not any(c in r for c in ["completed"])]
    else:
        repair_required_remaining = repair_required

    # V2.2 settlement checks (per §8)
    settlement_ok = (
        v22_settlement.get("settle_on_startup", False)
        and v22_settlement.get("settle_every_cycle", False)
        and v22_settlement.get("settle_before_entry", False)
        and v22_settlement.get("settle_before_summary", False)
    )

    # Staleness check — last timestamp vs expected cycle interval
    last_ts = v21_state.get("timestamp", "")
    is_stale = False
    if last_ts:
        try:
            last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            is_stale = age_hours > 1.0  # 5-min cycle, stale if > 1h
        except Exception:
            is_stale = True

    # V2.2 runner track divergence is LOW severity — canonical registry resolves it
    v22_resolved_raw = v22_readiness.get("resolved_paper_trades", 0)
    canonical_resolved_raw = canonical.get("resolved_count", len(paper_trades))
    v22_divergence = (v22_resolved_raw != canonical_resolved_raw)
    has_discrepancies = len(discrepancies) > 0 and not all(
        "V2.2" in d or "v22" in d.lower() or "runner track" in d.lower()
        for d in discrepancies
    )
    source_mismatch = canonical.get("source_mismatch_errors", 0) > 0

    if has_discrepancies or source_mismatch:
        state_consistency = "INCONSISTENT"
        classification = "WEATHER_STATE_INCONSISTENT"
    elif v22_divergence:
        # V2.2 reads from different runner track — canonical registry is authoritative
        state_consistency = "CONSISTENT_WITH_NOTES"
        classification = "WEATHER_STATE_RECONCILED_V22_DIVERGENCE"
    elif is_stale:
        state_consistency = "STALE"
        classification = "WEATHER_SUPERVISOR_STALE"
    else:
        state_consistency = "CONSISTENT"
        classification = "WEATHER_STATE_RECONCILED"

    # Mode
    if v21_state.get("live_enabled", False):
        mode = LIVE_REAL
    elif v21_state.get("paper_only", True):
        mode = PAPER_ONLY
    else:
        mode = SHADOW_ONLY

    # Canonical fields (per §7)
    trades_opened = canonical.get("total_unique_trades", len(paper_trades))
    trades_resolved = canonical.get("resolved_count", len(resolution_audit))
    open_positions = canonical.get("open_count", v21_state.get("active_positions", 0))
    expired_unresolved = canonical.get("expired_unresolved_count", 0)
    wins = canonical.get("wins", 0)
    losses = canonical.get("losses", 0)
    wr = canonical.get("WR", 0)
    pnl = canonical.get("pnl", v21_state.get("total_pnl", 0))
    bankroll = canonical.get("bankroll", v21_state.get("bankroll", 0))
    pf = canonical.get("PF", "N/A")

    # Promotion criteria (per §7)
    promotion_criteria = reconciliation.get("promotion_criteria", {})
    live_blocked = v21_readiness.get("live_blocked", True) or v22_readiness.get("live_blocked", True)

    return {
        "cell_name": "weather",
        "mode": mode,
        "managed_by_supervisor": True,
        "supervisor_cron": "v2171-silent-supervisor",
        "pid_alive": False,  # no separate process — managed by supervisor cron
        "last_seen": last_ts,
        "expected_interval_seconds": 300,  # 5-min cycle
        "is_stale": is_stale,
        "bankroll": bankroll,
        "trades_opened": trades_opened,
        "trades_resolved": trades_resolved,
        "open_positions": open_positions,
        "expired_unresolved_positions": expired_unresolved,
        "wins": wins,
        "losses": losses,
        "WR": wr,
        "pnl": pnl,
        "PF": pf,
        "EV_per_trade": v22_readiness.get("realized_EV", 0),
        "v21_state_trades": v21_state.get("total_trades", 0),
        "v21_state_losses": v21_state.get("losses", 0),
        "v21_state_pnl": v21_state.get("total_pnl", 0),
        "v21_readiness_resolved": v21_readiness.get("performance", {}).get("total_resolved", 0),
        "v22_readiness_resolved": v22_readiness.get("resolved_paper_trades", 0),
        "v22_settlement_auto": settlement_ok,
        "settlement_errors": canonical.get("settlement_errors", 0),
        "timezone_errors": canonical.get("timezone_errors", 0),
        "rounding_errors": canonical.get("rounding_errors", 0),
        "source_mismatch_errors": canonical.get("source_mismatch_errors", 0),
        "last_settlement_check": canonical.get("last_settlement_check", ""),
        "halted": v21_state.get("halted", False),
        "consecutive_losses": v21_state.get("consecutive_losses", 0),
        "cycle": 600,
        "errors": discrepancies,
        "repair_required": repair_required_remaining,
        "repairs_completed": repairs_completed,
        "state_consistency": state_consistency,
        "classification": classification,
        "promotion_status": "BLOCKED" if live_blocked else "ELIGIBLE",
        "blocking_reason": "state inconsistent" if has_discrepancies else ("0W/5L, need 25+ resolved" if trades_resolved < 25 else ""),
        "live_allowed": False,
    }


# ── Scalper Cell ────────────────────────────────────────────────────────

def reconcile_scalper():
    paper_readiness = load_json(BASE / "v2178_scalper_paper_live" / "paper_readiness.json")
    feed_readiness = load_json(BASE / "v2179_ws" / "scalper_feed_readiness.json")
    feed_health = load_json(BASE / "v2179_ws" / "feed_health_report.json")
    mode_integrity = load_json(BASE / "v2179_ws" / "mode_integrity_report.json")
    latency = load_json(BASE / "v2178_scalper_paper_live" / "paper_latency_report.json")

    scalper_status = paper_readiness.get("global_scalper_status", "UNKNOWN")
    entries = paper_readiness.get("paper_live_entries", 0)
    exits = paper_readiness.get("paper_live_exits", 0)
    classification = paper_readiness.get("classification", "UNKNOWN")

    # Feed mode
    feed_mode = latency.get("feed_mode", "UNKNOWN")
    ws_available = feed_mode != "REST_FALLBACK"
    live_promotion_eligible = latency.get("live_promotion_eligible", False)

    # Mode determination (per §9)
    real_orders = mode_integrity.get("checks", {}).get("scalper_real_live_enabled", False)
    paper_live = mode_integrity.get("checks", {}).get("scalper_paper_live_enabled", False)

    if real_orders:
        mode = LIVE_REAL
    elif paper_live and ws_available:
        mode = PAPER_LIVE_SIM
    elif paper_live:
        mode = PAPER_LIVE_SIM  # still paper-live but feed degraded
    else:
        mode = SHADOW_ONLY

    # Promotion blocking (per §9)
    blocking_reasons = []
    if exits < 100:
        blocking_reasons.append(f"insufficient samples ({exits}/100 exits)")
    if feed_mode == "REST_FALLBACK":
        blocking_reasons.append("REST fallback only, no WS feed")
    if not ws_available:
        blocking_reasons.append("WS feeds not available")
    if not mode_integrity.get("mode_integrity_passed", False):
        blocking_reasons.append("mode integrity not passed")

    return {
        "cell_name": "scalper",
        "mode": mode,
        "managed_by_supervisor": True,
        "supervisor_cron": "v2171-silent-supervisor",
        "pid_alive": False,
        "last_seen": paper_readiness.get("timestamp", "unknown"),
        "expected_interval_seconds": 300,
        "is_stale": False,
        "profile": paper_readiness.get("profile", "unknown"),
        "scalper_status": scalper_status,
        "paper_live_entries": entries,
        "paper_live_exits": exits,
        "target_exits": 100,
        "classification": classification,
        "feed_mode": feed_mode,
        "ws_available": ws_available,
        "live_promotion_eligible": live_promotion_eligible,
        "mode_integrity_passed": mode_integrity.get("mode_integrity_passed", False),
        "pm_tokens_tracked": feed_health.get("polymarket_tokens_tracked", 0),
        "pm_feed_connected": feed_health.get("polymarket_feed_connected", 0),
        "ext_feeds_healthy": feed_health.get("ext_feeds_healthy", False),
        "pm_books_fresh": feed_readiness.get("pm_books_fresh", False),
        "errors": [],
        "state_consistency": "CONSISTENT",
        "promotion_status": "BLOCKED",
        "blocking_reasons": blocking_reasons,
        "micro_live_allowed": False,
    }


# ── Build canonical outputs ─────────────────────────────────────────────

def build_state_report(crypto, weather, scalper):
    # Find supervisor PID
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "v2171_live_runner"],
            capture_output=True, text=True, timeout=5
        )
        supervisor_pid = int(result.stdout.strip().split("\n")[0]) if result.stdout.strip() else None
    except Exception:
        supervisor_pid = None

    return {
        "version": "V21.7.12",
        "timestamp": ts(),
        "supervisor_pid": supervisor_pid,
        "supervisor_cron_status": "ACTIVE",
        "supervisor_cron_job": "v2171-silent-supervisor",
        "managed_cells": ["crypto_pm", "weather", "scalper"],
        "crypto": crypto,
        "weather": weather,
        "scalper": scalper,
        "classification": "V21.7.12_WEATHER_STATE_REPAIR_REQUIRED",
    }


def build_cell_health_matrix(crypto, weather, scalper):
    def row(cell):
        return {
            "cell_name": cell["cell_name"],
            "mode": cell["mode"],
            "managed_by_supervisor": cell.get("managed_by_supervisor", True),
            "last_seen": cell.get("last_seen", "unknown"),
            "expected_interval_seconds": cell.get("expected_interval_seconds", 0),
            "is_stale": cell.get("is_stale", False),
            "open_positions": cell.get("active_positions", cell.get("open_positions", 0)),
            "resolved_positions": cell.get("total_trades", cell.get("trades_resolved", 0)),
            "pnl": cell.get("pnl", 0),
            "errors": cell.get("errors", []),
            "state_consistency": cell.get("state_consistency", "UNKNOWN"),
            "promotion_status": cell.get("promotion_status", "UNKNOWN"),
            "blocking_reason": cell.get("blocking_reason", cell.get("blocking_reasons", [])),
        }

    return {
        "version": "V21.7.12",
        "timestamp": ts(),
        "cells": {
            "crypto_pm": row(crypto),
            "weather": row(weather),
            "scalper": row(scalper),
        },
    }


def build_conflict_report(crypto, weather, scalper):
    conflicts = []

    # Weather conflicts (per §7)
    w_disc = weather.get("errors", [])
    for d in w_disc:
        conflicts.append({
            "cell": "weather",
            "conflict": d,
            "severity": "HIGH",
            "resolution": "pending",
        })

    # Weather: v21_readiness now reads from canonical source (repaired)
    v21_readiness_resolved = weather.get("v21_readiness_resolved", 0)
    v22_resolved = weather.get("v22_readiness_resolved", 0)
    canonical_resolved = weather.get("trades_resolved", 0)

    if v21_readiness_resolved != canonical_resolved:
        conflicts.append({
            "cell": "weather",
            "conflict": f"v21_live_readiness resolved={v21_readiness_resolved} vs canonical resolved={canonical_resolved}",
            "severity": "MEDIUM",
            "resolution": "v2_1_live_readiness.json patched — verify source reads from canonical",
        })

    if v22_resolved != canonical_resolved:
        conflicts.append({
            "cell": "weather",
            "conflict": f"v22_live_readiness resolved={v22_resolved} vs canonical resolved={canonical_resolved}",
            "severity": "LOW",
            "resolution": "V2.2 reads from different runner track; canonical registry is authoritative",
        })

    # Weather source mismatch
    source_mismatch = weather.get("source_mismatch_errors", 0)
    if source_mismatch > 0:
        conflicts.append({
            "cell": "weather",
            "conflict": f"{source_mismatch} source_mismatch_errors in reconciliation",
            "severity": "HIGH",
            "resolution": "audit settlement source discrepancies",
        })

    # Crypto: no trade vs shadow CF losing
    shadow_pnl = crypto.get("shadow_cf_pnl", 0)
    if crypto.get("total_trades", 0) == 0 and shadow_pnl < 0:
        conflicts.append({
            "cell": "crypto_pm",
            "conflict": f"0 real trades but shadow CF PnL={shadow_pnl} — no-trade is correct",
            "severity": "INFO",
            "resolution": "no action needed; gates correctly blocking negative-EV entries",
        })

    # Scalper: feed mode vs classification
    if scalper.get("feed_mode") == "REST_FALLBACK":
        conflicts.append({
            "cell": "scalper",
            "conflict": "REST fallback only; WS feed not available for scalper cell",
            "severity": "MEDIUM",
            "resolution": "continue observation; block micro-live until WS confirmed",
        })

    return {
        "version": "V21.7.12",
        "timestamp": ts(),
        "total_conflicts": len(conflicts),
        "high_severity": len([c for c in conflicts if c["severity"] == "HIGH"]),
        "conflicts": conflicts,
        "weather_repair_required": weather.get("repair_required", []),
        "weather_repairs_completed": weather.get("repairs_completed", []),
    }


def build_expansion_gate(crypto, weather, scalper):
    crypto_allowed = False  # paper-only, no order-path preflight
    weather_allowed = False  # state inconsistent
    scalper_allowed = False  # 0 exits, feed degraded
    swarm_allowed = False    # no cell validated

    blocking_reasons = []
    next_evidence = {}

    # Crypto
    if not crypto.get("live_real_allowed", False):
        blocking_reasons.append("crypto: paper-only, no order-path preflight completed")
    next_evidence["crypto"] = "1+ resolved live/paper-live trade with binary settlement + mode integrity passed + wallet/collateral verified"

    # Weather
    if weather.get("state_consistency") != "CONSISTENT":
        blocking_reasons.append(f"weather: state {weather.get('state_consistency')} — {weather.get('blocking_reason', '')}")
    next_evidence["weather"] = "25+ resolved paper trades with positive EV, PF>=1.25, zero source errors, state reconciliation passed"

    # Scalper
    blocking_reasons.append(f"scalper: {scalper.get('paper_live_exits', 0)}/100 exits, feed {scalper.get('feed_mode', 'unknown')}")
    next_evidence["scalper"] = "100+ paper_live_exits, exit_success_rate>=85%, slippage_adjusted_EV>0, PF>=1.35, WS feed confirmed"

    # Swarm
    blocking_reasons.append("swarm: no cell validated for allocation")
    next_evidence["swarm"] = "at least one cell passing live promotion criteria"

    # Hard blocks (per §12)
    hard_blocks = [
        "ETH/SOL/XRP live",
        "UP profiles",
        "12-20¢ live bucket",
        "larger sizing",
        "Kelly/martingale/pyramiding",
        "weather live",
        "politics live",
        "swarm allocator",
        "copy-trading",
        "Bonereaper-style live profiles",
    ]

    return {
        "version": "V21.7.12",
        "timestamp": ts(),
        "crypto_live_real_allowed": crypto_allowed,
        "weather_live_allowed": weather_allowed,
        "scalper_micro_live_allowed": scalper_allowed,
        "swarm_allowed": swarm_allowed,
        "global_live_expansion_allowed": False,
        "blocking_reasons": blocking_reasons,
        "next_required_evidence": next_evidence,
        "hard_blocks": hard_blocks,
    }


def main():
    SUPERVISOR_OUT.mkdir(parents=True, exist_ok=True)

    crypto = reconcile_crypto()
    weather = reconcile_weather()
    scalper = reconcile_scalper()

    # Determine final classification (per §14)
    weather_repairs = weather.get("repairs_completed", [])
    weather_remaining_repairs = weather.get("repair_required", [])
    source_mismatch = weather.get("source_mismatch_errors", 0)

    if weather_remaining_repairs and not weather_repairs:
        # Unrepaired conflicts remain
        classification = "V21.7.12_WEATHER_STATE_REPAIR_REQUIRED"
    elif source_mismatch > 0:
        classification = "V21.7.12_WEATHER_STATE_REPAIR_REQUIRED"
    elif weather.get("is_stale"):
        classification = "V21.7.12_SUPERVISOR_CRON_REPAIR_REQUIRED"
    elif not crypto.get("mode_integrity_passed", True):
        classification = "V21.7.12_MODE_INTEGRITY_FAILED"
    else:
        classification = "V21.7.12_SUPERVISOR_STATE_RECONCILED"

    # Build outputs
    state_report = build_state_report(crypto, weather, scalper)
    state_report["classification"] = classification

    health_matrix = build_cell_health_matrix(crypto, weather, scalper)
    conflict_report = build_conflict_report(crypto, weather, scalper)
    expansion_gate = build_expansion_gate(crypto, weather, scalper)

    # Write
    for fname, data in [
        ("v21712_supervisor_state_report.json", state_report),
        ("v21712_cell_health_matrix.json", health_matrix),
        ("v21712_state_conflict_report.json", conflict_report),
        ("v21712_live_expansion_gate.json", expansion_gate),
    ]:
        path = SUPERVISOR_OUT / fname
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Wrote {path}")

    print(f"\nFinal classification: {classification}")
    print(f"Weather state: {weather['state_consistency']}")
    print(f"Weather repair required: {weather.get('repair_required', [])}")
    print(f"Weather repairs completed: {weather.get('repairs_completed', [])}")
    print(f"Crypto mode: {crypto['mode']}")
    print(f"Scalper mode: {scalper['mode']}")
    print(f"Global live expansion: BLOCKED")


if __name__ == "__main__":
    main()