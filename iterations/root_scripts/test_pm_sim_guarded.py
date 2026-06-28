#!/usr/bin/env python3
"""
Guarded Multi-Asset Simulation — matches pm_engine.py v2.1 with DD guardrails.
Drawdown-based sizing reduction + mid-window stop-loss.
"""
import sys, random, json, statistics
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

DD_GUARD = {
    "downgrade_dd": 0.05, "probe_only_dd": 0.08, "halt_dd": 0.12,
}

MID_WINDOW_STOP = {
    "enabled": True, "check_at_pct": 0.50, "loss_threshold_pct": -0.015,
}

REGIMES = ["trending_up", "ranging", "trending_down", "volatile"]
REGIME_DRIFT = {"trending_up": 2.0, "ranging": 0.2, "trending_down": -2.0, "volatile": 0.5}
REGIME_VOL = {"trending_up": 0.8, "ranging": 0.4, "trending_down": 1.0, "volatile": 2.5}
REGIME_TREND_WIN_RATE = {"trending_up": 0.78, "ranging": 0.50, "trending_down": 0.22, "volatile": 0.52}


def price_walk(start, n, regime, base_drift, base_vol):
    d = base_drift * REGIME_DRIFT[regime]; v = base_vol * REGIME_VOL[regime]
    prices = [float(start)]
    for _ in range(n - 1): prices.append(prices[-1] * (1 + np.random.normal(d, v)))
    return prices

def mk_contracts(price, sym, seed, count=3):
    r = random.Random(seed)
    return [{"up_price": round(max(0.03, min(0.95, 0.50+r.uniform(-0.20,0.20)+r.uniform(-0.10,0.10))),3),
             "down_price": round(1-round(max(0.03, min(0.95, 0.50+r.uniform(-0.20,0.20)+r.uniform(-0.10,0.10))),3),3),
             "volume": r.uniform(10000,300000), "mins_to_expiry": r.randint(2,15), "asset": sym}
            for j in range(count)]

