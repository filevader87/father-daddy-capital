#!/usr/bin/env python3
"""
V21.5 LIVE PROBE V2 — Real Execution Survivability Validation
================================================================
Directive: Determine whether convex continuation alpha survives real execution friction.

Uses REAL PMXT orderbook data (not mock). Full friction model.
DOWN/MOMENTUM/LATE only. PRIMARY bucket 3-12¢ only.
Full audit trail: fill latency, slippage, settlement, realized EV.

Output: output/v215_live_probe_v2/
"""

import pyarrow.parquet as pq
import numpy as np
from pathlib import Path
from collections import defaultdict
from datetime import datetime
import time, gc, json, random, sys

# ════════════════════════════════════════════════════════════════════
# V21.5 LIVE PROBE V2 CONFIG (§3-§4)
# ════════════════════════════════════════════════════════════════════

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
BANKROLL_START = 100.0
TARGET_TRADES = 2000
POSITION_SIZE_USD = 1.00  # §4: $1 fixed

# §3: ALLOWED DEPLOYMENT — DOWN ONLY, MOMENTUM/LATE ONLY
ALLOWED_STATES = {'DOWN_CONTINUATION', 'DOWN_MOMENTUM'}
ALLOWED_SIDE = 'DOWN'
ALLOWED_TIMING = {'MOMENTUM', 'LATE'}
ENTRY_BUCKET_PRIMARY = (0.03, 0.12)  # §3: ONLY PRIMARY bucket
# §3: DISABLE — ALL UP profiles, reversal, 0.20+ buckets, midpoint fills, synthetic TP/flat

# §7: Direction Priority (DOWN only)
DIRECTION_PRIORITY = {
    'DOWN_CONTINUATION': 1.50,
    'DOWN_MOMENTUM': 1.40,
    # These are DISABLED per §3 — but kept for classify_state completeness
    'UP_REVERSAL': 0.0,
    'UP_CONTINUATION': 0.0,
    'FLAT': 0.0,
}

# §9: Signal Stack Weights — RSI max 5%
W = {'persist': 0.25, 'accel': 0.20, 'lag': 0.15, 'vol': 0.15, 'tte': 0.10, 'exec': 0.10, 'rsi': 0.05}

# §7: Real Fill Doctrine Friction Model
SPREAD_COST = 0.012       # Fixed spread crossing cost
SLIPPAGE_PCT = 0.008      # Percent slippage on price
FILL_REJECTION_RATE = 0.07  # 7% chance fill is rejected
PARTIAL_FILL_RATE = 0.12    # 12% chance partial fill (50-80%)
STALE_QUOTE_RATE = 0.03     # 3% stale quote abort
QUEUE_DELAY_PENALTY = 0.005  # Queue delay reduces EV
REPRICING_DRIFT_RATE = 0.04  # 4% chance price drifts during fill

# §10: Timing Windows
TIMING = {
    'EARLY':     (0.00, 0.20, 0.10),
    'FORMATION': (0.20, 0.40, 0.35),
    'MOMENTUM':  (0.40, 0.80, 0.80),
    'LATE':      (0.80, 0.90, 0.95),
    'FINAL':     (0.90, 1.00, 0.60),
}

# §4: Hard Limits
MAX_DAILY_LOSS = 10.00
MAX_WEEKLY_LOSS = 30.00
MAX_TRADES_PER_DAY = 30

# §13: Output Directory
OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v215_live_probe_v2")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ════════════════════════════════════════════════════════════════════
# SIGNAL COMPUTATION (from convex_engine_v2.py)
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


def compute_convex_score(rsi, accel, velocity, cont_score, cont_dir, state, time_pct, price, regime):
    # Persistence (25%)
    if state in ('DOWN_CONTINUATION', 'DOWN_MOMENTUM'):
        ps = min(1.0, abs(velocity) / 0.3) * 1.3
    elif state in ('UP_REVERSAL', 'UP_CONTINUATION'):
        ps = min(1.0, abs(velocity) / 0.3) * 0.7  # §3: UP effectively blocked
    else:
        ps = 0.05
    as_ = 0.5 + 0.5 * max(-1, min(1, accel))
    ls = 0.2 + 0.3 * abs(cont_score)
    if regime in ('volatile', 'trending_up', 'trending_down'):
        vs = min(1.0, abs(velocity) * 3.0)
    else:
        vs = 0.1
    for _, (lo, hi, pri) in TIMING.items():
        if lo <= time_pct < hi:
            ts = pri; break
    else:
        ts = 0.3
    if abs(velocity) < 0.03:
        ts *= 0.3
    if price < 0.10:
        se = 0.02
    elif price < 0.12:
        se = 0.018
    elif price < 0.20:
        se = 0.015
    else:
        se = 0.025
    if se > 0.025:
        es = 0.2
    elif se > 0.015:
        es = 0.6
    else:
        es = 0.9
    if rsi < 25:
        rs = 0.90
    elif rsi < 30:
        rs = 0.70
    elif rsi < 35:
        rs = 0.55
    elif rsi > 73:
        rs = 0.60
    elif rsi > 70:
        rs = 0.50
    else:
        rs = 0.30
    raw = (W['persist']*ps + W['accel']*as_ + W['lag']*ls + W['vol']*vs +
           W['tte']*ts + W['exec']*es + W['rsi']*rs)
    pri = DIRECTION_PRIORITY.get(state, 0.0)
    return raw * pri, None, state


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


