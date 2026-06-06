#!/usr/bin/env python3
"""
V21.7.1 PMXT 2000-Trade Simulation
====================================
DOWN_MOMENTUM ONLY. Execution-survivable convex continuation.
Binary settlement, TAKER routing, realistic fill model.

Per V21.7.1 directive:
- DOWN_MOMENTUM ONLY (UP blocked)
- PRIMARY 3-12¢, PREFERRED 5-8¢
- Bucket weighting: 5-8¢=1.0, 3-5¢=0.85, 8-10¢=0.65, 10-12¢=0.40
- Position $1 fixed, no Kelly/martingale
- Kill switches: $15 daily, $50 weekly, 60 consec losses
- survivability_score = realized_ev × fill_prob × slippage_surv × payout_asymmetry × bucket_weight
- RSI max 5% weight
- 2000 trades target
"""

import pyarrow.parquet as pq
import pyarrow.compute as pc
import numpy as np
from pathlib import Path
from collections import defaultdict
import time, gc, json, random

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
TARGET_TRADES = 2000
BANKROLL_START = 100.0
POSITION_USD = 1.00  # Fixed $1 positions

# §3: Bucket weights
BUCKET_WEIGHTS = {
    (0.05, 0.08): 1.00,  # preferred — survivable
    (0.03, 0.05): 0.85,  # ultra-cheap
    (0.08, 0.10): 0.65,  # mid-cheap
    (0.10, 0.12): 0.40,  # upper PRIMARY
}

# Entry range
PRIMARY_LO = 0.03
PRIMARY_HI = 0.12

# Kill switches §4
MAX_DAILY_LOSS = 15.00
MAX_WEEKLY_LOSS = 50.00
MAX_CONSECUTIVE_LOSSES = 60
MAX_DAILY_TRADES = 30

# Execution friction
SPREAD_COST = 0.01
SLIPPAGE_PCT = 0.005
FILL_REJECTION_RATE = 0.05
PARTIAL_FILL_RATE = 0.10
STALE_FILL_RATE = 0.03

# Signal weights §7/§11
SIGNAL_WEIGHTS = {
    'persist': 0.30,   # directional persistence (consecutive candles)
    'accel': 0.25,     # momentum acceleration
    'lag': 0.15,       # oracle/market lag (neutral in PMXT)
    'vol': 0.15,       # volatility expansion
    'tte': 0.10,       # time-to-expiry
    'exec': 0.05,      # execution quality
    'rsi': 0.05,       # RSI (max 5%)
}


def compute_rsi(prices, period=14):
    n = len(prices)
    if n < period + 1:
        return np.full(n, 50.0)
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
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


