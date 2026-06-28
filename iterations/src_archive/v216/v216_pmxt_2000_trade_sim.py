#!/usr/bin/env python3
"""
V21.6 Adaptive Execution Organism — 2000-Trade PMXT Simulation
================================================================
§1: maximize realized convex EV after execution friction
NOT raw EV, NOT payout, NOT win rate, NOT theoretical.

Modules:
  liquidity_topology.py → §3
  friction_surface.py  → §5
  execution_router.py  → §7
  execution_memory.py  → §10

Hierarchy: execution survivability > fillability > realized EV > raw convexity
"""

import pyarrow.parquet as pq
import numpy as np
from pathlib import Path
from collections import defaultdict
from datetime import datetime
import time, gc, json, random, sys

# V21.6 modules
sys.path.insert(0, str(Path(__file__).parent))
from liquidity_topology import LiquidityTopology, DEFAULT_BUCKET_ZONES
from friction_surface import FrictionSurface, FrictionObservation
from execution_router import ExecutionRouter, ExecutionMode
from execution_memory import ExecutionMemory, ExecutionTrace

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
BANKROLL_START = 100.0
TARGET_TRADES = 2000
POSITION_SIZE_USD = 1.00  # §14: $1 fixed

# §2: Net convexity weights / friction model
SPREAD_COST = 0.012
SLIPPAGE_PCT = 0.008
FILL_REJECTION_RATE = 0.07
PARTIAL_FILL_RATE = 0.12
STALE_QUOTE_RATE = 0.03
QUEUE_DELAY_PENALTY = 0.005
REPRICING_DRIFT_RATE = 0.04

# §9: Exploration allocation
EXPLORE_PROVEN = 0.60
EXPLORE_PROMISING = 0.25
EXPLORE_EXPERIMENTAL = 0.15

# §6: Timing phases
TIMING = {
    'EARLY':     (0.00, 0.20, 0.10),
    'FORMATION': (0.20, 0.40, 0.35),
    'MOMENTUM':  (0.40, 0.80, 0.80),
    'LATE':      (0.80, 0.90, 0.95),
    'FINAL':     (0.90, 1.00, 0.40),
}

# §7: Direction priority (DOWN primary per V21.5 validated thesis)
DIRECTION_PRIORITY = {
    'DOWN_CONTINUATION': 1.50,
    'DOWN_MOMENTUM': 1.40,
    'UP_REVERSAL': 0.60,
    'UP_CONTINUATION': 0.30,
    'FLAT': 0.10,
}

# §9: Signal weights — RSI max 5%
W = {'persist': 0.25, 'accel': 0.20, 'lag': 0.15, 'vol': 0.15, 'tte': 0.10, 'exec': 0.10, 'rsi': 0.05}

OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v216")
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════════════════
# SIGNAL COMPUTATION (from V21.5, unchanged core logic)
# ════════════════════════════════════════════════════════════════════

def compute_rsi(prices, period=14):
    n = len(prices)
    if n < period + 1:
        return np.full(n, 50.0)
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = np.zeros(n); avg_l = np.zeros(n)
    avg_g[period] = np.mean(gains[1:period+1])
    avg_l[period] = np.mean(losses[1:period+1])
    for i in range(period+1, n):
        avg_g[i] = (avg_g[i-1]*(period-1) + gains[i]) / period
        avg_l[i] = (avg_l[i-1]*(period-1) + losses[i]) / period
    rs = np.where(avg_l > 0, avg_g / avg_l, 100.0)
    rsi = np.where(avg_l > 0, 100 - 100/(1+rs), 100.0)
    rsi[:period] = 50.0
    return rsi


def compute_acceleration(prices):
    n = len(prices)
    if n < 4:
        return 0.0, 0.0, 0
    windows = [4, 8, 16, 32]
    weights = [0.4, 0.3, 0.2, 0.1]
    velocities = []
    for w in windows:
        if n > w:
            v = (prices[-1] - prices[-1-w]) / max(abs(prices[-1-w]), 1e-9) * 100
            velocities.append(v)
        else:
            velocities.append(0.0)
    velocity = sum(w*v for w, v in zip(weights, velocities))
    accel = velocities[0] - velocities[1] if len(velocities) >= 2 else 0.0
    consec = 0
    for i in range(1, min(6, n)):
        if n-i-1 >= 0 and prices[-i] > prices[-i-1]:
            consec += 1
        elif n-i-1 >= 0 and prices[-i] < prices[-i-1]:
            consec -= 1
    return np.tanh(accel / 0.5), velocity, consec