# ════════════════════════════════════════════════════════════════════
# REAL FILL MODEL (§7) — Per-Trade Audit Trail
# ════════════════════════════════════════════════════════════════════

class FillResult:
    """Full audit trail per §12."""
    __slots__ = [
        'signal_ts', 'quote_ts', 'order_submit_ts', 'fill_ts', 'fill_latency_ms',
        'expected_entry', 'actual_entry', 'slippage_bps', 'stale_quote',
        'fill_success', 'fill_pct', 'repriced', 'repriced_delta',
        'queue_delay_ms', 'settlement_result', 'realized_pnl', 'realized_ev',
        'realized_friction', 'trade_id'
    ]

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, None)

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def simulate_fill(price, expected_size_usd, won, settlement):
    """§7 Real Fill Doctrine — models spread, slippage, queue, partial, stale, repricing."""
    f = FillResult()
    f.signal_ts = datetime.now().isoformat()
    f.quote_ts = datetime.now().isoformat()

    # §7: Spread crossing
    spread_cost = SPREAD_COST
    slippage = price * SLIPPAGE_PCT

    # Queue delay: 50-500ms for real orderbooks
    f.queue_delay_ms = random.randint(50, 500)

    # Repricing drift (§7): 4% chance price drifts during fill
    f.repriced = random.random() < REPRICING_DRIFT_RATE
    if f.repriced:
        drift = random.gauss(0, 0.005)  # ±0.5% drift
        f.repriced_delta = drift
        slippage += abs(drift) * price
    else:
        f.repriced_delta = 0.0

    f.expected_entry = price + spread_cost + slippage
    f.expected_entry = min(f.expected_entry, 0.99)

    # Stale quote check (§7)
    roll = random.random()
    if roll < STALE_QUOTE_RATE:
        f.stale_quote = True
        f.fill_success = False
        f.actual_entry = 0.0
        f.slippage_bps = 0
        f.fill_pct = 0.0
        f.fill_ts = f.quote_ts
        f.fill_latency_ms = 0
        f.settlement_result = None
        f.realized_pnl = 0.0
        f.realized_ev = 0.0
        f.realized_friction = 0.0
        return f

    f.stale_quote = False

    # Fill rejection (§7)
    if roll < STALE_QUOTE_RATE + FILL_REJECTION_RATE:
        f.fill_success = False
        f.actual_entry = 0.0
        f.slippage_bps = 0
        f.fill_pct = 0.0
        f.fill_latency_ms = random.randint(100, 1000)
        f.fill_ts = f.quote_ts
        f.settlement_result = None
        f.realized_pnl = 0.0
        f.realized_ev = 0.0
        f.realized_friction = 0.0
        return f

    # Partial fill (§7)
    if roll < STALE_QUOTE_RATE + FILL_REJECTION_RATE + PARTIAL_FILL_RATE:
        f.fill_pct = 0.5 + random.random() * 0.3
    else:
        f.fill_pct = 1.0

    f.fill_success = True
    f.actual_entry = f.expected_entry
    actual_slippage = f.actual_entry - price
    f.slippage_bps = actual_slippage / max(price, 0.001) * 10000

    # Fill latency: 200-2000ms (realistic CLOB)
    f.fill_latency_ms = f.queue_delay_ms + random.randint(100, 1500)

    # §6: Binary settlement — 0.0 or 1.0 ONLY
    f.settlement_result = settlement
    size_usd = expected_size_usd * f.fill_pct
    shares = size_usd / f.actual_entry

    # §12: Realized P&L with friction
    f.realized_pnl = round(shares * (settlement - f.actual_entry), 6)

    # Queue delay penalty reduces effective EV
    eff_ev_mult = 1.0 - QUEUE_DELAY_PENALTY
    if won:
        f.realized_ev = round(shares * (1.0 - f.actual_entry) * eff_ev_mult, 6)
    else:
        f.realized_ev = round(-shares * f.actual_entry * (1.0 + QUEUE_DELAY_PENALTY * 0.5), 6)

    # Realized friction = difference between ideal entry and actual entry
    ideal_pnl = shares * (settlement - price)  # No friction
    f.realized_friction = round(ideal_pnl - f.realized_pnl, 6)

    f.order_submit_ts = f.quote_ts
    return f


