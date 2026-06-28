#!/usr/bin/env python3
"""V19 MC Quick — Krajekis-enhanced confluence scoring"""
import sys, os, json, random, math
from pathlib import Path
from datetime import datetime, timezone
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from pm_engine_v18_8 import (
    compute_rsi, detect_btc_direction, generate_signal_v188, get_regime,
    MIN_CONFIDENCE, MIN_BET, MAX_OPEN_POSITIONS,
    compute_win_probability, kelly_size, TradeJournal,
)
from paper_trade_v19 import (
    compute_ema, compute_atr, compute_vwap, compute_macd,
    get_session, classify_volatility, compute_confluence, MIN_CONFLUENCE,
    STOP_LOSS_PCT, TAKE_PROFIT_PRICE, TRAILING_STOP_PCT, TIER_CONFIG,
    DAILY_LOSS_LIMIT,
)

BANKROLL = 400.0
LOGLAB = 100
MIN_EDGE = 0.03
MIN_CAPITAL = 5.0
MAX_BANKROLL_FRAC = 0.08

HARD_MODE = True
SLIPPAGE_BASE = 0.02
LATENCY_MISS_PROB = 0.05
PARTIAL_FILL_PROB = 0.10
MARKOV_DRIFT_PPD = 0.03

def mc_v19(seeds=20, cycles=500, bankroll=400.0):
    print(f"\nV19 MC — Krajekis-Enhanced | Seeds={seeds} Cycles={cycles} Bankroll=${bankroll}")
    print(f"Confluence gate: {MIN_CONFLUENCE}/10 | Daily loss limit: {DAILY_LOSS_LIMIT}")
    print()
    
    all_finals, all_wrs, all_qualified = [], [], []
    exit_stats = {"stop_loss": 0, "take_profit": 0, "trailing_stop": 0, "time_decay": 0, "expiry_win": 0, "expiry_loss": 0}
    entry_stats = {"direct": {"w": 0, "l": 0}, "fair_price": {"w": 0, "l": 0}}
    conf_stats = {"high": {"w": 0, "l": 0}, "low": {"w": 0, "l": 0}}
    session_stats = {}
    vol_stats = {}
    journal = TradeJournal()
    
    for seed in range(seeds):
        rng = random.Random(seed)
        np.random.seed(seed)
        cap = bankroll
        peak = bankroll
        n = w = l = 0
        positions = []
        daily_losses = 0
        
        price = 73500 + rng.gauss(0, 2000)
        prices = [price] * 100
        regimes = []
        r_cycle = 0
        for _ in range(cycles + 20):
            r_len = rng.randint(20, 60)
            r_type = rng.choices(["trending_up", "ranging", "trending_down", "volatile"], weights=[0.30, 0.25, 0.25, 0.20])[0]
            regimes.append((r_cycle, r_cycle + r_len, r_type))
            r_cycle += r_len
        
        for cycle in range(cycles):
            regime = "ranging"
            for rs, re, rt in regimes:
                if rs <= cycle < re:
                    regime = rt
                    break
            
            drift_map = {"trending_up": rng.gauss(0.0003, 0.002), "trending_down": rng.gauss(-0.0003, 0.002),
                        "volatile": rng.gauss(0, 0.004), "ranging": rng.gauss(0, 0.001)}
            drift = drift_map.get(regime, rng.gauss(0, 0.001))
            if len(prices) > 20:
                sma20 = sum(prices[-20:]) / 20
                drift += (sma20 - price) / price * 0.1
            price *= (1 + drift)
            prices.append(price)
            
            # Resolve open positions
            for pos in positions[:]:
                pos["held"] += 1
                pos["peak"] = max(pos["peak"], pos["cur"])
                won = pos["won"]
                if won:
                    pos["cur"] = pos["entry"] + (1.0 - pos["entry"]) * min(1.0, pos["held"] / 5.0) * rng.uniform(0.7, 1.0)
                else:
                    pos["cur"] = pos["entry"] * (1 - min(1.0, pos["held"] / 5.0) * rng.uniform(0.5, 0.9))
                pos["cur"] = max(0.01, min(0.99, pos["cur"]))
                
                drop = (pos["entry"] - pos["cur"]) / pos["entry"] if pos["entry"] > 0 else 0
                if drop >= STOP_LOSS_PCT:
                    exit_val = pos["bet"] * (pos["cur"] / pos["entry"])
                    cap += exit_val; l += 1
                    exit_stats["stop_loss"] += 1
                    entry_stats.setdefault(pos["etype"], {"w":0,"l":0})["l"] += 1
                    positions.remove(pos); continue
                if pos["cur"] >= TAKE_PROFIT_PRICE:
                    exit_val = pos["bet"] * (pos["cur"] / pos["entry"])
                    cap += exit_val; w += 1
                    exit_stats["take_profit"] += 1
                    entry_stats.setdefault(pos["etype"], {"w":0,"l":0})["w"] += 1
                    positions.remove(pos); continue
                if pos["held"] >= 2 and pos["peak"] > pos["entry"]:
                    if (pos["peak"] - pos["cur"]) / pos["peak"] >= TRAILING_STOP_PCT:
                        exit_val = pos["bet"] * (pos["cur"] / pos["entry"])
                        cap += exit_val
                        if pos["cur"] > pos["entry"]: w += 1
                        else: l += 1
                        exit_stats["trailing_stop"] += 1
                        positions.remove(pos); continue
                if pos["held"] >= 5:
                    if won:
                        p = pos["bet"] / pos["entry"]; cap += p
                        w += 1; exit_stats["expiry_win"] += 1
                    else:
                        l += 1; exit_stats["expiry_loss"] += 1
                    positions.remove(pos); continue
            
            if cap < MIN_CAPITAL: break
            if daily_losses >= DAILY_LOSS_LIMIT: continue
            
            rsi_arr = compute_rsi(prices)
            current_rsi = rsi_arr[-1]
            if HARD_MODE and rng.random() < 0.03:
                current_rsi += rng.gauss(0, 3)
                current_rsi = max(0, min(100, current_rsi))
            
            direction, strength = detect_btc_direction([{'close': p} for p in prices], len(prices) - 1)
            signal = generate_signal_v188(prices, [{'close': p} for p in prices], len(prices) - 1)
            if signal['direction'] == 'neutral': continue
            if HARD_MODE and rng.random() < LATENCY_MISS_PROB: continue
            
            sig_dir = signal['direction'].upper()
            sig_conf = signal['confidence']
            sig_strategy = signal['strategy']
            
            # V19: Confluence
            ema21 = compute_ema(prices, 21)
            ema50 = compute_ema(prices, 50)
            vwap = sum(prices[-20:]) / min(20, len(prices))
            atr = np.std(prices[-14:]) * 1.5 if len(prices) >= 14 else price * 0.005
            _, _, macd_h = compute_macd(prices)
            session = get_session(cycle % 24)
            vol_regime, vol_max = classify_volatility(atr, price)
            
            confluence, details = compute_confluence(
                current_rsi, direction, regime, ema21, ema50, vwap, price,
                macd_h, session, (vol_regime, vol_max), sig_dir
            )
            
            if confluence < MIN_CONFLUENCE: continue
            
            tier_cfg = TIER_CONFIG.get(sig_strategy, {"size": 0.03, "max_price": 0.08})
            tier_size = tier_cfg["size"]
            tier_max = min(tier_cfg["max_price"], vol_max)
            if vol_regime == "low_vol" and confluence >= 8: tier_size *= 1.3
            elif vol_regime == "high_vol": tier_size *= 0.7
            
            price_move = rng.gauss(0, 0.003)
            entry_type = None
            entry_price = None
            
            if abs(price_move) < 0.002 and sig_conf >= 0.70:
                entry_price = 0.48 + rng.uniform(-0.03, 0.07)
                entry_type = "fair_price"
            elif (sig_dir == 'UP' and price_move < -0.003) or (sig_dir == 'DOWN' and price_move > 0.003):
                entry_price = max(0.02, min(tier_max * 1.5, abs(price_move) * 8 + rng.uniform(0.01, 0.04)))
                entry_price = min(entry_price, 0.15)
                if entry_price <= tier_max * 1.5:
                    entry_type = "direct"
            
            if entry_type is None: continue
            
            actual_wr = TIER_CONFIG.get(sig_strategy, {"wr": 0.65}).get("wr", 0.65)
            if HARD_MODE: actual_wr = max(0.05, min(0.95, actual_wr + rng.gauss(0, MARKOV_DRIFT_PPD)))
            won = rng.random() < actual_wr
            
            if HARD_MODE: entry_price = min(0.99, entry_price + rng.uniform(0, SLIPPAGE_BASE))
            
            odds = 1.0 - entry_price
            edge = actual_wr - entry_price
            if edge < MIN_EDGE * 0.5: continue
            
            bet = max(MIN_BET, min(cap * tier_size, cap * MAX_BANKROLL_FRAC))
            if bet > cap * 0.5 or bet < MIN_BET: continue
            if len(positions) >= MAX_OPEN_POSITIONS: continue
            
            cap -= bet
            n += 1
            
            positions.append({
                "entry": entry_price, "cur": entry_price, "peak": entry_price,
                "bet": bet, "won": won, "held": 0, "etype": entry_type,
                "strategy": sig_strategy, "confluence": confluence, "session": session[0], "vol_regime": vol_regime,
            })
            
            session_stats.setdefault(session[0], {"w": 0, "l": 0})
            vol_stats.setdefault(vol_regime, {"w": 0, "l": 0})
            
            if cap > peak: peak = cap
            if (peak - cap) / peak > 0.5: break
        
        # Resolve remaining
        for pos in positions:
            if pos["won"]:
                cap += pos["bet"] / pos["entry"]; w += 1; exit_stats["expiry_win"] += 1
            else:
                l += 1; exit_stats["expiry_loss"] += 1
        
        all_finals.append(cap)
        all_wrs.append(w / max(n, 1))
        if n >= 5: all_qualified.append(w / max(n, 1))
    
    total_exits = sum(exit_stats.values())
    profitable = sum(1 for f in all_finals if f > bankroll)
    bust = sum(1 for f in all_finals if f < MIN_CAPITAL)
    
    print(f"\nV19 MC RESULTS")
    print(f"  Seeds: {seeds} | Cycles: {cycles} | Bankroll: ${bankroll}")
    print(f"  Avg WR: {np.mean(all_wrs):.1%} | Qualified WR: {np.mean(all_qualified):.1%}" if all_qualified else f"  Avg WR: {np.mean(all_wrs):.1%}")
    print(f"  Avg final: ${np.mean(all_finals):,.0f} | Median: ${np.median(all_finals):,.0f}")
    print(f"  Profitable: {profitable}/{seeds} | Bust: {bust}/{seeds}")
    print(f"\n  EXIT BREAKDOWN:")
    for k, v in sorted(exit_stats.items(), key=lambda x: x[1], reverse=True):
        print(f"    {k}: {v} ({v/total_exits*100:.1f}%)")
    print(f"\n  ENTRY TYPE:")
    for k, v in entry_stats.items():
        t = v["w"] + v["l"]; print(f"    {k}: {t} trades, {v['w']/max(1,t)*100:.0f}% WR")
    print(f"\n  SESSION:")
    for k, v in sorted(session_stats.items(), key=lambda x: x[1]['w']+x[1]['l'], reverse=True):
        t = v["w"] + v["l"]; print(f"    {k}: {t} trades, {v['w']/max(1,t)*100:.0f}% WR")
    print(f"\n  VOLATILITY:")
    for k, v in sorted(vol_stats.items(), key=lambda x: x[1]['w']+x[1]['l'], reverse=True):
        t = v["w"] + v["l"]; print(f"    {k}: {t} trades, {v['w']/max(1,t)*100:.0f}% WR")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--cycles", type=int, default=300)
    parser.add_argument("--bankroll", type=float, default=400.0)
    args = parser.parse_args()
    mc_v19(seeds=args.seeds, cycles=args.cycles, bankroll=args.bankroll)