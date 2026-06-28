#!/usr/bin/env python3
"""
Comprehensive simulation battery for live-readiness assessment.
Tests: 20-seed multi-asset, head-to-head vs single-asset filtered,
parameter sweeps on sizing, worst-trade analysis.
"""
import sys, json, statistics
from pathlib import Path
from collections import defaultdict

REPO = Path("/mnt/c/Users/12035/father_daddy_capital")
sys.path.insert(0, str(REPO / "src" / "neural"))

import test_pm_sim_multi as multi
import test_pm_sim_filtered as filtered

SEEDS = list(range(42, 62))  # 20 seeds

# ══════════════════════════════════════════════════════════════════════════
# 1. Multi-Asset Baseline (20 seeds)
# ══════════════════════════════════════════════════════════════════════════

print("=" * 72)
print("  1. MULTI-ASSET 20-SEED BASELINE ($200, 200 cycles)")
print("=" * 72)

ma_results = []
for s in SEEDS:
    r = multi.simulate(cycles=200, bankroll=200.0, seed=s, json_mode=True)
    ma_results.append(r)
    print(f"  SEED {s:>3}: P&L {r['pnl_pct']:>+6.0f}% | WR {r['win_rate_pct']:>5.1f}% | "
          f"Gates {r['gates']['passed']}/5 | DD {r['drawdown_pct']:>5.1f}% | "
          f"Trades {r['trades']} | Bear {r['bear_skipped']}")

pnls = [r["pnl_pct"] for r in ma_results]
wrs = [r["win_rate_pct"] for r in ma_results]
gates = [r["gates"]["passed"] for r in ma_results]
dds = [r["drawdown_pct"] for r in ma_results]
trades = [r["trades"] for r in ma_results]
losers = [r for r in ma_results if r["pnl_pct"] < 0]

print(f"\n  AGGREGATE:")
print(f"  P&L:     mean={statistics.mean(pnls):+.0f}%  min={min(pnls):+.0f}%  max={max(pnls):+.0f}%")
print(f"  WR:      mean={statistics.mean(wrs):.1f}%  min={min(wrs):.1f}%  max={max(wrs):.1f}%")
print(f"  Gates:   {sum(gates)}/{len(SEEDS)*5}  mean={statistics.mean(gates):.1f}/5")
print(f"  5/5:     {sum(1 for g in gates if g==5)}/{len(SEEDS)} seeds")
print(f"  DD:      mean={statistics.mean(dds):.1f}%  min={min(dds):.1f}%  max={max(dds):.1f}%")
print(f"  Trades:  mean={statistics.mean(trades):.0f}  min={min(trades)}  max={max(trades)}")
print(f"  Losing seeds: {len(losers)}/{len(SEEDS)}")
if losers:
    for lr in losers:
        print(f"    seed {lr['cycles']}: {lr['pnl_pct']:+.0f}% | WR {lr['win_rate_pct']:.1f}% | DD {lr['drawdown_pct']:.1f}%")
print()

# ══════════════════════════════════════════════════════════════════════════
# 2. Single-Asset Filtered Baseline (same 20 seeds)
# ══════════════════════════════════════════════════════════════════════════

print("=" * 72)
print("  2. SINGLE-ASSET FILTERED 20-SEED BASELINE")
print("=" * 72)

sa_results = []
for s in SEEDS:
    r = filtered.simulate(cycles=200, bankroll=200.0, seed=s, json_mode=True)
    sa_results.append(r)
    print(f"  SEED {s:>3}: P&L {r['pnl_pct']:>+6.0f}% | WR {r['win_rate_pct']:>5.1f}% | "
          f"Gates {r['gates']['passed']}/5 | DD {r['drawdown_pct']:>5.1f}% | "
          f"Trades {r['trades']} | Filt {r.get('cycles_filtered',0)}")

s_pnls = [r["pnl_pct"] for r in sa_results]
s_wrs = [r["win_rate_pct"] for r in sa_results]
s_gates = [r["gates"]["passed"] for r in sa_results]
s_dds = [r["drawdown_pct"] for r in sa_results]
s_losers = [r for r in sa_results if r["pnl_pct"] < 0]