def compute_continuation(prices, consec):
    n = len(prices)
    if n < 10:
        return 0.0, 'unknown'
    persistence = abs(consec) / 6.0
    recent = prices[-min(10, n):]
    lower_lows = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i-1])
    higher_highs = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1])
    if lower_lows > higher_highs:
        raw = (lower_lows - higher_highs) / max(len(recent)-1, 1) + persistence * 0.5
        direction = 'DOWN_CONTINUATION'
    elif higher_highs > lower_lows:
        raw = (higher_highs - lower_lows) / max(len(recent)-1, 1) + persistence * 0.5
        direction = 'UP_CONTINUATION'
    elif consec <= -2:
        raw = 0.15 + persistence * 0.2
        direction = 'DOWN_CONTINUATION'
    elif consec >= 2:
        raw = 0.15 + persistence * 0.2
        direction = 'UP_CONTINUATION'
    else:
        raw = 0.0
        direction = 'FLAT'
    return np.tanh(raw), direction


def classify_state(accel, velocity, consec, cont_dir, rsi):
    if cont_dir == 'DOWN_CONTINUATION' or (consec <= -3 and velocity < -0.05):
        return 'DOWN_MOMENTUM' if abs(velocity) > 0.3 else 'DOWN_CONTINUATION'
    elif cont_dir == 'UP_CONTINUATION' or (consec >= 3 and velocity > 0.05):
        if rsi < 30:
            return 'UP_REVERSAL'
        return 'UP_CONTINUATION'
    return 'FLAT'


def get_regime(prices):
    if len(prices) < 20: return 'unknown'
    r = prices[-20:]
    vol = np.std(r) / max(abs(np.mean(r)), 1e-9)
    trend = (r[-1] - r[0]) / max(abs(r[0]), 1e-9)
    if vol < 0.01: return 'ranging'
    if vol > 0.05: return 'volatile'
    if trend > 0.02: return 'trending_up'
    if trend < -0.02: return 'trending_down'
    return 'balanced'


def get_timing_window(time_pct):
    for name, (lo, hi, _) in TIMING.items():
        if lo <= time_pct < hi:
            return name
    return 'UNKNOWN'


def get_bucket_zone(price):
    for zone, (lo, hi) in DEFAULT_BUCKET_ZONES.items():
        if lo <= price < hi:
            return zone
    return 'expensive' if price >= 0.50 else 'ultra_cheap'


# ════════════════════════════════════════════════════════════════════
# MAIN SIMULATION
# ════════════════════════════════════════════════════════════════════

