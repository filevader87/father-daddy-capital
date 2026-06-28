#!/usr/bin/env python3
"""
V21.7.33 — Live Quote Cache (In-Memory)
========================================
In-memory quote cache for hot-path optimization.
Canary reads from this cache, NOT from synchronous discovery.

Design:
- Atomic update via lock-free reads (single-writer pattern)
- QuoteSnapshot dataclass for minimal hot-path object
- Preloaded current + next market tokens
- Previous active market retained until resolved
- Non-blocking read path for scanner/executor
- Async journal writer for historical data
"""

import json
import time
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict, field
from typing import Dict, Optional, Any, List
from collections import deque

log = logging.getLogger('live_quote_cache')

PROJECT_ROOT = Path("/home/naq1987s/father-daddy-capital")
OUT_DIR = PROJECT_ROOT / "output" / "v21733_feed_hotpath_optimizer"

# ─── QuoteSnapshot — Minimal Hot-Path Data Object (Section 8) ───

@dataclass
class QuoteSnapshot:
    """Minimal quote data for hot-path consumption.
    No raw book, no Gamma response, no CLOB response, no verbose logs.
    V21.7.34: Added identity fields (condition_id, window, expiry, identity_valid)."""
    asset: str
    interval: str
    side: str
    market_slug: str
    condition_id: str
    token_id: str
    best_bid: float
    best_ask: float
    spread: float
    quote_source: str
    quote_age_ms: int
    time_to_expiry: int
    book_valid: bool
    updated_at: str
    zone: str = ""
    # V21.7.34 identity fields
    up_token_id: str = ""
    expiry_timestamp: int = 0
    identity_valid: bool = False
    identity_source: str = ""
    is_current_window: bool = False

    def is_live_eligible(self) -> bool:
        """Check if quote source is live-eligible (PM_WS_BOOK, PM_WS_BEST_BID_ASK, PM_CLOB_READ)."""
        return self.quote_source in {"PM_WS_BOOK", "PM_WS_BEST_BID_ASK", "PM_CLOB_READ"}

    def is_fresh(self, max_age_ms: int = 5000) -> bool:
        """Check if quote is within freshness threshold."""
        return self.quote_age_ms < max_age_ms

    def to_canary_order_intent(self, size_usd: float = 5.0, order_type: str = "FAK") -> dict:
        """Convert to CanaryOrderIntent dict for order submission path."""
        import uuid
        return {
            "timestamp": self.updated_at,
            "asset": self.asset,
            "interval": self.interval,
            "side": self.side,
            "market_slug": self.market_slug,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "best_ask": self.best_ask,
            "best_bid": self.best_bid,
            "spread": self.spread,
            "quote_age_ms": self.quote_age_ms,
            "time_to_expiry": self.time_to_expiry,
            "size_usd": size_usd,
            "order_type": order_type,
            "limit_price": self.best_ask,
            "risk_snapshot_id": f"rcs-{uuid.uuid4().hex[:8]}",
        }


# ─── Ring Buffer for Hot-Path Logging (Section 9) ───

class RingBuffer:
    """Lock-free ring buffer for hot-path event logging.
    No synchronous file writes before order submission."""

    def __init__(self, capacity: int = 1024):
        self._buffer: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def push(self, event: dict):
        """Push event to ring buffer. Non-blocking."""
        with self._lock:
            self._buffer.append(event)

    def snapshot(self) -> List[dict]:
        """Take a snapshot of current events. Non-blocking."""
        with self._lock:
            return list(self._buffer)

    def clear(self):
        """Clear buffer."""
        with self._lock:
            self._buffer.clear()


# ─── Async Journal Writer ───

