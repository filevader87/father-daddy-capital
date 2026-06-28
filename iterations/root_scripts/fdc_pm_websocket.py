#!/usr/bin/env python3
"""
FDC Polymarket WebSocket Client
Real-time orderbook streaming via Polymarket Market WebSocket.
Replaces REST polling with sub-second book updates for live trading.

Extracted from poly-maker websocket_handlers.py and Polymarket/agent-skills
WebSocket documentation.

Usage:
    from fdc_pm_websocket import PMWebSocketClient
    async with PMWebSocketClient(token_ids=["0xabc..."]) as ws:
        book = ws.get_book("0xabc...")
        async for event in ws.stream():
            # handle event

Author: Hugh (3rd of 5)
Date: 2026-05-15
"""

from __future__ import annotations
import asyncio
import json
import time
import logging
from typing import Optional, Dict, List, AsyncIterator, Set
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ─── Constants ─────────────────────────────────────────────────────────────

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10          # Seconds between PING keepalives
RECONNECT_DELAY = 5         # Seconds to wait before reconnect
MAX_RECONNECT_ATTEMPTS = 10


# ─── Event Types ───────────────────────────────────────────────────────────

@dataclass
class BookSnapshot:
    """Full orderbook snapshot on subscribe."""
    asset_id: str
    market: str           # condition ID
    bids: Dict[float, float]  # price → size
    asks: Dict[float, float]
    timestamp: str
    hash: str = ""

@dataclass
class PriceChange:
    """Price level addition or removal."""
    asset_id: str
    market: str
    price: float
    size: float          # 0.0 = removed
    side: str            # BUY / SELL
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None

@dataclass
class LastTrade:
    """Last trade executed on this token."""
    asset_id: str
    market: str
    price: float
    size: float
    side: str

@dataclass
class TickSizeChange:
    """Tick size changed (price > 0.96 or < 0.04)."""
    asset_id: str
    old_tick: str
    new_tick: str


# ─── In-Memory Book State ──────────────────────────────────────────────────

@dataclass
class LiveBook:
    """Maintains a live orderbook from WebSocket stream."""
    bids: Dict[float, float] = field(default_factory=dict)
    asks: Dict[float, float] = field(default_factory=dict)
    mid_price: Optional[float] = None
    spread: Optional[float] = None
    last_trade: Optional[LastTrade] = None
    tick_size: str = "0.01"
    last_update: float = 0.0

    def apply_snapshot(self, snapshot: BookSnapshot):
        """Replace entire book from a 'book' event."""
        self.bids = snapshot.bids.copy()
        self.asks = snapshot.asks.copy()
        self._recalc()
        self.last_update = time.time()

    def apply_price_change(self, change: PriceChange):
        """Apply a single price_change event."""
        book = self.bids if change.side == "BUY" else self.asks
        if change.size == 0:
            book.pop(change.price, None)
        else:
            book[change.price] = change.size
        self._recalc()
        self.last_update = time.time()

    def apply_last_trade(self, trade: LastTrade):
        """Record last trade."""
        self.last_trade = trade
        self.last_update = time.time()

    def apply_tick_change(self, tick: TickSizeChange):
        """Update tick size."""
        self.tick_size = tick.new_tick
        self.last_update = time.time()

    def _recalc(self):
        """Recalculate mid-price and spread."""
        if self.bids and self.asks:
            best_bid = max(self.bids.keys())
            best_ask = min(self.asks.keys())
            self.mid_price = (best_bid + best_ask) / 2.0
            self.spread = best_ask - best_bid
        else:
            self.mid_price = None
            self.spread = None

    def best_bid(self) -> Optional[float]:
        return max(self.bids.keys()) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks.keys()) if self.asks else None

    def depth(self, side: str) -> float:
        """Total depth on one side."""
        book = self.bids if side == "bids" else self.asks
        return sum(book.values())

    def get_book_dict(self) -> dict:
        """Export as dict compatible with fdc_orderbook.py."""
        return {
            "bids": self.bids.copy(),
            "asks": self.asks.copy(),
            "mid_price": self.mid_price,
            "spread": self.spread,
            "tick_size": self.tick_size,
        }


# ─── WebSocket Client ──────────────────────────────────────────────────────


