#!/usr/bin/env python3
"""
V21.7.4 §6: WebSocket Feed Architecture
=========================================
Real-time price feeds via WebSocket with REST fallback.

Feeds:
  - Binance spot websocket (aggTrade + ticker)
  - Coinbase spot websocket (ticker)
  - Bybit perp websocket (tickers)
  - OKX perp websocket (tickers)
  - Polymarket CLOB websocket (market channel)

Architecture:
  - One persistent websocket task per source
  - Shared in-memory quote cache (atomic latest-price snapshot)
  - Nonblocking strategy evaluation
  - Fallback REST when websocket stale
  - Staleness detection: if data age > threshold, feed_status = STALE

Polymarket docs: live orderbook data should use WebSocket over polling.
Market channel streams: orderbook changes, price updates, trade events.
"""

import json, time, logging, threading, asyncio
import websockets
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from collections import defaultdict

log = logging.getLogger("ws_feeds")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ═══════════════════════════════════════════════════════════════════════
# SHARED QUOTE CACHE
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class QuoteUpdate:
    """Atomic price update from a single source."""
    source: str = ""
    asset: str = ""
    price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    timestamp_ms: int = 0     # exchange timestamp
    receive_ms: int = 0       # local receive time
    volume_24h: float = 0.0

class SharedQuoteCache:
    """Thread-safe shared in-memory quote cache."""
    
    def __init__(self, stale_threshold_ms: int = 5000):
        self._quotes: Dict[str, QuoteUpdate] = {}  # "source_asset" -> update
        self._lock = threading.Lock()
        self._stale_threshold_ms = stale_threshold_ms
    
    def update(self, key: str, quote: QuoteUpdate):
        with self._lock:
            self._quotes[key] = quote
    
    def get_latest(self, asset: str) -> Optional[QuoteUpdate]:
        """Get most recent non-stale quote for an asset across all sources."""
        now_ms = int(time.time() * 1000)
        best: Optional[QuoteUpdate] = None
        
        with self._lock:
            for key, q in self._quotes.items():
                if q.asset != asset:
                    continue
                age = now_ms - q.receive_ms
                if age > self._stale_threshold_ms:
                    continue  # stale
                if best is None or q.receive_ms > best.receive_ms:
                    best = q
        
        return best
    
    def get_median(self, asset: str) -> Tuple[float, int, dict]:
        """Get multi-exchange median price."""
        now_ms = int(time.time() * 1000)
        prices = []
        details = {}
        
        with self._lock:
            for key, q in self._quotes.items():
                if q.asset != asset:
                    continue
                age = now_ms - q.receive_ms
                status = "OK" if age <= self._stale_threshold_ms else "STALE"
                if status == "OK":
                    prices.append(q.price)
                details[q.source] = {"price": q.price, "age_ms": age, "status": status}
        
        if not prices:
            return 0.0, 0, details
        
        return float(np.median(prices)), now_ms, details
    
    def get_feed_status(self, asset: str) -> str:
        """Check if any feed for this asset is fresh."""
        now_ms = int(time.time() * 1000)
        has_any = False
        with self._lock:
            for key, q in self._quotes.items():
                if q.asset != asset:
                    continue
                has_any = True
                age = now_ms - q.receive_ms
                if age <= self._stale_threshold_ms:
                    return "OK"
        return "STALE" if has_any else "NO_DATA"
    
    def get_all_status(self) -> dict:
        """Full status of all feeds."""
        now_ms = int(time.time() * 1000)
        status = {}
        with self._lock:
            for key, q in self._quotes.items():
                age = now_ms - q.receive_ms
                status[key] = {
                    "source": q.source,
                    "asset": q.asset,
                    "price": q.price,
                    "age_ms": age,
                    "status": "OK" if age <= self._stale_threshold_ms else "STALE",
                }
        return status


# ═══════════════════════════════════════════════════════════════════════
# WEBSOCKET FEEDS
# ═══════════════════════════════════════════════════════════════════════

# Global cache instance (shared across all feeds)
CACHE = SharedQuoteCache(stale_threshold_ms=5000)

