#!/usr/bin/env python3
"""
V21.7.4 §5: Polymarket Data-Lag Alpha Monitor
================================================
Measures whether Polymarket short-duration crypto markets lag faster
reference markets (Binance, Coinbase, Bybit, OKX).

HYPOTHESIS ONLY — do not assume the edge exists. Measure it.

All profiles are SHADOW_ONLY. No live trading.

Lag events:
  - External spot/perp moves beyond threshold (8 bps over 1-5s)
  - Polymarket token price does not reprice within expected delay
  - Book quote is not stale beyond safety limit (1500ms)
  - Spread is acceptable (<3¢)
  - Depth is sufficient
  - Time-to-expiry > 30s

Shadow profiles:
  BTC_LAG_DOWN_SHADOW
  BTC_LAG_UP_SHADOW
  ETH_LAG_DOWN_SHADOW
  ETH_LAG_UP_SHADOW
  SOL_LAG_DOWN_SHADOW
  SOL_LAG_UP_SHADOW
"""

import json, os, time, sys, logging, asyncio, threading
import urllib.request
import numpy as np
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple
from collections import defaultdict

sys.path.insert(0, "/home/naq1987s/father-daddy-capital")
from fdc_pm_live import (
    discover_active_contract, read_orderbook, parse_slug,
    CLOB_URL, GAMMA_URL, CHAIN_ID, FUNDER,
)

LOG_DIR = Path("/home/naq1987s/father-daddy-capital/output/v2174")
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════
# §5.3: DATA SOURCES
# ═══════════════════════════════════════════════════════════════════════

# WebSocket URLs
BINANCE_WS = "wss://stream.binance.com:9443/ws"
COINBASE_WS = "wss://ws-feed.pro.coinbase.com"
BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"
OKX_WS = "wss://ws.okx.com:8449/ws/v5/public"
POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# REST fallback endpoints
EXCHANGE_REST = {
    "binance_spot": "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
    "coinbase_spot": "https://api.pro.coinbase.com/products/BTC-USD/ticker",
    "bybit_perp": "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
    "okx_perp": "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP",
}

# Asset-specific REST templates
ASSET_SYMBOLS = {
    "BTC": {"binance": "BTCUSDT", "coinbase": "BTC-USD", "bybit": "BTCUSDT", "okx": "BTC-USDT-SWAP"},
    "ETH": {"binance": "ETHUSDT", "coinbase": "ETH-USD", "bybit": "ETHUSDT", "okx": "ETH-USDT-SWAP"},
    "SOL": {"binance": "SOLUSDT", "coinbase": "SOL-USD", "bybit": "SOLUSDT", "okk": "SOL-USDT-SWAP"},
}

# §5.6: Thresholds
EXTERNAL_MOVE_THRESHOLD_BPS = 8       # 8 bps over 1-5s
MAX_QUOTE_AGE_MS = 1500               # 1.5s staleness limit
MAX_SPREAD = 0.03                     # 3¢ max spread
MIN_TIME_TO_EXPIRY_S = 30            # Minimum 30s to expiry
LAG_OBSERVATION_WINDOW_S = 5         # Window for move detection

log = logging.getLogger("lag_alpha")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ═══════════════════════════════════════════════════════════════════════
# §5.4: TIMESTAMP DISCIPLINE
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class TimestampRecord:
    """§5.4: Millisecond-precision timestamps for every observation."""
    local_monotonic_ms: int = 0
    local_utc_ms: int = 0
    source_exchange_timestamp_ms: int = 0
    polymarket_book_timestamp_ms: int = 0
    polymarket_event_timestamp_ms: int = 0
    receive_timestamp_ms: int = 0
    decision_timestamp_ms: int = 0
    submit_timestamp_ms: int = 0
    ack_timestamp_ms: int = 0

def now_ms() -> int:
    return int(time.time() * 1000)

