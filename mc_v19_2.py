#!/usr/bin/env python3
"""
V19.1 Monte Carlo Simulation — 7 Refinements
================================================
Hard-mode MC with slippage, latency, partial fills, Markov drift.
V19.2 MC: 3-zone filter (20-50¢ dead, 8-20¢ mid w/ conf≥8), straddle filter
dynamic max price, stricter RSI, time decay, cooldown.
"""

import json, os, sys, math
import numpy as np
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

from paper_trade_v19_2 import (
    compute_ema, compute_atr, compute_vwap, compute_macd,
    get_session, classify_volatility, compute_confluence,
    MIN_CONFLUENCE, TIER_CONFIG,
    BANKROLL, STOP_LOSS_PCT, TAKE_PROFIT_PRICE,
    TRAILING_STOP_PCT, DAILY_LOSS_LIMIT, DAILY_LOSS_PCT,
    MAX_SAME_DIRECTION, DEAD_ZONE_LOW, DEAD_ZONE_HIGH, COOLDOWN_MINS,
    MID_ZONE_LOW, MID_ZONE_HIGH, MID_ZONE_MIN_CONFLUENCE,
)
from pm_engine_v18_8 import (
    compute_rsi, detect_btc_direction, generate_signal_v188, get_regime,
    MIN_CONFIDENCE,
)

# Hard-mode MC penalties
SLIPPAGE_EXTRA = 0.02
LATENCY_MISS_RATE = 0.05
PARTIAL_FILL_RATE = 0.10
MARKOV_DRIFT_RATE = 0.06

# Simulation params
BANKROLL_MC = 100.0
N_SEEDS = 30
N_CYCLES = 5000
INITIAL_BANKROLL = 100.0


