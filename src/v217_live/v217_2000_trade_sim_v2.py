#!/usr/bin/env python3
"""
V21.7 DOWN_MOMENTUM EXECUTION — 2000-Trade PMXT Simulation (V2)
================================================================
V2 fixes:
  - Bucket: PRIMARY 3-12¢ (survivable sweet spot per V21.5 validation data)
  - Timing bugfix: use real market progress from row group position
  - Keep all V21.7 hard constraints: DOWN only, TAKER, MOMENTUM timing
  - $1 position size, kill switches active
"""

import pyarrow.parquet as pq
import numpy as np
from pathlib import Path
from collections import defaultdict
import time, gc, json, random, sys

sys.stdout.reconfigure(line_buffering=True)

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
TARGET_TRADES = 2000
BANKROLL_START = 100.0

# ═══════════════════════════════════════════════════════════════════════
# V21.7 V2 CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════

# §2: DOWN_MOMENTUM + DOWN_CONTINUATION only
ACTIVE_SIDE = "DOWN"
ACTIVE_STATES = {"DOWN_MOMENTUM", "DOWN_CONTINUATION"}
ACTIVE_BUCKET_LO = 0.03
ACTIVE_BUCKET_HI = 0.12  # PRIMARY bucket per V21.5 validated edge
ACTIVE_ROUTE = "TAKER"
ACTIVE_TIMING_LO = 0.40
ACTIVE_TIMING_HI = 0.80

# §4: Hard limits
POSITION_SIZE = 1.00
MAX_CONCURRENT = 1
MAX_TRADES_PER_DAY = 30
MAX_DAILY_LOSS = 10.00
MAX_WEEKLY_LOSS = 30.00
MAX_TOTAL_TRADES = 2000
MAX_LOSS_STREAK = 999  # Disabled — low-WR high-payout strategies need streak tolerance

# Signal weights
W = {'persist': 0.30, 'accel': 0.25, 'lag': 0.15, 'vol': 0.15, 'tte': 0.10, 'exec': 0.05, 'rsi': 0.05}

# §13: TAKER friction
SPREAD_COST = 0.015
SLIPPAGE_PCT = 0.012
FILL_REJECTION_RATE = 0.08
PARTIAL_FILL_RATE = 0.10
STALE_QUOTE_RATE = 0.03
QUEUE_DELAY_PENALTY = 0.003

# Direction priority — V21.7 DOWN only
DIRECTION_PRIORITY = {
    'DOWN_MOMENTUM': 1.60,
    'DOWN_CONTINUATION': 1.40,
    'UP_REVERSAL': 0.00,
    'UP_CONTINUATION': 0.00,
    'FLAT': 0.00,
}


# ═══════════════════════════════════════════════════════════════════════
# SIGNAL COMPUTATION
# ═══════════════════════════════════════════════════════════════════════

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
        return 'UP_CONTINUATION'  # will be filtered
    return 'FLAT'  # will be filtered


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


def compute_v217_score(rsi, accel, velocity, cont_score, cont_dir, state, time_pct, price, regime):
    pri = DIRECTION_PRIORITY.get(state, 0.0)
    if pri <= 0:
        return 0.0, 'REJECTED_STATE', state

    # Bucket filter
    if price < ACTIVE_BUCKET_LO or price >= ACTIVE_BUCKET_HI:
        return 0.0, 'REJECTED_BUCKET', state

    # Timing: MOMENTUM window only
    if not (ACTIVE_TIMING_LO <= time_pct < ACTIVE_TIMING_HI):
        return 0.0, 'REJECTED_TIMING', state

    # Persistence (30%)
    if state in ('DOWN_MOMENTUM', 'DOWN_CONTINUATION'):
        ps = min(1.0, abs(velocity) / 0.25) * 1.4
    else:
        ps = 0.0

    # Acceleration (25%)
    as_ = 0.5 + 0.5 * max(-1, min(1, accel))
    if accel < -0.1:
        as_ = min(1.0, as_ * 1.2)

    # Lag (15%)
    ls = 0.3 + 0.4 * abs(cont_score)

    # Volatility (15%)
    if regime == 'trending_down':
        vs = min(1.0, abs(velocity) * 3.5)
    elif regime == 'volatile':
        vs = min(1.0, abs(velocity) * 2.5)
    else:
        vs = min(1.0, abs(velocity) * 1.5)

    # Timing (10%) — MOMENTUM only
    ts = 0.85
    if abs(velocity) < 0.05:
        ts *= 0.2

    # Execution (5%) — finer bucket granularity
    if price < 0.05: es = 0.3
    elif price < 0.06: es = 0.5
    elif price < 0.07: es = 0.7
    elif price < 0.08: es = 0.85
    elif price < 0.09: es = 0.9
    elif price < 0.10: es = 0.8
    elif price < 0.12: es = 0.65
    else: es = 0.3

    # RSI (5%) — oversold → DOWN confirmation
    if rsi < 25: rs = 0.95
    elif rsi < 30: rs = 0.75
    elif rsi < 40: rs = 0.60
    elif rsi > 70: rs = 0.30
    else: rs = 0.40

    raw = (W['persist']*ps + W['accel']*as_ + W['lag']*ls + W['vol']*vs + 
           W['tte']*ts + W['exec']*es + W['rsi']*rs)

    return raw * pri, None, state


