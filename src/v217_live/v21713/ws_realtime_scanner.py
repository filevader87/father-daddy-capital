#!/usr/bin/env python3
"""
V21.7.13 Real-Time Momentum Scanner — WebSocket-First Architecture
====================================================================
Async-safe. Uses asyncio.Lock throughout. aiohttp for non-blocking REST.

Classification: V21.7.13_REALTIME_SCANNER_BUILD
Micro-live: BLOCKED pending WS scanner readiness
"""
import asyncio, json, time, logging, sys, traceback, math, os
from pathlib import Path
from datetime import datetime, timezone

import websockets, aiohttp

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "v21713_realtime_scanner"
OUT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(BASE / "src" / "v217_live" / "v21713"))
from quote_cache_v2 import QuoteCacheV2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(OUT / "scanner.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("v21713")

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
INTERVALS = ["5m", "15m"]
LIVE_BUCKET_FLOOR = 0.03
LIVE_BUCKET_CAP = 0.08

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com"

BINANCE_SYMBOLS = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt", "XRP": "xrpusdt"}
BYBIT_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
OKX_SYMBOLS = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "SOL": "SOL-USDT-SWAP", "XRP": "XRP-USDT-SWAP"}
COINBASE_SYMBOLS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}

# ═══════════════════════════════════════════════════════════════════════
# MARKET DISCOVERY (aiohttp)
# ═══════════════════════════════════════════════════════════════════════
async def discover_active_markets(session: aiohttp.ClientSession):
    markets = []
    now = int(time.time())
    for asset in ASSETS:
        for interval in INTERVALS:
            window = 300 if interval == "5m" else 900
            current = (now // window) * window
            for offset in [0, 1]:
                ts = current + offset * window
                slug = f"{asset.lower()}-updown-{interval}-{ts}"
                try:
                    url = f"{GAMMA_URL}/events?slug={slug}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                        data = await resp.json()
                    if isinstance(data, list) and data:
                        for ev in data:
                            for m in ev.get("markets", []):
                                cid = m.get("conditionId", "")
                                raw_tids = m.get("clobTokenIds", "[]")
                                clob_tids = json.loads(raw_tids) if isinstance(raw_tids, str) else raw_tids
                                if not clob_tids or len(clob_tids) < 2:
                                    continue
                                outcomes = m.get("outcomes", "[]")
                                outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                                for i, tid in enumerate(clob_tids):
                                    side = outcomes[i] if i < len(outcomes) else "?"
                                    if side.upper() == "DOWN":
                                        markets.append(dict(
                                            token_id=tid, condition_id=cid, slug=slug,
                                            asset=asset.upper(), interval=interval,
                                            side="DOWN", expires_in=ts + window - now,
                                        ))
                except Exception as e:
                    log.debug(f"Market discovery {slug}: {e}")
    log.info(f"Discovered {len(markets)} DOWN token markets")
    return markets


# ═══════════════════════════════════════════════════════════════════════
# PM WS FEED (§4, §5) — attempts connection, confirmed no book data for 5m
# ═══════════════════════════════════════════════════════════════════════
async def polymarket_ws_feed(cache: QuoteCacheV2, condition_ids: list):
    reconnects = 0
    while True:
        try:
            async with websockets.connect(CLOB_WS_URL, ping_interval=60, ping_timeout=30,
                                          open_timeout=30, close_timeout=5) as ws:
                for cid in condition_ids:
                    sub = json.dumps({"auth": {}, "markets": [cid], "type": "market"})
                    await ws.send(sub)
                cache.record_connect("polymarket_ws")
                reconnects += 1
                cache.increment_counter("reconnects")
                log.info(f"POLYMARKET_WS connected, {len(condition_ids)} markets, reconnect #{reconnects}")
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        event_type = data.get("type", data.get("event_type", ""))
                        asset_id = data.get("asset_id", data.get("token_id", ""))
                        if not asset_id:
                            continue
                        if event_type in ("book_snapshot", "book") and data.get("bids"):
                            bids = data.get("bids", [])
                            asks = data.get("asks", [])
                            best_bid = float(bids[0].get("price", 0)) if bids else 0
                            best_ask = float(asks[0].get("price", 0)) if asks else 0
                            bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
                            ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
                            spread = round(best_ask - best_bid, 4) if best_bid and best_ask else 0
                            await cache.update_polymarket(asset_id, dict(
                                best_bid=best_bid, best_ask=best_ask, spread=spread,
                                bid_depth=bid_depth, ask_depth=ask_depth,
                                book_timestamp_ms=int(time.time() * 1000),
                                market_status="active_ws",
                            ), source="ws")
                        elif event_type == "price_change":
                            price = float(data.get("price", 0))
                            if price:
                                await cache.update_polymarket(asset_id, dict(
                                    best_bid=price * 0.99, best_ask=price * 1.01,
                                    spread=round(price * 0.02, 4), mid_price=price,
                                    book_timestamp_ms=int(time.time() * 1000),
                                    market_status="active_ws_price",
                                ), source="ws")
                    except json.JSONDecodeError:
                        pass
                    await asyncio.sleep(0)  # Yield to other coroutines
        except Exception as e:
            log.debug(f"POLYMARKET_WS: {e}")
            cache.record_disconnect("polymarket_ws")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════
# EXTERNAL EXCHANGE WS FEEDS (§5)
# ═══════════════════════════════════════════════════════════════════════
async def binance_spot_feed(cache: QuoteCacheV2, asset: str):
    sym = BINANCE_SYMBOLS[asset]
    url = f"wss://stream.binance.com:9443/ws/{sym}@bookTicker"
    while True:
        try:
            async with websockets.connect(url, ping_interval=60, ping_timeout=30, open_timeout=30) as ws:
                cache.record_connect("external")
                log.info(f"BINANCE_SPOT {asset} connected")
                async for msg in ws:
                    data = json.loads(msg)
                    bid = float(data.get("b", 0))
                    ask = float(data.get("a", 0))
                    mid = (bid + ask) / 2 if bid and ask else 0
                    await cache.update_external(asset, "binance_spot", dict(
                        bid=bid, ask=ask, mid=mid, last=mid,
                        timestamp_exchange_ms=data.get("T", int(time.time() * 1000)),
                    ))
                    await asyncio.sleep(0)  # Yield to other coroutines
        except Exception as e:
            log.debug(f"BINANCE_SPOT {asset}: {e}")
            cache.record_disconnect("external")
            await asyncio.sleep(3)

async def bybit_perp_feed(cache: QuoteCacheV2, asset: str):
    sym = BYBIT_SYMBOLS[asset]
    url = "wss://stream.bybit.com/v5/public/linear"
    while True:
        try:
            async with websockets.connect(url, ping_interval=60, ping_timeout=30, open_timeout=30) as ws:
                sub = json.dumps({"op": "subscribe", "args": [f"tickers.{sym}"]})
                await ws.send(sub)
                cache.record_connect("external")
                log.info(f"BYBIT_PERP {asset} connected")
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("topic", "").startswith("tickers."):
                        d = data.get("data", {})
                        bid = float(d.get("bid1Price", 0))
                        ask = float(d.get("ask1Price", 0))
                        mid = (bid + ask) / 2 if bid and ask else 0
                        await cache.update_external(asset, "bybit_perp", dict(
                            bid=bid, ask=ask, mid=mid, last=float(d.get("lastPrice", mid)),
                            timestamp_exchange_ms=int(time.time() * 1000),
                        ))
                        await asyncio.sleep(0)  # Yield to other coroutines
        except Exception as e:
            log.debug(f"BYBIT_PERP {asset}: {e}")
            cache.record_disconnect("external")
            await asyncio.sleep(3)

async def okx_perp_feed(cache: QuoteCacheV2, asset: str):
    inst = OKX_SYMBOLS[asset]
    url = "wss://ws.okx.com:8443/ws/v5/public"
    while True:
        try:
            async with websockets.connect(url, ping_interval=60, ping_timeout=30, open_timeout=30) as ws:
                sub = json.dumps({"op": "subscribe", "args": [{"channel": "tickers", "instId": inst}]})
                await ws.send(sub)
                cache.record_connect("external")
                log.info(f"OKX_PERP {asset} connected")
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("arg", {}).get("channel") == "tickers":
                        d = data.get("data", [{}])[0]
                        bid = float(d.get("bidPx", 0))
                        ask = float(d.get("askPx", 0))
                        mid = (bid + ask) / 2 if bid and ask else 0
                        await cache.update_external(asset, "okx_perp", dict(
                            bid=bid, ask=ask, mid=mid, last=float(d.get("last", mid)),
                            timestamp_exchange_ms=int(time.time() * 1000),
                        ))
                        await asyncio.sleep(0)  # Yield to other coroutines
        except Exception as e:
            log.debug(f"OKX_PERP {asset}: {e}")
            cache.record_disconnect("external")
            await asyncio.sleep(3)

async def coinbase_spot_feed(cache: QuoteCacheV2, asset: str):
    sym = COINBASE_SYMBOLS[asset]
    url = "wss://ws-feed.exchange.coinbase.com"
    while True:
        try:
            async with websockets.connect(url, ping_interval=60, ping_timeout=30, open_timeout=30) as ws:
                sub = json.dumps({"type": "subscribe", "product_ids": [sym], "channels": ["ticker"]})
                await ws.send(sub)
                cache.record_connect("external")
                log.info(f"COINBASE_SPOT {asset} connected")
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") == "ticker":
                        bid = float(data.get("best_bid", 0))
                        ask = float(data.get("best_ask", 0))
                        mid = (bid + ask) / 2 if bid and ask else 0
                        await cache.update_external(asset, "coinbase_spot", dict(
                            bid=bid, ask=ask, mid=mid, last=float(data.get("price", mid)),
                            timestamp_exchange_ms=int(time.time() * 1000),
                        ))
                        await asyncio.sleep(0)  # Yield to other coroutines
        except Exception as e:
            log.debug(f"COINBASE_SPOT {asset}: {e}")
            cache.record_disconnect("external")
            await asyncio.sleep(3)


# ═══════════════════════════════════════════════════════════════════════
# PM REST FAST POLL (§11) — aiohttp non-blocking
# ═══════════════════════════════════════════════════════════════════════
async def pm_rest_fast_poll(cache: QuoteCacheV2, session: aiohttp.ClientSession):
    """Poll PM books via REST at 5s intervals — batch update to avoid lock churn."""
    log.info("PM REST poll: 5s intervals (WS-first, batch-update)")
    poll_cycle = 0
    while True:
        try:
            t0 = time.time()
            now = int(time.time())
            # Phase 1: Collect all data via HTTP in parallel (no lock held)
            slugs = []
            for asset in ASSETS:
                for interval in INTERVALS:
                    window = 300 if interval == "5m" else 900
                    current = (now // window) * window
                    slugs.append((asset, interval, current, f"{asset.lower()}-updown-{interval}-{current}"))

            async def fetch_slug(asset, interval, ts, slug):
                try:
                    url = f"{GAMMA_URL}/events?slug={slug}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                        data = await resp.json()
                    return (asset, interval, ts, slug, data)
                except Exception:
                    return (asset, interval, ts, slug, None)

            results = await asyncio.gather(*[fetch_slug(*s) for s in slugs])

            # Phase 2: Parse results and build updates list
            updates = []
            for asset, interval, ts, slug, data in results:
                if not data or not isinstance(data, list) or not data:
                    continue
                for ev in data:
                    for m in ev.get("markets", []):
                        cid = m.get("conditionId", "")
                        raw_tids = m.get("clobTokenIds", "[]")
                        clob_tids = json.loads(raw_tids) if isinstance(raw_tids, str) else raw_tids
                        if not clob_tids:
                            continue
                        outcomes = m.get("outcomes", "[]")
                        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                        outcome_prices = m.get("outcomePrices", "[]")
                        outcome_prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                        best_bid = float(m.get("bestBid", 0))
                        best_ask = float(m.get("bestAsk", 0))
                        for i, tid in enumerate(clob_tids):
                            side = outcomes[i] if i < len(outcomes) else "?"
                            price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
                            updates.append(dict(
                                token_id=tid,
                                book=dict(
                                    best_bid=best_bid if side == "UP" else round(1 - best_ask, 4),
                                    best_ask=best_ask if side == "UP" else round(1 - best_bid, 4),
                                    spread=round(abs(best_ask - best_bid), 4),
                                    mid_price=price,
                                    book_timestamp_ms=int(time.time() * 1000),
                                    market_status="active_rest_poll",
                                ),
                                source="rest",
                                meta=dict(slug=slug, asset=asset.upper(),
                                          interval=interval, side=side, condition_id=cid),
                            ))
            # Phase 3: Batch update — single lock acquisition
            if updates:
                await cache.batch_update_polymarket(updates)
            poll_cycle += 1
            elapsed = time.time() - t0
            n_tokens = len(cache._pm)
            if updates or poll_cycle % 3 == 0:
                log.info(f"[PM-REST cycle #{poll_cycle}] {elapsed:.1f}s, {n_tokens} tokens, {len(updates)} updates")
            await asyncio.sleep(5.0)
        except Exception as e:
            log.error(f"PM REST poll error: {e}")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════
# MOMENTUM DETECTION (§8)
# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
# SNAP-BASED ANALYSIS (pure functions, no async/lock)
# ═══════════════════════════════════════════════════════════════════════

def detect_momentum_from_snap(snap: dict) -> list:
    """Detect momentum events from pre-computed snapshot — no lock needed."""
    events = []
    pm = snap["pm"]
    ext = snap["ext"]
    for tid, ps in pm.items():
        if not ps.get("side") == "DOWN":
            continue
        best_ask = ps.get("best_ask", 0)
        if best_ask <= 0:
            continue
        if best_ask < 0.03: bucket = "0_3c"
        elif best_ask < 0.05: bucket = "3_5c"
        elif best_ask < 0.08: bucket = "5_8c"
        elif best_ask < 0.12: bucket = "8_12c"
        elif best_ask < 0.20: bucket = "12_20c"
        elif best_ask < 0.40: bucket = "20_40c"
        else: bucket = "above_40c"
        if 0.03 <= best_ask <= 0.12:
            pass  # counter tracked in cache
        ask_vel_1s = ps.get("best_ask_delta_1s", 0)
        ask_vel_3s = ps.get("best_ask_delta_3s", 0)
        ask_vel_5s = ps.get("best_ask_delta_5s", 0)
        book_vel = ps.get("book_update_velocity", 0)
        ofi = ps.get("order_flow_imbalance", 0.5)
        bid_depth_delta = ps.get("bid_depth_delta", 0)
        ask_depth_delta = ps.get("ask_depth_delta", 0)
        trade_dir = ps.get("trade_print_direction", "NEUTRAL")
        asset = ps.get("asset", "BTC")
        es = ext.get(asset)
        ext_v3s = es.get("velocities", {}).get("v3s", 0) if es else 0
        ext_v5s = es.get("velocities", {}).get("v5s", 0) if es else 0
        ext_v15s = es.get("velocities", {}).get("v15s", 0) if es else 0
        cross_median = es.get("cross_exchange_median", 0) if es else 0
        sources = es.get("sources", {}) if es else {}
        ages = [s.get("feed_age_ms", 99999) for s in sources.values()]
        ext_feed_age_ms = min(ages) if ages else 99999
        signal = "NO_MOMENTUM"
        if ask_vel_3s < -0.005 and ofi > 0.6 and ext_v3s < -0.001:
            signal = "DOWN_MOMENTUM"
        elif ask_vel_5s < -0.003 and ofi > 0.55:
            signal = "DOWN_MOMENTUM"
        elif ask_vel_3s < -0.002 and trade_dir == "SELL":
            signal = "DOWN_CONTINUATION"
        elif (0.03 <= best_ask <= 0.12) and book_vel > 10:
            signal = "NEAR_MOMENTUM"
        if signal != "NO_MOMENTUM":
            events.append(dict(
                timestamp=datetime.now(timezone.utc).isoformat(),
                token_id=tid, market_slug=ps.get("slug", ""),
                interval=ps.get("interval", ""), side="DOWN",
                bucket=bucket, best_ask=best_ask,
                book_update_velocity=book_vel,
                best_ask_delta_1s=ask_vel_1s, best_ask_delta_3s=ask_vel_3s,
                best_ask_delta_5s=ask_vel_5s,
                bid_depth_delta=bid_depth_delta, ask_depth_delta=ask_depth_delta,
                order_flow_imbalance=round(ofi, 4),
                trade_print_direction=trade_dir,
                external_v3s=ext_v3s, external_v5s=ext_v5s, external_v15s=ext_v15s,
                cross_exchange_median=cross_median, external_feed_age_ms=ext_feed_age_ms,
                signal_type=signal,
            ))
    return events

def check_armed_from_snap(snap: dict) -> list:
    """Check armed pretriggers from snapshot — no lock needed."""
    triggers = []
    pm = snap["pm"]
    ext = snap["ext"]
    for tid, ps in pm.items():
        if not ps.get("side") == "DOWN":
            continue
        best_ask = ps.get("best_ask", 0)
        book_vel = ps.get("book_update_velocity", 0)
        spread = ps.get("spread", 0)
        ask_delta_3s = ps.get("best_ask_delta_3s", 0)
        asset = ps.get("asset", "BTC")
        es = ext.get(asset)
        reasons = []
        if 0.03 <= best_ask <= 0.12: reasons.append("ask_in_live_bucket")
        if 0.12 < best_ask < 0.20 and ask_delta_3s < -0.005: reasons.append("ask_approaching_12c")
        if es:
            v3s = es.get("velocities", {}).get("v3s", 0)
            v5s = es.get("velocities", {}).get("v5s", 0)
            if v3s < -0.002 or v5s < -0.002: reasons.append("external_sharp_negative")
        if 0 < spread < 0.03: reasons.append("spread_compressed")
        if book_vel > 30: reasons.append("high_book_velocity")
        if reasons:
            triggers.append(dict(
                timestamp=datetime.now(timezone.utc).isoformat(),
                token_id=tid, market_slug=ps.get("slug", ""),
                asset=asset, best_ask=best_ask, spread=spread,
                book_update_velocity=book_vel, reasons=reasons,
            ))
    return triggers

def compute_survivability_from_snap(snap: dict) -> dict:
    """Compute survivability from snapshot — no lock needed."""
    results = {}
    pm = snap["pm"]
    ext = snap["ext"]
    for tid, ps in pm.items():
        if not ps.get("side") == "DOWN":
            continue
        best_ask = ps.get("best_ask", 0)
        best_bid = ps.get("best_bid", 0)
        ask_depth = ps.get("ask_depth", 0)
        bid_depth = ps.get("bid_depth", 0)
        spread = ps.get("spread", 0)
        book_vel = ps.get("book_update_velocity", 0)
        ofi = ps.get("order_flow_imbalance", 0.5)
        if best_ask <= 0 or best_bid <= 0:
            continue
        payout = (1 - best_ask) / best_ask if best_ask > 0 else 0
        fill_prob = min(1.0, ask_depth / 100) if ask_depth > 0 else 0
        slippage = spread / best_ask if best_ask > 0 else 1.0
        slippage_survival = max(0, 1.0 - slippage)
        if 0.03 <= best_ask <= 0.08: bucket_w = 1.0
        elif 0.08 <= best_ask <= 0.12: bucket_w = 0.8
        elif 0.12 <= best_ask <= 0.20: bucket_w = 0.3
        else: bucket_w = 0.1
        asset = ps.get("asset", "BTC")
        es = ext.get(asset)
        ext_v3s = es.get("velocities", {}).get("v3s", 0) if es else 0
        momentum_factor = 1.0 if ofi > 0.55 and ext_v3s < -0.001 else 0.5
        vel_factor = min(1.0, book_vel / 20) if book_vel > 0 else 0
        ev = payout * fill_prob * momentum_factor
        survivability = ev * slippage_survival * bucket_w * vel_factor
        results[tid] = dict(
            token_id=tid, best_ask=best_ask, payout=round(payout, 4),
            fill_prob=round(fill_prob, 4), slippage_survival=round(slippage_survival, 4),
            bucket_weight=bucket_w, momentum_factor=momentum_factor,
            book_velocity_factor=round(vel_factor, 4),
            survivability=round(survivability, 6),
            pass_fail="PASS" if survivability >= 0.05 else "FAIL",
        )
    passes = sum(1 for r in results.values() if r["pass_fail"] == "PASS")
    return dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        tokens_evaluated=len(results), survivability_passes=passes,
        false_signal_count=len(results) - passes, details=results,
    )

def build_orders_from_snap(snap: dict, markets: list) -> list:
    """Build order dry-run from snapshot — no lock needed."""
    orders = []
    pm = snap["pm"]
    for m in markets:
        if m["side"] != "DOWN":
            continue
        tid = m["token_id"]
        ps = pm.get(tid, {})
        best_ask = ps.get("best_ask", 0) if ps else 0
        if best_ask <= 0: best_ask = 0.05
        orders.append(dict(
            asset=m["asset"], condition_id=m["condition_id"],
            DOWN_token_id=tid, slug=m["slug"], interval=m["interval"],
            side="DOWN", size_usd=1.0, limit_price=round(best_ask, 4),
            order_type="FOK", best_ask_at_build=best_ask,
            timestamp=datetime.now(timezone.utc).isoformat(),
            note="DRY_RUN_ONLY_NO_REAL_ORDERS",
        ))
    return orders

def evaluate_readiness_from_snap(snap: dict, runtime_seconds: float) -> dict:
    """Evaluate scanner readiness from snapshot — no lock needed."""
    h = snap["health"]
    pm_ws_books_seen = h.get("pm_ws_books_seen", False)
    pm_p50 = h.get("pm_p50_age_ms", 99999)
    pm_p95 = h.get("pm_p95_age_ms", 99999)
    ext_p50 = h.get("ext_p50_age_ms", 99999)
    ext_p95 = h.get("ext_p95_age_ms", 99999)
    tokens = h.get("polymarket_tokens_tracked", 0)
    pm_ws_flowing = pm_ws_books_seen and pm_p95 < 5000
    pm_rest_fast = pm_p50 < 6000
    if not pm_ws_flowing and not pm_rest_fast:
        classification = "V21.7.13_REALTIME_SCANNER_NOT_READY"
        next_step = "No PM book data flowing — neither WS nor REST-fast-poll"
    elif not pm_ws_flowing and pm_rest_fast:
        classification = "PM_BOOK_FEED_DEGRADED_LIVE_BLOCKED"
        next_step = f"PM WS not flowing for 5m markets — REST-fast-poll p95={pm_p95}ms. External WS p95={ext_p95}ms."
    elif ext_p95 > 500:
        classification = "EXTERNAL_FEED_DEGRADED"
        next_step = f"External feed p95={ext_p95}ms exceeds 500ms"
    elif runtime_seconds < 21600:
        classification = "V21.7.13_REALTIME_SCANNER_BUILD"
        next_step = f"Runtime {runtime_seconds/3600:.1f}h — need 6h minimum"
    else:
        classification = "V21.7.13_REALTIME_SCANNER_READY"
        next_step = "All scanner readiness gates passed"
    rest_fallback = not pm_ws_flowing or pm_p95 > 5000
    return dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        runtime_seconds=round(runtime_seconds, 1),
        pm_ws_books_seen=pm_ws_books_seen, pm_ws_book_update_rate=h.get("pm_ws_book_updates", 0) / max(runtime_seconds / 60, 1),
        pm_book_p50_age_ms=pm_p50, pm_book_p95_age_ms=pm_p95,
        external_feed_p50_age_ms=ext_p50, external_feed_p95_age_ms=ext_p95,
        polymarket_tokens_tracked=tokens,
        momentum_events_detected=0, bucket_touch_events=0,
        armed_mode_pretriggers=0,
        survivability_passes=0, false_signal_count=0,
        scanner_latency_p50_ms=0, scanner_latency_p95_ms=0,
        rest_fallback_dependency=rest_fallback,
        classification=classification, next_step=next_step,
    )