def simulate_cycle(bd, prices, rng):
    """Simulate one trading cycle with V19.1 logic"""
    # Generate a random market window
    price_start = rng.uniform(70000, 80000)
    price_move_pct = rng.normal(0, 0.002)  # ~0.2% std per 5m window
    
    # Markov drift: 6% chance of mean-reverting
    if rng.random() < MARKOV_DRIFT_RATE:
        price_move_pct = -price_move_pct * 0.5
    
    # Generate indicators
    rsi = rng.uniform(15, 85)
    direction = rng.choice(['UP', 'DOWN', 'NEUTRAL', 'FLAT'])
    regime = rng.choice(['trending_up', 'trending_down', 'ranging'])
    
    # V19.1 #5: Stricter RSI
    if direction == 'DOWN' or (rsi < 45 and regime in ('trending_down', 'ranging')):
        implied_dir = 'DOWN'
    elif direction == 'UP' or (rsi > 55 and regime in ('trending_up', 'ranging')):
        implied_dir = 'UP'
    else:
        return bd, None  # No clear direction
    
    # RSI filter: DOWN requires RSI<38, UP requires RSI>55
    if implied_dir == 'DOWN' and rsi > 38:
        return bd, None
    if implied_dir == 'UP' and rsi < 55:
        return bd, None
    
    # Confluence score (random but realistic)
    base_conf = 5.5
    if implied_dir == 'DOWN':
        if rsi < 25: base_conf += 1.5
        elif rsi < 30: base_conf += 1.0
        elif rsi < 38: base_conf += 0.3
    elif implied_dir == 'UP':
        if rsi > 75: base_conf += 1.5
        elif rsi > 65: base_conf += 1.0
        elif rsi > 55: base_conf += 0.3
    
    # Direction alignment
    if regime == 'trending_down' and implied_dir == 'DOWN': base_conf += 1.0
    elif regime == 'trending_up' and implied_dir == 'UP': base_conf += 1.0
    
    # Other confluence factors (random)
    base_conf += rng.uniform(-0.5, 1.5)
    confluence = max(3.0, min(10.0, base_conf + rng.normal(0, 0.5)))
    
    # V19.1 #6: Time decay
    time_decay = rng.uniform(0.85, 1.0)
    confluence *= time_decay
    confluence = round(confluence, 1)
    
    if confluence < MIN_CONFLUENCE:
        return bd, None
    
    # V19.1 #3: Confluence-weighted sizing
    tier_size = 0.03  # Base
    if confluence >= 8.0:
        tier_size = min(0.06, tier_size * 1.5)
    elif confluence >= 7.0:
        tier_size = min(0.05, tier_size * 1.25)
    if confluence < 6.5:
        tier_size = min(tier_size, 0.03)
    
    # V19.1 #4: Dynamic max price by vol regime
    vol_regime = rng.choice(['low_vol', 'normal_vol', 'high_vol'], p=[0.3, 0.5, 0.2])
    if vol_regime == 'high_vol':
        tier_max_price = 0.08
    elif vol_regime == 'low_vol':
        tier_max_price = 0.20
    else:
        tier_max_price = 0.15
    
    # Vol-adaptive sizing
    if vol_regime == 'low_vol' and confluence >= 8:
        tier_size *= 1.3
    elif vol_regime == 'high_vol':
        tier_size *= 0.7
    
    # Generate entry price (simulating CLOB)
    entry_price = None
    entry_type = None
    
    # Cheap-side entries: simulate CLOB offering cheap side tokens
    if rng.random() < 0.7:  # 70% chance of finding cheap-side entry
        entry_price = rng.uniform(0.02, min(0.08, tier_max_price))
        entry_type = "cheap"
    elif rng.random() < 0.15:  # 15% fair-price (60¢+)
        entry_price = rng.uniform(0.60, 0.95)
        entry_type = "fair_price"
    else:  # 15% no entry available
        return bd, None
    
    # V19.2: Three-zone price filter
    if entry_price > DEAD_ZONE_LOW and entry_price < DEAD_ZONE_HIGH:
        return bd, None  # Dead zone 20-50¢
    elif entry_price > MID_ZONE_LOW and entry_price <= MID_ZONE_HIGH:
        if confluence < MID_ZONE_MIN_CONFLUENCE:
            return bd, None  # Mid-zone requires conf≥8
    
    # Latency miss
    if rng.random() < LATENCY_MISS_RATE:
        return bd, None
    
    # Bet sizing
    bet = bd * tier_size
    bet = max(0.25, min(bet, bd * 0.08))
    if bd < 5 or bet > bd:
        return bd, None
    
    # Slippage
    entry_price *= (1 + SLIPPAGE_EXTRA * rng.uniform(-0.5, 1.0))
    entry_price = max(0.01, min(0.99, entry_price))
    
    # Partial fill
    actual_bet = bet
    if rng.random() < PARTIAL_FILL_RATE:
        actual_bet *= rng.uniform(0.5, 0.95)
    
    # Outcome determination
    # Base WR depends on entry price (cheap-side edge)
    if entry_price <= 0.05:
        base_wr = 0.75  # Ultra-cheap: strong edge
    elif entry_price <= 0.08:
        base_wr = 0.72
    elif entry_price >= 0.60:
        base_wr = 0.65  # Fair-price: modest edge
    else:
        base_wr = 0.50  # Dead zone (shouldn't reach here)
    
    # Confluence bonus
    confluence_bonus = (confluence - 6.0) * 0.02  # 6→0%, 8→4%, 10→8%
    base_wr += confluence_bonus
    
    # Direction alignment bonus
    if regime == 'trending_down' and implied_dir == 'DOWN':
        base_wr += 0.05
    elif regime == 'trending_up' and implied_dir == 'UP':
        base_wr += 0.05
    
    wr = max(0.30, min(0.90, base_wr))
    won = rng.random() < wr
    
    # Exit simulation
    peak_price = entry_price
    cur_price = entry_price
    exit_type = None
    exit_price = None
    
    for step in range(5):
        progress = (step + 1) / 5
        if won:
            cur_price = entry_price + (1.0 - entry_price) * progress * rng.uniform(0.7, 1.0)
        else:
            cur_price = entry_price * (1 - progress * rng.uniform(0.5, 0.9))
        cur_price = max(0.01, min(0.99, cur_price))
        peak_price = max(peak_price, cur_price)
        
        if (entry_price - cur_price) / max(entry_price, 0.01) >= STOP_LOSS_PCT:
            exit_type = "stop_loss"
            exit_price = cur_price
            break
        if cur_price >= TAKE_PROFIT_PRICE:
            exit_type = "take_profit"
            exit_price = cur_price
            break
        if step >= 2 and peak_price > entry_price:
            if (peak_price - cur_price) / max(peak_price, 0.01) >= TRAILING_STOP_PCT:
                exit_type = "trailing_stop"
                exit_price = cur_price
                break
    
    if exit_type is None:
        if won:
            exit_type = "expiry_win"
            exit_price = 1.0
        else:
            exit_type = "expiry_loss"
            exit_price = 0.0
    
    # PnL calculation
    if won or exit_type == "take_profit":
        pnl = actual_bet * ((exit_price / max(entry_price, 0.01)) - 1)
    elif exit_type == "stop_loss":
        pnl = -actual_bet * STOP_LOSS_PCT
    else:
        pnl = -actual_bet
    
    pnl = round(pnl, 2)
    
    return bd + pnl, {
        'won': won, 'pnl': pnl, 'entry_price': round(entry_price, 4),
        'exit_type': exit_type, 'tier_size': tier_size, 'confluence': confluence,
        'direction': implied_dir, 'entry_type': entry_type, 'vol_regime': vol_regime,
    }


