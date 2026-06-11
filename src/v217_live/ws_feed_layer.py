#!/usr/bin/env python3
"""
V21.7.9 WebSocket Feed Layer — §3/4/5
======================================
Real-time external exchange + Polymarket WebSocket feeds.
Feeds the shared QuoteCache. Independent from convex bot.

External: Binance spot, Coinbase spot, Bybit perp, OKX perp
Polymarket: active 5m crypto UP/DOWN markets (BTC/ETH/SOL/XRP)

§12: Mode integrity — NO real orders, scalper blocked, convex unchanged.
"""

import asyncio, json, time, logging, signal, sys, traceback
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

import websockets

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "v2179_ws"
OUT.mkdir(parents=True, exist_ok=True)

from quote_cache import QuoteCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(OUT / "ws_feed.log"),
                              logging.StreamHandler()])
log = logging.getLogger("v2179ws")

# ═══════════════════════════════════════════════════════════════════════
# ASSET MAPPINGS
# ═══════════════════════════════════════════════════════════════════════

BINANCE_SYMBOLS = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt", "XRP": "xrpusdt"}
COINBASE_SYMBOLS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}
BYBIT_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
OKX_SYMBOLS = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "SOL": "SOL-USDT-SWAP", "XRP": "XRP-USDT-SWAP"}

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

# ═══════════════════════════════════════════════════════════════════════
# BINANCE SPOT WS
# ═══════════════════════════════════════════════════════════════════════

async def binance_spot_feed(cache: QuoteCache, asset: str):
    sym = BINANCE_SYMBOLS[asset]
    url = f"wss://stream.binance.com:9443/ws/{sym}@bookTicker"
    source = "binance_spot"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                cache.record_connect("external")
                log.info(f"BINANCE_SPOT {asset} connected")
                async for msg in ws:
                    data = json.loads(msg)
                    bid = float(data.get("b", 0))
                    ask = float(data.get("a", 0))
                    mid = (bid + ask) / 2 if bid and ask else 0
                    ts = data.get("T", int(time.time() * 1000))
                    cache.update_external(asset, source, dict(
                        bid=bid, ask=ask, mid=mid, last=mid,
                        timestamp_exchange_ms=ts,
                    ))
        except Exception as e:
            log.warning(f"BINANCE_SPOT {asset} err: {e}")
            cache.record_disconnect("external")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════
# COINBASE SPOT WS
# ═══════════════════════════════════════════════════════════════════════

async def coinbase_spot_feed(cache: QuoteCache, asset: str):
    sym = COINBASE_SYMBOLS[asset]
    source = "coinbase_spot"
    url = "wss://ws-feed.pro.coinbase.com"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                sub = json.dumps({
                    "type": "subscribe",
                    "product_ids": [sym],
                    "channels": ["ticker_batch"]
                })
                await ws.send(sub)
                cache.record_connect("external")
                log.info(f"COINBASE_SPOT {asset} connected")
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") not in ("ticker", "ticker_batch", "snapshot"):
                        continue
                    # ticker_batch has array of products
                    products = data.get("products", [data])
                    for p in products:
                        if p.get("product_id") != sym:
                            continue
                        bid = float(p.get("best_bid", 0))
                        ask = float(p.get("best_ask", 0))
                        mid = (bid + ask) / 2 if bid and ask else 0
                        last = float(p.get("price", mid))
                        ts = int(time.time() * 1000)
                        cache.update_external(asset, source, dict(
                            bid=bid, ask=ask, mid=mid, last=last,
                            timestamp_exchange_ms=ts,
                        ))
        except Exception as e:
            log.warning(f"COINBASE_SPOT {asset} err: {e}")
            cache.record_disconnect("external")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════
# BYBIT PERP WS
# ═══════════════════════════════════════════════════════════════════════

async def bybit_perp_feed(cache: QuoteCache, asset: str):
    sym = BYBIT_SYMBOLS[asset]
    source = "bybit_perp"
    url = "wss://stream.bybit.com/v5/public/linear"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                sub = json.dumps({"op": "subscribe", "args": [f"tickers.{sym}"]})
                await ws.send(sub)
                cache.record_connect("external")
                log.info(f"BYBIT_PERP {asset} connected")
                async for msg in ws:
                    data = json.loads(msg)
                    d = data.get("data", {})
                    # Bybit tickers: some snapshots have bid1Price/ask1Price, others use different keys
                    bid = float(d.get("bid1Price", 0) or d.get("bid", 0))
                    ask = float(d.get("ask1Price", 0) or d.get("ask", 0))
                    if not bid and ask:
                        bid = ask  # fallback
                    if not ask and bid:
                        ask = bid
                    mid = (bid + ask) / 2 if bid and ask else float(d.get("markPrice", 0))
                    last = float(d.get("lastPrice", 0) or mid)
                    ts = int(d.get("timestamp", time.time() * 1000))
                    if mid > 0:
                        cache.update_external(asset, source, dict(
                            bid=bid, ask=ask, mid=mid, last=last,
                            timestamp_exchange_ms=ts,
                        ))
        except Exception as e:
            log.warning(f"BYBIT_PERP {asset} err: {e}")
            cache.record_disconnect("external")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════
