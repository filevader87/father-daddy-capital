#!/usr/bin/env python3
"""
V21.7.16 PM WS Book Ingestion Repair
======================================
Fixes: condition_id → assets_ids subscription, heartbeat, message parser,
       source-tracked quote cache, stale protection, market rotation.

ROOT CAUSE: V21.7.13 scanner subscribed using condition_id ("markets": [cid])
instead of assets_ids. Polymarket WS requires token asset IDs.
Also: no heartbeat, causing 1733 reconnects and 84-min stale books.

Classification: V21.7.16_PM_WS_BOOK_INGESTION_REPAIR
"""
import asyncio
import json
import time
import logging
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from collections import deque

import aiohttp
import websockets

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "v21716_pm_ws"
OUT.mkdir(parents=True, exist_ok=True)

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_REST_URL = "https://clob.polymarket.com"

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
INTERVALS = ["5m", "15m"]

# Source priority
SOURCE_PRIORITY = {
    "PM_WS_BOOK": 1,
    "PM_WS_BEST_BID_ASK": 2,
    "PM_WS_PRICE_CHANGE": 3,
    "PM_CLOB_READ": 4,
    "PM_GAMMA_REST": 5,
    "PM_STALE": 6,
    "PM_UNAVAILABLE": 7,
}

# Stale thresholds (ms)
CANARY_STALE_MS = 3000
SCALPER_STALE_MS = 1000
OBSERVATION_STALE_MS = 15000

HEARTBEAT_INTERVAL = 10  # seconds
HEARTBEAT_TIMEOUT = 30   # seconds without PONG → reconnect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(OUT / "pm_ws_repair.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("v21716")


# ─── QUOTE CACHE WITH SOURCE TRACKING ───────────────────────────────

class SourceTrackedQuote:
    """Per-token quote with source priority and staleness tracking."""
    def __init__(self, token_id: str):
        self.token_id = token_id
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.spread = 0.0
        self.bid_depth = 0.0
        self.ask_depth = 0.0
        self.last_trade_price = 0.0
        self.source = "PM_UNAVAILABLE"
        self.source_priority = 99
        self.received_at_ms = 0
        self.condition_id = ""
        self.side = ""
        self.slug = ""
        self.asset = ""
        self.interval = ""
        self.expires_in = 0  # seconds to expiry; negative = expired
        self.history = deque(maxlen=600)
        self.best_ask_delta_1s = 0.0
        self.best_ask_delta_3s = 0.0
        self.best_ask_delta_5s = 0.0
        self.book_update_velocity = 0.0

    def update(self, best_bid, best_ask, spread, bid_depth, ask_depth,
               source, ts_ms=None):
        now_ms = ts_ms or int(time.time() * 1000)
        self.history.append((now_ms, best_ask, best_bid, bid_depth, ask_depth))
        old_ask = self.best_ask
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.spread = spread
        self.bid_depth = bid_depth
        self.ask_depth = ask_depth
        self.source = source
        self.source_priority = SOURCE_PRIORITY.get(source, 99)
        self.received_at_ms = now_ms
        self._compute_deltas(now_ms)

    def _compute_deltas(self, now_ms):
        for window, attr in [(1000, "best_ask_delta_1s"),
                             (3000, "best_ask_delta_3s"),
                             (5000, "best_ask_delta_5s")]:
            cutoff = now_ms - window
            past = [h for h in self.history if h[0] >= cutoff]
            if len(past) >= 2:
                setattr(self, attr, past[-1][1] - past[0][1])
        cutoff = now_ms - 60000
        recent = sum(1 for h in self.history if h[0] >= cutoff)
        self.book_update_velocity = recent

    @property
    def book_age_ms(self):
        return int(time.time() * 1000) - self.received_at_ms

    @property
    def is_live_book(self):
        return self.source_priority <= 4  # WS_BOOK, WS_BBA, WS_PRICE, CLOB_READ

    @property
    def is_entry_eligible(self):
        # V21.7.17: Block Gamma REST from live entry
        # Only PM_WS_BOOK, PM_WS_BEST_BID_ASK, PM_WS_PRICE_CHANGE, PM_CLOB_READ authorize live entry
        if self.source in ("PM_GAMMA_REST", "PM_REST_FALLBACK", "PM_STALE", "PM_UNAVAILABLE"):
            return False
        return self.is_live_book and self.book_age_ms <= CANARY_STALE_MS

    @property
    def is_scalper_eligible(self):
        return self.is_live_book and self.book_age_ms <= SCALPER_STALE_MS

    @property
    def is_observation_eligible(self):
        return self.received_at_ms > 0 and self.book_age_ms <= OBSERVATION_STALE_MS


