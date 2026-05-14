#!/usr/bin/env python3
"""
Regime-filtered simulation — same architecture, adds bear market guard.
Skips entries when BTC < 20-SMA AND MACD(6/13) < 0 (trending_down filter).
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
    return {"question": f"Bitcoin {ctype} ${st:,.0f} on May {r.randint(18,28)}",
            "conditionId": f"0x{seed:064x}", "yes_price": yp, "no_price": round(1-yp,3),
            "volume": r.uniform(15000, 200000), "strike": st,
            "contract_type": ctype, "hours_to_resolution": r.uniform(2, 72)}

def resolve(c, final_btc):
    return 1 if (c["contract_type"] == "above" and final_btc >= c["strike"]) or (c["contract_type"] == "below" and final_btc <= c["strike"]) else 0

def btc_signal(prices):
    if len(prices) < 14:
        return {"direction": "neutral", "confidence": 0.0, "rsi": 50, "price": 0}
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

def size(edge, odds, bankroll, cal_factor, certainty, updates, multiplier=1.5, cold_until=10, warm_until=30):
    if edge<=0 or bankroll<=0: return 0.0
    if updates<cold_until: return round(bankroll*0.02,2)
    cf = max(0.25,cal_factor) if updates<warm_until else cal_factor
    ct = max(0.25,certainty) if updates<warm_until else certainty
    return round(min((edge/max(odds,0.01))*0.5*multiplier*cf*ct, 0.02)*bankroll,2)

def is_bear_market(prices):
    """Regime filter: skip entries when BTC < 20-SMA AND MACD < 0"""
    if len(prices) < 20:
        return False
    sma20 = sum(prices[-20:])/20
    def ema(v,s):
        a=2/(s+1); r=v[0]
        for vv in v[1:]: r=a*vv+(1-a)*r
        return r
    macd = ema(prices,6)-ema(prices,13)
    return prices[-1] < sma20 and macd < 0

def simulate(cycles=200, bankroll=200.0, seed=42, json_mode=False, verbose=False):
    random.seed(seed); np.random.seed(seed)
    cal = bl.BayesianCalibrator()
    cal.brier_sum=cal.brier_count=cal.updates=0
    cal.beta=np.zeros(13); cal.precision=np.full(13,bl.PRIOR_PRECISION)
    cal.learning_rate=bl.INITIAL_LR
    enc = fe.FeatureEncoder(calibrator=cal)
    cap=bankroll; btc=87000.0; peak=bankroll
    n=w=l=0; pnl_t=0.0; log=[]; filtered=0
    cold_until=max(5,cycles//20); warm_until=max(15,cycles//7)

    if not json_mode:
        print(f"\n{'='*72}")
        print(f"  🎲 FILTERED SIM — ${bankroll:.0f} / {cycles} cycles")
        print(f"  Regime guard: BTC < 20-SMA AND MACD < 0 → skip")
        print(f"{'='*72}")
        print(f"  {'#':>4} {'Regime':>14} {'Sig':>5} {'Dir':>4} {'Edge':>7} "
              f"{'CalP':>7} {'Bet':>6} {'P&L':>8} {'Cap':>9}  {'Sz'}")
        print(f"  {'─'*86}")

    regimes_cycle = ["trending_up","ranging","trending_down","volatile"]
    cpr = cycles//4

    for i in range(cycles):
        rg = i//cpr; reg = regimes_cycle[min(rg,3)]
        prices = btc_walk(btc, 60, reg)
        btc = prices[-1]

        # ═══ REGIME FILTER ═══
        if is_bear_market(prices):
            filtered += 1
            continue

        sig = btc_signal(prices)
        for j in range(3):
            ct = random.choice(["above","below"])
            con = mk_contract(btc, ct, seed=i*300+j*100+7)
            if sig["direction"]=="neutral" or sig["confidence"]<0.12: continue
            d = sig["direction"]
            if (d=="up" and ct!="above") or (d=="down" and ct!="below"): continue
            ep = con["yes_price"] if d=="up" else con["no_price"]
            if ep<0.05 or ep>0.85: continue
            te = sig["confidence"]-ep
            if te<0.005: continue
            fv = enc.encode(prices, con["yes_price"], con["no_price"], con["volume"], con["hours_to_resolution"])
            res = cal.predict(fv, market_price=ep)
            cp, cert, cf = res["probability"], res["certainty"], cal.calibration_factor
            ce = cp-ep if d=="up" else (1-cp)-ep
            bw = min(0.30, cal.updates/50)
            edge = te*(1-bw)+ce*bw
            if edge<0.002: continue
            odds = 1.0-ep
            bet = size(edge, odds, cap, cf, cert, cal.updates, multiplier=1.5, cold_until=cold_until, warm_until=warm_until)
            if bet<1.0 or bet>cap: continue
            cap-=bet; n+=1
            direc = "YES" if d=="up" else "NO"
            fbm = {"trending_up": (0.005,0.06), "trending_down": (-0.06,-0.005),
                   "volatile": (-0.05,0.05), "ranging": (-0.02,0.02)}
            lo,hi = fbm.get(reg,(0.0,0.0))
            fbtc = btc*(1+random.uniform(lo,hi))
            out = resolve(con, fbtc)
            won = (direc=="YES" and out==1) or (direc=="NO" and out==0)
            pnl = bet/ep-bet if won else -bet
            cap+=bet+pnl; pnl_t+=pnl; peak=max(peak,cap)
            if won: w+=1
            else: l+=1
            cal_out = out if direc=="YES" else 1-out
            cal.update(fv, cal_out)
            sz_label = "cold" if cal.updates<=cold_until else ("warm" if cal.updates<=warm_until else "live")
            if not json_mode:
                print(f"  {n:>4} {reg:>14} {d:>5} {direc:>4} {edge:>+7.4f} "
                      f"{cp:>7.4f} ${bet:>5.2f} ${pnl:>+7.2f} ${cap:>8.2f}  {sz_label}")
            log.append({"trade":n,"regime":reg,"signal_dir":d,"side":direc,
                        "pnl":pnl,"won":won,"bet":bet,"edge":edge,
                        "entry_price":ep,"sizing_phase":sz_label})

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
    for t in log:
        r=t["regime"]; rs[r]["n"]+=1; rs[r]["pnl"]+=t["pnl"]
        if t["won"]: rs[r]["w"]+=1

    gate_sharpe=sh>1.5; gate_wr=wr>55; gate_pf=pf>1.5; gate_dd=dd_pct<8; gate_green=green_trades>=7
    gates_passed=sum([gate_sharpe,gate_wr,gate_pf,gate_dd,gate_green])

    result = {
        "timestamp": datetime.now().isoformat(), "cycles": cycles,
        "cycles_filtered": filtered, "filter_pct": round(filtered/cycles*100,1),
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
    print(f"  📊 FINAL — {cycles} cycles ({filtered} bear-filtered, {filtered/cycles*100:.0f}%)")
    print(f"  Capital: ${cap:,.2f} | P&L: ${pnl_t:+,.2f} ({pnl_t/bankroll*100:+.1f}%)")
    print(f"  Win rate: {wr:.1f}% ({w}W/{l}L) | DD: {dd_pct:.1f}% | Sharpe: {sh:.2f} | PF: {pf:.2f}")
    print(f"  🚦 GATES: {gates_passed}/5")
    for r,d in sorted(rs.items()):
        wr_r = d["w"]/max(d["n"],1)*100
        print(f"  {r:<15} {d['n']:>3} tr  P&L ${d['pnl']:>+7.2f}  WR {wr_r:>5.0f}%")
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
        elif arg in ("--verbose","-v"): verbose=True
    simulate(cycles=cycles, bankroll=bankroll, seed=seed, json_mode=json_mode, verbose=verbose)