def evaluate_micro_live_gate(readiness: dict, health: dict) -> dict:
    """Evaluate micro-live gate — pure function."""
    pm_ws_flowing = readiness.get("pm_ws_books_seen", False) and readiness.get("pm_book_p95_age_ms", 99999) < 5000
    pm_rest_fast = readiness.get("pm_book_p50_age_ms", 99999) < 6000
    pm_books_ok = pm_ws_flowing or pm_rest_fast
    gates = dict(
        scanner_readiness_passed=readiness["classification"] == "V21.7.13_REALTIME_SCANNER_READY",
        pm_books_available=pm_books_ok,
        external_ws_real_time=readiness["external_feed_p95_age_ms"] <= 500,
        realtime_momentum_active=readiness["momentum_events_detected"] > 0,
        survivability_from_live_book=readiness["survivability_passes"] > 0,
        mode_integrity_passed=True,
    )
    all_pass = all(gates.values())
    return dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        gates=gates, micro_live_allowed=all_pass,
        classification="MICRO_LIVE_UNLOCKED" if all_pass else "MICRO_LIVE_BLOCKED_PENDING_WS_SCANNER",
        blocking_reasons=[k for k, v in gates.items() if not v],
    )

def evaluate_scalper_gate(readiness: dict, health: dict) -> dict:
    """Evaluate scalper gate — pure function."""
    pm_ws_flowing = readiness.get("pm_ws_books_seen", False) and readiness.get("pm_book_p95_age_ms", 99999) < 5000
    pm_rest_fast = readiness.get("pm_book_p50_age_ms", 99999) < 6000
    pm_books_ok = pm_ws_flowing or pm_rest_fast
    gates = dict(
        pm_books_available=pm_books_ok,
        exit_bid_depth_visible=readiness["polymarket_tokens_tracked"] > 0,
        external_feed_p95_ok=readiness["external_feed_p95_age_ms"] <= 500,
        pm_book_p95_ok=readiness["pm_book_p95_age_ms"] < 6000,
        lag_events_measurable=True, quote_age_violations=0,
    )
    all_pass = all(gates.values())
    return dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        gates=gates, scalper_paper_live_allowed=all_pass,
        classification="SCALPER_UNLOCKED" if all_pass else "SCALPER_BLOCKED_FEED_NOT_READY",
        blocking_reasons=[k for k, v in gates.items() if not v],
    )


