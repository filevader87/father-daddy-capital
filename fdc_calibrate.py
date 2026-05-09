#!/usr/bin/env python3
"""
Father Daddy Capital — Calibration Scorer (Self-Assessment)
============================================================
Compares our paper trading fills against target trader patterns.
No ClickHouse required — works from local trade journals.

Dimensions scored (0–100, higher = closer to target):
  1. Market Mix — BTC vs ETH, 15m vs 1h distribution match
  2. Timing Distribution — seconds-to-end at entry time
  3. Sizing Distribution — share sizes match target tables
  4. Edge Capture — effective edge after fills vs theoretical edge
  5. Fill Rate — % of quoted orders that simulated-fill

Also generates a calibration report with per-dimension breakdown.
"""

import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

OUTPUT = Path("/mnt/c/Users/12035/father_daddy_capital/output")
CALIBRATION_PATH = Path("/mnt/c/Users/12035/father_daddy_capital/output/calibration.json")

# ─── Target Distributions (from polybot observations) ──────────────────────

TARGET_MARKET_MIX = {
    "btc-day": 0.60,
    "eth-day": 0.40,
}

TARGET_TIMING_BUCKETS = {
    "<60s":   0.10,
    "60-180s": 0.15,
    "180-300s": 0.20,
    "300-600s": 0.25,
    "600-900s": 0.15,
    "900-1800s": 0.10,
    ">=1800s": 0.05,
}

TARGET_SHARE_SIZES = {
    "btc-day": {5: 0.05, 8: 0.10, 10: 0.15, 12: 0.15,
                14: 0.20, 16: 0.15, 18: 0.10, 20: 0.10},
    "eth-day": {5: 0.10, 7: 0.15, 9: 0.15, 11: 0.20,
                13: 0.15, 15: 0.15, 17: 0.05, 20: 0.05},
}

# ─── L1 Distance ────────────────────────────────────────────────────────────

def l1_distance(a: dict, b: dict) -> float:
    """L1 distance between two normalized distributions."""
    keys = set(a.keys()) | set(b.keys())
    return sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)


def normalize_counts(counts: dict) -> dict:
    """Convert raw counts to normalized distribution."""
    total = sum(counts.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in counts.items()}


# ─── Data Extraction ────────────────────────────────────────────────────────

def load_arb_state() -> dict | None:
    """Load arb engine state for calibration."""
    path = OUTPUT / "arb_state.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def extract_arb_fills(state: dict) -> list[dict]:
    """Extract arb fills from journal for scoring."""
    journal = state.get("journal", [])
    fills = []
    for entry in journal:
        # We want individual fill events, not settlements
        if entry.get("type") == "arb_fill":
            fills.append(entry)
    return fills


def extract_arb_positions(state: dict) -> dict:
    """Get current position state for dimension analysis."""
    return state.get("positions", {})


# ─── Dimension Scoring ──────────────────────────────────────────────────────

def score_market_mix(positions: dict) -> dict:
    """Score how well our market mix matches target distribution."""
    series_counts = Counter()
    for pos in positions.values():
        mkt = pos.get("market", {})
        series = mkt.get("series", "other")
        series_counts[series] += 1

    our_mix = normalize_counts(series_counts)

    l1 = l1_distance(our_mix, TARGET_MARKET_MIX)
    score = max(0.0, 100.0 * (1.0 - l1 / 2.0))

    return {
        "dimension": "market_mix",
        "score": round(score, 1),
        "l1_distance": round(l1, 3),
        "our_distribution": our_mix,
        "target_distribution": TARGET_MARKET_MIX,
        "sample_size": len(positions),
    }


def score_timing_distribution(positions: dict) -> dict:
    """Score how our entry timing matches target (seconds-to-end buckets)."""
    now_dt = datetime.now(timezone.utc)
    timing_counts = Counter()

    for pos in positions.values():
        mkt = pos.get("market", {})
        end = mkt.get("end_time")
        if end is None:
            continue

        # Convert string end_time back to datetime if needed
        if isinstance(end, str):
            try:
                end = datetime.fromisoformat(end)
            except ValueError:
                continue

        seconds = (end - now_dt).total_seconds()

        if seconds < 60:
            bucket = "<60s"
        elif seconds < 180:
            bucket = "60-180s"
        elif seconds < 300:
            bucket = "180-300s"
        elif seconds < 600:
            bucket = "300-600s"
        elif seconds < 900:
            bucket = "600-900s"
        elif seconds < 1800:
            bucket = "900-1800s"
        else:
            bucket = ">=1800s"

        timing_counts[bucket] += 1

    our_timing = normalize_counts(timing_counts)
    l1 = l1_distance(our_timing, TARGET_TIMING_BUCKETS)
    score = max(0.0, 100.0 * (1.0 - l1 / 2.0))

    return {
        "dimension": "timing_distribution",
        "score": round(score, 1),
        "l1_distance": round(l1, 3),
        "our_distribution": our_timing,
        "target_distribution": TARGET_TIMING_BUCKETS,
        "sample_size": len(timing_counts),
    }