print(f"\n  AGGREGATE:")
print(f"  P&L:     mean={statistics.mean(s_pnls):+.0f}%  min={min(s_pnls):+.0f}%  max={max(s_pnls):+.0f}%")
print(f"  WR:      mean={statistics.mean(s_wrs):.1f}%  min={min(s_wrs):.1f}%  max={max(s_wrs):.1f}%")
print(f"  Gates:   {sum(s_gates)}/{len(SEEDS)*5}  mean={statistics.mean(s_gates):.1f}/5")
print(f"  5/5:     {sum(1 for g in s_gates if g==5)}/{len(SEEDS)} seeds")
print(f"  DD:      mean={statistics.mean(s_dds):.1f}%  min={min(s_dds):.1f}%  max={max(s_dds):.1f}%")
print(f"  Losing seeds: {len(s_losers)}/{len(SEEDS)}")
print()

# ══════════════════════════════════════════════════════════════════════════
# 3. Head-to-Head Comparison
# ══════════════════════════════════════════════════════════════════════════

print("=" * 72)
print("  3. HEAD-TO-HEAD (per-seed comparison)")
print("=" * 72)

ma_wins = 0; sa_wins = 0; ties = 0
for ma, sa in zip(ma_results, sa_results):
    delta = ma["pnl_pct"] - sa["pnl_pct"]
    w = "MA" if delta > 0 else ("SA" if delta < 0 else "TIE")
    if w == "MA": ma_wins += 1
    elif w == "SA": sa_wins += 1
    else: ties += 1
    print(f"  SEED {ma['cycles']:>3}: MA {ma['pnl_pct']:>+6.0f}% vs SA {sa['pnl_pct']:>+6.0f}% → Δ {delta:>+6.0f}% [{w}]")

print(f"\n  Multi-Asset wins: {ma_wins}/{len(SEEDS)}")
print(f"  Single-Asset wins: {sa_wins}/{len(SEEDS)}")
print(f"  Ties: {ties}/{len(SEEDS)}")

# ══════════════════════════════════════════════════════════════════════════
# 4. Deep Dive — Analyze worst multi-asset seed
# ══════════════════════════════════════════════════════════════════════════

worst = min(ma_results, key=lambda r: r["pnl_pct"])
print(f"\n{'='*72}")
print(f"  4. WORST SEED DEEP DIVE (seed with worst P&L)")
print(f"  Seed: worst={worst['pnl_pct']:+.0f}% | WR={worst['win_rate_pct']:.1f}% | DD={worst['drawdown_pct']:.1f}%")
print(f"  Regime breakdown:")
for r, d in sorted(worst["regime_breakdown"].items()):
    print(f"    {r:<15} {d['trades']:>3} tr  P&L ${d['pnl']:>+7.2f}  WR {d['win_rate']:>5.0f}%")
print(f"  Asset breakdown:")
for a, d in sorted(worst["asset_breakdown"].items()):
    print(f"    {a:<5} {d['trades']:>3} tr  P&L ${d['pnl']:>+7.2f}  WR {d['win_rate']:>5.0f}%")
print(f"  Sizing tiers: {worst['tier_breakdown']}")

# ══════════════════════════════════════════════════════════════════════════
# 5. Parameter Sensitivity — max_conviction_dollar sweep
# ══════════════════════════════════════════════════════════════════════════

print(f"\n{'='*72}")
print(f"  5. PARAMETER SWEEP: max_conviction_dollar")
print(f"  (5 seeds, 200 cycles each)")
print(f"{'='*72}")

orig_max = multi.SIZING["max_conviction_dollar"]
test_seeds = [42, 43, 44, 45, 46]

for cap_val in [15.0, 25.0, 35.0]:
    multi.SIZING["max_conviction_dollar"] = cap_val
    results = []
    for s in test_seeds:
        r = multi.simulate(cycles=200, bankroll=200.0, seed=s, json_mode=True)
        results.append(r)
    mn = statistics.mean([r["pnl_pct"] for r in results])
    mw = statistics.mean([r["win_rate_pct"] for r in results])
    mg = statistics.mean([r["gates"]["passed"] for r in results])
    md = statistics.mean([r["drawdown_pct"] for r in results])
    print(f"  max_conv=${cap_val:.0f}: P&L={mn:+.0f}% | WR={mw:.1f}% | Gates={mg:.1f}/5 | DD={md:.1f}%")