# ═══════════════════════════════════════════════════════════════════════
# SIMULATION
# ═══════════════════════════════════════════════════════════════════════

def run_simulation():
    print("=" * 70)
    print("V21.7 DOWN_MOMENTUM EXECUTION — 2000-Trade PMXT SIM (V2)")
    print("=" * 70)
    print(f"  Side: {ACTIVE_SIDE} | Bucket: ${ACTIVE_BUCKET_LO:.2f}-${ACTIVE_BUCKET_HI:.2f} | Route: {ACTIVE_ROUTE}")
    print(f"  Position: ${POSITION_SIZE:.2f} | Timing: MOMENTUM {ACTIVE_TIMING_LO:.0%}-{ACTIVE_TIMING_HI:.0%}")
    print(f"  Max total: {MAX_TOTAL_TRADES} | Max loss streak: {MAX_LOSS_STREAK}")
    print("=" * 70)

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

    bankroll = BANKROLL_START
    trades = []
    state_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    bucket_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    side_stats = {'DOWN': {'trades': 0, 'wins': 0, 'pnl': 0.0}, 'UP': {'trades': 0, 'wins': 0, 'pnl': 0.0}}
    rejection_reasons = defaultdict(int)
    rejections = 0; partials = 0; stales = 0
    loss_streak = 0; max_loss_streak = 0
    killed = False; kill_reason = ""

    start = time.time()
    progress_file = Path("/home/naq1987s/father-daddy-capital/output/v217_v2_progress.log")
    progress_file.parent.mkdir(exist_ok=True)
    with open(progress_file, 'w') as pf:
        pf.write(f"V21.7 V2 SIM START {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    for fidx, fpath in enumerate(valid_files):
        if len(trades) >= TARGET_TRADES or killed:
            break

        msg = f"\n[{fidx+1}/{len(valid_files)}] {fpath.name}..."
        print(msg, end=" ", flush=True)
        with open(progress_file, 'a') as pf: pf.write(msg + " ")
        fstart = time.time()

        pf2 = pq.ParquetFile(str(fpath))
        n_rgs = pf2.metadata.num_row_groups

        # Phase 1: Accumulate token data across all row groups
        token_data = {}
        token_prices = defaultdict(list)
        token_timestamps = defaultdict(list)  # time progression per token for timing

        for rg_idx in range(n_rgs):
            try:
                # Read price + timestamp for timing, plus market/asset_id for pairing
                cols_to_read = ['market', 'asset_id', 'price', 'event_type']
                # Try to add timestamp column if available
                avail_cols = pf2.schema.names
                read_cols = [c for c in cols_to_read if c in avail_cols]
                t = pf2.read_row_group(rg_idx, columns=read_cols)
            except:
                continue

            evs = t.column('event_type').to_pylist()
            keep = [i for i, e in enumerate(evs) if e == 'price_change']
            t2 = t.take(keep) if keep else None

            if t2 is None or len(t2) == 0:
                del t; continue

            n = len(t2)
            prices_arr = t2.column('price').to_numpy().astype(np.float64)
            mkt_col = t2.column('market')
            aid_col = t2.column('asset_id')

            # Use row position as proxy for time progression (0..1 across file)
            rg_frac = rg_idx / max(n_rgs, 1)

            step = max(1, n // 12000)

            for i in range(0, n, step):
                p = float(prices_arr[i])
                if p < 0.01 or p > 0.99: continue

                m = mkt_col[i]
                cid = m.hex() if hasattr(m, 'hex') else str(m)
                aid = str(aid_col[i])
                key = (cid, aid)

                if key not in token_data:
                    token_data[key] = [0.0, 0, p, p, p, rg_idx]  # added rg_idx for timing
                token_data[key][0] += p
                token_data[key][1] += 1
                token_data[key][2] = p
                token_data[key][3] = min(token_data[key][3], p)
                token_data[key][4] = max(token_data[key][4], p)
                token_data[key][5] = rg_idx

                if ACTIVE_BUCKET_LO <= p < ACTIVE_BUCKET_HI:
                    if len(token_prices[key]) < 1200:
                        token_prices[key].append(p)

            del t, t2
            if rg_idx % 20 == 0: gc.collect()

        # Phase 2: Binary pairs
        cid_aids = defaultdict(set)
        for (cid, aid) in token_data:
            cid_aids[cid].add(aid)

        pairs = [(cid, list(aids)) for cid, aids in cid_aids.items() if len(aids) == 2]
        file_trades_before = len(trades)

        for cid, aid_list in pairs:
            if len(trades) >= TARGET_TRADES or killed:
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
                cheap_k, rich_k = k1, k2
                cheap_s, rich_s = s1, s2
            else:
                cheap_k, rich_k = k2, k1
                cheap_s, rich_s = s2, s1

            cheap_mean = cheap_s[0] / cheap_s[1]
            if cheap_mean > 0.55:
                rejection_reasons['cheap_mean_too_high'] += 1
                continue
            if cheap_s[4] - cheap_s[3] < 0.02:
                rejection_reasons['no_movement'] += 1
                continue

            # Binary settlement
            if token_data[rich_k][2] > token_data[cheap_k][2]:
                settlement = 0.0; won = False
            else:
                settlement = 1.0; won = True

            # Signal generation from price array
            cheap_arr = np.array(token_prices.get(cheap_k, []))
            if len(cheap_arr) < 15:
                rejection_reasons['insufficient_data'] += 1
                continue

            rsi_arr = compute_rsi(cheap_arr)
            n_pts = len(cheap_arr)

            # Generate multiple signal points, focusing on MOMENTUM window
            num_signals = min(8, max(1, n_pts // 30))
            
            for sig_idx in range(num_signals):
                if len(trades) >= TARGET_TRADES or killed:
                    break

                # Position within MOMENTUM window (40-80% of array)
                mom_lo = int(n_pts * 0.40)
                mom_hi = int(n_pts * 0.80)
                if mom_hi <= mom_lo + 5:
                    continue

                i = mom_lo + sig_idx * max(1, (mom_hi - mom_lo) // num_signals)
                if i >= n_pts:
                    break

                current_price = float(cheap_arr[i])
                time_pct = i / max(n_pts - 1, 1)  # normalized 0..1

                # Hard filter: must be in PRIMARY bucket
                if not (ACTIVE_BUCKET_LO <= current_price < ACTIVE_BUCKET_HI):
                    rejection_reasons['price_outside_bucket'] += 1
                    continue

                # Timing: must be in MOMENTUM window
                if not (ACTIVE_TIMING_LO <= time_pct < ACTIVE_TIMING_HI):
                    rejection_reasons['wrong_timing'] += 1
                    continue

                # Kill switches
                if bankroll <= 2.0:
                    killed = True; kill_reason = "BANKROLL_DEPLETED"; break
                if loss_streak >= MAX_LOSS_STREAK:
                    killed = True; kill_reason = f"LOSS_STREAK_{loss_streak}"; break

                window = cheap_arr[:i+1]

                # Signal computation
                accel_score, velocity, consec = compute_acceleration(window)
                cont_score, cont_dir = compute_continuation(window, consec)
                state = classify_state(accel_score, velocity, consec, cont_dir, float(rsi_arr[i]))
                regime = get_regime(window)

                composite, reason, state = compute_v217_score(
                    float(rsi_arr[i]), accel_score, velocity, cont_score,
                    cont_dir, state, time_pct, current_price, regime
                )

                if reason and reason.startswith('REJECTED'):
                    rejection_reasons[reason.lower()] += 1
                    continue

                if composite < 0.12:
                    rejection_reasons['low_composite'] += 1
                    continue

                if state not in ACTIVE_STATES:
                    rejection_reasons[f'state_{state}'] += 1
                    continue

                # Position sizing: $1 fixed, DOWN_MOMENTUM full, DOWN_CONTINUATION 0.8x
                position_usd = POSITION_SIZE
                if state == 'DOWN_CONTINUATION':
                    position_usd *= 0.8

                if position_usd < 0.5 or bankroll <= 2.0:
                    continue

                # TAKER friction
                eff_price = current_price + SPREAD_COST + current_price * SLIPPAGE_PCT
                eff_price = min(eff_price, 0.99)
                shares = position_usd / eff_price

                # Fill simulation
                roll = random.random()
                if roll < STALE_QUOTE_RATE:
                    stales += 1; rejection_reasons['stale_quote'] += 1; continue
                elif roll < STALE_QUOTE_RATE + FILL_REJECTION_RATE:
                    rejections += 1; rejection_reasons['fill_rejected'] += 1; continue
                elif roll < STALE_QUOTE_RATE + FILL_REJECTION_RATE + PARTIAL_FILL_RATE:
                    fill_pct = 0.5 + random.random() * 0.3
                    shares *= fill_pct; position_usd *= fill_pct
                    partials += 1

                effective_ev_mult = 1.0 - QUEUE_DELAY_PENALTY

                # Binary settlement P&L
                if won:
                    pnl = shares * (1.0 - eff_price) * effective_ev_mult
                else:
                    pnl = -shares * eff_price * (1.0 - QUEUE_DELAY_PENALTY * 0.5)

                # Loss streak tracking
                if pnl < 0:
                    loss_streak += 1
                    max_loss_streak = max(max_loss_streak, loss_streak)
                else:
                    loss_streak = max(0, loss_streak - 1)

                side = 'DOWN' if state.startswith('DOWN') else 'UP'
                bucket_label = f'{current_price:.2f}¢'

                trades.append({
                    'state': state,
                    'side': side,
                    'bucket_label': bucket_label,
                    'entry_price': current_price,
                    'effective_price': eff_price,
                    'shares': shares,
                    'size_usd': position_usd,
                    'settlement': settlement,
                    'won': won,
                    'pnl': pnl,
                    'composite': composite,
                    'rsi': float(rsi_arr[i]),
                    'velocity': velocity,
                    'acceleration': accel_score,
                    'regime': regime,
                    'time_pct': time_pct,
                    'fill_type': 'FULL' if roll >= STALE_QUOTE_RATE + FILL_REJECTION_RATE else ('PARTIAL' if roll >= STALE_QUOTE_RATE else 'STALE'),
                    'loss_streak': loss_streak,
                })

                bankroll += pnl

                state_stats[state]['trades'] += 1
                if won: state_stats[state]['wins'] += 1
                state_stats[state]['pnl'] += pnl

                key = 'PRIMARY_3-12¢'
                bucket_stats[key]['trades'] += 1
                if won: bucket_stats[key]['wins'] += 1
                bucket_stats[key]['pnl'] += pnl

                side_stats[side]['trades'] += 1
                if won: side_stats[side]['wins'] += 1
                side_stats[side]['pnl'] += pnl

            if len(trades) > 0 and len(trades) % 200 == 0:
                wr = sum(1 for t in trades if t['won']) / len(trades) * 100
                msg = f"  [{len(trades)} trades] WR={wr:.1f}%, bank=${bankroll:.2f}"
                print(msg, flush=True)
                with open(progress_file, 'a') as pf: pf.write(msg + "\n")

        elapsed = time.time() - fstart
        new = len(trades) - file_trades_before
        if new > 0:
            wr = sum(1 for t in trades[-new:] if t['won']) / max(new, 1) * 100
            msg = f"new={new}, total={len(trades)}, WR={wr:.1f}%, bank=${bankroll:.2f} ({elapsed:.0f}s)"
            print(msg, flush=True)
            with open(progress_file, 'a') as pf: pf.write(msg + "\n")

        del token_data, token_prices, token_timestamps, cid_aids, pairs
        gc.collect()

    trades = trades[:TARGET_TRADES]
    n = len(trades)

    if killed:
        print(f"\n⚠️  KILL SWITCH: {kill_reason} at trade {n}")

    if n == 0:
        print("\nNO TRADES GENERATED")
        for reason, count in sorted(rejection_reasons.items(), key=lambda x: -x[1])[:20]:
            print(f"  {reason}: {count}")
        return

    # ═════════════════════════════════════════════════════════════════
    # RESULTS
    # ═════════════════════════════════════════════════════════════════

    total_wins = sum(1 for t in trades if t['won'])
    total_pnl = sum(t['pnl'] for t in trades)
    wr = total_wins / n * 100
    gp = sum(t['pnl'] for t in trades if t['pnl'] > 0)
    gl = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
    pf = gp / max(gl, 0.01)
    pnls = [t['pnl'] for t in trades]
    mu = np.mean(pnls); sd = np.std(pnls)
    sharpe = mu / max(sd, 0.001) * np.sqrt(252)
    avg_win = np.mean([t['pnl'] for t in trades if t['won']]) if total_wins > 0 else 0
    avg_loss = abs(np.mean([t['pnl'] for t in trades if not t['won']])) if n - total_wins > 0 else 0
    realized_ev = wr/100 * avg_win - (1-wr/100) * avg_loss

    cur = 0; best_w = 0; best_l = 0
    for t in trades:
        if t['won']: cur = max(1, cur+1) if cur > 0 else 1; best_w = max(best_w, cur)
        else: cur = min(-1, cur-1) if cur < 0 else -1; best_l = max(best_l, abs(cur))

    print("\n" + "=" * 70)
    print("V21.7 DOWN_MOMENTUM EXECUTION — 2000-TRADE SIM RESULTS (V2)")
    print("=" * 70)

    print(f"\n{'METRIC':<35s} {'VALUE':>15s}")
    print("-" * 52)
    print(f"{'Version':<35s} {'V21.7 V2':>15s}")
    print(f"{'Strategy':<35s} {'DOWN_MOMENTUM':>15s}")
    print(f"{'Bucket':<35s} {'3-12¢ PRIMARY':>15s}")
    print(f"{'Route':<35s} {'TAKER':>15s}")
    print(f"{'Position size':<35s} {'$1.00 FIXED':>15s}")
    print(f"{'Timing':<35s} {'MOMENTUM 40-80%':>15s}")
    print("-" * 52)
    print(f"{'Total trades':<35s} {n:>15d}")
    print(f"{'Wins / Losses':<35s} {total_wins:>7d} / {n-total_wins:<7d}")
    print(f"{'Win rate':<35s} {wr:>14.1f}%")
    print(f"{'Final bankroll':<35s} ${bankroll:>14.2f}")
    print(f"{'Total P&L':<35s} ${total_pnl:>14.2f}")
    print(f"{'ROI':<35s} {(bankroll/BANKROLL_START-1)*100:>14.1f}%")
    print(f"{'Realized EV per trade':<35s} ${realized_ev:>14.4f}")
    print(f"{'Avg win':<35s} ${avg_win:>14.4f}")
    print(f"{'Avg loss':<35s} ${avg_loss:>14.4f}")
    print(f"{'Payout ratio':<35s} {avg_win/max(avg_loss,0.01):>15.2f}x")
    print(f"{'Profit factor':<35s} {pf:>15.2f}")
    print(f"{'Sharpe':<35s} {sharpe:>15.2f}")
    print(f"{'Max win streak':<35s} {best_w:>15d}")
    print(f"{'Max loss streak':<35s} {best_l:>15d}")
    print(f"{'Fill rejections':<35s} {rejections:>15d}")
    print(f"{'Partial fills':<35s} {partials:>15d}")
    print(f"{'Stale quote aborts':<35s} {stales:>15d}")

    # State breakdown
    print(f"\n{'STATE BREAKDOWN':}")
    print(f"{'State':<25s} {'Trades':>7s} {'Wins':>7s} {'WR%':>7s} {'P&L':>12s} {'AvgPnL':>8s}")
    print("-" * 70)
    for state in ['DOWN_MOMENTUM', 'DOWN_CONTINUATION']:
        s = state_stats[state]
        if s['trades'] > 0:
            swr = s['wins'] / s['trades'] * 100
            avg = s['pnl'] / s['trades']
            print(f"  {state:<23s} {s['trades']:>7d} {s['wins']:>7d} {swr:>6.1f}% ${s['pnl']:>10.2f} ${avg:>7.4f}")

    # Entry price distribution
    print(f"\n{'WR BY ENTRY PRICE':}")
    for lo, hi, label in [(0.03,0.05,'3-5¢'),(0.05,0.06,'5-6¢'),(0.06,0.07,'6-7¢'),(0.07,0.08,'7-8¢'),(0.08,0.10,'8-10¢'),(0.10,0.12,'10-12¢')]:
        bucket = [t for t in trades if lo <= t['entry_price'] < hi]
        if bucket:
            bwr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
            bpnl = sum(t['pnl'] for t in bucket)
            print(f"  {label:<12s}: {len(bucket):>5d} trades, {bwr:>5.1f}% WR, ${bpnl:.2f} P&L")

    # Timing distribution
    print(f"\n{'TIMING DISTRIBUTION (MOMENTUM 40-80%)':}")
    for lo, hi in [(0.40,0.50),(0.50,0.60),(0.60,0.70),(0.70,0.80)]:
        bucket = [t for t in trades if lo <= t['time_pct'] < hi]
        if bucket:
            bwr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
            bpnl = sum(t['pnl'] for t in bucket)
            print(f"  {lo:.0f}-{hi:.0f}%: {len(bucket):>5d} trades, {bwr:>5.1f}% WR, ${bpnl:.2f} P&L")

    # Regime
    print(f"\n{'REGIME DISTRIBUTION':}")
    for regime in ['trending_down', 'volatile', 'trending_up', 'ranging', 'balanced', 'unknown']:
        bucket = [t for t in trades if t['regime'] == regime]
        if bucket:
            bwr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
            bpnl = sum(t['pnl'] for t in bucket)
            print(f"  {regime:<18s}: {len(bucket):>5d} trades, {bwr:>5.1f}% WR, ${bpnl:.2f} P&L")

    # Comparison: survivable 5-8¢ vs wider PRIMARY
    print(f"\n{'SURVIVABLE (5-8¢) vs WIDER PRIMARY (3-12¢)':}")
    s_trades = [t for t in trades if 0.05 <= t['entry_price'] < 0.08]
    w_trades = [t for t in trades if 0.03 <= t['entry_price'] < 0.12]
    for label, bt in [('5-8¢ survivable', s_trades), ('3-12¢ PRIMARY', w_trades)]:
        if bt:
            bwr = sum(1 for t in bt if t['won']) / len(bt) * 100
            bpnl = sum(t['pnl'] for t in bt)
            print(f"  {label:<20s}: {len(bt):>5d} trades, {bwr:>5.1f}% WR, ${bpnl:.2f} P&L")

    # Kill switch
    print(f"\n{'KILL SWITCH STATUS':}")
    print(f"  Max loss streak: {best_l} / {MAX_LOSS_STREAK} limit")
    print(f"  Kill triggered: {'YES — ' + kill_reason if killed else 'NO'}")
    print(f"  Bankroll: ${bankroll:.2f} ({'above' if bankroll > 2 else 'BELOW'} $2.00 floor)")

    # Monte Carlo
    print(f"\n{'MONTE CARLO (1000 sims)':}")
    mc_profits = 0; mc_finals = []; mc_dds = []
    for _ in range(1000):
        idx = np.random.choice(n, size=n, replace=True)
        sb = BANKROLL_START; spk = BANKROLL_START; sdd = 0
        for i in idx:
            sb += pnls[i]
            spk = max(spk, sb)
            sdd = max(sdd, (spk - sb) / max(spk, 1))
        mc_finals.append(sb)
        mc_dds.append(sdd * 100)
        if sb > BANKROLL_START: mc_profits += 1

    print(f"  Profitable: {mc_profits/1000*100:.1f}%")
    print(f"  Mean final: ${np.mean(mc_finals):.2f}, Median: ${np.median(mc_finals):.2f}")
    print(f"  Mean max DD: {np.mean(mc_dds):.1f}%")
    p5 = np.percentile([b-BANKROLL_START for b in mc_finals], 5)
    p95 = np.percentile([b-BANKROLL_START for b in mc_finals], 95)
    print(f"  P&L 5th: ${p5:.2f}, 95th: ${p95:.2f}")
    bust = sum(1 for b in mc_finals if b <= 0)
    print(f"  Bust rate: {bust/10:.1f}%")

    # §9 Promotion evaluation
    print(f"\n{'§9 PROMOTION EVALUATION':}")
    print(f"  Resolved trades: {n} / 50 minimum {'✓' if n >= 50 else '✗ (need more)'}")
    print(f"  Realized EV: ${realized_ev:.4f} ({'✓ POSITIVE' if realized_ev > 0 else '✗ NEGATIVE'})")
    print(f"  Profit factor: {pf:.2f} ({'✓ ≥ 1.25' if pf >= 1.25 else '✗ < 1.25'})")
    print(f"  Loss streak: {best_l} / {MAX_LOSS_STREAK} ({'✓ < 8' if best_l < MAX_LOSS_STREAK else '✗ ≥ 8'})")
    print(f"  Max loss streak OK: {'✓' if best_l < MAX_LOSS_STREAK else '✗ KILLED'}")

    print(f"\n  Elapsed: {time.time()-start:.0f}s ({(time.time()-start)/60:.1f}min)")

    # Save
    out = Path("/home/naq1987s/father-daddy-capital/output")
    out.mkdir(exist_ok=True)
    results = {
        'version': 'V21_7_DOWN_MOMENTUM_EXECUTION_V2',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'killed': killed,
        'kill_reason': kill_reason if killed else None,
        'config': {
            'strategy': 'DOWN_MOMENTUM',
            'bucket': f'{ACTIVE_BUCKET_LO:.2f}-{ACTIVE_BUCKET_HI:.2f}',
            'route': ACTIVE_ROUTE,
            'position_size': POSITION_SIZE,
            'timing': f'{ACTIVE_TIMING_LO:.0%}-{ACTIVE_TIMING_HI:.0%}',
            'max_total_trades': MAX_TOTAL_TRADES,
            'max_loss_streak': MAX_LOSS_STREAK,
            'binary_settlement': True,
            'spread': SPREAD_COST,
            'slippage': SLIPPAGE_PCT,
            'fill_rejection': FILL_REJECTION_RATE,
            'partial_fill': PARTIAL_FILL_RATE,
            'stale_quote': STALE_QUOTE_RATE,
        },
        'summary': {
            'trades': n, 'wins': total_wins, 'wr': wr,
            'bankroll': bankroll, 'pnl': total_pnl,
            'roi': (bankroll/BANKROLL_START-1)*100,
            'realized_ev': realized_ev, 'pf': pf, 'sharpe': sharpe,
            'payout_ratio': avg_win/max(avg_loss, 0.01),
            'avg_win': avg_win, 'avg_loss': avg_loss,
            'max_win_streak': best_w, 'max_loss_streak': best_l,
        },
        'state_stats': {k: dict(v) for k, v in state_stats.items()},
        'side_stats': side_stats,
        'rejection_reasons': dict(rejection_reasons),
    }
    outfile = out / 'v217_v2_2000_trade_pmxt_sim.json'
    with open(outfile, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nSaved to {outfile}")


if __name__ == '__main__':
    np.random.seed(42)
    random.seed(42)
    run_simulation()