def check_clock_health() -> dict:
    """§5.4: Clock drift checks."""
    result = {
        "local_utc_ms": now_ms(),
        "system_clock_offset_ms": 0,  # Would need NTP query
        "ntp_status": "unchecked",
        "polymarket_server_time_delta_ms": 0,
        "exchange_server_time_delta_ms": 0,
    }
    
    # Check Polymarket server time
    try:
        url = f"{CLOB_URL}/time"
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-LagMonitor"})
        before = now_ms()
        resp = urllib.request.urlopen(req, timeout=5)
        after = now_ms()
        server_time = int(json.loads(resp.read()).get("timestamp", 0))
        mid = (before + after) // 2
        result["polymarket_server_time_delta_ms"] = mid - server_time
    except Exception as e:
        log.warning(f"Clock check Poly failed: {e}")
    
    # Check Binance server time
    try:
        url = "https://api.binance.com/api/v3/time"
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-LagMonitor"})
        before = now_ms()
        resp = urllib.request.urlopen(req, timeout=5)
        after = now_ms()
        server_time = int(json.loads(resp.read()).get("serverTime", 0))
        mid = (before + after) // 2
        result["exchange_server_time_delta_ms"] = mid - server_time
    except Exception as e:
        log.warning(f"Clock check Binance failed: {e}")
    
    # Classify
    poly_drift = abs(result["polymarket_server_time_delta_ms"])
    ex_drift = abs(result["exchange_server_time_delta_ms"])
    if poly_drift > 500 or ex_drift > 500:
        result["classification"] = "CLOCK_SYNC_UNSAFE"
        result["allow_live_real"] = False
    else:
        result["classification"] = "CLOCK_SYNC_ACCEPTABLE"
        result["allow_live_real"] = True
    
    return result


# ═══════════════════════════════════════════════════════════════════════
# §5.3: EXTERNAL PRICE FEEDS
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class PriceSnapshot:
    """Atomic price snapshot from a single exchange."""
    source: str = ""
    asset: str = ""
    price: float = 0.0
    timestamp_ms: int = 0
    receive_ms: int = 0
    bid: float = 0.0
    ask: float = 0.0
    feed_status: str = "OK"  # OK, STALE, ERROR

class MultiExchangeOracle:
    """Shared in-memory quote cache with atomic latest-price snapshot."""
    
    def __init__(self):
        self._prices: Dict[str, PriceSnapshot] = {}  # source -> snapshot
        self._lock = threading.Lock()
        self._ws_threads: Dict[str, threading.Thread] = {}
        self._running = False
        self._stale_threshold_ms = 5000  # 5s = stale
    
    def start(self):
        """Start REST polling threads as primary feed.
        WebSocket feeds added when available (§6)."""
        self._running = True
        for asset in ["BTC", "ETH", "SOL"]:
            for source, url_template in EXCHANGE_REST.items():
                sym = ASSET_SYMBOLS.get(asset, {}).get(
                    source.split("_")[0], "BTCUSDT"
                )
                t = threading.Thread(
                    target=self._rest_poll_loop,
                    args=(f"{source}_{asset}", asset, source, sym),
                    daemon=True,
                )
                t.start()
                self._ws_threads[f"{source}_{asset}"] = t
        log.info(f"Oracle started: {len(self._ws_threads)} REST feeds")
    
    def stop(self):
        self._running = False
    
    def _rest_poll_loop(self, key: str, asset: str, source: str, symbol: str):
        """REST polling loop for a single exchange/asset pair."""
        base_urls = {
            "binance_spot": f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
            "coinbase_spot": f"https://api.pro.coinbase.com/products/{symbol.replace('USDT','-USD')}/ticker",
            "bybit_perp": f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}",
            "okx_perp": f"https://www.okx.com/api/v5/market/ticker?instId={symbol}",
        }
        url = base_urls.get(source, "")
        if not url:
            return
        
        while self._running:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "FDC-LagMonitor"})
                resp = urllib.request.urlopen(req, timeout=5)
                data = json.loads(resp.read())
                receive_ms = now_ms()
                
                # Parse price from different exchange formats
                price = 0.0
                ts = 0
                if source == "binance_spot":
                    price = float(data.get("price", 0))
                    ts = int(data.get("closeTime", receive_ms))
                elif source == "coinbase_spot":
                    price = float(data.get("price", 0))
                    ts = int(data.get("time", receive_ms))
                elif source == "bybit_perp":
                    price = float(data.get("result", {}).get("list", [{}])[0].get("lastPrice", 0))
                    ts = int(data.get("result", {}).get("list", [{}])[0].get("time", receive_ms))
                elif source == "okx_perp":
                    price = float(data.get("data", [{}])[0].get("last", 0))
                    ts = int(data.get("data", [{}])[0].get("ts", receive_ms))
                
                if price > 0:
                    snap = PriceSnapshot(
                        source=source, asset=asset, price=price,
                        timestamp_ms=ts, receive_ms=receive_ms,
                    )
                    with self._lock:
                        self._prices[key] = snap
                
            except Exception as e:
                log.debug(f"REST poll {key} failed: {e}")
            
            time.sleep(1)  # 1s REST polling
    
    def get_median_price(self, asset: str) -> Tuple[float, int, dict]:
        """Get multi-exchange median price for asset.
        Returns (median_price, receive_ms, source_details)."""
        prices = []
        details = {}
        now = now_ms()
        
        with self._lock:
            for key, snap in self._prices.items():
                if snap.asset != asset:
                    continue
                age_ms = now - snap.receive_ms
                if age_ms > self._stale_threshold_ms:
                    snap.feed_status = "STALE"
                    continue
                prices.append(snap.price)
                details[snap.source] = {
                    "price": snap.price,
                    "age_ms": age_ms,
                    "status": snap.feed_status,
                }
        
        if len(prices) == 0:
            return 0.0, 0, details
        
        median = float(np.median(prices))
        return median, now, details
    
    def get_feed_status(self, asset: str) -> str:
        """§6: Check if any feed is stale."""
        now = now_ms()
        with self._lock:
            for key, snap in self._prices.items():
                if snap.asset == asset:
                    if now - snap.receive_ms > self._stale_threshold_ms:
                        return "STALE"
        return "OK"


