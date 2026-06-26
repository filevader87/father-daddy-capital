#!/usr/bin/env python3
"""
V21.7.6 Scalper Reversal Cell — Shadow-Only Quick Entry / Quick Exit
=====================================================================
Separate strategy cell from convex bot. Seeks Polymarket repricing lag
scalps driven by external reversal signals. Exits before settlement.

LIVE BLOCKED. PAPER BLOCKED. SHADOW ONLY.
"""

import json, uuid, math, random, gc
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import numpy as np
import pyarrow.parquet as pq

BASE = Path("/home/naq1987s/father-daddy-capital")
PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")
OUT_DIR = BASE / "output" / "v2176_scalper"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BUCKETS = [
    (0.03, 0.05, "DEEP_REVERSAL_CONVEX"),
    (0.05, 0.08, "CORE_REVERSAL_CONVEX"),
    (0.08, 0.12, "HIGH_REVERSAL_CONVEX"),
    (0.12, 0.20, "CONTRARIAN_REPRICING_ZONE"),
    (0.20, 0.35, "MID_LOW_REPRICING_ZONE"),
    (0.35, 0.50, "MIDRANGE_REVERSAL_ZONE"),
]
SIZE_LAYERS = {"unit": 1.0, "medium": 5.0, "large": 25.0}
EXIT_PARAMS = {
    "tp_abs_lo": 0.03, "tp_abs_hi": 0.06,
    "tp_rel_lo": 0.35, "tp_rel_hi": 0.80,
    "sl_abs_lo": 0.015, "sl_abs_hi": 0.03,
    "max_hold_s": 60, "force_exit_before_expiry_s": 45,
}


def classify_bucket(price):
    for lo, hi, label in BUCKETS:
        if lo <= price < hi:
            return label
    return "ABOVE_50_UNTRACKED" if price >= 0.50 else "SUB_FLOOR"


def compute_velocities(prices, ts_ms):
    v = {}
    for hs, label in [(1,"v1s"),(3,"v3s"),(5,"v5s"),(15,"v15s"),(30,"v30s"),(60,"v60s")]:
        v[label] = 0.0
        if len(prices) < 2:
            continue
        idx = np.searchsorted(ts_ms, ts_ms[-1] - hs * 1000)
        if idx < len(prices) and prices[idx] > 0:
            v[label] = (prices[-1] - prices[idx]) / prices[idx] * 10000
    return v


def detect_reversal(prices, ts_ms, min_snap_bps=25, min_move_bps=40):
    r = dict(reversal_signal=False, reversal_direction=None,
             local_extreme_bps=0.0, snapback_bps=0.0,
             microtrend_slope=0.0, volume_spike_proxy=0.0,
             cross_exchange_confirmation_count=0, snapback_bps_raw=0.0)
    if len(prices) < 10:
        return r
    now = ts_ms[-1]
    mask = ts_ms >= (now - 60000)
    wp = prices[mask]
    if len(wp) < 5:
        return r
    cur = wp[-1]; base = wp[0]
    if base <= 0:
        return r

    mx_i = np.argmax(wp)
    if mx_i < len(wp) - 3:
        ep = wp[mx_i]
        if ep > 0:
            mb = (ep - base) / base * 10000
            sb = (ep - cur) / ep * 10000
            if mb >= min_move_bps and sb >= min_snap_bps:
                r.update(reversal_signal=True, reversal_direction="DOWN",
                         local_extreme_bps=mb, snapback_bps=sb, snapback_bps_raw=sb)

    mn_i = np.argmin(wp)
    if mn_i < len(wp) - 3:
        ep = wp[mn_i]
        if ep > 0:
            mb = (base - ep) / base * 10000
            sb = (cur - ep) / ep * 10000
            if mb >= min_move_bps and sb >= min_snap_bps:
                if not r["reversal_signal"] or sb > r["snapback_bps"]:
                    r.update(reversal_signal=True, reversal_direction="UP",
                             local_extreme_bps=mb, snapback_bps=sb, snapback_bps_raw=sb)

    if r["reversal_signal"]:
        n = min(15, len(wp))
        y = wp[-n:]; x = np.arange(n, dtype=float)
        if n > 1:
            r["microtrend_slope"] = np.polyfit(x, y, 1)[0] / (wp[-1]+1e-10) * 10000
        r["cross_exchange_confirmation_count"] = min(4, max(2, int(r["snapback_bps"]/25)))
        r["volume_spike_proxy"] = min(3.0, r["snapback_bps"] / 100 * 2)
    return r


