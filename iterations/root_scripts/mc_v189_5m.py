#!/usr/bin/env python3
"""
V18.9 Monte Carlo Simulation — 5m Up/Down with Exit Strategies
================================================================
Simulates 5-minute BTC Up/Down market trading with:
- V18.8 signal generation (RSI/direction/regime)
- 4 exit strategies (stop-loss, take-profit, trailing stop, time-decay)
- Hard-mode penalties (latency, slippage, partial fills, Markov drift)
- $400 bankroll, max 3 concurrent positions
"""

import json, os, sys, time, math, random
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
from pm_engine_v18_8 import (
    compute_rsi, detect_btc_direction, generate_signal_v188, get_regime,
    MIN_CONFIDENCE, RSI_OVERSOLD_SEVERE, RSI_OVERBOUGHT_SEVERE,
    WIN_PROB_BASE, MIN_BET, MAX_OPEN_POSITIONS,
    compute_win_probability, kelly_size, TradeJournal,
)

# ═══════════════════════════════════════════════════════════════
# EXIT STRATEGY CONFIG (same as V18.9 paper trader)
# ═══════════════════════════════════════════════════════════════
BANKROLL = 400.0
STOP_LOSS_PCT = 0.50
TAKE_PROFIT_PRICE = 0.90
TRAILING_STOP_PCT = 0.40
TRAILING_ACTIVATE_CYCLES = 2  # 2 MC cycles = ~2 minutes in 5m window
TIME_DECAY_SELL_CYCLES = 1    # <1 min left
TIME_DECAY_MIN_PRICE = 0.03

# Hard-mode penalties
HARD_MODE = True
SLIPPAGE_BASE = 0.02
LATENCY_MISS_PROB = 0.05
PARTIAL_FILL_PROB = 0.10
MARKOV_DRIFT_PPD = 0.03
MAKER_FILL_FAIL_PROB = 0.05
MAKER_FAIL_TAKER_PENALTY = 0.0112

# Tiers
TIER_CONFIG = {
    "severe_oversold":      {"size": 0.10, "max_price": 0.30, "wr": 0.82},
    "severe_overbought":    {"size": 0.10, "max_price": 0.30, "wr": 0.72},
    "oversold_down":        {"size": 0.06, "max_price": 0.15, "wr": 0.78},
    "overbought_up":        {"size": 0.06, "max_price": 0.15, "wr": 0.68},
    "direction_down_cheap": {"size": 0.03, "max_price": 0.08, "wr": 0.70},
    "direction_up_cheap":   {"size": 0.03, "max_price": 0.08, "wr": 0.68},
    "fair_price_down":      {"size": 0.03, "max_price": 0.55, "wr": 0.58},
    "fair_price_up":        {"size": 0.03, "max_price": 0.55, "wr": 0.56},
}

MIN_EDGE = 0.03
MIN_CAPITAL = 5.0
MAX_DAILY_LOSS = 50.0
MAX_DRAWDOWN_PCT = 0.50
LOOKBACK = 100
MIN_DIRECTION_CHANGE = 0.10
MAX_BANKROLL_FRAC = 0.08


