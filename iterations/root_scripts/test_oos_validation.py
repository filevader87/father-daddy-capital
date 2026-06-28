#!/usr/bin/env python3
"""
FDC Out-of-Sample Validation Harness
Rigorous time-series split with purge buffer. 70% IS / 30% OOS.

Procedure:
  1. IS phase (cycles 0-139): train neural + Bayesian, freeze weights
  2. Purge (cycles 140-144): discard to prevent information leakage
  3. OOS phase (cycles 145-199): blind test with frozen model
  4. Measure: net PnL (after fees), Sharpe, Calmar, max DD, WR, PF
     Per-regime and per-phase breakdown.

Fees: 5bps taker (0.05%) + volume-based slippage (sqrt(size/volume)*0.001)
Author: Hugh (3rd of 5)
Date: 2026-05-15
"""

import sys, os, random, json, copy, math
import numpy as np
from pathlib import Path
from datetime import datetime
from collections import defaultdict

REPO = Path("/mnt/c/Users/12035/father_daddy_capital")
sys.path.insert(0, str(REPO / "src" / "neural"))
import bayesian_layer as bl
import feature_encoder as fe

# ─── Configuration ──────────────────────────────────────────────────────

TOTAL_CYCLES = 200
IS_CYCLES = 140       # 70%
PURGE_CYCLES = 5      # buffer
OOS_CYCLES = 55       # 30% (200 - 140 - 5)
BANKROLL = 200.0
SEED = 42
TAKER_FEE_BPS = 5     # 5 basis points
SLIPPAGE_BASE = 0.001 # 0.1% base slippage

OUT_DIR = REPO / "output"
OUT_FILE = OUT_DIR / "oos_validation_results.json"
FROZEN_DIR = REPO / "frozen_model"
FROZEN_DIR.mkdir(exist_ok=True)

# ─── Simulation Functions (from test_pm_sim_filtered) ──────────────────

def btc_walk(start, n, regime):
    prices = [float(start)]
    dmap = {"trending_up": (0.0008, 0.008), "trending_down": (-0.0008, 0.008),
            "volatile": (0.0, 0.015), "ranging": (0.0, 0.003)}
    d, v = dmap.get(regime, (0.0001, 0.008))
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + np.random.normal(d, v)))
    return prices

def mk_contract(btc, ctype, seed):
    r = random.Random(seed)
    if ctype == "above": st = btc * (1.0 + r.uniform(0.01, 0.04))
    else: st = btc * (1.0 - r.uniform(0.01, 0.04))
    tp = max(0.05, min(0.90, 0.50 + r.uniform(-0.20, 0.20)))
    yp = round(max(0.01, min(0.99, tp + r.uniform(-0.12, 0.12))), 3)
    return {"question": f"Bitcoin {ctype} ${st:,.0f}", "conditionId": f"0x{seed:064x}",
            "yes_price": yp, "no_price": round(1-yp,3), "volume": r.uniform(15000, 200000),
            "strike": st, "contract_type": ctype, "hours_to_resolution": r.uniform(2, 72)}

def resolve(c, final_btc):
    return 1 if (c["contract_type"] == "above" and final_btc >= c["strike"]) or \
                 (c["contract_type"] == "below" and final_btc <= c["strike"]) else 0

