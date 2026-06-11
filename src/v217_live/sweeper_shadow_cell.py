#!/usr/bin/env python3
"""
V21.7.19 — Sweeper Shadow Cell
================================
Shadow-only sweeper observation module. Tracks near-certain outcome
opportunities (price 99.2-99.8¢) WITHOUT executing any trades.

Classification: SWEEPER_SHADOW_ONLY
Do NOT paper trade. Do NOT live trade.
Promotion requires: resolved_shadow_events >= 100, tail_loss_rate acceptable,
net_EV positive after fees/friction, queue-position sensitivity understood.
"""

import json, time, logging, sys, urllib.request
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v21719_execution_edges")
OUT_DIR.mkdir(parents=True, exist_ok=True)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

# Sweeper parameters
SWEEPER_PRICE_LO = 0.992  # 99.2¢
SWEEPER_PRICE_HI = 0.998  # 99.8¢
MIN_REMAINING_TIME_S = 30  # At least 30s remaining
MIN_DEPTH_USD = 10  # At least $10 in book

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
INTERVALS = ["5m", "15m"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('sweeper_shadow')


def fetch_active_markets():
    """Fetch all active UP/DOWN markets from Gamma."""
    markets = []
    for asset in ASSETS:
        for interval in INTERVALS:
            slug_prefix = f"{asset.lower()}-updown-{interval}"
            try:
                url = f"{GAMMA_URL}/markets?active=true&closed=false&limit=10&slug_contains={slug_prefix}"
                req = urllib.request.Request(url, headers={'User-Agent': 'FDC/21.7.19'})
                resp = urllib.request.urlopen(req, timeout=10)
                data = json.loads(resp.read())
                for m in data:
                    m['_asset'] = asset
                    m['_interval'] = interval
                    markets.append(m)
            except Exception as e:
                log.debug(f"Gamma fetch {slug_prefix}: {e}")
    return markets


def evaluate_sweeper_candidate(token_id, side, asset, interval, market_data):
    """Evaluate a token as a sweeper shadow candidate."""
    # Fetch orderbook
    try:
        url = f"{CLOB_URL}/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={'User-Agent': 'FDC/21.7.19'})
        resp = urllib.request.urlopen(req, timeout=5)
        book = json.loads(resp.read())
    except Exception:
        return None
    
    bids = book.get('bids', [])
    asks = book.get('asks', [])
    
    if not bids or not asks:
        return None
    
    best_bid = float(bids[0]['price'])
    best_ask = float(asks[0]['price'])
    mid = (best_bid + best_ask) / 2
    
    # Check if in sweeper price range
    if not (SWEEPER_PRICE_LO <= mid <= SWEEPER_PRICE_HI or
            (1 - SWEEPER_PRICE_HI) <= mid <= (1 - SWEEPER_PRICE_LO)):
        return None
    
    # The "winning" side has high price, "losing" side has low price
    is_near_certain = mid >= SWEEPER_PRICE_LO
    
    # Available depth
    bid_depth_usd = sum(float(b.get('size', 0)) * float(b.get('price', 0)) for b in bids[:5])
    ask_depth_usd = sum(float(a.get('size', 0)) * float(a.get('price', 0)) for a in asks[:5])
    total_depth = bid_depth_usd + ask_depth_usd
    
    # Spread
    spread = best_ask - best_bid
    
    # Settlement probability estimate
    if is_near_certain:
        settlement_prob = mid  # UP token at 99.5¢ ≈ 99.5% chance of winning
        tail_reversal_risk = 1.0 - mid
    else:
        settlement_prob = 1.0 - mid  # DOWN token at 0.5¢ ≈ 0.5% chance (not sweeper)
        tail_reversal_risk = mid
        return None  # Low-price side is NOT a sweeper candidate
    
    # Expected penny edge
    # If we buy at best_ask (99.7¢) and it settles at 100¢, profit = 0.3¢
    # But gas/fees eat into that
    entry_price = best_ask
    if entry_price >= 1.0:
        return None
    
    gross_payout = 1.0  # Binary settlement
    gross_profit = gross_payout - entry_price
    gas_cost = 0.50  # Polygon redemption gas
    spread_cost = spread / 2
    slippage = entry_price * 0.005
    net_profit_per_dollar = gross_profit - gas_cost / 100 - spread_cost - slippage
    
    # Time to expiry estimate
    slug = market_data.get('slug', '')
    tte_s = 300 if '5m' in slug else 900 if '15m' in slug else 0
    
    # Queue priority estimate (rough)
    queue_position = len(asks)  # Number of ask levels ahead
    
    # Shadow classification
    classification = "SWEEPER_SHADOW_ONLY"
    
    return {
        'timestamp': int(time.time() * 1000),
        'asset': asset,
        'interval': interval,
        'side': side,
        'slug': slug,
        'token_id': token_id,
        'mid_price': round(mid, 4),
        'best_bid': round(best_bid, 4),
        'best_ask': round(best_ask, 4),
        'spread': round(spread, 6),
        'settlement_probability': round(settlement_prob, 6),
        'tail_reversal_risk': round(tail_reversal_risk, 6),
        'available_depth_usd': round(total_depth, 2),
        'bid_depth_usd': round(bid_depth_usd, 2),
        'ask_depth_usd': round(ask_depth_usd, 2),
        'queue_priority_estimate': queue_position,
        'expected_penny_edge': round(net_profit_per_dollar, 6),
        'capital_required_usd': round(entry_price * 1, 2),  # $1 position
        'binary_outcome': 'WIN' if settlement_prob > 0.99 else 'UNCERTAIN',
        'tte_estimate_s': tte_s,
        'classification': classification,
        'promotion_status': 'BLOCKED — needs >= 100 resolved shadow events',
    }


def run_sweeper_shadow():
    log.info("Sweeper Shadow Cell starting — SHADOW ONLY, NO TRADING")
    log.info(f"Price range: {SWEEPER_PRICE_LO:.1%} - {SWEEPER_PRICE_HI:.1%}")
    
    markets = fetch_active_markets()
    log.info(f"Scanning {len(markets)} markets for sweeper candidates")
    
    shadow_events = []
    candidate_count = 0
    
    for m in markets:
        asset = m.get('_asset', '')
        interval = m.get('_interval', '')
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
            
            result = evaluate_sweeper_candidate(tid, side, asset, interval, m)
            if result:
                shadow_events.append(result)
                candidate_count += 1
                log.info(f"  SHADOW: {asset}/{interval}/{side} mid={result['mid_price']:.4f} "
                        f"prob={result['settlement_probability']:.4f} edge={result['expected_penny_edge']:.4f} "
                        f"depth=${result['available_depth_usd']:.0f} tte={result['tte_estimate_s']}s")
    
    # Aggregate stats
    stats = {
        'total_markets_scanned': len(markets),
        'sweeper_candidates_found': candidate_count,
        'avg_settlement_prob': round(float(np.mean([e['settlement_probability'] for e in shadow_events])), 4) if shadow_events else 0,
        'avg_penny_edge': round(float(np.mean([e['expected_penny_edge'] for e in shadow_events])), 6) if shadow_events else 0,
        'avg_depth_usd': round(float(np.mean([e['available_depth_usd'] for e in shadow_events])), 2) if shadow_events else 0,
        'total_depth_usd': round(sum(e['available_depth_usd'] for e in shadow_events), 2),
    }
    
    report = {
        'classification': 'SWEEPER_SHADOW_ONLY',
        'version': 'V21.7.19',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'stats': stats,
        'promotion_requirements': {
            'resolved_shadow_events_needed': 100,
            'tail_loss_rate_threshold': 'acceptable (TBD)',
            'net_EV_after_fees': 'positive (TBD)',
            'queue_position_sensitivity': 'understood (TBD)',
            'current_status': 'BLOCKED — shadow observation only',
        },
        'live_gates_unchanged': True,
    }
    
    # Save
    with open(OUT_DIR / 'sweeper_shadow_report.json', 'w') as f:
        json.dump(report, f, indent=2, default=str)
    with open(OUT_DIR / 'sweeper_shadow_events.jsonl', 'w') as f:
        for ev in shadow_events:
            f.write(json.dumps(ev, default=str) + '\n')
    
    log.info(f"Sweeper shadow: {candidate_count} candidates, {stats['avg_settlement_prob']:.4f} avg prob, {stats['avg_penny_edge']:.4f} avg edge")
    return report


if __name__ == '__main__':
    run_sweeper_shadow()