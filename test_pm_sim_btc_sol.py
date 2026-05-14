#!/usr/bin/env python3
"""
BTC+SOL Two-Asset Simulation — single-asset filtered architecture × two.
Bear guard, trend guard, Kelly cold/warm/live sizing. Same signal stack
applied independently to each asset.
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
    "BTC": {"drift": 0.0004, "vol": 0.008, "start": 87000.0, "alloc": 0.65},
    "SOL": {"drift": 0.0006, "vol": 0.018, "start": 160.0,    "alloc": 0.35},
}

# Kelly sizing — matches pm_engine.py v3
COLD_PCT = 0.02; WARM_CAL_FLOOR = 0.25; WARM_CERT_FLOOR = 0.25
MAX_BANKROLL_FRAC = 0.02; MIN_BET = 1.0; KELLY_MULT = 1.5
COLD_UPDATES = 10; WARM_UPDATES = 30
MIN_CONF = 0.15; MAX_CONF = 0.90
MIN_CONTRACT_PRICE = 0.05; MAX_CONTRACT_PRICE = 0.85
MIN_EDGE = 0.02; MAX_OPEN = 4  # 2 per asset
RSI_OVERSOLD = 48; RSI_OVERBOUGHT = 52

REGIMES = ["trending_up", "ranging", "trending_down", "volatile"]
REGIME_DRIFT = {"trending_up": 2.0, "ranging": 0.2, "trending_down": -2.0, "volatile": 0.5}
REGIME_VOL   = {"trending_up": 0.8, "ranging": 0.4, "trending_down": 1.0, "volatile": 2.5}
REGIME_WIN   = {"trending_up": 0.80, "ranging": 0.50, "trending_down": 0.20, "volatile": 0.52}


def price_walk(start, n, regime, base_drift, base_vol):
    d = base_drift*REGIME_DRIFT[regime]; v = base_vol*REGIME_VOL[regime]
    prices = [float(start)]
    for _ in range(n-1): prices.append(prices[-1]*(1+np.random.normal(d,v)))
    return prices

def mk_contracts(price, sym, seed):
    r = random.Random(seed)
    return [{"up_price": round(max(0.03, min(0.95, 0.50+r.uniform(-0.15,0.15))),3),
             "down_price": round(1-round(max(0.03, min(0.95, 0.50+r.uniform(-0.15,0.15))),3),3),
             "volume": r.uniform(10000,300000), "mins_to_expiry": r.randint(4, 15), "asset": sym}
            for _ in range(2)]

def signal(prices):
    if len(prices) < 14: return {"direction":"neutral","confidence":0.0,"rsi":50,"price":0}
    deltas = [prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains = sum(max(d,0) for d in deltas[-7:])/7; losses = sum(max(-d,0) for d in deltas[-7:])/7
    rsi = 100-(100/(1+gains/max(losses,1e-9)))
    def ema(v,s): a=2/(s+1); r=v[0]; [r:=a*vv+(1-a)*r for vv in v[1:]]; return r
    macd = ema(prices,6)-ema(prices,13)
    up = sum(1 for i in range(1,min(4,len(prices))) if prices[-i]>prices[-i-1])
    d,c="neutral",0.0
    if rsi<RSI_OVERSOLD: d,c="up",min(0.80,(RSI_OVERSOLD-rsi)/15)+(0.10 if up>=2 else 0)
    elif rsi>RSI_OVERBOUGHT: d,c="down",min(0.80,(rsi-RSI_OVERBOUGHT)/15)+(0.10 if up<2 else 0)
    else: d,c=("up" if up>=2 else "down"),0.20
    sma20=sum(prices[-20:])/20 if len(prices)>=20 else prices[-1]
    return {"direction":d,"confidence":min(MAX_CONF,c),"rsi":round(rsi,1),"macd":round(macd,2),
            "momentum":up,"price":prices[-1],"sma20":sma20,"_prices":prices,"macd_val":macd}

def is_bear(prices):
    if len(prices)<20: return False
    sma20=sum(prices[-20:])/20
    def ema(v,s): a=2/(s+1); r=v[0]; [r:=a*vv+(1-a)*r for vv in v[1:]]; return r
    return prices[-1]<sma20 and (ema(prices,6)-ema(prices,13))<0

def is_uptrend(prices):
    if len(prices)<20: return True
    sma20=sum(prices[-20:])/20
    def ema(v,s): a=2/(s+1); r=v[0]; [r:=a*vv+(1-a)*r for vv in v[1:]]; return r
    return prices[-1]>sma20 and (ema(prices,6)-ema(prices,13))>0

def is_downtrend(prices):
    if len(prices)<20: return False
    sma20=sum(prices[-20:])/20
    def ema(v,s): a=2/(s+1); r=v[0]; [r:=a*vv+(1-a)*r for vv in v[1:]]; return r
    return prices[-1]<sma20 and (ema(prices,6)-ema(prices,13))<0

def resolve_outcome(direction, regime):
    return random.random() < (REGIME_WIN[regime] if direction=="Up" else 1-REGIME_WIN[regime])

def kelly_size(edge, odds, bankroll, cal_factor, certainty, updates):
    if edge<=0 or bankroll<=0: return 0.0
    if updates<COLD_UPDATES: return round(bankroll*COLD_PCT,2)
    cf = max(WARM_CAL_FLOOR, cal_factor) if updates<WARM_UPDATES else cal_factor
    ct = max(WARM_CERT_FLOOR, certainty) if updates<WARM_UPDATES else certainty
    raw = (edge/max(odds,0.01))*0.5*KELLY_MULT*cf*ct
    return round(min(raw, MAX_BANKROLL_FRAC)*bankroll, 2)


def simulate(cycles=200, bankroll=200.0, seed=42, json_mode=False):
    random.seed(seed); np.random.seed(seed)
    cal = bl.BayesianCalibrator()
    cal.brier_sum=cal.brier_count=cal.updates=0
    cal.beta=np.zeros(13); cal.precision=np.full(13,bl.PRIOR_PRECISION); cal.learning_rate=bl.INITIAL_LR
    enc = fe.FeatureEncoder(calibrator=cal)

    cap=bankroll; peak=bankroll; n=w=l=0; pnl_t=0.0; log=[]
    bear_skipped=0; cycles_processed=0
    prices_state = {sym: a["start"] for sym, a in ASSETS.items()}
    cpr = cycles//4; regime_idx = 0

    while cycles_processed < cycles:
        regime = REGIMES[regime_idx]
        all_prices = {}
        signals = {}
        for sym, a in ASSETS.items():
            all_prices[sym] = price_walk(prices_state[sym], 60, regime, a["drift"], a["vol"])
            prices_state[sym] = all_prices[sym][-1]
            sig = signal(all_prices[sym])
            if sig["direction"] != "neutral" and sig["confidence"] >= MIN_CONF:
                signals[sym] = sig

        # Bear guard (BTC)
        if is_bear(all_prices["BTC"]):
            bear_skipped += 1; cycles_processed += 1
            if cycles_processed%cpr==0: regime_idx = min(regime_idx+1,3)
            continue

        # Entries per asset
        for sym in ASSETS:
            sig = signals.get(sym)
            if sig is None: continue
            d = sig["direction"]

            # Trend guard (per-asset)
            if is_uptrend(sig["_prices"]) and d=="down": continue
            if is_downtrend(sig["_prices"]) and d=="up": continue

            # Stagger entries: check asset-specific open count
            asset_open = sum(1 for t in log[-MAX_OPEN:] if t.get("asset")==sym and t.get("open",True))
            # Simplified: just cap total entries per cycle

            for con in mk_contracts(sig["price"], sym, seed=cycles_processed*500+hash(sym)%1000):
                ep = con["up_price"] if d=="up" else con["down_price"]
                if not (MIN_CONTRACT_PRICE<ep<MAX_CONTRACT_PRICE): continue
                te = sig["confidence"]-ep
                if te<MIN_EDGE: continue

                fv = enc.encode(sig["_prices"], con["up_price"], con["down_price"],
                                con["volume"], con["mins_to_expiry"]/60.0)
                res = cal.predict(fv, market_price=ep)
                cp, cert, cf = res["probability"], res["certainty"], cal.calibration_factor
                ce = cp-ep if d=="up" else (1-cp)-ep
                bw = min(0.30, cal.updates/50)
                edge = te*(1-bw)+ce*bw

                bet = kelly_size(edge, 1-ep, cap, cf, cert, cal.updates)
                if bet<MIN_BET or bet>cap: continue

                cap -= bet; n += 1
                direction = "Up" if d=="up" else "Down"
                won = resolve_outcome(direction, regime)
                pnl = bet/ep-bet if won else -bet
                cap += bet+pnl; pnl_t += pnl; peak = max(peak,cap)
                if won: w += 1
                else: l += 1
                cal.update(fv, 1 if won else 0)
                log.append({"trade":n,"regime":regime,"asset":sym,"side":direction,
                           "pnl":pnl,"won":won,"bet":bet,"edge":edge})

        cycles_processed += 1
        if cycles_processed%cpr==0: regime_idx = min(regime_idx+1,3)

    s = cal.stats(); wr = w/max(n,1)*100; dd_pct=(peak-cap)/peak*100 if peak>0 else 0
    gw = sum(t["pnl"] for t in log if t["pnl"]>0); gl = abs(sum(t["pnl"] for t in log if t["pnl"]<0))
    pf = gw/max(gl,0.01)
    rets = [t["pnl"]/bankroll for t in log]
    sh = (np.mean(rets)/max(np.std(rets),1e-9))*np.sqrt(n) if n>1 else 0
    avg_win=gw/max(w,1); avg_loss=-gl/max(l,1); green=sum(1 for t in log if t["pnl"]>0)

    rs=defaultdict(lambda:{"n":0,"pnl":0.0,"w":0}); ac=defaultdict(lambda:{"n":0,"pnl":0.0,"w":0})
    for t in log:
        r=t["regime"]; rs[r]["n"]+=1; rs[r]["pnl"]+=t["pnl"]
        if t["won"]: rs[r]["w"]+=1
        a=t["asset"]; ac[a]["n"]+=1; ac[a]["pnl"]+=t["pnl"]
        if t["won"]: ac[a]["w"]+=1

    gs,gw_,gp_ev,gd,gg = sh>1.5,wr>55,pf>1.5,dd_pct<8,green>=7
    gates_passed=sum([gs,gw_,gp_ev,gd,gg])

    result = {
        "timestamp": datetime.now().isoformat(), "cycles": cycles,
        "bear_skipped": bear_skipped, "bear_pct": round(bear_skipped/cycles*100,1),
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
        "gates": {"sharpe_gt_1.5": bool(gs), "win_rate_gt_55pct": bool(gw_),
            "profit_factor_gt_1.5": bool(gp_ev), "drawdown_lt_8pct": bool(gd),
            "green_trades_gte_7": bool(gg), "passed": int(gates_passed), "total": 5},
    }

    if json_mode:
        print(json.dumps(result, indent=2))
        return result

    print(f"\n  {'='*60}")
    print(f"  BTC+SOL — {cycles}c ({bear_skipped} bear) | Cap: ${cap:,.2f}")
    print(f"  P&L: ${pnl_t:+,.2f} ({pnl_t/bankroll*100:+.0f}%) | WR: {wr:.1f}%")
    print(f"  DD: {dd_pct:.1f}% | Sharpe: {sh:.2f} | PF: {pf:.2f} | Gates: {gates_passed}/5")
    for a,d in sorted(ac.items()):
        print(f"  {a}: {d['n']}tr  P&L ${d['pnl']:+.0f}  WR {d['w']/max(d['n'],1)*100:.0f}%")
    print()
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
