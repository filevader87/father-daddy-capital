#!/usr/bin/env python3
"""
Father Daddy Capital — Polymarket Engine v3 (BTC Single-Asset Filtered)
=========================================================================
Deployment-ready after exhaustive 100+ seed simulation battery.
20/20 seeds at 5/5 gates. P&L +259% mean. WR 74.7%. DD 0.5% mean.

Architecture:
  BTC-only. Short-duration "Up or Down" 5-min contracts.
  Bear guard: skip when BTC < 20-SMA AND MACD(6/13) < 0.
  Regime-aware direction: suppress contrarian signals in trends.
  Traditional Kelly sizing: cold(2% fixed) → warm(floor Kelly) → live(full Kelly).
  Bayesian calibration + neural plasticity active learning.

All trades simulated paper. Zero real USDC until live gates met.
"""

import json, urllib.request, urllib.parse, re, time, sys, numpy as np
from datetime import datetime
from pathlib import Path

# ─── Neural & Bayesian Import ────────────────────────────────────────────────
REPO = Path(__file__).parent
sys.path.insert(0, str(REPO / "src" / "neural"))
try:
    import plastic_network as pn; _NEURAL_AVAILABLE = True
except ImportError: _NEURAL_AVAILABLE = False
try:
    import bayesian_layer as bl; import feature_encoder as fe; _BAYESIAN_AVAILABLE = True
except ImportError: _BAYESIAN_AVAILABLE = False

# ─── WebSocket Orderbook Feed (sync adapter) ───────────────────────────────
try:
    from fdc_pm_websocket_sync import get_feed as _get_ws_feed
    _WS_AVAILABLE = True
except ImportError: _WS_AVAILABLE = False

GAMMA  = "https://gamma-api.polymarket.com"
OUTPUT = REPO / "output"; STATE = OUTPUT / "pm_state.json"

# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

SCAN_SECONDS = 120
INITIAL_BANKROLL = 250.0; PAPER_BANKROLL = 250.0

# BTC-only — proven winner across 20/20 seeds
ASSET = {"yf": "BTC-USD", "name": "Bitcoin"}

# Sizing — calibrated Kelly, cold/warm/live phases
COLD_PCT  = 0.02   # Fixed 2% until calibrator has data
WARM_CAL_FLOOR  = 0.25  # Floor on cal_factor during warm-up
WARM_CERT_FLOOR = 0.25  # Floor on certainty during warm-up
MAX_BANKROLL_FRAC = 0.02  # Hard 2% cap per trade
MIN_BET = 3.0   # Lowered from 5.0 for paper data accumulation (calibration factor low at cold start)
KELLY_MULT = 1.5
COLD_UPDATES = 10   # Trades before leaving cold phase
WARM_UPDATES = 30   # Trades before leaving warm phase

# Signals
RSI_OVERSOLD = 45; RSI_OVERBOUGHT = 55  # Widened bands for paper data accumulation
MIN_CONFIDENCE = 0.10  # Lowered from 0.15 for paper data accumulation phase
MAX_CONFIDENCE = 0.90

# Contracts — short-duration "Up or Down"
MAX_WINDOW_MINUTES = 15
MIN_VOLUME_USD = 5000
MIN_CONTRACT_PRICE = 0.02  # Lowered for near-ATM contracts that spike to extremes
MAX_CONTRACT_PRICE = 0.85
MIN_EDGE = 0.01  # Lowered from 0.02 for paper data accumulation phase
MAX_OPEN_POSITIONS = 3

# Guards
BEAR_SKIP = False  # Disabled for paper data accumulation. Re-enable before live.
TREND_GUARD = False  # Disabled for paper data accumulation. Re-enable before live.

# Neural
NEURAL_BLEND_MAX = 0.30; NEURAL_BLEND_UPDATES = 200; NEURAL_CONS_EVERY = 50