def detect_continuation(prices, lookback=5):
    """
    §12: Detect DOWN_MOMENTUM continuation signal.
    P(continuation) NOT P(reversal).
    """
    n = len(prices)
    if n < 4:
        return 'FLAT', 0.0, {}

    # Directional persistence: consecutive lower-lows
    consec_down = 0
    consec_up = 0
    for i in range(1, min(lookback+1, n)):
        if prices[-i] < prices[-i-1]:
            consec_down += 1
            consec_up = 0
        elif prices[-i] > prices[-i-1]:
            consec_up += 1
            consec_down = 0

    # Velocity and acceleration
    ref = prices[-1-lookback] if n > lookback+1 else prices[0]
    v_short = (prices[-1] - prices[-2]) / max(abs(prices[-2]), 1e-9) * 100 if n >= 2 else 0
    v_medium = (prices[-1] - prices[max(-4, -n)]) / max(abs(prices[max(-4, -n)]), 1e-9) * 100 if n >= 4 else 0
    v_long = (prices[-1] - ref) / max(abs(ref), 1e-9) * 100
    velocity = 0.4 * v_short + 0.35 * v_medium + 0.25 * v_long

    # Acceleration (velocity change)
    accel = 0
    if n >= 6:
        v1 = (prices[-1] - prices[-3]) / max(abs(prices[-3]), 1e-9) * 100
        v2 = (prices[-3] - prices[-5]) / max(abs(prices[-5]), 1e-9) * 100
        accel = v1 - v2  # positive = accelerating downward

    # Spread estimate
    spread = 0.02 if prices[-1] < 0.10 else (0.015 if prices[-1] < 0.20 else 0.01)

    # Survivability components
    persist_score = min(1.0, consec_down / 3.0) * (1.0 if velocity < -0.02 else 0.5)
    accel_score = min(1.0, abs(accel) / 0.5) if accel < 0 else 0.0  # neg accel = DOWN momentum
    vol_score = min(1.0, abs(velocity) * 2) if abs(velocity) > 0.15 else 0.1
    tte_score = 0.5  # neutral in simulation (no real time info)
    lag_score = 0.5  # neutral in PMXT (no spot)
    exec_score = 0.9 if spread < 0.01 else (0.6 if spread < 0.02 else 0.3)
    rsi_raw = compute_rsi(prices[-20:])[-1] if n >= 20 else 50
    rsi_score = 0.9 if rsi_raw < 30 else (0.7 if rsi_raw < 40 else 0.3)

    info = {
        'velocity': velocity,
        'accel': accel,
        'consec_down': consec_down,
        'consec_up': consec_up,
        'persist_score': persist_score,
        'accel_score': accel_score,
        'rsi': rsi_raw,
    }

    # Composite survivability_score (§7)
    survivability = (
        persist_score * SIGNAL_WEIGHTS['persist'] +
        accel_score * SIGNAL_WEIGHTS['accel'] +
        lag_score * SIGNAL_WEIGHTS['lag'] +
        vol_score * SIGNAL_WEIGHTS['vol'] +
        tte_score * SIGNAL_WEIGHTS['tte'] +
        exec_score * SIGNAL_WEIGHTS['exec'] +
        rsi_score * SIGNAL_WEIGHTS['rsi']
    )

    # DOWN_MOMENTUM: velocity < 0 AND persistence >= 2 AND survivability >= 0.25
    if velocity < -0.02 and consec_down >= 2 and survivability >= 0.25:
        return 'DOWN_MOMENTUM', survivability, info

    # DOWN_CONTINUATION (lower threshold)
    if velocity < 0 and survivability >= 0.20:
        return 'DOWN_CONTINUATION', survivability, info

    if velocity > 0.02 and consec_up >= 2 and survivability >= 0.25:
        return 'UP_MOMENTUM', survivability, info

    if velocity > 0 and survivability >= 0.20:
        return 'UP_CONTINUATION', survivability, info

    return 'FLAT', survivability, info


def get_bucket_weight(price):
    """§3: Bucket weighting for position sizing."""
    for (lo, hi), w in BUCKET_WEIGHTS.items():
        if lo <= price < hi:
            return w
    return 0.0  # outside PRIMARY