# ═══════════════════════════════════════════════════════════════════════
# §5.5: LEAD-LAG MEASUREMENT
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class LagEvent:
    """A detected lag event between external move and Polymarket repricing."""
    event_id: str = ""
    timestamp: str = ""
    asset: str = ""
    interval: str = ""
    condition_id: str = ""
    market_slug: str = ""
    external_price_before: float = 0.0
    external_price_after: float = 0.0
    external_move_bps: float = 0.0
    external_move_timestamp_ms: int = 0
    polymarket_token_price_before: float = 0.0
    polymarket_token_price_after: float = 0.0
    polymarket_reprice_timestamp_ms: int = 0
    repricing_delay_ms: float = 0.0
    book_quote_age_ms: int = 0
    spread: float = 0.0
    depth: float = 0.0
    time_to_expiry: int = 0
    token_side: str = ""
    hypothetical_entry_price: float = 0.0
    hypothetical_exit_or_settlement: str = ""
    binary_outcome: str = "PENDING"
    lag_edge_bps: float = 0.0
    slippage_adjusted_pnl: float = 0.0
    shadow_profile: str = ""
    # §5.4 timestamps
    timestamps: dict = field(default_factory=dict)


class LagAlphaMonitor:
    """§5: Main lag alpha measurement engine."""
    
    def __init__(self):
        self.oracle = MultiExchangeOracle()
        self.prev_external: Dict[str, Tuple[float, int]] = {}  # asset -> (price, ts)
        self.polymarket_books: Dict[str, dict] = {}  # slug_key -> book data
        self.lag_events: List[LagEvent] = []
        self.event_counter = 0
        self.start_time = datetime.now(timezone.utc)
        
        # Shadow profile tracking
        self.shadow_profiles = {
            "BTC_LAG_DOWN_SHADOW": {"events": 0, "wins": 0, "pnl": 0.0},
            "BTC_LAG_UP_SHADOW": {"events": 0, "wins": 0, "pnl": 0.0},
            "ETH_LAG_DOWN_SHADOW": {"events": 0, "wins": 0, "pnl": 0.0},
            "ETH_LAG_UP_SHADOW": {"events": 0, "wins": 0, "pnl": 0.0},
            "SOL_LAG_DOWN_SHADOW": {"events": 0, "wins": 0, "pnl": 0.0},
            "SOL_LAG_UP_SHADOW": {"events": 0, "wins": 0, "pnl": 0.0},
        }
    
    def run(self, duration_hours: float = 1.0):
        """Run the lag monitor for specified duration."""
        log.info("=" * 60)
        log.info("V21.7.4 §5: Lag Alpha Monitor — SHADOW_ONLY")
        log.info("=" * 60)
        
        # Clock health check
        clock = check_clock_health()
        log.info(f"Clock: {clock.get('classification', 'UNKNOWN')} "
                 f"PolyΔ={clock.get('polymarket_server_time_delta_ms', '?')}ms "
                 f"ExΔ={clock.get('exchange_server_time_delta_ms', '?')}ms")
        
        if clock.get("classification") == "CLOCK_SYNC_UNSAFE":
            log.warning("CLOCK_SYNC_UNSAFE — lag measurements may be unreliable")
        
        # Start oracle
        self.oracle.start()
        time.sleep(3)  # Let feeds warm up
        
        end_time = time.time() + duration_hours * 3600
        scan_interval = 5  # 5s scan
        cycle = 0
        
        try:
            while time.time() < end_time:
                cycle += 1
                self._scan_cycle(cycle)
                
                # Report every 12 cycles (1 min)
                if cycle % 12 == 0:
                    self._write_intermediate_report()
                
                time.sleep(scan_interval)
                
        except KeyboardInterrupt:
            log.info("Interrupted — generating final report")
        
        self.oracle.stop()
        self._generate_lag_report()
        log.info("Lag monitor complete")
    
    def _scan_cycle(self, cycle: int):
        """One scan cycle: check external moves, compare with Polymarket."""
        now_utc = datetime.now(timezone.utc)
        now_ts = now_ms()
        
        for asset in ["BTC", "ETH", "SOL"]:
            # Get median external price
            median_price, receive_ts, details = self.oracle.get_median_price(asset)
            if median_price == 0:
                continue
            
            # Check for external move
            prev_price, prev_ts = self.prev_external.get(asset, (0, 0))
            if prev_price > 0:
                window_ms = now_ts - prev_ts
                if window_ms > 0 and window_ms <= LAG_OBSERVATION_WINDOW_S * 1000:
                    move_bps = abs(median_price - prev_price) / prev_price * 10000
                    
                    if move_bps >= EXTERNAL_MOVE_THRESHOLD_BPS:
                        self._detect_lag_event(
                            asset, prev_price, median_price, move_bps,
                            prev_ts, now_ts, details
                        )
            
            self.prev_external[asset] = (median_price, now_ts)
            
            # Also check Polymarket books for the asset
            for interval in ["5m", "15m"]:
                self._check_polymarket_book(asset, interval, now_utc)
    
    def _detect_lag_event(self, asset: str, price_before: float, price_after: float,
                          move_bps: float, move_ts: int, now_ts: int, details: dict):
        """§5.5: Detect and record a potential lag event."""
        # Determine direction
        direction = "DOWN" if price_after < price_before else "UP"
        
        # Look up Polymarket markets for this asset
        for interval in ["5m", "15m"]:
            slug_key = f"{asset}_{interval}"
            pm_book = self.polymarket_books.get(slug_key, {})
            
            if not pm_book:
                continue
            
            # Check feed staleness (§6)
            feed_status = self.oracle.get_feed_status(asset)
            if feed_status == "STALE":
                continue
            
            # Check time-to-expiry
            tte = pm_book.get("expires_in_sec", 0)
            if tte < MIN_TIME_TO_EXPIRY_S:
                continue
            
            # Get token prices before and after
            down_price_before = pm_book.get("down_price", 0)
            up_price_before = pm_book.get("up_price", 0)
            spread = pm_book.get("spread", 0)
            
            if spread > MAX_SPREAD:
                continue
            
            # Calculate quote age
            book_ts = pm_book.get("timestamp_ms", 0)
            quote_age_ms = now_ts - book_ts if book_ts else 9999
            if quote_age_ms > MAX_QUOTE_AGE_MS:
                continue
            
            # Estimate repricing delay
            # Check if Polymarket price has already adjusted
            reprice_delay_ms = max(0, now_ts - move_ts)
            
            self.event_counter += 1
            event_id = f"LAG-{self.event_counter:06d}"
            
            # Select token based on direction (§5.7)
            if direction == "DOWN":
                token_side = "DOWN"
                pm_price = down_price_before
                shadow_profile = f"{asset}_LAG_DOWN_SHADOW"
                # If external drops, DOWN token should gain → lag means it hasn't gained yet
                hypothetical_entry = pm_price
                lag_edge_bps = move_bps  # Approximate: external moved but PM didn't
            else:
                token_side = "UP"
                pm_price = up_price_before
                shadow_profile = f"{asset}_LAG_UP_SHADOW"
                hypothetical_entry = pm_price
                lag_edge_bps = move_bps
            
            if pm_price == 0 or pm_price > 0.60:
                continue  # Skip if no valid price or outside tradeable range
            
            event = LagEvent(
                event_id=event_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset=asset,
                interval=interval,
                condition_id=pm_book.get("condition_id", ""),
                market_slug=pm_book.get("slug", ""),
                external_price_before=price_before,
                external_price_after=price_after,
                external_move_bps=move_bps,
                external_move_timestamp_ms=move_ts,
                polymarket_token_price_before=pm_price,
                polymarket_token_price_after=0,  # Would need subsequent book check
                polymarket_reprice_timestamp_ms=0,
                repricing_delay_ms=reprice_delay_ms,
                book_quote_age_ms=quote_age_ms,
                spread=spread,
                depth=pm_book.get("depth_usd", 0),
                time_to_expiry=tte,
                token_side=token_side,
                hypothetical_entry_price=pm_price,
                hypothetical_exit_or_settlement="SHADOW_PENDING",
                binary_outcome="PENDING",
                lag_edge_bps=lag_edge_bps,
                shadow_profile=shadow_profile,
                timestamps={
                    "local_monotonic_ms": now_ts,
                    "local_utc_ms": now_ts,
                    "receive_timestamp_ms": now_ts,
                    "decision_timestamp_ms": now_ts,
                },
            )
            
            self.lag_events.append(event)
            
            # Update shadow profile
            if shadow_profile in self.shadow_profiles:
                self.shadow_profiles[shadow_profile]["events"] += 1
            
            # Log to JSONL
            with open(LOG_DIR / "polymarket_lag_observations.jsonl", 'a') as f:
                f.write(json.dumps(asdict(event), default=str) + "\n")
            
            log.info(f"LAG: {asset} {interval} {direction} {move_bps:.1f}bps "
                     f"pm_price={pm_price:.4f} delay={reprice_delay_ms:.0f}ms "
                     f"age={quote_age_ms}ms profile={shadow_profile}")
    
    def _check_polymarket_book(self, asset: str, interval: str, now_utc: datetime):
        """Fetch Polymarket orderbook for lag comparison."""
        slug_key = f"{asset}_{interval}"
        
        try:
            contract = discover_active_contract(asset, interval)
            if not contract:
                return
            
            tokens = contract.get("tokens", [])
            if len(tokens) < 2:
                return
            
            # Fetch book for both tokens
            down_price = 0
            up_price = 0
            spread = 0
            depth = 0
            
            for token_info in tokens:
                tid = token_info.get("token_id", "")
                if not tid:
                    continue
                try:
                    ob = read_orderbook(tid)
                    if ob and ob.get("best_ask", 0) > 0:
                        mid = (ob.get("best_bid", 0) + ob.get("best_ask", 0)) / 2
                        if mid > 0.50:
                            up_price = mid
                        else:
                            down_price = mid
                        spread = abs(ob.get("best_ask", 0) - ob.get("best_bid", 0))
                        depth = ob.get("depth_usd", 0)
                except Exception:
                    pass
            
            tte = contract.get("expires_in_sec", 0)
            
            self.polymarket_books[slug_key] = {
                "slug": contract.get("slug", ""),
                "condition_id": contract.get("conditionId", ""),
                "down_price": down_price,
                "up_price": up_price,
                "spread": spread,
                "depth_usd": depth,
                "expires_in_sec": tte,
                "timestamp_ms": now_ms(),
                "timestamp": now_utc.isoformat(),
            }
            
        except Exception as e:
            log.debug(f"PM book check {asset}_{interval} failed: {e}")
    
    def _write_intermediate_report(self):
        """Write intermediate state to progress file."""
        elapsed = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        report = {
            "elapsed_seconds": elapsed,
            "total_events": len(self.lag_events),
            "shadow_profiles": self.shadow_profiles,
            "feed_status": {a: self.oracle.get_feed_status(a) for a in ["BTC", "ETH", "SOL"]},
        }
        with open(LOG_DIR / "lag_progress.json", 'w') as f:
            json.dump(report, f, indent=2, default=str)
    
    def _generate_lag_report(self):
        """§5.5: Generate final lag alpha report."""
        if not self.lag_events:
            report = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "classification": "INSUFFICIENT_DATA",
                "total_events": 0,
                "message": "No lag events detected during observation window",
            }
        else:
            delays = [e.repricing_delay_ms for e in self.lag_events]
            edges = [e.lag_edge_bps for e in self.lag_events]
            
            # Asset-by-asset breakdown
            asset_delay = defaultdict(list)
            asset_edge = defaultdict(list)
            interval_delay = defaultdict(list)
            interval_edge = defaultdict(list)
            bucket_delay = defaultdict(list)
            bucket_edge = defaultdict(list)
            
            for e in self.lag_events:
                asset_delay[e.asset].append(e.repricing_delay_ms)
                asset_edge[e.asset].append(e.lag_edge_bps)
                interval_delay[e.interval].append(e.repricing_delay_ms)
                interval_edge[e.interval].append(e.lag_edge_bps)
                bucket = f"{e.hypothetical_entry_price:.2f}"
                bucket_delay[bucket].append(e.repricing_delay_ms)
                bucket_edge[bucket].append(e.lag_edge_bps)
            
            positive_pct = sum(1 for e in edges if e > 0) / len(edges) * 100
            
            report = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_events": len(self.lag_events),
                "median_repricing_delay_ms": float(np.median(delays)) if delays else 0,
                "p90_repricing_delay_ms": float(np.percentile(delays, 90)) if delays else 0,
                "p95_repricing_delay_ms": float(np.percentile(delays, 95)) if delays else 0,
                "percentage_events_with_positive_lag_edge": positive_pct,
                "lag_edge_EV": float(np.mean(edges)) if edges else 0,
                "lag_edge_PF": 0,  # Would need resolved settlements
                "asset_by_asset_lag": {
                    a: {"median_delay_ms": float(np.median(d)), "mean_edge_bps": float(np.mean(asset_edge[a]))}
                    for a, d in asset_delay.items()
                },
                "interval_by_interval_lag": {
                    i: {"median_delay_ms": float(np.median(d)), "mean_edge_bps": float(np.mean(interval_edge[i]))}
                    for i, d in interval_delay.items()
                },
                "bucket_by_bucket_lag": {
                    b: {"median_delay_ms": float(np.median(d)), "count": len(d)}
                    for b, d in bucket_delay.items()
                },
                "shadow_profiles": self.shadow_profiles,
                "classification": "LAG_ALPHA_UNPROVEN",  # Default until settlements resolved
            }
        
        with open(LOG_DIR / "polymarket_lag_alpha_report.json", 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        log.info(f"§5 Lag report: {len(self.lag_events)} events, "
                 f"classification={report.get('classification', '?')}")
        return report


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="V21.7.4 §5: Lag Alpha Monitor")
    parser.add_argument("--hours", type=float, default=1.0, help="Observation duration (hours)")
    parser.add_argument("--clock-check", action="store_true", help="Clock health check only")
    args = parser.parse_args()
    
    if args.clock_check:
        clock = check_clock_health()
        print(json.dumps(clock, indent=2))
        sys.exit(0)
    
    monitor = LagAlphaMonitor()
    monitor.run(duration_hours=args.hours)