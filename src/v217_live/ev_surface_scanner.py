#!/usr/bin/env python3
"""
V21.7.19 — EV Surface Scanner
================================
Scans all asset/interval/side/bucket combinations for expected value.
Does NOT trade. Classification: SHADOW_ONLY for non-canary buckets.

Live execution remains BTC DOWN 15m/5m only, 3-8¢ only.
"""

import json, time, logging, sys, urllib.request
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v21719_execution_edges")
OUT_DIR.mkdir(parents=True, exist_ok=True)

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
INTERVALS = ["5m", "15m"]
SIDES = ["UP", "DOWN"]
PRICE_BUCKETS = [
    (0.00, 0.03, "0-3¢"),
    (0.03, 0.05, "3-5¢"),
    (0.05, 0.08, "5-8¢"),
    (0.08, 0.12, "8-12¢"),
    (0.12, 0.20, "12-20¢"),
    (0.20, 0.40, "20-40¢"),
    (0.40, 0.60, "40-60¢"),
    (0.60, 0.85, "60-85¢"),
    (0.85, 0.99, "85-99¢"),
]

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

# V21.7.18 resolution friction constants
SPREAD_COST = 0.01
SLIPPAGE_PCT = 0.005
REDEEM_GAS_COST = 0.50
RESOLUTION_TIME_S = 900
ANNUAL_OPPORTUNITY_RATE = 0.05

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('ev_surface_scanner')


def classify_bucket(price, side, asset, interval):
    """Classify a bucket for live eligibility."""
    if (asset == "BTC" and interval in ("5m", "15m") and side == "DOWN"
            and 0.03 <= price <= 0.08):
        return "LIVE_ELIGIBLE"
    if 0.03 <= price <= 0.12:
        return "PAPER_ONLY"
    if 0.12 <= price <= 0.20:
        return "SHADOW_ONLY"
    return "REJECTED"


def compute_ev_surface(token_id, side, asset, interval):
    """Compute EV surface for a single token."""
    try:
        # Fetch orderbook
        url = f"{CLOB_URL}/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC/21.7.19'})
        resp = urllib.request.urlopen(req, timeout=5)
        book = json.loads(resp.read())
        
        bids = book.get('bids', [])
        asks = book.get('asks', [])
        
        if not bids or not asks:
            return None
        
        best_bid = float(bids[0]['price'])
        best_ask = float(asks[0]['price'])
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        
        # Depth analysis
        bid_depth = sum(float(b.get('size', 0)) for b in bids[:5])
        ask_depth = sum(float(a.get('size', 0)) for a in asks[:5])
        total_depth = bid_depth + ask_depth
        depth_imbalance = (bid_depth - ask_depth) / max(total_depth, 0.001)
        
        # Model probability from mid price
        # For UP token: mid ≈ P(UP wins)
        # For DOWN token: mid ≈ P(DOWN wins)
        model_probability = mid if side == "UP" else (1.0 - mid)
        
        # Market implied probability
        market_probability = mid if side == "UP" else (1.0 - mid)
        
        # EV calculations
        entry_price = best_ask if side == "UP" else best_ask  # Buying at ask
        
        # Raw EV
        raw_ev = model_probability - entry_price
        
        # Spread-adjusted EV
        spread_adj_ev = model_probability - entry_price - spread / 2
        
        # Slippage-adjusted EV
        slippage_adj_ev = model_probability - entry_price - spread / 2 - entry_price * SLIPPAGE_PCT
        
        # Resolution friction adjusted EV
        position_usd = 1.0  # $1 position for EV calculation
        lockup_penalty = position_usd * (RESOLUTION_TIME_S / 86400) * ANNUAL_OPPORTUNITY_RATE / 365
        gas = REDEEM_GAS_COST if model_probability > 0.5 else 0
        friction = spread * position_usd / entry_price + lockup_penalty + gas
        friction_adj_ev = slippage_adj_ev - friction
        
        # Depth-adjusted EV (reduce by depth imbalance penalty)
        depth_penalty = abs(depth_imbalance) * 0.02
        depth_adj_ev = friction_adj_ev - depth_penalty
        
        # Fill quality
        expected_fill_quality = min(1.0, total_depth / 100) * (1 - spread)
        expected_fill_quality = max(0, min(1, expected_fill_quality))
        
        # Time to expiry estimate from slug
        tte_estimate = 300 if interval == "5m" else 900  # seconds
        
        # Bucket
        bucket_name = "unknown"
        for lo, hi, name in PRICE_BUCKETS:
            if lo <= mid < hi:
                bucket_name = name
                break
        
        classification = classify_bucket(mid, side, asset, interval)
        
        return {
            'timestamp': int(time.time() * 1000),
            'asset': asset, 'interval': interval, 'side': side,
            'token_id': token_id, 'bucket': bucket_name,
            'mid_price': round(mid, 4),
            'best_bid': round(best_bid, 4),
            'best_ask': round(best_ask, 4),
            'spread': round(spread, 4),
            'model_probability': round(model_probability, 4),
            'market_probability': round(market_probability, 4),
            'raw_ev': round(raw_ev, 6),
            'spread_adj_ev': round(spread_adj_ev, 6),
            'slippage_adj_ev': round(slippage_adj_ev, 6),
            'friction_adj_ev': round(friction_adj_ev, 6),
            'depth_adj_ev': round(depth_adj_ev, 6),
            'bid_depth': round(bid_depth, 2),
            'ask_depth': round(ask_depth, 2),
            'depth_imbalance': round(depth_imbalance, 4),
            'total_depth': round(total_depth, 2),
            'expected_fill_quality': round(expected_fill_quality, 4),
            'tte_estimate_s': tte_estimate,
            'classification': classification,
        }
    except Exception as e:
        log.debug(f"EV compute error for {token_id}: {e}")
        return None


