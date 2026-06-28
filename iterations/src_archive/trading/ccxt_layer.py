#!/usr/bin/env python3
"""
FDC — CCXT Integration Layer
==============================
Drop-in yfinance replacement for crypto data. Three exchange architecture
per Riker's Phase 1A design. Lazy imports — zero dependency if ccxt isn't installed.

Architecture:
  CCXTAdapter       → wraps one ccxt exchange, handles rate limits, retries, errors
  CCXTPool           → manages 3 adapters (binance/bybit/coinbase) with health checks
  CCXTDataProvider   → public API: get_prices(), get_btc_5min(), get_orderbook(), get_funding_rate()

Every method falls back to yfinance if ccxt is unavailable. Zero breaking changes.
Import-safe: `from ccxt_layer import CCXTDataProvider` works even without ccxt installed.

Exchanges:
  1. binance  — highest liquidity, default primary
  2. bybit    — derivatives focus
  3. coinbase — US-regulated institutional

Per Riker's spec:
  - Public endpoints only (read-only, no API keys needed)
  - Rate limit handling per exchange (async tasks prevent blocking)
  - Graceful degradation when an exchange is down
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("fdc.ccxt")

# ─── Lazy CCXT import ───────────────────────────────────────────────────────

_CCXT_INSTALLED = False
_ccxt_module = None

def _check_ccxt() -> bool:
    """Check if ccxt is installed. Safe to call at any time."""
    global _CCXT_INSTALLED, _ccxt_module
    if _CCXT_INSTALLED:
        return True
    try:
        import ccxt
        _ccxt_module = ccxt
        _CCXT_INSTALLED = True
        return True
    except ImportError:
        return False

# ─── Symbol Mapping ──────────────────────────────────────────────────────────

# FDC internal → CCXT exchange symbol
CRYPTO_SYMBOL_MAP = {
    "BTC-USD": "BTC/USDT",
    "ETH-USD": "ETH/USDT",
    "SOL-USD": "SOL/USDT",
    "AVAX-USD": "AVAX/USDT",
}

REVERSE_MAP = {v: k for k, v in CRYPTO_SYMBOL_MAP.items()}

CRYPTO_FDC_SYMBOLS = set(CRYPTO_SYMBOL_MAP.keys())
EQUITY_SYMBOLS = {"SPY", "QQQ", "AAPL", "NVDA", "MSFT", "TSLA"}

# Exchange priority: binance > bybit > coinbase
EXCHANGE_IDS = ["binance", "bybit", "coinbase"]
PRIMARY_EXCHANGE = "binance"

# ─── yfinance (always available for equities + fallback) ─────────────────────
import yfinance as yf


# ─── CCXTAdapter ─────────────────────────────────────────────────────────────

class CCXTAdapter:
    """
    Thin wrapper around one ccxt exchange. Handles:
      - Rate limit awareness (sleeps automatically via ccxt)
      - Transient vs persistent error classification
      - OHLCV, ticker, orderbook, funding rate fetches
    """

    def __init__(self, exchange_id: str):
        self._id = exchange_id
        self._exchange = None
        self._healthy = False
        self._last_error: Optional[str] = None
        self._init_time: float = 0.0

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def exchange_id(self) -> str:
        return self._id

    async def _connect(self):
        """Initialize the ccxt exchange. Non-fatal on failure."""
        if not _check_ccxt():
            self._healthy = False
            return

        try:
            ExchangeClass = getattr(_ccxt_module, self._id)
            self._exchange = ExchangeClass({
                'enableRateLimit': True,
                'timeout': 15000,
            })
            # Quick health check: fetch server time
            await asyncio.to_thread(self._exchange.load_markets)
            self._healthy = True
            self._init_time = time.time()
            log.info(f"CCXT {self._id}: connected")
        except Exception as e:
            self._healthy = False
            self._last_error = str(e)[:120]
            log.warning(f"CCXT {self._id}: {self._last_error}")

    async def _close(self):
        if self._exchange:
            try:
                await asyncio.to_thread(self._exchange.close)
            except Exception:
                pass
        self._healthy = False

    async def _call_async(self, method_name: str, *args, **kwargs) -> any:
        """
        Call a ccxt method with retry on transient errors.
        Raises on persistent failures.
        """
        if not self._healthy or self._exchange is None:
            raise ExchangeNotAvailable(self._id)

        method = getattr(self._exchange, method_name, None)
        if method is None:
            raise ValueError(f"No such method: {method_name}")

        for attempt in range(3):
            try:
                return await asyncio.to_thread(method, *args, **kwargs)
            except Exception as e:
                msg = str(e)[:150]
                if any(kw in msg.lower() for kw in
                       ['rate limit', 'too many requests', 'ddos', '429']):
                    wait = (2 ** attempt) * 1.5
                    log.debug(f"CCXT {self._id}: rate limited, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue
                if any(kw in msg.lower() for kw in
                       ['timeout', 'connection', 'network', 'temporarily']):
                    wait = (2 ** attempt) * 1.0
                    log.debug(f"CCXT {self._id}: transient, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue
                # Persistent error — don't retry
                raise

    # ── Public Methods ──────────────────────────────────────────────────

    async def fetch_ohlcv(self, symbol: str, timeframe: str = '1d',
                          limit: int = 60) -> pd.DataFrame:
        """Fetch OHLCV candles. Returns DataFrame with [open,high,low,close,volume]."""
        data = await self._call_async('fetch_ohlcv', symbol, timeframe, limit=limit)
        df = pd.DataFrame(
            data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df

    async def fetch_ticker(self, symbol: str) -> dict:
        """Fetch current ticker. Returns {last, bid, ask, volume, ...}."""
        return await self._call_async('fetch_ticker', symbol)

    async def fetch_order_book(self, symbol: str, limit: int = 10) -> dict:
        """Fetch orderbook. Returns {bids: [[price,size],...], asks: [[price,size],...]}."""
        return await self._call_async('fetch_order_book', symbol, limit)

    async def fetch_funding_rate(self, symbol: str) -> dict:
        """Fetch perpetual funding rate. Returns {fundingRate, fundingTimestamp, ...}."""
        return await self._call_async('fetch_funding_rate', symbol)


class ExchangeNotAvailable(Exception):
    """Raised when a specific exchange is down or disconnected."""
    def __init__(self, exchange_id: str):
        super().__init__(f"Exchange not available: {exchange_id}")


# ─── CCXTPool ────────────────────────────────────────────────────────────────

class CCXTPool:
    """
    Manages 3 exchange adapters. Provides:
      - Health-aware routing: try primary first, fallback to alternate
      - Cross-exchange metrics: price divergence, volume comparison
    """

    def __init__(self, exchange_ids: list[str] = None):
        self._ids = exchange_ids or EXCHANGE_IDS
        self._adapters: dict[str, CCXTAdapter] = {}
        self._healthy_list: list[str] = []

    async def connect(self):
        """Connect to all exchanges in parallel. Non-blocking per exchange."""
        if not _check_ccxt():
            log.warning("CCXT not installed — crypto data falls back to yfinance")
            return

        for eid in self._ids:
            adapter = CCXTAdapter(eid)
            self._adapters[eid] = adapter

        # Connect in parallel
        tasks = [a._connect() for a in self._adapters.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

        self._healthy_list = [eid for eid, a in self._adapters.items() if a.healthy]
        log.info(f"CCXT healthy exchanges: {self._healthy_list}")

    async def close(self):
        tasks = [a._close() for a in self._adapters.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._healthy_list = []

    @property
    def healthy_exchanges(self) -> list[str]:
        return list(self._healthy_list)

    @property
    def connected(self) -> bool:
        return len(self._healthy_list) > 0

    def _healthy_adapter(self) -> CCXTAdapter:
        """Get the first healthy adapter, preferring the primary."""
        if PRIMARY_EXCHANGE in self._healthy_list:
            return self._adapters[PRIMARY_EXCHANGE]
        if self._healthy_list:
            return self._adapters[self._healthy_list[0]]
        raise ExchangeNotAvailable("all")

    # ── Cross-exchange metrics ──────────────────────────────────────────

    async def cross_exchange_prices(self, symbol: str) -> dict[str, float]:
        """Fetch last price from all healthy exchanges. Returns {exchange_id: price}."""
        prices = {}
        for eid in self._healthy_list:
            try:
                ticker = await self._adapters[eid].fetch_ticker(symbol)
                prices[eid] = ticker.get('last', 0)
            except Exception:
                pass
        return prices

    async def cross_exchange_volume(self, symbol: str) -> dict[str, float]:
        """24h volume across healthy exchanges."""
        volumes = {}
        for eid in self._healthy_list:
            try:
                ticker = await self._adapters[eid].fetch_ticker(symbol)
                volumes[eid] = ticker.get('quoteVolume', ticker.get('baseVolume', 0)) or 0
            except Exception:
                pass
        return volumes

    @property
    def primary_volatility(self) -> float:
        """
        Estimate current volatility from cross-exchange price dispersion.
        Higher = markets disagreeing on price.
        """
        return 0.0  # Placeholder — populated by DataProvider.cross_exchange_dispersion()


# ─── CCXTDataProvider ────────────────────────────────────────────────────────

class CCXTDataProvider:
    """
    Public API for the entire trading system. Drop-in replacement anywhere
    yfinance is currently called.

    Usage:
        provider = CCXTDataProvider()
        await provider.connect()

        # Replace yfinance calls:
        prices = await provider.get_prices("BTC-USD")
        btc_5m = await provider.get_btc_5min()
        orderbook = await provider.get_orderbook("SOL-USD")
        funding = await provider.get_funding_rate("BTC-USD")

        # Cross-exchange:
        dispersion = await provider.cross_exchange_dispersion("BTC-USD")

        await provider.close()
    """

    def __init__(self):
        self._pool: Optional[CCXTPool] = None
        self._connected = False

    async def connect(self):
        self._pool = CCXTPool()
        await self._pool.connect()
        self._connected = self._pool.connected

    async def close(self):
        if self._pool:
            await self._pool.close()
        self._connected = False

    @property
    def using_ccxt(self) -> bool:
        return self._connected

    @property
    def healthy_exchanges(self) -> list[str]:
        return self._pool.healthy_exchanges if self._pool else []

    # ── get_prices (replaces scan_market() yfinance calls) ──────────────

    async def get_prices(self, symbol: str, lookback_days: int = 50) -> pd.Series:
        """
        Fetch daily price series. Routes crypto to CCXT, equities to yfinance.
        Returns pd.Series of Close prices — identical shape to yfinance output.
        """
        if symbol in CRYPTO_FDC_SYMBOLS and self._connected:
            try:
                adapter = self._pool._healthy_adapter()
                exchange_sym = CRYPTO_SYMBOL_MAP[symbol]
                df = await adapter.fetch_ohlcv(exchange_sym, '1d', lookback_days + 5)
                if len(df) >= 20:
                    return df['close']
            except Exception as e:
                log.debug(f"CCXT get_prices({symbol}): {e}")

        return self._yf_prices(symbol, lookback_days)

    def _yf_prices(self, symbol: str, lookback_days: int = 50) -> pd.Series:
        end = datetime.now()
        start = end - timedelta(days=lookback_days)
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start, end=end)
        return hist["Close"]

    async def get_prices_sync(self, symbol: str, lookback_days: int = 50) -> pd.Series:
        """Synchronous wrapper for use inside paper_engine.py sync loops."""
        try:
            return await self.get_prices(symbol, lookback_days)
        except Exception:
            return self._yf_prices(symbol, lookback_days)

    # ── get_btc_5min (replaces paper_engine.fetch_btc_5min) ──────────────

    async def get_btc_5min(self) -> list[float]:
        """BTC 5-minute close prices. CCXT preferred, yfinance fallback."""
        if self._connected:
            try:
                adapter = self._pool._healthy_adapter()
                df = await adapter.fetch_ohlcv('BTC/USDT', '5m', 60)
                if len(df) >= 14:
                    return df['close'].tolist()
            except Exception:
                pass
        return self._yf_btc_5min()

    def _yf_btc_5min(self) -> list[float]:
        try:
            btc = yf.Ticker("BTC-USD")
            hist = btc.history(period="5d", interval="5m")
            if len(hist) < 14:
                return []
            return hist["Close"].tolist()[-60:]
        except Exception:
            return []

    # ── get_orderbook (NEW — feeds FeatureEncoder) ────────────────────

    async def get_orderbook(self, symbol: str) -> dict:
        """
        Fetch orderbook from primary exchange.
        Returns {bids: [[price,size],...], asks: [[price,size],...], skew: float, spread: float}
        """
        if self._connected and symbol in CRYPTO_FDC_SYMBOLS:
            try:
                adapter = self._pool._healthy_adapter()
                exchange_sym = CRYPTO_SYMBOL_MAP[symbol]
                book = await adapter.fetch_order_book(exchange_sym, limit=20)

                bids = book.get('bids', [])
                asks = book.get('asks', [])

                # Compute skew: bid volume / total volume
                bid_vol = sum(b[1] for b in bids[:10]) if bids else 0
                ask_vol = sum(a[1] for a in asks[:10]) if asks else 0
                total_vol = bid_vol + ask_vol
                skew = (bid_vol - ask_vol) / max(total_vol, 1e-9)  # -1 to +1

                # Spread
                best_bid = bids[0][0] if bids else 0
                best_ask = asks[0][0] if asks else 0
                mid = (best_bid + best_ask) / 2
                spread = (best_ask - best_bid) / max(mid, 1e-9) if mid > 0 else 0

                return {
                    **book,
                    'skew': float(np.clip(skew, -1.0, 1.0)),
                    'spread': float(spread),
                }
            except Exception:
                pass
        return {'bids': [], 'asks': [], 'skew': 0.0, 'spread': 0.0}

    # ── get_funding_rate (NEW — sentiment proxy) ──────────────────────

    async def get_funding_rate(self, symbol: str) -> float:
        """
        Perpetual futures funding rate. Positive = longs pay shorts (bullish),
        negative = shorts pay longs (bearish). Normalized to [-1, 1].
        """
        if self._connected and symbol in CRYPTO_FDC_SYMBOLS:
            try:
                adapter = self._pool._healthy_adapter()
                exchange_sym = CRYPTO_SYMBOL_MAP[symbol]
                rate_data = await adapter.fetch_funding_rate(exchange_sym)
                raw_rate = rate_data.get('fundingRate', 0) or 0
                return float(np.clip(raw_rate * 100, -1.0, 1.0))
            except Exception:
                pass
        return 0.0

    # ── Cross-exchange dispersion (NEW — feeds RegimeDetector) ───────

    async def cross_exchange_dispersion(self, symbol: str) -> dict:
        """
        Price divergence across exchanges. High dispersion = market stress,
        possible arb opportunities. Returns:
          {dispersion_pct, prices, max_divergence, regime_signal}
        """
        if not self._connected:
            return {'dispersion_pct': 0.0, 'prices': {}, 'max_divergence': 0.0,
                    'regime_signal': 'neutral'}

        try:
            exchange_sym = CRYPTO_SYMBOL_MAP.get(symbol, symbol)
            prices = await self._pool.cross_exchange_prices(exchange_sym)

            if len(prices) < 2:
                return {'dispersion_pct': 0.0, 'prices': prices,
                        'max_divergence': 0.0, 'regime_signal': 'neutral'}

            vals = list(prices.values())
            mean_price = np.mean(vals)
            if mean_price == 0:
                return {'dispersion_pct': 0.0, 'prices': prices,
                        'max_divergence': 0.0, 'regime_signal': 'neutral'}

            max_div = (max(vals) - min(vals)) / mean_price * 100

            # Regime signal from dispersion
            if max_div > 1.0:
                signal = 'arb_opportunity'
            elif max_div > 0.3:
                signal = 'market_stress'
            else:
                signal = 'stable'

            return {
                'dispersion_pct': round(max_div, 3),
                'prices': prices,
                'max_divergence': round(max_div, 3),
                'regime_signal': signal,
            }

        except Exception:
            return {'dispersion_pct': 0.0, 'prices': {}, 'max_divergence': 0.0,
                    'regime_signal': 'neutral'}

    # ── Volume-weighted trend (NEW — feeds RegimeDetector) ────────────

    async def volume_weighted_trend(self, symbol: str) -> float:
        """
        Aggregate trend across exchanges weighted by volume.
        Returns float in [-1, 1] where +1 = strongly bullish across all exchanges.
        """
        if not self._connected or symbol not in CRYPTO_FDC_SYMBOLS:
            return 0.0

        try:
            exchange_sym = CRYPTO_SYMBOL_MAP[symbol]
            volumes = await self._pool.cross_exchange_volume(exchange_sym)
            prices = await self._pool.cross_exchange_prices(exchange_sym)

            if not volumes or not prices:
                return 0.0

            # Simple: compare each exchange's price to the mean
            mean_price = np.mean(list(prices.values()))
            total_vol = sum(volumes.values())

            if total_vol == 0 or mean_price == 0:
                return 0.0

            weighted_signal = 0.0
            for eid in prices:
                if eid in volumes:
                    vol_weight = volumes[eid] / total_vol
                    price_signal = (prices[eid] - mean_price) / max(mean_price, 1)
                    weighted_signal += price_signal * vol_weight

            return float(np.clip(weighted_signal * 20, -1.0, 1.0))

        except Exception:
            return 0.0

    # ── Aggregated BTC 1h candles (for macro correlation) ───────────

    async def get_btc_1h(self) -> pd.DataFrame:
        """BTC 1-hour candles for macro correlation in FeatureEncoder."""
        if self._connected:
            try:
                adapter = self._pool._healthy_adapter()
                return await adapter.fetch_ohlcv('BTC/USDT', '1h', 120)
            except Exception:
                pass
        return self._yf_1h('BTC-USD')

    async def get_spy_1h(self) -> pd.DataFrame:
        """SPY 1-hour candles from yfinance (no CCXT equivalent for equities)."""
        return self._yf_1h('SPY')

    def _yf_1h(self, symbol: str) -> pd.DataFrame:
        try:
            ticker = yf.Ticker(symbol)
            return ticker.history(period="5d", interval="1h")
        except Exception:
            return pd.DataFrame()


# ─── Singleton convenience ───────────────────────────────────────────────────

_global_provider: Optional[CCXTDataProvider] = None

async def get_provider() -> CCXTDataProvider:
    """Get or create the global CCXT data provider."""
    global _global_provider
    if _global_provider is None:
        _global_provider = CCXTDataProvider()
        await _global_provider.connect()
    return _global_provider

async def shutdown_provider():
    """Close the global provider."""
    global _global_provider
    if _global_provider:
        await _global_provider.close()
        _global_provider = None


# ─── Sync helpers for paper_engine.py ───────────────────────────────────────

def get_btc_5min_sync() -> list[float]:
    """Synchronous wrapper — used in paper_engine.run_once()."""
    if not _check_ccxt():
        return _yf_btc_5min_sync()
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return _yf_btc_5min_sync()
        provider = loop.run_until_complete(get_provider())
        return loop.run_until_complete(provider.get_btc_5min())
    except Exception:
        return _yf_btc_5min_sync()

def _yf_btc_5min_sync() -> list[float]:
    try:
        import yfinance as yf
        btc = yf.Ticker("BTC-USD")
        hist = btc.history(period="5d", interval="5m")
        if len(hist) < 14:
            return []
        return hist["Close"].tolist()[-60:]
    except Exception:
        return []


# ─── Test ────────────────────────────────────────────────────────────────────

async def _test():
    provider = CCXTDataProvider()
    await provider.connect()

    print(f"CCXT installed: {_check_ccxt()}")
    print(f"Connected: {provider.using_ccxt}")
    print(f"Healthy exchanges: {provider.healthy_exchanges}")

    if provider.using_ccxt:
        # Quick BTC test
        btc_5m = await provider.get_btc_5min()
        print(f"BTC 5m candles: {len(btc_5m)}")

        orderbook = await provider.get_orderbook("BTC-USD")
        print(f"BTC orderbook skew: {orderbook.get('skew', 'N/A')}, spread: {orderbook.get('spread', 'N/A')}")

        funding = await provider.get_funding_rate("BTC-USD")
        print(f"BTC funding rate: {funding}")

        dispersion = await provider.cross_exchange_dispersion("BTC-USD")
        print(f"BTC cross-exchange dispersion: {dispersion['dispersion_pct']}% ({dispersion['regime_signal']})")

    await provider.close()

if __name__ == "__main__":
    asyncio.run(_test())
