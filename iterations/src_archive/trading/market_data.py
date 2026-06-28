#!/usr/bin/env python3
"""
FDC — MarketDataService
========================
Normalization layer. Consumes CCXTAdapter, produces FDC-typed data frames.
Owns the normalization schema — raw ccxt output never crosses this boundary.

Produces:
  - Normalized ticker data (symbol, bid, ask, last, volume, timestamp)
  - OHLCV as pandas DataFrame
  - Orderbook snapshots (bids, asks, mid, spread)

Phase 1A: Binance, Bybit, Coinbase. Read-only, public endpoints.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from ccxt_adapter import CCXTAdapter, AdapterPool, ExchangeUnavailable

log = logging.getLogger("fdc.market_data")


# ─── Data Types ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Ticker:
    symbol: str
    exchange: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    base_volume: Optional[float]
    quote_volume: Optional[float]
    change_24h_pct: Optional[float]
    timestamp: datetime
    # Optional: raw ccxt ticker dict preserved for debugging
    _raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class OrderbookSnapshot:
    symbol: str
    exchange: str
    bids: list[tuple[float, float]]   # (price, size)
    asks: list[tuple[float, float]]
    best_bid: Optional[float]
    best_ask: Optional[float]
    mid: Optional[float]
    spread: Optional[float]
    spread_pct: Optional[float]
    timestamp: datetime


@dataclass
class OhlcvFrame:
    symbol: str
    exchange: str
    timeframe: str
    df: pd.DataFrame      # columns: open, high, low, close, volume
    cached_at: datetime


# ─── MarketDataService ──────────────────────────────────────────────────────

class MarketDataService:
    """Normalized market data access layer.

    Takes an AdapterPool (already connected) and provides typed,
    normalized data. Raw ccxt output is consumed and discarded here.
    """

    def __init__(self, pool: AdapterPool):
        self._pool = pool

    @property
    def available_exchanges(self) -> list[str]:
        return self._pool.healthy_exchanges

    # ── Ticker ─────────────────────────────────────────────────────────

    def _normalize_ticker(self, raw: dict[str, Any], exchange: str) -> Ticker:
        """Normalize a single ccxt ticker into FDC Ticker type."""
        ts = (
            datetime.fromtimestamp(raw["timestamp"] / 1000, tz=timezone.utc)
            if raw.get("timestamp")
            else datetime.now(timezone.utc)
        )
        return Ticker(
            symbol=raw.get("symbol", "?"),
            exchange=exchange,
            bid=float(raw["bid"]) if raw.get("bid") is not None else None,
            ask=float(raw["ask"]) if raw.get("ask") is not None else None,
            last=float(raw["last"]) if raw.get("last") is not None else None,
            base_volume=float(raw["baseVolume"]) if raw.get("baseVolume") is not None else None,
            quote_volume=float(raw["quoteVolume"]) if raw.get("quoteVolume") is not None else None,
            change_24h_pct=float(raw["percentage"]) if raw.get("percentage") is not None else None,
            timestamp=ts,
            _raw=raw,
        )

    async def get_ticker(self, symbol: str, exchange: str) -> Ticker:
        """Fetch normalized ticker for a single symbol."""
        adapter = self._pool.get(exchange)
        raw = await adapter.fetch_ticker(symbol)
        return self._normalize_ticker(raw, exchange)

    async def get_tickers(
        self, symbols: list[str], exchanges: Optional[list[str]] = None
    ) -> list[Ticker]:
        """Fetch tickers across exchanges. Runs exchanges in parallel."""
        if exchanges is None:
            exchanges = self.available_exchanges

        async def _fetch_exchange(eid: str) -> list[Ticker]:
            try:
                adapter = self._pool.get(eid)
                raw = await adapter.fetch_tickers(symbols)
                # raw is dict[symbol -> ticker]
                result = []
                for sym in symbols:
                    if sym in raw:
                        result.append(self._normalize_ticker(raw[sym], eid))
                    else:
                        log.debug(f"[{eid}] symbol {sym} not found in tickers")
                return result
            except ExchangeUnavailable:
                log.warning(f"[{eid}] unavailable, skipping ticker fetch")
                return []
            except Exception as e:
                log.error(f"[{eid}] ticker fetch error: {e}")
                return []

        tasks = [asyncio.create_task(_fetch_exchange(eid)) for eid in exchanges]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        tickers = []
        for r in results:
            if isinstance(r, list):
                tickers.extend(r)
        return tickers

    # ── OHLCV ──────────────────────────────────────────────────────────

    def _normalize_ohlcv(
        self, raw: list[list[float]], symbol: str, exchange: str, timeframe: str
    ) -> OhlcvFrame:
        """Convert raw OHLCV list to pandas DataFrame."""
        if not raw:
            return OhlcvFrame(
                symbol=symbol,
                exchange=exchange,
                timeframe=timeframe,
                df=pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
                cached_at=datetime.now(timezone.utc),
            )

        df = pd.DataFrame(
            raw,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return OhlcvFrame(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            df=df,
            cached_at=datetime.now(timezone.utc),
        )

    async def get_ohlcv(
        self, symbol: str, exchange: str,
        timeframe: str = "15m",
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> OhlcvFrame:
        """Fetch normalized OHLCV data."""
        adapter = self._pool.get(exchange)
        since_ms = int(since.timestamp() * 1000) if since else None
        raw = await adapter.fetch_ohlcv(symbol, timeframe, since_ms, limit)
        return self._normalize_ohlcv(raw, symbol, exchange, timeframe)

    async def get_multi_ohlcv(
        self, symbols: list[str], exchange: str,
        timeframe: str = "15m", limit: int = 100,
    ) -> dict[str, OhlcvFrame]:
        """Fetch OHLCV for multiple symbols from a single exchange (sequential)."""
        adapter = self._pool.get(exchange)
        results = {}
        for sym in symbols:
            try:
                raw = await adapter.fetch_ohlcv(sym, timeframe, limit=limit)
                results[sym] = self._normalize_ohlcv(raw, sym, exchange, timeframe)
            except Exception as e:
                log.warning(f"[{exchange}] OHLCV fetch failed for {sym}: {e}")
        return results

    # ── Orderbook ───────────────────────────────────────────────────────

    def _normalize_orderbook(
        self, raw: dict[str, Any], symbol: str, exchange: str
    ) -> OrderbookSnapshot:
        """Normalize orderbook snapshot."""
        bids = [(float(b[0]), float(b[1])) for b in (raw.get("bids", []) or [])]
        asks = [(float(a[0]), float(a[1])) for a in (raw.get("asks", []) or [])]

        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        mid = ((best_bid + best_ask) / 2) if best_bid and best_ask else None
        spread = (best_ask - best_bid) if best_bid and best_ask else None
        spread_pct = (spread / mid * 100) if spread and mid else None

        ts = (
            datetime.fromtimestamp(raw["timestamp"] / 1000, tz=timezone.utc)
            if raw.get("timestamp")
            else datetime.now(timezone.utc)
        )

        return OrderbookSnapshot(
            symbol=symbol,
            exchange=exchange,
            bids=bids[:10],
            asks=asks[:10],
            best_bid=best_bid,
            best_ask=best_ask,
            mid=round(mid, 8) if mid else None,
            spread=round(spread, 8) if spread else None,
            spread_pct=round(spread_pct, 4) if spread_pct else None,
            timestamp=ts,
        )

    async def get_orderbook(
        self, symbol: str, exchange: str, depth: int = 10
    ) -> OrderbookSnapshot:
        """Fetch normalized orderbook snapshot."""
        adapter = self._pool.get(exchange)
        raw = await adapter.fetch_order_book(symbol, depth)
        return self._normalize_orderbook(raw, symbol, exchange)

    # ── Symbol Mapping ──────────────────────────────────────────────────

    def find_symbols(self, pattern: str, exchange: Optional[str] = None) -> dict[str, list[str]]:
        """Find matching symbols across exchanges. E.g., pattern='BTC/USDT'."""
        exchanges = [exchange] if exchange else self.available_exchanges
        result = {}
        for eid in exchanges:
            try:
                adapter = self._pool.get(eid)
                markets = adapter.markets
                matches = [sym for sym in markets if pattern.upper() in sym.upper()]
                if matches:
                    result[eid] = matches
            except ExchangeUnavailable:
                continue
        return result

    def get_symbol_info(self, symbol: str, exchange: str) -> dict[str, Any] | None:
        """Get exchange-specific market info (precision, limits, etc.)."""
        try:
            adapter = self._pool.get(exchange)
            return adapter.markets.get(symbol)
        except ExchangeUnavailable:
            return None