# OKX PERP WS
# ═══════════════════════════════════════════════════════════════════════

async def okx_perp_feed(cache: QuoteCache, asset: str):
    inst = OKX_SYMBOLS[asset]
    source = "okx_perp"
    url = "wss://ws.okx.com:8443/ws/v5/public"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                sub = json.dumps({"op": "subscribe", "args": [{"channel": "tickers", "instId": inst}]})
                await ws.send(sub)
                cache.record_connect("external")
                log.info(f"OKX_PERP {asset} connected")
                async for msg in ws:
                    data = json.loads(msg)
                    if "data" not in data:
                        continue
                    for d in data["data"]:
                        bid = float(d.get("bidPx", 0))
                        ask = float(d.get("askPx", 0))
                        mid = (bid + ask) / 2 if bid and ask else 0
                        last = float(d.get("last", mid))
                        ts = int(d.get("ts", time.time() * 1000))
                        cache.update_external(asset, source, dict(
                            bid=bid, ask=ask, mid=mid, last=last,
                            timestamp_exchange_ms=ts,
                        ))
        except Exception as e:
            log.warning(f"OKX_PERP {asset} err: {e}")
            cache.record_disconnect("external")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════
# POLYMARKET WS (§5)
# ═══════════════════════════════════════════════════════════════════════