def run_mc(seed, n_cycles=N_CYCLES):
    """Run MC for one seed"""
    rng = np.random.default_rng(seed)
    bd = BANKROLL_MC
    trades = []
    daily_losses = 0
    daily_loss_amt = 0.0
    cooldown_remaining = 0
    open_dirs = []
    
    for cycle in range(n_cycles):
        # Daily reset
        if cycle % (12 * 24) == 0:  # ~1 day per 288 cycles (5min each)
            daily_losses = 0
            daily_loss_amt = 0.0
        
        # Cooldown after stop-loss
        if cooldown_remaining > 0:
            cooldown_remaining -= 1
            continue
        
        # Daily loss limit
        if daily_losses >= DAILY_LOSS_LIMIT or daily_loss_amt >= BANKROLL_MC * DAILY_LOSS_PCT:
            continue
        
        # V19.1 #2: Correlation limit
        open_dirs = open_dirs[-MAX_SAME_DIRECTION:]  # Keep only recent
        # (simplified: no actual multi-position tracking in MC)
        
        new_bd, trade = simulate_cycle(bd, [], rng)
        if trade is None:
            continue
        
        # Check correlation
        if trade['direction'] == 'DOWN' and sum(1 for d in open_dirs if d == 'DOWN') >= MAX_SAME_DIRECTION:
            continue
        if trade['direction'] == 'UP' and sum(1 for d in open_dirs if d == 'UP') >= MAX_SAME_DIRECTION:
            continue
        
        open_dirs.append(trade['direction'])
        if len(open_dirs) > 3:
            open_dirs = open_dirs[-3:]
        
        trades.append(trade)
        bd = new_bd
        
        if trade['pnl'] < 0:
            daily_losses += 1
            daily_loss_amt += abs(trade['pnl'])
            if trade['exit_type'] == 'stop_loss':
                cooldown_remaining = int(COOLDOWN_MINS / 5)  # Convert to cycles
        
        if bd < 1:
            break
    
    wins = sum(1 for t in trades if t['won'])
    total = len(trades)
    wr = wins / total if total > 0 else 0
    
    return {
        'seed': seed,
        'trades': total,
        'wins': wins,
        'wr': wr,
        'final_bankroll': round(bd, 2),
        'pnl': round(bd - BANKROLL_MC, 2),
        'avg_pnl': round((bd - BANKROLL_MC) / max(total, 1), 2),
    }