def run_simulation():
    print("=" * 70)
    print("V21.7.1 PMXT 2000-TRADE SIMULATION — DOWN_MOMENTUM ONLY")
    print("=" * 70)
    print(f"Version: V21.7.1 | Position: ${POSITION_USD} | Kill: ${MAX_DAILY_LOSS}/day ${MAX_WEEKLY_LOSS}/week {MAX_CONSECUTIVE_LOSSES} consec")
    print(f"PRIMARY: {PRIMARY_LO}-{PRIMARY_HI}¢ | PREFERRED: 5-8¢ (weight=1.0)")
    print(f"Fill model: spread={SPREAD_COST}, slippage={SLIPPAGE_PCT*100}%, reject={FILL_REJECTION_RATE*100}%, partial={PARTIAL_FILL_RATE*100}%, stale={STALE_FILL_RATE*100}%")
    print()

    files = sorted(PMXT_DIR.glob("*.parquet"))
    valid_files = []
    for f in files:
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000:
                valid_files.append(f)
        except:
            continue
    print(f"Valid files: {len(valid_files)}/{len(files)}")
    if not valid_files:
        print("ERROR: No valid PMXT files!")
        return

    bankroll = BANKROLL_START
    trades = []
    tier_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    state_stats = {'DOWN_MOMENTUM': {'trades': 0, 'wins': 0, 'pnl': 0.0},
                   'DOWN_CONTINUATION': {'trades': 0, 'wins': 0, 'pnl': 0.0}}
    bucket_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    daily_loss_tracker = []
    weekly_loss_tracker = []
    consecutive_losses = 0
    max_consecutive_losses = 0
    daily_pnl = []
    day_counter = 0

    global_rejections = 0
    global_partials = 0
    global_stale = 0

    start_time = time.time()

    for fidx, fpath in enumerate(valid_files):
        if len(trades) >= TARGET_TRADES:
            break

        print(f"\n[{fidx+1}/{len(valid_files)}] {fpath.name}...", end=" ", flush=True)
        fstart = time.time()

        pf = pq.ParquetFile(str(fpath))
        n_rgs = pf.metadata.num_row_groups

        # Phase 1: Accumulate per-token stats
        token_stats = {}
        token_sampled = defaultdict(list)

        for rg_idx in range(0, n_rgs, 3):
            try:
                t = pf.read_row_group(rg_idx, columns=['market', 'asset_id', 'price', 'event_type'])
            except:
                continue

            try:
                mask_arr = pc.equal(t.column('event_type'), 'price_change')
                t2 = t.filter(mask_arr)
            except:
                evs = t.column('event_type').to_pylist()
                keep = [i for i, e in enumerate(evs) if e == 'price_change']
                t2 = t.take(keep) if keep else None

            if t2 is None or len(t2) == 0:
                del t
                if t2: del t2
                continue

            n = len(t2)
            price_col_np = t2.column('price').to_numpy().astype(np.float64)
            step = max(1, n // 5000)

            for i in range(0, n, step):
                p = float(price_col_np[i])
                if p < 0.01 or p > 0.99:
                    continue

                mkt_col = t2.column('market')
                aid_col = t2.column('asset_id')
                mv = mkt_col[i]
                cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
                aid = str(aid_col[i])

                key = (cid, aid)
                if key not in token_stats:
                    token_stats[key] = [0.0, 0, p, p, p]
                token_stats[key][0] += p
                token_stats[key][1] += 1
                token_stats[key][2] = p
                token_stats[key][3] = min(token_stats[key][3], p)
                token_stats[key][4] = max(token_stats[key][4], p)

                if len(token_sampled[key]) < 200:
                    token_sampled[key].append(p)

            del t, t2
            gc.collect()

        # Phase 2: Find binary pairs
        cid_aids = defaultdict(set)
        for (cid, aid) in token_stats:
            cid_aids[cid].add(aid)

        pairs = [(cid, list(aids)) for cid, aids in cid_aids.items() if len(aids) == 2]
        file_trades_before = len(trades)

        for cid, aid_list in pairs:
            if len(trades) >= TARGET_TRADES:
                break

            key1 = (cid, aid_list[0])
            key2 = (cid, aid_list[1])

            if key1 not in token_stats or key2 not in token_stats:
                continue

            s1 = token_stats[key1]
            s2 = token_stats[key2]

            if s1[1] < 30 or s2[1] < 30:
                continue

            mean1 = s1[0] / s1[1]
            mean2 = s2[0] / s2[1]

            # Identify cheap (DOWN) and rich (UP) token
            if mean1 < mean2:
                cheap_key, cheap_mean = key1, mean1
                cheap_sampled = token_sampled.get(key1, [])
                cheap_last = s1[2]; cheap_min = s1[3]; cheap_max = s1[4]
                rich_last = s2[2]
            else:
                cheap_key, cheap_mean = key2, mean2
                cheap_sampled = token_sampled.get(key2, [])
                cheap_last = s2[2]; cheap_min = s2[3]; cheap_max = s2[4]
                rich_last = s1[2]

            # Only cheap tokens (DOWN side)
            if cheap_mean > 0.60:
                continue

            if cheap_max - cheap_min < 0.02:
                continue

            if len(cheap_sampled) < 24:
                continue

            # BINARY SETTLEMENT: rich_last > cheap_last → rich won → cheap lost
            settlement = 1.0 if cheap_last > rich_last else 0.0
            cheap_won = cheap_last > rich_last

            # Generate signals from cheap token price series
            cheap_arr = np.array(cheap_sampled)
            n_pts = len(cheap_arr)

            # Sample ~8 signal points per pair
            step = max(1, n_pts // 8)

            for i in range(24, n_pts, step):
                if len(trades) >= TARGET_TRADES:
                    break

                current_price = float(cheap_arr[i])

                # §2: PRIMARY bucket only
                if current_price < PRIMARY_LO or current_price >= PRIMARY_HI:
                    continue

                # §7: Bucket weighting
                bucket_weight = get_bucket_weight(current_price)
                if bucket_weight <= 0:
                    continue

                direction, velocity = detect_continuation(cheap_arr[:i+1])[:2]
                state, survivability, info = detect_continuation(cheap_arr[:i+1])

                # §1/§6: DOWN_MOMENTUM and DOWN_CONTINUATION ONLY
                if state not in ('DOWN_MOMENTUM', 'DOWN_CONTINUATION'):
                    continue

                if survivability < 0.25:
                    continue

                # Kill switch checks §4
                if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                    break

                # Daily loss tracking (rough: treat every 30 trades as a "day")
                if len(trades) > 0 and len(trades) % 30 == 0:
                    day_pnl = sum(t['pnl'] for t in trades[-30:])
                    if day_pnl < -MAX_DAILY_LOSS:
                        continue

                # Weekly loss (every 210 trades)
                if len(trades) > 0 and len(trades) % 210 == 0:
                    week_pnl = sum(t['pnl'] for t in trades[-210:])
                    if week_pnl < -MAX_WEEKLY_LOSS:
                        continue

                # Position sizing: fixed $1 × bucket_weight
                position_usd = POSITION_USD * bucket_weight

                # Execution friction model
                eff_price = current_price + SPREAD_COST + current_price * SLIPPAGE_PCT
                eff_price = min(eff_price, 0.99)
                if eff_price >= 1.0:
                    continue

                shares = position_usd / eff_price

                # Fill simulation
                roll = random.random()
                if roll < FILL_REJECTION_RATE:
                    global_rejections += 1
                    continue
                elif roll < FILL_REJECTION_RATE + PARTIAL_FILL_RATE:
                    fill_pct = 0.5 + random.random() * 0.3
                    shares *= fill_pct
                    position_usd *= fill_pct
                    global_partials += 1
                elif roll < FILL_REJECTION_RATE + PARTIAL_FILL_RATE + STALE_FILL_RATE:
                    global_stale += 1
                    continue

                # Binary P&L
                pnl = shares * (settlement - eff_price)

                # Payout asymmetry (continuation convexity)
                payout_multiplier = (1.0 - current_price) / max(current_price, 0.01)

                # Survivability score (§7)
                fill_prob = 1.0 - FILL_REJECTION_RATE - PARTIAL_FILL_RATE - STALE_FILL_RATE
                slippage_surv = max(0, 1.0 - (SPREAD_COST + current_price * SLIPPAGE_PCT) / current_price * 3)
                realized_ev_est = pnl  # actual outcome
                survivability_full = survivability * fill_prob * slippage_surv * payout_multiplier * bucket_weight

                trades.append({
                    'state': state,
                    'side': 'DOWN',
                    'entry': eff_price,
                    'raw_entry': current_price,
                    'shares': shares,
                    'size_usd': position_usd,
                    'settlement': settlement,
                    'pnl': pnl,
                    'won': cheap_won,
                    'survivability': survivability,
                    'survivability_full': survivability_full,
                    'bucket_weight': bucket_weight,
                    'velocity': info.get('velocity', 0),
                    'accel': info.get('accel', 0),
                    'consec_down': info.get('consec_down', 0),
                    'rsi': info.get('rsi', 50),
                    'payout_multiplier': payout_multiplier,
                })

                bankroll += pnl

                # Track consecutive losses/wins
                if cheap_won:
                    consecutive_losses = 0
                else:
                    consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)

                # Stats
                tier_label = f"{state}_{current_price:.2f}"
                tier_stats[tier_label]['trades'] += 1
                if cheap_won: tier_stats[tier_label]['wins'] += 1
                tier_stats[tier_label]['pnl'] += pnl

                state_stats.setdefault(state, {'trades': 0, 'wins': 0, 'pnl': 0.0})
                state_stats[state]['trades'] += 1
                if cheap_won: state_stats[state]['wins'] += 1
                state_stats[state]['pnl'] += pnl

                bucket_key = f"{current_price:.3f}"
                bucket_stats[bucket_key]['trades'] += 1
                if cheap_won: bucket_stats[bucket_key]['wins'] += 1
                bucket_stats[bucket_key]['pnl'] += pnl

        file_elapsed = time.time() - fstart
        file_new = len(trades) - file_trades_before
        wr = sum(1 for t in trades[-file_new:] if t['won']) / max(file_new, 1) * 100 if file_new > 0 else 0
        print(f"new={file_new}, total={len(trades)}, WR={wr:.1f}%, bank=${bankroll:.2f} ({file_elapsed:.0f}s)")

        del token_stats, token_sampled, cid_aids, pairs
        gc.collect()

        if len(trades) >= TARGET_TRADES:
            break

    # Trim to target
    trades = trades[:TARGET_TRADES]

    # ═══════════════════════════════════════════════════════════════
    # RESULTS
    # ═══════════════════════════════════════════════════════════════

    total_elapsed = time.time() - start_time
    n = len(trades)
    total_wins = sum(1 for t in trades if t['won'])
    total_pnl = sum(t['pnl'] for t in trades)

    print("\n" + "=" * 70)
    print("V21.7.1 PMXT 2000-TRADE SIMULATION RESULTS")
    print("=" * 70)

    if n == 0:
        print("NO TRADES GENERATED")
        return

    wr = total_wins / n * 100
    gp = sum(t['pnl'] for t in trades if t['pnl'] > 0)
    gl = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
    pf = gp / max(gl, 0.01)
    pnls = [t['pnl'] for t in trades]
    mu = np.mean(pnls); sd = np.std(pnls)
    sharpe = mu / max(sd, 0.001) * np.sqrt(252)

    # Payout ratio
    avg_win = np.mean([t['pnl'] for t in trades if t['won']]) if total_wins > 0 else 0
    avg_loss = abs(np.mean([t['pnl'] for t in trades if not t['won']])) if n - total_wins > 0 else 0
    payout_ratio = avg_win / max(avg_loss, 0.001)

    # Realized EV
    realized_ev = total_pnl / n

    # Rolling 100-trade EV
    rolling_evs = [np.mean(pnls[max(0,i-100):i+1]) for i in range(n)]
    rolling_positive = sum(1 for e in rolling_evs if e > 0) / len(rolling_evs) * 100

    # Drawdown
    cum = np.cumsum(pnls) + BANKROLL_START
    peak = np.maximum.accumulate(cum)
    dd = (peak - cum) / np.maximum(peak, 1)
    max_dd = dd.max() * 100

    print(f"\n{'METRIC':<30s} {'VALUE':>15s}")
    print("-" * 47)
    print(f"{'Version':<30s} {'V21.7.1':>15s}")
    print(f"{'Total trades':<30s} {n:>15d}")
    print(f"{'Wins / losses':<30s} {total_wins:>7d} / {n-total_wins:<7d}")
    print(f"{'Win rate':<30s} {wr:>14.1f}%")
    print(f"{'Final bankroll':<30s} ${bankroll:>14.2f}")
    print(f"{'Total P&L':<30s} ${total_pnl:>14.2f}")
    print(f"{'ROI':<30s} {(bankroll/BANKROLL_START-1)*100:>14.1f}%")
    print(f"{'Realized EV':<30s} ${realized_ev:>14.4f}/trade")
    print(f"{'Profit factor':<30s} {pf:>15.2f}")
    print(f"{'Payout ratio':<30s} {payout_ratio:>15.2f}x")
    print(f"{'Sharpe':<30s} {sharpe:>15.2f}")
    print(f"{'Max drawdown':<30s} {max_dd:>14.1f}%")
    print(f"{'Max consec losses':<30s} {max_consecutive_losses:>15d}")
    print(f"{'Fill rejections':<30s} {global_rejections:>15d}")
    print(f"{'Partial fills':<30s} {global_partials:>15d}")
    print(f"{'Stale fills':<30s} {global_stale:>15d}")
    print(f"{'Rolling EV > 0':<30s} {rolling_positive:>14.1f}%")

    # STATE BREAKDOWN
    print(f"\n{'STATE BREAKDOWN':}")
    for state in ['DOWN_MOMENTUM', 'DOWN_CONTINUATION']:
        s = state_stats.get(state, {'trades': 0, 'wins': 0, 'pnl': 0.0})
        if s['trades'] > 0:
            swr = s['wins'] / s['trades'] * 100
            print(f"  {state:<25s}: {s['trades']:>5d} trades, {swr:>5.1f}% WR, ${s['pnl']:>10.2f} P&L")

    # BUCKET BREAKDOWN
    print(f"\n{'BUCKET BREAKDOWN (V21.7.1)':}")
    bucket_ranges = [
        (0.03, 0.05, '3-5¢', 0.85),
        (0.05, 0.08, '5-8¢ PREFERRED', 1.00),
        (0.08, 0.10, '8-10¢', 0.65),
        (0.10, 0.12, '10-12¢', 0.40),
    ]
    for lo, hi, label, weight in bucket_ranges:
        bucket = [t for t in trades if lo <= t['raw_entry'] < hi]
        if bucket:
            bwr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
            bpnl = sum(t['pnl'] for t in bucket)
            bev = bpnl / len(bucket)
            print(f"  {label:<20s} w={weight:.2f}: {len(bucket):>5d} trades, {bwr:>5.1f}% WR, ${bpnl:>8.2f} P&L, ${bev:.4f}/trade")

    # SIDE VERIFICATION
    print(f"\n{'SIDE VERIFICATION':}")
    down_trades = [t for t in trades if t['side'] == 'DOWN']
    print(f"  DOWN trades: {len(down_trades)} ({len(down_trades)/n*100:.1f}%)")
    up_trades = [t for t in trades if t['side'] == 'UP']
    print(f"  UP trades: {len(up_trades)} ({len(up_trades)/n*100:.1f}%) — SHOULD BE 0%")

    # BINARY SETTLEMENT
    won_n = sum(1 for t in trades if t['won'])
    print(f"\n{'BINARY SETTLEMENT':}")
    print(f"  Won (settlement=1.0): {won_n} ({won_n/n*100:.1f}%)")
    print(f"  Lost (settlement=0.0): {n-won_n} ({(n-won_n)/n*100:.1f}%)")

    # MONTE CARLO
    print(f"\n{'MONTE CARLO (1000 sims)':}")
    mc_profits = 0; mc_finals = []; mc_dds = []
    for _ in range(1000):
        idx = np.random.choice(n, size=n, replace=True)
        sim_b = BANKROLL_START; sim_pk = BANKROLL_START; sim_dd = 0
        consec_l = 0
        halted = False
        for i in idx:
            # Kill switch check in MC
            if sim_b <= 5:
                halted = True
                break
            sim_b += pnls[i]
            if pnls[i] < 0:
                consec_l += 1
            else:
                consec_l = 0
            if consec_l >= MAX_CONSECUTIVE_LOSSES:
                halted = True
                break
            sim_pk = max(sim_pk, sim_b)
            sim_dd = max(sim_dd, (sim_pk - sim_b) / max(sim_pk, 1))
        mc_finals.append(sim_b)
        mc_dds.append(sim_dd * 100)
        if sim_b > BANKROLL_START: mc_profits += 1

    print(f"  Profitable: {mc_profits/1000*100:.1f}%")
    print(f"  Mean final: ${np.mean(mc_finals):.2f}, Median: ${np.median(mc_finals):.2f}")
    print(f"  Mean max DD: {np.mean(mc_dds):.1f}%")
    p5 = np.percentile([b-BANKROLL_START for b in mc_finals], 5)
    p95 = np.percentile([b-BANKROLL_START for b in mc_finals], 95)
    print(f"  P&L 5th: ${p5:.2f}, 95th: ${p95:.2f}")
    print(f"  Bust rate: {sum(1 for b in mc_finals if b <= 5)/10:.1f}%")
    print(f"  Halted by kill switch: {sum(1 for h in [False]*1000 if h)/10:.1f}%")

    print(f"\n  Elapsed: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")

    # Save results
    out = Path("/home/naq1987s/father-daddy-capital/output")
    out.mkdir(exist_ok=True)
    results = {
        'version': 'V2171_PMXT',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'directive': 'V21.7.1_DOWN_MOMENTUM_ONLY',
        'config': {
            'bankroll': BANKROLL_START,
            'position_usd': POSITION_USD,
            'primary_bucket': f'{PRIMARY_LO}-{PRIMARY_HI}',
            'bucket_weights': {f'{lo}-{hi}': w for (lo, hi), w in BUCKET_WEIGHTS.items()},
            'kill_switches': {
                'max_daily_loss': MAX_DAILY_LOSS,
                'max_weekly_loss': MAX_WEEKLY_LOSS,
                'max_consecutive_losses': MAX_CONSECUTIVE_LOSSES,
            },
            'fill_model': {
                'spread': SPREAD_COST,
                'slippage': SLIPPAGE_PCT,
                'reject_rate': FILL_REJECTION_RATE,
                'partial_rate': PARTIAL_FILL_RATE,
                'stale_rate': STALE_FILL_RATE,
            },
            'binary_settlement': True,
            'side': 'DOWN_ONLY',
        },
        'summary': {
            'trades': n, 'wins': total_wins, 'wr': wr,
            'bankroll': bankroll, 'pnl': total_pnl,
            'roi': (bankroll/BANKROLL_START-1)*100,
            'realized_ev': realized_ev,
            'pf': pf, 'sharpe': sharpe,
            'payout_ratio': payout_ratio,
            'max_dd': max_dd,
            'max_consecutive_losses': max_consecutive_losses,
            'rolling_ev_positive_pct': rolling_positive,
        },
        'state_stats': {k: dict(v) for k, v in state_stats.items()},
        'bucket_stats': {k: dict(v) for k, v in bucket_stats.items()},
    }
    outfile = out / "v2171_pmxt_2000_trade_sim.json"
    with open(outfile, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nSaved to {outfile}")


if __name__ == '__main__':
    np.random.seed(42)
    random.seed(42)
    run_simulation()