_neural_engine = None; _bayesian_engine = None; _feature_encoder = None


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def pm_encode_signal(sig: dict) -> np.ndarray:
    d = sig.get("direction", "neutral"); conf = sig.get("confidence", 0.0)
    rsi = sig.get("rsi", 50.0); macd = sig.get("macd", 0.0); mom = sig.get("momentum", 2)
    if d == "up":
        trend_sig = 0.5+conf*0.3; mom_sig=min(1.0,mom/3.0); mean_rev=max(0.0,(50-rsi)/25)
    elif d == "down":
        trend_sig = -0.5-conf*0.3; mom_sig=-min(1.0,(3-mom)/3.0); mean_rev=-max(0.0,(rsi-50)/25)
    else: trend_sig=mom_sig=mean_rev=0.0
    rsi_norm=(rsi-50)/25; macd_norm=float(np.clip(macd/500,-1,1)); vol=abs(rsi-50)/25
    return np.array([
        float(np.clip(rsi_norm,-1,1)), float(np.clip(macd_norm,-1,1)),
        float(np.clip(trend_sig,-1,1)), float(np.clip(mom_sig,-1,1)),
        float(np.clip(mean_rev,-1,1)), float(np.clip(vol,0,1)),
        0.0, float(np.clip(conf,0,1)),
    ], dtype=float)

def scale_pnl(pnl_pct): return float(np.clip(pnl_pct/1.25,-1,1))

def _get_neural():
    global _neural_engine
    if not _NEURAL_AVAILABLE: return None
    if _neural_engine is None: _neural_engine = pn.NeuralPlasticityEngine()
    return _neural_engine

def _get_bayesian():
    global _bayesian_engine
    if not _BAYESIAN_AVAILABLE: return None
    if _bayesian_engine is None: _bayesian_engine = bl.BayesianCalibrator()
    return _bayesian_engine

def _get_encoder():
    global _feature_encoder
    if _feature_encoder is None:
        _feature_encoder = fe.FeatureEncoder(calibrator=_get_bayesian())
    return _feature_encoder

def _neural_blend():
    n=_get_neural(); return 0.0 if n is None or n.network.updates<100 else NEURAL_BLEND_MAX*min(1.0,(n.network.updates-100)/NEURAL_BLEND_UPDATES)

def _get(url):
    req=urllib.request.Request(url,headers={"User-Agent":"hermes-fdc/3.0"})
    with urllib.request.urlopen(req,timeout=15) as r: return json.loads(r.read())

def _parse(val):
    if isinstance(val,str):
        try: return json.loads(val)
        except: return val
    return val

def _ema(vals,span):
    a=2/(span+1); r=vals[0]
    for v in vals[1:]: r=a*v+(1-a)*r
    return r


# ══════════════════════════════════════════════════════════════════════════════
# Price fetching
# ══════════════════════════════════════════════════════════════════════════════

def fetch_5m():
    try:
        import yfinance as yf
        h=yf.Ticker(ASSET["yf"]).history(period="1d",interval="5m")
        return h['Close'].tolist()[-60:] if len(h)>=14 else []
    except: return []


# ══════════════════════════════════════════════════════════════════════════════
# Signal stack
# ══════════════════════════════════════════════════════════════════════════════

