#!/usr/bin/env python3
"""
V21.7.19 — Drawdown State Map Engine
======================================
Builds empirical drawdown state maps from PMXT and live shadow data.
Maps (asset, interval, side, price_bucket, tte, velocity, imbalance) →
(future_outcome, future_max_dd, future_recovery_prob, settlement_result).

Classification: DIAGNOSTIC / SHADOW VETO CANDIDATE
Do NOT use as live entry generator.
"""

import json, time, logging, sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v21719_execution_edges")
OUT_DIR.mkdir(parents=True, exist_ok=True)

PMXT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/pmxt_data")

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
INTERVALS = ["5m", "15m"]
SIDES = ["UP", "DOWN"]
PRICE_BUCKETS = [
    (0.00, 0.03, "0-3¢"), (0.03, 0.05, "3-5¢"), (0.05, 0.08, "5-8¢"),
    (0.08, 0.12, "8-12¢"), (0.12, 0.20, "12-20¢"), (0.20, 0.40, "20-40¢"),
    (0.40, 0.60, "40-60¢"), (0.60, 0.85, "60-85¢"), (0.85, 0.99, "85-99¢"),
]
TTE_BUCKETS = [(0, 0.2, "0-20%"), (0.2, 0.4, "20-40%"), (0.4, 0.6, "40-60%"),
                (0.6, 0.8, "60-80%"), (0.8, 1.0, "80-100%")]

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('drawdown_state_mapper')


def get_price_bucket(price):
    for lo, hi, name in PRICE_BUCKETS:
        if lo <= price < hi:
            return name
    return "unknown"


def get_tte_bucket(tte_pct):
    for lo, hi, name in TTE_BUCKETS:
        if lo <= tte_pct < hi:
            return name
    return "unknown"


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