def mc_v189(seeds=20, cycles=1000, bankroll=400.0):
    """V18.9 MC with exit strategies for 5m Up/Down markets."""
    print("\n" + "=" * 70)
    print("V18.9 MONTE CARLO — 5m Up/Down with Exit Strategies")
    print("=" * 70)
    print(f"Seeds: {seeds} | Cycles: {cycles} | Bankroll: ${bankroll}")
    print(f"Exit strategies: SL@{STOP_LOSS_PCT:.0%} TP@{TAKE_PROFIT_PRICE*100:.0f}¢ Trail@{TRAILING_STOP_PCT:.0%} after {TRAILING_ACTIVATE_CYCLES}c")
    print(f"Hard mode: {HARD_MODE}")
    print(f"Min confidence: {MIN_CONFIDENCE}")
    print()
    
    all_finals = []
    all_wrs = []
    all_qualified_wrs = []
    all_trade_counts = []
    exit_stats = {"stop_loss": 0, "take_profit": 0, "trailing_stop": 0, "time_decay": 0, "expiry_win": 0, "expiry_loss": 0}
    entry_stats = {"direct": {"w": 0, "l": 0, "pnl": 0}, "fair_price": {"w": 0, "l": 0, "pnl": 0}}
    tier_stats = {}
    journal = TradeJournal()
    
    for seed in range(seeds):
        rng = random.Random(seed)
        np.random.seed(seed)
        cap = bankroll
        peak = bankroll
        max_dd = 0
        n = w = l = 0
        positions = []  # Open positions with exit tracking
        daily_pnl = 0.0
        
        # Simulate BTC price walk
        price = 73500 + rng.gauss(0, 2000)
        prices = [price] * LOOKBACK
        
        # Regime schedule
        regimes = []
        r_cycle = 0
        for _ in range(cycles + 20):
            r_len = rng.randint(20, 60)
            r_type = rng.choices(
                ["trending_up", "ranging", "trending_down", "volatile"],
                weights=[0.30, 0.25, 0.25, 0.20]
            )[0]
            regimes.append((r_cycle, r_cycle + r_len, r_type))
            r_cycle += r_len
        
        for cycle in range(cycles):
            regime = "ranging"
            for rs, re, rt in regimes:
                if rs <= cycle < re:
                    regime = rt
                    break
            
            # Price movement
            if regime == "trending_up":
                drift = rng.gauss(0.0003, 0.002)
            elif regime == "trending_down":
                drift = rng.gauss(-0.0003, 0.002)
            elif regime == "volatile":
                drift = rng.gauss(0, 0.004)
            else:
                drift = rng.gauss(0, 0.001)
            
            if len(prices) > 20:
                sma20 = sum(prices[-20:]) / 20
                drift += (sma20 - price) / price * 0.1
            
            price *= (1 + drift)
            prices.append(price)
            
            # Update open positions (simulate intrabar price movement)
            for pos in positions[:]:
                pos["cycles_held"] += 1
                pos["peak_price"] = max(pos["peak_price"], pos["cur_price"])
                
                # Simulate price movement within 5m window
                # Token price fluctuates based on signal direction and BTC movement
                if pos["won"]:  # Token trending toward $1
                    price_progress = min(1.0, pos["cycles_held"] / 5.0)
                    pos["cur_price"] = pos["entry_price"] + (1.0 - pos["entry_price"]) * price_progress * rng.uniform(0.7, 1.0)
                else:  # Token trending toward $0
                    price_progress = min(1.0, pos["cycles_held"] / 5.0)
                    pos["cur_price"] = pos["entry_price"] * (1.0 - price_progress * rng.uniform(0.5, 0.9))
                
                pos["cur_price"] = max(0.01, min(0.99, pos["cur_price"]))
                
                # ── Exit 1: Stop-loss (50% drop from entry) ──
                price_drop = (pos["entry_price"] - pos["cur_price"]) / pos["entry_price"]
                if price_drop >= STOP_LOSS_PCT:
                    exit_value = pos["bet"] * (pos["cur_price"] / pos["entry_price"])
                    cap += exit_value
                    journal.record_exit(f"seed{seed}_trade{n+1}", "stop_loss", exit_value - pos["bet"],
                                       strategy=pos["strategy"], regime=regime)
                    exit_stats["stop_loss"] += 1
                    entry_stats[pos["entry_type"]]["l"] += 1
                    tier_stats.setdefault(pos["strategy"], {"w": 0, "l": 0, "pnl": 0})["l"] += 1
                    positions.remove(pos)
                    daily_pnl += exit_value - pos["bet"]
                    continue
                
                # ── Exit 2: Take-profit (token reaches 90¢) ──
                if pos["cur_price"] >= TAKE_PROFIT_PRICE:
                    exit_value = pos["bet"] * (pos["cur_price"] / pos["entry_price"])
                    cap += exit_value
                    journal.record_exit(f"seed{seed}_trade{n+1}", "take_profit", exit_value - pos["bet"],
                                       strategy=pos["strategy"], regime=regime)
                    exit_stats["take_profit"] += 1
                    w += 1
                    entry_stats[pos["entry_type"]]["w"] += 1
                    tier_stats.setdefault(pos["strategy"], {"w": 0, "l": 0, "pnl": 0})["w"] += 1
                    positions.remove(pos)
                    daily_pnl += exit_value - pos["bet"]
                    continue
                
                # ── Exit 3: Trailing stop (40% drop from peak after 2 cycles) ──
                if pos["cycles_held"] >= TRAILING_ACTIVATE_CYCLES and pos["peak_price"] > pos["entry_price"]:
                    drop_from_peak = (pos["peak_price"] - pos["cur_price"]) / pos["peak_price"]
                    if drop_from_peak >= TRAILING_STOP_PCT:
                        exit_value = pos["bet"] * (pos["cur_price"] / pos["entry_price"])
                        cap += exit_value
                        journal.record_exit(f"seed{seed}_trade{n+1}", "trailing_stop", exit_value - pos["bet"],
                                           strategy=pos["strategy"], regime=regime)
                        exit_stats["trailing_stop"] += 1
                        if pos["cur_price"] > pos["entry_price"]:
                            w += 1
                            entry_stats[pos["entry_type"]]["w"] += 1
                        else:
                            l += 1
                            entry_stats[pos["entry_type"]]["l"] += 1
                        positions.remove(pos)
                        daily_pnl += exit_value - pos["bet"]
                        continue
                
                # ── Exit 4: Time-decay (losing with <1 cycle left) ──
                if pos["cycles_held"] >= 4 and pos["cur_price"] < pos["entry_price"] * 0.5:
                    if pos["cur_price"] > TIME_DECAY_MIN_PRICE:
                        exit_value = pos["bet"] * (pos["cur_price"] / pos["entry_price"])
                        cap += exit_value
                        journal.record_exit(f"seed{seed}_trade{n+1}", "time_decay", exit_value - pos["bet"],
                                           strategy=pos["strategy"], regime=regime)
                        exit_stats["time_decay"] += 1
                        l += 1
                        entry_stats[pos["entry_type"]]["l"] += 1
                        positions.remove(pos)
                        daily_pnl += exit_value - pos["bet"]
                        continue
                
                # ── Exit 5: Expiry (5 cycles = 5 minutes) ──
                if pos["cycles_held"] >= 5:
                    if pos["won"]:
                        payout = pos["bet"] / pos["entry_price"]
                        profit = payout - pos["bet"]
                        cap += payout
                        journal.record_exit(f"seed{seed}_trade{n+1}", "expiry_win", profit,
                                           strategy=pos["strategy"], regime=regime)
                        exit_stats["expiry_win"] += 1
                        w += 1
                        entry_stats[pos["entry_type"]]["w"] += 1
                        tier_stats.setdefault(pos["strategy"], {"w": 0, "l": 0, "pnl": 0})["w"] += 1
                    else:
                        loss = -pos["bet"]
                        cap += 0  # Token expires worthless
                        journal.record_exit(f"seed{seed}_trade{n+1}", "expiry_loss", loss,
                                           strategy=pos["strategy"], regime=regime)
                        exit_stats["expiry_loss"] += 1
                        l += 1
                        entry_stats[pos["entry_type"]]["l"] += 1
                        tier_stats.setdefault(pos["strategy"], {"w": 0, "l": 0, "pnl": 0})["l"] += 1
                    positions.remove(pos)
                    daily_pnl += profit if pos["won"] else loss
                    continue
            
            # Resolve expired positions
            n_resolved = len([p for p in positions if p["cycles_held"] >= 5])
            
            # Compute RSI and signal
            if len(prices) < LOOKBACK:
                continue
            
            rsi_arr = compute_rsi(prices)
            current_rsi = rsi_arr[-1]
            
            # Hard-mode RSI perturbation
            if HARD_MODE and rng.random() < MARKOV_DRIFT_PPD * 10:
                current_rsi += rng.gauss(0, 3)
                current_rsi = max(0, min(100, current_rsi))
            
            # Direction
            recent = prices[-1]
            prev = prices[-1 - LOOKBACK] if len(prices) > LOOKBACK else prices[0]
            change_pct = (recent - prev) / prev * 100
            
            if change_pct > MIN_DIRECTION_CHANGE:
                direction = 'UP'
            elif change_pct < -MIN_DIRECTION_CHANGE:
                direction = 'DOWN'
            else:
                direction = 'FLAT'
            
            # Signal
            signal = generate_signal_v188(prices, [{'close': p} for p in prices], len(prices) - 1)
            if signal['direction'] == 'neutral':
                continue
            
            # Hard-mode: latency miss
            if HARD_MODE and rng.random() < LATENCY_MISS_PROB:
                continue
            
            sig_dir = signal['direction'].upper()
            sig_conf = signal['confidence']
            sig_strategy = signal['strategy']
            
            tier_cfg = TIER_CONFIG.get(sig_strategy, {"size": 0.03, "max_price": 0.08, "wr": 0.65})
            tier_size = tier_cfg["size"]
            tier_max_price = tier_cfg["max_price"]
            tier_wr = tier_cfg["wr"]
            
            # Hard-mode: partial fill miss
            if HARD_MODE and rng.random() < PARTIAL_FILL_PROB:
                continue
            
            # Determine entry type and price
            price_move = rng.gauss(0, 0.003)  # Random BTC move during window
            entry_type = None
            entry_price = None
            
            if abs(price_move) < 0.002:
                # Fair-price entry (~50¢)
                if sig_conf >= 0.70:
                    entry_price = 0.48 + rng.uniform(-0.03, 0.07)
                    entry_type = "fair_price"
            elif (sig_dir == 'UP' and price_move < -0.003) or (sig_dir == 'DOWN' and price_move > 0.003):
                # Direct cheap entry
                entry_price = max(0.03, min(tier_max_price * 1.5, abs(price_move) * 8 + rng.uniform(0.01, 0.04)))
                entry_price = min(entry_price, 0.15)
                if entry_price <= tier_max_price * 1.5:
                    entry_type = "direct"
            
            if entry_type is None:
                continue
            
            # Win probability based on strategy
            actual_wr = tier_wr
            if HARD_MODE:
                actual_wr += rng.gauss(0, MARKOV_DRIFT_PPD)
                actual_wr = max(0.05, min(0.95, actual_wr))
            
            won = rng.random() < actual_wr
            
            # Apply hard-mode slippage
            if HARD_MODE:
                entry_price += rng.uniform(0, SLIPPAGE_BASE)
                entry_price = min(0.99, entry_price)
            
            # Position sizing
            odds = 1.0 - entry_price
            edge = actual_wr - entry_price
            if edge < MIN_EDGE * 0.5:
                continue
            
            cal_factor = journal.get_calibration_factor() if hasattr(journal, 'get_calibration_factor') else 0.5
            bet = kelly_size(edge, odds, cap, cal_factor, sig_conf, n)
            bet = max(MIN_BET, min(bet, cap * tier_size, cap * MAX_BANKROLL_FRAC))
            
            if bet > cap * 0.5 or bet < MIN_BET:
                continue
            
            # Position limit
            if len(positions) >= MAX_OPEN_POSITIONS:
                continue
            
            # Kill switch
            if cap < MIN_CAPITAL:
                break
            if daily_pnl < -MAX_DAILY_LOSS:
                break
            
            # Record trade
            positions.append({
                "entry_price": entry_price,
                "cur_price": entry_price,
                "peak_price": entry_price,
                "bet": bet,
                "direction": sig_dir,
                "strategy": sig_strategy,
                "entry_type": entry_type,
                "won": won,
                "cycles_held": 0,
                "rsi": current_rsi,
                "regime": regime,
                "confidence": sig_conf,
            })
            
            cap -= bet
            n += 1
            
            # Drawdown check
            if cap > peak:
                peak = cap
            dd = (peak - cap) / peak
            max_dd = max(max_dd, dd)
            if dd > MAX_DRAWDOWN_PCT:
                break
        
        # Resolve remaining positions at expiry
        for pos in positions[:]:
            if pos["won"]:
                payout = pos["bet"] / pos["entry_price"]
                profit = payout - pos["bet"]
                cap += payout
                journal.record_exit(f"seed{seed}_final", "expiry_win", profit,
                                   strategy=pos["strategy"], regime=pos["regime"])
                w += 1
                exit_stats["expiry_win"] += 1
            else:
                l += 1
                journal.record_exit(f"seed{seed}_final", "expiry_loss", -pos["bet"],
                                   strategy=pos["strategy"], regime=pos["regime"])
                exit_stats["expiry_loss"] += 1
        
        all_finals.append(cap)
        wr = w / max(n, 1)
        all_wrs.append(wr)
        if n >= 5:
            all_qualified_wrs.append(wr)
        all_trade_counts.append(n)
    
    # ── RESULTS ──
    total_exits = sum(exit_stats.values())
    profitable_seeds = sum(1 for f in all_finals if f > bankroll)
    bust_seeds = sum(1 for f in all_finals if f < MIN_CAPITAL)
    
    print(f"\n{'='*70}")
    print(f"V18.9 MC RESULTS — 5m Up/Down with Exit Strategies")
    print(f"{'='*70}")
    print(f"Seeds: {seeds} | Cycles: {cycles} | Bankroll: ${bankroll}")
    print(f"Hard mode: {HARD_MODE}")
    print()
    print(f"TRADE STATS:")
    print(f"  Avg trades/seed: {np.mean(all_trade_counts):.0f} (range: {min(all_trade_counts)}-{max(all_trade_counts)})")
    print(f"  Avg WR: {np.mean(all_wrs):.1%} (range: {min(all_wrs):.1%} - {max(all_wrs):.1%})")
    if all_qualified_wrs:
        print(f"  Qualified WR (≥5 trades): {np.mean(all_qualified_wrs):.1%} ({len(all_qualified_wrs)}/{seeds} seeds)")
    print()
    print(f"EXIT BREAKDOWN:")
    for k, v in sorted(exit_stats.items()):
        pct = v / total_exits * 100 if total_exits > 0 else 0
        print(f"  {k}: {v} ({pct:.1f}%)")
    print()
    print(f"ENTRY TYPE STATS:")
    for k, v in entry_stats.items():
        total = v["w"] + v["l"]
        wr = v["w"] / total * 100 if total > 0 else 0
        print(f"  {k}: {total} trades, {wr:.1f}% WR, ${v['pnl']:+.2f} PnL")
    print()
    print(f"TIER STATS:")
    for k, v in sorted(tier_stats.items(), key=lambda x: x[1].get("pnl", 0), reverse=True):
        total = v["w"] + v["l"]
        wr = v["w"] / total * 100 if total > 0 else 0
        print(f"  {k}: {total} trades, {wr:.0f}% WR")
    print()
    print(f"FINANCIAL:")
    print(f"  Final bankroll: ${np.mean(all_finals):,.0f} (range: ${min(all_finals):,.0f} - ${max(all_finals):,.0f})")
    print(f"  Median: ${np.median(all_finals):,.0f}")
    print(f"  Profitable: {profitable_seeds}/{seeds} ({profitable_seeds/seeds:.0%})")
    print(f"  Bust (<${MIN_CAPITAL}): {bust_seeds}/{seeds}")
    print(f"  Max drawdown (avg): {np.mean([1 - f/bankroll for f in all_finals]):.1%}")
    
    # WR by strategy (from journal)
    print(f"\nWR BY STRATEGY (journal):")
    for k, v in sorted(journal.wr_by_strategy.items()):
        if v['total'] > 0:
            print(f"  {k}: {v['wins']}/{v['total']} ({v['wins']/v['total']:.1%})")
    
    results = {
        "version": "v189_mc",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "seeds": seeds, "cycles": cycles, "bankroll": bankroll,
            "stop_loss_pct": STOP_LOSS_PCT, "take_profit_price": TAKE_PROFIT_PRICE,
            "trailing_stop_pct": TRAILING_STOP_PCT, "hard_mode": HARD_MODE,
        },
        "summary": {
            "avg_wr": float(np.mean(all_wrs)),
            "qualified_wr": float(np.mean(all_qualified_wrs)) if all_qualified_wrs else 0,
            "avg_final_bankroll": float(np.mean(all_finals)),
            "median_final": float(np.median(all_finals)),
            "profitable_pct": profitable_seeds / seeds,
            "bust_pct": bust_seeds / seeds,
            "avg_trades_per_seed": float(np.mean(all_trade_counts)),
        },
        "exit_stats": exit_stats,
        "entry_stats": {k: v for k, v in entry_stats.items()},
        "tier_stats": {k: v for k, v in tier_stats.items()},
    }
    
    out = REPO / "output" / "mc_v189_results.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults saved to {out}")
    
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--cycles", type=int, default=1000)
    parser.add_argument("--bankroll", type=float, default=400.0)
    args = parser.parse_args()
    mc_v189(seeds=args.seeds, cycles=args.cycles, bankroll=args.bankroll)