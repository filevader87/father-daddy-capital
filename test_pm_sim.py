#!/usr/bin/env python3
"""
Polymarket Small-Account Simulation — Full Regime Sweep
========================================================
Matches real pm_engine.py architecture:
1. Traditional RSI(7)/MACD(6/13) signal stack → direction + confidence
2. Feature encoding (12-dim) → Bayesian calibrator → calibrated edge
3. Hybrid sizing (cold/warm/live, adaptive thresholds) → settle → learn

CLI:
  --once         Default: 200-cycle full regime sweep (50/regime)
  --cycles N     Override cycle count
  --seed N       Random seed (default: 42)
  --json         Output JSON summary for cron/automation
  --bankroll N   Starting capital (default: 200.0)
  --verbose      Show regime transitions + trade details
"""
import sys, random, json
import numpy as np
from pathlib import Path
from datetime import datetime
from collections import defaultdict

REPO = Path("/mnt/c/Users/12035/father_daddy_capital")
sys.path.insert(0, str(REPO / "src" / "neural"))
import bayesian_layer as bl
import feature_encoder as fe


# ══════════════════════════════════════════════════════════════════════════════
# Market generators
# ══════════════════════════════════════════════════════════════════════════════

def btc_walk(start, n, regime):
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


def mk_contract(btc, ctype, seed):
    r = random.Random(seed)
    if ctype == "above":
        st = btc * (1.0 + r.uniform(0.01, 0.04))
    else:
        st = btc * (1.0 - r.uniform(0.01, 0.04))
    tp = max(0.05, min(0.90, 0.50 + r.uniform(-0.20, 0.20)))
    yp = round(max(0.01, min(0.99, tp + r.uniform(-0.12, 0.12))), 3)
    return {
        "question": f"Bitcoin {ctype} ${st:,.0f} on May {r.randint(18,28)}",
        "conditionId": f"0x{seed:064x}",
        "yes_price": yp,
        "no_price": round(1 - yp, 3),
        "volume": r.uniform(15000, 200000),
        "strike": st,
        "contract_type": ctype,
        "hours_to_resolution": r.uniform(2, 72),
    }


def resolve(c, final_btc):
    if c["contract_type"] == "above":
        return 1 if final_btc >= c["strike"] else 0
    return 1 if final_btc <= c["strike"] else 0


# ══════════════════════════════════════════════════════════════════════════════
# Traditional signal stack — matches pm_engine.py btc_signal()
# ══════════════════════════════════════════════════════════════════════════════