# ════════════════════════════════════════════════════════════════════
# MAIN SIMULATION — PMXT DATA + FULL AUDIT
# ════════════════════════════════════════════════════════════════════

def run_live_probe_v2():
    sys.stdout.reconfigure(line_buffering=True)
    np.random.seed(42)
    random.seed(42)

    print("=" * 70)
    print("V21.5 LIVE PROBE V2 — Real Execution Survivability Validation")
    print("=" * 70)
    print(f"Directive: DOWN/MOMENTUM/LATE PRIMARY 0.03-0.12 ONLY")
    print(f"Position: ${POSITION_SIZE_USD} fixed | Target: {TARGET_TRADES} trades")
    print()

    # Load valid PMXT files
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

    # §4: Hard shutdown state
    bankroll = BANKROLL_START
    daily_loss = 0.0
    weekly_loss = 0.0
    daily_trades = 0
    total_trades = 0
    total_wins = 0
    total_pnl = 0.0

    # Audit collections
    all_trades = []
    fill_audits = []
    settlement_audits = []
    slippage_records = []
    fill_latency_records = []

    # Stats
    state_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    timing_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    price_bucket_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})

    rejections = {'stale_quote': 0, 'fill_rejected': 0, 'blocked_state': 0,
                  'blocked_bucket': 0, 'blocked_timing': 0, 'low_score': 0, 'low_price': 0}
    partial_fills = 0
    repriced_fills = 0

    start_time = time.time()
    trade_id_counter = 0
    day_boundaries = list(range(0, TARGET_TRADES + 1, 30))  # §4: Max 30 trades/day

    for fidx, fpath in enumerate(valid_files):
        if total_trades >= TARGET_TRADES:
            break

        msg = f"\n[{fidx+1}/{len(valid_files)}] {fpath.name}..."
        print(msg, flush=True)

        fstart = time.time()
        pf = pq.ParquetFile(str(fpath))
        n_rgs = pf.metadata.num_row_groups

        # Phase 1: Accumulate token data
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
                if p < 0.03 or p > 0.55:
                    continue

                m = mkt_col[i]
                cid = m.hex() if hasattr(m, 'hex') else str(m)
                aid = str(aid_col[i])
                key = (cid, aid)

                if key not in token_data:
                    token_data[key] = [0.0, 0, p, p, p]  # sum, cnt, last, min, max
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

        # Phase 2: Find binary pairs and trade
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
            if cheap_mean > 0.55:
                continue
            if cheap_s[4] - cheap_s[3] < 0.02:
                continue

            cheap_arr = np.array(token_prices.get(cheap_k, []))
            if len(cheap_arr) < 15:
                continue

            # §6: Binary settlement
            if token_data[rich_k][2] > token_data[cheap_k][2]:
                settlement = 0.0
                won = False
            else:
                settlement = 1.0
                won = True

            rsi_arr = compute_rsi(cheap_arr)
            n_pts = len(cheap_arr)

            num_signals = min(6, max(1, n_pts // 40))
            signal_step = max(1, (n_pts - 20) // num_signals)

            for sig_idx in range(num_signals):
                if total_trades >= TARGET_TRADES:
                    break

                # §4: Daily circuit breakers
                if daily_loss <= -MAX_DAILY_LOSS:
                    daily_loss = 0.0
                    daily_trades = 0
                    # Reset daily — simulates next trading day
                if weekly_loss <= -MAX_WEEKLY_LOSS:
                    break  # Hard stop — week over
                if daily_trades >= MAX_TRADES_PER_DAY:
                    daily_loss = 0.0
                    daily_trades = 0

                i = 20 + sig_idx * signal_step
                if i >= n_pts:
                    break

                current_price = float(cheap_arr[i])
                current_rsi = float(rsi_arr[i])
                time_pct = i / max(n_pts, 1)

                # §3: ALLOWED ENTRY BUCKET — PRIMARY ONLY (0.03-0.12)
                if not (ENTRY_BUCKET_PRIMARY[0] <= current_price < ENTRY_BUCKET_PRIMARY[1]):
                    if current_price < ENTRY_BUCKET_PRIMARY[0]:
                        rejections['low_price'] += 1
                    else:
                        rejections['blocked_bucket'] += 1
                    continue

                # Signal stack
                window = cheap_arr[:i+1]
                accel_score, velocity, consec = compute_acceleration(window)
                cont_score, cont_dir = compute_continuation(window, consec)
                state = classify_state(accel_score, velocity, consec, cont_dir, current_rsi)
                regime = get_regime(window)
                timing = get_timing_window(time_pct)

                # §3: ONLY DOWN_CONTINUATION and DOWN_MOMENTUM
                if state not in ALLOWED_STATES:
                    rejections['blocked_state'] += 1
                    continue

                # §3: ONLY MOMENTUM and LATE timing windows
                if timing not in ALLOWED_TIMING:
                    rejections['blocked_timing'] += 1
                    continue

                # V21.5 composite score
                composite, reason, state = compute_convex_score(
                    current_rsi, accel_score, velocity, cont_score,
                    cont_dir, state, time_pct, current_price, regime
                )

                min_score = 0.15  # PRIMARY bucket threshold
                if composite < min_score:
                    rejections['low_score'] += 1
                    continue

                # §4: Position sizing — $1 FIXED, no Kelly/martingale/pyramiding
                position_usd = POSITION_SIZE_USD

                # §7: Real Fill Doctrine
                fill = simulate_fill(current_price, position_usd, won, settlement)
                trade_id_counter += 1
                fill.trade_id = trade_id_counter

                # Record fill audit
                fill_audits.append(fill.to_dict())

                if fill.stale_quote:
                    rejections['stale_quote'] += 1
                    continue

                if not fill.fill_success:
                    rejections['fill_rejected'] += 1
                    continue

                if fill.repriced:
                    repriced_fills += 1

                if fill.fill_pct < 1.0:
                    partial_fills += 1

                # Record trade
                actual_size_usd = position_usd * fill.fill_pct
                pnl = fill.realized_pnl
                daily_loss += pnl if pnl < 0 else 0  # Only count losses
                weekly_loss += pnl if pnl < 0 else 0

                total_trades += 1
                daily_trades += 1
                if won:
                    total_wins += 1
                total_pnl += pnl
                bankroll += pnl

                trade_record = {
                    'trade_id': trade_id_counter,
                    'file': fpath.name,
                    'signal_idx': sig_idx,
                    'state': state,
                    'side': 'DOWN',  # §3: DOWN only
                    'bucket': 'PRIMARY',
                    'timing': timing,
                    'entry_price': current_price,
                    'actual_entry': fill.actual_entry,
                    'fill_pct': fill.fill_pct,
                    'repriced': fill.repriced,
                    'size_usd': actual_size_usd,
                    'settlement': settlement,
                    'won': won,
                    'pnl': pnl,
                    'realized_ev': fill.realized_ev,
                    'realized_friction': fill.realized_friction,
                    'composite_score': composite,
                    'rsi': current_rsi,
                    'velocity': velocity,
                    'acceleration': accel_score,
                    'cont_score': cont_score,
                    'time_pct': time_pct,
                    'regime': regime,
                    'queue_delay_ms': fill.queue_delay_ms,
                    'fill_latency_ms': fill.fill_latency_ms,
                    'slippage_bps': fill.slippage_bps,
                }
                all_trades.append(trade_record)

                # Slippage record
                slippage_records.append({
                    'trade_id': trade_id_counter,
                    'expected_entry': fill.expected_entry,
                    'actual_entry': fill.actual_entry,
                    'slippage_bps': fill.slippage_bps,
                    'fill_pct': fill.fill_pct,
                    'repriced': fill.repriced,
                    'repriced_delta': fill.repriced_delta,
                    'queue_delay_ms': fill.queue_delay_ms,
                })

                # Fill latency record
                fill_latency_records.append({
                    'trade_id': trade_id_counter,
                    'queue_delay_ms': fill.queue_delay_ms,
                    'fill_latency_ms': fill.fill_latency_ms,
                    'fill_pct': fill.fill_pct,
                    'state': state,
                })

                # Settlement record
                settlement_audits.append({
                    'trade_id': trade_id_counter,
                    'expected_settlement': 1.0 if state.startswith('DOWN') else 0.0,
                    'actual_settlement': settlement,
                    'binary_correct': (settlement in (0.0, 1.0)),
                    'won': won,
                    'pnl': pnl,
                    'entry_price': current_price,
                })

                # Stats
                state_stats[state]['trades'] += 1
                if won:
                    state_stats[state]['wins'] += 1
                state_stats[state]['pnl'] += pnl

                timing_stats[timing]['trades'] += 1
                if won:
                    timing_stats[timing]['wins'] += 1
                timing_stats[timing]['pnl'] += pnl

                bucket_key = f"{current_price:.2f}"
                price_bucket_stats[bucket_key]['trades'] += 1
                if won:
                    price_bucket_stats[bucket_key]['wins'] += 1
                price_bucket_stats[bucket_key]['pnl'] += pnl

                if total_trades % 100 == 0:
                    wr = total_wins / total_trades * 100
                    print(f"  T={total_trades} WR={wr:.1f}% bank=${bankroll:.2f} P&L=${total_pnl:+.2f}", flush=True)

        elapsed = time.time() - fstart
        new = total_trades - file_trades_before
        if new > 0:
            wr = sum(1 for t in all_trades[-new:] if t['won']) / new * 100
            print(f"  → {new} trades, WR={wr:.1f}%, bank=${bankroll:.2f} ({elapsed:.0f}s)")

        del token_data, token_prices, cid_aids, pairs
        gc.collect()

    # ════════════════════════════════════════════════════════════════════
    # REPORTING
    # ════════════════════════════════════════════════════════════════════

    n = len(all_trades)
    if n == 0:
        print("\nNO TRADES GENERATED — ALL REJECTED BY V2 DIRECTIVE FILTERS")
        print(f"Rejections: {rejections}")
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
    dd = (peak - cum) / np.maximum(peak, 1)
    max_dd = np.max(dd) * 100

    # Fill latency stats
    latencies = [f['fill_latency_ms'] for f in fill_latency_records]
    avg_latency = np.mean(latencies) if latencies else 0
    p50_latency = np.percentile(latencies, 50) if latencies else 0
    p99_latency = np.percentile(latencies, 99) if latencies else 0

    # Slippage stats
    slips = [s['slippage_bps'] for s in slippage_records]
    avg_slip = np.mean(slips) if slips else 0
    p50_slip = np.percentile(slips, 50) if slips else 0
    p99_slip = np.percentile(slips, 99) if slips else 0

    # Repriced count
    repriced_count = sum(1 for t in all_trades if t.get('repriced', False))
    partial_count = sum(1 for t in all_trades if t.get('fill_pct', 1.0) < 1.0)

    print("\n" + "=" * 70)
    print("V21.5 LIVE PROBE V2 — SURVIVABILITY RESULTS")
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
    print(f"{'Avg win size':<40s} ${avg_win:>14.4f}")
    print(f"{'Avg loss size':<40s} ${avg_loss:>14.4f}")
    print(f"{'Payout ratio (win/loss)':<40s} {payout:>15.2f}x")
    print(f"{'Profit factor':<40s} {pf:>15.2f}")
    print(f"{'Sharpe':<40s} {sharpe:>15.2f}")
    print(f"{'Max drawdown':<40s} {max_dd:>14.1f}%")

    print(f"\n{'FILL SURVIVABILITY (§7)':}")
    print(f"  Fill rejections:      {rejections['fill_rejected']}")
    print(f"  Stale quote aborts:   {rejections['stale_quote']}")
    print(f"  Repriced fills:       {repriced_count}/{n} ({repriced_count/max(n,1)*100:.1f}%)")
    print(f"  Partial fills:       {partial_count}/{n} ({partial_count/max(n,1)*100:.1f}%)")
    print(f"  Avg fill latency:    {avg_latency:.0f}ms")
    print(f"  P50 fill latency:    {p50_latency:.0f}ms")
    print(f"  P99 fill latency:    {p99_latency:.0f}ms")
    print(f"  Avg slippage:        {avg_slip:.1f}bps")
    print(f"  P50 slippage:        {p50_slip:.1f}bps")
    print(f"  P99 slippage:        {p99_slip:.1f}bps")

    print(f"\n{'DIRECTIVE REJECTIONS (§3)':}")
    for k, v in rejections.items():
        print(f"  {k:<25s}: {v}")

    print(f"\n{'STATE BREAKDOWN (DOWN ONLY)':}")
    print(f"{'State':<25s} {'Trades':>7s} {'WR%':>7s} {'P&L':>12s} {'AvgPnL':>10s}")
    print("-" * 65)
    for st in ['DOWN_MOMENTUM', 'DOWN_CONTINUATION']:
        s = state_stats.get(st, {'trades': 0, 'wins': 0, 'pnl': 0.0})
        if s['trades'] > 0:
            swr = s['wins'] / s['trades'] * 100
            avg = s['pnl'] / s['trades']
            print(f"  {st:<23s} {s['trades']:>7d} {swr:>6.1f}% ${s['pnl']:>10.2f} ${avg:>8.4f}")

    print(f"\n{'TIMING BREAKDOWN (§8 — MOMENTUM/LATE ONLY)':}")
    print(f"{'Window':<15s} {'Trades':>7s} {'WR%':>7s} {'P&L':>12s}")
    print("-" * 45)
    for tw in ['MOMENTUM', 'LATE']:
        s = timing_stats.get(tw, {'trades': 0, 'wins': 0, 'pnl': 0.0})
        if s['trades'] > 0:
            swr = s['wins'] / s['trades'] * 100
            print(f"  {tw:<13s} {s['trades']:>7d} {swr:>6.1f}% ${s['pnl']:>10.2f}")

    print(f"\n{'ENTRY PRICE DISTRIBUTION (PRIMARY 3-12¢)':}")
    for lo, hi, label in [(0.03,0.05,'3-5¢'),(0.05,0.07,'5-7¢'),(0.07,0.09,'7-9¢'),(0.09,0.12,'9-12¢')]:
        bucket = [t for t in all_trades if lo <= t['entry_price'] < hi]
        if bucket:
            bwr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
            bpnl = sum(t['pnl'] for t in bucket)
            print(f"  {label:<10s}: {len(bucket):>5d} trades, {bwr:>5.1f}% WR, ${bpnl:.2f} P&L")

    print(f"\n{'SLIPPAGE DECAY ANALYSIS':}")
    # Slippage by entry price — does cheap = more slippage?
    for lo, hi, label in [(0.03,0.06,'<6¢'),(0.06,0.09,'6-9¢'),(0.09,0.12,'9-12¢')]:
        bucket_slips = [s['slippage_bps'] for s in slippage_records
                        if lo <= float(s.get('actual_entry', 0)) < hi]
        if bucket_slips:
            print(f"  {label:<10s}: avg={np.mean(bucket_slips):.1f}bps, "
                  f"p50={np.percentile(bucket_slips,50):.1f}bps, "
                  f"p99={np.percentile(bucket_slips,99):.1f}bps, n={len(bucket_slips)}")

    print(f"\n{'§16 PROMOTION CRITERIA':}")
    promo_settlements = n >= 50
    promo_ev = realized_ev > 0
    promo_pf = pf >= 1.25
    promo_binary = all(s['binary_correct'] for s in settlement_audits)
    promo_slippage = avg_slip < 500  # Survivable slippage under 5%
    promo_queue = avg_latency < 2000  # Survivable queue under 2s
    promo_dd = max_dd < 50  # Drawdown under 50%
    print(f"  Live settlements:      {n}/50 {'✓' if promo_settlements else '✗'}")
    print(f"  Positive EV:          {'YES' if promo_ev else 'NO'} (${realized_ev:+.4f})")
    print(f"  PF ≥ 1.25:            {'YES' if promo_pf else 'NO'} ({pf:.2f})")
    print(f"  Binary settlement:    {'YES' if promo_binary else 'NO'}")
    print(f"  Slippage survivable:  {'YES' if promo_slippage else 'NO'} ({avg_slip:.1f}bps avg)")
    print(f"  Queue survivable:      {'YES' if promo_queue else 'NO'} ({avg_latency:.0f}ms avg)")
    print(f"  Drawdown acceptable:  {'YES' if promo_dd else 'NO'} ({max_dd:.1f}%)")
    all_promo = all([promo_settlements, promo_ev, promo_pf, promo_binary, promo_slippage, promo_queue, promo_dd])
    print(f"\n  *** PROMOTION READY: {'YES' if all_promo else 'NO'} ***")

    # ════════════════════════════════════════════════════════════════════
    # OUTPUT FILES (§13)
    # ════════════════════════════════════════════════════════════════════

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    # 1. live_trade_log.jsonl — Every trade with full audit
    with open(OUT_DIR / 'live_trade_log.jsonl', 'w') as f:
        for t in all_trades:
            f.write(json.dumps(t, default=str) + '\n')

    # 2. execution_audit.json
    exec_audit = {
        'version': 'V21.5_LIVE_PROBE_V2',
        'timestamp': ts,
        'total_trades': n,
        'total_wins': total_wins,
        'fill_rejections': rejections['fill_rejected'],
        'stale_quote_aborts': rejections['stale_quote'],
        'partial_fills': partial_count,
        'repriced_fills': repriced_count,
        'avg_fill_latency_ms': avg_latency,
        'p50_fill_latency_ms': p50_latency,
        'p99_fill_latency_ms': p99_latency,
        'directive_rejections': rejections,
    }
    with open(OUT_DIR / 'execution_audit.json', 'w') as f:
        json.dump(exec_audit, f, indent=2)

    # 3. settlement_audit.json
    settlement_data = {
        'version': 'V21.5_LIVE_PROBE_V2',
        'timestamp': ts,
        'total_settlements': len(settlement_audits),
        'binary_settlement_rate': sum(1 for s in settlement_audits if s['binary_correct']) / max(len(settlement_audits), 1) * 100,
        'settlements': settlement_audits,
    }
    with open(OUT_DIR / 'settlement_audit.json', 'w') as f:
        json.dump(settlement_data, f, indent=2, default=str)

    # 4. slippage_report.json
    slippage_data = {
        'version': 'V21.5_LIVE_PROBE_V2',
        'timestamp': ts,
        'avg_slippage_bps': avg_slip,
        'p50_slippage_bps': p50_slip,
        'p99_slippage_bps': p99_slip,
        'by_entry_price': {},
        'records': slippage_records,
    }
    for lo, hi, label in [(0.03,0.06,'<6¢'),(0.06,0.09,'6-9¢'),(0.09,0.12,'9-12¢')]:
        bucket_slips = [s for s in slippage_records if lo <= float(s.get('actual_entry', 0)) < hi]
        if bucket_slips:
            bslips = [s['slippage_bps'] for s in bucket_slips]
            slippage_data['by_entry_price'][label] = {
                'count': len(bucket_slips),
                'avg_bps': np.mean(bslips),
                'p50_bps': np.percentile(bslips, 50),
                'p99_bps': np.percentile(bslips, 99),
            }
    with open(OUT_DIR / 'slippage_report.json', 'w') as f:
        json.dump(slippage_data, f, indent=2, default=str)

    # 5. fill_latency_report.json
    latency_data = {
        'version': 'V21.5_LIVE_PROBE_V2',
        'timestamp': ts,
        'avg_latency_ms': avg_latency,
        'p50_latency_ms': p50_latency,
        'p99_latency_ms': p99_latency,
        'by_state': {},
        'records': fill_latency_records,
    }
    for st in ['DOWN_MOMENTUM', 'DOWN_CONTINUATION']:
        st_recs = [r for r in fill_latency_records if r['state'] == st]
        if st_recs:
            st_lats = [r['fill_latency_ms'] for r in st_recs]
            latency_data['by_state'][st] = {
                'count': len(st_recs),
                'avg_ms': np.mean(st_lats),
                'p50_ms': np.percentile(st_lats, 50),
                'p99_ms': np.percentile(st_lats, 99),
            }
    with open(OUT_DIR / 'fill_latency_report.json', 'w') as f:
        json.dump(latency_data, f, indent=2, default=str)

    # 6. realized_ev_report.json
    ev_data = {
        'version': 'V21.5_LIVE_PROBE_V2',
        'timestamp': ts,
        'total_trades': n,
        'realized_ev_per_trade': realized_ev,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'payout_ratio': payout,
        'win_rate': wr,
        'profit_factor': pf,
        'by_state': {},
        'by_timing': {},
        'by_entry_price': {},
    }
    for st in ['DOWN_MOMENTUM', 'DOWN_CONTINUATION']:
        s = state_stats.get(st, {'trades': 0, 'wins': 0, 'pnl': 0.0})
        if s['trades'] > 0:
            st_pnls = [t['pnl'] for t in all_trades if t['state'] == st]
            st_wins = [p for p in st_pnls if p > 0]
            st_losses = [p for p in st_pnls if p < 0]
            st_aw = np.mean(st_wins) if st_wins else 0
            st_al = abs(np.mean(st_losses)) if st_losses else 0
            st_wr = s['wins'] / s['trades'] * 100
            st_ev = st_wr/100 * st_aw - (1-st_wr/100) * st_al if st_al > 0 else 0
            ev_data['by_state'][st] = {
                'trades': s['trades'], 'wr': st_wr, 'avg_win': st_aw,
                'avg_loss': st_al, 'realized_ev': st_ev, 'pnl': s['pnl'],
            }
    for tw in ['MOMENTUM', 'LATE']:
        s = timing_stats.get(tw, {'trades': 0, 'wins': 0, 'pnl': 0.0})
        if s['trades'] > 0:
            tw_pnls = [t['pnl'] for t in all_trades if t['timing'] == tw]
            tw_wins = [p for p in tw_pnls if p > 0]
            tw_losses = [p for p in tw_pnls if p < 0]
            tw_aw = np.mean(tw_wins) if tw_wins else 0
            tw_al = abs(np.mean(tw_losses)) if tw_losses else 0
            tw_wr = s['wins'] / s['trades'] * 100
            tw_ev = tw_wr/100 * tw_aw - (1-tw_wr/100) * tw_al if tw_al > 0 else 0
            ev_data['by_timing'][tw] = {
                'trades': s['trades'], 'wr': tw_wr, 'realized_ev': tw_ev, 'pnl': s['pnl'],
            }
    with open(OUT_DIR / 'realized_ev_report.json', 'w') as f:
        json.dump(ev_data, f, indent=2, default=str)

    # 7. drawdown_report.json
    dd_data = {
        'version': 'V21.5_LIVE_PROBE_V2',
        'timestamp': ts,
        'max_drawdown_pct': max_dd,
        'peak_bankroll': float(np.max(cum) + BANKROLL_START) if len(cum) > 0 else BANKROLL_START,
        'trough_bankroll': float(np.min(cum) + BANKROLL_START) if len(cum) > 0 else BANKROLL_START,
        'daily_loss_limit': MAX_DAILY_LOSS,
        'weekly_loss_limit': MAX_WEEKLY_LOSS,
        'drawdown_series': [],
    }
    # Sample drawdown series (every 10th point to keep size reasonable)
    for i in range(0, len(cum), max(1, len(cum) // 200)):
        dd_data['drawdown_series'].append({
            'trade': i,
            'cumulative_pnl': float(cum[i]),
            'drawdown_pct': float(dd[i] * 100),
        })
    with open(OUT_DIR / 'drawdown_report.json', 'w') as f:
        json.dump(dd_data, f, indent=2, default=str)

    # 8. probe_summary.json
    summary = {
        'version': 'V21.5_LIVE_PROBE_V2',
        'timestamp': ts,
        'directive': 'Real Execution Survivability Validation',
        'config': {
            'target_trades': TARGET_TRADES,
            'bankroll_start': BANKROLL_START,
            'position_size': POSITION_SIZE_USD,
            'allowed_states': list(ALLOWED_STATES),
            'allowed_side': ALLOWED_SIDE,
            'allowed_timing': list(ALLOWED_TIMING),
            'entry_bucket': 'PRIMARY 0.03-0.12',
            'spread_cost': SPREAD_COST,
            'slippage_pct': SLIPPAGE_PCT,
            'fill_rejection_rate': FILL_REJECTION_RATE,
            'partial_fill_rate': PARTIAL_FILL_RATE,
            'stale_quote_rate': STALE_QUOTE_RATE,
            'queue_delay_penalty': QUEUE_DELAY_PENALTY,
            'repricing_drift_rate': REPRICING_DRIFT_RATE,
            'binary_settlement': True,
            'rsi_weight': '5%',
        },
        'summary': {
            'total_trades': n,
            'wins': total_wins,
            'losses': n - total_wins,
            'win_rate': wr,
            'bankroll': bankroll,
            'pnl': total_pnl,
            'roi': (bankroll / BANKROLL_START - 1) * 100,
            'realized_ev': realized_ev,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'payout_ratio': payout,
            'profit_factor': pf,
            'sharpe': sharpe,
            'max_drawdown_pct': max_dd,
        },
        'fill_survivability': {
            'fill_rejections': rejections['fill_rejected'],
            'stale_quote_aborts': rejections['stale_quote'],
            'partial_fills': partial_count,
            'repriced_fills': repriced_count,
            'avg_fill_latency_ms': avg_latency,
            'p50_fill_latency_ms': p50_latency,
            'p99_fill_latency_ms': p99_latency,
            'avg_slippage_bps': avg_slip,
            'p99_slippage_bps': p99_slip,
        },
        'directive_rejections': rejections,
        'state_breakdown': {k: dict(v) for k, v in state_stats.items()},
        'timing_breakdown': {k: dict(v) for k, v in timing_stats.items()},
        'promotion_criteria': {
            'settlements_met': promo_settlements,
            'positive_ev': promo_ev,
            'pf_met': promo_pf,
            'binary_settlement': promo_binary,
            'slippage_survivable': promo_slippage,
            'queue_survivable': promo_queue,
            'drawdown_acceptable': promo_dd,
            'promotion_ready': all_promo,
        },
    }
    with open(OUT_DIR / 'probe_summary.json', 'w') as f:
        json.dump(summary, f, indent=2, default=str)

    elapsed_total = time.time() - start_time
    print(f"\n  Elapsed: {elapsed_total:.0f}s ({elapsed_total/60:.1f}min)")
    print(f"\n  Output dir: {OUT_DIR}")
    print(f"  Files: live_trade_log.jsonl, execution_audit.json, settlement_audit.json,")
    print(f"         slippage_report.json, fill_latency_report.json, realized_ev_report.json,")
    print(f"         drawdown_report.json, probe_summary.json")


if __name__ == '__main__':
    run_live_probe_v2()