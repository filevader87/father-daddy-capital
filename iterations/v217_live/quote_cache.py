#!/usr/bin/env python3
"""
V21.7.9 Quote Cache — §6
========================
In-memory shared atomic quote cache for WebSocket feed layer.

Nonblocking reads, atomic latest snapshots, per-source freshness,
per-asset median, per-token Polymarket book state, monotonic timestamps.
"""

import time, threading, json
from collections import defaultdict
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, List

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "v2179_ws"
OUT.mkdir(parents=True, exist_ok=True)


class QuoteCache:
    def __init__(self, stale_ms=2000, pm_stale_ms=15000):
        # External WS feeds: 2s threshold
        # PM REST books: 15s threshold (REST cycle ~10-12s for 16 markets)
        self._lock = threading.Lock()
        self._stale_ms = stale_ms
        self._pm_stale_ms = pm_stale_ms

        # External: asset -> source -> quote
        self._ext: Dict[str, Dict[str, dict]] = defaultdict(dict)
        # Polymarket: token_id -> book
        self._pm: Dict[str, dict] = {}
        # Condition -> token_ids
        self._condition_tokens: Dict[str, List[str]] = defaultdict(list)
        # Feed health counters
        self._health = dict(
            ext_connects=0, ext_disconnects=0,
            pm_connects=0, pm_disconnects=0,
            dropped=0, reconnects=0,
            start_time=time.time(),
        )

    # ── External feeds ──────────────────────────────────────

    def update_external(self, asset: str, source: str, quote: dict):
        """Update external exchange quote. Thread-safe."""
        now_ms = int(time.time() * 1000)
        quote["received_ms"] = now_ms
        quote["source"] = source
        quote["asset"] = asset
        with self._lock:
            self._ext[asset][source] = quote

    def get_external_snapshot(self, asset: str) -> Optional[dict]:
        """Nonblocking read of latest external quotes for an asset."""
        with self._lock:
            srcs = self._ext.get(asset, {})
            if not srcs:
                return None
            now_ms = int(time.time() * 1000)
            fresh = {}
            for src, q in srcs.items():
                age = now_ms - q.get("received_ms", 0)
                q["feed_age_ms"] = age
                q["stale"] = age > self._stale_ms
                fresh[src] = q
            return dict(asset=asset, sources=fresh, timestamp_ms=now_ms)

    def get_cross_exchange_median(self, asset: str) -> Optional[float]:
        """Compute median mid across fresh sources."""
        snap = self.get_external_snapshot(asset)
        if not snap:
            return None
        mids = [q["mid"] for q in snap["sources"].values()
                if not q.get("stale") and q.get("mid", 0) > 0]
        if not mids:
            return None
        mids.sort()
        n = len(mids)
        return mids[n // 2] if n % 2 else (mids[n // 2 - 1] + mids[n // 2]) / 2

    # ── Polymarket feeds ─────────────────────────────────────

    def update_polymarket(self, token_id: str, book: dict):
        """Update Polymarket orderbook state."""
        now_ms = int(time.time() * 1000)
        book["received_ms"] = now_ms
        with self._lock:
            self._pm[token_id] = book
            # Evict entries older than 60s (expired market windows)
            expired = [tid for tid, b in self._pm.items()
                       if now_ms - b.get("received_ms", 0) > 60000]
            for tid in expired:
                del self._pm[tid]

    def get_polymarket_snapshot(self, token_id: str) -> Optional[dict]:
        """Nonblocking read of latest Polymarket book."""
        with self._lock:
            q = self._pm.get(token_id)
            if not q:
                return None
            now_ms = int(time.time() * 1000)
            q["book_age_ms"] = now_ms - q.get("received_ms", 0)
            return dict(q)

    def register_condition(self, condition_id: str, token_ids: List[str]):
        with self._lock:
            self._condition_tokens[condition_id] = token_ids

    # ── Feed health ──────────────────────────────────────────

    def record_connect(self, feed_type: str):
        with self._lock:
            if feed_type == "external":
                self._health["ext_connects"] += 1
            else:
                self._health["pm_connects"] += 1

    def record_disconnect(self, feed_type: str):
        with self._lock:
            if feed_type == "external":
                self._health["ext_disconnects"] += 1
            else:
                self._health["pm_disconnects"] += 1
            self._health["reconnects"] += 1

    def record_dropped(self):
        with self._lock:
            self._health["dropped"] += 1

    def get_feed_health(self) -> dict:
        """Full feed health snapshot."""
        with self._lock:
            h = dict(self._health)
        h["runtime_seconds"] = time.time() - h["start_time"]

        # External stats
        now_ms = int(time.time() * 1000)
        ext_ages = []
        ext_connected = 0
        ext_stale = 0
        for asset, srcs in self._ext.items():
            for src, q in srcs.items():
                age = now_ms - q.get("received_ms", 0)
                ext_ages.append(age)
                if age <= self._stale_ms:
                    ext_connected += 1
                else:
                    ext_stale += 1

        pm_ages = []
        pm_connected = 0
        pm_stale = 0
        for tid, q in self._pm.items():
            age = now_ms - q.get("received_ms", 0)
            pm_ages.append(age)
            if age <= self._pm_stale_ms:
                pm_connected += 1
            else:
                pm_stale += 1

        h["external_feed_connected_count"] = ext_connected
        h["external_feed_stale_count"] = ext_stale
        h["polymarket_feed_connected"] = pm_connected
        h["polymarket_feed_stale"] = pm_stale
        h["polymarket_tokens_tracked"] = len(pm_ages)

        import numpy as np
        if ext_ages:
            h["external_feed_p50_age_ms"] = int(np.percentile(ext_ages, 50))
            h["external_feed_p95_age_ms"] = int(np.percentile(ext_ages, 95))
        else:
            h["external_feed_p50_age_ms"] = -1
            h["external_feed_p95_age_ms"] = -1
        if pm_ages:
            h["polymarket_book_p50_age_ms"] = int(np.percentile(pm_ages, 50))
            h["polymarket_book_p95_age_ms"] = int(np.percentile(pm_ages, 95))
        else:
            h["polymarket_book_p50_age_ms"] = -1
            h["polymarket_book_p95_age_ms"] = -1

        # Classification
        if ext_stale > ext_connected:
            h["feed_health_classification"] = "EXTERNAL_FEED_STALE"
        elif pm_stale > pm_connected:
            h["feed_health_classification"] = "POLYMARKET_FEED_STALE"
        elif ext_ages and h["external_feed_p95_age_ms"] > 2000:
            h["feed_health_classification"] = "WEBSOCKET_LAYER_UNSTABLE"
        else:
            h["feed_health_classification"] = "FEED_HEALTHY"

        return h

    def save_snapshots(self):
        """Write latest state to disk."""
        with self._lock:
            ext = dict(self._ext)
            pm = dict(self._pm)

        # External quotes
        out_ext = {}
        for asset, srcs in ext.items():
            now_ms = int(time.time() * 1000)
            sources = {}
            for src, q in srcs.items():
                sources[src] = dict(
                    bid=q.get("bid", 0), ask=q.get("ask", 0),
                    mid=q.get("mid", 0), last=q.get("last", 0),
                    timestamp_exchange_ms=q.get("timestamp_exchange_ms", 0),
                    received_ms=q.get("received_ms", 0),
                    feed_age_ms=now_ms - q.get("received_ms", 0),
                    source_status="LIVE" if (now_ms - q.get("received_ms", 0)) < self._stale_ms else "STALE",
                )
            median = self.get_cross_exchange_median(asset)
            cross_spread = 0
            if sources:
                bids = [s["bid"] for s in sources.values() if s["bid"] > 0]
                asks = [s["ask"] for s in sources.values() if s["ask"] > 0]
                if bids and asks:
                    cross_spread = round((max(asks) - min(bids)) / ((max(asks) + min(bids)) / 2) * 10000, 1)
            out_ext[asset] = dict(
                sources=sources,
                multi_exchange_median_mid=round(median, 2) if median else None,
                cross_exchange_spread_bps=cross_spread,
                fastest_source=min(sources.values(), key=lambda s: s["feed_age_ms"])["source_status"] if sources else None,
                stale_source_count=sum(1 for s in sources.values() if s["source_status"] == "STALE"),
            )
        with open(OUT / "latest_external_quotes.json", "w") as f:
            json.dump(out_ext, f, indent=2, default=str)

        # Polymarket books
        out_pm = {}
        now_ms = int(time.time() * 1000)
        for tid, q in pm.items():
            out_pm[tid] = dict(
                best_bid=q.get("best_bid", 0), best_ask=q.get("best_ask", 0),
                spread=q.get("spread", 0),
                bid_depth=q.get("bid_depth", 0), ask_depth=q.get("ask_depth", 0),
                last_trade_price=q.get("last_trade_price", 0),
                book_timestamp_ms=q.get("book_timestamp_ms", 0),
                received_ms=q.get("received_ms", 0),
                book_age_ms=now_ms - q.get("received_ms", 0),
                market_status=q.get("market_status", ""),
                slug=q.get("slug", ""),
                side=q.get("side", ""),
                asset=q.get("asset", ""),
            )
        with open(OUT / "latest_polymarket_books.json", "w") as f:
            json.dump(out_pm, f, indent=2, default=str)

        # Feed health
        health = self.get_feed_health()
        with open(OUT / "feed_health_report.json", "w") as f:
            json.dump(health, f, indent=2, default=str)