class AsyncJournalWriter:
    """Async file writer for journal entries.
    Writes are batched and flushed periodically.
    NEVER blocks the order path."""

    def __init__(self, output_path: Path, flush_interval_s: float = 5.0):
        self._output_path = output_path
        self._flush_interval = flush_interval_s
        self._queue: deque = deque(maxlen=10000)
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start background flush thread."""
        self._running = True
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()
        log.info(f"AsyncJournalWriter started: {self._output_path}")

    def stop(self):
        """Stop background flush thread and flush remaining."""
        self._running = False
        self._flush()
        if self._thread:
            self._thread.join(timeout=5.0)

    def write(self, entry: dict):
        """Queue an entry for async writing. Non-blocking."""
        with self._lock:
            self._queue.append(entry)

    def _flush_loop(self):
        """Background flush loop."""
        while self._running:
            time.sleep(self._flush_interval)
            self._flush()

    def _flush(self):
        """Flush queued entries to file."""
        with self._lock:
            if not self._queue:
                return
            entries = list(self._queue)
            self._queue.clear()

        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._output_path, "a") as f:
                for entry in entries:
                    f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            log.error(f"Journal flush error: {e}")


# ─── Live Quote Cache ───

class LiveQuoteCache:
    """In-memory quote cache for hot-path optimization.
    
    Design principles:
    - Single writer (update from feed_ingestion process)
    - Multiple readers (scanner, executor read concurrently)
    - Atomic updates via threading.Lock
    - Non-blocking reads via copy-on-read
    - Preloaded current + next market tokens
    - Previous active market retained until resolved
    
    Usage:
        cache = LiveQuoteCache()
        # Writer (feed_ingestion):
        cache.update(quote_snapshot)
        # Reader (scanner/executor):
        quote = cache.get_latest_quote("BTC", "15m", "DOWN")
    """

    LIVE_QUOTE_SOURCES = {"PM_WS_BOOK", "PM_WS_BEST_BID_ASK", "PM_CLOB_READ"}

    def __init__(self):
        self._quotes: Dict[str, QuoteSnapshot] = {}  # key: "{asset}_{interval}_{side}"
        self._by_token: Dict[str, QuoteSnapshot] = {}  # key: token_id
        self._by_slug: Dict[str, QuoteSnapshot] = {}  # key: market_slug + "_" + side
        self._lock = threading.RLock()
        self._update_count = 0
        self._last_update_at: Optional[str] = None
        self._previous_markets: Dict[str, QuoteSnapshot] = {}  # retained until resolved
        self._event_buffer = RingBuffer(capacity=2048)
        self._journal = AsyncJournalWriter(
            OUT_DIR / "quote_cache_events.jsonl",
            flush_interval_s=5.0,
        )
        self._journal_started = False

    def start_journal(self):
        """Start async journal writer."""
        if not self._journal_started:
            self._journal.start()
            self._journal_started = True

    def stop_journal(self):
        """Stop async journal writer."""
        if self._journal_started:
            self._journal.stop()
            self._journal_started = False

    def update(self, snapshot: QuoteSnapshot):
        """Update cache with new quote snapshot. Atomic write."""
        with self._lock:
            key = f"{snapshot.asset}_{snapshot.interval}_{snapshot.side}"
            self._quotes[key] = snapshot
            if snapshot.token_id:
                self._by_token[snapshot.token_id] = snapshot
            slug_key = f"{snapshot.market_slug}_{snapshot.side}"
            self._by_slug[slug_key] = snapshot
            self._update_count += 1
            self._last_update_at = snapshot.updated_at

        # Ring buffer event (non-blocking)
        self._event_buffer.push({
            "ts": snapshot.updated_at,
            "key": key,
            "ask": snapshot.best_ask,
            "source": snapshot.quote_source,
            "zone": snapshot.zone,
        })

        # Async journal write (non-blocking)
        self._journal.write(asdict(snapshot))

    def retain_previous_market(self, slug: str, side: str, snapshot: QuoteSnapshot):
        """Retain previous active market quote until resolved.
        Called during market rotation to preserve last known quote."""
        key = f"{slug}_{side}"
        self._previous_markets[key] = snapshot

    def get_latest_quote(self, asset: str, interval: str, side: str) -> Optional[QuoteSnapshot]:
        """Get latest quote by asset/interval/side. Non-blocking read."""
        key = f"{asset}_{interval}_{side}"
        with self._lock:
            return self._quotes.get(key)

    def get_by_token(self, token_id: str) -> Optional[QuoteSnapshot]:
        """Get latest quote by token_id. Non-blocking read."""
        with self._lock:
            return self._by_token.get(token_id)

    def get_by_slug(self, slug: str, side: str) -> Optional[QuoteSnapshot]:
        """Get latest quote by market slug and side. Non-blocking read."""
        key = f"{slug}_{side}"
        with self._lock:
            return self._by_slug.get(key)

    def get_previous_market(self, slug: str, side: str) -> Optional[QuoteSnapshot]:
        """Get retained previous market quote."""
        key = f"{slug}_{side}"
        return self._previous_markets.get(key)

    def get_canary_quote(self) -> Optional[QuoteSnapshot]:
        """Get BTC 15m DOWN quote for canary. Non-blocking read."""
        return self.get_latest_quote("BTC", "15m", "DOWN")

    def is_canary_live_eligible(self, max_age_ms: int = 5000) -> bool:
        """Check if canary quote is live-eligible and fresh."""
        quote = self.get_canary_quote()
        if quote is None:
            return False
        return quote.is_live_eligible() and quote.is_fresh(max_age_ms)

    def preload_markets(self, markets: List[dict], quotes: Dict[str, dict]):
        """Preload current + next market tokens into cache."""
        from v21726_scanner_bridge import classify_zone

        for m in markets:
            asset = m.get("asset", "")
            interval = m.get("interval", "")
            slug = m.get("slug", "")
            condition_id = m.get("condition_id", "")

            for side, tid_key in [("DOWN", "down_token_id"), ("UP", "up_token_id")]:
                tid = m.get(tid_key, "")
                q = quotes.get(tid, {})
                if not q:
                    continue

                ask = q.get("best_ask", 0)
                zone = classify_zone(ask) if ask else "UNKNOWN"

                snapshot = QuoteSnapshot(
                    asset=asset,
                    interval=interval,
                    side=side,
                    market_slug=slug,
                    condition_id=condition_id,
                    token_id=tid,
                    best_bid=q.get("best_bid", 0),
                    best_ask=ask,
                    spread=q.get("spread", 0),
                    quote_source=q.get("price_source", "UNKNOWN"),
                    quote_age_ms=int(q.get("quote_age_ms", -1)),
                    time_to_expiry=m.get("tte", 0),
                    book_valid=q.get("is_valid", False),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    zone=zone,
                    up_token_id=m.get("up_token_id", "") if side == "DOWN" else (tid if side == "UP" else ""),
                    expiry_timestamp=m.get("expiry_ts", 0),
                    identity_valid=bool(condition_id and tid),
                    identity_source="scanner_bridge_discover_all_markets",
                    is_current_window=m.get("tte", 0) > 0 and m.get("tte", 0) <= 900,
                )
                self.update(snapshot)

    def stats(self) -> dict:
        """Cache statistics."""
        with self._lock:
            return {
                "total_quotes": len(self._quotes),
                "total_tokens": len(self._by_token),
                "total_slugs": len(self._by_slug),
                "previous_markets": len(self._previous_markets),
                "update_count": self._update_count,
                "last_update_at": self._last_update_at,
                "live_eligible_sources": list(self.LIVE_QUOTE_SOURCES),
            }

    def get_all_zones(self) -> Dict[str, str]:
        """Get current zone for all cached quotes."""
        with self._lock:
            return {k: v.zone for k, v in self._quotes.items()}


# ─── Module-level singleton ───

_cache_instance: Optional[LiveQuoteCache] = None
_cache_lock = threading.Lock()


def get_quote_cache() -> LiveQuoteCache:
    """Get or create the singleton LiveQuoteCache."""
    global _cache_instance
    with _cache_lock:
        if _cache_instance is None:
            _cache_instance = LiveQuoteCache()
        return _cache_instance


def close_quote_cache():
    """Stop journal writer on shutdown."""
    global _cache_instance
    if _cache_instance is not None:
        _cache_instance.stop_journal()