def signal(prices):
    if len(prices) < 14: return {"direction":"neutral","confidence":0.0,"rsi":50,"price":0}
    deltas = [prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains = sum(max(d,0) for d in deltas[-7:])/7
    losses = sum(max(-d,0) for d in deltas[-7:])/7
    rsi = 100-(100/(1+gains/max(losses,1e-9)))
    def ema(v,s): a=2/(s+1); r=v[0]; [r:=a*vv+(1-a)*r for vv in v[1:]]; return r
    macd = ema(prices,6)-ema(prices,13)
    up = sum(1 for i in range(1,min(4,len(prices))) if prices[-i]>prices[-i-1])
    d,c="neutral",0.0
    if rsi<48: d,c="up",min(0.80,(48-rsi)/15)+(0.10 if up>=2 else 0)
    elif rsi>52: d,c="down",min(0.80,(rsi-52)/15)+(0.10 if up<2 else 0)
    else: d,c=("up" if up>=2 else "down"),0.20
    return {"direction":d,"confidence":min(0.90,c),"rsi":round(rsi,1),"macd":round(macd,2),
            "momentum":up,"price":prices[-1],"_prices":prices}

def is_bear(prices):
    if len(prices)<20: return False
    sma20=sum(prices[-20:])/20
    def ema(v,s): a=2/(s+1); r=v[0]; [r:=a*vv+(1-a)*r for vv in v[1:]]; return r
    return prices[-1]<sma20 and (ema(prices,6)-ema(prices,13))<0

def resolve_outcome(direction, regime):
    return random.random() < (REGIME_TREND_WIN_RATE[regime] if direction=="Up" else 1-REGIME_TREND_WIN_RATE[regime])

def size_conviction(edge, bankroll, dd=0.0):
    if edge<=SIZING["probe_threshold"] or bankroll<=0: return 0.0,"skip"
    if dd>=DD_GUARD["halt_dd"]: return 0.0,"halted"
    if dd>=DD_GUARD["probe_only_dd"]: return round(max(bankroll*SIZING["probe_pct"],SIZING["min_bet"]),2),"probe"
    dg = dd>=DD_GUARD["downgrade_dd"]
    if edge>=SIZING["conviction_threshold"]:
        if dg: return round(max(bankroll*SIZING["confidence_pct"],SIZING["min_bet"]),2),"confidence"
        return round(max(min(bankroll*SIZING["conviction_pct"],SIZING["max_conviction_dollar"]),SIZING["min_bet"]),2),"conviction"
    elif edge>=SIZING["confidence_threshold"]:
        if dg: return round(max(bankroll*SIZING["probe_pct"],SIZING["min_bet"]),2),"probe"
        return round(max(bankroll*SIZING["confidence_pct"],SIZING["min_bet"]),2),"confidence"
    return round(max(bankroll*SIZING["probe_pct"],SIZING["min_bet"]),2),"probe"


def simulate(cycles=200, bankroll=200.0, seed=42, json_mode=False):
    random.seed(seed); np.random.seed(seed)
    cal = bl.BayesianCalibrator()
    cal.brier_sum=cal.brier_count=cal.updates=0
    cal.beta=np.zeros(13); cal.precision=np.full(13,bl.PRIOR_PRECISION); cal.learning_rate=bl.INITIAL_LR
    enc = fe.FeatureEncoder(calibrator=cal)

    cap=bankroll; peak=bankroll
    n=w=l=0; pnl_t=0.0; log=[]; bear_skipped=0; cycles_processed=0; stopped_early=0
    open_positions = {}  # key → {bet, direction, entry_price, mins_to_expiry, entry_cycle}
    prices_state = {sym: a["start"] for sym, a in ASSETS.items()}

    cpr = cycles // 4; regime_idx = 0

    while cycles_processed < cycles:
        regime = REGIMES[regime_idx]
        all_prices = {}
        for sym, a in ASSETS.items():
            all_prices[sym] = price_walk(prices_state[sym], 60, regime, a["drift"], a["vol"])
            prices_state[sym] = all_prices[sym][-1]

        if is_bear(all_prices["BTC"]):
            bear_skipped += 1; cycles_processed += 1
            if cycles_processed%cpr==0: regime_idx = min(regime_idx+1,3)
            continue

        # ── Mid-window stop-loss check ───────────────────────────────────
        if MID_WINDOW_STOP["enabled"]:
            to_close = []
            for key, pos in list(open_positions.items()):
                elapsed = cycles_processed - pos["entry_cycle"]
                half_win = pos["mins_to_expiry"] * MID_WINDOW_STOP["check_at_pct"]
                if elapsed >= half_win:
                    current_price = prices_state[pos["asset"]]
                    move_pct = (current_price - pos["entry_price"]) / pos["entry_price"]
                    against = (pos["direction"] == "Up" and move_pct < MID_WINDOW_STOP["loss_threshold_pct"]) or \
                              (pos["direction"] == "Down" and move_pct > -MID_WINDOW_STOP["loss_threshold_pct"])
                    if against:
                        to_close.append(key)
            for key in to_close:
                pos = open_positions.pop(key)
                pnl = -pos["bet"]
                cap += pnl; pnl_t += pnl; peak = max(peak, cap); n += 1; l += 1
                stopped_early += 1
                log.append({"trade": n, "regime": regime, "asset": pos["asset"], "side": pos["direction"],
                           "pnl": pnl, "won": False, "bet": pos["bet"], "edge": 0, "tier": "stopped"})

        # ── DD computation ───────────────────────────────────────────────
        dd = max(0.0, (peak - cap) / peak) if peak > 0 else 0.0

        # ── Entries ──────────────────────────────────────────────────────
        for sym in ASSETS:
            sig = signal(all_prices[sym])
            if sig["direction"] == "neutral" or sig["confidence"] < 0.12: continue
            d = sig["direction"]

            if regime=="trending_up" and d=="down": continue
            if regime=="trending_down" and d=="up": continue

            con = random.choice(mk_contracts(sig["price"], sym, seed=cycles_processed*500+hash(sym)%1000, count=2))
            ep = con["up_price"] if d=="up" else con["down_price"]
            if not (0.03 < ep < 0.90): continue
            te = sig["confidence"] - ep
            if te < 0.005: continue

            fv = enc.encode(sig["_prices"], con["up_price"], con["down_price"], con["volume"], con["mins_to_expiry"]/60.0)
            res = cal.predict(fv, market_price=ep)

            bet, tier = size_conviction(te, cap, dd=dd)
            if bet < SIZING["min_bet"] or bet > cap: continue

            cap -= bet; n += 1
            direction = "Up" if d=="up" else "Down"
            won = resolve_outcome(direction, regime)
            pnl = bet/ep-bet if won else -bet
            cap += bet + pnl; pnl_t += pnl; peak = max(peak, cap)
            if won: w += 1
            else: l += 1
            cal.update(fv, 1 if won else 0)
            log.append({"trade": n, "regime": regime, "asset": sym, "side": direction,
                       "pnl": pnl, "won": won, "bet": bet, "edge": te, "tier": tier})

        cycles_processed += 1
        if cycles_processed%cpr==0: regime_idx = min(regime_idx+1,3)

    s = cal.stats()
    wr = w/max(n,1)*100; dd_pct=(peak-cap)/peak*100 if peak>0 else 0
    gw = sum(t["pnl"] for t in log if t["pnl"]>0); gl = abs(sum(t["pnl"] for t in log if t["pnl"]<0))
    pf = gw/max(gl,0.01)
    rets = [t["pnl"]/bankroll for t in log]
    sh = (np.mean(rets)/max(np.std(rets),1e-9))*np.sqrt(n) if n>1 else 0
    avg_win=gw/max(w,1); avg_loss=-gl/max(l,1); green_trades=sum(1 for t in log if t["pnl"]>0)
    rs=defaultdict(lambda:{"n":0,"pnl":0.0,"w":0}); ac=defaultdict(lambda:{"n":0,"pnl":0.0,"w":0}); tc=defaultdict(int)
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
        "bear_skipped": bear_skipped, "stopped_early": stopped_early,
        "trades": int(n), "wins": int(w), "losses": int(l),
        "win_rate_pct": round(float(wr),1),
        "capital_final": round(float(cap),2), "capital_start": float(bankroll),
        "pnl_total": round(float(pnl_t),2), "pnl_pct": round(float(pnl_t/bankroll)*100,1),
        "drawdown_pct": round(float(dd_pct),1),
        "sharpe_like": round(float(sh),2), "profit_factor": round(float(pf),2),
        "avg_win": round(float(avg_win),2), "avg_loss": round(float(avg_loss),2),
        "brier_score": round(float(s["brier_score"]),4),
        "calibration_factor": round(float(s["calibration_factor"]),4),
        "regime_breakdown": {r: {"trades": int(d["n"]), "pnl": round(float(d["pnl"]),2),
            "win_rate": round(float(d["w"])/max(float(d["n"]),1)*100,1)} for r,d in sorted(rs.items())},
        "asset_breakdown": {a: {"trades": int(d["n"]), "pnl": round(float(d["pnl"]),2),
            "win_rate": round(float(d["w"])/max(float(d["n"]),1)*100,1)} for a,d in sorted(ac.items())},
        "tier_breakdown": {k: int(v) for k,v in tc.items()},
        "gates": {"sharpe_gt_1.5": bool(gate_sharpe), "win_rate_gt_55pct": bool(gate_wr),
            "profit_factor_gt_1.5": bool(gate_pf), "drawdown_lt_8pct": bool(gate_dd),
            "green_trades_gte_7": bool(gate_green), "passed": int(gates_passed), "total": 5},
    }

    if json_mode:
        print(json.dumps(result, indent=2))
        return result

    print(f"\n  {'='*72}")
    print(f"  📊 GUARDED — {cycles} cycles ({bear_skipped} bear, {stopped_early} stopped)")
    print(f"  Capital: ${cap:,.2f} | P&L: ${pnl_t:+,.2f} ({pnl_t/bankroll*100:+.1f}%)")
    print(f"  WR: {wr:.1f}% ({w}W/{l}L) | DD: {dd_pct:.1f}% | Sharpe: {sh:.2f} | PF: {pf:.2f}")
    print(f"  🚦 GATES: {gates_passed}/5")
    print(f"  Sizing: {dict(tc)}")
    for r,d in sorted(rs.items()):
        wr_r=d["w"]/max(d["n"],1)*100
        print(f"  {r:<15} {d['n']:>3} tr  P&L ${d['pnl']:>+7.2f}  WR {wr_r:>5.0f}%")
    print(f"\n  ⚠  PAPER ONLY.\n")
    return result

if __name__ == "__main__":
    cycles=200; seed=42; bankroll=200.0; json_mode=False
    args=sys.argv[1:]
    for i,arg in enumerate(args):
        if arg=="--cycles" and i+1<len(args): cycles=int(args[i+1])
        elif arg=="--seed" and i+1<len(args): seed=int(args[i+1])
        elif arg=="--bankroll" and i+1<len(args): bankroll=float(args[i+1])
        elif arg=="--json": json_mode=True
    simulate(cycles=cycles, bankroll=bankroll, seed=seed, json_mode=json_mode)