def btc_signal(prices):
    """RSI(7) + MACD(6/13) + momentum direction."""
    if len(prices) < 14:
        return {"direction": "neutral", "confidence": 0.0, "rsi": 50, "price": 0}

    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = sum(max(d, 0) for d in deltas[-7:]) / 7
    losses = sum(max(-d, 0) for d in deltas[-7:]) / 7
    rs = gains / max(losses, 1e-9)
    rsi = 100 - (100 / (1 + rs))

    def ema(vals, span):
        a = 2 / (span + 1)
        result = vals[0]
        for v in vals[1:]:
            result = a * v + (1 - a) * result
        return result
    macd = ema(prices, 6) - ema(prices, 13)

    up = sum(1 for i in range(1, min(4, len(prices))) if prices[-i] > prices[-i-1])

    direction, confidence = "neutral", 0.0
    if rsi < 48:
        direction = "up"; confidence = min(0.80, (48 - rsi) / 15)
        if up >= 2: confidence += 0.10
    elif rsi > 52:
        direction = "down"; confidence = min(0.80, (rsi - 52) / 15)
        if up < 2: confidence += 0.10
    else:
        direction = "up" if up >= 2 else "down"
        confidence = 0.20

    return {
        "direction": direction,
        "confidence": min(0.90, confidence),
        "rsi": round(rsi, 1),
        "macd": round(macd, 2),
        "momentum": up,
        "price": prices[-1],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Adaptive hybrid sizing — thresholds scale with simulation length
# Cold: 2% fixed. Warm: floored Kelly ×1.5. Live: full calibrated Kelly ×1.5.
# ══════════════════════════════════════════════════════════════════════════════

def size(edge, odds, bankroll, cal_factor, certainty, updates, multiplier=1.5,
         cold_until=10, warm_until=30):
    if edge <= 0 or bankroll <= 0:
        return 0.0
    if updates < cold_until:
        return round(bankroll * 0.02, 2)
    cf = max(0.25, cal_factor) if updates < warm_until else cal_factor
    ct = max(0.25, certainty) if updates < warm_until else certainty
    adj = (edge / max(odds, 0.01)) * 0.5 * multiplier * cf * ct
    return round(min(adj, 0.02) * bankroll, 2)


# ══════════════════════════════════════════════════════════════════════════════
# Simulation
# ══════════════════════════════════════════════════════════════════════════════

def simulate(cycles=200, bankroll=200.0, seed=42, json_mode=False, verbose=False):
    random.seed(seed); np.random.seed(seed)

    cal = bl.BayesianCalibrator()
    cal.brier_sum = cal.brier_count = cal.updates = 0
    cal.beta = np.zeros(13)
    cal.precision = np.full(13, bl.PRIOR_PRECISION)
    cal.learning_rate = bl.INITIAL_LR
    enc = fe.FeatureEncoder(calibrator=cal)

    cap = bankroll; btc = 87000.0; peak = bankroll
    n = w = l = 0; pnl_t = 0.0; log = []
    prev_regime = "none"

    # Extended thresholds for longer sims
    cold_until = max(5, cycles // 20)     # ~10 for 200
    warm_until = max(15, cycles // 7)     # ~30 for 200
    min_edge = 0.002  # Lowered for ranging/volatile (was 0.005)

    if not json_mode:
        header = (
            f"\n{'='*72}\n"
            f"  🎲 POLYMARKET ${bankroll:.0f} SIM — {cycles} CYCLES × 3 CONTRACTS\n"
            f"  Signal: RSI(7)/MACD(6/13) → 12-dim features → Bayesian\n"
            f"  Sizing: 2% fixed(cold:{cold_until}) → Floor(cold→warm:{warm_until}) → Full Kelly\n"
            f"  4 regimes × {cycles//4} cycles each\n"
            f"{'='*72}"
        )
        print(header)
        print(f"  {'#':>4} {'Regime':>14} {'Sig':>5} {'Dir':>4} {'Edge':>7} "
              f"{'CalP':>7} {'Bet':>6} {'P&L':>8} {'Cap':>9}  {'Sz'}")
        print(f"  {'─'*86}")

    regimes_cycle = ["trending_up", "ranging", "trending_down", "volatile"]
    cycles_per_regime = cycles // 4

    for i in range(cycles):
        regime_block = i // cycles_per_regime
        reg = regimes_cycle[min(regime_block, 3)]

        # Regime transition marker
        if reg != prev_regime and not json_mode and verbose:
            print(f"  {'─'*86}")
            print(f"  >>> REGIME: {reg.upper()} (cycles {i+1}–{min(i+cycles_per_regime, cycles)})")
            print(f"  {'─'*86}")
        prev_regime = reg

        prices = btc_walk(btc, 60, reg)
        btc = prices[-1]
        sig = btc_signal(prices)

        for j in range(3):
            ct = random.choice(["above", "below"])
            con = mk_contract(btc, ct, seed=i * 300 + j * 100 + 7)

            if sig["direction"] == "neutral" or sig["confidence"] < 0.12:
                continue

            d = sig["direction"]
            if d == "up" and ct != "above": continue
            if d == "down" and ct != "below": continue

            entry_price = con["yes_price"] if d == "up" else con["no_price"]
            if entry_price < 0.05 or entry_price > 0.85:
                continue
            trad_edge = sig["confidence"] - entry_price
            if trad_edge < 0.005:
                continue

            # Bayesian
            fv = enc.encode(
                prices, con["yes_price"], con["no_price"],
                con["volume"], con["hours_to_resolution"],
            )
            res = cal.predict(fv)
            cp, cert, cf = res["probability"], res["certainty"], cal.calibration_factor

            # Blend
            cal_edge = cp - entry_price if d == "up" else (1 - cp) - entry_price
            bayes_w = min(0.30, cal.updates / 50)
            edge = trad_edge * (1 - bayes_w) + cal_edge * bayes_w

            if edge < min_edge:
                continue

            # Size
            odds = 1.0 - entry_price
            bet = size(edge, odds, cap, cf, cert, cal.updates,
                       multiplier=1.5, cold_until=cold_until, warm_until=warm_until)
            if bet < 1.0 or bet > cap:
                continue

            # Execute
            cap -= bet; n += 1
            direction = "YES" if d == "up" else "NO"

            if reg == "trending_up":
                fm = random.uniform(0.005, 0.06)
            elif reg == "trending_down":
                fm = random.uniform(-0.06, -0.005)
            elif reg == "volatile":
                fm = random.uniform(-0.05, 0.05)
            else:
                fm = random.uniform(-0.02, 0.02)

            fbtc = btc * (1 + fm); out = resolve(con, fbtc)
            won = (direction == "YES" and out == 1) or (direction == "NO" and out == 0)
            pnl = bet / entry_price - bet if won else -bet
            cap += bet + pnl; pnl_t += pnl; peak = max(peak, cap)
            if won: w += 1
            else:   l += 1

            # Bayesian learn
            cal_out = out if direction == "YES" else 1 - out
            cal.update(fv, cal_out)

            updates = cal.updates
            if updates <= cold_until:
                sz_label = "cold"
            elif updates <= warm_until:
                sz_label = "warm"
            else:
                sz_label = "live"

            if not json_mode:
                print(
                    f"  {n:>4} {reg:>14} {d:>5} {direction:>4} {edge:>+7.4f} "
                    f"{cp:>7.4f} ${bet:>5.2f} ${pnl:>+7.2f} ${cap:>8.2f}  {sz_label}"
                )

            log.append({
                "trade": n, "regime": reg, "signal_dir": d, "side": direction,
                "pnl": pnl, "won": won, "bet": bet, "edge": edge,
                "entry_price": entry_price, "sizing_phase": sz_label,
            })

    # ── Final metrics ────────────────────────────────────────────────────────
    s = cal.stats()
    wr = w / max(n, 1) * 100
    dd_pct = (peak - cap) / peak * 100 if peak > 0 else 0

    gw = sum(t["pnl"] for t in log if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in log if t["pnl"] < 0))
    pf = gw / max(gl, 0.01)

    rets = [t["pnl"] / bankroll for t in log]
    sh = (np.mean(rets) / max(np.std(rets), 1e-9)) * np.sqrt(n) if n > 1 else 0

    avg_win = gw / max(w, 1)
    avg_loss = -gl / max(l, 1)
    green_trades = sum(1 for t in log if t["pnl"] > 0)

    # By-regime breakdown
    rs = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0})
    for t in log:
        r = t["regime"]; rs[r]["n"] += 1; rs[r]["pnl"] += t["pnl"]
        if t["won"]: rs[r]["w"] += 1

    # Gate assessment
    gate_sharpe = sh > 1.5
    gate_wr = wr > 55
    gate_pf = pf > 1.5
    gate_dd = dd_pct < 8
    gate_green = green_trades >= 7
    gates_passed = sum([gate_sharpe, gate_wr, gate_pf, gate_dd, gate_green])

    result = {
        "timestamp": datetime.now().isoformat(),
        "cycles": cycles,
        "regimes_swept": len([r for r, d in rs.items() if d["n"] > 0]),
        "trades": n, "wins": w, "losses": l,
        "win_rate_pct": round(wr, 1),
        "capital_final": round(cap, 2), "capital_start": bankroll,
        "pnl_total": round(pnl_t, 2), "pnl_pct": round(pnl_t / bankroll * 100, 1),
        "drawdown_pct": round(dd_pct, 1),
        "sharpe_like": round(sh, 2), "profit_factor": round(pf, 2),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "brier_score": round(s["brier_score"], 4),
        "calibration_factor": round(s["calibration_factor"], 4),
        "learning_rate": s["learning_rate"],
        "sizer_config": {"cold_until": cold_until, "warm_until": warm_until,
                         "min_edge": min_edge},
        "regime_breakdown": {
            r: {"trades": d["n"], "pnl": round(d["pnl"], 2),
                "win_rate": round(d["w"] / max(d["n"], 1) * 100, 1)}
            for r, d in sorted(rs.items())
        },
        "gates": {
            "sharpe_gt_1.5": gate_sharpe, "win_rate_gt_55pct": gate_wr,
            "profit_factor_gt_1.5": gate_pf, "drawdown_lt_8pct": gate_dd,
            "green_trades_gte_7": gate_green,
            "passed": gates_passed, "total": 5,
        },
        "beta_top5": {f"b{i}": round(v, 4) for i, v in
                       sorted(enumerate(s["beta_coefficients"]),
                              key=lambda x: abs(x[1]), reverse=True)[:5]},
    }

    if json_mode:
        print(json.dumps(result, indent=2))
        return result

    # ── Human-readable summary ───────────────────────────────────────────────
    print(f"\n  {'='*72}")
    print(f"  📊 FINAL — {cycles} cycles, {n} trades across {len(rs)} regimes")
    print(f"  Capital: ${cap:,.2f} | P&L: ${pnl_t:+,.2f} ({pnl_t/bankroll*100:+.1f}%)")
    print(f"  Win rate: {wr:.1f}% ({w}W/{l}L) | DD: {dd_pct:.1f}% | Sharpe: {sh:.2f}")
    print(f"  Profit factor: {pf:.2f} | Avg win: ${avg_win:.2f} | Avg loss: ${avg_loss:.2f}")
    print(f"  Bayesian: Brier={s['brier_score']:.4f} | Cal={s['calibration_factor']:.2%} | LR={s['learning_rate']:.6f}")

    print(f"\n  🚦 LIVE GATES ({gates_passed}/5):")
    print(f"     Sharpe >1.5:          {'✅' if gate_sharpe else '❌'} ({sh:.2f})")
    print(f"     Win rate >55%:        {'✅' if gate_wr else '❌'} ({wr:.1f}%)")
    print(f"     Profit factor >1.5:   {'✅' if gate_pf else '❌'} ({pf:.2f})")
    print(f"     Drawdown <8%:         {'✅' if gate_dd else '❌'} ({dd_pct:.1f}%)")
    print(f"     Green trades ≥7:      {'✅' if gate_green else '❌'} ({green_trades})")

    print(f"\n  By Regime:")
    for r, d in sorted(rs.items()):
        wr_r = d["w"] / max(d["n"], 1) * 100
        bar = "█" * int(wr_r / 10) + "░" * (10 - int(wr_r / 10))
        print(f"  {r:<15} {d['n']:>3} tr  P&L ${d['pnl']:>+7.2f}  WR {wr_r:>5.0f}%  [{bar}]")

    if n == 0:
        print(f"\n  ⚠  Zero trades. min_edge={min_edge} may be too high for {cycles} cycles.")
    print(f"\n  ⚠  PAPER ONLY — NO REAL MONEY.\n")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cycles = 200; seed = 42; bankroll = 200.0
    json_mode = False; verbose = False
    args = sys.argv[1:]

    for i, arg in enumerate(args):
        if arg == "--cycles" and i + 1 < len(args):
            cycles = int(args[i + 1])
        elif arg == "--seed" and i + 1 < len(args):
            seed = int(args[i + 1])
        elif arg == "--bankroll" and i + 1 < len(args):
            bankroll = float(args[i + 1])
        elif arg == "--json":
            json_mode = True
        elif arg == "--verbose" or arg == "-v":
            verbose = True

    simulate(cycles=cycles, bankroll=bankroll, seed=seed,
             json_mode=json_mode, verbose=verbose)