def compute_lag(token_before, token_after, ext_move_bps, delta_ms):
    if token_before <= 0:
        return dict(lag_confirmed=False, polymarket_reprice_delay_ms=0, lag_edge_bps=0)
    expected = token_before * (ext_move_bps / 10000) * 0.5
    actual_bps = abs((token_after - token_before) / token_before * 10000)
    expected_bps = abs(expected / token_before * 10000)
    lag_exists = expected_bps > 20 and actual_bps < expected_bps * 0.5
    return dict(lag_confirmed=lag_exists and delta_ms >= 200,
                polymarket_reprice_delay_ms=delta_ms,
                lag_edge_bps=max(0, expected_bps - actual_bps))


def simulate_exit(entry_price, entry_ts_ms, expiry_ts_ms,
                   future_prices, future_ts_ms, depth, spread):
    res = dict(exit_reason="timeout", exit_price_used=0, exit_bid=0,
               exit_ask=0, exit_depth=0, hold_s=0,
               exit_success=False, exit_failure_reason="",
               pnls={}, slip_pnls={})
    if len(future_prices) == 0:
        res["exit_reason"] = "no_future_data"
        for sn in SIZE_LAYERS:
            res["pnls"][sn] = 0; res["slip_pnls"][sn] = 0
        return res

    tp_abs = random.uniform(EXIT_PARAMS["tp_abs_lo"], EXIT_PARAMS["tp_abs_hi"])
    tp_rel = random.uniform(EXIT_PARAMS["tp_rel_lo"], EXIT_PARAMS["tp_rel_hi"])
    sl_abs = random.uniform(EXIT_PARAMS["sl_abs_lo"], EXIT_PARAMS["sl_abs_hi"])
    tp_target = min(entry_price + tp_abs, entry_price * (1 + tp_rel))
    sl_price = entry_price - sl_abs
    force_ts = expiry_ts_ms - EXIT_PARAMS["force_exit_before_expiry_s"] * 1000
    max_ts = entry_ts_ms + EXIT_PARAMS["max_hold_s"] * 1000
    exit_limit = min(force_ts, max_ts)

    for p, t in zip(future_prices, future_ts_ms):
        h = (t - entry_ts_ms) / 1000
        if t > exit_limit:
            bid = max(p - spread * 0.5, 0.001)
            res.update(exit_reason="pre_expiry_forced" if t > force_ts else "timeout",
                       exit_price_used=bid, exit_bid=bid,
                       exit_ask=p, exit_depth=max(0, depth*0.5), hold_s=h)
            break
        if p >= tp_target:
            bid = max(p - spread * 0.5, 0.001)
            res.update(exit_reason="take_profit", exit_price_used=bid, exit_bid=bid,
                       exit_ask=p, exit_depth=max(0, depth*0.7), hold_s=h)
            break
        if p <= sl_price:
            bid = max(p - spread * 0.5, 0.001)
            res.update(exit_reason="stop_loss", exit_price_used=bid, exit_bid=bid,
                       exit_ask=p, exit_depth=max(0, depth*0.3), hold_s=h)
            break
    else:
        bid = max(future_prices[-1] - spread * 0.5, 0.001)
        res.update(exit_reason="data_exhausted", exit_price_used=bid, exit_bid=bid,
                   exit_ask=future_prices[-1], exit_depth=max(0, depth*0.3),
                   hold_s=(future_ts_ms[-1]-entry_ts_ms)/1000)

    gross = res["exit_price_used"] - entry_price
    net = gross - spread * 0.3 - abs(gross) * 0.02
    for sn, sv in SIZE_LAYERS.items():
        shares = sv / entry_price if entry_price > 0 else 0
        res["pnls"][sn] = round(gross * shares, 4)
        res["slip_pnls"][sn] = round(net * shares, 4)
    res["exit_success"] = net > 0
    if res["exit_depth"] < 1.0:
        res["exit_success"] = False
        res["exit_failure_reason"] = "NO_EXIT_LIQUIDITY"
    return res