def score_sizing(positions: dict) -> dict:
    """Score how well our share sizes match target tables per series."""
    series_shares = {}
    for pos in positions.values():
        mkt = pos.get("market", {})
        series = mkt.get("series")
        if not series:
            continue

        orders = pos.get("orders", {})
        for leg, order in orders.items():
            shares = order.get("shares", 0)
            series_shares.setdefault(series, []).append(int(round(shares)))

    # Compare per series
    series_scores = {}
    total_weight = 0
    weighted_score = 0

    for series, sizes in series_shares.items():
        target = TARGET_SHARE_SIZES.get(series)
        if not target:
            continue

        our_counts = Counter(sizes)
        our_norm = normalize_counts(our_counts)
        l1 = l1_distance(our_norm, target)
        s_score = max(0.0, 100.0 * (1.0 - l1 / 2.0))

        weight = len(sizes)
        series_scores[series] = {
            "score": round(s_score, 1),
            "l1": round(l1, 3),
            "sample": len(sizes),
            "our_sizes": our_norm,
        }
        weighted_score += s_score * weight
        total_weight += weight

    overall = round(weighted_score / max(total_weight, 1), 1)

    return {
        "dimension": "share_sizing",
        "score": overall,
        "l1_distance": round(
            sum(s["l1"] for s in series_scores.values()) / max(len(series_scores), 1), 3
        ),
        "by_series": series_scores,
        "total_sample": total_weight,
    }


def score_fill_efficiency(state: dict) -> dict:
    """Score what fraction of quoted orders result in fills."""
    positions = state.get("positions", {})

    total_orders = 0
    filled_orders = 0

    for pos in positions.values():
        inv_up = pos.get("inv_up_shares", 0)
        inv_down = pos.get("inv_down_shares", 0)

        # Count orders placed in journal
        for entry in state.get("journal", []):
            if entry.get("type") == "arb_order":
                total_orders += 1
            if entry.get("type") == "arb_fill":
                filled_orders += 1

    if total_orders == 0:
        fill_rate = 0.0
    else:
        fill_rate = filled_orders / total_orders

    # Target: 60-80% fill rate is healthy for maker strategies
    # Score: 100 if between 50-90%, tapering off outside
    if 0.50 <= fill_rate <= 0.90:
        score = 100.0
    else:
        score = max(0.0, 100.0 - abs(fill_rate - 0.70) * 200)

    return {
        "dimension": "fill_efficiency",
        "score": round(score, 1),
        "fill_rate": round(fill_rate * 100, 1),
        "orders_placed": total_orders,
        "orders_filled": filled_orders,
    }


def score_edge_capture(state: dict) -> dict:
    """Score effective edge captured vs theoretical edge."""
    journal = state.get("journal", [])
    settlements = [e for e in journal if e.get("type") == "arb_settle"]

    if not settlements:
        return {
            "dimension": "edge_capture",
            "score": 50.0,  # neutral if no data
            "theoretical_edge": 0.0,
            "captured_edge": 0.0,
            "settlements": 0,
        }

    total_theoretical = 0.0
    total_captured = 0.0
    count = 0

    for s in settlements:
        sets = s.get("complete_sets", 0)
        if sets == 0:
            continue
        # Theoretical: (1.0 - avg_cost_up - avg_cost_down) per set
        theo_edge = max(0, 1.0 - s.get("avg_cost_up", 0.5) - s.get("avg_cost_down", 0.5))
        captured = s.get("total_pnl", 0)
        invested = s.get("total_invested", 1)
        captured_edge = captured / max(invested, 1)

        total_theoretical += theo_edge * sets
        total_captured += captured
        count += sets

    if count == 0:
        return {
            "dimension": "edge_capture",
            "score": 50.0,
            "theoretical_edge": 0.0,
            "captured_edge": 0.0,
            "settlements": len(settlements),
        }

    avg_theo = total_theoretical / count
    avg_captured = total_captured / max(count, 1)  # not per-share but total

    # Score: higher capture ratio = better
    capture_ratio = avg_captured / max(avg_theo, 0.001)
    score = min(100.0, max(0.0, capture_ratio * 100))

    return {
        "dimension": "edge_capture",
        "score": round(score, 1),
        "theoretical_edge_pct": round(avg_theo * 100, 1),
        "captured_edge_absolute": round(avg_captured, 4),
        "capture_ratio": round(capture_ratio, 2),
        "settlements": len(settlements),
        "complete_sets": count,
    }