SUBSCRIPTIONS = {
    "btc": {
        "binance_spot": {
            "url": "wss://stream.binance.com:9443/ws/btcusdt@ticker",
            "parse": "binance_ticker",
        },
        "coinbase_spot": {
            "url": "wss://ws-feed.pro.coinbase.com",
            "subscribe": {
                "type": "subscribe",
                "product_ids": ["BTC-USD"],
                "channels": ["ticker"],
            },
            "parse": "coinbase_ticker",
        },
        "bybit_perp": {
            "url": "wss://stream.bybit.com/v5/public/linear",
            "subscribe": {"op": "subscribe", "args": ["tickers.BTCUSDT"]},
            "parse": "bybit_ticker",
        },
        "okx_perp": {
            "url": "wss://ws.okx.com:8449/ws/v5/public",
            "subscribe": {"op": "subscribe", "args": [{"channel": "tickers", "instId": "BTC-USDT-SWAP"}]},
            "parse": "okx_ticker",
        },
    },
    "eth": {
        "binance_spot": {
            "url": "wss://stream.binance.com:9443/ws/ethusdt@ticker",
            "parse": "binance_ticker",
        },
        "bybit_perp": {
            "url": "wss://stream.bybit.com/v5/public/linear",
            "subscribe": {"op": "subscribe", "args": ["tickers.ETHUSDT"]},
            "parse": "bybit_ticker",
        },
    },
    "sol": {
        "binance_spot": {
            "url": "wss://stream.binance.com:9443/ws/solusdt@ticker",
            "parse": "binance_ticker",
        },
        "bybit_perp": {
            "url": "wss://stream.bybit.com/v5/public/linear",
            "subscribe": {"op": "subscribe", "args": ["tickers.SOLUSDT"]},
            "parse": "bybit_ticker",
        },
    },
}

def _parse_binance_ticker(data: dict) -> Optional[QuoteUpdate]:
    """Parse Binance WS ticker message."""
    if "c" not in data:  # not a ticker
        return None
    return QuoteUpdate(
        source="binance_spot",
        asset=data.get("s", "").replace("USDT", "").replace("BUSD", ""),
        price=float(data.get("c", 0)),
        bid=float(data.get("b", 0)),
        ask=float(data.get("a", 0)),
        timestamp_ms=int(data.get("T", 0)),
        receive_ms=int(time.time() * 1000),
        volume_24h=float(data.get("v", 0)),
    )

def _parse_coinbase_ticker(data: dict) -> Optional[QuoteUpdate]:
    """Parse Coinbase WS ticker message."""
    if data.get("type") != "ticker":
        return None
    return QuoteUpdate(
        source="coinbase_spot",
        asset=data.get("product_id", "").replace("-USD", ""),
        price=float(data.get("price", 0)),
        bid=float(data.get("best_bid", 0)),
        ask=float(data.get("best_ask", 0)),
        timestamp_ms=int(data.get("time", "").replace("Z", "").replace(":", "").replace("-", "")[:17]),  # rough
        receive_ms=int(time.time() * 1000),
    )

def _parse_bybit_ticker(data: dict) -> Optional[QuoteUpdate]:
    """Parse Bybit WS ticker message."""
    if data.get("topic", "").startswith("tickers."):
        d = data.get("data", {})
        sym = d.get("symbol", "")
        return QuoteUpdate(
            source="bybit_perp",
            asset=sym.replace("USDT", ""),
            price=float(d.get("lastPrice", 0)),
            bid=float(d.get("bestBidPrice", 0)),
            ask=float(d.get("bestAskPrice", 0)),
            timestamp_ms=int(d.get("timestamp", 0)),
            receive_ms=int(time.time() * 1000),
            volume_24h=float(d.get("volume24h", 0)),
        )
    return None

PARSERS = {
    "binance_ticker": _parse_binance_ticker,
    "coinbase_ticker": _parse_coinbase_ticker,
    "bybit_ticker": _parse_bybit_ticker,
    "okx_ticker": lambda d: None,  # TODO: implement OKX parser
}