def scan_surface():
    """Scan all active markets and compute EV surface."""
    log.info("EV Surface Scanner starting — OBSERVATION ONLY, no live trading")
    
    all_events = []
    bucket_summary = defaultdict(lambda: {
        'count': 0, 'ev_sum': 0, 'friction_ev_sum': 0,
        'depth_ev_sum': 0, 'fill_quality_sum': 0,
    })
    
    for asset in ASSETS:
        for interval in INTERVALS:
            slug_prefix = f"{asset.lower()}-updown-{interval}"
            try:
                url = f"{GAMMA_URL}/markets?active=true&closed=false&limit=10&slug_contains={slug_prefix}"
                req = urllib.request.Request(url, headers={'User-Agent': 'FDC/21.7.19'})
                resp = urllib.request.urlopen(req, timeout=10)
                markets = json.loads(resp.read())
            except Exception as e:
                log.error(f"Gamma fetch {slug_prefix}: {e}")
                continue
            
            for m in markets:
                slug = m.get('slug', '')
                tokens_str = m.get('clobTokenIds', '[]')
                try:
                    tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
                except json.JSONDecodeError:
                    continue
                outcomes = m.get('outcomes', '[]')
                try:
                    outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                except json.JSONDecodeError:
                    outcomes = ['Up', 'Down']
                
                for i, tid in enumerate(tokens):
                    side = 'UP' if (i < len(outcomes) and outcomes[i] == 'Up') else 'DOWN'
                    
                    ev_data = compute_ev_surface(tid, side, asset, interval)
                    if ev_data:
                        all_events.append(ev_data)
                        
                        # Bucket aggregation
                        bkey = f"{asset}_{interval}_{side}_{ev_data['bucket']}"
                        bs = bucket_summary[bkey]
                        bs['count'] += 1
                        bs['ev_sum'] += ev_data['raw_ev']
                        bs['friction_ev_sum'] += ev_data['friction_adj_ev']
                        bs['depth_ev_sum'] += ev_data['depth_adj_ev']
                        bs['fill_quality_sum'] += ev_data['expected_fill_quality']
                        
                        log.info(f"  {asset}/{interval}/{side} mid={ev_data['mid_price']:.4f} "
                                f"bucket={ev_data['bucket']} class={ev_data['classification']} "
                                f"raw_EV={ev_data['raw_ev']:.4f} friction_EV={ev_data['friction_adj_ev']:.4f}")
    
    # Generate report
    report = {
        'classification': 'EV_SURFACE_SHADOW_ACTIVE',
        'version': 'V21.7.19',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'total_scanned': len(all_events),
        'live_gates_unchanged': True,
        'buckets': {},
    }
    
    for bkey, bs in sorted(bucket_summary.items()):
        n = max(bs['count'], 1)
        report['buckets'][bkey] = {
            'count': bs['count'],
            'avg_raw_ev': round(bs['ev_sum'] / n, 6),
            'avg_friction_ev': round(bs['friction_ev_sum'] / n, 6),
            'avg_depth_ev': round(bs['depth_ev_sum'] / n, 6),
            'avg_fill_quality': round(bs['fill_quality_sum'] / n, 4),
        }
    
    # Live eligible summary
    live_events = [e for e in all_events if e['classification'] == 'LIVE_ELIGIBLE']
    report['live_eligible'] = {
        'count': len(live_events),
        'avg_friction_ev': round(float(np.mean([e['friction_adj_ev'] for e in live_events])), 6) if live_events else 0,
        'avg_depth_ev': round(float(np.mean([e['depth_adj_ev'] for e in live_events])), 6) if live_events else 0,
    }
    
    # Save
    with open(OUT_DIR / 'ev_surface_report.json', 'w') as f:
        json.dump(report, f, indent=2, default=str)
    with open(OUT_DIR / 'ev_surface_events.jsonl', 'w') as f:
        for ev in all_events:
            f.write(json.dumps(ev, default=str) + '\n')
    
    log.info(f"EV Surface: {len(all_events)} tokens scanned, {len(live_events)} LIVE_ELIGIBLE")
    return report


if __name__ == '__main__':
    scan_surface()