def run_v216():
    sys.stdout.reconfigure(line_buffering=True)
    np.random.seed(42)
    random.seed(42)

    # §3: Initialize organism modules
    topology = LiquidityTopology()
    friction = FrictionSurface(window_size=500)
    router = ExecutionRouter()
    memory = ExecutionMemory(
        memory_path=str(OUT_DIR / "execution_memory.json")
    )

    print("=" * 70)
    print("V21.6 ADAPTIVE EXECUTION ORGANISM — PMXT 2000-TRADE SIMULATION")
    print("=" * 70)
    print(f"Hierarchy: survivability > fillability > realized EV > raw convexity")
    print(f"Dynamic buckets, maker/taker routing, friction-adapted scoring")
    print()

    # Load PMXT data
    files = sorted(PMXT_DIR.glob("*.parquet"))
    valid_files = []
    for f in files:
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000:
                valid_files.append(f)
        except:
            continue
    print(f"Valid PMXT files: {len(valid_files)}/{len(files)}")

    bankroll = BANKROLL_START
    all_trades = []
    state_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    bucket_zone_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    timing_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    mode_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    side_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    rejections = defaultdict(int)
    total_trades = 0

    start = time.time()

    for fidx, fpath in enumerate(valid_files):
        if total_trades >= TARGET_TRADES:
            break

        print(f"\n[{fidx+1}/{len(valid_files)}] {fpath.name}...", end=" ", flush=True)
        fstart = time.time()

        pf = pq.ParquetFile(str(fpath))
        n_rgs = pf.metadata.num_row_groups

        # Phase 1: Token accumulation
        token_data = {}
        token_prices = defaultdict(list)

        for rg_idx in range(n_rgs):
            try:
                t = pf.read_row_group(rg_idx, columns=['market', 'asset_id', 'price', 'event_type'])
            except:
                continue
            evs = t.column('event_type').to_pylist()
            keep = [i for i, e in enumerate(evs) if e == 'price_change']
            t2 = t.take(keep) if keep else None
            del t

            if t2 is None or len(t2) == 0:
                continue

            n = len(t2)
            prices_arr = t2.column('price').to_numpy().astype(np.float64)
            mkt_col = t2.column('market')
            aid_col = t2.column('asset_id')
            step = max(1, n // 10000)

            for i in range(0, n, step):
                p = float(prices_arr[i])
                if p < 0.02 or p > 0.99:
                    continue
                m = mkt_col[i]
                cid = m.hex() if hasattr(m, 'hex') else str(m)
                aid = str(aid_col[i])
                key = (cid, aid)
                if key not in token_data:
                    token_data[key] = [0.0, 0, p, p, p]
                token_data[key][0] += p
                token_data[key][1] += 1
                token_data[key][2] = p
                token_data[key][3] = min(token_data[key][3], p)
                token_data[key][4] = max(token_data[key][4], p)
                if len(token_prices[key]) < 800:
                    token_prices[key].append(p)

            del t2
            if rg_idx % 20 == 0:
                gc.collect()

        # Phase 2: Trade with organism
        cid_aids = defaultdict(set)
        for (cid, aid) in token_data:
            cid_aids[cid].add(aid)
        pairs = [(cid, list(aids)) for cid, aids in cid_aids.items() if len(aids) == 2]
        file_trades_before = total_trades

        for cid, aid_list in pairs:
            if total_trades >= TARGET_TRADES:
                break

            k1 = (cid, aid_list[0])
            k2 = (cid, aid_list[1])
            if k1 not in token_data or k2 not in token_data:
                continue
            s1, s2 = token_data[k1], token_data[k2]
            if s1[1] < 15 or s2[1] < 15:
                continue
            m1, m2 = s1[0]/s1[1], s2[0]/s2[1]

            if m1 < m2:
                cheap_k, cheap_s, rich_k, rich_s = k1, s1, k2, s2
            else:
                cheap_k, cheap_s, rich_k, rich_s = k2, s2, k1, s1

            cheap_mean = cheap_s[0] / cheap_s[1]
            if cheap_mean > 0.55 or cheap_mean < 0.02:
                continue
            if cheap_s[4] - cheap_s[3] < 0.02:
                continue

            # §6: Binary settlement
            settlement = 1.0 if token_data[cheap_k][2] > token_data[rich_k][2] else 0.0
            won = settlement > 0.5

            cheap_arr = np.array(token_prices.get(cheap_k, []))
            if len(cheap_arr) < 15:
                continue

            rsi_arr = compute_rsi(cheap_arr)
            n_pts = len(cheap_arr)
            num_signals = min(8, max(1, n_pts // 30))
            signal_step = max(1, (n_pts - 20) // num_signals)

            for sig_idx in range(num_signals):
                if total_trades >= TARGET_TRADES:
                    break

                i = 20 + sig_idx * signal_step
                if i >= n_pts:
                    break

                current_price = float(cheap_arr[i])
                current_rsi = float(rsi_arr[i])
                time_pct = i / max(n_pts, 1)

                # Signal computation
                window = cheap_arr[:i+1]
                accel_score, velocity, consec = compute_acceleration(window)
                cont_score, cont_dir = compute_continuation(window, consec)
                state = classify_state(accel_score, velocity, consec, cont_dir, current_rsi)
                regime = get_regime(window)
                timing = get_timing_window(time_pct)
                bucket_zone = get_bucket_zone(current_price)

                # === V21.6: DYNAMIC PIPELINE ===

                # §2: Raw convexity score (from V21.5 engine)
                raw_composite = 0.0
                pri = DIRECTION_PRIORITY.get(state, 0.1)

                ps = min(1.0, abs(velocity) / 0.3) * (1.3 if state.startswith('DOWN') else 0.7)
                as_ = 0.5 + 0.5 * max(-1, min(1, accel_score))
                ls = 0.2 + 0.3 * abs(cont_score)
                vs = min(1.0, abs(velocity) * 3.0) if regime in ('volatile', 'trending_up', 'trending_down') else 0.1
                for _, (lo, hi, tpri) in TIMING.items():
                    if lo <= time_pct < hi:
                        ts = tpri; break
                else:
                    ts = 0.3
                if abs(velocity) < 0.03:
                    ts *= 0.3
                se = 0.02 if current_price < 0.10 else (0.018 if current_price < 0.12 else (0.015 if current_price < 0.20 else 0.025))
                es = 0.9 if se < 0.019 else (0.6 if se < 0.02 else 0.2)
                rs = (0.9 if current_rsi < 25 else (0.7 if current_rsi < 30 else (0.55 if current_rsi < 35 else (0.6 if current_rsi > 73 else (0.5 if current_rsi > 70 else 0.3)))))
                raw_composite = (W['persist']*ps + W['accel']*as_ + W['lag']*ls + W['vol']*vs + W['tte']*ts + W['exec']*es + W['rsi']*rs) * pri

                # §5: Check friction surface — should we enter?
                should_enter, enter_reason = friction.should_enter(
                    current_price, bucket_zone
                )
                if not should_enter:
                    rejections[enter_reason] += 1
                    continue

                # §2: Net convexity (realized post-friction)
                win_prob = 0.35  # Base estimate
                net_convexity = friction.compute_net_convexity(
                    current_price, win_prob, bucket_zone
                )
                if net_convexity <= 0:
                    rejections['negative_net_convexity'] += 1
                    continue

                # §4: Dynamic bucket scoring
                bucket_score_val = net_convexity  # Simplified: uses net_convexity × survivability

                # §10: Execution memory — avoid hostile paths
                cell_key = ('BTC', '5m', 'DOWN' if state.startswith('DOWN') else 'UP',
                            bucket_zone, timing, regime)
                should_avoid, avoid_reason = memory.should_avoid(cell_key)
                if should_avoid:
                    rejections[f'avoid_{avoid_reason}'] += 1
                    continue

                # §7: Execution routing
                liquidity_score = topology.cells[cell_key].liquidity_score if cell_key in topology.cells else 0.5
                friction_score = friction.compute_friction_score(bucket_zone)
                hostility_score = topology.compute_hostility_score(
                    topology.cells[cell_key]) if cell_key in topology.cells else 0.3

                # Spread estimation from price
                spread_bps = 300 if current_price < 0.08 else (200 if current_price < 0.15 else 150)
                spread_trend = 'stable'
                if velocity < -0.1 and state.startswith('DOWN'):
                    spread_trend = 'tightening'  # DOWN momentum → spread collapsing for cheap tokens
                elif velocity > 0.1 and state.startswith('UP'):
                    spread_trend = 'widening'

                routing = router.route(
                    convexity_score=net_convexity,
                    liquidity_score=liquidity_score,
                    friction_score=friction_score,
                    spread_bps=spread_bps,
                    spread_trend=spread_trend,
                    momentum_strength=velocity,
                    volatility=abs(velocity),
                    hostility_score=hostility_score,
                    price=current_price,
                    bucket_zone=bucket_zone,
                )

                if routing.mode == ExecutionMode.ABORT:
                    rejections[f'abort_{routing.reason}'] += 1
                    continue

                # §6: Timing priority — EARLY and FINAL deprioritized unless very high score
                if timing in ('EARLY',) and net_convexity < 0.3:
                    rejections['timing_early'] += 1
                    continue

                # Position sizing: $1 fixed (§14), modulated by routing
                position_usd = POSITION_SIZE_USD * routing.size_pct

                # === Realized Fill Model (§5/§7) ===
                roll = random.random()
                if roll < STALE_QUOTE_RATE:
                    rejections['stale_quote'] += 1
                    # Record in friction/memory
                    obs = FrictionObservation(
                        slippage_bps=0, expected_slippage_bps=0,
                        queue_delay_ms=0, fill_latency_ms=0, fill_pct=0.0,
                        was_rejected=False, was_stale=True, was_repriced=False,
                        repriced_delta=0.0, spread_bps=spread_bps,
                        entry_price=current_price, bucket_zone=bucket_zone,
                        asset='BTC', timing=timing, regime=regime,
                    )
                    friction.record(obs)
                    topology.record_observation(
                        'BTC', '5m', 'DOWN' if state.startswith('DOWN') else 'UP',
                        current_price, timing, regime,
                        filled=False, stale=True, spread_bps=spread_bps,
                    )
                    continue

                fill_pct = 1.0
                if roll < STALE_QUOTE_RATE + FILL_REJECTION_RATE:
                    rejections['fill_rejected'] += 1
                    obs = FrictionObservation(
                        slippage_bps=0, expected_slippage_bps=0,
                        queue_delay_ms=random.randint(100, 1000), fill_latency_ms=0,
                        fill_pct=0.0, was_rejected=True, was_stale=False,
                        was_repriced=False, repriced_delta=0.0,
                        spread_bps=spread_bps, entry_price=current_price,
                        bucket_zone=bucket_zone, asset='BTC',
                        timing=timing, regime=regime,
                    )
                    friction.record(obs)
                    topology.record_observation(
                        'BTC', '5m', 'DOWN' if state.startswith('DOWN') else 'UP',
                        current_price, timing, regime,
                        filled=False, rejected=True, spread_bps=spread_bps,
                    )
                    continue

                if roll < STALE_QUOTE_RATE + FILL_REJECTION_RATE + PARTIAL_FILL_RATE:
                    fill_pct = 0.5 + random.random() * 0.3

                # Execution mode affects slippage
                if routing.mode == ExecutionMode.MAKER:
                    spread_used = SPREAD_COST * 0.6  # Better fills for makers
                    slippage_mult = 0.7  # Less slippage
                elif routing.mode == ExecutionMode.TAKER:
                    spread_used = SPREAD_COST * 1.2  # Pay the spread
                    slippage_mult = 1.3  # More slippage
                else:  # HYBRID
                    spread_used = SPREAD_COST * 0.85
                    slippage_mult = 1.0

                # Repricing
                was_repriced = random.random() < REPRICING_DRIFT_RATE
                repriced_delta = random.gauss(0, 0.005) * current_price if was_repriced else 0.0

                eff_price = current_price + spread_used + current_price * SLIPPAGE_PCT * slippage_mult + abs(repriced_delta)
                eff_price = min(eff_price, 0.99)

                shares = (position_usd * fill_pct) / eff_price
                queue_delay_ms = random.randint(50, 500) if routing.mode == ExecutionMode.MAKER else random.randint(20, 200)
                fill_latency_ms = queue_delay_ms + random.randint(100, 1500)

                # Slippage calculation
                actual_slippage_bps = (eff_price - current_price) / max(current_price, 0.001) * 10000

                # §12: Binary settlement
                pnl = shares * (settlement - eff_price)
                side = 'DOWN' if state.startswith('DOWN') else ('UP' if state.startswith('UP') else 'FLAT')

                # === Record observations ===

                # §5: Friction observation
                obs = FrictionObservation(
                    slippage_bps=actual_slippage_bps,
                    expected_slippage_bps=current_price * SLIPPAGE_PCT * slippage_mult / max(current_price, 0.001) * 10000,
                    queue_delay_ms=queue_delay_ms,
                    fill_latency_ms=fill_latency_ms,
                    fill_pct=fill_pct,
                    was_rejected=False, was_stale=False,
                    was_repriced=was_repriced, repriced_delta=repriced_delta,
                    spread_bps=spread_bps,
                    entry_price=current_price, bucket_zone=bucket_zone,
                    asset='BTC', timing=timing, regime=regime,
                )
                friction.record(obs)

                # §3: Topology observation
                topology.record_observation(
                    'BTC', '5m', side, current_price, timing, regime,
                    filled=True, partial=(fill_pct < 1.0),
                    slippage_bps=actual_slippage_bps,
                    spread_bps=spread_bps,
                    queue_delay_ms=queue_delay_ms,
                    fill_latency_ms=fill_latency_ms,
                    fill_pct=fill_pct, pnl=pnl, won=won,
                )

                # §10: Execution memory trace
                trace = ExecutionTrace(
                    trade_id=total_trades + 1,
                    asset='BTC', interval='5m', side=side,
                    bucket_zone=bucket_zone, timing=timing, regime=regime,
                    execution_mode=routing.mode.value,
                    entry_price=current_price, actual_entry=eff_price,
                    slippage_bps=actual_slippage_bps,
                    fill_pct=fill_pct, fill_latency_ms=fill_latency_ms,
                    settlement=settlement, won=won, pnl=pnl,
                    realized_ev=pnl, net_convexity=net_convexity,
                    friction_score=friction_score,
                    liquidity_score=liquidity_score,
                    hostility_score=hostility_score,
                    spread_bps=spread_bps, spread_trend=spread_trend,
                )
                memory.record(trace)

                # Trade record
                bankroll += pnl
                total_trades += 1
                all_trades.append({
                    'trade_id': total_trades,
                    'state': state, 'side': side, 'bucket_zone': bucket_zone,
                    'timing': timing, 'execution_mode': routing.mode.value,
                    'entry_price': current_price, 'actual_entry': eff_price,
                    'slippage_bps': actual_slippage_bps,
                    'fill_pct': fill_pct, 'was_repriced': was_repriced,
                    'settlement': settlement, 'won': won, 'pnl': pnl,
                    'net_convexity': net_convexity,
                    'friction_score': friction_score,
                    'liquidity_score': liquidity_score,
                    'hostility_score': hostility_score,
                    'routing_confidence': routing.confidence,
                    'routing_reason': routing.reason,
                    'size_pct': routing.size_pct,
                    'regime': regime, 'velocity': velocity,
                    'acceleration': accel_score, 'rsi': current_rsi,
                    'time_pct': time_pct,
                })

                # Stats
                state_stats[state]['trades'] += 1
                if won: state_stats[state]['wins'] += 1
                state_stats[state]['pnl'] += pnl
                bucket_zone_stats[bucket_zone]['trades'] += 1
                if won: bucket_zone_stats[bucket_zone]['wins'] += 1
                bucket_zone_stats[bucket_zone]['pnl'] += pnl
                timing_stats[timing]['trades'] += 1
                if won: timing_stats[timing]['wins'] += 1
                timing_stats[timing]['pnl'] += pnl
                mode_stats[routing.mode.value]['trades'] += 1
                if won: mode_stats[routing.mode.value]['wins'] += 1
                mode_stats[routing.mode.value]['pnl'] += pnl
                side_stats[side]['trades'] += 1
                if won: side_stats[side]['wins'] += 1
                side_stats[side]['pnl'] += pnl

                if total_trades % 200 == 0:
                    wr = sum(1 for t in all_trades if t['won']) / len(all_trades) * 100
                    print(f"\n  T={total_trades} WR={wr:.1f}% bank=${bankroll:.2f} P&L=${bankroll-BANKROLL_START:+.2f}", flush=True)

        elapsed = time.time() - fstart
        new = total_trades - file_trades_before
        if new > 0:
            wr = sum(1 for t in all_trades[-new:] if t['won']) / new * 100
            print(f"{new} trades, WR={wr:.1f}%, bank=${bankroll:.2f} ({elapsed:.0f}s)")
        else:
            print(f"no new trades ({elapsed:.0f}s)")

        del token_data, token_prices, cid_aids, pairs
        gc.collect()

    # ════════════════════════════════════════════════════════════════════
    # RESULTS
    # ════════════════════════════════════════════════════════════════════

    n = len(all_trades)
    if n == 0:
        print("\nNO TRADES GENERATED")
        print(f"Rejections: {dict(rejections)}")
        return

    total_wins = sum(1 for t in all_trades if t['won'])
    total_pnl = sum(t['pnl'] for t in all_trades)
    wr = total_wins / n * 100
    gp = sum(t['pnl'] for t in all_trades if t['pnl'] > 0)
    gl = abs(sum(t['pnl'] for t in all_trades if t['pnl'] < 0))
    pf = gp / max(gl, 0.01)
    pnls = [t['pnl'] for t in all_trades]
    mu = np.mean(pnls); sd = np.std(pnls)
    sharpe = mu / max(sd, 0.001) * np.sqrt(252)
    avg_win = np.mean([t['pnl'] for t in all_trades if t['won']]) if total_wins > 0 else 0
    avg_loss = abs(np.mean([t['pnl'] for t in all_trades if not t['won']])) if n-total_wins > 0 else 0
    realized_ev = wr/100 * avg_win - (1-wr/100) * avg_loss
    payout = avg_win / max(avg_loss, 0.01)

    # Drawdown
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    dd = (peak - cum) / np.maximum(peak + BANKROLL_START, 1)
    max_dd = np.max(dd) * 100

    # Net convexity stats
    nc_vals = [t['net_convexity'] for t in all_trades]
    friction_vals = [t['friction_score'] for t in all_trades]
    liq_vals = [t['liquidity_score'] for t in all_trades]
    host_vals = [t['hostility_score'] for t in all_trades]
    slip_vals = [t['slippage_bps'] for t in all_trades]

    print("\n" + "=" * 70)
    print("V21.6 ADAPTIVE EXECUTION ORGANISM — RESULTS")
    print("=" * 70)

    print(f"\n{'METRIC':<40s} {'VALUE':>15s}")
    print("-" * 57)
    print(f"{'Total trades':<40s} {n:>15d}")
    print(f"{'Wins / Losses':<40s} {total_wins:>7d}/{n-total_wins:<7d}")
    print(f"{'Win rate':<40s} {wr:>14.1f}%")
    print(f"{'Final bankroll':<40s} ${bankroll:>14.2f}")
    print(f"{'Total P&L':<40s} ${total_pnl:>14.2f}")
    print(f"{'ROI':<40s} {(bankroll/BANKROLL_START-1)*100:>14.1f}%")
    print(f"{'Realized EV per trade':<40s} ${realized_ev:>14.4f}")
    print(f"{'Payout ratio':<40s} {payout:>15.2f}x")
    print(f"{'Profit factor':<40s} {pf:>15.2f}")
    print(f"{'Sharpe':<40s} {sharpe:>15.2f}")
    print(f"{'Max drawdown':<40s} {max_dd:>14.1f}%")

    print(f"\n{'NET CONVEXITY (§2)':}")
    print(f"  Mean: {np.mean(nc_vals):.4f}")
    print(f"  P50: {np.median(nc_vals):.4f}")
    print(f"  Positive: {sum(1 for v in nc_vals if v > 0)/len(nc_vals)*100:.1f}%")

    print(f"\n{'FRICTION (§5)':}")
    print(f"  Mean friction: {np.mean(friction_vals):.4f}")
    print(f"  Mean slippage: {np.mean(slip_vals):.1f}bps")
    print(f"  P99 slippage: {np.percentile(slip_vals, 99):.1f}bps")

    print(f"\n{'EXECUTION MODES (§7)':}")
    for mode in ['maker', 'taker', 'hybrid']:
        s = mode_stats.get(mode, {'trades': 0, 'wins': 0, 'pnl': 0.0})
        if s['trades'] > 0:
            mwr = s['wins'] / s['trades'] * 100
            print(f"  {mode:<15s}: {s['trades']:>5d} trades, {mwr:>5.1f}% WR, ${s['pnl']:>+10.2f}")

    print(f"\n{'BUCKET ZONES (§4 DYNAMIC)':}")
    for zone in ['ultra_cheap', 'cheap', 'mid_cheap', 'mid', 'mid_rich', 'rich', 'expensive']:
        s = bucket_zone_stats.get(zone, {'trades': 0, 'wins': 0, 'pnl': 0.0})
        if s['trades'] > 0:
            zwr = s['wins'] / s['trades'] * 100
            lo, hi = DEFAULT_BUCKET_ZONES.get(zone, (0, 1))
            print(f"  {zone:<15s} ({lo:.2f}-{hi:.2f}): {s['trades']:>5d} trades, {zwr:>5.1f}% WR, ${s['pnl']:>+10.2f}")

    print(f"\n{'TIMING (§6)':}")
    for tw in ['EARLY', 'FORMATION', 'MOMENTUM', 'LATE', 'FINAL']:
        s = timing_stats.get(tw, {'trades': 0, 'wins': 0, 'pnl': 0.0})
        if s['trades'] > 0:
            twr = s['wins'] / s['trades'] * 100
            print(f"  {tw:<15s}: {s['trades']:>5d} trades, {twr:>5.1f}% WR, ${s['pnl']:>+10.2f}")

    print(f"\n{'SIDE':}")
    for side in ['DOWN', 'UP']:
        s = side_stats[side]
        if s['trades'] > 0:
            swr = s['wins'] / s['trades'] * 100
            print(f"  {side:<10s}: {s['trades']:>5d} trades, {swr:>5.1f}% WR, ${s['pnl']:>+10.2f}")

    print(f"\n{'REJECTIONS':}")
    for k, v in sorted(rejections.items(), key=lambda x: -x[1]):
        print(f"  {k:<35s}: {v:>6d}")

    # §4: Dynamic bucket reranking
    bucket_ranks = topology.rerank_buckets()
    print(f"\n{'DYNAMIC BUCKET RANKING (§4)':}")
    for zone, score in sorted(bucket_ranks.items(), key=lambda x: -x[1]):
        s = bucket_zone_stats.get(zone, {'trades': 0, 'pnl': 0.0})
        print(f"  {zone:<15s}: score={score:.4f}, trades={s['trades']}, P&L=${s['pnl']:+.2f}")

    # §13: Promotion gates
    print(f"\n{'§13 PROMOTION GATES':}")
    print(f"  Live settlements:    {n}/500 {'✓' if n >= 500 else '✗'}")
    print(f"  Positive EV:        {'YES' if realized_ev > 0 else 'NO'} (${realized_ev:+.4f})")
    print(f"  PF ≥ 1.50:          {'YES' if pf >= 1.50 else 'NO'} ({pf:.2f})")
    print(f"  Avg slippage < 8%:  {'YES' if np.mean(slip_vals) < 800 else 'NO'} ({np.mean(slip_vals):.0f}bps)")
    print(f"  Max DD acceptable:   {'YES' if max_dd < 50 else 'NO'} ({max_dd:.1f}%)")
    all_promo = (n >= 500 and realized_ev > 0 and pf >= 1.50 and np.mean(slip_vals) < 800 and max_dd < 50)
    print(f"  *** PROMOTION READY: {'YES' if all_promo else 'NO'} ***")

    # Save outputs
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    results = {
        'version': 'V21_6_ADAPTIVE_EXECUTION_ORGANISM',
        'timestamp': ts,
        'total_trades': n,
        'config': {
            'bankroll': BANKROLL_START, 'position_size': POSITION_SIZE_USD,
            'dynamic_buckets': True, 'maker_taker_routing': True,
            'friction_surface': True, 'execution_memory': True,
            'net_convexity_scoring': True,
        },
        'summary': {
            'trades': n, 'wins': total_wins, 'wr': wr,
            'bankroll': bankroll, 'pnl': total_pnl,
            'roi': (bankroll/BANKROLL_START-1)*100,
            'realized_ev': realized_ev, 'payout_ratio': payout,
            'profit_factor': pf, 'sharpe': sharpe, 'max_dd': max_dd,
            'avg_slippage_bps': float(np.mean(slip_vals)),
            'net_convexity_mean': float(np.mean(nc_vals)),
            'friction_mean': float(np.mean(friction_vals)),
        },
        'state_stats': {k: dict(v) for k, v in state_stats.items()},
        'bucket_zone_stats': {k: dict(v) for k, v in bucket_zone_stats.items()},
        'timing_stats': {k: dict(v) for k, v in timing_stats.items()},
        'mode_stats': {k: dict(v) for k, v in mode_stats.items()},
        'side_stats': side_stats,
        'bucket_ranks': bucket_ranks,
        'routing_stats': router.get_routing_stats(),
        'memory_summary': memory.summary(),
        'rejections': dict(rejections),
    }
    with open(OUT_DIR / 'v216_simulation_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)

    # Save execution memory
    memory.save()

    # Save trade log
    with open(OUT_DIR / 'v216_trade_log.jsonl', 'w') as f:
        for t in all_trades:
            f.write(json.dumps(t, default=str) + '\n')

    elapsed_total = time.time() - start
    print(f"\nElapsed: {elapsed_total:.0f}s ({elapsed_total/60:.1f}min)")
    print(f"Output: {OUT_DIR}")


if __name__ == '__main__':
    run_v216()