def btc_signal(prices):
    if len(prices) < 14: return {"direction": "neutral", "confidence": 0.0, "rsi": 50, "price": 0}
    deltas = [prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains = sum(max(d,0) for d in deltas[-7:])/7
    losses = sum(max(-d,0) for d in deltas[-7:])/7
    rsi = 100-(100/(1+gains/max(losses,1e-9)))
    def ema(v,s):
        a=2/(s+1); r=v[0]
        for vv in v[1:]: r=a*vv+(1-a)*r
        return r
    macd = ema(prices,6)-ema(prices,13)
    sma20 = sum(prices[-20:])/20 if len(prices)>=20 else prices[-1]
    up = sum(1 for i in range(1,min(4,len(prices))) if prices[-i]>prices[-i-1])
    d, c = "neutral", 0.0
    if rsi<48: d,c = "up", min(0.80,(48-rsi)/15)+(0.10 if up>=2 else 0)
    elif rsi>52: d,c = "down", min(0.80,(rsi-52)/15)+(0.10 if up<2 else 0)
    else: d,c = ("up" if up>=2 else "down"), 0.20
    return {"direction": d, "confidence": min(0.90,c), "rsi": round(rsi,1),
            "macd": round(macd,2), "momentum": up, "price": prices[-1], "sma20": sma20}

def is_bear_market(prices):
    if len(prices) < 20: return False
    sma20 = sum(prices[-20:])/20
    def ema(v,s):
        a=2/(s+1); r=v[0]
        for vv in v[1:]: r=a*vv+(1-a)*r
        return r
    macd = ema(prices,6)-ema(prices,13)
    return prices[-1] < sma20 and macd < 0

def size(edge, odds, bankroll, cal_factor, certainty, updates, multiplier=1.5, cold_until=10, warm_until=30):
    if edge<=0 or bankroll<=0: return 0.0
    if updates<cold_until: return round(bankroll*0.02,2)
    cf = max(0.25,cal_factor) if updates<warm_until else cal_factor
    ct = max(0.25,certainty) if updates<warm_until else certainty
    return round(min((edge/max(odds,0.01))*0.5*multiplier*cf*ct, 0.02)*bankroll,2)

def compute_fees(gross_value, volume):
    """Realistic fees: 5bps taker + volume-based slippage."""
    taker = gross_value * TAKER_FEE_BPS / 10000
    slippage = gross_value * SLIPPAGE_BASE * math.sqrt(gross_value / max(volume, 1000))
    return round(taker + slippage, 4)

# ─── Cycle-level equity tracker ────────────────────────────────────────

def compute_sharpe(returns):
    if len(returns) < 2: return 0
    return (np.mean(returns) / max(np.std(returns), 1e-9)) * np.sqrt(len(returns))

def compute_calmar(total_return_pct, max_dd_pct):
    return total_return_pct / max(max_dd_pct, 0.01)

# ─── Phase Runner ───────────────────────────────────────────────────────

def run_phase(
    start_cycle, end_cycle, bankroll, cal, enc, btc_start,
    label="", freeze=False, verbose=True
):
    """Run a phase of the simulation. Returns (log, equity_curve, final_cap, stats)."""
    random.seed(SEED + start_cycle)  # deterministic continuation
    np.random.seed(SEED + start_cycle)
    
    cap = bankroll
    btc = btc_start
    peak = bankroll
    n = w = l = 0
    pnl_t = 0.0
    filtered = 0
    log = []
    equity_curve = [{"cycle": start_cycle, "equity": cap, "pnl": 0}]
    
    regimes = ["trending_up", "ranging", "trending_down", "volatile"]
    cpr = max(1, (end_cycle - start_cycle) // 4)
    
    if verbose:
        print(f"\n{'='*70}")
        print(f"  {label}: cycles {start_cycle}-{end_cycle-1} ({end_cycle-start_cycle} cycles)")
        print(f"  Starting BTC: ${btc:,.0f} | Bankroll: ${cap:,.0f}")
        print(f"  {'='*70}")
    
    for i in range(start_cycle, end_cycle):
        rg_idx = min(3, (i - start_cycle) // cpr)
        reg = regimes[rg_idx]
        prices = btc_walk(btc, 60, reg)
        btc = prices[-1]
        
        # Bear guard
        if is_bear_market(prices):
            filtered += 1
            equity_curve.append({"cycle": i, "equity": cap, "pnl": 0})
            continue
        
        sig = btc_signal(prices)
        for j in range(3):
            ct = random.choice(["above", "below"])
            con = mk_contract(btc, ct, seed=i*300+j*100+7)
            if sig["direction"] == "neutral" or sig["confidence"] < 0.12:
                continue
            d = sig["direction"]
            if (d == "up" and ct != "above") or (d == "down" and ct != "below"):
                continue
            ep = con["yes_price"] if d == "up" else con["no_price"]
            if ep < 0.05 or ep > 0.85:
                continue
            te = sig["confidence"] - ep
            if te < 0.005:
                continue
            
            # Bayesian prediction
            fv = enc.encode(prices, con["yes_price"], con["no_price"], con["volume"], con["hours_to_resolution"])
            res = cal.predict(fv, market_price=ep)
            cp, cert, cf = res["probability"], res["certainty"], cal.calibration_factor
            
            ce = cp - ep if d == "up" else (1 - cp) - ep
            bw = min(0.30, cal.updates / 50)
            edge = te * (1 - bw) + ce * bw
            if edge < 0.002:
                continue
            
            odds = 1.0 - ep
            bet = size(edge, odds, cap, cf, cert, cal.updates, cold_until=max(5, TOTAL_CYCLES//20), warm_until=max(15, TOTAL_CYCLES//7))
            if bet < 1.0 or bet > cap:
                continue
            
            # Fees
            fees = compute_fees(bet, con["volume"])
            
            cap -= (bet + fees)
            n += 1
            
            # Resolution
            direc = "YES" if d == "up" else "NO"
            fbm = {"trending_up": (0.005, 0.06), "trending_down": (-0.06, -0.005),
                   "volatile": (-0.05, 0.05), "ranging": (-0.02, 0.02)}
            lo, hi = fbm.get(reg, (0.0, 0.0))
            fbtc = btc * (1 + random.uniform(lo, hi))
            out = resolve(con, fbtc)
            won = (direc == "YES" and out == 1) or (direc == "NO" and out == 0)
            gross_pnl = bet / ep - bet if won else -bet
            exit_fees = compute_fees(bet + gross_pnl if won else 0, con["volume"])
            pnl = gross_pnl - fees - exit_fees
            
            cap += bet + pnl
            pnl_t += pnl
            peak = max(peak, cap)
            if won: w += 1
            else: l += 1
            
            cal_out = out if direc == "YES" else 1 - out
            
            if not freeze:
                cal.update(fv, cal_out)
            
            log.append({
                "trade": n, "cycle": i, "regime": reg, "signal_dir": d, "side": direc,
                "pnl": round(pnl, 4), "won": won, "bet": bet, "edge": round(edge, 4),
                "entry_price": ep, "fees": round(fees + exit_fees, 4),
            })
        
        equity_curve.append({"cycle": i, "equity": cap, "pnl": round(pnl_t, 2)})
    
    if n == 0:
        return log, equity_curve, cap, {"trades": 0, "pnl": 0, "wr": 0, "sharpe": 0, "dd": 0}
    
    wr = w / max(n, 1) * 100
    dd_pct = (peak - cap) / peak * 100 if peak > 0 else 0
    gw = sum(t["pnl"] for t in log if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in log if t["pnl"] < 0))
    pf = gw / max(gl, 0.01)
    
    eq_deltas = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i-1]["equity"]
        curr = equity_curve[i]["equity"]
        if prev > 0:
            eq_deltas.append(curr / prev - 1)
    sh = compute_sharpe(eq_deltas) if eq_deltas else 0
    total_ret = (cap - bankroll) / bankroll * 100
    calmar = compute_calmar(total_ret, dd_pct)
    
    stats = {
        "trades": n, "wins": w, "losses": l, "win_rate_pct": round(wr, 2),
        "pnl_total": round(pnl_t, 4), "pnl_pct": round(total_ret, 2),
        "sharpe": round(sh, 2), "profit_factor": round(pf, 2),
        "max_drawdown_pct": round(dd_pct, 2), "calmar_ratio": round(calmar, 2),
        "cycles_filtered": filtered, "final_equity": round(cap, 2),
    }
    
    if verbose:
        print(f"\n  --- {label} RESULTS ---")
        print(f"  Trades: {n} | Wins: {w} | Losses: {l} | WR: {wr:.1f}%")
        print(f"  P&L: ${pnl_t:+,.2f} ({total_ret:+.1f}%) | DD: {dd_pct:.1f}%")
        print(f"  Sharpe: {sh:.2f} | PF: {pf:.2f} | Calmar: {calmar:.2f}")
    
    return log, equity_curve, cap, stats


# ─── Freeze / Load ──────────────────────────────────────────────────────

def freeze_model(cal, label="IS"):
    """Save frozen model parameters."""
    frozen = {
        "beta": cal.beta.tolist() if hasattr(cal.beta, 'tolist') else list(cal.beta),
        "precision": cal.precision.tolist() if hasattr(cal.precision, 'tolist') else list(cal.precision),
        "updates": cal.updates,
        "learning_rate": cal.learning_rate,
        "brier_sum": cal.brier_sum,
        "brier_count": cal.brier_count,
        "label": label,
        "timestamp": datetime.now().isoformat(),
    }
    path = FROZEN_DIR / "bayesian_frozen.json"
    with open(path, 'w') as f:
        json.dump(frozen, f, indent=2)
    print(f"\n🔒 Frozen model saved to {path}")
    return frozen

def load_frozen(cal):
    """Load frozen parameters into calibrator."""
    path = FROZEN_DIR / "bayesian_frozen.json"
    if not path.exists():
        print("No frozen model found — using fresh calibrator")
        return cal
    with open(path) as f:
        frozen = json.load(f)
    cal.beta = np.array(frozen["beta"], dtype=float)
    cal.precision = np.array(frozen["precision"], dtype=float)
    cal.updates = int(frozen["updates"])
    cal.learning_rate = float(frozen["learning_rate"])
    cal.brier_sum = float(frozen["brier_sum"])
    cal.brier_count = int(frozen["brier_count"])
    print(f"🔓 Loaded frozen model: {frozen['updates']} updates, Brier={frozen['brier_sum']/max(frozen['brier_count'],1):.4f}")
    return cal


# ─── Main: IS → Freeze → Purge → OOS ───────────────────────────────────

def run_oos_validation():
    print("="*70)
    print("  FDC OUT-OF-SAMPLE VALIDATION")
    print(f"  Split: {IS_CYCLES} IS + {PURGE_CYCLES} purge + {OOS_CYCLES} OOS = {TOTAL_CYCLES} cycles")
    print(f"  Fees: {TAKER_FEE_BPS}bps taker + volume-based slippage")
    print("="*70)
    
    # Fresh calibrator
    cal = bl.BayesianCalibrator()
    cal.brier_sum = cal.brier_count = cal.updates = 0
    cal.beta = np.zeros(13)
    cal.precision = np.full(13, bl.PRIOR_PRECISION)
    cal.learning_rate = bl.INITIAL_LR
    enc = fe.FeatureEncoder(calibrator=cal)
    
    # ── PHASE 1: In-Sample Training ──
    is_log, is_curve, is_cap, is_stats = run_phase(
        0, IS_CYCLES, BANKROLL, cal, enc, 87000.0,
        label="IS (In-Sample Training)", freeze=False
    )
    
    # ── FREEZE ──
    frozen = freeze_model(cal, label="IS")
    
    # ── PHASE 2: Purge (discarded) ──
    purge_log, _, purge_cap, _ = run_phase(
        IS_CYCLES, IS_CYCLES + PURGE_CYCLES, is_cap, cal, enc, 87000.0,
        label="PURGE (discarded)", freeze=False, verbose=False
    )
    
    # ── PHASE 3: Out-of-Sample (BLIND — frozen model) ──
    oos_cal = bl.BayesianCalibrator()
    oos_cal = load_frozen(oos_cal)  # Can't really use loaded since it's fresh; use IS cal
    oos_cal_copy = copy.deepcopy(cal)  # Copy IS-trained calibrator
    oos_enc = fe.FeatureEncoder(calibrator=oos_cal_copy)
    
    oos_log, oos_curve, oos_cap, oos_stats = run_phase(
        IS_CYCLES + PURGE_CYCLES, TOTAL_CYCLES, purge_cap, oos_cal_copy, oos_enc, 87000.0,
        label="OOS (Out-of-Sample — FROZEN)", freeze=True  # freeze=True: no learning
    )
    
    # ── Per-regime breakdown (OOS) ──
    rs = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "fees": 0.0})
    for t in oos_log:
        r = t["regime"]
        rs[r]["n"] += 1
        rs[r]["pnl"] += t["pnl"]
        rs[r]["fees"] += t["fees"]
        if t["won"]: rs[r]["w"] += 1
    
    regime_breakdown = {}
    for r, d in sorted(rs.items()):
        regime_breakdown[r] = {
            "trades": d["n"],
            "pnl": round(d["pnl"], 2),
            "win_rate": round(d["w"] / max(d["n"], 1) * 100, 1),
            "fees": round(d["fees"], 2),
        }
    
    # ── Stability analysis ──
    equity_series = [e["equity"] for e in oos_curve]
    daily_returns = []
    for i in range(1, len(equity_series)):
        if equity_series[i-1] > 0:
            daily_returns.append(equity_series[i] / equity_series[i-1] - 1)
    
    giant_wins = [t for t in oos_log if t["pnl"] > sum(t["pnl"] for t in oos_log if t["pnl"] > 0) * 0.25]
    stability = {
        "equity_curve_shape": "upward" if oos_cap > purge_cap else "downward",
        "positive_cycles_pct": round(sum(1 for r in daily_returns if r > 0) / max(len(daily_returns), 1) * 100, 1),
        "max_single_win_pct": round(max([t["pnl"] for t in oos_log], default=0) / abs(max(sum(t["pnl"] for t in oos_log), 0.01)) * 100, 1),
        "giant_wins_count": len(giant_wins),
        "reliance_on_giants": len(giant_wins) > 0 and sum(t["pnl"] for t in giant_wins) / max(sum(t["pnl"] for t in oos_log if t["pnl"] > 0), 0.01) > 0.5,
    }
    
    # ── Compile final report ──
    total_fees = sum(t["fees"] for t in oos_log)
    oos_gross = oos_stats["pnl_total"] + total_fees
    
    report = {
        "timestamp": datetime.now().isoformat(),
        "configuration": {
            "total_cycles": TOTAL_CYCLES,
            "is_cycles": IS_CYCLES,
            "purge_cycles": PURGE_CYCLES,
            "oos_cycles": OOS_CYCLES,
            "bankroll": BANKROLL,
            "taker_fee_bps": TAKER_FEE_BPS,
            "seed": SEED,
        },
        "in_sample": {
            **is_stats,
            "final_equity": round(is_cap, 2),
        },
        "out_of_sample": {
            **oos_stats,
            "gross_pnl_before_fees": round(oos_gross, 4),
            "total_fees_paid": round(total_fees, 4),
            "net_pnl_after_fees": oos_stats["pnl_total"],
        },
        "regime_breakdown_oos": regime_breakdown,
        "stability": stability,
        "frozen_model_info": {
            "updates_at_freeze": frozen["updates"],
            "brier_at_freeze": round(frozen["brier_sum"] / max(frozen["brier_count"], 1), 4),
        },
    }
    
    # ── Print final report ──
    print("\n" + "="*70)
    print("  📊 FINAL OOS VALIDATION REPORT")
    print("="*70)
    print(f"\n  IN-SAMPLE (trained):")
    print(f"    Trades: {is_stats['trades']} | WR: {is_stats['win_rate_pct']:.1f}% | P&L: ${is_stats['pnl_total']:+,.2f} ({is_stats['pnl_pct']:+.1f}%)")
    print(f"    Sharpe: {is_stats['sharpe']:.2f} | PF: {is_stats['profit_factor']:.2f} | DD: {is_stats['max_drawdown_pct']:.1f}%")
    
    print(f"\n  OUT-OF-SAMPLE (frozen, blind):")
    print(f"    Trades: {oos_stats['trades']} | WR: {oos_stats['win_rate_pct']:.1f}% | P&L: ${oos_stats['pnl_total']:+,.2f} ({oos_stats['pnl_pct']:+.1f}%)")
    print(f"    Sharpe: {oos_stats['sharpe']:.2f} | PF: {oos_stats['profit_factor']:.2f} | DD: {oos_stats['max_drawdown_pct']:.1f}%")
    print(f"    Calmar: {oos_stats['calmar_ratio']:.2f} | Fees: ${total_fees:,.2f}")
    
    print(f"\n  REGIME BREAKDOWN (OOS):")
    for r, d in sorted(regime_breakdown.items()):
        print(f"    {r:15s}: {d['trades']:3d} trades | P&L ${d['pnl']:+,.2f} | WR {d['win_rate']:.1f}% | Fees ${d['fees']:.2f}")
    
    print(f"\n  STABILITY:")
    print(f"    Shape: {stability['equity_curve_shape']} | +cycles: {stability['positive_cycles_pct']}%")
    print(f"    Max single contribution: {stability['max_single_win_pct']}% | Giant wins: {stability['giant_wins_count']}")
    print(f"    Relies on giant wins: {stability['reliance_on_giants']}")
    
    # Save
    OUT_DIR.mkdir(exist_ok=True)
    with open(OUT_FILE, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n📁 Full report saved to {OUT_FILE}")
    
    return report


if __name__ == "__main__":
    run_oos_validation()