def build_drawdown_map_from_pmxt():
    """Build drawdown state maps from PMXT orderbook data."""
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    
    state_map = defaultdict(lambda: {
        'observations': 0, 'wins': 0, 'max_dd_sum': 0,
        'recovery_count': 0, 'settlement_1_count': 0,
    })
    
    files = sorted(PMXT_DIR.glob("*.parquet")) if PMXT_DIR.exists() else []
    valid_files = []
    for f in files:
        try:
            pf = pq.ParquetFile(str(f))
            if pf.metadata.num_rows > 10000:
                valid_files.append(f)
        except:
            continue
    
    log.info(f"PMXT files: {len(valid_files)}")
    
    for fidx, fpath in enumerate(valid_files[:3]):  # Limit to 3 files for speed
        log.info(f"Processing {fpath.name}...")
        try:
            pf = pq.ParquetFile(str(fpath))
        except:
            continue
        
        for rg_idx in range(0, min(pf.metadata.num_row_groups, 4), 2):
            try:
                t = pf.read_row_group(rg_idx, columns=['market', 'asset_id', 'price', 'event_type'])
            except:
                continue
            
            event_col = t.column('event_type')
            try:
                mask_arr = pc.equal(event_col, 'price_change')
                idxs = np.where(mask_arr.to_numpy())[0]
            except:
                continue
            if len(idxs) == 0:
                del t
                continue
            
            mkt_col = t.column('market')
            aid_col = t.column('asset_id')
            price_col = t.column('price').to_numpy().astype(np.float64)
            
            from collections import defaultdict as dd
            pair_prices = dd(dict)
            step = max(1, len(idxs) // 5000)
            for i in idxs[::step]:
                mv = mkt_col[i.as_py() if hasattr(i, 'as_py') else i]
                cid = mv.hex() if isinstance(mv, (bytes, bytearray)) else str(mv)
                aid = str(aid_col[i])
                p = float(price_col[i])
                if 0.01 < p < 0.99:
                    if aid not in pair_prices[cid]:
                        pair_prices[cid][aid] = []
                    pair_prices[cid][aid].append(p)
            
            del t
            
            for cid, aids in pair_prices.items():
                if len(aids) != 2:
                    continue
                aid_list = list(aids)
                p1 = np.array(aids[aid_list[0]])
                p2 = np.array(aids[aid_list[1]])
                
                if len(p1) < 30 or len(p2) < 30:
                    continue
                
                # Identify cheap vs rich
                if np.mean(p1[-20:]) < np.mean(p2[-20:]):
                    cheap_prices, rich_prices = p1, p2
                    cheap_side = 'DOWN'
                else:
                    cheap_prices, rich_prices = p2, p1
                    cheap_side = 'UP'
                
                # Sample drawdown states
                n = len(cheap_prices)
                rsi_arr = compute_rsi(cheap_prices)
                settlement_window = max(5, n // 5)
                
                for i in range(20, n - 5, max(3, n // 10)):
                    price = float(cheap_prices[i])
                    if price < 0.01 or price > 0.99:
                        continue
                    
                    tte_pct = i / max(n, 1)
                    bucket = get_price_bucket(price)
                    tte_bucket = get_tte_bucket(tte_pct)
                    
                    # Velocity
                    if i >= 5:
                        velocity = abs(float(cheap_prices[i]) - float(cheap_prices[i-5])) / max(float(cheap_prices[i-5]), 0.01)
                    else:
                        velocity = 0
                    
                    # Book imbalance (approximate from price distribution)
                    recent = cheap_prices[max(0,i-20):i+1]
                    if len(recent) > 1:
                        imbalance = (float(np.mean(recent[-5:])) - float(np.mean(recent[:5]))) / max(float(np.mean(recent)), 0.001)
                    else:
                        imbalance = 0
                    
                    # Settlement outcome
                    final_region = cheap_prices[min(i+5, n-5):min(i+settlement_window, n)]
                    if len(final_region) < 3:
                        continue
                    settlement_price = float(np.mean(final_region[-5:]))
                    won = settlement_price > price
                    
                    # Future max drawdown from this point
                    future = cheap_prices[i+1:min(i+1+settlement_window, n)]
                    if len(future) < 2:
                        continue
                    
                    peak = price
                    max_dd = 0
                    for fp in future:
                        fp = float(fp)
                        peak = max(peak, fp)
                        dd = (peak - fp) / max(peak, 0.001)
                        max_dd = max(max_dd, dd)
                    
                    # Recovery: did price return to entry within settlement window?
                    recovered = any(float(fp) >= price for fp in future)
                    
                    # State key
                    key = f"BTC_15m_{cheap_side}_{bucket}_{tte_bucket}_v{velocity:.2f}_imb{imbalance:.2f}"
                    
                    s = state_map[key]
                    s['observations'] += 1
                    if won:
                        s['wins'] += 1
                    s['max_dd_sum'] += max_dd
                    if recovered:
                        s['recovery_count'] += 1
                    if settlement_price >= 0.95:
                        s['settlement_1_count'] += 1
            
            del pair_prices
    
    return dict(state_map)


def run_drawdown_mapper():
    log.info("Drawdown State Mapper starting — DIAGNOSTIC ONLY")
    log.info("Classification: DRAWDOWN_STATE_MAPPING_ACTIVE")
    
    state_map = build_drawdown_map_from_pmxt()
    
    # Aggregate into report
    report = {
        'classification': 'DRAWDOWN_STATE_MAPPING_ACTIVE',
        'version': 'V21.7.19',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'total_states': len(state_map),
        'live_gates_unchanged': True,
        'state_summary': {},
    }
    
    for key, s in sorted(state_map.items()):
        n = max(s['observations'], 1)
        report['state_summary'][key] = {
            'observations': s['observations'],
            'win_rate': round(s['wins'] / n, 4),
            'avg_max_dd': round(s['max_dd_sum'] / n, 4),
            'recovery_rate': round(s['recovery_count'] / n, 4),
            'settlement_1_rate': round(s['settlement_1_count'] / n, 4),
        }
    
    # Save
    with open(OUT_DIR / 'drawdown_state_map.json', 'w') as f:
        json.dump(state_map, f, indent=2, default=str)
    with open(OUT_DIR / 'drawdown_state_report.json', 'w') as f:
        json.dump(report, f, indent=2, default=str)
    
    log.info(f"Drawdown map: {len(state_map)} states mapped")
    return report


if __name__ == '__main__':
    run_drawdown_mapper()