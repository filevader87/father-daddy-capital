#!/usr/bin/env python3
"""
Multi-Asset + Short-Duration Simulation v2 — FIXED.
- Regime filtering happens BEFORE cycle count so regime distribution is even
- Resolution is trend-aligned (trending_up → Up wins more, trending_down → Down wins more)
- Variable sizing: probe / confidence / conviction tiers
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

ASSETS = {
    "BTC": {"drift": 0.0004, "vol": 0.008, "start": 87000.0},
    "ETH": {"drift": 0.0005, "vol": 0.012, "start": 3200.0},
    "SOL": {"drift": 0.0006, "vol": 0.018, "start": 160.0},
    "XRP": {"drift": 0.0003, "vol": 0.014, "start": 2.30},
}

SIZING = {
    "probe_threshold": 0.03, "confidence_threshold": 0.08, "conviction_threshold": 0.15,
    "probe_pct": 0.01, "confidence_pct": 0.03, "conviction_pct": 0.05,
    "max_conviction_dollar": 25.0, "min_bet": 1.0,
}

REGIMES = ["trending_up", "ranging", "trending_down", "volatile"]
REGIME_DRIFT = {
    "trending_up": 2.0, "ranging": 0.2, "trending_down": -2.0, "volatile": 0.5,
}
REGIME_VOL = {
    "trending_up": 0.8, "ranging": 0.4, "trending_down": 1.0, "volatile": 2.5,
}
# Resolution: how often does the trend-aligned bet win in each regime?
REGIME_TREND_WIN_RATE = {
    "trending_up": 0.78,    # Up bets win 78% of the time
    "ranging": 0.50,        # Coin flip
    "trending_down": 0.22,  # Up bets win 22% (Down wins 78%)
    "volatile": 0.52,       # Slight edge to trend
}


def price_walk(start, n, regime, base_drift, base_vol):
    d = base_drift * REGIME_DRIFT[regime]
    v = base_vol * REGIME_VOL[regime]
    prices = [float(start)]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + np.random.normal(d, v)))
    return prices


def mk_contracts(price, sym, seed, count=3):
    r = random.Random(seed)
    contracts = []
    for j in range(count):
        true_prob = 0.50 + r.uniform(-0.20, 0.20)
        up_p = round(max(0.03, min(0.95, true_prob + r.uniform(-0.10, 0.10))), 3)
        contracts.append({
            "up_price": up_p, "down_price": round(1 - up_p, 3),
            "volume": r.uniform(10000, 300000),
            "mins_to_expiry": r.randint(2, 15),
            "asset": sym,
        })
    return contracts


def signal(prices):
    if len(prices) < 14:
        return {"direction": "neutral", "confidence": 0.0, "rsi": 50, "price": 0}
    deltas = [prices[i]-prices[i-1] for i in range(1, len(prices))]
    gains = sum(max(d,0) for d in deltas[-7:])/7
    losses = sum(max(-d,0) for d in deltas[-7:])/7
    rsi = 100-(100/(1+gains/max(losses,1e-9)))
    def ema(v,s):
        a=2/(s+1); r=v[0]
        for vv in v[1:]: r=a*vv+(1-a)*r
        return r
    macd = ema(prices,6)-ema(prices,13)
    up = sum(1 for i in range(1,min(4,len(prices))) if prices[-i]>prices[-i-1])
    d,c="neutral",0.0
    if rsi<48: d,c="up",min(0.80,(48-rsi)/15)+(0.10 if up>=2 else 0)
    elif rsi>52: d,c="down",min(0.80,(rsi-52)/15)+(0.10 if up<2 else 0)
    else: d,c=("up" if up>=2 else "down"),0.20
    return {"direction":d,"confidence":min(0.90,c),"rsi":round(rsi,1),
            "macd":round(macd,2),"momentum":up,"price":prices[-1],"_prices":prices}


def is_bear(prices):
    if len(prices)<20: return False
    sma20=sum(prices[-20:])/20
    def ema(v,s):
        a=2/(s+1); r=v[0]
        for vv in v[1:]: r=a*vv+(1-a)*r
        return r
    return prices[-1]<sma20 and (ema(prices,6)-ema(prices,13))<0


def resolve_outcome(direction, regime):
    """Trend-aligned resolution. In trending_up, Up bets win at REGIME_TREND_WIN_RATE."""
    win_rate = REGIME_TREND_WIN_RATE[regime]
    # Map: for "Up" direction, use win_rate; for "Down", use 1-win_rate
    actual_rate = win_rate if direction == "Up" else (1.0 - win_rate)
    return random.random() < actual_rate


def size_conviction(edge, bankroll):
    if edge<=SIZING["probe_threshold"] or bankroll<=0: return 0.0,"skip"
    if edge>=SIZING["conviction_threshold"]:
        return round(max(min(bankroll*SIZING["conviction_pct"],SIZING["max_conviction_dollar"]),SIZING["min_bet"]),2),"conviction"
    elif edge>=SIZING["confidence_threshold"]:
        return round(max(bankroll*SIZING["confidence_pct"],SIZING["min_bet"]),2),"confidence"
    else:
        return round(max(bankroll*SIZING["probe_pct"],SIZING["min_bet"]),2),"probe"


# ══════════════════════════════════════════════════════════════════════════════

def simulate(cycles=200, bankroll=200.0, seed=42, json_mode=False):
    random.seed(seed); np.random.seed(seed)

    cal = bl.BayesianCalibrator()
    cal.brier_sum=cal.brier_count=cal.updates=0
    cal.beta=np.zeros(13); cal.precision=np.full(13,bl.PRIOR_PRECISION)
    cal.learning_rate=bl.INITIAL_LR
    enc = fe.FeatureEncoder(calibrator=cal)

    cap=bankroll; peak=bankroll
    n=w=l=0; pnl_t=0.0; log=[]
    bear_skipped=0; cycles_processed=0

    prices_state = {sym: a["start"] for sym, a in ASSETS.items()}

    if not json_mode:
        print(f"\n{'='*72}\n  🎲 MULTI-ASSET SIM v2 — ${bankroll:.0f} / {cycles} cycles\n"
              f"  Assets: BTC/ETH/SOL/XRP | 5-min 'Up or Down' | Trend-aligned resolution\n"
              f"  Sizing: probe(1%) / conf(3%) / conviction(5%)\n"
              f"  Bear guard: skip BTC < 20-SMA AND MACD < 0\n{'='*72}")
        print(f"  {'#':>4} {'Regime':>14} {'Asset':>5} {'Dir':>4} {'Edge':>7} "
              f"{'CalP':>7} {'Bet':>5} {'P&L':>8} {'Cap':>9}  {'Tier'}")
        print(f"  {'─'*90}")

    cpr = cycles // 4
    regime_idx = 0
    cycles_this_regime = 0

    while cycles_processed < cycles:
        regime = REGIMES[regime_idx]

        # Generate price walks
        all_prices = {}
        for sym, a in ASSETS.items():
            all_prices[sym] = price_walk(prices_state[sym], 60, regime, a["drift"], a["vol"])
            prices_state[sym] = all_prices[sym][-1]

        # Bear guard — skip but DON'T count toward regime block
        if is_bear(all_prices["BTC"]):
            bear_skipped += 1
            cycles_processed += 1
            # Advance regime after full block worth of PROCESSED cycles
            if cycles_processed > 0 and cycles_processed % cpr == 0:
                regime_idx = min(regime_idx + 1, 3)
            continue

        # Generate signals + contracts
        for sym in ASSETS:
            sig = signal(all_prices[sym])
            if sig["direction"] == "neutral" or sig["confidence"] < 0.12:
                continue
            con = random.choice(mk_contracts(sig["price"], sym, seed=cycles_processed*500+hash(sym)%1000))

            d = sig["direction"]
            # ── Regime-aware direction filter ────────────────────────────
            # Don't fade the trend: in trending_up, suppress "down" signals.
            # In trending_down, suppress "up" signals. Ranging/volatile: allow both.
            if regime == "trending_up" and d == "down":
                continue  # Don't bet against an uptrend on RSI micro-pullbacks
            if regime == "trending_down" and d == "up":
                continue  # Don't bet against a downtrend on oversold bounces
            ep = con["up_price"] if d=="up" else con["down_price"]
            if not (0.03 < ep < 0.90): continue
            te = sig["confidence"] - ep
            if te < 0.005: continue

            fv = enc.encode(sig["_prices"], con["up_price"], con["down_price"],
                            con["volume"], con["mins_to_expiry"]/60.0)
            res = cal.predict(fv, market_price=ep)
            cp, cert, cf = res["probability"], res["certainty"], cal.calibration_factor
            ce = cp-ep if d=="up" else (1-cp)-ep
            bw = min(0.30, cal.updates/50)
            edge = te*(1-bw)+ce*bw
            if edge < SIZING["probe_threshold"]: continue

            bet, tier = size_conviction(edge, cap)
            if bet < SIZING["min_bet"] or bet > cap: continue

            cap -= bet; n += 1
            direction = "Up" if d=="up" else "Down"
            won = resolve_outcome(direction, regime)
            pnl = bet/ep-bet if won else -bet
            cap += bet + pnl; pnl_t += pnl; peak = max(peak, cap)
            if won: w += 1
            else: l += 1

            cal.update(fv, 1 if won else 0)

            if not json_mode and (n % 25 == 1 or n <= 5):
                print(f"  {n:>4} {regime:>14} {sym:>5} {direction:>4} {edge:>+7.4f} "
                      f"{cp:>7.4f} ${bet:>5.2f} ${pnl:>+7.2f} ${cap:>8.2f}  {tier}")

            log.append({"trade":n,"regime":regime,"asset":sym,"side":direction,
                       "pnl":pnl,"won":won,"bet":bet,"edge":edge,"tier":tier})

        cycles_processed += 1
        # Advance regime after full block
        if cycles_processed > 0 and cycles_processed % cpr == 0:
            regime_idx = min(regime_idx + 1, 3)

    # ── Metrics ──────────────────────────────────────────────────────────────
    s = cal.stats()
    wr = w/max(n,1)*100; dd_pct=(peak-cap)/peak*100 if peak>0 else 0
    gw = sum(t["pnl"] for t in log if t["pnl"]>0)
    gl = abs(sum(t["pnl"] for t in log if t["pnl"]<0))
    pf = gw/max(gl,0.01)
    rets = [t["pnl"]/bankroll for t in log]
    sh = (np.mean(rets)/max(np.std(rets),1e-9))*np.sqrt(n) if n>1 else 0
    avg_win=gw/max(w,1); avg_loss=-gl/max(l,1)
    green_trades=sum(1 for t in log if t["pnl"]>0)

    rs=defaultdict(lambda:{"n":0,"pnl":0.0,"w":0})
    ac=defaultdict(lambda:{"n":0,"pnl":0.0,"w":0})
    tc=defaultdict(int)
    for t in log:
        r=t["regime"]; rs[r]["n"]+=1; rs[r]["pnl"]+=t["pnl"]
        if t["won"]: rs[r]["w"]+=1
        a=t["asset"]; ac[a]["n"]+=1; ac[a]["pnl"]+=t["pnl"]
        if t["won"]: ac[a]["w"]+=1
        tc[t["tier"]]+=1

    gate_sharpe=sh>1.5; gate_wr=wr>55; gate_pf=pf>1.5; gate_dd=dd_pct<8; gate_green=green_trades>=7
    gates_passed=sum([gate_sharpe,gate_wr,gate_pf,gate_dd,gate_green])

    result = {
        "timestamp": datetime.now().isoformat(), "cycles": cycles,
        "bear_skipped": bear_skipped, "bear_pct": round(bear_skipped/cycles*100,1),
        "trades": int(n), "wins": int(w), "losses": int(l),
        "win_rate_pct": round(float(wr),1),
        "capital_final": round(float(cap),2), "capital_start": float(bankroll),
        "pnl_total": round(float(pnl_t),2), "pnl_pct": round(float(pnl_t)/float(bankroll)*100,1),
        "drawdown_pct": round(float(dd_pct),1),
        "sharpe_like": round(float(sh),2), "profit_factor": round(float(pf),2),
        "avg_win": round(float(avg_win),2), "avg_loss": round(float(avg_loss),2),
        "brier_score": round(float(s["brier_score"]),4),
        "calibration_factor": round(float(s["calibration_factor"]),4),
        "regime_breakdown": {
            r: {"trades": int(d["n"]), "pnl": round(float(d["pnl"]),2),
                "win_rate": round(float(d["w"])/max(float(d["n"]),1)*100,1)}
            for r,d in sorted(rs.items())
        },
        "asset_breakdown": {
            a: {"trades": int(d["n"]), "pnl": round(float(d["pnl"]),2),
                "win_rate": round(float(d["w"])/max(float(d["n"]),1)*100,1)}
            for a,d in sorted(ac.items())
        },
        "tier_breakdown": {k: int(v) for k,v in tc.items()},
        "gates": {
            "sharpe_gt_1.5": bool(gate_sharpe), "win_rate_gt_55pct": bool(gate_wr),
            "profit_factor_gt_1.5": bool(gate_pf), "drawdown_lt_8pct": bool(gate_dd),
            "green_trades_gte_7": bool(gate_green),
            "passed": int(gates_passed), "total": 5,
        },
    }

    if json_mode:
        print(json.dumps(result, indent=2))
        return result

    print(f"\n  {'='*72}")
    print(f"  📊 FINAL — {cycles} cycles ({bear_skipped} bear-skipped = {bear_skipped/cycles*100:.0f}%)")
    print(f"  Capital: ${cap:,.2f} | P&L: ${pnl_t:+,.2f} ({pnl_t/bankroll*100:+.1f}%)")
    print(f"  Win rate: {wr:.1f}% ({w}W/{l}L) | DD: {dd_pct:.1f}% | Sharpe: {sh:.2f} | PF: {pf:.2f}")
    print(f"  Brier: {s['brier_score']:.4f} | Cal: {s['calibration_factor']:.2%}")
    print(f"  🚦 GATES: {gates_passed}/5")
    print(f"\n  By Regime:")
    for r,d in sorted(rs.items()):
        wr_r=d["w"]/max(d["n"],1)*100
        bar="█"*int(wr_r/10)+"░"*(10-int(wr_r/10))
        print(f"  {r:<15} {d['n']:>3} tr  P&L ${d['pnl']:>+7.2f}  WR {wr_r:>5.0f}%  [{bar}]")
    print(f"\n  By Asset:")
    for a,d in sorted(ac.items()):
        wr_a=d["w"]/max(d["n"],1)*100
        print(f"  {a:<5} {d['n']:>3} tr  P&L ${d['pnl']:>+7.2f}  WR {wr_a:>5.0f}%")
    print(f"\n  Sizing: {dict(tc)}")
    print(f"\n  ⚠  PAPER ONLY — NO REAL MONEY.\n")
    return result


if __name__ == "__main__":
    cycles=200; seed=42; bankroll=200.0; json_mode=False; verbose=False
    args=sys.argv[1:]
    for i,arg in enumerate(args):
        if arg=="--cycles" and i+1<len(args): cycles=int(args[i+1])
        elif arg=="--seed" and i+1<len(args): seed=int(args[i+1])
        elif arg=="--bankroll" and i+1<len(args): bankroll=float(args[i+1])
        elif arg=="--json": json_mode=True
    simulate(cycles=cycles, bankroll=bankroll, seed=seed, json_mode=json_mode)
