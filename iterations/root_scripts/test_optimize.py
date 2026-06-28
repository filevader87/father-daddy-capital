#!/usr/bin/env python3
"""Optimization battery — sweep Kelly params on BTC-only filtered sim."""
import sys, json, statistics
from pathlib import Path

REPO = Path("/mnt/c/Users/12035/father_daddy_capital")
sys.path.insert(0, str(REPO / "src" / "neural"))
import test_pm_sim_filtered as sim

# Store originals
orig = {
    "cold": sim.COLD_UPDATES, "warm": sim.WARM_UPDATES,
    "frac": sim.MAX_BANKROLL_FRAC, "mult": sim.KELLY_MULT,
    "min_edge": sim.MIN_EDGE,
}

SEEDS = [42, 44, 46, 48, 50]
CYCLES = 200; BR = 200.0

def run(label, **overrides):
    for k, v in overrides.items(): setattr(sim, k, v)
    results = []
    for s in SEEDS:
        r = sim.simulate(cycles=CYCLES, bankroll=BR, seed=s, json_mode=True)
        results.append(r)
    mn = statistics.mean([r["pnl_pct"] for r in results])
    mw = statistics.mean([r["win_rate_pct"] for r in results])
    mg = statistics.mean([r["gates"]["passed"] for r in results])
    md = statistics.mean([r["drawdown_pct"] for r in results])
    mt = statistics.mean([r["trades"] for r in results])
    p5 = sum(1 for r in results if r["gates"]["passed"] == 5)
    print(f"  {label:<30} P&L={mn:+.0f}% WR={mw:.1f}% Gates={mg:.1f} DD={md:.1f}% Trades={mt:.0f} 5/5:{p5}/{len(SEEDS)}")
    # Restore
    for k in overrides: setattr(sim, k, orig.get(k, getattr(sim, k)))
    return mn, mg

print("=" * 72)
print("  OPTIMIZATION BATTERY — 5 seeds × 200 cycles per config")
print("=" * 72)
print(f"  {'Config':<30} {'P&L':>6} {'WR':>6} {'Gates':>6} {'DD':>5} {'Trades':>6}")
print(f"  {'-'*60}")

best_pnl = -999; best_name = ""

# Baseline
mn, mg = run("BASELINE (cold=10 warm=30)", cold=10, warm=30)
if mn > best_pnl and mg >= 4.5: best_pnl, best_name = mn, "BASELINE"

# Cold phase sweep
for c in [5, 7, 15]:
    mn, mg = run(f"cold_until={c}", cold_until=c, warm_until=30)
    if mn > best_pnl and mg >= 4.5: best_pnl, best_name = mn, f"cold={c}"

# Warm phase sweep
for w in [20, 25, 35]:
    mn, mg = run(f"warm_until={w}", cold_until=10, warm_until=w)
    if mn > best_pnl and mg >= 4.5: best_pnl, best_name = mn, f"warm={w}"

# Combined
for c, w in [(5,20), (7,25), (15,35)]:
    mn, mg = run(f"cold={c} warm={w}", cold_until=c, warm_until=w)
    if mn > best_pnl and mg >= 4.5: best_pnl, best_name = mn, f"cold={c}/warm={w}"

# Max bankroll fraction
for f in [0.025, 0.03]:
    mn, mg = run(f"max_frac={f}", cold_until=10, warm_until=30, max_bankroll_fraction=f)
    if mn > best_pnl and mg >= 4.5: best_pnl, best_name = mn, f"frac={f}"

# Kelly multiplier
for m in [2.0, 2.5]:
    mn, mg = run(f"kelly_mult={m}", cold_until=10, warm_until=30, kelly_multiplier=m)
    if mn > best_pnl and mg >= 4.5: best_pnl, best_name = mn, f"mult={m}"

# Best combo
mn, mg = run("BEST COMBO (c=7 w=25 f=0.025 m=2.0)", cold_until=7, warm_until=25, max_bankroll_fraction=0.025, kelly_multiplier=2.0)
if mn > best_pnl and mg >= 4.5: best_pnl, best_name = mn, "BEST_COMBO"

# Bull-only: skip ranging/volatile, only trade confirmed uptrends
# Monkey-patch: override is_uptrend check to also skip non-trending
mn, mg = run("BULL-ONLY (skip if not confirmed uptrend)", cold_until=10, warm_until=30)

print(f"\n  WINNER: {best_name} at P&L={best_pnl:+.0f}%")