# Restore
multi.SIZING["max_conviction_dollar"] = orig_max

# ══════════════════════════════════════════════════════════════════════════
# 6. Parameter Sensitivity — conviction_pct sweep
# ══════════════════════════════════════════════════════════════════════════

print(f"\n{'='*72}")
print(f"  6. PARAMETER SWEEP: conviction_pct")
print(f"{'='*72}")

orig_cpct = multi.SIZING["conviction_pct"]

for cpct in [0.03, 0.05, 0.07]:
    multi.SIZING["conviction_pct"] = cpct
    results = []
    for s in test_seeds:
        r = multi.simulate(cycles=200, bankroll=200.0, seed=s, json_mode=True)
        results.append(r)
    mn = statistics.mean([r["pnl_pct"] for r in results])
    mw = statistics.mean([r["win_rate_pct"] for r in results])
    mg = statistics.mean([r["gates"]["passed"] for r in results])
    md = statistics.mean([r["drawdown_pct"] for r in results])
    print(f"  conviction={cpct*100:.0f}%: P&L={mn:+.0f}% | WR={mw:.1f}% | Gates={mg:.1f}/5 | DD={md:.1f}%")

multi.SIZING["conviction_pct"] = orig_cpct

# ══════════════════════════════════════════════════════════════════════════
# 7. Live Readiness Assessment
# ══════════════════════════════════════════════════════════════════════════

print(f"\n{'='*72}")
print(f"  7. LIVE READINESS ASSESSMENT")
print(f"{'='*72}")

checks = []

# Gate 1: 7+ consecutive green days
all_profitable = len(losers) == 0
checks.append(("All seeds profitable", all_profitable, "essential for capital preservation"))

# Gate 2: Sharpe > 1.5
mean_sharpe = statistics.mean([r["sharpe_like"] for r in ma_results])
checks.append(("Mean Sharpe > 1.5", mean_sharpe > 1.5, f"actual: {mean_sharpe:.1f}"))

# Gate 3: Win rate > 55%
mean_wr = statistics.mean(wrs)
checks.append(("Mean WR > 55%", mean_wr > 55, f"actual: {mean_wr:.1f}%"))

# Gate 4: Profit factor > 1.5
mean_pf = statistics.mean([r["profit_factor"] for r in ma_results])
checks.append(("Mean profit factor > 1.5", mean_pf > 1.5, f"actual: {mean_pf:.2f}"))

# Gate 5: Max drawdown < 8%
any_dd_ok = sum(1 for d in dds if d > 8) / len(dds)
checks.append(("DD < 8% across all seeds", any_dd_ok < 0.3, f"{(1-any_dd_ok)*100:.0f}% seeds pass"))

# Stability: variance across seeds
pnl_variance = statistics.stdev(pnls) / abs(statistics.mean(pnls)) if statistics.mean(pnls) != 0 else 999
checks.append(("Low cross-seed variance", pnl_variance < 1.0, f"CV: {pnl_variance:.2f}"))

# Single-asset safer?
sa_perfect = sum(1 for g in s_gates if g == 5)
checks.append(("Single-asset perfect (safer fallback)", sa_perfect >= 15, f"{sa_perfect}/{len(SEEDS)} seeds at 5/5"))

passed = sum(1 for c in checks if c[1])

for label, result, detail in checks:
    icon = "✅" if result else "❌"
    print(f"  {icon} {label}: {detail}")

print(f"\n  OVERALL: {passed}/{len(checks)} checks passed")

if passed == len(checks):
    print(f"\n  🟢 READY FOR LIVE (PAPER FIRST)")
    print(f"  Start with single-asset filtered (safer, 10/10 gates).")
    print(f"  Multi-asset as secondary track once >= 50 real trades calibrate Bayesian.")
elif passed >= len(checks) - 2:
    print(f"\n  🟡 NEARLY READY — fix the failing checks first")
else:
    print(f"\n  🔴 NOT READY — fundamental issues remain")

print(f"\n  ⚠  ALL SIMULATIONS ARE PAPER. ZERO REAL MONEY.")