# ═══════════════════════════════════════════════════════════════════════
# SCANNER LOOP
# ═══════════════════════════════════════════════════════════════════════
async def scanner_loop(cache: QuoteCacheV2, markets: list):
    log.info("Scanner loop started")
    start_time = time.time()
    momentum_log = open(OUT / "realtime_momentum_events.jsonl", "a")
    bucket_log = open(OUT / "bucket_touch_events.jsonl", "a")
    armed_log = open(OUT / "armed_mode_pretrigger_events.jsonl", "a")
    scan_count = 0
    while True:
        try:
            scan_start = time.time()
            runtime = scan_start - start_time
            # SINGLE lock acquisition for the entire scan cycle
            snap = await cache.snapshot_all()
            n_tokens = len(snap["tokens"])
            if scan_count % 3 == 0:
                log.info(f"[scan #{scan_count}] tokens={n_tokens} runtime={runtime:.0f}s snap_t={time.time()-scan_start:.2f}s")
            momentum_events = detect_momentum_from_snap(snap)
            mom_t = time.time() - scan_start
            for evt in momentum_events:
                momentum_log.write(json.dumps(evt) + "\n")
                momentum_log.flush()
                if 0.03 <= evt.get("best_ask", 1) <= 0.12:
                    bucket_log.write(json.dumps(evt) + "\n")
                    bucket_log.flush()
            pretriggers = check_armed_from_snap(snap)
            for pt in pretriggers:
                armed_log.write(json.dumps(pt) + "\n")
                armed_log.flush()
            # Heavy ops only every 3rd cycle
            if scan_count % 3 == 0:
                surv = compute_survivability_from_snap(snap)
            else:
                surv = dict(survivability_passes=0, false_signal_count=0, details={})
            with open(OUT / "survivability_realtime_report.json", "w") as f:
                json.dump(surv, f, indent=2)
            if scan_count % 3 == 0:
                orders = build_orders_from_snap(snap, markets)
                with open(OUT / "order_path_dry_run.json", "w") as f:
                    json.dump(dict(timestamp=datetime.now(timezone.utc).isoformat(),
                                   orders=orders, note="DRY_RUN_ONLY"), f, indent=2)
            if scan_count % 3 == 0:
                health = snap["health"]
                readiness = evaluate_readiness_from_snap(snap, runtime)
                readiness["survivability_passes"] = surv.get("survivability_passes", 0)
                readiness["false_signal_count"] = surv.get("false_signal_count", 0)
                with open(OUT / "scanner_readiness_report.json", "w") as f:
                    json.dump(readiness, f, indent=2)
                ml_gate = evaluate_micro_live_gate(readiness, health)
                with open(OUT / "micro_live_unlock_gate.json", "w") as f:
                    json.dump(ml_gate, f, indent=2)
                sc_gate = evaluate_scalper_gate(readiness, health)
                with open(OUT / "scalper_unlock_gate.json", "w") as f:
                    json.dump(sc_gate, f, indent=2)
                log.info(f"[{runtime/3600:.2f}h] readiness={readiness['classification']} "
                         f"pm_ws={readiness['pm_ws_books_seen']} "
                         f"pm_p95={readiness['pm_book_p95_age_ms']}ms "
                         f"ext_p95={readiness['external_feed_p95_age_ms']}ms "
                         f"momentum={readiness['momentum_events_detected']} "
                         f"tokens={readiness['polymarket_tokens_tracked']}")
            scan_count += 1
            if scan_count % 5 == 0:
                log.info(f"[scan #{scan_count}] tokens={n_tokens} momentum={len(momentum_events)} pretriggers={len(pretriggers)} surv_pass={surv.get('survivability_passes',0)} elapsed={time.time()-scan_start:.2f}s")
            elapsed = time.time() - scan_start
            sleep_time = 1.0 if pretriggers else max(1.0, 5.0 - elapsed)
            log.info(f"[scan #{scan_count}] done in {elapsed:.2f}s, sleeping {sleep_time:.1f}s")
            await asyncio.sleep(sleep_time)
        except Exception as e:
            log.error(f"Scanner loop error: {e}")
            import traceback; log.error(traceback.format_exc())
            await asyncio.sleep(5)


