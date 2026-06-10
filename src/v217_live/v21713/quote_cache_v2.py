#!/usr/bin/env python3
"""
V21.7.13 QuoteCache V2 — Tick-Level Book Flow Tracking
======================================================
Async-safe. Uses asyncio.Lock for compatibility with WS event loop.
Fed by WS feeds, consumed by scanner loop.
"""
import time, json, math, asyncio
from collections import deque
from pathlib import Path
from typing import Optional, Dict, List

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "v21713_realtime_scanner"
OUT.mkdir(parents=True, exist_ok=True)

RING_BUFFER_SIZE = 600  # 600 updates at ~1/s = 10min history
VELOCITY_WINDOWS = [1.0, 3.0, 5.0, 15.0, 30.0, 60.0]


class TokenBookState:
    """Per-token book state with delta tracking."""
    def __init__(self, token_id: str):
        self.token_id = token_id
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.spread = 0.0
        self.bid_depth = 0.0
        self.ask_depth = 0.0
        self.mid_price = 0.0
        self.last_update_ms = 0
        self.update_count = 0
        self.source = "rest"
        self.history = deque(maxlen=RING_BUFFER_SIZE)
        self.best_ask_delta_1s = 0.0
        self.best_ask_delta_3s = 0.0
        self.best_ask_delta_5s = 0.0
        self.bid_depth_delta = 0.0
        self.ask_depth_delta = 0.0
        self.book_update_velocity = 0.0
        self.order_flow_imbalance = 0.0
        self.trade_print_direction = "NEUTRAL"

    def update(self, best_bid: float, best_ask: float, spread: float,
               bid_depth: float, ask_depth: float, ts_ms: int = None):
        now_ms = ts_ms or int(time.time() * 1000)
        self.history.append((now_ms, best_ask, best_bid, bid_depth, ask_depth))
        old_bid_depth = self.bid_depth
        old_ask_depth = self.ask_depth
        old_best_ask = self.best_ask
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.spread = spread
        self.bid_depth = bid_depth
        self.ask_depth = ask_depth
        self.mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
        self.last_update_ms = now_ms
        self.update_count += 1
        self._compute_deltas(now_ms)
        self.bid_depth_delta = bid_depth - old_bid_depth if old_bid_depth else 0
        self.ask_depth_delta = ask_depth - old_ask_depth if old_ask_depth else 0
        total = bid_depth + ask_depth
        self.order_flow_imbalance = ask_depth / total if total > 0 else 0.5
        if old_best_ask > 0:
            ask_move = best_ask - old_best_ask
            if ask_move < -0.005:
                self.trade_print_direction = "SELL"
            elif ask_move > 0.005:
                self.trade_print_direction = "BUY"
            else:
                self.trade_print_direction = "NEUTRAL"

    def _compute_deltas(self, now_ms: int):
        for window, attr in [(1000, "best_ask_delta_1s"),
                             (3000, "best_ask_delta_3s"),
                             (5000, "best_ask_delta_5s")]:
            cutoff = now_ms - window
            past = [h for h in self.history if h[0] >= cutoff]
            if len(past) >= 2:
                setattr(self, attr, past[-1][1] - past[0][1])
            elif len(past) == 1:
                setattr(self, attr, 0.0)
        cutoff = now_ms - 60000
        recent = sum(1 for h in self.history if h[0] >= cutoff)
        self.book_update_velocity = recent