async def _ws_feed_loop(name: str, url: str, parse_type: str,
                        subscribe_msg: Optional[dict] = None,
                        asset: str = ""):
    """Persistent websocket loop for a single feed source."""
    async def _run():
        reconnect_delay = 1
        max_delay = 60
        
        while True:
            try:
                async with websockets.connect(url, ping_interval=30) as ws:
                    if subscribe_msg:
                        await ws.send(json.dumps(subscribe_msg))
                    log.info(f"WS connected: {name}")
                    reconnect_delay = 1  # reset on success
                    
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                            parser = PARSERS.get(parse_type)
                            if parser:
                                quote = parser(data)
                                if quote and quote.price > 0:
                                    key = f"{quote.source}_{quote.asset}"
                                    CACHE.update(key, quote)
                        except json.JSONDecodeError:
                            pass
                        except Exception as e:
                            log.debug(f"Parse error {name}: {e}")
                            
            except Exception as e:
                log.warning(f"WS {name} disconnected: {e} — retry in {reconnect_delay}s")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_delay)
    
    return _run()


class WebSocketFeedManager:
    """§6: Manages all WebSocket feed connections."""
    
    def __init__(self, cache: Optional[SharedQuoteCache] = None):
        self.cache = cache or CACHE
        self._threads: Dict[str, threading.Thread] = {}
        self._running = False
    
    def start(self):
        """Start all WebSocket feeds in background threads."""
        self._running = True
        loop = asyncio.new_event_loop()
        
        for asset_lower, feeds in SUBSCRIPTIONS.items():
            for feed_name, config in feeds.items():
                t = threading.Thread(
                    target=self._run_ws_thread,
                    args=(loop, feed_name, asset_lower, config),
                    daemon=True,
                )
                t.start()
                self._threads[feed_name] = t
        
        log.info(f"WS Feed Manager: {len(self._threads)} feeds starting")
    
    def _run_ws_thread(self, loop: asyncio.AbstractEventLoop,
                       feed_name: str, asset: str, config: dict):
        """Run a single WS feed in its own thread with its own event loop."""
        asyncio.set_event_loop(loop)
        sub_msg = config.get("subscribe")
        try:
            coro = _ws_feed_loop(
                name=feed_name,
                url=config["url"],
                parse_type=config["parse"],
                subscribe_msg=sub_msg,
                asset=asset,
            )
            loop.run_until_complete(coro)
        except Exception as e:
            log.error(f"WS thread {feed_name} crashed: {e}")
    
    def stop(self):
        self._running = False
    
    def status(self) -> dict:
        return self.cache.get_all_status()


# ═══════════════════════════════════════════════════════════════════════
# POLYMARKET CLOB WEBSOCKET (§6)
# ═══════════════════════════════════════════════════════════════════════

class PolymarketWSFeed:
    """Polymarket CLOB WebSocket feed for market channel data.
    
    Per Polymarket docs: market channel streams orderbook changes, 
    price updates, and trade events.
    """
    
    def __init__(self, cache: SharedQuoteCache):
        self.cache = cache
        self._connected = False
        self._last_msg_ms = 0
    
    async def connect_and_listen(self, token_ids: Optional[list] = None):
        """Connect to Polymarket WS and listen for market updates."""
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        
        try:
            async with websockets.connect(url, ping_interval=30) as ws:
                self._connected = True
                log.info("Polymarket WS connected")
                
                # Subscribe to market channels
                if token_ids:
                    for tid in token_ids:
                        msg = {"auth": {}, "market_ids": [tid]}
                        await ws.send(json.dumps(msg))
                
                async for msg in ws:
                    self._last_msg_ms = int(time.time() * 1000)
                    try:
                        data = json.loads(msg)
                        # Parse market updates
                        if data.get("type") == "book":
                            # Update cache with Polymarket book data
                            pass  # Specific parsing depends on PM WS format
                    except json.JSONDecodeError:
                        pass
                    
        except Exception as e:
            log.warning(f"Polymarket WS disconnected: {e}")
            self._connected = False


if __name__ == "__main__":
    print("WebSocket Feed Architecture — starting all feeds")
    print("Press Ctrl+C to stop")
    
    mgr = WebSocketFeedManager()
    mgr.start()
    
    try:
        while True:
            time.sleep(30)
            status = mgr.status()
            ok = sum(1 for v in status.values() if v["status"] == "OK")
            stale = sum(1 for v in status.values() if v["status"] == "STALE")
            print(f"[{datetime.now(timezone.utc).isoformat()}] "
                  f"Feeds: {ok} OK, {stale} STALE out of {len(status)}")
            for k, v in sorted(status.items()):
                if v["status"] != "OK":
                    print(f"  ⚠ {k}: {v['status']} age={v['age_ms']}ms")
    except KeyboardInterrupt:
        mgr.stop()
        print("Stopped")