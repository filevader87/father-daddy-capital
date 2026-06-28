#!/usr/bin/env python3
"""
FDC Polymarket WebSocket Adapter — Synchronous
Thin sync wrapper around fdc_pm_websocket.py.
Runs WebSocket in background thread, exposes polling interface
for pm_engine.py's synchronous loop.

Usage:
    from fdc_pm_websocket_sync import PMBookFeed
    feed = PMBookFeed()
    feed.subscribe(["0xabc123...", "0xdef456..."])
    # ... later, in scan loop:
    book = feed.get_book("0xabc123...")
    # book = {'best_bid': 0.48, 'best_ask': 0.52, 'mid_price': 0.50, ...}

Author: Hugh (3rd of 5)
Date: 2026-05-15
"""

from __future__ import annotations
import threading
import asyncio
import time
import logging
from typing import Optional, List, Dict
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Thread-safe book state (minimal — just the numbers pm_engine cares about)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SyncBook:
    """Snapshot of what pm_engine.py needs from an orderbook."""
    best_bid: Optional[float] = None
    best_bid_size: Optional[float] = None
    best_ask: Optional[float] = None
    best_ask_size: Optional[float] = None
    mid_price: Optional[float] = None
    spread: Optional[float] = None
    bids: Dict[float, float] = None
    asks: Dict[float, float] = None
    last_update: float = 0.0
    tick_size: str = "0.01"

    def __post_init__(self):
        if self.bids is None:
            self.bids = {}
        if self.asks is None:
            self.asks = {}

    def to_dict(self) -> dict:
        return {
            "best_bid": self.best_bid,
            "best_bid_size": self.best_bid_size,
            "best_ask": self.best_ask,
            "best_ask_size": self.best_ask_size,
            "mid_price": self.mid_price,
            "spread": self.spread,
            "bids": self.bids.copy(),
            "asks": self.asks.copy(),
            "tick_size": self.tick_size,
            "last_update": self.last_update,
            "age_seconds": round(time.time() - self.last_update, 1),
        }