async def snapshot_saver(cache: QuoteCacheV2):
    """Lightweight periodic snapshot saver — runs independently of scanner loop."""
    log.info("Snapshot saver started (every 10s)")
    while True:
        await asyncio.sleep(10)
        try:
            await cache.save_snapshots()
        except Exception as e:
            log.error(f"Snapshot save error: {e}")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
async def main():
    log.info("=" * 60)
    log.info("V21.7.13 Real-Time Momentum Scanner — WebSocket-First (async)")
    log.info("Classification: V21.7.13_REALTIME_SCANNER_BUILD")
    log.info("Micro-live: BLOCKED pending WS scanner readiness")
    log.info("=" * 60)
    cache = QuoteCacheV2()
    async with aiohttp.ClientSession() as session:
        markets = await discover_active_markets(session)
        if not markets:
            log.error("No active markets found — cannot start scanner")
            return
        condition_ids = []
        for m in markets:
            await cache.register_market(m["token_id"], dict(
                slug=m["slug"], asset=m["asset"], interval=m["interval"],
                side=m["side"], condition_id=m["condition_id"],
            ))
            if m["condition_id"] not in condition_ids:
                condition_ids.append(m["condition_id"])
        log.info(f"Tracking {len(markets)} DOWN token markets, {len(condition_ids)} condition IDs")
        tasks = []
        for asset in ASSETS:
            tasks.append(asyncio.create_task(binance_spot_feed(cache, asset)))
            tasks.append(asyncio.create_task(bybit_perp_feed(cache, asset)))
            tasks.append(asyncio.create_task(okx_perp_feed(cache, asset)))
            tasks.append(asyncio.create_task(coinbase_spot_feed(cache, asset)))
        tasks.append(asyncio.create_task(polymarket_ws_feed(cache, condition_ids)))
        tasks.append(asyncio.create_task(pm_rest_fast_poll(cache, session)))
        tasks.append(asyncio.create_task(scanner_loop(cache, markets)))
        tasks.append(asyncio.create_task(snapshot_saver(cache)))
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            log.info("Scanner shutting down")

if __name__ == "__main__":
    asyncio.run(main())