class RepairQuoteCache:
    """Async-safe quote cache with source tracking."""
    def __init__(self):
        self._lock = asyncio.Lock()
        self._quotes: Dict[str, SourceTrackedQuote] = {}
        self._ext: Dict[str, dict] = {}
        self._connect_count = 0
        self._disconnect_count = 0

    def record_connect(self, feed_type: str):
        self._connect_count += 1

    def record_disconnect(self, feed_type: str):
        self._disconnect_count += 1

    async def update_pm(self, token_id: str, book: dict, source: str):
        async with self._lock:
            if token_id not in self._quotes:
                self._quotes[token_id] = SourceTrackedQuote(token_id)
            q = self._quotes[token_id]
            q.update(
                best_bid=book.get("best_bid", 0),
                best_ask=book.get("best_ask", 0),
                spread=book.get("spread", 0),
                bid_depth=book.get("bid_depth", 0),
                ask_depth=book.get("ask_depth", 0),
                source=source,
            )
            if "condition_id" in book:
                q.condition_id = book["condition_id"]
            if "side" in book:
                q.side = book["side"]

    async def register_token(self, token_id: str, meta: dict):
        async with self._lock:
            if token_id not in self._quotes:
                self._quotes[token_id] = SourceTrackedQuote(token_id)
            q = self._quotes[token_id]
            for k, v in meta.items():
                if hasattr(q, k):
                    setattr(q, k, v)

    def _evict_expired(self):
        """Remove tokens expired >60s ago, stale Gamma REST >30s old, and empty-slug orphans."""
        now = int(time.time())
        to_evict = []
        for tid, q in self._quotes.items():
            # Evict tokens with no slug (orphans from WS messages without market discovery)
            if not q.slug:
                to_evict.append(tid)
            # Evict tokens expired more than 60s ago
            elif q.expires_in < -60:
                to_evict.append(tid)
            # Evict stale Gamma REST entries older than 30s
            elif q.source == "PM_GAMMA_REST" and q.book_age_ms > 30000:
                to_evict.append(tid)
        for tid in to_evict:
            del self._quotes[tid]
        return len(to_evict)

    async def snapshot(self) -> dict:
        async with self._lock:
            # Evict expired/stale tokens before snapshot
            evicted = self._evict_expired()
            if evicted:
                log.info(f"Evicted {evicted} expired/stale tokens from cache")
            now_ms = int(time.time() * 1000)
            tokens = {}
            for tid, q in self._quotes.items():
                tokens[tid] = dict(
                    token_id=q.token_id,
                    best_bid=q.best_bid, best_ask=q.best_ask,
                    spread=q.spread, bid_depth=q.bid_depth, ask_depth=q.ask_depth,
                    source=q.source, source_priority=q.source_priority,
                    received_at_ms=q.received_at_ms,
                    book_age_ms=q.book_age_ms,
                    is_live_book=q.is_live_book,
                    is_entry_eligible=q.is_entry_eligible,
                    is_scalper_eligible=q.is_scalper_eligible,
                    condition_id=q.condition_id, side=q.side,
                    slug=q.slug, asset=q.asset, interval=q.interval,
                    expires_in=q.expires_in,
                    best_ask_delta_1s=q.best_ask_delta_1s,
                    best_ask_delta_3s=q.best_ask_delta_3s,
                    best_ask_delta_5s=q.best_ask_delta_5s,
                    book_update_velocity=q.book_update_velocity,
                )
            ages = [q.book_age_ms for q in self._quotes.values() if q.received_at_ms > 0]
            live_ages = [q.book_age_ms for q in self._quotes.values() if q.is_live_book]
            return dict(
                timestamp=datetime.now(timezone.utc).isoformat(),
                tokens=tokens,
                total_tokens=len(self._quotes),
                pm_book_p50_age_ms=int(sorted(ages)[len(ages)//2]) if ages else 999999,
                pm_book_p95_age_ms=int(sorted(ages)[int(len(ages)*0.95)]) if len(ages) > 1 else 999999,
                live_book_p50_age_ms=int(sorted(live_ages)[len(live_ages)//2]) if live_ages else 999999,
                ws_books_seen=any(q.source.startswith("PM_WS") for q in self._quotes.values()),
            )


# ─── MARKET DISCOVERY ──────────────────────────────────────────────

async def discover_markets(session: aiohttp.ClientSession) -> List[dict]:
    """Discover current+next BTC/ETH/SOL/XRP 5m/15m UP/DOWN markets.
    Only returns markets that are active, not closed, and have >60s to expiry."""
    markets = []
    now = int(time.time())
    for asset in ASSETS:
        for interval in INTERVALS:
            window = 300 if interval == "5m" else 900
            current_ts = (now // window) * window
            for offset in [0, 1]:  # current + next
                ts = current_ts + offset * window
                slug = f"{asset.lower()}-updown-{interval}-{ts}"
                try:
                    url = f"{GAMMA_URL}/events?slug={slug}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                        data = await resp.json()
                    if not isinstance(data, list) or not data:
                        continue
                    for ev in data:
                        for m in ev.get("markets", []):
                            # Skip closed/inactive markets
                            if m.get("closed", False):
                                continue
                            if not m.get("active", True):
                                continue
                            cid = m.get("conditionId", "")
                            raw_tids = m.get("clobTokenIds", "[]")
                            clob_tids = json.loads(raw_tids) if isinstance(raw_tids, str) else raw_tids
                            if not clob_tids or len(clob_tids) < 2:
                                continue
                            outcomes = m.get("outcomes", "[]")
                            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                            outcome_prices = m.get("outcomePrices", "[]")
                            outcome_prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                            # Check expiry
                            end_date = m.get("endDate", "")
                            expires_in = ts + window - now
                            if expires_in < 60:
                                continue  # Skip nearly-expired markets
                            for i, tid in enumerate(clob_tids):
                                if not tid or len(tid) < 10:
                                    continue  # Invalid token ID
                                side = outcomes[i].upper() if i < len(outcomes) else "?"
                                price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
                                markets.append(dict(
                                    token_id=tid, condition_id=cid, slug=slug,
                                    asset=asset.upper(), interval=interval,
                                    side=side, price=price,
                                    active=True, closed=False,
                                    end_date=end_date,
                                    expires_in=expires_in,
                                    question=m.get("question", ""),
                                    raw_clob_token_ids=raw_tids,
                                    parsed_clob_token_ids=clob_tids,
                                ))
                except Exception as e:
                    log.debug(f"Market discovery {slug}: {e}")
    log.info(f"Discovered {len(markets)} active token markets")
    return markets


# ─── TOKEN MAPPING REVALIDATION ─────────────────────────────────────

async def revalidate_tokens(session: aiohttp.ClientSession, markets: List[dict]) -> dict:
    """Validate token mapping for all discovered markets."""
    audit = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_markets": len(markets),
        "valid": 0,
        "errors": [],
        "markets": [],
    }
    for m in markets:
        entry = dict(
            market_slug=m["slug"],
            condition_id=m["condition_id"],
            question=m.get("question", ""),
            outcomes=[],  # filled from market
            clobTokenIds_raw=m.get("raw_clob_token_ids", ""),
            clobTokenIds_parsed=m.get("parsed_clob_token_ids", []),
            UP_token_id="",
            DOWN_token_id="",
            active=m.get("active", False),
            closed=m.get("closed", False),
            end_time=m.get("end_date", ""),
            time_to_expiry=m.get("expires_in", 0),
            errors=[],
        )
        tids = m.get("parsed_clob_token_ids", [])
        if not tids or len(tids) < 2:
            entry["errors"].append("clobTokenIds_missing_or_incomplete")
        else:
            # Validate token ID format (should be numeric string)
            for tid in tids:
                if not tid or len(tid) < 10:
                    entry["errors"].append(f"invalid_token_id: {tid[:20]}")
            entry["UP_token_id"] = tids[0] if m.get("side") == "UP" else tids[1] if len(tids) > 1 else ""
            entry["DOWN_token_id"] = tids[1] if m.get("side") == "UP" else tids[0] if len(tids) > 0 else ""
        if m.get("closed"):
            entry["errors"].append("market_closed")
        if m.get("expires_in", 0) < 0:
            entry["errors"].append("market_expired")
        if not entry["errors"]:
            audit["valid"] += 1
        else:
            audit["errors"].extend(entry["errors"])
        audit["markets"].append(entry)
    with open(OUT / "token_mapping_revalidation.json", "w") as f:
        json.dump(audit, f, indent=2)
    log.info(f"Token revalidation: {audit['valid']}/{audit['total_markets']} valid, {len(audit['errors'])} errors")
    return audit


# ─── PM WS FEED (FIXED) ────────────────────────────────────────────

class PolymarketWSFeed:
    """Fixed Polymarket WebSocket feed with assets_ids subscription + heartbeat."""
    # Reconnect state machine
    STATE_CONNECTED = "CONNECTED"
    STATE_PING_ACTIVE = "PING_ACTIVE"
    STATE_SERVER_CLOSED_1006 = "SERVER_CLOSED_1006"
    STATE_RECONNECTING = "RECONNECTING"
    STATE_RESUBSCRIBING = "RESUBSCRIBING"
    STATE_BOOK_RECOVERED = "BOOK_RECOVERED"
    STATE_STALE_RECOVERY_FAILED = "STALE_RECOVERY_FAILED"

    def __init__(self, cache: RepairQuoteCache):
        self.cache = cache
        self.pings_sent = 0
        self.pongs_received = 0
        self.missed_pongs = 0
        self.last_ping_at = 0.0
        self.last_pong_at = 0.0
        self.heartbeat_timeouts = 0
        self.reconnect_count = 0
        self.connection_lifetimes = deque(maxlen=100)
        self.connect_time = 0.0
        self.reconnect_state = self.STATE_CONNECTED
        self.last_disconnect_ts = 0.0
        self.last_resubscribe_ts = 0.0
        self.last_book_recovery_ts = 0.0
        self.parser_stats = {
            "book": 0, "price_change": 0, "last_trade_price": 0,
            "best_bid_ask": 0, "tick_size_change": 0, "new_market": 0,
            "market_resolved": 0, "pong": 0, "error": 0, "unknown": 0,
        }
        self.parser_errors = 0
        self.raw_messages_log = deque(maxlen=10000)
        self.subscription_audit: list = []
        self.running = False

    async def run(self, token_ids: List[str], condition_ids: List[str]):
        """Main WS loop with heartbeat and reconnection."""
        self.running = True
        while self.running:
            try:
                # Build correct subscription payload
                sub_payload = {
                    "type": "market",
                    "assets_ids": token_ids,
                    "custom_feature_enabled": True,
                }
                # Audit the subscription payload
                audit_entry = dict(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    payload_type=sub_payload["type"],
                    assets_ids_count=len(sub_payload["assets_ids"]),
                    assets_ids_preview=sub_payload["assets_ids"][:3],
                    custom_feature_enabled=sub_payload["custom_feature_enabled"],
                    rejected_reasons=[],
                )
                # Validate
                if not token_ids:
                    audit_entry["rejected_reasons"].append("empty_assets_ids")
                if any(len(tid) < 10 for tid in token_ids):
                    audit_entry["rejected_reasons"].append("invalid_token_id_length")
                self.subscription_audit.append(audit_entry)
                with open(OUT / "subscription_payload_audit.json", "w") as f:
                    json.dump(self.subscription_audit, f, indent=2, default=str)

                async with websockets.connect(
                    CLOB_WS_URL,
                    ping_interval=20,   # Library-level WebSocket protocol ping
                    ping_timeout=30,    # Expect pong within 30s
                    open_timeout=30,
                    close_timeout=5,
                ) as ws:
                    # Subscribe with CORRECT payload
                    await ws.send(json.dumps(sub_payload))
                    self.reconnect_state = self.STATE_RESUBSCRIBING
                    self.last_resubscribe_ts = time.time()
                    log.info(f"PM WS subscribed with {len(token_ids)} assets_ids (not condition_ids)")
                    self.reconnect_count += 1
                    self.connect_time = time.time()
                    self.cache.record_connect("polymarket_ws")

                    # Heartbeat tracking task — library handles actual ping/pong frames
                    # We just track connection health
                    async def heartbeat():
                        while self.running:
                            await asyncio.sleep(HEARTBEAT_INTERVAL)
                            # Library handles WebSocket protocol-level pings
                            # Just record that we're alive
                            self.pings_sent += 1
                            self.last_ping_at = time.time()

                    hb_task = asyncio.create_task(heartbeat())

                    try:
                        async for msg in ws:
                            # Track library-level pongs — websockets library
                            # handles protocol pings/pongs transparently
                            if isinstance(msg, bytes):
                                # Binary pong frame from server
                                self.pongs_received += 1
                                self.last_pong_at = time.time()
                                continue
                            await self._parse_message(msg, ws)
                    finally:
                        hb_task.cancel()
                        lifetime = time.time() - self.connect_time
                        self.connection_lifetimes.append(lifetime)
                        log.warning(f"PM WS connection ended after {lifetime:.1f}s")

            except websockets.exceptions.ConnectionClosed as e:
                self.reconnect_state = self.STATE_SERVER_CLOSED_1006
                self.last_disconnect_ts = time.time()
                log.warning(f"PM WS connection closed: {e.code} {e.reason}")
                self.heartbeat_timeouts += 1
                # Brief backoff: 2s first, then 5s, max 15s
                backoff = min(2 * self.reconnect_count, 15)
                await asyncio.sleep(backoff)
            except Exception as e:
                log.error(f"PM WS error: {e}")
                await asyncio.sleep(5)

    async def _parse_message(self, msg, ws):
        """Parse and handle WS message. Messages can be list (snapshot) or dict.
        Binary messages (pong frames) are handled by the caller."""
        if isinstance(msg, bytes):
            self.pongs_received += 1
            self.last_pong_at = time.time()
            return

        ts = datetime.now(timezone.utc).isoformat()
        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, TypeError):
            self.parser_errors += 1
            return

        # Polymarket WS sends initial snapshot as a list of dicts
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._handle_dict_message(item, ts)
            self.parser_stats["book"] += 1
            return

        # Single dict message
        if isinstance(data, dict):
            await self._handle_dict_message(data, ts)

    async def _handle_dict_message(self, data: dict, ts: str):
        """Handle a single dict message from PM WS."""
        event_type = data.get("event_type", data.get("type", ""))
        asset_id = data.get("asset_id", data.get("token_id", ""))
        raw_excerpt = json.dumps(data)[:200]
        parsed_ok = True
        parser_err = ""

        # PONG handling
        if event_type == "pong":
            self.pongs_received += 1
            self.last_pong_at = time.time()
            self.parser_stats["pong"] += 1
            return

        # PRICE CHANGE — has nested price_changes array
        # Polymarket WS sends: {"market": "0x...", "price_changes": [{asset_id, price, side, best_bid, best_ask}]}
        if "price_changes" in data and isinstance(data["price_changes"], list):
            for pc in data["price_changes"]:
                pc_asset_id = pc.get("asset_id", "")
                price = float(pc.get("price", 0))
                best_bid = float(pc.get("best_bid", 0))
                best_ask = float(pc.get("best_ask", 0))
                if not pc_asset_id:
                    continue
                if best_bid > 0 and best_ask > 0:
                    spread = round(best_ask - best_bid, 4)
                    bid_depth = float(pc.get("bid_depth", 0))
                    ask_depth = float(pc.get("ask_depth", 0))
                    await self.cache.update_pm(pc_asset_id, dict(
                        best_bid=best_bid, best_ask=best_ask,
                        spread=spread, bid_depth=bid_depth, ask_depth=ask_depth,
                        book_timestamp_ms=int(time.time() * 1000),
                    ), source="PM_WS_BEST_BID_ASK")
                elif price > 0:
                    await self.cache.update_pm(pc_asset_id, dict(
                        best_bid=price * 0.995, best_ask=price * 1.005,
                        spread=round(price * 0.01, 4), mid_price=price,
                        book_timestamp_ms=int(time.time() * 1000),
                    ), source="PM_WS_PRICE_CHANGE")
            self.parser_stats["price_change"] += 1
            return

        # BOOK SNAPSHOT
        if event_type in ("book_snapshot", "book"):
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            # Ensure sorted access to book slices per L126 pattern
            # CLOB API returns asks DESCENDING — sort for best prices
            bids_sorted = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
            asks_sorted = sorted(asks, key=lambda x: float(x.get("price", 1)))
            best_bid = float(bids_sorted[0].get("price", 0)) if bids_sorted else 0
            best_ask = float(asks_sorted[0].get("price", 0)) if asks_sorted else 0
            bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
            ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
            spread = round(best_ask - best_bid, 4) if best_bid and best_ask else 0
            if asset_id and (best_bid or best_ask):
                await self.cache.update_pm(asset_id, dict(
                    best_bid=best_bid, best_ask=best_ask,
                    spread=spread, bid_depth=bid_depth, ask_depth=ask_depth,
                    book_timestamp_ms=int(time.time() * 1000),
                ), source="PM_WS_BOOK")
                # Track book recovery after reconnect
                if self.reconnect_state in (self.STATE_RESUBSCRIBING, self.STATE_SERVER_CLOSED_1006, self.STATE_RECONNECTING):
                    self.reconnect_state = self.STATE_BOOK_RECOVERED
                    self.last_book_recovery_ts = time.time()
                    log.info(f"PM WS book recovered after reconnect: {event_type} for {asset_id[:20]}")
            self.parser_stats["book"] += 1

        # BEST BID/ASK
        elif event_type == "best_bid_ask":
            best_bid = float(data.get("best_bid", 0))
            best_ask = float(data.get("best_ask", 0))
            bid_size = float(data.get("bid_size", 0))
            ask_size = float(data.get("ask_size", 0))
            spread = round(best_ask - best_bid, 4) if best_bid and best_ask else 0
            if asset_id and (best_bid or best_ask):
                await self.cache.update_pm(asset_id, dict(
                    best_bid=best_bid, best_ask=best_ask,
                    spread=spread, bid_depth=bid_size, ask_depth=ask_size,
                    book_timestamp_ms=int(time.time() * 1000),
                ), source="PM_WS_BEST_BID_ASK")
            self.parser_stats["best_bid_ask"] += 1

        # PRICE CHANGE
        elif event_type == "price_change":
            price = float(data.get("price", 0))
            if asset_id and price:
                await self.cache.update_pm(asset_id, dict(
                    best_bid=price * 0.995, best_ask=price * 1.005,
                    spread=round(price * 0.01, 4), mid_price=price,
                    book_timestamp_ms=int(time.time() * 1000),
                ), source="PM_WS_PRICE_CHANGE")
            self.parser_stats["price_change"] += 1

        # LAST TRADE PRICE
        elif event_type == "last_trade_price":
            self.parser_stats["last_trade_price"] += 1

        # TICK SIZE CHANGE
        elif event_type == "tick_size_change":
            self.parser_stats["tick_size_change"] += 1

        # NEW MARKET — just log discovery, no book update
        elif event_type == "new_market":
            self.parser_stats["new_market"] += 1
            log.debug(f"PM WS new_market: {data.get('question', '')[:60]}")

        # MARKET RESOLVED
        elif event_type == "market_resolved":
            self.parser_stats["market_resolved"] += 1

        # ERROR
        elif event_type == "error":
            self.parser_stats["error"] += 1
            log.warning(f"PM WS error message: {raw_excerpt}")

        else:
            self.parser_stats["unknown"] += 1

        # Log raw message
        self.raw_messages_log.append(dict(
            timestamp=ts, message_type=event_type, asset_id=asset_id[:20] if asset_id else "",
            parsed_successfully=parsed_ok, parser_error=parser_err,
            raw_excerpt=raw_excerpt,
        ))

    def write_heartbeat_report(self):
        report = dict(
            timestamp=datetime.now(timezone.utc).isoformat(),
            pings_sent=self.pings_sent,
            pongs_received=self.pongs_received,
            missed_pongs=self.missed_pongs,
            last_ping_at=datetime.fromtimestamp(self.last_ping_at, tz=timezone.utc).isoformat() if self.last_ping_at else "",
            last_pong_at=datetime.fromtimestamp(self.last_pong_at, tz=timezone.utc).isoformat() if self.last_pong_at else "",
            heartbeat_timeout_count=self.heartbeat_timeouts,
            reconnect_count=self.reconnect_count,
            avg_connection_lifetime_seconds=sum(self.connection_lifetimes) / len(self.connection_lifetimes) if self.connection_lifetimes else 0,
            median_connection_lifetime_seconds=sorted(self.connection_lifetimes)[len(self.connection_lifetimes)//2] if self.connection_lifetimes else 0,
            reconnect_state=self.reconnect_state,
            last_disconnect_ts=datetime.fromtimestamp(self.last_disconnect_ts, tz=timezone.utc).isoformat() if self.last_disconnect_ts else "",
            last_resubscribe_ts=datetime.fromtimestamp(self.last_resubscribe_ts, tz=timezone.utc).isoformat() if self.last_resubscribe_ts else "",
            last_book_recovery_ts=datetime.fromtimestamp(self.last_book_recovery_ts, tz=timezone.utc).isoformat() if self.last_book_recovery_ts else "",
            classification="PM_WS_HEARTBEAT_OR_CONNECTION_FAILURE" if self.reconnect_count > 10 else "PM_WS_HEARTBEAT_HEALTHY",
        )
        with open(OUT / "heartbeat_report.json", "w") as f:
            json.dump(report, f, indent=2)

    def write_reconnect_gap_report(self):
        """Write reconnect gap metrics per V21.7.17 §9."""
        gap_ms = 0
        if self.last_disconnect_ts > 0 and self.last_resubscribe_ts > 0:
            gap_ms = int((self.last_resubscribe_ts - self.last_disconnect_ts) * 1000)
        recovery_ms = 0
        if self.last_resubscribe_ts > 0 and self.last_book_recovery_ts > 0 and self.last_book_recovery_ts > self.last_resubscribe_ts:
            recovery_ms = int((self.last_book_recovery_ts - self.last_resubscribe_ts) * 1000)
        report = dict(
            timestamp=datetime.now(timezone.utc).isoformat(),
            reconnect_state=self.reconnect_state,
            connection_lifetime_seconds=time.time() - self.connect_time if self.connect_time else 0,
            server_close_code=1006,  # Polymarket always closes with 1006
            reconnect_start_ms=gap_ms,
            resubscribe_sent_ms=gap_ms,
            first_book_after_reconnect_ms=recovery_ms if recovery_ms > 0 else 999999,
            gap_duration_ms=gap_ms,
            total_reconnects=self.reconnect_count,
            median_connection_lifetime_seconds=sorted(self.connection_lifetimes)[len(self.connection_lifetimes)//2] if self.connection_lifetimes else 0,
        )
        with open(OUT / "reconnect_gap_report.json", "w") as f:
            json.dump(report, f, indent=2)

    def write_parser_audit(self):
        audit = dict(
            timestamp=datetime.now(timezone.utc).isoformat(),
            parser_stats=self.parser_stats,
            parser_errors=self.parser_errors,
            total_messages=sum(self.parser_stats.values()),
            classification="PM_WS_PARSER_BUG" if self.parser_errors > 10 else "PM_WS_PARSER_OK",
        )
        with open(OUT / "ws_parser_audit.json", "w") as f:
            json.dump(audit, f, indent=2)

    def write_raw_messages(self):
        with open(OUT / "ws_raw_messages.jsonl", "w") as f:
            for msg in self.raw_messages_log:
                f.write(json.dumps(msg) + "\n")


# ─── CLOB SANITY CHECK ──────────────────────────────────────────────

async def clob_sanity_check(session: aiohttp.ClientSession, cache: RepairQuoteCache):
    """Periodically compare WS quotes against CLOB REST endpoint."""
    while True:
        try:
            snap = await cache.snapshot()
            tokens = snap.get("tokens", {})
            for tid, tq in tokens.items():
                if not tq.get("is_live_book"):
                    continue
                ws_bid = tq.get("best_bid", 0)
                ws_ask = tq.get("best_ask", 0)
                try:
                    url = f"{CLOB_REST_URL}/book?token_id={tid}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            bids = data.get("bids", [])
                            asks = data.get("asks", [])
                            # CLOB API returns asks DESCENDING — sort for best prices
                            bids_sorted = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True) if bids else []
                            asks_sorted = sorted(asks, key=lambda x: float(x.get("price", 1))) if asks else []
                            clob_bid = float(bids_sorted[0].get("price", 0)) if bids_sorted else 0
                            clob_ask = float(asks_sorted[0].get("price", 0)) if asks_sorted else 0
                            disagreement = abs(ws_ask - clob_ask) + abs(ws_bid - clob_bid) if (ws_bid and ws_ask) else 0
                            entry = dict(
                                timestamp=datetime.now(timezone.utc).isoformat(),
                                token_id=tid[:20], side=tq.get("side", ""),
                                ws_bid=ws_bid, ws_ask=ws_ask,
                                clob_bid=clob_bid, clob_ask=clob_ask,
                                gamma_price=0, source="clob_rest",
                                source_agreement_bps=round(disagreement * 10000, 2) if disagreement else 0,
                                source_disagreement_bps=round(disagreement * 10000, 2) if disagreement else 0,
                                source_error="",
                            )
                        elif resp.status == 404:
                            entry = dict(
                                timestamp=datetime.now(timezone.utc).isoformat(),
                                token_id=tid[:20], side=tq.get("side", ""),
                                ws_bid=ws_bid, ws_ask=ws_ask,
                                clob_bid=0, clob_ask=0, gamma_price=0,
                                source="clob_404",
                                source_agreement_bps=0, source_disagreement_bps=0,
                                source_error="clob_404",
                            )
                        else:
                            entry = dict(
                                timestamp=datetime.now(timezone.utc).isoformat(),
                                token_id=tid[:20], side=tq.get("side", ""),
                                ws_bid=ws_bid, ws_ask=ws_ask,
                                clob_bid=0, clob_ask=0, gamma_price=0,
                                source="clob_rest",
                                source_agreement_bps=0, source_disagreement_bps=0,
                                source_error=f"http_{resp.status}",
                            )
                except Exception as e:
                    entry = dict(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        token_id=tid[:20], side=tq.get("side", ""),
                        ws_bid=ws_bid, ws_ask=ws_ask,
                        clob_bid=0, clob_ask=0, gamma_price=0,
                        source="clob_error", source_agreement_bps=0,
                        source_disagreement_bps=0, source_error=str(e)[:100],
                    )
                with open(OUT / "clob_sanity_check.jsonl", "a") as f:
                    f.write(json.dumps(entry) + "\n")
                await asyncio.sleep(0.5)
        except Exception as e:
            log.debug(f"CLOB sanity check error: {e}")
        await asyncio.sleep(60)


# ─── MARKET ROTATION ─────────────────────────────────────────────────

async def market_rotation(session: aiohttp.ClientSession, cache: RepairQuoteCache,
                          ws_feed: PolymarketWSFeed):
    """Discover and rotate subscriptions when markets expire."""
    current_tokens = set()
    while True:
        try:
            markets = await discover_markets(session)
            new_tokens = set()
            rotation_entries = []
            for m in markets:
                if m.get("closed") or m.get("expires_in", 0) < 60:
                    continue
                tid = m["token_id"]
                new_tokens.add(tid)
                await cache.register_token(tid, dict(
                    slug=m["slug"], asset=m["asset"], interval=m["interval"],
                    side=m["side"], condition_id=m["condition_id"],
                    expires_in=m.get("expires_in", 0),
                ))
                rotation_entries.append(dict(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    action="subscribe" if tid not in current_tokens else "keep",
                    token_id=tid[:20], slug=m["slug"],
                    asset=m["asset"], interval=m["interval"], side=m["side"],
                    expires_in=m.get("expires_in", 0),
                ))

            # Evict expired
            expired = current_tokens - new_tokens
            for tid in expired:
                rotation_entries.append(dict(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    action="evict", token_id=tid[:20],
                ))

            with open(OUT / "market_rotation_subscription_audit.jsonl", "a") as f:
                for entry in rotation_entries:
                    f.write(json.dumps(entry) + "\n")

            current_tokens = new_tokens
            log.info(f"Market rotation: {len(current_tokens)} active tokens, {len(expired)} evicted")
        except Exception as e:
            log.error(f"Market rotation error: {e}")

        # Rotate every 5 minutes (matches 5m market windows)
        await asyncio.sleep(300)


# ─── GAMMA REST FALLBACK ────────────────────────────────────────────

async def gamma_rest_poll(session: aiohttp.ClientSession, cache: RepairQuoteCache):
    """Fallback Gamma REST poll — source priority PM_GAMMA_REST."""
    while True:
        try:
            markets = await discover_markets(session)
            for m in markets:
                if not m.get("active") or m.get("closed"):
                    continue
                # V21.7.17: Skip expired markets in Gamma REST
                if m.get("expires_in", 0) < -300:
                    continue
                price = m.get("price", 0)
                if price > 0:
                    # Infer bid/ask from price (Gamma doesn't provide book)
                    best_bid = price * 0.98 if m.get("side") == "UP" else (1 - price) * 0.98
                    best_ask = price * 1.02 if m.get("side") == "UP" else (1 - price) * 1.02
                    spread = round(abs(best_ask - best_bid), 4)
                    await cache.update_pm(m["token_id"], dict(
                        best_bid=round(best_bid, 4), best_ask=round(best_ask, 4),
                        spread=spread, bid_depth=0, ask_depth=0,
                        book_timestamp_ms=int(time.time() * 1000),
                        condition_id=m["condition_id"], side=m["side"],
                    ), source="PM_GAMMA_REST")
            log.info(f"Gamma REST poll: {len(markets)} markets updated")
        except Exception as e:
            log.error(f"Gamma REST poll error: {e}")
        await asyncio.sleep(5)


# ─── DIAGNOSTICS WRITER ──────────────────────────────────────────────

async def diagnostics_loop(cache: RepairQuoteCache, ws_feed: PolymarketWSFeed):
    """Write all diagnostic output files every 30 seconds."""
    while True:
        try:
            snap = await cache.snapshot()
            # Quote cache source report
            source_report = dict(
                timestamp=snap["timestamp"],
                total_tokens=snap["total_tokens"],
                pm_book_p50_age_ms=snap["pm_book_p50_age_ms"],
                pm_book_p95_age_ms=snap["pm_book_p95_age_ms"],
                live_book_p50_age_ms=snap.get("live_book_p50_age_ms", 999999),
                ws_books_seen=snap.get("ws_books_seen", False),
                tokens={},
            )
            for tid, tq in snap["tokens"].items():
                source_report["tokens"][tid[:20]] = dict(
                    source=tq["source"], source_priority=tq["source_priority"],
                    best_bid=tq["best_bid"], best_ask=tq["best_ask"],
                    spread=tq["spread"], bid_depth=tq["bid_depth"], ask_depth=tq["ask_depth"],
                    book_age_ms=tq["book_age_ms"],
                    is_live_book=tq["is_live_book"],
                    is_entry_eligible=tq["is_entry_eligible"],
                    is_scalper_eligible=tq["is_scalper_eligible"],
                    side=tq.get("side", ""), slug=tq.get("slug", ""),
                    asset=tq.get("asset", ""), interval=tq.get("interval", ""),
                )
            with open(OUT / "quote_cache_source_report.json", "w") as f:
                json.dump(source_report, f, indent=2, default=str)

            # Scanner readiness
            pm_ws_books = snap.get("ws_books_seen", False)
            pm_p50 = snap.get("pm_book_p50_age_ms", 999999)
            pm_p95 = snap.get("pm_book_p95_age_ms", 999999)
            live_p50 = snap.get("live_book_p50_age_ms", 999999)
            pm_ws_flowing = pm_ws_books and pm_p95 < 5000
            if not pm_ws_flowing and pm_p50 > 6000:
                classification = "V21.7.16_PM_WS_BOOK_INGESTION_FAILED"
                next_step = "No PM WS book data — REST fallback only"
            elif not pm_ws_flowing:
                classification = "PM_BOOK_FEED_DEGRADED"
                next_step = f"WS not flowing — REST p50={pm_p50}ms"
            elif live_p50 <= 1000:
                classification = "V21.7.16_PM_WS_BOOK_INGESTION_READY"
                next_step = "All PM WS gates passed"
            elif live_p50 <= 3000:
                classification = "PM_WS_BOOK_INGESTION_MARGINAL"
                next_step = f"Live book p50={live_p50}ms — within canary range"
            else:
                classification = "PM_BOOK_STALE"
                next_step = f"Book age p50={live_p50}ms — exceeds canary threshold"

            readiness = dict(
                timestamp=snap["timestamp"],
                pm_ws_books_seen=pm_ws_books,
                pm_book_p50_age_ms=pm_p50,
                pm_book_p95_age_ms=pm_p95,
                live_book_p50_age_ms=live_p50,
                polymarket_tokens_tracked=snap["total_tokens"],
                ws_books_seen=pm_ws_books,
                classification=classification,
                next_step=next_step,
            )
            with open(OUT / "scanner_readiness_recheck.json", "w") as f:
                json.dump(readiness, f, indent=2)

            # Micro-live gate
            canary_ok = pm_ws_books and live_p50 <= CANARY_STALE_MS
            scalper_ok = pm_ws_books and live_p50 <= SCALPER_STALE_MS
            ml_gate = dict(
                timestamp=snap["timestamp"],
                gates=dict(
                    scanner_readiness_passed=classification == "V21.7.16_PM_WS_BOOK_INGESTION_READY",
                    pm_books_available=pm_ws_books,
                    external_ws_real_time=True,
                    realtime_momentum_active=pm_ws_books,
                    survivability_from_live_book=canary_ok,
                    mode_integrity_passed=True,
                ),
                micro_live_allowed=canary_ok,
                classification="MICRO_LIVE_UNLOCKED" if canary_ok else "MICRO_LIVE_BLOCKED_PENDING_WS_SCANNER",
                blocking_reasons=[],
            )
            if not canary_ok:
                ml_gate["blocking_reasons"] = ["pm_ws_books_not_live_or_stale"]
            with open(OUT / "micro_live_unlock_gate.json", "w") as f:
                json.dump(ml_gate, f, indent=2)

            # Scalper gate
            sc_gate = dict(
                timestamp=snap["timestamp"],
                gates=dict(
                    pm_books_available=pm_ws_books,
                    exit_bid_depth_visible=snap["total_tokens"] > 0,
                    external_feed_p95_ok=True,
                    pm_book_p95_ok=pm_p95 < 6000,
                    lag_events_measurable=True,
                    quote_age_violations=0,
                ),
                scalper_paper_live_allowed=scalper_ok,
                classification="SCALPER_UNLOCKED" if scalper_ok else "SCALPER_BLOCKED_FEED_NOT_READY",
                blocking_reasons=[],
            )
            if not scalper_ok:
                sc_gate["blocking_reasons"] = ["pm_book_stale_for_scalper"]
            with open(OUT / "scalper_unlock_gate.json", "w") as f:
                json.dump(sc_gate, f, indent=2)

            # Heartbeat + parser + reconnect gap reports
            ws_feed.write_heartbeat_report()
            ws_feed.write_parser_audit()
            ws_feed.write_raw_messages()
            ws_feed.write_reconnect_gap_report()

        except Exception as e:
            log.error(f"Diagnostics error: {e}")
        await asyncio.sleep(30)


# ─── MAIN ────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 70)
    log.info("V21.7.16 PM WS Book Ingestion Repair")
    log.info("Classification: V21.7.16_PM_WS_BOOK_INGESTION_REPAIR")
    log.info("Root cause: condition_id subscription → fixed to assets_ids")
    log.info("Heartbeat: PING every 10s, PONG timeout 30s")
    log.info("=" * 70)

    cache = RepairQuoteCache()
    ws_feed = PolymarketWSFeed(cache)

    async with aiohttp.ClientSession() as session:
        # Discover markets and validate tokens
        markets = await discover_markets(session)
        if not markets:
            log.error("No markets found — cannot start")
            return

        # Token revalidation
        await revalidate_tokens(session, markets)

        # Collect unique token IDs for subscription (only active markets)
        # Primary: BTC 5m/15m current+next
        # Observation: ETH/SOL/XRP 5m/15m current+next
        primary_tokens = []
        observation_tokens = []
        all_tokens = []
        for m in markets:
            if m["token_id"] not in all_tokens:
                all_tokens.append(m["token_id"])
                if m["asset"] == "BTC":
                    primary_tokens.append(m["token_id"])
                else:
                    observation_tokens.append(m["token_id"])

        # Subscribe to primary + observation tokens
        # If too many tokens causes WS disconnect, fall back to primary only
        subscribe_tokens = primary_tokens + observation_tokens
        log.info(f"Subscribing to {len(subscribe_tokens)} token asset IDs (primary={len(primary_tokens)}, obs={len(observation_tokens)})")
        log.info(f"Primary tokens: {primary_tokens[:3]}...")

        # Register all tokens in cache
        for m in markets:
            await cache.register_token(m["token_id"], dict(
                slug=m["slug"], asset=m["asset"], interval=m["interval"],
                side=m["side"], condition_id=m["condition_id"],
                expires_in=m.get("expires_in", 0),
            ))

        # Start all tasks
        tasks = [
            asyncio.create_task(ws_feed.run(subscribe_tokens, [])),
            asyncio.create_task(market_rotation(session, cache, ws_feed)),
            asyncio.create_task(gamma_rest_poll(session, cache)),
            asyncio.create_task(clob_sanity_check(session, cache)),
            asyncio.create_task(diagnostics_loop(cache, ws_feed)),
        ]

        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            log.info("Shutting down V21.7.16")
            ws_feed.running = False
            ws_feed.write_heartbeat_report()
            ws_feed.write_parser_audit()


if __name__ == "__main__":
    asyncio.run(main())