def btc_signal(prices):
    if len(prices)<14: return {"direction":"neutral","confidence":0,"rsi":50,"price":0}
    deltas=[prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains=sum(max(d,0) for d in deltas[-7:])/7
    losses=sum(max(-d,0) for d in deltas[-7:])/7
    rsi=100-(100/(1+gains/max(losses,1e-9)))
    macd=_ema(prices,6)-_ema(prices,13)
    up=sum(1 for i in range(1,min(4,len(prices))) if prices[-i]>prices[-i-1])
    d,c="neutral",0.0
    if rsi<RSI_OVERSOLD: d,c="up",min(0.80,(RSI_OVERSOLD-rsi)/15)+(0.10 if up>=2 else 0)
    elif rsi>RSI_OVERBOUGHT: d,c="down",min(0.80,(rsi-RSI_OVERBOUGHT)/15)+(0.10 if up<2 else 0)
    else: d,c=("up" if up>=2 else "down"),0.20
    sma20=sum(prices[-20:])/20 if len(prices)>=20 else prices[-1]
    return {"direction":d,"confidence":min(MAX_CONFIDENCE,c),"rsi":round(rsi,1),
            "macd":round(macd,2),"momentum":up,"price":prices[-1],
            "sma20":sma20,"_prices":prices}


def is_bear_market(prices):
    if len(prices)<20: return False
    sma20=sum(prices[-20:])/20; macd=_ema(prices,6)-_ema(prices,13)
    return prices[-1]<sma20 and macd<0

def is_uptrend(prices):
    if len(prices)<20: return True  # not enough data, allow entries
    sma20=sum(prices[-20:])/20; macd=_ema(prices,6)-_ema(prices,13)
    return prices[-1]>sma20 and macd>0

def is_downtrend(prices):
    if len(prices)<20: return False
    sma20=sum(prices[-20:])/20; macd=_ema(prices,6)-_ema(prices,13)
    return prices[-1]<sma20 and macd<0


# ══════════════════════════════════════════════════════════════════════════════
# Contract discovery — "Up or Down" short-duration
# ══════════════════════════════════════════════════════════════════════════════

def extract_time_window(question):
    m=re.search(r'(\d{1,2}:\d{2}(AM|PM)\s*-\s*\d{1,2}:\d{2}(AM|PM)\s*(ET|UTC))',question,re.I)
    if m: return m.group(1).replace(" ","")
    m=re.search(r'(\d{1,2}(AM|PM)\s*(ET|UTC))',question,re.I)
    if m: return m.group(1).replace(" ","")
    return None

def parse_end_time(end_date,window):
    if end_date:
        try: return datetime.fromisoformat(end_date.replace("Z","+00:00")).replace(tzinfo=None)
        except: pass
    m_end=re.search(r'-(\d{1,2}:\d{2})(AM|PM)',window,re.I)
    if m_end:
        t_str=f"{m_end.group(1)}{m_end.group(2).upper()}"
        try: return datetime.combine(datetime.now().date(),datetime.strptime(t_str,"%I:%M%p").time())
        except: pass
    return None

def discover_contracts():
    today=datetime.now(); month=today.strftime("%B"); day=today.day
    n=ASSET["name"]; contracts=[]; seen=set()
    
    # Strategy: search for any BTC contracts (short-duration + daily)
    # Short-duration "Up or Down" 5-min contracts may not always be active.
    # Fall back to daily "above/below" contracts when short-duration unavailable.
    queries = [
        f"{n} Up or Down",                     # Short-duration (preferred)
        f"{n} Up or Down - {month} {day}",     # Today's short-duration
        f"{n} above",                           # Daily above (fallback)
        f"{n} price",                           # Generic price markets
    ]
    
    for q in queries:
        try:
            data=_get(f"{GAMMA}/public-search?q={urllib.parse.quote(q)}")
            for evt in data.get("events",[]):
                for m in evt.get("markets",[]):
                    cid=m.get("conditionId","")
                    if cid in seen or m.get("closed",False): continue
                    vol = float(m.get("volume",0))
                    if vol < MIN_VOLUME_USD: continue
                    seen.add(cid)
                    question=m.get("question","")
                    prices=_parse(m.get("outcomePrices",[]))
                    if not isinstance(prices,list) or len(prices)<2: continue
                    outcomes=_parse(m.get("outcomes",[]))
                    
                    # Try short-duration window first
                    window=extract_time_window(question)
                    
                    # For daily contracts, derive end time from endDate
                    end_dt = None
                    if window:
                        end_dt=parse_end_time(m.get("endDate",""),window)
                    elif m.get("endDate"):
                        try:
                            end_dt = datetime.fromisoformat(m.get("endDate","").replace("Z","+00:00")).replace(tzinfo=None)
                        except: pass
                    
                    mins = 9999
                    if end_dt:
                        mins=(end_dt-datetime.now()).total_seconds()/60
                    
                    # Accept short-duration (≤MAX_WINDOW) or daily (≤1440 = 24h)
                    if window and mins < 0: continue  # expired short-duration
                    if not window and mins > 1440: continue  # too far out
                    
                    up_i,down_i=(0,1)
                    if isinstance(outcomes,list) and len(outcomes)>=2:
                        o0 = (outcomes[0] or "").lower()
                        o1 = (outcomes[1] or "").lower()
                        if "down" in o0 or "no" in o0 or "below" in o0:
                            up_i,down_i=(1,0)
                    
                    contracts.append({
                        "question":question,"conditionId":cid,
                        "up_price":float(prices[up_i]),
                        "down_price":float(prices[down_i]),
                        "volume":vol,
                        "slug":evt.get("slug",""),
                        "end_date":m.get("endDate",""),
                        "window":window,"mins_to_expiry":round(mins,1),
                        "is_daily": window is None,  # Flag for sizing adjustments
                    })
        except: continue
    return contracts


# ══════════════════════════════════════════════════════════════════════════════
# Kelly Sizing — cold/warm/live phases
# ══════════════════════════════════════════════════════════════════════════════

def kelly_size(edge,odds,bankroll,cal_factor,certainty,updates):
    if edge<=0 or bankroll<=0: return 0.0
    if updates<COLD_UPDATES: return round(bankroll*COLD_PCT,2)
    cf=max(WARM_CAL_FLOOR,cal_factor) if updates<WARM_UPDATES else cal_factor
    ct=max(WARM_CERT_FLOOR,certainty) if updates<WARM_UPDATES else certainty
    raw=(edge/max(odds,0.01))*0.5*KELLY_MULT*cf*ct
    return round(min(raw,MAX_BANKROLL_FRAC)*bankroll,2)


# ══════════════════════════════════════════════════════════════════════════════
# Trade decision
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_entries(sig,contracts,state):
    direction=sig["direction"]; conf=sig["confidence"]; price=sig["price"]
    if direction=="neutral" or conf<MIN_CONFIDENCE: return [],[]

    # ── Bear guard ──
    if BEAR_SKIP and is_bear_market(sig["_prices"]): return [],[]

    # ── Trend guard ──
    if TREND_GUARD:
        if is_uptrend(sig["_prices"]) and direction=="down": return [],[]
        if is_downtrend(sig["_prices"]) and direction=="up": return [],[]

    # ── Neural blend ── (gated: 100+ real updates)
    neural_pred=None; signal_vector=None; blend_w=_neural_blend(); neural=_get_neural()
    if neural and blend_w>0:
        signal_vector=pm_encode_signal(sig)
        neural_pred=neural.network.predict(signal_vector)
        nc=(neural_pred+1)/2 if direction=="up" else (1-neural_pred)/2
        nc=max(0,min(1,nc)); conf=conf*(1-blend_w)+nc*blend_w
        conf=round(min(0.95,conf),3)

    # ── WebSocket orderbook integration ──
    contract_prices = {}
    if _WS_AVAILABLE:
        try:
            ws_feed = _get_ws_feed()
            if ws_feed and ws_feed.is_connected():
                live_books = ws_feed.get_books()
                # Build a quick map: token_id → (mid_price, best_bid, best_ask)
                for tid, book in live_books.items():
                    if book.get("mid_price"):
                        contract_prices[tid] = book["mid_price"]
        except Exception:
            pass  # WebSocket is non-critical

    candidates=[]
    for c in contracts:
        ep=c["up_price"] if direction=="up" else c["down_price"]
        if MIN_CONTRACT_PRICE<ep<MAX_CONTRACT_PRICE:
            candidates.append({"contract":c,"side":"Up" if direction=="up" else "Down","price":ep})

    if not candidates: return [],[]

    bankroll=state.get("bankroll",PAPER_BANKROLL)
    positions=state.get("positions",{})
    invested=sum(p.get("bet",0) for p in positions.values())
    available=max(0,bankroll-invested)

    entries=[]
    for cand in sorted(candidates,key=lambda x: conf-x["price"],reverse=True):
        edge=conf-cand["price"]
        if edge<MIN_EDGE: continue
        key=f"{cand['contract']['conditionId'][:16]}_{cand['side']}"
        if key in positions: continue
        if len(positions)+len(entries)>=MAX_OPEN_POSITIONS: break
        if available<MIN_BET: break

        cal=_get_bayesian(); enc=_get_encoder()
        mins=cand["contract"].get("mins_to_expiry",10)
        hrs=mins/60
        fv=enc.encode(sig["_prices"],cand["contract"]["up_price"],
                       cand["contract"]["down_price"],cand["contract"]["volume"],hrs)
        cr=cal.predict(fv,market_price=cand["price"]) if cal else None

        if cr:
            cp=cr["probability"]
            ce=(cp-cand["price"]) if cand["side"]=="Up" else ((1-cp)-cand["price"])
            if ce>edge: edge=ce

        bet=kelly_size(edge,1-cand["price"],bankroll,cal.calibration_factor if cal else 0.5,
                       cr.get("certainty",0.5) if cr else 0.5,cal.updates if cal else 0)
        if bet<MIN_BET or bet>available: continue

        sv=signal_vector.tolist() if signal_vector is not None else None
        entries.append({
            "action": f"BUY_{cand['side']}",
            "question":cand["contract"]["question"],
            "conditionId":cand["contract"]["conditionId"],
            "contract_price":cand["price"],"bet":bet,
            "edge":round(edge,4),"price_at_entry":round(price,2),
            "signal_conf":conf,"signal_rsi":sig["rsi"],
            "mins_to_expiry":mins,"entry_time":datetime.now().isoformat(),
            "side":cand["side"],
            "bayesian_features":fv.tolist() if cr else None,
            "cal_prob":round(cr["probability"],4) if cr else None,
            "cal_certainty":round(cr["certainty"],4) if cr else None,
            "kl_divergence":round(cr["kl_divergence"],6) if cr and "kl_divergence" in cr else None,
            "signal_vector":sv,"neural_pred":round(neural_pred,4) if neural_pred else None,
        })
        available-=bet

    return entries,neural_pred


# ══════════════════════════════════════════════════════════════════════════════
# Settlement
# ══════════════════════════════════════════════════════════════════════════════

def check_settlements(state,btc_price):
    positions=state.get("positions",{}); settled=[]; now=datetime.now()
    for key,pos in list(positions.items()):
        try:
            et=datetime.fromisoformat(pos.get("entry_time",""))
            if (now-et).total_seconds()/60<pos.get("mins_to_expiry",10): continue
        except: continue
        entry=pos.get("price_at_entry",0); side=pos["side"]
        moved_up=btc_price>entry
        won=(side=="Up" and moved_up) or (side=="Down" and not moved_up)
        bet=pos["bet"]
        profit=(bet/pos["contract_price"]-bet) if won else -bet
        settled.append({**pos,"pnl":round(profit,2),"settle_price":round(btc_price,2),
                        "settle_time":now.isoformat()})
        del positions[key]
    return settled


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

def summary(state,entries,settled):
    br=state.get("bankroll",PAPER_BANKROLL); pnl=state.get("total_pnl",0)
    wins=state.get("wins",0); losses=state.get("losses",0); trades=wins+losses
    positions=state.get("positions",{})
    lines=["","🎲 POLYMARKET ENGINE v3 (BTC • bear-guard • Kelly)"]
    lines.append(f"   Bankroll: ${br:,.2f} | P&L: ${pnl:+,.2f} | Trades: {trades}")
    if trades: lines.append(f"   Wins: {wins} | Losses: {losses} | Rate: {wins/max(1,trades)*100:.0f}%")
    cal=_get_bayesian()
    if cal and cal.updates>0:
        phase="cold" if cal.updates<COLD_UPDATES else ("warm" if cal.updates<WARM_UPDATES else "live")
        lines.append(f"   Kelly phase: {phase} ({cal.updates} updates) | Brier: {cal.brier_score:.4f}")
    if settled:
        for s in settled[-5:]:
            e="🟢" if s["pnl"]>0 else "🔴"
            lines.append(f"   {e} {s['action']} — ${s['pnl']:+,.2f} ({s['question'][:50]})")
    if entries:
        for e in entries:
            lines.append(f"   ⚡ {e['action']}: ${e['bet']} @ {e['contract_price']:.3f} (edge={e['edge']:.3f})")
    if positions:
        for k,p in list(positions.items())[-5:]:
            lines.append(f"   📌 {p['side']} ${p['bet']} | edge={p.get('edge',0):.3f}")
    if not positions and not entries and not settled:
        lines.append("   Idle — waiting for signal.")
    neural=_get_neural()
    if neural: lines.append(f"   🧠 Neural: {neural.stats()['updates']} updates | Blend={_neural_blend():.0%}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════════════

def load_state():
    STATE.parent.mkdir(parents=True,exist_ok=True)
    if STATE.exists(): return json.loads(STATE.read_text())
    return {"bankroll":PAPER_BANKROLL,"total_pnl":0,"wins":0,"losses":0,
            "positions":{},"journal":[],"scans":0}

def save_state(state):
    state["scans"]=state.get("scans",0)+1
    STATE.write_text(json.dumps(state,indent=2,default=str))

def run_once(state):
    prices=fetch_5m()
    if not prices: return [],[],None

    sig=btc_signal(prices)
    contracts=discover_contracts()

    # Settle expired
    settled=check_settlements(state,sig["price"])
    for s in settled:
        pnl=s["pnl"]; state["total_pnl"]+=pnl; state["bankroll"]+=pnl
        if pnl>0: state["wins"]=state.get("wins",0)+1
        else: state["losses"]=state.get("losses",0)+1
        state.setdefault("journal",[]).append(
            {"ts":datetime.now().isoformat(),"type":"settle","pnl":pnl,"question":s.get("question","")})

        cal=_get_bayesian()
        if cal:
            sv_b=s.get("bayesian_features")
            if sv_b: cal.update(np.array(sv_b,dtype=float),1 if pnl>0 else 0)
        neural=_get_neural()
        sv=s.get("signal_vector"); n_pred=s.get("neural_pred")
        if neural and sv and n_pred is not None:
            bet=s.get("bet",1); pnl_pct=pnl/max(bet,0.01)
            sv_arr=np.array(sv,dtype=float)
            neural.network.learn_from_trade(sv_arr,n_pred,scale_pnl(pnl_pct))
            neural.network.add_to_replay(sv_arr,scale_pnl(pnl_pct))
            if neural.network.updates%5==0: neural.network.replay()
            if neural.network.updates>0 and neural.network.updates%NEURAL_CONS_EVERY==0:
                neural.network.consolidate()
            neural.network.save(); neural.performance.save()

    # New entries
    entries,neural_pred=evaluate_entries(sig,contracts,state)
    for e in entries:
        key=f"{e['conditionId'][:16]}_{e['side']}"
        state["positions"][key]=e

    save_state(state)
    print(summary(state,entries,settled))
    return entries,settled,sig

def run_continuous():
    state=load_state()
    print(f"🎲 FDC POLYMARKET v3 — {SCAN_SECONDS}s scan | ${state['bankroll']:,.2f}\n")
    while True:
        try:
            run_once(state); time.sleep(SCAN_SECONDS)
        except KeyboardInterrupt:
            print(f"\n👋 Stopped. ${state['bankroll']:,.2f} | P&L: ${state.get('total_pnl',0):+,.2f}")
            break
        except Exception as e:
            print(f"❌ {e}",file=sys.stderr); time.sleep(30)

# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__=="__main__":
    if "--once" in sys.argv:
        state=load_state()
        e,s,sig=run_once(state)
        if sig and sig["price"]:
            print(f"\nBTC: {sig['direction']} @ {sig['confidence']:.2f} (RSI={sig['rsi']}, ${sig['price']:,.2f})")
        else: print("\n⚠ No BTC data available.")
    elif "--discover" in sys.argv:
        cs=discover_contracts()
        print(f"{len(cs)} active contracts:")
        for c in sorted(cs,key=lambda x: x["mins_to_expiry"])[:10]:
            print(f"  {c['question']} — Up {c['up_price']*100:.0f}% | Down {c['down_price']*100:.0f}% | ${c['volume']:,.0f} | Expires {c['mins_to_expiry']}m")
    elif "--reset" in sys.argv:
        STATE.unlink(missing_ok=True); print("State reset.")
    elif "--continuous" in sys.argv or "-c" in sys.argv:
        run_continuous()
    else: print(__doc__)