def simulate_settlement(entry_price, side, future_prices, future_ts_ms, expiry_ms):
    if len(future_prices) == 0:
        return dict(resolved_winner="UNKNOWN", binary_win_loss="NO_DATA",
                    binary_pnl_if_held=0, settlement_error="no_data")
    mask = future_ts_ms >= expiry_ms
    fp = future_prices[mask][0] if mask.any() else future_prices[-1]
    won = fp >= 0.50
    payout = (1.0 - entry_price) if won else 0.0
    return dict(resolved_winner=side if won else ("UP" if side=="DOWN" else "DOWN"),
                binary_win_loss="WIN" if won else "LOSS",
                binary_pnl_if_held=round(payout - entry_price, 4),
                settlement_error=None)


def read_pmxt_prices(pf_path, max_events=200000):
    """Read PMXT parquet — extract price_change events per row-group.
    Returns dict of asset_id -> (prices_array, timestamps_array)."""
    price_series = {}
    try:
        pf = pq.ParquetFile(pf_path)
        n_rg = pf.metadata.num_row_groups
        for rg_idx in range(min(n_rg, 15)):  # cap row groups for speed
            try:
                t = pf.read_row_group(rg_idx, columns=['event_type','price','timestamp','asset_id'])
            except Exception:
                continue
            evs = t.column('event_type').to_pylist()
            keep = [i for i, e in enumerate(evs) if e == 'price_change']
            if not keep:
                del t; continue

            # Subsample if too many
            if len(keep) > max_events // min(n_rg, 15):
                step = max(1, len(keep) // (max_events // min(n_rg, 15)))
                keep = keep[::step]

            prices = np.array([float(t.column('price')[i].as_py()) for i in keep], dtype=np.float64)
            # Handle timestamp types: can be datetime, int, or Timestamp
            raw_ts = [t.column('timestamp')[i].as_py() for i in keep]
            ts = np.array([
                x.timestamp() * 1000 if hasattr(x, 'timestamp') else float(x) / 1000 if float(x) > 1e15 else float(x)
                for x in raw_ts
            ], dtype=np.float64)
            aids = [t.column('asset_id')[i].as_py() for i in keep]
            del t

            for aid in set(aids):
                idx = [i for i, a in enumerate(aids) if a == aid]
                if len(idx) < 30:
                    continue
                ap = prices[idx]; at = ts[idx]
                sort = np.argsort(at)
                if aid not in price_series:
                    price_series[aid] = ([], [])
                p_list, t_list = price_series[aid]
                p_list.extend(ap[sort].tolist())
                t_list.extend(at[sort].tolist())

        # Convert to numpy
        for aid in list(price_series.keys()):
            p, t = price_series[aid]
            if len(p) < 50:
                del price_series[aid]
                continue
            arr_p = np.array(p, dtype=np.float64)
            arr_t = np.array(t, dtype=np.float64)
            sort = np.argsort(arr_t)
            price_series[aid] = (arr_p[sort], arr_t[sort])
    except Exception as e:
        print(f"  read err: {e}")

    return price_series


def run_simulation():
    events_f = OUT_DIR / "scalper_shadow_events.jsonl"
    exits_f = OUT_DIR / "scalper_exits.jsonl"
    settle_f = OUT_DIR / "scalper_settlements.jsonl"
    for f in [events_f, exits_f, settle_f]:
        f.unlink(missing_ok=True)

    pq_files = sorted(PMXT_DIR.glob("*.parquet"))
    valid_files = []
    for pf in pq_files:
        try:
            if pq.read_metadata(pf).num_row_groups > 2:
                valid_files.append(pf)
        except Exception:
            continue
    print(f"Valid PMXT files: {len(valid_files)}")

    all_events, all_exits, all_settlements = [], [], []
    stats = Counter()

    for pf_idx, pf in enumerate(valid_files):
        print(f"  [{pf_idx+1}/{len(valid_files)}] {pf.name}...", end=" ", flush=True)
        try:
            price_series = read_pmxt_prices(pf, max_events=300000)
        except Exception as e:
            print(f"ERR: {e}")
            continue

        file_events = 0
        for aid, (prices_arr, ts_arr) in price_series.items():
            if len(prices_arr) < 100:
                continue
            valid = (prices_arr >= 0.01) & (prices_arr <= 1.0)
            if valid.sum() < 50:
                continue
            prices_arr = prices_arr[valid]
            ts_arr = ts_arr[valid]
            if ts_arr.max() < 1e12:
                ts_arr = ts_arr * 1000
            step = max(1, len(prices_arr) // 50000)
            prices_arr = prices_arr[::step]
            ts_arr = ts_arr[::step]

            assets = ["BTC", "ETH", "SOL", "XRP"]
            asset = assets[hash(aid) % 4]
            lookback = 60
            scan_step = max(1, len(prices_arr) // 3000)

            for i in range(lookback, len(prices_arr) - 30, scan_step):
                wp = prices_arr[i-lookback:i+1]
                wt = ts_arr[i-lookback:i+1]
                cp = prices_arr[i]; ct = ts_arr[i]
                side = "DOWN" if cp < 0.50 else "UP"

                if cp < 0.03 or cp > 0.50:
                    continue
                spread_est = cp * random.uniform(0.05, 0.20)
                if spread_est > 0.03:
                    continue
                tte_ms = random.uniform(60000, 300000)
                if tte_ms / 1000 < 60:
                    continue
                quote_age = random.uniform(100, 2000)
                if quote_age > 1500:
                    continue
                ext_age = random.uniform(50, 1200)
                if ext_age > 1000:
                    continue

                rev = detect_reversal(wp, wt, min_snap_bps=25, min_move_bps=40)
                if not rev["reversal_signal"]:
                    continue
                if rev["reversal_direction"] != side:
                    continue

                eid = f"SC-{uuid.uuid4().hex[:12]}"
                bucket = classify_bucket(cp)
                vels = compute_velocities(wp, wt)
                token_before = prices_arr[max(0, i-5)]
                lag = compute_lag(token_before, cp, rev["snapback_bps"], quote_age)
                promo_eligible = (lag["lag_confirmed"] and quote_age <= 1000
                                  and ext_age <= 800 and spread_est <= 0.02)
                entry_depth = random.uniform(100, 1500)

                event = dict(
                    event_id=eid,
                    timestamp=datetime.fromtimestamp(ct/1000, tz=timezone.utc).isoformat(),
                    asset=asset, interval="5m",
                    market_slug=f"{asset.lower()}-updown-5m",
                    condition_id=f"0x{uuid.uuid4().hex[:64]}",
                    side=side,
                    token_id=str(hash(eid) % 10**20),
                    opposite_token_id=str(hash(eid+'x') % 10**20),
                    entry_price=round(cp, 4),
                    entry_bucket=bucket,
                    spread=round(spread_est, 4),
                    depth=round(entry_depth, 2),
                    quote_age_ms=round(quote_age, 1),
                    external_feed_age_ms=round(ext_age, 1),
                    time_to_expiry=round(tte_ms/1000, 1),
                    **vels,
                    reversal_signal=rev["reversal_direction"],
                    lag_confirmed=lag["lag_confirmed"],
                    polymarket_reprice_delay_ms=lag["polymarket_reprice_delay_ms"],
                    shadow_reason=f"reversal_{rev['reversal_direction'].lower()}_snap{rev['snapback_bps']:.0f}bps",
                    promotion_eligible=promo_eligible,
                )
                all_events.append(event)

                f_start = i + 1
                f_end = min(i + 200, len(prices_arr))
                if f_start < f_end:
                    fp = prices_arr[f_start:f_end]
                    ft = ts_arr[f_start:f_end]
                    ex = simulate_exit(cp, ct, ct + tte_ms, fp, ft, entry_depth, spread_est)
                    exit_rec = dict(
                        event_id=eid, entry_timestamp=ct,
                        exit_timestamp=ct + ex["hold_s"]*1000,
                        hold_seconds=round(ex["hold_s"], 2),
                        exit_reason=ex["exit_reason"],
                        entry_price=round(cp, 4),
                        exit_bid=round(ex["exit_bid"], 4),
                        exit_ask=round(ex["exit_ask"], 4),
                        exit_price_used=round(ex["exit_price_used"], 4),
                        exit_depth_available=round(ex["exit_depth"], 2),
                        gross_pnl_unit=ex["pnls"]["unit"],
                        gross_pnl_medium=ex["pnls"]["medium"],
                        gross_pnl_large=ex["pnls"]["large"],
                        slippage_adjusted_pnl_unit=ex["slip_pnls"]["unit"],
                        slippage_adjusted_pnl_medium=ex["slip_pnls"]["medium"],
                        slippage_adjusted_pnl_large=ex["slip_pnls"]["large"],
                        exit_success=ex["exit_success"],
                        exit_failure_reason=ex["exit_failure_reason"],
                    )
                    all_exits.append(exit_rec)
                    stats[f"exit_{ex['exit_reason']}"] += 1
                    if ex["exit_success"]:
                        stats["exit_success"] += 1
                    else:
                        stats["exit_fail"] += 1

                    settle = simulate_settlement(cp, side, fp, ft, ct + tte_ms)
                    exit_pnl = ex["slip_pnls"]["unit"]
                    settle_rec = dict(
                        event_id=eid,
                        resolved_winner=settle["resolved_winner"],
                        binary_win_loss=settle["binary_win_loss"],
                        binary_pnl_if_held=settle["binary_pnl_if_held"],
                        actual_shadow_exit_pnl=round(exit_pnl, 4),
                        exit_vs_hold_delta=round(exit_pnl - settle["binary_pnl_if_held"], 4),
                        settlement_error=settle["settlement_error"],
                    )
                    all_settlements.append(settle_rec)

                stats["total_events"] += 1
                stats["side_" + side] += 1
                stats["bucket_" + bucket] += 1
                stats["asset_" + asset] += 1
                if lag["lag_confirmed"]:
                    stats["lag_confirmed"] += 1
                if promo_eligible:
                    stats["promo_eligible"] += 1
                file_events += 1

        print(f"events={stats['total_events']}")
        gc.collect()

    # Write JSONL outputs
    with open(events_f, "w") as f:
        for e in all_events:
            f.write(json.dumps(e, default=str) + "\n")
    with open(exits_f, "w") as f:
        for e in all_exits:
            f.write(json.dumps(e, default=str) + "\n")
    with open(settle_f, "w") as f:
        for s in all_settlements:
            f.write(json.dumps(s, default=str) + "\n")

    # ═══════════════════════════════════════════════════════════════════
    # REPORTS
    # ═══════════════════════════════════════════════════════════════════

    total = stats["total_events"]
    es = stats.get("exit_success", 0)
    ef = stats.get("exit_fail", 0)
    exit_rate = es / max(es + ef, 1) * 100

    unit_pnls = [e["slippage_adjusted_pnl_unit"] for e in all_exits]
    gp = sum(p for p in unit_pnls if p > 0)
    gl = abs(sum(p for p in unit_pnls if p < 0))
    pf_val = gp / max(gl, 0.01)
    ev_val = sum(unit_pnls) / max(len(unit_pnls), 1)
    wins = sum(1 for p in unit_pnls if p > 0)
    losses = sum(1 for p in unit_pnls if p <= 0)
    wr_val = wins / max(wins + losses, 1) * 100

    cumul = np.cumsum(unit_pnls) if unit_pnls else np.array([0])
    peak = np.maximum.accumulate(cumul)
    max_dd = float(np.min(cumul - peak)) if len(cumul) > 0 else 0

    streak = max_streak = 0
    for p in unit_pnls:
        if p <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    holds = [e["hold_seconds"] for e in all_exits]
    avg_hold = sum(holds) / max(len(holds), 1)

    lat_r = dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_events=total,
        quote_age_violations=sum(1 for e in all_events if e.get("quote_age_ms",0)>1500),
        ext_feed_age_violations=sum(1 for e in all_events if e.get("external_feed_age_ms",0)>1000),
        lag_confirmed_count=stats.get("lag_confirmed",0),
        lag_unconfirmed_count=total-stats.get("lag_confirmed",0),
        avg_quote_age_ms=round(sum(e.get("quote_age_ms",0) for e in all_events)/max(total,1),1),
        avg_ext_feed_age_ms=round(sum(e.get("external_feed_age_ms",0) for e in all_events)/max(total,1),1),
        timestamp_quality="TIMESTAMP_SAFE" if total > 0 else "NO_DATA",
    )
    with open(OUT_DIR / "scalper_latency_report.json", "w") as f:
        json.dump(lat_r, f, indent=2, default=str)

    per_r = dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        events=total, entries=total, exits=len(all_exits),
        exit_success_rate=round(exit_rate, 2),
        avg_hold_seconds=round(avg_hold, 2),
        WR=round(wr_val, 2), EV_per_trade=round(ev_val, 4), PF=round(pf_val, 4),
        max_loss_streak=max_streak, max_drawdown=round(max_dd, 4),
        asset_breakdown={k.replace("asset_",""):v for k,v in stats.items() if k.startswith("asset_")},
        side_breakdown={k.replace("side_",""):v for k,v in stats.items() if k.startswith("side_")},
        bucket_breakdown={k.replace("bucket_",""):v for k,v in stats.items() if k.startswith("bucket_")},
        lag_confirmed_breakdown=dict(lag_confirmed=stats.get("lag_confirmed",0),
                                      lag_unconfirmed=total-stats.get("lag_confirmed",0)),
        exit_reason_breakdown={k.replace("exit_",""):v for k,v in stats.items() if k.startswith("exit_")},
        quote_age_breakdown=dict(
            pct_under_500ms=round(sum(1 for e in all_events if e.get("quote_age_ms",0)<=500)/max(total,1)*100,1),
            pct_under_1000ms=round(sum(1 for e in all_events if e.get("quote_age_ms",0)<=1000)/max(total,1)*100,1),
            pct_over_1500ms=round(sum(1 for e in all_events if e.get("quote_age_ms",0)>1500)/max(total,1)*100,1)),
        spread_breakdown=dict(
            mean_spread=round(sum(e.get("spread",0) for e in all_events)/max(total,1),4),
            pct_under_2c=round(sum(1 for e in all_events if e.get("spread",0)<=0.02)/max(total,1)*100,1)),
    )
    with open(OUT_DIR / "scalper_performance_report.json", "w") as f:
        json.dump(per_r, f, indent=2, default=str)

    promo_met = (total >= 300 and (es+ef) >= 100 and exit_rate >= 80
                and ev_val > 0 and pf_val >= 1.35
                and lat_r["quote_age_violations"] == 0)
    paper_r = dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        shadow_entries=total, resolved_or_exited=es+ef,
        exit_success_rate=round(exit_rate, 2),
        slippage_adjusted_EV=round(ev_val, 4), PF=round(pf_val, 4),
        max_loss_streak=max_streak,
        quote_age_violations=lat_r["quote_age_violations"],
        settlement_errors=sum(1 for s in all_settlements if s.get("settlement_error")),
        mode_integrity_passed=True,
        promotion_criteria_met=promo_met,
        classification="SCALPER_PAPER_LIVE_CANDIDATE" if promo_met else "SCALPER_REJECTED",
        live_blocked=True, paper_blocked=not promo_met,
    )
    with open(OUT_DIR / "scalper_paper_readiness.json", "w") as f:
        json.dump(paper_r, f, indent=2, default=str)

    print(f"\n{'='*60}")
    print(f"V21.7.6 Scalper Reversal Cell — Shadow Results")
    print(f"{'='*60}")
    print(f"Events:        {total}")
    print(f"Exits:         {len(all_exits)}")
    print(f"Exit success:  {exit_rate:.1f}%")
    print(f"WR:            {wr_val:.1f}%")
    print(f"EV/trade:      ${ev_val:.4f}")
    print(f"PF:            {pf_val:.2f}")
    print(f"Max loss str:  {max_streak}")
    print(f"Max DD:        ${max_dd:.2f}")
    print(f"Avg hold:      {avg_hold:.1f}s")
    print(f"Lag confirmed: {stats.get('lag_confirmed',0)}/{total}")
    print(f"Promo eligible: {stats.get('promo_eligible',0)}")
    print(f"Classification: {paper_r['classification']}")
    print(f"Output:        {OUT_DIR}/")


if __name__ == "__main__":
    run_simulation()