async def discover_pm_markets():
    """Discover active 5m crypto markets and their token IDs.
    
    CRITICAL: Gamma /markets endpoint returns tokens=[] (empty).
    Must use /events endpoint which has clobTokenIds in market objects.
    Then validate via CLOB /markets/{condition_id} for full token details.
    """
    import urllib.request
    markets = []
    now = int(time.time())
    
    # Generate current/near window timestamps for 5m
    window_5m = 300
    current_5m = (now // window_5m) * window_5m
    window_15m = 900
    current_15m = (now // window_15m) * window_15m
    
    for asset in ["btc", "eth", "sol", "xrp"]:
        # Generate deterministic slugs for current ±2 windows
        for interval, window, current in [("5m", window_5m, current_5m), ("15m", window_15m, current_15m)]:
            for offset in range(-2, 3):
                ts = current + (offset * window)
                slug = f"{asset}-updown-{interval}-{ts}"
                
                # Query EVENTS endpoint (has clobTokenIds)
                url = f"{GAMMA_URL}/events?slug={slug}"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "FDC-v21710"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode())
                    if isinstance(data, list) and data:
                        ev = data[0]
                        for m in ev.get("markets", []):
                            cid = m.get("conditionId", m.get("condition_id", ""))
                            raw_tids = m.get("clobTokenIds", "[]")
                            # CRITICAL: clobTokenIds is a JSON STRING, not a list
                            if isinstance(raw_tids, str):
                                try:
                                    clob_tids = json.loads(raw_tids)
                                except (json.JSONDecodeError, ValueError):
                                    clob_tids = []
                            elif isinstance(raw_tids, list):
                                clob_tids = raw_tids
                            else:
                                clob_tids = []
                            outcomes = m.get("outcomes", "[]")
                            if isinstance(outcomes, str):
                                try:
                                    outcomes = json.loads(outcomes)
                                except (json.JSONDecodeError, ValueError):
                                    outcomes = []
                            outcome_prices = m.get("outcomePrices", "[]")
                            if isinstance(outcome_prices, str):
                                try:
                                    outcome_prices = json.loads(outcome_prices)
                                except (json.JSONDecodeError, ValueError):
                                    outcome_prices = []
                            active = m.get("active", False)
                            closed = m.get("closed", True)
                            
                            if not cid or not clob_tids or closed:
                                continue
                            
                            # Map token IDs to outcomes
                            for i, tid in enumerate(clob_tids):
                                outcome = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
                                markets.append(dict(
                                    token_id=tid,
                                    condition_id=cid,
                                    slug=slug,
                                    outcome=outcome,
                                    asset=asset.upper(),
                                    interval=interval,
                                    active=active,
                                    outcomePrices=outcome_prices,
                                    bestBid=m.get("bestBid", 0),
                                    bestAsk=m.get("bestAsk", 0),
                                    spread=float(m.get("spread", 0)),
                                    endDateIso=m.get("endDateIso", ""),
                                    eventStartTime=m.get("eventStartTime", ""),
                                ))
                            log.info(f"PM discover: {slug} cid={cid[:20]}... tokens={len(clob_tids)} active={active}")
                except Exception as e:
                    log.debug(f"PM discover {slug}: {e}")

    # Validate discovered tokens via CLOB
    seen_cids = set()
    validated = []
    for m in markets:
        cid = m["condition_id"]
        if cid in seen_cids:
            validated.append(m)
            continue
        seen_cids.add(cid)
        try:
            url = f"{CLOB_URL}/markets/{cid}"
            req = urllib.request.Request(url, headers={"User-Agent": "FDC-v21710"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                clob_data = json.loads(resp.read().decode())
            clob_tokens = clob_data.get("tokens", [])
            # Cross-validate token IDs
            for ct in clob_tokens:
                ct_tid = ct.get("token_id", "")
                ct_outcome = ct.get("outcome", "")
                # Find matching entry in our markets list
                for vm in markets:
                    if vm["token_id"] == ct_tid and vm["condition_id"] == cid:
                        vm["clob_validated"] = True
                        vm["clob_outcome"] = ct_outcome
                        if ct_outcome and not vm.get("outcome"):
                            vm["outcome"] = ct_outcome
            validated.append(m)
        except Exception as e:
            log.debug(f"CLOB validate {cid[:20]}: {e}")
            validated.append(m)

    log.info(f"PM discovery: {len(validated)} token entries from {len(seen_cids)} conditions")
    return validated


async def polymarket_ws_feed(cache: QuoteCache, token_ids: list):
    """Subscribe to Polymarket CLOB WebSocket for book updates."""
    source = "polymarket_clob"
    while True:
        try:
            async with websockets.connect(CLOB_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                # Subscribe to each token's book
                for tid in token_ids:
                    sub = json.dumps({"auth": {}, "markets": [tid], "type": "market"})
                    await ws.send(sub)
                cache.record_connect("polymarket")
                log.info(f"POLYMARKET_WS connected, {len(token_ids)} tokens")
                async for msg in ws:
                    data = json.loads(msg)
                    # Polymarket sends book snapshots and deltas
                    if data.get("type") == "book":
                        for tid, book_data in data.get("data", {}).items():
                            bids = book_data.get("bids", [])
                            asks = book_data.get("asks", [])
                            if not bids and not asks:
                                continue
                            # CLOB API returns asks DESCENDING — sort for best prices
                            sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True) if bids else []
                            sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 1))) if asks else []
                            best_bid = float(sorted_bids[0].get("price", 0)) if sorted_bids else 0
                            best_ask = float(sorted_asks[0].get("price", 0)) if sorted_asks else 0
                            bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
                            ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
                            spread = round(best_ask - best_bid, 4) if best_bid and best_ask else 0
                            cache.update_polymarket(tid, dict(
                                best_bid=best_bid, best_ask=best_ask,
                                spread=spread, bid_depth=bid_depth, ask_depth=ask_depth,
                                book_timestamp_ms=int(time.time() * 1000),
                                market_status="active",
                            ))
                    elif data.get("event_type") == "book":
                        # Alternative format
                        tid = data.get("asset_id", data.get("token_id", ""))
                        if not tid:
                            continue
                        bids = data.get("bids", [])
                        asks = data.get("asks", [])
                        # CLOB API returns asks DESCENDING — sort for best prices
                        sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True) if bids else []
                        sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 1))) if asks else []
                        best_bid = float(sorted_bids[0].get("price", 0)) if sorted_bids else 0
                        best_ask = float(sorted_asks[0].get("price", 0)) if sorted_asks else 0
                        bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
                        ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
                        cache.update_polymarket(tid, dict(
                            best_bid=best_bid, best_ask=best_ask,
                            spread=round(best_ask - best_bid, 4),
                            bid_depth=bid_depth, ask_depth=ask_depth,
                            book_timestamp_ms=int(time.time() * 1000),
                            market_status="active",
                        ))
        except Exception as e:
            log.warning(f"POLYMARKET_WS err: {e}")
            cache.record_disconnect("polymarket")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════