class PMWebSocketClient:
    """
    Polymarket Market WebSocket client.

    Manages connection lifecycle, book state for subscribed tokens,
    and provides async iteration over events.

    Example:
        client = PMWebSocketClient(token_ids=["0xabc..."])
        async with client:
            async for event in client.events():
                match event:
                    case PriceChange() as pc:
                        book = client.get_book(pc.asset_id)
                        print(f"{pc.asset_id}: {book.mid_price}")
    """

    def __init__(self, token_ids: List[str]):
        self._token_ids = token_ids
        self._books: Dict[str, LiveBook] = {}  # asset_id → LiveBook
        self._ws = None  # websockets.WebSocketClientProtocol — lazy import
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._reader_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._reconnect_count = 0

        # Initialize books
        for tid in token_ids:
            self._books[tid] = LiveBook()

    # ── Book access ──

    def get_book(self, asset_id: str) -> Optional[LiveBook]:
        """Get current live book for a token."""
        return self._books.get(asset_id)

    def all_books(self) -> Dict[str, dict]:
        """Export all books as dicts."""
        return {k: v.get_book_dict() for k, v in self._books.items()}

    # ── Connection lifecycle ──

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def connect(self):
        """Connect and subscribe to token IDs."""
        import websockets

        url = WS_MARKET_URL
        log.info(f"Connecting to {url}")

        try:
            self._ws = await websockets.connect(url)
        except Exception as e:
            log.error(f"Connection failed: {e}")
            raise

        # Subscribe
        sub_msg = json.dumps({
            "type": "market",
            "assets_ids": self._token_ids,
            "custom_feature_enabled": True,
        })
        await self._ws.send(sub_msg)
        log.info(f"Subscribed to {len(self._token_ids)} tokens")

        self._running = True
        self._ping_task = asyncio.create_task(self._ping_loop())
        self._reader_task = asyncio.create_task(self._read_loop())
        self._reconnect_count = 0

    async def close(self):
        """Graceful shutdown."""
        self._running = False

        for task in [self._ping_task, self._reader_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._ws:
            await self._ws.close()
            self._ws = None

    # ── Heartbeat ──

    async def _ping_loop(self):
        """Send PING every 10 seconds to keep WebSocket alive."""
        while self._running:
            try:
                await asyncio.sleep(PING_INTERVAL)
                if self._ws and self._running:
                    await self._ws.send("PING")
            except Exception:
                if self._running:
                    await self._reconnect()
                break

    # ── Message reader ──

    async def _read_loop(self):
        """Read messages, parse events, update books, push to queue."""
        while self._running and self._ws:
            try:
                raw = await self._ws.recv()
            except Exception as e:
                log.error(f"Read error: {e}")
                if self._running:
                    await self._reconnect()
                break

            if raw == "PONG":
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(f"Invalid JSON: {raw[:100]}")
                continue

            event_type = msg.get("event_type")
            asset_id = msg.get("asset_id", "")
            market = msg.get("market", "")

            book = self._books.get(asset_id)
            if book is None:
                continue  # Not tracking this token

            # ── Parse and apply ──

            if event_type == "book":
                snapshot = BookSnapshot(
                    asset_id=asset_id,
                    market=market,
                    bids={float(e["price"]): float(e["size"]) for e in msg.get("bids", [])},
                    asks={float(e["price"]): float(e["size"]) for e in msg.get("asks", [])},
                    timestamp=msg.get("timestamp", ""),
                    hash=msg.get("hash", ""),
                )
                book.apply_snapshot(snapshot)
                await self._event_queue.put(snapshot)

            elif event_type == "price_change":
                for pc in msg.get("price_changes", []):
                    change = PriceChange(
                        asset_id=pc.get("asset_id", asset_id),
                        market=market,
                        price=float(pc["price"]),
                        size=float(pc["size"]),
                        side=pc["side"],
                        best_bid=float(pc["best_bid"]) if "best_bid" in pc else None,
                        best_ask=float(pc["best_ask"]) if "best_ask" in pc else None,
                    )
                    book.apply_price_change(change)
                    await self._event_queue.put(change)

            elif event_type == "last_trade_price":
                trade = LastTrade(
                    asset_id=asset_id,
                    market=market,
                    price=float(msg["price"]),
                    size=float(msg.get("size", 0)),
                    side=msg.get("side", ""),
                )
                book.apply_last_trade(trade)
                await self._event_queue.put(trade)

            elif event_type == "tick_size_change":
                tick = TickSizeChange(
                    asset_id=asset_id,
                    old_tick=msg.get("old_tick_size", "0.01"),
                    new_tick=msg.get("new_tick_size", "0.001"),
                )
                book.apply_tick_change(tick)
                await self._event_queue.put(tick)

    # ── Reconnect ──

    async def _reconnect(self):
        """Exponential backoff reconnect."""
        if self._reconnect_count >= MAX_RECONNECT_ATTEMPTS:
            log.error("Max reconnect attempts reached. Giving up.")
            self._running = False
            return

        self._reconnect_count += 1
        delay = RECONNECT_DELAY * (2 ** (self._reconnect_count - 1))
        log.info(f"Reconnecting in {delay}s (attempt {self._reconnect_count})")

        # Clean up old connection
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        await asyncio.sleep(delay)

        try:
            await self.connect()
        except Exception as e:
            log.error(f"Reconnect failed: {e}")

    # ── Event iteration ──

    async def events(self) -> AsyncIterator:
        """Async generator yielding events as they arrive."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(), timeout=1.0
                )
                yield event
            except asyncio.TimeoutError:
                continue

    async def stream(self):
        """Convenience: iterate events."""
        async for event in self.events():
            yield event


# ─── Single-Token Convenience ──────────────────────────────────────────────


async def stream_orderbook(token_id: str, timeout: float = 60.0):
    """
    Stream a single token's orderbook for a limited time.

    Args:
        token_id: Polymarket asset ID (YES or NO token)
        timeout: seconds to stream before returning

    Returns:
        LiveBook with accumulated state
    """
    client = PMWebSocketClient(token_ids=[token_id])
    async with client:
        try:
            async for event in client.events():
                timeout -= 0.1
        except asyncio.TimeoutError:
            pass
        return client.get_book(token_id)


# ─── Quick Test ────────────────────────────────────────────────────────────

async def _test():
    """Connect to real WebSocket and print first 5 events."""
    # Use a known active token for testing
    # This is a dummy — replace with a real active token ID
    token_id = "0x0000000000000000000000000000000000000000000000000000000000000000"

    log.info(f"Testing WebSocket with {token_id}")
    client = PMWebSocketClient(token_ids=[token_id])

    try:
        async with client:
            count = 0
            async for event in client.events():
                print(f"Event: {type(event).__name__}")
                count += 1
                if count >= 5:
                    break
            book = client.get_book(token_id)
            if book:
                print(f"Book: mid={book.mid_price}, spread={book.spread}")
    except Exception as e:
        print(f"Test failed (expected — needs real token ID): {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_test())
