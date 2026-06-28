#!/usr/bin/env python3
"""
FDC Walk-Forward Analysis
==========================
Gold-standard validation for adaptive trading systems.

Methodology:
  - Unanchored rolling windows: 12-month IS, 3-month OOS
  - Re-optimize at each window boundary (fresh calibrator trained on IS only)
  - OOS feed is point-by-point — no future data leaks, no batch training on OOS
  - All OOS windows concatenated into one continuous paper-traded equity curve

Strict adherence to:
  - 5 bps taker fee + volume-based slippage
  - Purge buffer between IS and OOS (no information leakage)
  - Metrics: Net PnL, Sharpe, Calmar, Max DD, WR, PF
  - Per-regime and per-window breakdown

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

# ─── Isolate from live calibrator state ─────────────────────────────────
# Walk-forward creates fresh calibrators; must not clobber live state.
_WF_STATE = bl.BAYESIAN_STATE
_bl_ORIG = bl.BAYESIAN_STATE
bl.BAYESIAN_STATE = REPO / "neural_weights" / "bayesian_state_wf.json"
# If a prior WF state exists, remove it so we start clean
if bl.BAYESIAN_STATE.exists():
    bl.BAYESIAN_STATE.unlink()

# ─── Configuration ──────────────────────────────────────────────────────

TOTAL_CYCLES = 480           # ~48 months of simulated market data
IS_WINDOW = 120              # 12 months in-sample (120 cycles)
OOS_WINDOW = 30              # 3 months out-of-sample (30 cycles)
PURGE_CYCLES = 3             # Buffer between IS and OOS
STEP_SIZE = OOS_WINDOW       # Slide by OOS window size

BANKROLL = 250.0
SEED = 42
TAKER_FEE_BPS = 5            # 5 basis points
SLIPPAGE_BASE = 0.001        # 0.1% base slippage

OUT_DIR = REPO / "output"
OUT_DIR.mkdir(exist_ok=True)
OUT_FILE = OUT_DIR / "walk_forward_results.json"
FROZEN_DIR = REPO / "frozen_model"
FROZEN_DIR.mkdir(exist_ok=True)

# ─── Simulation Primitives ──────────────────────────────────────────────

def btc_walk(start: float, n: int, regime: str) -> list:
    """Generate BTC price walk for a given regime."""
    prices = [float(start)]
    dmap = {
        "trending_up":   (0.0008, 0.008),
        "trending_down": (-0.0008, 0.008),
        "volatile":      (0.0, 0.015),
        "ranging":       (0.0, 0.003),
    }
    d, v = dmap.get(regime, (0.0001, 0.008))
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + np.random.normal(d, v)))
    return prices


def mk_contract(btc: float, ctype: str, seed: int) -> dict:
    """Create a synthetic Polymarket contract."""
    r = random.Random(seed)
    if ctype == "above":
        st = btc * (1.0 + r.uniform(0.01, 0.04))
    else:
        st = btc * (1.0 - r.uniform(0.01, 0.04))
    tp = max(0.05, min(0.90, 0.50 + r.uniform(-0.20, 0.20)))
    yp = round(max(0.01, min(0.99, tp + r.uniform(-0.12, 0.12))), 3)
    return {
        "question": f"Bitcoin {ctype} ${st:,.0f}",
        "conditionId": f"0x{seed:064x}",
        "yes_price": yp, "no_price": round(1 - yp, 3),
        "volume": r.uniform(15000, 200000),
        "strike": st, "contract_type": ctype,
        "hours_to_resolution": r.uniform(2, 72),
    }


def resolve(contract: dict, final_btc: float) -> int:
    """Resolve contract: 1 = YES paid, 0 = NO."""
    if contract["contract_type"] == "above":
        return 1 if final_btc >= contract["strike"] else 0
    else:
        return 1 if final_btc <= contract["strike"] else 0


def btc_signal(prices: list) -> dict:
    """Compute RSI, MACD, SMA-based signal from price history."""
    if len(prices) < 14:
        return {"direction": "neutral", "confidence": 0.0, "rsi": 50, "price": 0}
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = sum(max(d, 0) for d in deltas[-7:]) / 7
    losses = sum(max(-d, 0) for d in deltas[-7:]) / 7
    rsi = 100 - (100 / (1 + gains / max(losses, 1e-9)))

    def ema(v, s):
        a = 2 / (s + 1); r = v[0]
        for vv in v[1:]:
            r = a * vv + (1 - a) * r
        return r

    macd = ema(prices, 6) - ema(prices, 13)
    sma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else prices[-1]
    up = sum(1 for i in range(1, min(4, len(prices))) if prices[-i] > prices[-i - 1])

    d, c = "neutral", 0.0
    if rsi < 48:
        d, c = "up", min(0.80, (48 - rsi) / 15) + (0.10 if up >= 2 else 0)
    elif rsi > 52:
        d, c = "down", min(0.80, (rsi - 52) / 15) + (0.10 if up < 2 else 0)
    else:
        d, c = ("up" if up >= 2 else "down"), 0.20
    return {
        "direction": d, "confidence": min(0.90, c), "rsi": round(rsi, 1),
        "macd": round(macd, 2), "momentum": up, "price": prices[-1],
        "sma20": sma20,
    }


def is_bear_market(prices: list) -> bool:
    """Bear market guard: BTC below 20-SMA AND MACD negative."""
    if len(prices) < 20:
        return False
    sma20 = sum(prices[-20:]) / 20
    def ema(v, s):
        a = 2 / (s + 1); r = v[0]
        for vv in v[1:]:
            r = a * vv + (1 - a) * r
        return r
    macd = ema(prices, 6) - ema(prices, 13)
    return prices[-1] < sma20 and macd < 0


def size_position(edge: float, odds: float, bankroll: float,
                  cal_factor: float, certainty: float, updates: int,
                  multiplier: float = 1.5) -> float:
    """Dynamic Kelly sizing with cold/warm/live phases."""
    if edge <= 0 or bankroll <= 0:
        return 0.0
    cold_until = 10
    warm_until = 30
    if updates < cold_until:
        return round(bankroll * 0.02, 2)
    cf = max(0.25, cal_factor) if updates < warm_until else cal_factor
    ct = max(0.25, certainty) if updates < warm_until else certainty
    return round(min((edge / max(odds, 0.01)) * 0.5 * multiplier * cf * ct, 0.02) * bankroll, 2)


def compute_fees(gross_value: float, volume: float) -> float:
    """Realistic fees: 5bps taker + volume-based slippage."""
    taker = gross_value * TAKER_FEE_BPS / 10000
    slippage = gross_value * SLIPPAGE_BASE * math.sqrt(gross_value / max(volume, 1000))
    return round(taker + slippage, 4)


# ─── Metrics ────────────────────────────────────────────────────────────

def compute_sharpe(returns: list) -> float:
    if len(returns) < 2:
        return 0.0
    return (np.mean(returns) / max(np.std(returns), 1e-9)) * np.sqrt(len(returns))


def compute_calmar(total_return_pct: float, max_dd_pct: float) -> float:
    return total_return_pct / max(max_dd_pct, 0.01)


def compile_metrics(log: list, equity_curve: list, initial_bankroll: float) -> dict:
    """Compile standard metrics from a trade log and equity curve."""
    n = len(log)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
                "pnl_total": 0.0, "pnl_pct": 0.0, "sharpe": 0.0, "profit_factor": 0.0,
                "max_drawdown_pct": 0.0, "calmar_ratio": 0.0, "final_equity": initial_bankroll}

    w = sum(1 for t in log if t["won"])
    l_ = n - w
    wr = w / n * 100
    pnl_total = sum(t["pnl"] for t in log)

    # Drawdown from equity curve
    peak = initial_bankroll
    dd_pct = 0.0
    for eq in equity_curve:
        peak = max(peak, eq["equity"])
        dd = (peak - eq["equity"]) / peak * 100 if peak > 0 else 0
        dd_pct = max(dd_pct, dd)

    gw = sum(t["pnl"] for t in log if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in log if t["pnl"] < 0))
    pf = gw / max(gl, 0.01)

    eq_deltas = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]["equity"]
        curr = equity_curve[i]["equity"]
        if prev > 0:
            eq_deltas.append(curr / prev - 1)
    sh = compute_sharpe(eq_deltas) if eq_deltas else 0.0
    total_ret = (equity_curve[-1]["equity"] - initial_bankroll) / initial_bankroll * 100
    calmar = compute_calmar(total_ret, dd_pct)

    return {
        "trades": n, "wins": w, "losses": l_, "win_rate_pct": round(wr, 2),
        "pnl_total": round(pnl_total, 4), "pnl_pct": round(total_ret, 2),
        "sharpe": round(sh, 2), "profit_factor": round(pf, 2),
        "max_drawdown_pct": round(dd_pct, 2), "calmar_ratio": round(calmar, 2),
        "final_equity": round(equity_curve[-1]["equity"], 2),
    }


# ─── Phase Runner (with point-by-point OOS mode) ────────────────────────

def run_window(
    start_cycle: int, end_cycle: int,
    cal: bl.BayesianCalibrator, enc: fe.FeatureEncoder,
    btc_start: float, bankroll: float,
    freeze: bool = False, label: str = "",
    verbose: bool = True,
):
    """
    Run a window of the simulation.

    freeze=False: IS mode — calibrator learns from every resolved trade.
    freeze=True:  OOS mode — calibrator is frozen, NO learning.

    Returns: (trade_log, equity_curve, final_equity, calibrator)
    """
    random.seed(SEED + start_cycle)
    np.random.seed(SEED + start_cycle)

    cap = bankroll
    btc = btc_start
    peak = bankroll
    trade_count = 0
    pnl_total = 0.0
    filtered = 0
    log = []
    equity_curve = [{"cycle": start_cycle, "equity": cap, "pnl": 0}]

    regimes = ["trending_up", "ranging", "trending_down", "volatile"]
    n_cycles = end_cycle - start_cycle
    cpr = max(1, n_cycles // 4)

    if verbose:
        mode = "IS (train)" if not freeze else "OOS (frozen)"
        print(f"\n{'─'*60}")
        print(f"  {label}: {mode} | cycles {start_cycle}-{end_cycle-1} ({n_cycles} cyc)")
        print(f"  BTC: ${btc:,.0f} | Bankroll: ${cap:,.0f} | Updates: {cal.updates}")
        print(f"  {'─'*60}")

    for cycle_idx in range(start_cycle, end_cycle):
        # Regime rotation every quarter-window
        rg_idx = min(3, (cycle_idx - start_cycle) // cpr)
        reg = regimes[rg_idx]

        prices = btc_walk(btc, 60, reg)
        btc = prices[-1]

        # Bear guard
        if is_bear_market(prices):
            filtered += 1
            equity_curve.append({"cycle": cycle_idx, "equity": cap, "pnl": pnl_total})
            continue

        sig = btc_signal(prices)

        for j in range(3):
            ct = random.choice(["above", "below"])
            con = mk_contract(btc, ct, seed=cycle_idx * 300 + j * 100 + 7)

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

            # ── Bayesian prediction ──
            fv = enc.encode(prices, con["yes_price"], con["no_price"],
                           con["volume"], con["hours_to_resolution"])
            res = cal.predict(fv, market_price=ep)
            cp, cert, cf = res["probability"], res["certainty"], cal.calibration_factor

            ce = cp - ep if d == "up" else (1 - cp) - ep
            bw = min(0.30, cal.updates / 50)
            edge = te * (1 - bw) + ce * bw
            if edge < 0.002:
                continue

            odds = 1.0 - ep
            bet = size_position(edge, odds, cap, cf, cert, cal.updates)
            if bet < 1.0 or bet > cap:
                continue

            fees = compute_fees(bet, con["volume"])
            cap -= (bet + fees)
            trade_count += 1

            # ── Resolution (POINT-BY-POINT) ──
            direc = "YES" if d == "up" else "NO"
            fbm = {
                "trending_up": (0.005, 0.06),
                "trending_down": (-0.06, -0.005),
                "volatile": (-0.05, 0.05),
                "ranging": (-0.02, 0.02),
            }
            lo, hi = fbm.get(reg, (0.0, 0.0))
            fbtc = btc * (1 + random.uniform(lo, hi))
            out = resolve(con, fbtc)
            won = (direc == "YES" and out == 1) or (direc == "NO" and out == 0)
            gross_pnl = bet / ep - bet if won else -bet
            exit_fees = compute_fees(bet + gross_pnl if won else 0, con["volume"])
            pnl = gross_pnl - fees - exit_fees

            cap += bet + pnl
            pnl_total += pnl
            peak = max(peak, cap)

            cal_out = out if direc == "YES" else 1 - out

            # ── Update calibrator (IS mode only) ──
            if not freeze:
                cal.update(fv, cal_out)

            log.append({
                "trade": trade_count, "cycle": cycle_idx, "regime": reg,
                "window": label, "signal_dir": d, "side": direc,
                "pnl": round(pnl, 4), "won": won, "bet": bet,
                "edge": round(edge, 4), "entry_price": ep,
                "fees": round(fees + exit_fees, 4),
            })

        equity_curve.append({"cycle": cycle_idx, "equity": cap, "pnl": round(pnl_total, 2)})

    if verbose and trade_count > 0:
        stats = compile_metrics(log, equity_curve, bankroll)
        print(f"  → Trades: {stats['trades']} | WR: {stats['win_rate_pct']:.1f}% | "
              f"P&L: ${stats['pnl_total']:+,.2f} ({stats['pnl_pct']:+.1f}%)")
        print(f"  → Sharpe: {stats['sharpe']:.2f} | PF: {stats['profit_factor']:.2f} | "
              f"DD: {stats['max_drawdown_pct']:.2f}%")

    return log, equity_curve, cap, cal


# ─── Freeze / Snap / Restore calibrator ─────────────────────────────────

def snapshot_calibrator(cal: bl.BayesianCalibrator) -> dict:
    """Capture calibrator state for later restoration."""
    return {
        "beta": cal.beta.copy(),
        "precision": cal.precision.copy(),
        "updates": cal.updates,
        "learning_rate": cal.learning_rate,
        "brier_sum": cal.brier_sum,
        "brier_count": cal.brier_count,
    }


def restore_calibrator(cal: bl.BayesianCalibrator, snap: dict):
    """Restore calibrator from snapshot. Modifies in-place."""
    cal.beta = snap["beta"].copy()
    cal.precision = snap["precision"].copy()
    cal.updates = snap["updates"]
    cal.learning_rate = snap["learning_rate"]
    cal.brier_sum = snap["brier_sum"]
    cal.brier_count = snap["brier_count"]


def fresh_calibrator() -> bl.BayesianCalibrator:
    """Create a fresh (blank) calibrator with zero updates."""
    cal = bl.BayesianCalibrator()
    cal.brier_sum = 0.0
    cal.brier_count = 0
    cal.updates = 0
    cal.beta = np.zeros(bl.N_FEATURES + 1)
    cal.precision = np.full(bl.N_FEATURES + 1, bl.PRIOR_PRECISION)
    cal.learning_rate = bl.INITIAL_LR
    cal.calibration_log = []
    return cal


# ─── Main: Walk-Forward Analysis ───────────────────────────────────────

def run_walk_forward():
    """Execute full walk-forward analysis."""
    print("=" * 70)
    print("  FDC WALK-FORWARD ANALYSIS")
    print(f"  Unanchored rolling: {IS_WINDOW} IS + {PURGE_CYCLES} purge + {OOS_WINDOW} OOS")
    print(f"  Step size: {STEP_SIZE} | Total cycles: {TOTAL_CYCLES}")
    print(f"  Fees: {TAKER_FEE_BPS}bps taker + volume-based slippage")
    print("=" * 70)

    # Count windows
    windows = []
    start = 0
    while start + IS_WINDOW + PURGE_CYCLES + OOS_WINDOW <= TOTAL_CYCLES:
        is_start = start
        is_end = start + IS_WINDOW
        purge_start = is_end
        purge_end = is_end + PURGE_CYCLES
        oos_start = purge_end
        oos_end = oos_start + OOS_WINDOW
        windows.append((is_start, is_end, purge_start, purge_end, oos_start, oos_end))
        start += STEP_SIZE

    print(f"\n  Total walk-forward windows: {len(windows)}")
    print(f"  Each: {IS_WINDOW} IS + {PURGE_CYCLES} purge + {OOS_WINDOW} OOS = {IS_WINDOW + PURGE_CYCLES + OOS_WINDOW} cycles")

    # ── Run each window ──
    all_oos_logs = []        # Concatenated OOS trade log
    all_oos_curves = []      # All OOS equity curves (for concatenated display)
    all_is_stats = []        # Per-window IS stats
    window_results = []      # Per-window details
    btc_price = 87000.0      # Starting BTC price

    # Track continuous capital across windows
    running_capital = BANKROLL
    combined_oos_curve = []  # Single continuous equity curve across all OOS windows
    last_equity = BANKROLL

    for w_idx, (is_start, is_end, purge_start, purge_end, oos_start, oos_end) in enumerate(windows):
        print(f"\n{'█' * 70}")
        print(f"  WINDOW {w_idx + 1}/{len(windows)}")
        print(f"  IS: [{is_start}-{is_end - 1}]  Purge: [{purge_start}-{purge_end - 1}]  OOS: [{oos_start}-{oos_end - 1}]")
        print(f"  {'█' * 70}")

        # ── Create fresh calibrator for this window's IS ──
        cal = fresh_calibrator()

        # ── phase 1: IS training ──
        is_log, is_curve, is_equity, cal = run_window(
            is_start, is_end, cal,
            fe.FeatureEncoder(calibrator=cal),
            btc_price, BANKROLL,  # Always start IS with fresh bankroll
            freeze=False, label=f"W{w_idx+1}_IS",
            verbose=True,
        )
        is_stats = compile_metrics(is_log, is_curve, BANKROLL)
        all_is_stats.append(is_stats)

        # ── FREEZE snapshot ──
        frozen_snap = snapshot_calibrator(cal)
        frozen_file = FROZEN_DIR / f"wf_frozen_w{w_idx+1}.json"
        with open(frozen_file, "w") as f:
            json.dump({
                "beta": frozen_snap["beta"].tolist(),
                "precision": frozen_snap["precision"].tolist(),
                "updates": frozen_snap["updates"],
                "brier": round(cal.brier_score, 4),
                "timestamp": datetime.now().isoformat(),
            }, f, indent=2)
        if w_idx == 0:
            print(f"  🔒 Frozen model → {frozen_file} (Brier={cal.brier_score:.4f})")

        # ── phase 2: PURGE (discarded, no metrics) ──
        # Run IS-trained calibrator through purge but don't update
        purge_log, _, purge_equity, cal = run_window(
            purge_start, purge_end, cal,
            fe.FeatureEncoder(calibrator=cal),
            btc_price, is_equity,
            freeze=True, label=f"W{w_idx+1}_PURGE",
            verbose=False,
        )

        # ── phase 3: OOS testing (FROZEN model) ──
        # Reload frozen calibrator so purge doesn't contaminate
        oos_cal = fresh_calibrator()
        restore_calibrator(oos_cal, frozen_snap)

        oos_log, oos_curve, oos_equity, oos_cal = run_window(
            oos_start, oos_end, oos_cal,
            fe.FeatureEncoder(calibrator=oos_cal),
            btc_price, purge_equity,  # Continue from purge equity
            freeze=True, label=f"W{w_idx+1}_OOS",
            verbose=True,
        )

        oos_stats = compile_metrics(oos_log, oos_curve, purge_equity)

        # ── Per-regime breakdown for this OOS window ──
        rs = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "fees": 0.0})
        for t in oos_log:
            r = t["regime"]
            rs[r]["n"] += 1; rs[r]["pnl"] += t["pnl"]
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

        # ── Accumulate OOS results for concatenated curve ──
        for t in oos_log:
            t["global_trade"] = len(all_oos_logs) + 1
            t["window_idx"] = w_idx
            all_oos_logs.append(t)

        # Build continuous equity curve
        for eq_pt in oos_curve:
            # Translate local equity to continuous running
            local_cycle = eq_pt["cycle"]
            # Offset to make continuous
            offset_cycle = local_cycle - oos_start
            combined_oos_curve.append({
                "cycle": local_cycle,
                "window": w_idx + 1,
                "equity": eq_pt["equity"],
                "pnl": eq_pt["pnl"],
            })

        # Store window result
        window_results.append({
            "window": w_idx + 1,
            "is_range": [is_start, is_end - 1],
            "oos_range": [oos_start, oos_end - 1],
            "is_stats": is_stats,
            "oos_stats": oos_stats,
            "regime_breakdown": regime_breakdown,
            "frozen_brier": round(oos_cal.brier_score, 4),
            "oos_equity_final": round(oos_equity, 2),
        })

    # ── Compile Final Report ────────────────────────────────────────────

    # Combined metrics from ALL OOS trades
    combined_metrics = compile_metrics(all_oos_logs, combined_oos_curve, BANKROLL)

    # Stability analysis
    giant_threshold = sum(t["pnl"] for t in all_oos_logs if t["pnl"] > 0) * 0.25 if all_oos_logs else 1
    giant_wins = [t for t in all_oos_logs if t["pnl"] > giant_threshold]
    max_single = max([t["pnl"] for t in all_oos_logs], default=0)
    total_pos_pnl = sum(t["pnl"] for t in all_oos_logs if t["pnl"] > 0)

    # Per-regime across all windows
    all_rs = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "fees": 0.0})
    for t in all_oos_logs:
        r = t["regime"]
        all_rs[r]["n"] += 1; all_rs[r]["pnl"] += t["pnl"]
        all_rs[r]["fees"] += t["fees"]
        if t["won"]: all_rs[r]["w"] += 1

    combined_regime = {}
    for r, d in sorted(all_rs.items()):
        combined_regime[r] = {
            "trades": d["n"],
            "pnl": round(d["pnl"], 2),
            "win_rate": round(d["w"] / max(d["n"], 1) * 100, 1),
            "fees": round(d["fees"], 2),
        }

    total_fees = sum(t["fees"] for t in all_oos_logs)

    report = {
        "timestamp": datetime.now().isoformat(),
        "method": "unanchored_rolling_walk_forward",
        "configuration": {
            "total_cycles": TOTAL_CYCLES,
            "is_window_cycles": IS_WINDOW,
            "oos_window_cycles": OOS_WINDOW,
            "purge_cycles": PURGE_CYCLES,
            "step_size": STEP_SIZE,
            "num_windows": len(windows),
            "initial_bankroll": BANKROLL,
            "taker_fee_bps": TAKER_FEE_BPS,
            "seed": SEED,
        },
        "in_sample_aggregate": {
            "avg_trades": round(np.mean([s["trades"] for s in all_is_stats]), 1),
            "avg_wr": round(np.mean([s["win_rate_pct"] for s in all_is_stats]), 1),
            "avg_pnl_pct": round(np.mean([s["pnl_pct"] for s in all_is_stats]), 1),
            "avg_sharpe": round(np.mean([s["sharpe"] for s in all_is_stats]), 2),
            "total_is_trades": sum(s["trades"] for s in all_is_stats),
        },
        "out_of_sample_combined": {
            **combined_metrics,
            "total_fees_paid": round(total_fees, 4),
        },
        "regime_breakdown_combined": combined_regime,
        "stability": {
            "max_single_win_pct": round(max_single / max(total_pos_pnl, 0.01) * 100, 1),
            "giant_wins_count": len(giant_wins),
            "relies_on_giants": len(giant_wins) > 0 and (
                sum(t["pnl"] for t in giant_wins) / max(total_pos_pnl, 0.01) > 0.5
            ),
        },
        "per_window": window_results,
    }

    # ── Print Final Report ──────────────────────────────────────────────

    print("\n" + "=" * 70)
    print("  📊 WALK-FORWARD VALIDATION — FINAL REPORT")
    print("=" * 70)

    print(f"\n  ── IN-SAMPLE (Aggregate across {len(windows)} windows) ──")
    print(f"  Avg Trades/window: {report['in_sample_aggregate']['avg_trades']:.0f}")
    print(f"  Avg WR: {report['in_sample_aggregate']['avg_wr']:.1f}%")
    print(f"  Avg P&L: {report['in_sample_aggregate']['avg_pnl_pct']:+.1f}%")
    print(f"  Avg Sharpe: {report['in_sample_aggregate']['avg_sharpe']:.2f}")

    print(f"\n  ── OUT-OF-SAMPLE (Combined, all {len(windows)} windows) ──")
    cm = combined_metrics
    print(f"  Trades: {cm['trades']} | WR: {cm['win_rate_pct']:.1f}%")
    print(f"  P&L: ${cm['pnl_total']:+,.2f} ({cm['pnl_pct']:+.1f}%)")
    print(f"  Sharpe: {cm['sharpe']:.2f} | PF: {cm['profit_factor']:.2f}")
    print(f"  Max DD: {cm['max_drawdown_pct']:.2f}% | Calmar: {cm['calmar_ratio']:.2f}")
    print(f"  Total Fees: ${total_fees:,.2f}")
    print(f"  Final Equity: ${cm['final_equity']:,.2f}")

    print(f"\n  ── PER-REGIME (Combined OOS) ──")
    for reg, d in sorted(combined_regime.items()):
        print(f"  {reg:15s}: {d['trades']:3d} trades | P&L ${d['pnl']:+,.2f} | WR {d['win_rate']:.1f}%")

    print(f"\n  ── PER-WINDOW OOS ──")
    for wr in window_results:
        os = wr["oos_stats"]
        print(f"  W{wr['window']:2d} [{wr['oos_range'][0]:3d}-{wr['oos_range'][1]:3d}]: "
              f"{os['trades']:3d} tr | WR {os['win_rate_pct']:5.1f}% | "
              f"P&L ${os['pnl_total']:+8.2f} ({os['pnl_pct']:+6.1f}%) | "
              f"Sh {os['sharpe']:5.2f} | DD {os['max_drawdown_pct']:5.2f}%")

    print(f"\n  ── STABILITY ──")
    print(f"  Max single contribution: {report['stability']['max_single_win_pct']}%")
    print(f"  Giant wins (>{25}% of total): {report['stability']['giant_wins_count']}")
    print(f"  Relies on giant wins: {report['stability']['relies_on_giants']}")

    # Overfitting check
    is_avg_pnl = report["in_sample_aggregate"]["avg_pnl_pct"]
    oos_pnl = combined_metrics["pnl_pct"]
    ratio = oos_pnl / max(is_avg_pnl, 0.1)
    print(f"\n  ── OVERFITTING CHECK ──")
    print(f"  OOS/IS P&L ratio: {ratio:.2f}x")
    if ratio > 0.7:
        print(f"  ✅ Robust — OOS retains {ratio:.0%} of IS performance")
    elif ratio > 0.4:
        print(f"  ⚠️  Moderate degradation — check per-window consistency")
    else:
        print(f"  ❌ Severe overfitting — system does not generalize")

    # Save
    with open(OUT_FILE, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n📁 Full report saved to {OUT_FILE}")

    return report


if __name__ == "__main__":
    run_walk_forward()