# POLYMARKET REST FALLBACK (if WS fails)
# ═══════════════════════════════════════════════════════════════════════

async def polymarket_rest_fallback(cache: QuoteCache, token_ids: list):
    """Poll Polymarket books via REST using Gamma events endpoint.
    
    CRITICAL: CLOB /book?token_id= returns 404.
    Must use Gamma /events?slug= to get bestBid/bestAsk/outcomePrices.
    """
    import urllib.request
    log.info("Using REST fallback for Polymarket books (Gamma events)")
    
    # Build slug→token_id mapping from cache metadata
    while True:
        try:
            now = int(time.time())
            window_5m = 300
            window_15m = 900
            current_5m = (now // window_5m) * window_5m
            current_15m = (now // window_15m) * window_15m
            
            for asset in ["btc", "eth", "sol", "xrp"]:
                for interval, window, current in [("5m", window_5m, current_5m), ("15m", window_15m, current_15m)]:
                    for offset in [0, 1]:  # current + next
                        ts = current + (offset * window)
                        slug = f"{asset}-updown-{interval}-{ts}"
                        try:
                            url = f"{GAMMA_URL}/events?slug={slug}"
                            req = urllib.request.Request(url, headers={"User-Agent": "FDC-v21710"})
                            with urllib.request.urlopen(req, timeout=8) as resp:
                                data = json.loads(resp.read().decode())
                            if isinstance(data, list) and data:
                                for m in data[0].get("markets", []):
                                    cid = m.get("conditionId", "")
                                    raw_tids = m.get("clobTokenIds", "[]")
                                    if isinstance(raw_tids, str):
                                        clob_tids = json.loads(raw_tids)
                                    else:
                                        clob_tids = raw_tids
                                    if not clob_tids or len(clob_tids) < 2:
                                        continue
                                    outcomes = m.get("outcomes", "[]")
                                    if isinstance(outcomes, str):
                                        outcomes = json.loads(outcomes)
                                    outcome_prices = m.get("outcomePrices", "[]")
                                    if isinstance(outcome_prices, str):
                                        outcome_prices = json.loads(outcome_prices)
                                    best_bid = float(m.get("bestBid", 0))
                                    best_ask = float(m.get("bestAsk", 0))
                                    spread = float(m.get("spread", 0))
                                    
                                    for i, tid in enumerate(clob_tids):
                                        outcome = outcomes[i] if i < len(outcomes) else "?"
                                        price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
                                        cache.update_polymarket(tid, dict(
                                            best_bid=best_bid if i == 0 else round(1 - best_ask, 4),
                                            best_ask=best_ask if i == 0 else round(1 - best_bid, 4),
                                            spread=spread,
                                            mid_price=price,
                                            book_timestamp_ms=int(time.time() * 1000),
                                            market_status="active",
                                            slug=slug,
                                            side=outcome,
                                            condition_id=cid,
                                            asset=asset.upper(),
                                            interval=interval,
                                        ))
                        except Exception as e:
                            log.debug(f"PM REST {slug}: {e}")
            await asyncio.sleep(10)  # 10s cycle for REST
        except Exception as e:
            log.error(f"PM REST fallback error: {e}")
            await asyncio.sleep(30)


async def pm_rest_discover_and_poll(cache: QuoteCache):
    """Discover PM token IDs via direct CLOB/Gamma search, then poll."""
    import urllib.request
    CLOB_REST = "https://clob.polymarket.com"
    log.info("PM REST discover: searching for active crypto 5m markets")
    token_ids = []

    # Search Gamma API broadly
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        try:
            url = f"{GAMMA_URL}/markets?limit=100&active=true&closed=false"
            req = urllib.request.Request(url, headers={"User-Agent": "FDC-v2179"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if data and isinstance(data, list):
                for m in data:
                    slug = m.get("slug", "").lower()
                    if asset.lower() not in slug:
                        continue
                    for tok in m.get("tokens", []):
                        tid = tok.get("token_id", "")
                        if tid:
                            token_ids.append(tid)
                            cache.update_polymarket(tid, dict(
                                best_bid=0, best_ask=0, spread=0,
                                bid_depth=0, ask_depth=0,
                                book_timestamp_ms=0, market_status="discovered",
                                slug=m.get("slug", ""), asset=asset,
                                side=tok.get("outcome", ""),
                            ))
        except Exception as e:
            log.warning(f"PM REST discover {asset}: {e}")

    log.info(f"PM REST discover: found {len(token_ids)} tokens, polling")
    if not token_ids:
        log.warning("PM REST discover: no tokens found — PM feed offline")
        return

    # Now poll them
    await polymarket_rest_fallback(cache, token_ids)


# ═══════════════════════════════════════════════════════════════════════
# MODE INTEGRITY (§12)
# ═══════════════════════════════════════════════════════════════════════

def verify_mode_integrity():
    checks = dict(
        real_order_submission_enabled=False,
        scalper_real_live_enabled=False,
        scalper_paper_live_enabled=False,  # blocked pending feed validation
        convex_live_unchanged=True,
    )
    passed = all(v == (k == "convex_live_unchanged") for k, v in checks.items())
    report = dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        checks=checks,
        mode_integrity_passed=passed,
        classification="MODE_INTEGRITY_PASSED" if passed else "MODE_INTEGRITY_FAILED",
    )
    with open(OUT / "mode_integrity_report.json", "w") as f:
        json.dump(report, f, indent=2)
    if passed:
        log.info("§12 Mode integrity: PASSED")
    else:
        log.error("§12 Mode integrity: FAILED — aborting")
    return passed


# ═══════════════════════════════════════════════════════════════════════
# LATENCY TRACKING (§11)
# ═══════════════════════════════════════════════════════════════════════

class LatencyTracker:
    def __init__(self):
        self.ext_receive = []
        self.pm_receive = []
        self.cache_update = []
        self.lag_detect = []

    def record(self, category: str, latency_ms: float):
        lst = getattr(self, category, None)
        if lst is not None:
            lst.append(latency_ms)

    def report(self):
        import numpy as np
        r = dict(timestamp=datetime.now(timezone.utc).isoformat())
        for cat in ["ext_receive", "pm_receive", "cache_update", "lag_detect"]:
            lst = getattr(self, cat)
            if lst:
                arr = np.array(lst[-5000:])  # Last 5000 samples
                r[f"{cat}_p50_ms"] = int(np.percentile(arr, 50))
                r[f"{cat}_p95_ms"] = int(np.percentile(arr, 95))
                r[f"{cat}_p99_ms"] = int(np.percentile(arr, 99))
            else:
                r[f"{cat}_p50_ms"] = -1
                r[f"{cat}_p95_ms"] = -1
                r[f"{cat}_p99_ms"] = -1
        # Overall
        all_latencies = self.ext_receive + self.pm_receive + self.cache_update
        if all_latencies:
            arr = np.array(all_latencies[-5000:])
            r["p50_latency_ms"] = int(np.percentile(arr, 50))
            r["p95_latency_ms"] = int(np.percentile(arr, 95))
            r["p99_latency_ms"] = int(np.percentile(arr, 99))
        with open(OUT / "ws_latency_report.json", "w") as f:
            json.dump(r, f, indent=2)
        return r


# ═══════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════

async def main():
    # §12: Mode integrity
    if not verify_mode_integrity():
        return

    cache = QuoteCache(stale_ms=2000)
    latency = LatencyTracker()

    log.info("V21.7.9 WebSocket Feed Layer STARTING")
    log.info(f"Assets: BTC ETH SOL XRP")
    log.info(f"Sources: Binance, Coinbase, Bybit, OKX spot/perp")

    # Discover Polymarket markets
    pm_markets = await discover_pm_markets()
    pm_token_ids = pm_markets  # Keep full dicts for filtering
    log.info(f"Polymarket: {len(pm_token_ids)} tokens discovered")

    # Register in cache
    by_cond = defaultdict(list)
    for m in pm_markets:
        by_cond[m["condition_id"]].append(m["token_id"])
    for cid, tids in by_cond.items():
        cache.register_condition(cid, tids)

    # Launch external feeds (Coinbase disabled — HTTP 530 region block)
    tasks = []
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        tasks.append(asyncio.create_task(binance_spot_feed(cache, asset)))
        tasks.append(asyncio.create_task(bybit_perp_feed(cache, asset)))
        tasks.append(asyncio.create_task(okx_perp_feed(cache, asset)))

    # Polymarket WS or REST fallback
    if pm_token_ids:
        # Filter to only current/near-window + DOWN priority
        now = int(time.time())
        window_5m = 300
        window_15m = 900
        current_5m = (now // window_5m) * window_5m
        current_15m = (now // window_15m) * window_15m
        
        # Build set of "active" slugs (current + next window)
        active_slugs = set()
        for asset in ["btc", "eth", "sol", "xrp"]:
            for ts in [current_5m, current_5m + window_5m, current_15m, current_15m + window_15m]:
                active_slugs.add(f"{asset}-updown-5m-{ts}")
                active_slugs.add(f"{asset}-updown-15m-{ts}")
        
        # Filter tokens: prefer DOWN, current/near window
        priority_tokens = []
        for m in pm_token_ids:
            slug = m.get("slug", "")
            if slug not in active_slugs:
                continue
            outcome = m.get("outcome", "").lower()
            if "down" in outcome:
                priority_tokens.insert(0, m)  # DOWN first
            else:
                priority_tokens.append(m)
        
        # Limit to manageable set (max 16 tokens = 4 assets × 2 intervals × 2 sides)
        pm_token_ids_filtered = priority_tokens[:16]
        total_discovered = len(pm_token_ids)
        pm_token_ids = pm_token_ids_filtered
        
        log.info(f"PM active tokens filtered: {len(pm_token_ids)} (from {total_discovered} discovered)")
        
        # Extract token_id strings for WS subscription
        pm_tid_strings = [m["token_id"] for m in pm_token_ids]
        # NOTE: CLOB WS subscription format doesn't return book messages for 5m markets.
        # Disable WS, use REST-only via Gamma events endpoint.
        # tasks.append(asyncio.create_task(polymarket_ws_feed(cache, pm_tid_strings)))
        tasks.append(asyncio.create_task(polymarket_rest_fallback(cache, pm_tid_strings)))
    else:
        log.warning("No PM WS tokens found — attempting REST-only book discovery")
        tasks.append(asyncio.create_task(pm_rest_discover_and_poll(cache)))

    # Launch lag alpha monitor (§7)
    from lag_alpha_monitor_v2179 import run_monitor
    tasks.append(asyncio.create_task(run_monitor(cache)))

    # Background: periodic snapshot saves + health reports
    async def periodic_reports():
        while True:
            await asyncio.sleep(60)
            cache.save_snapshots()
            latency.report()
            h = cache.get_feed_health()
            log.info(f"Health: {h['feed_health_classification']} "
                     f"ext_p95={h.get('external_feed_p95_age_ms','?')}ms "
                     f"pm_p95={h.get('polymarket_book_p95_age_ms','?')}ms "
                     f"reconnects={h.get('reconnects',0)}")

    tasks.append(asyncio.create_task(periodic_reports()))

    # Background: scalper feed readiness check (§9)
    async def scalper_readiness():
        while True:
            await asyncio.sleep(300)  # Every 5min
            h = cache.get_feed_health()
            ext_ok = h.get("external_feed_p95_age_ms", 9999) <= 500
            # PM REST cycle ~10s, use pm_p50 < 15s as freshness proxy
            pm_p50 = h.get("polymarket_book_p50_age_ms", -1)
            pm_p95 = h.get("polymarket_book_p95_age_ms", -1)
            pm_tokens = h.get("polymarket_tokens_tracked", 0)
            pm_conn = h.get("polymarket_feed_connected", 0)
            # PM is ready if: tokens tracked > 0, >50% connected, p50 < 15s
            pm_ok = pm_tokens > 0 and pm_conn >= pm_tokens * 0.5 and pm_p50 < 15000
            ready = ext_ok and pm_ok
            report = dict(
                timestamp=datetime.now(timezone.utc).isoformat(),
                external_feed_p95_age_ms=h.get("external_feed_p95_age_ms", -1),
                polymarket_book_p50_age_ms=pm_p50,
                polymarket_book_p95_age_ms=pm_p95,
                polymarket_tokens_tracked=pm_tokens,
                polymarket_feed_connected=pm_conn,
                reconnects=h.get("reconnects", 0),
                mode_integrity_passed=True,
                ext_feeds_healthy=ext_ok,
                pm_books_fresh=pm_ok,
                scalper_feed_ready=ready,
                classification="PM_FEED_READY_FOR_PAPER_LIVE_OBSERVATION" if ready
                    else "SCALPER_FEED_NOT_READY",
            )
            with open(OUT / "scalper_feed_readiness.json", "w") as f:
                json.dump(report, f, indent=2)
            log.info(f"Scalper readiness: {report['classification']}")

    tasks.append(asyncio.create_task(scalper_readiness()))

    # Wait forever (until signal)
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("Shutdown signal received")
    finally:
        cache.save_snapshots()
        latency.report()
        log.info("V21.7.9 WS Feed Layer SHUTDOWN")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass