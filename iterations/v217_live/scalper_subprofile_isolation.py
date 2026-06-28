#!/usr/bin/env python3
"""
V21.7.7 Scalper Subprofile Isolation
====================================
Read V21.7.6 shadow data, slice by asset/side/bucket/lag/spread/TTE/hold/exit,
find any narrow subprofile that clears exit_success>=85%, PF>=1.50, EV>0.

SHADOW ONLY. NO DEPLOYMENT.
"""

import json, gc
from pathlib import Path
from collections import defaultdict
import numpy as np

BASE = Path("/home/naq1987s/father-daddy-capital")
IN_DIR = BASE / "output" / "v2176_scalper"
OUT_DIR = BASE / "output" / "v2177_scalper_subprofiles"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BUCKET_CUTS = [
    ("03_05", 0.03, 0.05),
    ("05_08", 0.05, 0.08),
    ("08_12", 0.08, 0.12),
    ("12_20", 0.12, 0.20),
    ("20_35", 0.20, 0.35),
    ("35_50", 0.35, 0.50),
]
TTE_CUTS = [
    ("60_90", 60, 90),
    ("90_180", 90, 180),
    ("180_300", 180, 300),
]
HOLD_CUTS = [
    ("0_15s", 0, 15),
    ("15_30s", 15, 30),
    ("30_60s", 30, 60),
    ("60s+", 60, 9999),
]
SPREAD_BANDS = [
    ("tight", 0, 0.015),
    ("medium", 0.015, 0.025),
    ("wide", 0.025, 0.03),
]
QUOTE_AGE_BANDS = [
    ("q0_500", 0, 500),
    ("q500_1000", 500, 1000),
    ("q1000_1500", 1000, 1500),
]


def load_jsonl(path):
    records = []
    with open(path) as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records


def classify_bucket(price):
    for label, lo, hi in BUCKET_CUTS:
        if lo <= price < hi:
            return label
    return "other"


def classify_tte(tte):
    for label, lo, hi in TTE_CUTS:
        if lo <= tte < hi:
            return label
    return "other"


def classify_hold(hold_s):
    for label, lo, hi in HOLD_CUTS:
        if lo <= hold_s < hi:
            return label
    return "other"


def classify_spread(spread):
    for label, lo, hi in SPREAD_BANDS:
        if lo <= spread < hi:
            return label
    return "other"


def classify_quote_age(qa):
    for label, lo, hi in QUOTE_AGE_BANDS:
        if lo <= qa < hi:
            return label
    return "other"


def compute_metrics(records, label):
    """Compute all §7 metrics for a set of joined event+exit records."""
    if len(records) < 10:
        return None

    n = len(records)
    exit_successes = sum(1 for r in records if r.get("exit_success"))
    exit_success_rate = exit_successes / n * 100

    pnls = [r.get("slippage_adjusted_pnl_unit", 0) for r in records]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr = len(wins) / n * 100 if n > 0 else 0
    gross_pnl = sum(pnls)
    ev = gross_pnl / n if n > 0 else 0
    gp = sum(wins)
    gl = abs(sum(losses))
    pf = gp / gl if gl > 0.01 else 999.0

    holds = [r.get("hold_seconds", 0) for r in records]
    avg_hold = np.mean(holds) if holds else 0
    med_hold = np.median(holds) if holds else 0

    # Exit reason rates
    reasons = defaultdict(int)
    for r in records:
        reasons[r.get("exit_reason", "unknown")] += 1
    sl_rate = reasons.get("stop_loss", 0) / n * 100
    to_rate = reasons.get("timeout", 0) / n * 100
    fe_rate = reasons.get("pre_expiry_forced", 0) / n * 100
    tp_rate = reasons.get("take_profit", 0) / n * 100

    # Max loss streak
    streak = max_streak = 0
    for p in pnls:
        if p <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # Settlement errors
    settle_errs = sum(1 for r in records if r.get("settlement_error"))

    # Quote age violations
    qa_violations = sum(1 for r in records if r.get("quote_age_ms", 0) > 1500)

    return dict(
        subprofile=label,
        event_count=n,
        exit_count=n,
        exit_success_rate=round(exit_success_rate, 2),
        wins=len(wins),
        losses=len(losses),
        WR=round(wr, 2),
        gross_PnL=round(gross_pnl, 4),
        slippage_adjusted_PnL=round(gross_pnl, 4),
        EV_per_trade=round(ev, 4),
        PF=round(pf, 4),
        max_loss_streak=max_streak,
        avg_hold_seconds=round(float(avg_hold), 2),
        median_hold_seconds=round(float(med_hold), 2),
        stop_loss_rate=round(sl_rate, 2),
        timeout_rate=round(to_rate, 2),
        forced_exit_rate=round(fe_rate, 2),
        take_profit_rate=round(tp_rate, 2),
        settlement_errors=settle_errs,
        quote_age_violations=qa_violations,
    )


