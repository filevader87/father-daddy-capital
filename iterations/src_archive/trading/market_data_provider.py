#!/usr/bin/env python3
"""
FDC — Market Data Provider (Unified)
=====================================
Single interface for paper_engine.py. Routes:
  - Crypto symbols → CCXT/MarketDataService (real-time exchange data)
  - Equity symbols → yfinance (historical + current)
  - BTC 5-min candles → CCXT (preferred) or yfinance (fallback)

Transparent fallback: if CCXT fails, drops to yfinance automatically.
Designed to be importable from paper_engine.py with no breaking changes.

Usage:
  from market_data_provider import MarketDataProvider
  provider = MarketDataProvider()
  
  # Crypto (uses CCXT)
  prices = provider.get_prices("BTC/USDT", lookback_days=50)
  signal = provider.compute_signals(prices)
  
  # Equities (uses yfinance)
  prices = provider.get_prices("AAPL", lookback_days=50)
  
  # BTC 5-min candles
  candles = provider.get_btc_5min()
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("fdc.data_provider")

# ─── yfinance (always available for equities + fallback) ────────────────────
import yfinance as yf

# ─── CCXT import (optional — gracefully degrades) ───────────────────────────
_CCXT_AVAILABLE = False
_ccxt_adapter = None
_market_data = None

# Add src/trading to path for local imports (when running from repo root)
_TRADING_PATH = Path(__file__).parent / "src" / "trading"
if str(_TRADING_PATH) not in sys.path:
    sys.path.insert(0, str(_TRADING_PATH))

try:
    from ccxt_adapter import AdapterPool, CCXTAdapter
    from market_data import MarketDataService
    _CCXT_AVAILABLE = True
except ImportError:
    pass


# ─── Symbol Mapping ─────────────────────────────────────────────────────────

# FDC internal symbols → exchange symbols for CCXT
CRYPTO_SYMBOL_MAP = {
    "BTC-USD": "BTC/USD",
    "ETH-USD": "ETH/USD",
    "SOL-USD": "SOL/USD",
    "AVAX-USD": "AVAX/USD",
}

# Which symbols route to CCXT (crypto) vs yfinance (equities)
CRYPTO_SYMBOLS = set(CRYPTO_SYMBOL_MAP.keys())
EQUITY_SYMBOLS = {"SPY", "QQQ", "AAPL", "NVDA", "MSFT", "TSLA"}


# ─── MarketDataProvider ─────────────────────────────────────────────────────

class MarketDataProvider:
    """Unified data provider for paper_engine.py."""

    def __init__(self, use_ccxt: bool = True, primary_exchange: str = "coinbase"):
        self._use_ccxt = use_ccxt and _CCXT_AVAILABLE
        self._primary_exchange = primary_exchange
        self._pool: Optional[AdapterPool] = None
        self._svc: Optional[MarketDataService] = None
        self._connected = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def connect(self):
        """Connect to CCXT exchanges. Idempotent, non-fatal on failure."""
        if not self._use_ccxt or self._connected:
            return

        try:
            self._pool = AdapterPool(exchange_ids=[self._primary_exchange])
            await self._pool.__aenter__()
            self._svc = MarketDataService(self._pool)
            self._connected = True
            log.info(f"CCXT connected: {self._pool.healthy_exchanges}")
        except Exception as e:
            log.warning(f"CCXT connect failed, falling back to yfinance: {e}")
            self._use_ccxt = False

    async def close(self):
        if self._pool:
            await self._pool.__aexit__()
            self._connected = False

    @property
    def using_ccxt(self) -> bool:
        return self._connected

    # ── Core: Get Prices (mirrors yfinance history shape) ──────────────

    async def get_prices_async(self, symbol: str, lookback_days: int = 50) -> pd.Series:
        """Fetch price series. Routes crypto→CCXT, equities→yfinance."""
        if symbol in CRYPTO_SYMBOLS and self._connected:
            try:
                exchange_sym = CRYPTO_SYMBOL_MAP.get(symbol, symbol)
                ohlcv = await self._svc.get_ohlcv(
                    exchange_sym, self._primary_exchange,
                    timeframe="1d", limit=lookback_days + 5
                )
                if ohlcv.df is not None and len(ohlcv.df) >= 20:
                    return ohlcv.df["close"]
            except Exception as e:
                log.debug(f"CCXT fetch failed for {symbol}: {e}")

        # Fallback: yfinance for everything
        return self._fetch_yfinance(symbol, lookback_days)

    def get_prices(self, symbol: str, lookback_days: int = 50) -> pd.Series:
        """Synchronous wrapper. Falls back to yfinance if not connected."""
        if symbol in CRYPTO_SYMBOLS and self._connected:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Already in async context — defer to caller to use async version
                    return self._fetch_yfinance(symbol, lookback_days)
                return loop.run_until_complete(
                    self.get_prices_async(symbol, lookback_days)
                )
            except Exception:
                pass
        return self._fetch_yfinance(symbol, lookback_days)

    def _fetch_yfinance(self, symbol: str, lookback_days: int = 50) -> pd.Series:
        """Original yfinance fetch logic."""
        end = datetime.now()
        start = end - timedelta(days=lookback_days)
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start, end=end)
        return hist["Close"]

    # ── BTC 5-Minute Candles ───────────────────────────────────────────

    async def get_btc_5min_async(self) -> list[float]:
        """Fetch BTC 5-min candles. CCXT preferred, yfinance fallback."""
        if self._connected:
            try:
                exchange_sym = CRYPTO_SYMBOL_MAP["BTC-USD"]
                ohlcv = await self._svc.get_ohlcv(
                    exchange_sym, self._primary_exchange,
                    timeframe="5m", limit=60
                )
                if ohlcv.df is not None and len(ohlcv.df) >= 14:
                    return ohlcv.df["close"].tolist()
            except Exception:
                pass

        return self._fetch_btc_5min_yfinance()

    def get_btc_5min(self) -> list[float]:
        """Synchronous wrapper."""
        if self._connected:
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_running():
                    return loop.run_until_complete(self.get_btc_5min_async())
            except Exception:
                pass
        return self._fetch_btc_5min_yfinance()

    def _fetch_btc_5min_yfinance(self) -> list[float]:
        """Original yfinance 5-min fetch."""
        try:
            btc = yf.Ticker("BTC-USD")
            hist = btc.history(period="5d", interval="5m")
            if len(hist) < 14:
                return []
            return hist["Close"].tolist()[-60:]
        except Exception:
            return []

    # ── Current Prices (no history, just spot) ─────────────────────────

    async def get_current_prices_async(self, symbols: list[str]) -> dict[str, float]:
        """Bulk current prices. CCXT for crypto, yfinance for equities."""
        result = {}
        crypto_to_fetch = []

        for sym in symbols:
            if sym in CRYPTO_SYMBOLS and self._connected:
                crypto_to_fetch.append(CRYPTO_SYMBOL_MAP[sym])
            else:
                try:
                    result[sym] = self._fetch_yfinance_current(sym)
                except Exception:
                    pass

        if crypto_to_fetch:
            try:
                tickers = await self._svc.get_tickers(crypto_to_fetch)
                # Map back from exchange symbols to FDC symbols
                reverse_map = {v: k for k, v in CRYPTO_SYMBOL_MAP.items()}
                for t in tickers:
                    fdc_sym = reverse_map.get(t.symbol, t.symbol)
                    if t.last is not None:
                        result[fdc_sym] = t.last
            except Exception as e:
                log.debug(f"CCXT current prices failed: {e}")
                # Fallback to yfinance for remaining
                for sym in symbols:
                    if sym not in result:
                        try:
                            result[sym] = self._fetch_yfinance_current(sym)
                        except Exception:
                            pass

        return result

    def _fetch_yfinance_current(self, symbol: str) -> float:
        """Quick current price from yfinance."""
        ticker = yf.Ticker(symbol)
        return float(ticker.history(period="1d").iloc[-1]["Close"])

    # ── Signal Computation (unchanged — works on any pd.Series) ────────

    def compute_signals(self, prices: pd.Series) -> dict:
        """Multi-factor signals. Identical to original paper_engine.compute_signals()."""
        if len(prices) < 20:
            return {"score": 0, "signals": {}, "confidence": 0}

        # RSI (14-day)
        delta = prices.diff()
        gain = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])

        # MACD
        ema12 = prices.ewm(span=12).mean()
        ema26 = prices.ewm(span=26).mean()
        macd = ema12 - ema26
        signal_line = macd.ewm(span=9).mean()
        macd_signal = 1 if macd.iloc[-1] > signal_line.iloc[-1] else -1

        # Trend strength
        sma20 = prices.rolling(20).mean().iloc[-1]
        sma50 = prices.rolling(50).mean().iloc[-1] if len(prices) >= 50 else sma20
        current_price = float(prices.iloc[-1])
        trend_20 = 1 if current_price > sma20 else -1
        trend_50 = 1 if current_price > sma50 else -1

        # Volatility
        returns = prices.pct_change().dropna()
        volatility = float(returns.rolling(20).std().iloc[-1] * np.sqrt(365))

        # Momentum
        momentum_5d = float((prices.iloc[-1] / prices.iloc[-6] - 1)) if len(prices) >= 6 else 0

        # Mean reversion
        z_score = float((current_price - sma20) / (prices.rolling(20).std().iloc[-1] + 1e-9))
        mean_reversion = -z_score

        # Composite
        rsi_signal = 1 if current_rsi < 30 else (-1 if current_rsi > 70 else 0)
        trend_signal = (trend_20 + trend_50) / 2.0

        score = (
            rsi_signal * 0.20 +
            macd_signal * 0.25 +
            trend_signal * 0.25 +
            np.clip(momentum_5d * 10, -1, 1) * 0.15 +
            np.clip(mean_reversion * 0.5, -1, 1) * 0.15
        )

        confidence = min(0.95, max(0.3,
            0.6 + abs(score) * 0.3 - volatility * 0.5
        ))

        return {
            "score": round(score, 3),
            "rsi": round(current_rsi, 1),
            "volatility": round(volatility, 3),
            "momentum_5d": round(momentum_5d, 4),
            "trend": "up" if trend_20 > 0 else "down",
            "confidence": round(confidence, 3),
            "signals": {
                "rsi": rsi_signal,
                "macd": macd_signal,
                "trend": trend_signal,
                "momentum": round(np.clip(momentum_5d * 10, -1, 1), 3),
                "mean_reversion": round(np.clip(mean_reversion * 0.5, -1, 1), 3),
            }
        }

    # ── Full Market Scan (replaces paper_engine.scan_market) ───────────

    async def scan_market_async(self, symbols: list[str], lookback_days: int = 50) -> list[dict]:
        """Async scan — uses CCXT for crypto, yfinance for equities."""
        results = []

        for symbol in symbols:
            try:
                prices = await self.get_prices_async(symbol, lookback_days)
                if len(prices) < 20:
                    continue

                current_price = float(prices.iloc[-1])
                signal = self.compute_signals(prices)

                results.append({
                    "symbol": symbol,
                    "price": round(current_price, 2),
                    "asset_class": "crypto" if symbol in CRYPTO_SYMBOLS else "equity",
                    **signal,
                })
            except Exception as e:
                log.warning(f"Scan failed for {symbol}: {e}")

        results.sort(key=lambda x: abs(x["score"]), reverse=True)
        return results

    def scan_market(self, symbols: list[str], lookback_days: int = 50) -> list[dict]:
        """Synchronous scan. Uses CCXT if connected, falls back to yfinance."""
        if self._connected:
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_running():
                    return loop.run_until_complete(
                        self.scan_market_async(symbols, lookback_days)
                    )
            except Exception:
                pass

        # Fallback: pure yfinance (original behavior)
        return self._scan_market_yfinance(symbols, lookback_days)

    def _scan_market_yfinance(self, symbols: list[str], lookback_days: int = 50) -> list[dict]:
        """Original yfinance-only scan — identical to current paper_engine.scan_market()."""
        results = []
        end = datetime.now()
        start = end - timedelta(days=lookback_days)

        for symbol in symbols:
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(start=start, end=end)
                if len(hist) < 20:
                    continue

                prices = hist["Close"]
                current_price = float(prices.iloc[-1])
                signal = self.compute_signals(prices)

                results.append({
                    "symbol": symbol,
                    "price": round(current_price, 2),
                    "asset_class": "crypto" if symbol in CRYPTO_SYMBOLS else "equity",
                    **signal,
                })
            except Exception:
                pass

        results.sort(key=lambda x: abs(x["score"]), reverse=True)
        return results