# ─── Composite Score ───────────────────────────────────────────────────────

def compute_composite(scores: list[dict]) -> dict:
    """Aggregate dimension scores into composite calibration score."""
    valid_scores = [s for s in scores if "score" in s and s.get("sample_size", 1) > 0]
    if not valid_scores:
        return {"composite_score": 50.0, "dimensions": 0}

    # Weighted: dimensions with more samples count more
    total_weight = sum(s.get("sample_size", 1) for s in valid_scores)
    weighted = sum(
        s["score"] * s.get("sample_size", 1) for s in valid_scores
    )

    composite = round(weighted / max(total_weight, 1), 1)

    return {
        "composite_score": composite,
        "dimensions_scored": len(valid_scores),
        "total_samples": total_weight,
    }


# ─── Calibration Run ────────────────────────────────────────────────────────

def run_calibration() -> dict:
    """Full calibration run — load state, score all dimensions."""
    state = load_arb_state()
    if state is None:
        return {"error": "No arb state found. Run the arb engine first."}

    positions = extract_arb_positions(state)

    dim_scores = [
        score_market_mix(positions),
        score_timing_distribution(positions),
        score_sizing(positions),
        score_fill_efficiency(state),
        score_edge_capture(state),
    ]

    composite = compute_composite(dim_scores)

    report = {
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        **composite,
        "dimensions": dim_scores,
        "state_summary": {
            "bankroll": state.get("bankroll", 0),
            "total_pnl": state.get("total_pnl", 0),
            "active_positions": len(positions),
            "scans": state.get("scans", 0),
        },
    }

    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_PATH.write_text(json.dumps(report, indent=2, default=str))

    return report


def format_report(report: dict) -> str:
    """Pretty-print calibration report."""
    if "error" in report:
        return f"❌ {report['error']}"

    comp = report["composite_score"]
    grade = "🟢" if comp >= 70 else ("🟡" if comp >= 50 else "🔴")

    lines = [
        "",
        "╔══════════════════════════════════════╗",
        "║   FDC ARB CALIBRATION REPORT         ║",
        "╚══════════════════════════════════════╝",
        "",
        f"  {grade} Composite Score: {comp}/100",
        f"     Bankroll: ${report['state_summary']['bankroll']:,.2f}",
        f"     P&L: ${report['state_summary']['total_pnl']:+,.2f}",
        f"     Active: {report['state_summary']['active_positions']} positions",
        f"     Scans: {report['state_summary']['scans']}",
        "",
        "  ── Dimensions ──",
    ]

    for d in report["dimensions"]:
        name = d["dimension"].replace("_", " ").title()
        score = d["score"]
        icon = "🟢" if score >= 70 else ("🟡" if score >= 50 else "🔴")
        lines.append(f"  {icon} {name}: {score}/100")

        if "l1_distance" in d:
            lines.append(f"     L1: {d['l1_distance']}")
        if "fill_rate" in d:
            lines.append(f"     Fill rate: {d['fill_rate']}%")
        if "capture_ratio" in d:
            lines.append(f"     Edge capture: {d['capture_ratio']}x theoretical")
        if "by_series" in d:
            for series, sd in d["by_series"].items():
                lines.append(f"     [{series}] size score: {sd['score']}/100 (n={sd['sample']})")

    lines.append("")
    return "\n".join(lines)


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--watch" in sys.argv or "-w" in sys.argv:
        import time
        print("📐 Calibration Watch Mode — scoring every 5 minutes. Ctrl+C to stop.\n")
        while True:
            try:
                report = run_calibration()
                print(format_report(report))
                time.sleep(300)
            except KeyboardInterrupt:
                print("\n👋 Stopped.")
                break
            except Exception as e:
                print(f"❌ {e}", file=sys.stderr)
                time.sleep(30)
    else:
        report = run_calibration()
        print(format_report(report))