def passes_criteria(m):
    """§8 pass criteria."""
    if m is None:
        return False
    return (m["event_count"] >= 100
            and m["exit_success_rate"] >= 85.0
            and m["EV_per_trade"] > 0
            and m["PF"] >= 1.50
            and m["settlement_errors"] == 0
            and m["quote_age_violations"] == 0
            and m["timeout_rate"] < 20.0
            and m["stop_loss_rate"] < 25.0)


def run():
    print("Loading events...")
    events = load_jsonl(IN_DIR / "scalper_shadow_events.jsonl")
    print(f"  {len(events)} events")
    print("Loading exits...")
    exits = load_jsonl(IN_DIR / "scalper_exits.jsonl")
    print(f"  {len(exits)} exits")
    print("Loading settlements...")
    settles = load_jsonl(IN_DIR / "scalper_settlements.jsonl")
    print(f"  {len(settles)} settlements")

    # Join by event_id
    exit_map = {e["event_id"]: e for e in exits}
    settle_map = {s["event_id"]: s for s in settles}

    joined = []
    for ev in events:
        eid = ev["event_id"]
        ex = exit_map.get(eid, {})
        st = settle_map.get(eid, {})
        rec = {**ev, **ex, **st}
        # Ensure key fields exist
        rec.setdefault("exit_success", False)
        rec.setdefault("hold_seconds", 0)
        rec.setdefault("exit_reason", "unknown")
        rec.setdefault("slippage_adjusted_pnl_unit", 0)
        rec.setdefault("settlement_error", None)
        joined.append(rec)

    print(f"Joined records: {len(joined)}")

    # ═══════════════════════════════════════════════════════════════
    # SINGLE-DIMENSION CUTS
    # ═══════════════════════════════════════════════════════════════
    grid = []

    # Asset
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        subset = [r for r in joined if r.get("asset") == asset]
        m = compute_metrics(subset, f"asset={asset}")
        if m:
            grid.append(m)

    # Side
    for side in ["DOWN", "UP"]:
        subset = [r for r in joined if r.get("side") == side]
        m = compute_metrics(subset, f"side={side}")
        if m:
            grid.append(m)

    # Bucket
    for blabel, blo, bhi in BUCKET_CUTS:
        subset = [r for r in joined if blo <= r.get("entry_price", 0) < bhi]
        m = compute_metrics(subset, f"bucket={blabel}")
        if m:
            grid.append(m)

    # Lag confirmed
    for lag_val in [True, False]:
        subset = [r for r in joined if r.get("lag_confirmed") == lag_val]
        m = compute_metrics(subset, f"lag={lag_val}")
        if m:
            grid.append(m)

    # Quote age band
    for qlabel, qlo, qhi in QUOTE_AGE_BANDS:
        subset = [r for r in joined if qlo <= r.get("quote_age_ms", 0) < qhi]
        m = compute_metrics(subset, f"quote_age={qlabel}")
        if m:
            grid.append(m)

    # Spread band
    for slabel, slo, shi in SPREAD_BANDS:
        subset = [r for r in joined if slo <= r.get("spread", 0) < shi]
        m = compute_metrics(subset, f"spread={slabel}")
        if m:
            grid.append(m)

    # TTE band
    for tlabel, tlo, thi in TTE_CUTS:
        subset = [r for r in joined if tlo <= r.get("time_to_expiry", 0) < thi]
        m = compute_metrics(subset, f"tte={tlabel}")
        if m:
            grid.append(m)

    # Hold band
    for hlabel, hlo, hhi in HOLD_CUTS:
        subset = [r for r in joined if hlo <= r.get("hold_seconds", 0) < hhi]
        m = compute_metrics(subset, f"hold={hlabel}")
        if m:
            grid.append(m)

    # Exit reason
    for reason in set(r.get("exit_reason", "") for r in joined):
        subset = [r for r in joined if r.get("exit_reason") == reason]
        m = compute_metrics(subset, f"exit_reason={reason}")
        if m:
            grid.append(m)

    # ═══════════════════════════════════════════════════════════════
    # §6 PRIORITY COMBO CUTS
    # ═══════════════════════════════════════════════════════════════
    combo_cuts = [
        # DOWN + lag + bucket
        ("DOWN_lag_bucket_05_08", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.05<=r.get("entry_price",0)<0.08),
        ("DOWN_lag_bucket_08_12", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.08<=r.get("entry_price",0)<0.12),
        ("DOWN_lag_bucket_12_20", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.12<=r.get("entry_price",0)<0.20),
        ("DOWN_lag_bucket_03_05", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.03<=r.get("entry_price",0)<0.05),
        # DOWN + lag + tight quote age
        ("DOWN_lag_q0_500", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and r.get("quote_age_ms",0)<=500),
        ("DOWN_lag_q0_1000", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and r.get("quote_age_ms",0)<=1000),
        # DOWN + lag + tight spread
        ("DOWN_lag_tight_spread", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and r.get("spread",0)<0.015),
        ("DOWN_lag_medium_spread", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and r.get("spread",0)<0.025),
        # DOWN + lag + TTE
        ("DOWN_lag_tte_60_180", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 60<=r.get("time_to_expiry",0)<180),
        ("DOWN_lag_tte_90_180", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 90<=r.get("time_to_expiry",0)<180),
        # DOWN + lag + fast exit
        ("DOWN_lag_exit_0_30s", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and r.get("hold_seconds",0)<30),
        ("DOWN_lag_exit_0_15s", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and r.get("hold_seconds",0)<15),
        # DOWN + lag + bucket + tight quote
        ("DOWN_lag_08_12_q500", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.08<=r.get("entry_price",0)<0.12 and r.get("quote_age_ms",0)<=500),
        ("DOWN_lag_05_08_q500", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.05<=r.get("entry_price",0)<0.08 and r.get("quote_age_ms",0)<=500),
        ("DOWN_lag_12_20_q500", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.12<=r.get("entry_price",0)<0.20 and r.get("quote_age_ms",0)<=500),
        # DOWN + lag + bucket + tight spread
        ("DOWN_lag_08_12_tight", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.08<=r.get("entry_price",0)<0.12 and r.get("spread",0)<0.015),
        ("DOWN_lag_05_08_tight", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.05<=r.get("entry_price",0)<0.08 and r.get("spread",0)<0.015),
        ("DOWN_lag_12_20_tight", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.12<=r.get("entry_price",0)<0.20 and r.get("spread",0)<0.015),
        # DOWN + lag + bucket + TTE
        ("DOWN_lag_08_12_tte60_180", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.08<=r.get("entry_price",0)<0.12 and 60<=r.get("time_to_expiry",0)<180),
        ("DOWN_lag_05_08_tte60_180", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.05<=r.get("entry_price",0)<0.08 and 60<=r.get("time_to_expiry",0)<180),
        # DOWN + lag + fast exit + bucket
        ("DOWN_lag_08_12_exit30", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.08<=r.get("entry_price",0)<0.12 and r.get("hold_seconds",0)<30),
        ("DOWN_lag_05_08_exit30", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.05<=r.get("entry_price",0)<0.08 and r.get("hold_seconds",0)<30),
        # UP diagnostic
        ("UP_lag_any", lambda r: r.get("side")=="UP" and r.get("lag_confirmed")),
        # Non-lag DOWN for comparison
        ("DOWN_nolag_05_08", lambda r: r.get("side")=="DOWN" and not r.get("lag_confirmed") and 0.05<=r.get("entry_price",0)<0.08),
        ("DOWN_nolag_08_12", lambda r: r.get("side")=="DOWN" and not r.get("lag_confirmed") and 0.08<=r.get("entry_price",0)<0.12),
        # Triple combo: DOWN + lag + bucket + quote + TTE
        ("DOWN_lag_08_12_q500_tte60_180", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.08<=r.get("entry_price",0)<0.12 and r.get("quote_age_ms",0)<=500 and 60<=r.get("time_to_expiry",0)<180),
        ("DOWN_lag_05_08_q500_tte60_180", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.05<=r.get("entry_price",0)<0.08 and r.get("quote_age_ms",0)<=500 and 60<=r.get("time_to_expiry",0)<180),
        ("DOWN_lag_12_20_q500_tte60_180", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.12<=r.get("entry_price",0)<0.20 and r.get("quote_age_ms",0)<=500 and 60<=r.get("time_to_expiry",0)<180),
        # Quadruple: + tight spread
        ("DOWN_lag_08_12_q500_tte60_180_tight", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.08<=r.get("entry_price",0)<0.12 and r.get("quote_age_ms",0)<=500 and 60<=r.get("time_to_expiry",0)<180 and r.get("spread",0)<0.015),
        ("DOWN_lag_05_08_q500_tte60_180_tight", lambda r: r.get("side")=="DOWN" and r.get("lag_confirmed") and 0.05<=r.get("entry_price",0)<0.08 and r.get("quote_age_ms",0)<=500 and 60<=r.get("time_to_expiry",0)<180 and r.get("spread",0)<0.015),
    ]

    for label, pred in combo_cuts:
        subset = [r for r in joined if pred(r)]
        m = compute_metrics(subset, label)
        if m:
            grid.append(m)

    # ═══════════════════════════════════════════════════════════════
    # CLASSIFY
    # ═══════════════════════════════════════════════════════════════
    candidates = [m for m in grid if passes_criteria(m)]
    rejected = [m for m in grid if not passes_criteria(m)]

    # Sort candidates by EV desc
    candidates.sort(key=lambda m: m["EV_per_trade"], reverse=True)
    rejected.sort(key=lambda m: m["EV_per_trade"], reverse=True)

    # Remove None metrics
    grid = [m for m in grid if m is not None]

    print(f"\nSubprofiles analyzed: {len(grid)}")
    print(f"Candidates (pass §8): {len(candidates)}")
    print(f"Rejected: {len(rejected)}")

    if candidates:
        print(f"\n{'='*60}")
        print("PASSING SUBPROFILES:")
        print(f"{'='*60}")
        for c in candidates[:10]:
            print(f"  {c['subprofile']}: n={c['event_count']}, exit_rate={c['exit_success_rate']}%, "
                  f"EV={c['EV_per_trade']}, PF={c['PF']}, max_str={c['max_loss_streak']}, "
                  f"SL={c['stop_loss_rate']}%, TO={c['timeout_rate']}%")

    best = candidates[0] if candidates else None

    # ═══════════════════════════════════════════════════════════════
    # REPORTS
    # ═══════════════════════════════════════════════════════════════

    # Grid report
    with open(OUT_DIR / "subprofile_grid_report.json", "w") as f:
        json.dump(grid, f, indent=2, default=str)

    # Top candidates
    with open(OUT_DIR / "top_candidate_subprofiles.json", "w") as f:
        json.dump(candidates, f, indent=2, default=str)

    # Rejected
    with open(OUT_DIR / "rejected_subprofiles.json", "w") as f:
        json.dump(rejected, f, indent=2, default=str)

    # Readiness
    readiness = dict(
        timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        global_scalper_status="SCALPER_REJECTED",
        global_exit_success_rate=72.59,
        best_subprofile_name=best["subprofile"] if best else "NONE",
        best_subprofile_metrics=best if best else {},
        paper_live_candidate_exists=len(candidates) > 0,
        total_subprofiles_analyzed=len(grid),
        passing_subprofiles=len(candidates),
        classification="SCALPER_SUBPROFILE_PAPER_CANDIDATE_FOUND" if candidates else "SCALPER_RESEARCH_REJECTED",
        reason_for_candidate_or_rejection=(
            f"{len(candidates)} subprofiles pass §8 criteria. Best: {best['subprofile']} "
            f"(exit_rate={best['exit_success_rate']}%, EV={best['EV_per_trade']}, PF={best['PF']})"
            if candidates else
            "No subprofile achieves exit_success_rate>=85% AND PF>=1.50 AND EV>0 simultaneously. "
            "Global scalper exit reliability 72.6% cannot be rescued by narrowing parameters. "
            "Simulated spread/quote_age values may overestimate real execution quality."
        ),
        recommended_next_action=(
            "Build paper-live simulator for top subprofile only. $1 positions, max_open=1, no real orders."
            if candidates else
            "Archive scalper cell. Do not continue until real bid/ask feed data exists. "
            "Return focus to convex bot and weather validation."
        ),
    )
    with open(OUT_DIR / "scalper_subprofile_readiness.json", "w") as f:
        json.dump(readiness, f, indent=2, default=str)

    print(f"\n{'='*60}")
    print("V21.7.7 SUBPROFILE ISOLATION — FINAL")
    print(f"{'='*60}")
    print(f"Subprofiles:    {len(grid)}")
    print(f"Candidates:     {len(candidates)}")
    print(f"Classification: {readiness['classification']}")
    if best:
        print(f"Best:           {best['subprofile']}")
        print(f"  n={best['event_count']}, exit_rate={best['exit_success_rate']}%, "
              f"EV={best['EV_per_trade']}, PF={best['PF']}")
    print(f"Output:         {OUT_DIR}/")


if __name__ == "__main__":
    run()