class PMBookFeed:
    """
    Synchronous Polymarket orderbook feed.

    Starts a background thread that connects to the Polymarket Market WebSocket
    and maintains live orderbook state. The main thread polls get_book().

    Falls back gracefully: if websockets not installed or connection fails,
    get_book() returns None without crashing. pm_engine.py already handles
    None gracefully by skipping orderbook-aware logic.
    """

    def __init__(self):
        self._books: Dict[str, SyncBook] = {}
        self._lock = threading.Lock()
        self._token_ids: List[str] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._connected = False
        self._available = False

        # Check if websockets is available
        try:
            import websockets
            self._available = True
        except ImportError:
            log.warning("websockets not installed — WebSocket feed disabled")
            self._available = False

    # ── Public API ──

    def subscribe(self, token_ids: List[str]):
        """Register token IDs to track. Starts background thread."""
        if not self._available:
            log.warning("WebSocket unavailable — falling back to REST polling")
            return

        self._token_ids = list(set(token_ids))
        for tid in self._token_ids:
            if tid not in self._books:
                self._books[tid] = SyncBook()

        self._running = True
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

    def get_book(self, asset_id: str) -> Optional[dict]:
        """Get current orderbook snapshot for a token. Thread-safe."""
        with self._lock:
            book = self._books.get(asset_id)
            if book is None:
                return None
            return book.to_dict()

    def get_books(self) -> Dict[str, dict]:
        """Get all tracked orderbooks. Thread-safe."""
        with self._lock:
            return {k: v.to_dict() for k, v in self._books.items()}

    def is_connected(self) -> bool:
        return self._connected

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Block until first book snapshot arrives or timeout."""
        return self._ready.wait(timeout=timeout)

    def shutdown(self):
        """Graceful shutdown of background thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    # ── Background asyncio event loop ──

    def _run_event_loop(self):
        """Runs in background daemon thread. Manages the asyncio WebSocket."""
        try:
            asyncio.run(self._async_main())
        except Exception as e:
            log.error(f"WebSocket thread crashed: {e}")
            self._connected = False

    async def _async_main(self):
        """Async main: connect, process events, update sync books."""
        import websockets

        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

        while self._running:
            try:
                ws = await websockets.connect(url)
                log.info(f"WebSocket connected. Subscribing to {len(self._token_ids)} tokens.")

                # Subscribe
                await ws.send(
                    '{"type":"market","assets_ids":[' +
                    ','.join(f'"{tid}"' for tid in self._token_ids) +
                    '],"custom_feature_enabled":true}'
                )

                self._connected = True
                first_book = False

                # Read loop
                while self._running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        # Send ping
                        if self._running and ws:
                            try:
                                await ws.send("PING")
                            except Exception:
                                break
                        continue

                    if raw == "PONG":
                        continue

                    try:
                        import json
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    event_type = msg.get("event_type")
                    asset_id = msg.get("asset_id", "")
                    market = msg.get("market", "")

                    # ── Parse and update sync books ──
                    if event_type == "book":
                        self._update_book(
                            asset_id,
                            bids={float(e["price"]): float(e["size"]) for e in msg.get("bids", [])},
                            asks={float(e["price"]): float(e["size"]) for e in msg.get("asks", [])},
                        )
                        if not first_book:
                            first_book = True
                            self._ready.set()

                    elif event_type == "price_change":
                        for pc in msg.get("price_changes", []):
                            pc_asset = pc.get("asset_id", asset_id)
                            side = pc.get("side", "")
                            price = float(pc.get("price", 0))
                            size = float(pc.get("size", 0))
                            self._apply_price_change(pc_asset, side, price, size)

                    elif event_type == "last_trade_price":
                        # Just note activity — book state is sufficient
                        pass

                    elif event_type == "tick_size_change":
                        new_tick = msg.get("new_tick_size", "0.001")
                        with self._lock:
                            book = self._books.get(asset_id)
                            if book:
                                book.tick_size = new_tick

                # Outer while loop: reconnect
                await ws.close()

            except Exception as e:
                log.error(f"WebSocket error: {e}. Reconnecting in 5s...")
                self._connected = False
                if self._running:
                    await asyncio.sleep(5)

    # ── Thread-safe book updates ──

    def _update_book(self, asset_id: str, bids: Dict[float, float], asks: Dict[float, float]):
        with self._lock:
            book = self._books.get(asset_id)
            if book is None:
                book = SyncBook()
                self._books[asset_id] = book

            book.bids = bids
            book.asks = asks
            book.last_update = time.time()

            if bids and asks:
                book.best_bid = max(bids.keys())
                book.best_bid_size = bids[book.best_bid]
                book.best_ask = min(asks.keys())
                book.best_ask_size = asks[book.best_ask]
                book.mid_price = (book.best_bid + book.best_ask) / 2.0
                book.spread = book.best_ask - book.best_bid
            else:
                book.best_bid = None
                book.best_ask = None
                book.mid_price = None
                book.spread = None

    def _apply_price_change(self, asset_id: str, side: str, price: float, size: float):
        with self._lock:
            book = self._books.get(asset_id)
            if book is None:
                book = SyncBook()
                self._books[asset_id] = book

            book.last_update = time.time()
            book_store = book.bids if side == "BUY" else book.asks

            if size == 0:
                book_store.pop(price, None)
            else:
                book_store[price] = size

            # Recalc
            if book.bids and book.asks:
                book.best_bid = max(book.bids.keys())
                book.best_bid_size = book.bids[book.best_bid]
                book.best_ask = min(book.asks.keys())
                book.best_ask_size = book.asks[book.best_ask]
                book.mid_price = (book.best_bid + book.best_ask) / 2.0
                book.spread = book.best_ask - book.best_bid


# ══════════════════════════════════════════════════════════════════════════════
# Module-level singleton (shared across all pm_engine.py invocations)
# ══════════════════════════════════════════════════════════════════════════════

_feed: Optional[PMBookFeed] = None
_ws_lock = threading.Lock()

def get_feed(token_ids: Optional[List[str]] = None) -> Optional[PMBookFeed]:
    """
    Get or create the global WebSocket feed singleton.
    
    First call starts the background thread. Subsequent calls return
    the running instance. If token_ids changes, resubscribe.
    """
    global _feed

    with _ws_lock:
        if _feed is None:
            _feed = PMBookFeed()
            if token_ids:
                _feed.subscribe(token_ids)
        elif token_ids:
            current = set(_feed._token_ids)
            new = set(token_ids)
            if new - current:
                # New tokens to track
                _feed._token_ids = list(current | new)
                # Background thread will pick them up on reconnect
    
    return _feed


def shutdown_feed():
    """Shutdown the global feed."""
    global _feed
    with _ws_lock:
        if _feed:
            _feed.shutdown()
            _feed = None


# ─── Quick Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== WebSocket Sync Adapter Test ===\n")

    # Test with a dummy token (won't get real data but tests lifecycle)
    token = "0x0000000000000000000000000000000000000000000000000000000000000000"
    feed = PMBookFeed()
    feed.subscribe([token])

    print(f"Connected: {feed.is_connected()}")
    print(f"Waiting for first book (5s timeout)...")
    ready = feed.wait_ready(timeout=5)
    print(f"Ready: {ready}")

    book = feed.get_book(token)
    if book:
        print(f"Book age: {book['age_seconds']}s")
        print(f"Mid: {book['mid_price']}")
    else:
        print("No book data (expected with dummy token)")

    print("\nShutting down...")
    feed.shutdown()
    print("Done.")