def main():
    print("=" * 70)
    print("V19.1 MONTE CARLO — Hard Mode (Slippage, Latency, Partial Fills, Markov Drift)")
    print("=" * 70)
    print(f"  Seeds: {N_SEEDS} | Cycles: {N_CYCLES} | Bankroll: ${BANKROLL_MC}")
    print(f"  Dead Zone: {DEAD_ZONE_LOW*100:.0f}-{DEAD_ZONE_HIGH*100:.0f}¢ | Mid Zone: {MID_ZONE_LOW*100:.0f}-{MID_ZONE_HIGH*100:.0f}¢ (conf≥{MID_ZONE_MIN_CONFLUENCE:.0f}) | Correlation: {MAX_SAME_DIRECTION} same-dir max")
    print(f"  Cooldown: {COOLDOWN_MINS}min after SL | Confluence sizing: 6→3%, 7→4%, 8+→5-6%")
    print(f"  RSI: DOWN requires RSI<38, UP requires RSI>55")
    print("=" * 70)
    
    all_results = []
    for seed in range(N_SEEDS):
        result = run_mc(seed)
        all_results.append(result)
    
    # Aggregate
    total_trades = sum(r['trades'] for r in all_results)
    total_wins = sum(r['wins'] for r in all_results)
    avg_wr = total_wins / total_trades if total_trades > 0 else 0
    avg_bankroll = np.mean([r['final_bankroll'] for r in all_results])
    avg_trades = np.mean([r['trades'] for r in all_results])
    
    # Qualified WR (seeds with ≥5 trades only)
    qualified = [r for r in all_results if r['trades'] >= 5]
    q_wr = np.mean([r['wr'] for r in qualified]) if qualified else 0
    
    # Per-seed WR distribution
    wrs = [r['wr'] for r in all_results if r['trades'] > 0]
    
    print(f"\n{'='*70}")
    print("V19.1 MC RESULTS")
    print(f"{'='*70}")
    print(f"  Total trades: {total_trades} | Avg/seed: {avg_trades:.1f}")
    print(f"  Win rate: {total_wins}/{total_trades} = {avg_wr:.1%}")
    print(f"  Qualified WR (≥5 trades): {q_wr:.1%} ({len(qualified)}/{N_SEEDS} seeds)")
    print(f"  Avg final bankroll: ${avg_bankroll:.2f} (started ${BANKROLL_MC})")
    print(f"  WR range: {min(wrs):.1%} - {max(wrs):.1%}")
    
    # Per-entry-type breakdown
    all_trades_flat = []
    for seed in range(N_SEEDS):
        rng = np.random.default_rng(seed)
        bd = BANKROLL_MC
        daily_losses = 0
        daily_loss_amt = 0.0
        cooldown_remaining = 0
        open_dirs = []
        
        for cycle in range(N_CYCLES):
            if cycle % 288 == 0:
                daily_losses = 0
                daily_loss_amt = 0.0
            if cooldown_remaining > 0:
                cooldown_remaining -= 1
                continue
            if daily_losses >= DAILY_LOSS_LIMIT or daily_loss_amt >= BANKROLL_MC * DAILY_LOSS_PCT:
                continue
            
            new_bd, trade = simulate_cycle(bd, [], rng)
            if trade is None:
                continue
            if trade['direction'] == 'DOWN' and sum(1 for d in open_dirs if d == 'DOWN') >= MAX_SAME_DIRECTION:
                continue
            if trade['direction'] == 'UP' and sum(1 for d in open_dirs if d == 'UP') >= MAX_SAME_DIRECTION:
                continue
            
            open_dirs.append(trade['direction'])
            if len(open_dirs) > 3:
                open_dirs = open_dirs[-3:]
            
            all_trades_flat.append(trade)
            bd = new_bd
            if trade['pnl'] < 0:
                daily_losses += 1
                daily_loss_amt += abs(trade['pnl'])
                if trade['exit_type'] == 'stop_loss':
                    cooldown_remaining = int(COOLDOWN_MINS / 5)
            if bd < 1:
                break
    
    # Entry type breakdown
    by_type = {}
    for t in all_trades_flat:
        et = t.get('entry_type', 'unknown')
        if et not in by_type:
            by_type[et] = {'wins': 0, 'total': 0, 'pnl': 0}
        by_type[et]['total'] += 1
        if t['won']:
            by_type[et]['wins'] += 1
        by_type[et]['pnl'] += t['pnl']
    
    print(f"\n  BY ENTRY TYPE:")
    for et, v in sorted(by_type.items()):
        wr = v['wins'] / v['total'] if v['total'] > 0 else 0
        print(f"    {et}: {v['total']} trades, {wr:.1%} WR, ${v['pnl']:+.2f} PnL")
    
    # Direction breakdown
    by_dir = {}
    for t in all_trades_flat:
        d = t.get('direction', 'unknown')
        if d not in by_dir:
            by_dir[d] = {'wins': 0, 'total': 0}
        by_dir[d]['total'] += 1
        if t['won']:
            by_dir[d]['wins'] += 1
    
    print(f"\n  BY DIRECTION:")
    for d, v in sorted(by_dir.items()):
        wr = v['wins'] / v['total'] if v['total'] > 0 else 0
        print(f"    {d}: {v['total']} trades, {wr:.1%} WR")
    
    # Exit type breakdown
    by_exit = {}
    for t in all_trades_flat:
        et = t.get('exit_type', 'unknown')
        if et not in by_exit:
            by_exit[et] = {'wins': 0, 'total': 0, 'pnl': 0}
        by_exit[et]['total'] += 1
        if t['won']:
            by_exit[et]['wins'] += 1
        by_exit[et]['pnl'] += t['pnl']
    
    print(f"\n  BY EXIT TYPE:")
    for et, v in sorted(by_exit.items()):
        wr = v['wins'] / v['total'] if v['total'] > 0 else 0
        print(f"    {et}: {v['total']} trades, {wr:.1%} WR, ${v['pnl']:+.2f} PnL")
    
    # Confluence breakdown
    by_conf = {'6-7': {'wins': 0, 'total': 0}, '7-8': {'wins': 0, 'total': 0}, '8+': {'wins': 0, 'total': 0}}
    for t in all_trades_flat:
        c = t.get('confluence', 0)
        if c < 7:
            bucket = '6-7'
        elif c < 8:
            bucket = '7-8'
        else:
            bucket = '8+'
        by_conf[bucket]['total'] += 1
        if t['won']:
            by_conf[bucket]['wins'] += 1
    
    print(f"\n  BY CONFLUENCE:")
    for b, v in sorted(by_conf.items()):
        wr = v['wins'] / v['total'] if v['total'] > 0 else 0
        print(f"    {b}: {v['total']} trades, {wr:.1%} WR")
    
    # Save results
    output = REPO / "output" / "mc_v19_1_results.json"
    output.parent.mkdir(exist_ok=True)
    with open(output, 'w') as f:
        json.dump({
            'seeds': all_results,
            'summary': {
                'total_trades': total_trades,
                'avg_wr': avg_wr,
                'qualified_wr': q_wr,
                'avg_bankroll': avg_bankroll,
                'by_type': by_type,
                'by_dir': by_dir,
                'by_exit': by_exit,
                'by_conf': by_conf,
            }
        }, f, indent=2, default=str)
    print(f"\n  Results saved to {output}")


if __name__ == '__main__':
    main()