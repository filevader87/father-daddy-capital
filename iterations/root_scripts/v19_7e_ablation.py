#!/usr/bin/env python3
"""V19.7e Ablation Tests — MC by direction and RSI zone."""

import sys, os, json, random, glob
sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
import pm_engine_v19_7 as eng

print("=" * 70)
print("V19.7e ABLATION — 30 seeds × 3000 cycles × $100")
print("Shadow mode: RSI 55-70 DOWN blocked, RSI 70-82 DOWN needs 2+ conf")
print("=" * 70)

random.seed(42)
results = eng.mc_backtest(seeds=30, cycles=3000)

# Load journal
journals = sorted(glob.glob('/mnt/c/Users/12035/father_daddy_capital/output/journal/journal_*.json'))
if not journals:
    print("No journal found")
    sys.exit(1)

with open(journals[-1]) as f:
    jdata = json.load(f)

entries = jdata.get('entries', [])

def classify_rsi(rsi):
    if rsi < 20: return 'RSI<20_blocked'
    elif rsi < 28: return 'RSI_20-28_deep_oversold'
    elif rsi < 35: return 'RSI_28-35_oversold'
    elif rsi < 45: return 'RSI_35-45_near_oversold'
    elif rsi < 55: return 'RSI_45-55_dead'
    elif rsi < 70: return 'RSI_55-70_moderate_ob'
    elif rsi < 82: return 'RSI_70-82_strong_ob'
    else: return 'RSI>82_blocked'

def compute_stats(entries, label):
    if not entries:
        print(f"  {label}: 0 trades")
        return {}
    wins = sum(1 for e in entries if e['exit'].get('won', False))
    total = len(entries)
    wr = wins / total * 100
    avg_entry_price = sum(e['entry'].get('contract_price', 0) for e in entries) / total
    total_pnl = sum(e['exit'].get('pnl', 0) for e in entries)
    net_ev = total_pnl / total
    
    gross_profit = sum(e['exit'].get('pnl', 0) for e in entries if e['exit'].get('pnl', 0) > 0)
    gross_loss = abs(sum(e['exit'].get('pnl', 0) for e in entries if e['exit'].get('pnl', 0) < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    pnls = [e['exit'].get('pnl', 0) for e in entries]
    cum = 0; peak = 0; max_dd = 0
    for p in pnls:
        cum += p; peak = max(peak, cum)
        dd = (peak - cum) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    
    streak = 0; max_streak = 0
    for e in entries:
        if e['exit'].get('won', False):
            streak = 0
        else:
            streak += 1; max_streak = max(max_streak, streak)
    
    print(f"  {label}:")
    print(f"    trades={total} | WR={wr:.1f}% | avg_entry=${avg_entry_price:.3f} | net_EV=${net_ev:.3f}/trade")
    print(f"    PnL=${total_pnl:.2f} | PF={pf:.2f} | DD={max_dd*100:.1f}% | loss_streak={max_streak}")
    return {"trades": total, "wr": wr, "net_ev": net_ev, "pf": pf, "dd": max_dd, "loss_streak": max_streak}

# Parse entries
up_entries = [e for e in entries if e['entry'].get('side') == 'Up']
down_entries = [e for e in entries if e['entry'].get('side') == 'Down']

print(f"\n{'='*70}")
print("DIRECTION ABLATION")
print(f"{'='*70}")
compute_stats(entries, "ALL")
compute_stats(up_entries, "UP-only (oversold bounce)")
compute_stats(down_entries, "DOWN-only (overbought reversal)")

print(f"\n{'='*70}")
print("RSI ZONE ABLATION")
print(f"{'='*70}")
zone_order = ['RSI<20_blocked', 'RSI_20-28_deep_oversold', 'RSI_28-35_oversold', 
              'RSI_35-45_near_oversold', 'RSI_45-55_dead', 'RSI_55-70_moderate_ob', 
              'RSI_70-82_strong_ob', 'RSI>82_blocked']
for zone in zone_order:
    zone_all = [e for e in entries if classify_rsi(e['entry'].get('rsi', 50)) == zone]
    zone_up = [e for e in zone_all if e['entry'].get('side') == 'Up']
    zone_down = [e for e in zone_all if e['entry'].get('side') == 'Down']
    if zone_all:
        compute_stats(zone_all, f"{zone}")
        if zone_up:
            compute_stats(zone_up, f"  {zone} → UP")
        if zone_down:
            compute_stats(zone_down, f"  {zone} → DOWN")

# Asset breakdown (MC is BTC-only but structure supports multi-asset)
print(f"\n{'='*70}")
print("REGIME ABLATION")
print(f"{'='*70}")
regimes = set(e['entry'].get('regime', 'unknown') for e in entries)
for regime in sorted(regimes):
    regime_entries = [e for e in entries if e['entry'].get('regime') == regime]
    compute_stats(regime_entries, f"  {regime}")

print(f"\n{'='*70}")
print("CONFIDENCE ABLATION")
print(f"{'='*70}")
conf_bins = [(0.0, 0.85, 'conf<0.85'), (0.85, 0.90, 'conf 0.85-0.90'), (0.90, 0.95, 'conf 0.90-0.95'), (0.95, 1.01, 'conf≥0.95')]
for lo, hi, label in conf_bins:
    bin_entries = [e for e in entries if lo <= e['entry'].get('confidence', 0) < hi]
    compute_stats(bin_entries, label)

print(f"\n{'='*70}")
print("OVERALL SUMMARY")
print(f"{'='*70}")
print(f"  Total entries: {len(entries)}")
print(f"  UP entries: {len(up_entries)} ({len(up_entries)/len(entries)*100:.0f}%)")
print(f"  DOWN entries: {len(down_entries)} ({len(down_entries)/len(entries)*100:.0f}%)")
if up_entries:
    up_wr = sum(1 for e in up_entries if e['exit'].get('won', False)) / len(up_entries) * 100
    up_pnl = sum(e['exit'].get('pnl', 0) for e in up_entries)
    print(f"  UP WR: {up_wr:.1f}% | UP PnL: ${up_pnl:.2f}")
if down_entries:
    down_wr = sum(1 for e in down_entries if e['exit'].get('won', False)) / len(down_entries) * 100
    down_pnl = sum(e['exit'].get('pnl', 0) for e in down_entries)
    print(f"  DOWN WR: {down_wr:.1f}% | DOWN PnL: ${down_pnl:.2f}")

# Shadow mode effectiveness
shadow_down = [e for e in down_entries if 55 <= e['entry'].get('rsi', 50) < 70]
strong_down = [e for e in down_entries if 70 <= e['entry'].get('rsi', 50) < 82]
print(f"\n  Shadow zone (RSI 55-70 DOWN): {len(shadow_down)} trades → shadow-blocked in production")
print(f"  Strong zone (RSI 70-82 DOWN): {len(strong_down)} trades → needs 2+ confirmations")
if shadow_down:
    shadow_wr = sum(1 for e in shadow_down if e['exit'].get('won', False)) / len(shadow_down) * 100
    print(f"  Shadow zone WR: {shadow_wr:.1f}% (would have been positive EV: {'YES' if shadow_wr > 52 else 'NO'})")
if strong_down:
    strong_wr = sum(1 for e in strong_down if e['exit'].get('won', False)) / len(strong_down) * 100
    print(f"  Strong zone WR: {strong_wr:.1f}% (positive EV: {'YES' if strong_wr > 55 else 'MARGINAL'})")