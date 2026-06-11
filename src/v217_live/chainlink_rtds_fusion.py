#!/usr/bin/env python3
"""
V21.7.19 — Chainlink / RTDS Fusion Layer
==========================================
Tracks settlement-source price fusion across Chainlink, Binance, Bybit, OKX,
and Polymarket RTDS/token prices. Computes cross-exchange median, velocity,
and repricing lag for every active BTC/ETH/SOL/XRP UP/DOWN market.

Classification: OBSERVATION ONLY — no live trading authority.
Live gates UNCHANGED.
"""

import json, time, threading, logging, os, sys
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime, timezone
import numpy as np

# Add parent paths for PM access
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

try:
    import ccxt
except ImportError:
    ccxt = None

try:
    import aiohttp
except ImportError:
    aiohttp = None

OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v21719_execution_edges")
OUT_DIR.mkdir(parents=True, exist_ok=True)

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
INTERVALS = ["5m", "15m"]
SIDES = ["UP", "DOWN"]
VELOCITY_WINDOWS = [1, 3, 5, 15, 30, 60]  # seconds

GAMMA_URL = "https://gamma-api.polymarket.com"

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('chainlink_rtds_fusion')


class ChainlinkRTDSFusion:
    def __init__(self):
        self.prices = defaultdict(lambda: defaultdict(dict))  # source -> asset -> {'bid','ask','last','ts'}
        self.token_quotes = {}  # token_id -> {bid, ask, last, ts, asset, interval, side}
        self.velocity_windows = {w: defaultdict(lambda: defaultdict(list)) for w in VELOCITY_WINDOWS}
        self.events = []
        self.report = {}
        self.running = False
        self.poll_count = 0
        self.last_report_time = 0
        
        # ccxt exchanges
        self.exchanges = {}
        if ccxt:
            self.exchanges['binance'] = ccxt.binance({'enableRateLimit': True})
            self.exchanges['bybit'] = ccxt.bybit({'enableRateLimit': True})
            # OKX has ccxt bug, use REST directly
            self.exchanges['okx'] = None
        
        # Chainlink price feeds (on-chain, use ccxt for ETH/USD proxy approximation)
        self.chainlink_feeds = {
            'BTC': '0xF403008594341A7A041928F6632cDE0C127833e3',
            'ETH': '0x5f4eC3Df9cbd43714FE2740f5E3616155c5AbD4B',
            'SOL': None,  # No canonical Chainlink SOL/USD on mainnet
            'XRP': None,  # No canonical Chainlink XRP/USD on mainnet
        }
    
    def fetch_external_prices(self):
        """Fetch Binance, Bybit spot + perp prices."""
        results = {}
        symbols = {
            'BTC': {'binance_spot': 'BTC/USDT', 'binance_perp': 'BTC/USDT:USDT',
                     'bybit_perp': 'BTC/USDT:USDT'},
            'ETH': {'binance_spot': 'ETH/USDT', 'binance_perp': 'ETH/USDT:USDT',
                     'bybit_perp': 'ETH/USDT:USDT'},
            'SOL': {'binance_spot': 'SOL/USDT', 'binance_perp': 'SOL/USDT:USDT',
                     'bybit_perp': 'SOL/USDT:USDT'},
            'XRP': {'binance_spot': 'XRP/USDT', 'binance_perp': 'XRP/USDT:USDT',
                     'bybit_perp': 'XRP/USDT:USDT'},
        }
        
        for asset, feeds in symbols.items():
            prices = {}
            for feed_name, symbol in feeds.items():
                ex_name = feed_name.split('_')[0]
                ex = self.exchanges.get(ex_name)
                if ex is None:
                    continue
                try:
                    t = ex.fetch_ticker(symbol)
                    prices[feed_name] = {
                        'bid': t.get('bid'), 'ask': t.get('ask'),
                        'last': t.get('last'), 'ts': t.get('timestamp', int(time.time()*1000))
                    }
                except Exception as e:
                    log.debug(f"{feed_name} fetch error: {e}")
            
            # OKX perp via REST
            okx_sym = f"{asset}-USDT-SWAP"
            try:
                import urllib.request
                url = f"https://www.okx.com/api/v5/market/ticker?instId={okx_sym}"
                req = urllib.request.Request(url, headers={'User-Agent': 'FDC/21.7.19'})
                resp = urllib.request.urlopen(req, timeout=5)
                data = json.loads(resp.read())
                if data.get('data'):
                    d = data['data'][0]
                    prices['okx_perp'] = {
                        'bid': float(d.get('bidPx', 0)), 'ask': float(d.get('askPx', 0)),
                        'last': float(d.get('last', 0)),
                        'ts': int(d.get('ts', int(time.time()*1000)))
                    }
            except Exception:
                pass
            
            results[asset] = prices
        
        return results
    
    def fetch_chainlink_approx(self):
        """
        Chainlink on-chain requires RPC + contract calls.
        Approximate via Binance spot (closest to Chainlink price reference).
        Real Chainlink would need Polygon RPC + feed aggregator read.
        """
        approx = {}
        for asset in ASSETS:
            ex = self.exchanges.get('binance')
            if ex is None:
                continue
            sym = f"{asset}/USDT"
            try:
                t = ex.fetch_ticker(sym)
                approx[asset] = {
                    'price': t.get('last', 0),
                    'bid': t.get('bid', 0),
                    'ask': t.get('ask', 0),
                    'ts': int(time.time() * 1000)
                }
            except Exception:
                pass
        return approx
    
    def compute_velocities(self, asset, current_price, now_s):
        """Compute price velocity for each window."""
        for w in VELOCITY_WINDOWS:
            self.velocity_windows[w][asset].append((now_s, current_price))
            # Keep only last 2*w samples
            max_len = max(w * 2, 10)
            if len(self.velocity_windows[w][asset]) > max_len:
                self.velocity_windows[w][asset] = self.velocity_windows[w][asset][-max_len:]
    
    def get_velocity(self, asset, window_s):
        """Get price velocity over window."""
        series = self.velocity_windows[window_s][asset]
        if len(series) < 2:
            return 0.0
        # Find entries within window
        now_s = series[-1][0]
        cutoff = now_s - window_s
        recent = [(t, p) for t, p in series if t >= cutoff]
        if len(recent) < 2:
            return 0.0
        dt = recent[-1][0] - recent[0][0]
        dp = recent[-1][1] - recent[0][1]
        if dt == 0:
            return 0.0
        return dp / dt  # price change per second
    
    def fetch_pm_token_quotes(self):
        """Fetch active PM token prices from Gamma API."""
        import urllib.request
        
        quotes = {}
        for asset in ASSETS:
            for interval in INTERVALS:
                slug_prefix = f"{asset.lower()}-updown-{interval}"
                try:
                    url = f"{GAMMA_URL}/markets?active=true&closed=false&limit=10&slug_contains={slug_prefix}"
                    req = urllib.request.Request(url, headers={'User-Agent': 'FDC/21.7.19'})
                    resp = urllib.request.urlopen(req, timeout=10)
                    markets = json.loads(resp.read())
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
                            # Get best bid/ask from CLOB
                            try:
                                book_url = f"https://clob.polymarket.com/book?token_id={tid}"
                                breq = urllib.request.Request(book_url, headers={'User-Agent': 'FDC/21.7.19'})
                                bresp = urllib.request.urlopen(breq, timeout=5)
                                bdata = json.loads(bresp.read())
                                bids = bdata.get('bids', [])
                                asks = bdata.get('asks', [])
                                # CLOB API returns asks DESCENDING — sort for best prices
                                sorted_bids = sorted(bids, key=lambda x: float(x['price']), reverse=True) if bids else []
                                sorted_asks = sorted(asks, key=lambda x: float(x['price'])) if asks else []
                                best_bid = float(sorted_bids[0]['price']) if sorted_bids else 0
                                best_ask = float(sorted_asks[0]['price']) if sorted_asks else 0
                                mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
                            except Exception:
                                best_bid = best_ask = mid = 0
                            
                            quotes[tid] = {
                                'asset': asset, 'interval': interval, 'side': side,
                                'slug': slug, 'bid': best_bid, 'ask': best_ask,
                                'mid': mid, 'ts': int(time.time() * 1000),
                                'token_id': tid,
                            }
                except Exception as e:
                    log.debug(f"PM fetch {slug_prefix}: {e}")
        
        return quotes
    
    def compute_cross_median(self, asset_prices):
        """Compute cross-exchange median for an asset."""
        all_prices = []
        for feed, data in asset_prices.items():
            if data and data.get('last'):
                all_prices.append(data['last'])
            elif data and data.get('bid') and data.get('ask'):
                all_prices.append((data['bid'] + data['ask']) / 2)
        if not all_prices:
            return 0
        return float(np.median(all_prices))
    
    def run_fusion_cycle(self):
        """Single fusion observation cycle."""
        now = time.time()
        now_ms = int(now * 1000)
        now_s = int(now)
        
        log.info(f"Fusion cycle #{self.poll_count + 1}")
        
        # Fetch external prices
        ext_prices = self.fetch_external_prices()
        chainlink_approx = self.fetch_chainlink_approx()
        pm_quotes = self.fetch_pm_token_quotes()
        
        events = []
        
        for asset in ASSETS:
            asset_ext = ext_prices.get(asset, {})
            chainlink_data = chainlink_approx.get(asset, {})
            
            # Cross-exchange median
            ext_median = self.compute_cross_median(asset_ext)
            chainlink_price = chainlink_data.get('price', 0) if chainlink_data else 0
            
            if ext_median > 0:
                self.compute_velocities(asset, ext_median, now_s)
            
            # Per-asset velocities
            velocities = {}
            for w in VELOCITY_WINDOWS:
                v = self.get_velocity(asset, w)
                velocities[f'external_median_v{w}s'] = round(v, 6)
                velocities[f'chainlink_v{w}s'] = round(v, 6)  # Approximated by Binance spot
            
            # Per-token deltas
            for tid, tq in pm_quotes.items():
                if tq['asset'] != asset:
                    continue
                
                token_mid = tq.get('mid', 0)
                token_price_delta = 0
                token_repricing_delay = 0
                
                if ext_median > 0 and token_mid > 0:
                    # Token implied probability vs external price
                    # For UP token: implied probability ≈ token_mid
                    # For DOWN token: implied probability ≈ 1 - token_mid
                    token_price_delta = round(token_mid - (ext_median / 100000), 8) if ext_median > 100000 else 0
                
                if tq.get('ts') and ext_median > 0:
                    token_repricing_delay = now_ms - tq.get('ts', now_ms)
                
                event = {
                    'timestamp': now_ms,
                    'asset': asset,
                    'interval': tq.get('interval', ''),
                    'side': tq.get('side', ''),
                    'slug': tq.get('slug', ''),
                    'chainlink_price': chainlink_price,
                    'binance_spot': asset_ext.get('binance_spot', {}).get('last', 0),
                    'bybit_perp': asset_ext.get('bybit_perp', {}).get('last', 0),
                    'okx_perp': asset_ext.get('okx_perp', {}).get('last', 0),
                    'external_median': ext_median,
                    'token_bid': tq.get('bid', 0),
                    'token_ask': tq.get('ask', 0),
                    'token_mid': token_mid,
                    'token_price_delta': token_price_delta,
                    'token_repricing_delay_ms': token_repricing_delay,
                    **velocities,
                }
                events.append(event)
                self.events.append(event)
        
        # Keep last 10000 events
        if len(self.events) > 10000:
            self.events = self.events[-10000:]
        
        self.poll_count += 1
        return events
    
    def generate_report(self):
        """Generate fusion report."""
        now = time.time()
        
        # Aggregate stats
        asset_stats = defaultdict(lambda: {
            'samples': 0, 'ext_median_mean': 0, 'ext_median_std': 0,
            'chainlink_mean': 0, 'token_deltas': [],
            'velocity_v1s': [], 'velocity_v5s': [], 'velocity_v15s': [],
        })
        
        for ev in self.events[-500:]:  # Last 500 events
            a = ev['asset']
            s = asset_stats[a]
            s['samples'] += 1
            if ev.get('external_median'):
                s['ext_median_mean'] += ev['external_median']
            if ev.get('chainlink_price'):
                s['chainlink_mean'] += ev['chainlink_price']
            if ev.get('token_price_delta'):
                s['token_deltas'].append(ev['token_price_delta'])
            for w in [1, 5, 15]:
                key = f'external_median_v{w}s'
                if ev.get(key):
                    s[f'velocity_v{w}s'].append(ev[key])
        
        report_assets = {}
        for asset, s in asset_stats.items():
            n = max(s['samples'], 1)
            report_assets[asset] = {
                'samples': s['samples'],
                'ext_median_mean': round(s['ext_median_mean'] / n, 2) if s['ext_median_mean'] else 0,
                'chainlink_mean': round(s['chainlink_mean'] / n, 2) if s['chainlink_mean'] else 0,
                'token_delta_mean': round(float(np.mean(s['token_deltas'])), 8) if s['token_deltas'] else 0,
                'token_delta_std': round(float(np.std(s['token_deltas'])), 8) if len(s['token_deltas']) > 1 else 0,
                'velocity_v1s_mean': round(float(np.mean(s['velocity_v1s'])), 6) if s['velocity_v1s'] else 0,
                'velocity_v5s_mean': round(float(np.mean(s['velocity_v5s'])), 6) if s['velocity_v5s'] else 0,
                'velocity_v15s_mean': round(float(np.mean(s['velocity_v15s'])), 6) if s['velocity_v15s'] else 0,
            }
        
        self.report = {
            'classification': 'CHAINLINK_RTDS_FUSION_ACTIVE',
            'version': 'V21.7.19',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'poll_count': self.poll_count,
            'total_events': len(self.events),
            'assets': report_assets,
            'live_gates_unchanged': True,
        }
        
        # Save report
        with open(OUT_DIR / 'chainlink_rtds_fusion_report.json', 'w') as f:
            json.dump(self.report, f, indent=2, default=str)
        
        # Save recent events as JSONL
        with open(OUT_DIR / 'chainlink_rtds_fusion_events.jsonl', 'w') as f:
            for ev in self.events[-1000:]:
                f.write(json.dumps(ev, default=str) + '\n')
        
        return self.report
    
    def run(self, cycles=5, interval=30):
        """Run fusion for N cycles."""
        self.running = True
        log.info(f"Chainlink/RTDS Fusion starting — {cycles} cycles, {interval}s interval")
        log.info("Classification: OBSERVATION ONLY — no live trading authority")
        
        for i in range(cycles):
            if not self.running:
                break
            try:
                events = self.run_fusion_cycle()
                log.info(f"Cycle {i+1}/{cycles}: {len(events)} fusion events, {self.poll_count} total polls")
            except Exception as e:
                log.error(f"Cycle {i+1} error: {e}")
            
            if i < cycles - 1:
                time.sleep(interval)
        
        # Generate final report
        report = self.generate_report()
        log.info(f"Fusion complete — {report['total_events']} events, {len(report['assets'])} assets")
        log.info(f"Report: {OUT_DIR / 'chainlink_rtds_fusion_report.json'}")
        self.running = False


if __name__ == '__main__':
    fusion = ChainlinkRTDSFusion()
    fusion.run(cycles=5, interval=30)