class ExternalAssetState:
    """Per-asset external exchange velocity tracking."""
    def __init__(self, asset: str):
        self.asset = asset
        self.sources = {}
        self.price_history = deque(maxlen=6000)
        self.velocities = {f"v{w}s": 0.0 for w in VELOCITY_WINDOWS}
        self.cross_exchange_median = 0.0
        self.last_update_ms = 0

    def update_source(self, source: str, quote: dict):
        ts_ms = int(time.time() * 1000)
        mid = quote.get("mid", 0)
        if mid > 0:
            quote["received_ms"] = ts_ms
            self.sources[source] = quote
            self.price_history.append((ts_ms, mid, source))
            self.last_update_ms = ts_ms
            self._compute_velocities(ts_ms)
            self._compute_cross_median()

    def _compute_velocities(self, now_ms: int):
        for window in VELOCITY_WINDOWS:
            cutoff = now_ms - int(window * 1000)
            past = [(ts, p) for ts, p, src in self.price_history if ts >= cutoff]
            if len(past) >= 2:
                p0 = past[0][1]
                p1 = past[-1][1]
                vel = math.log(p1 / p0) if p0 > 0 and p1 > 0 else 0.0
                self.velocities[f"v{int(window)}s"] = vel

    def _compute_cross_median(self):
        mids = [q["mid"] for q in self.sources.values() if q.get("mid", 0) > 0]
        if mids:
            mids.sort()
            n = len(mids)
            self.cross_exchange_median = mids[n // 2] if n % 2 else (mids[n // 2 - 1] + mids[n // 2]) / 2


class QuoteCacheV2:
    """Async-safe V2 quote cache with tick-level delta tracking.
    Uses asyncio.Lock — all methods that touch shared state must be awaited."""
    def __init__(self, stale_ms=2000, pm_stale_ms=5000):
        self._lock = asyncio.Lock()
        self._stale_ms = stale_ms
        self._pm_stale_ms = pm_stale_ms
        self._ext: Dict[str, ExternalAssetState] = {}
        self._pm: Dict[str, TokenBookState] = {}
        self._pm_meta: Dict[str, dict] = {}
        self._health = dict(
            ext_connects=0, ext_disconnects=0,
            pm_ws_connects=0, pm_ws_disconnects=0,
            pm_ws_books_seen=False,
            pm_ws_book_updates=0,
            dropped=0, reconnects=0,
            start_time=time.time(),
            _momentum_events=0,
            _bucket_touch_events=0,
            _armed_pretriggers=0,
            _survivability_passes=0,
            _false_signals=0,
        )

    # ── External feeds ──────────────────────────────────────

    async def update_external(self, asset: str, source: str, quote: dict):
        async with self._lock:
            if asset not in self._ext:
                self._ext[asset] = ExternalAssetState(asset)
            self._ext[asset].update_source(source, quote)

    async def get_external_snapshot(self, asset: str) -> Optional[dict]:
        async with self._lock:
            state = self._ext.get(asset)
            if not state:
                return None
            now_ms = int(time.time() * 1000)
            sources = {}
            for src, q in state.sources.items():
                age = now_ms - q.get("received_ms", q.get("timestamp_exchange_ms", now_ms))
                sources[src] = dict(q, feed_age_ms=age, stale=age > self._stale_ms)
            return dict(asset=asset, sources=sources,
                        velocities=state.velocities,
                        cross_exchange_median=state.cross_exchange_median,
                        timestamp_ms=now_ms)

    # ── Polymarket feeds ─────────────────────────────────────

    async def update_polymarket(self, token_id: str, book: dict, source: str = "ws"):
        async with self._lock:
            if source == "ws":
                self._health["pm_ws_book_updates"] += 1
                self._health["pm_ws_books_seen"] = True
            if token_id not in self._pm:
                self._pm[token_id] = TokenBookState(token_id)
            ts = book.get("book_timestamp_ms") or book.get("received_ms") or int(time.time() * 1000)
            self._pm[token_id].update(
                best_bid=book.get("best_bid", 0),
                best_ask=book.get("best_ask", 0),
                spread=book.get("spread", 0),
                bid_depth=book.get("bid_depth", 0),
                ask_depth=book.get("ask_depth", 0),
                ts_ms=ts,
            )
            self._pm[token_id].source = source

    async def register_market(self, token_id: str, meta: dict):
        async with self._lock:
            self._pm_meta[token_id] = meta

    async def get_polymarket_snapshot(self, token_id: str) -> Optional[dict]:
        async with self._lock:
            tbs = self._pm.get(token_id)
            if not tbs:
                return None
            now_ms = int(time.time() * 1000)
            age = now_ms - tbs.last_update_ms
            return dict(
                token_id=token_id,
                best_bid=tbs.best_bid, best_ask=tbs.best_ask,
                spread=tbs.spread, mid_price=tbs.mid_price,
                bid_depth=tbs.bid_depth, ask_depth=tbs.ask_depth,
                book_age_ms=age, update_count=tbs.update_count,
                best_ask_delta_1s=tbs.best_ask_delta_1s,
                best_ask_delta_3s=tbs.best_ask_delta_3s,
                best_ask_delta_5s=tbs.best_ask_delta_5s,
                bid_depth_delta=tbs.bid_depth_delta,
                ask_depth_delta=tbs.ask_depth_delta,
                book_update_velocity=tbs.book_update_velocity,
                order_flow_imbalance=tbs.order_flow_imbalance,
                trade_print_direction=tbs.trade_print_direction,
                source=tbs.source,
                **self._pm_meta.get(token_id, {}),
            )

    async def get_all_pm_tokens(self) -> List[str]:
        async with self._lock:
            return list(self._pm.keys())

    # ── Feed health ──────────────────────────────────────────

    def record_connect(self, feed_type: str):
        if feed_type == "external":
            self._health["ext_connects"] += 1
        elif feed_type == "polymarket_ws":
            self._health["pm_ws_connects"] += 1

    def record_disconnect(self, feed_type: str):
        if feed_type == "external":
            self._health["ext_disconnects"] += 1
        elif feed_type == "polymarket_ws":
            self._health["pm_ws_disconnects"] += 1

    def increment_counter(self, counter: str, n: int = 1):
        if counter in self._health:
            self._health[counter] += n

    async def get_feed_health(self) -> dict:
        async with self._lock:
            now_ms = int(time.time() * 1000)
            runtime = time.time() - self._health["start_time"]
            ext_ages = []
            for asset, state in self._ext.items():
                for src, q in state.sources.items():
                    age = now_ms - q.get("received_ms", q.get("timestamp_exchange_ms", now_ms))
                    ext_ages.append(age)
            pm_ages = [now_ms - tbs.last_update_ms for tbs in self._pm.values() if tbs.last_update_ms > 0]
            ext_p50 = sorted(ext_ages)[len(ext_ages) // 2] if ext_ages else 99999
            ext_p95 = sorted(ext_ages)[int(len(ext_ages) * 0.95)] if ext_ages else 99999
            pm_p50 = sorted(pm_ages)[len(pm_ages) // 2] if pm_ages else 99999
            pm_p95 = sorted(pm_ages)[int(len(pm_ages) * 0.95)] if len(pm_ages) >= 20 else max(pm_ages) if pm_ages else 99999
            pm_ws_books = self._health.get("pm_ws_books_seen", False)
            pm_ws_updates = self._health.get("pm_ws_book_updates", 0)
            pm_ws_update_rate = pm_ws_updates / max(runtime / 60, 1)
            return dict(
                ext_connects=self._health["ext_connects"],
                ext_disconnects=self._health["ext_disconnects"],
                pm_ws_connects=self._health["pm_ws_connects"],
                pm_ws_disconnects=self._health["pm_ws_disconnects"],
                pm_ws_books_seen=pm_ws_books,
                pm_ws_book_update_rate=round(pm_ws_update_rate, 2),
                pm_ws_book_updates=pm_ws_updates,
                external_feed_p50_age_ms=ext_p50,
                external_feed_p95_age_ms=ext_p95,
                polymarket_book_p50_age_ms=pm_p50,
                polymarket_book_p95_age_ms=pm_p95,
                polymarket_tokens_tracked=len(self._pm),
                polymarket_feed_connected=len([a for a in pm_ages if a < self._pm_stale_ms]),
                dropped=self._health["dropped"],
                reconnects=self._health["reconnects"],
                runtime_seconds=round(runtime, 1),
                feed_health_classification="FEED_HEALTHY" if ext_p95 < 1000 else "FEED_DEGRADED",
                _momentum_events=self._health.get("_momentum_events", 0),
                _bucket_touch_events=self._health.get("_bucket_touch_events", 0),
                _armed_pretriggers=self._health.get("_armed_pretriggers", 0),
                _survivability_passes=self._health.get("_survivability_passes", 0),
                _false_signals=self._health.get("_false_signals", 0),
            )

    async def snapshot_all(self) -> dict:
        """Acquire lock ONCE and return all data — eliminates per-token lock churn."""
        async with self._lock:
            now_ms = int(time.time() * 1000)
            ext = {}
            for asset, state in self._ext.items():
                sources = {}
                for src, q in state.sources.items():
                    age = now_ms - q.get("received_ms", q.get("timestamp_exchange_ms", now_ms))
                    sources[src] = dict(q, feed_age_ms=age, stale=age > self._stale_ms)
                ext[asset] = dict(asset=asset, sources=sources,
                                  velocities=state.velocities,
                                  cross_exchange_median=state.cross_exchange_median,
                                  timestamp_ms=now_ms)
            pm = {}
            for tid, tbs in self._pm.items():
                age = now_ms - tbs.last_update_ms
                pm[tid] = dict(
                    token_id=tid, best_bid=tbs.best_bid, best_ask=tbs.best_ask,
                    spread=tbs.spread, mid_price=tbs.mid_price,
                    bid_depth=tbs.bid_depth, ask_depth=tbs.ask_depth,
                    book_age_ms=age, update_count=tbs.update_count,
                    best_ask_delta_1s=round(tbs.best_ask_delta_1s, 6),
                    best_ask_delta_3s=round(tbs.best_ask_delta_3s, 6),
                    best_ask_delta_5s=round(tbs.best_ask_delta_5s, 6),
                    bid_depth_delta=round(tbs.bid_depth_delta, 4),
                    ask_depth_delta=round(tbs.ask_depth_delta, 4),
                    book_update_velocity=round(tbs.book_update_velocity, 2),
                    order_flow_imbalance=round(tbs.order_flow_imbalance, 4),
                    trade_print_direction=tbs.trade_print_direction,
                    source=tbs.source,
                    **self._pm_meta.get(tid, {}),
                )
            tokens = list(self._pm.keys())
            # Compute health stats while holding lock
            ext_ages = [now_ms - q.get("received_ms", q.get("timestamp_exchange_ms", now_ms))
                        for state in self._ext.values() for q in state.sources.values()]
            pm_ages = [now_ms - tbs.last_update_ms for tbs in self._pm.values() if tbs.last_update_ms > 0]
            ext_p50 = sorted(ext_ages)[len(ext_ages)//2] if ext_ages else 99999
            ext_p95 = sorted(ext_ages)[int(len(ext_ages)*0.95)] if ext_ages else 99999
            pm_p50 = sorted(pm_ages)[len(pm_ages)//2] if pm_ages else 99999
            pm_p95 = sorted(pm_ages)[int(len(pm_ages)*0.95)] if len(pm_ages) >= 20 else (max(pm_ages) if pm_ages else 99999)
            health = dict(
                ext_connects=self._health["ext_connects"],
                ext_disconnects=self._health["ext_disconnects"],
                pm_ws_connects=self._health["pm_ws_connects"],
                pm_ws_disconnects=self._health["pm_ws_disconnects"],
                pm_ws_books_seen=self._health.get("pm_ws_books_seen", False),
                pm_ws_book_updates=self._health.get("pm_ws_book_updates", 0),
                ext_p50_age_ms=ext_p50, ext_p95_age_ms=ext_p95,
                pm_p50_age_ms=pm_p50, pm_p95_age_ms=pm_p95,
                polymarket_tokens_tracked=len(self._pm),
            )
            return dict(ext=ext, pm=pm, tokens=tokens, health=health, now_ms=now_ms)

    async def save_snapshots(self):
        """Save external quotes and PM books to disk using snapshot_all."""
        snap = await self.snapshot_all()
        with open(OUT / "latest_external_quotes.json", "w") as f:
            json.dump(snap["ext"], f, indent=2)
        with open(OUT / "latest_polymarket_books.json", "w") as f:
            json.dump(snap["pm"], f, indent=2)

    async def batch_update_polymarket(self, updates: list[dict]):
        """Update multiple PM tokens in a single lock acquisition."""
        async with self._lock:
            for upd in updates:
                tid = upd["token_id"]
                book = upd["book"]
                source = upd.get("source", "rest")
                if source == "ws":
                    self._health["pm_ws_book_updates"] += 1
                    self._health["pm_ws_books_seen"] = True
                if tid not in self._pm:
                    self._pm[tid] = TokenBookState(tid)
                ts = book.get("book_timestamp_ms") or book.get("received_ms") or int(time.time() * 1000)
                self._pm[tid].update(
                    best_bid=book.get("best_bid", 0),
                    best_ask=book.get("best_ask", 0),
                    spread=book.get("spread", 0),
                    bid_depth=book.get("bid_depth", 0),
                    ask_depth=book.get("ask_depth", 0),
                    ts_ms=ts,
                )
                self._pm[tid].source = source
            for upd in updates:
                meta = upd.get("meta")
                if meta:
                    self._pm_meta[upd["token_id"]] = meta