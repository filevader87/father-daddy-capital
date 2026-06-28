#!/usr/bin/env python3
"""
FDC — CCXTAdapter
==================
Isolation layer around ccxt. Owns:
  - Per-exchange connection lifecycle
  - Rate-limit compliance with async isolation
  - Retry logic with exponential backoff
  - Graceful degradation (transient vs persistent failure detection)
  - Error normalization (ccxt errors → FDC-typed exceptions)

Architecture boundary: CCXTAdapter is the ONLY module that imports ccxt.
Raw ccxt output never leaks past this adapter. MarketDataService consumes
this adapter and owns the normalization schema.

Phase 1A: read-only, public endpoints. Binance, Bybit, Coinbase.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

import ccxt.async_support as ccxt_async
import ccxt

log = logging.getLogger("fdc.ccxt_adapter")

# ─── Version Constraint ─────────────────────────────────────────────────────
# ccxt>=4.0,<5.0  (pinned in requirements)
MIN_CCXT = (4, 0)
MAX_CCXT = (5, 0)
_actual = tuple(map(int, ccxt.__version__.split(".")[:2]))
assert MIN_CCXT <= _actual < MAX_CCXT, (
    f"ccxt version {ccxt.__version__} outside allowed range "
    f"[{'.'.join(map(str, MIN_CCXT))}, {'.'.join(map(str, MAX_CCXT))})"
)


# ─── Exceptions ─────────────────────────────────────────────────────────────

class AdapterError(Exception):
    """Base for all adapter errors."""


class TransientError(AdapterError):
    """Retryable — network, rate-limit, temporary exchange issue."""


class PersistentError(AdapterError):
    """Non-retryable — invalid symbol, auth failure, exchange maintenance."""


class ExchangeUnavailable(PersistentError):
    """Exchange is down or unreachable after retries."""


class RateLimited(TransientError):
    """Hit rate limit. Back off and retry."""


# ─── Exchange Config ────────────────────────────────────────────────────────

EXCHANGE_CLASS = {
    "binance":   ccxt_async.binance,
    "bybit":     ccxt_async.bybit,
    "coinbase":  ccxt_async.coinbase,
    "kraken":    ccxt_async.kraken,
    "gate":      ccxt_async.gate,
    "okx":       ccxt_async.okx,
}

DEFAULT_TIMEOUT = 15_000  # ms
MAX_RETRIES     = 3
BASE_DELAY      = 1.0     # seconds
MAX_DELAY       = 30.0
RATE_LIMIT_BUFFER = 0.15  # 15% safety margin on rate limits


@dataclass
class ExchangeStatus:
    exchange_id: str
    healthy: bool = True
    last_error: Optional[str] = None
    last_success: float = 0.0
    consecutive_failures: int = 0
    rate_limited_until: float = 0.0

    def record_success(self):
        self.healthy = True
        self.last_success = time.time()
        self.consecutive_failures = 0
        self.last_error = None

    def record_failure(self, error: str, is_transient: bool = True):
        self.consecutive_failures += 1
        self.last_error = error
        if self.consecutive_failures >= 5:
            self.healthy = False
        if not is_transient:
            self.healthy = False


# ─── CCXTAdapter ────────────────────────────────────────────────────────────

@dataclass
class CCXTAdapter:
    """Per-exchange adapter with rate-limit isolation."""

    exchange_id: str
    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = MAX_RETRIES
    base_delay: float = BASE_DELAY
    max_delay: float = MAX_DELAY

    # Internal
    _client: Any = field(default=None, repr=False)
    _status: ExchangeStatus = field(default=None, repr=False)
    _semaphore: asyncio.Semaphore = field(default=None, repr=False)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def connect(self):
        """Create exchange client. Idempotent."""
        if self._client is not None:
            return

        cls = EXCHANGE_CLASS.get(self.exchange_id)
        if cls is None:
            raise PersistentError(f"Unsupported exchange: {self.exchange_id}")

        self._client = cls({
            "timeout": self.timeout,
            "enableRateLimit": True,
        })
        self._semaphore = asyncio.Semaphore(3)
        self._status = ExchangeStatus(exchange_id=self.exchange_id)

        try:
            await self._client.load_markets()
            log.info(f"[{self.exchange_id}] connected, {len(self._client.markets)} markets")
            self._status.record_success()
        except Exception as e:
            # Close the partially-created client to prevent connector leaks
            await self._close_client()
            self._client = None
            self._status.record_failure(str(e), is_transient=False)
            raise ExchangeUnavailable(f"[{self.exchange_id}] failed to load markets: {e}") from e

    async def close(self):
        await self._close_client()

    async def _close_client(self):
        """Close the underlying ccxt client. Safe to call on partial init."""
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    @property
    def healthy(self) -> bool:
        return self._status is not None and self._status.healthy

    @property
    def markets(self) -> dict:
        """Raw ccxt market dict. Internal — MarketDataService wraps this."""
        if self._client is None:
            raise PersistentError(f"[{self.exchange_id}] not connected")
        return self._client.markets

    # ── Core Operations (Phase 1A: read-only) ──────────────────────────

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch current ticker — returns RAW ccxt dict."""
        return await self._call_with_retry("fetch_ticker", symbol)

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m",
        since: Optional[int] = None, limit: int = 100
    ) -> list[list[float]]:
        """Fetch OHLCV candles — returns RAW ccxt list."""
        return await self._call_with_retry(
            "fetch_ohlcv", symbol, timeframe, since, limit
        )

    async def fetch_order_book(
        self, symbol: str, limit: int = 10
    ) -> dict[str, Any]:
        """Fetch orderbook — returns RAW ccxt dict."""
        return await self._call_with_retry("fetch_order_book", symbol, limit)

    async def fetch_tickers(self, symbols: Optional[list[str]] = None) -> dict[str, Any]:
        """Batch fetch tickers — returns RAW ccxt dict."""
        return await self._call_with_retry("fetch_tickers", symbols)

    # ── Retry & Rate-Limit Engine ──────────────────────────────────────

    async def _call_with_retry(self, method: str, *args, **kwargs) -> Any:
        """Execute exchange call with retry, backoff, and rate-limit isolation."""
        async with self._semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    func = getattr(self._client, method)
                    result = await func(*args, **kwargs)
                    self._status.record_success()
                    return result

                except ccxt.RateLimitExceeded as e:
                    delay = self._rate_limit_delay(attempt)
                    log.warning(
                        f"[{self.exchange_id}] rate limited on {method}, "
                        f"retry {attempt+1}/{self.max_retries} in {delay:.1f}s"
                    )
                    self._status.rate_limited_until = time.time() + delay
                    if attempt < self.max_retries:
                        await asyncio.sleep(delay)
                        continue
                    self._status.record_failure(str(e), is_transient=True)
                    raise RateLimited(
                        f"[{self.exchange_id}] rate limited on {method} after {self.max_retries} retries"
                    ) from e

                except (ccxt.NetworkError, ccxt.DDoSProtection,
                        ccxt.ExchangeNotAvailable) as e:
                    delay = self._backoff_delay(attempt)
                    log.debug(
                        f"[{self.exchange_id}] transient error on {method}: {e}, "
                        f"retry {attempt+1}/{self.max_retries} in {delay:.1f}s"
                    )
                    if attempt < self.max_retries:
                        await asyncio.sleep(delay)
                        continue
                    self._status.record_failure(str(e), is_transient=True)
                    raise TransientError(
                        f"[{self.exchange_id}] {method} failed after {self.max_retries} retries: {e}"
                    ) from e

                except (ccxt.BadSymbol, ccxt.AuthenticationError,
                        ccxt.InvalidAddress) as e:
                    self._status.record_failure(str(e), is_transient=False)
                    raise PersistentError(
                        f"[{self.exchange_id}] {method} permanent error: {e}"
                    ) from e

                except Exception as e:
                    self._status.record_failure(str(e), is_transient=True)
                    raise TransientError(
                        f"[{self.exchange_id}] {method} unexpected: {e}"
                    ) from e

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter."""
        delay = min(self.max_delay, self.base_delay * (2 ** attempt))
        return delay * (0.5 + 0.5 * (hash(str(time.time())) % 1000) / 1000)

    def _rate_limit_delay(self, attempt: int) -> float:
        """Longer backoff for rate limits."""
        delay = min(self.max_delay * 2, self.base_delay * (4 ** attempt))
        return delay


# ─── Adapter Pool ───────────────────────────────────────────────────────────

@dataclass
class AdapterPool:
    """Manages multiple exchange adapters with independent rate limiting."""

    exchange_ids: list[str]
    adapters: dict[str, CCXTAdapter] = field(default_factory=dict, repr=False)

    async def __aenter__(self):
        for eid in self.exchange_ids:
            adapter = CCXTAdapter(exchange_id=eid)
            try:
                await adapter.connect()
                self.adapters[eid] = adapter
                log.info(f"AdapterPool: {eid} connected")
            except Exception as e:
                log.error(f"AdapterPool: {eid} failed to connect: {e}")
                # Continue with remaining exchanges — graceful degradation
        if not self.adapters:
            raise ExchangeUnavailable("No exchanges connected")
        return self

    async def __aexit__(self, *args):
        for adapter in self.adapters.values():
            await adapter.close()

    @property
    def healthy_exchanges(self) -> list[str]:
        return [eid for eid, a in self.adapters.items() if a.healthy]

    def get(self, exchange_id: str) -> CCXTAdapter:
        adapter = self.adapters.get(exchange_id)
        if adapter is None or not adapter.healthy:
            raise ExchangeUnavailable(
                f"{exchange_id} not available (connected={exchange_id in self.adapters}, "
                f"healthy={adapter.healthy if adapter else False})"
            )
        return adapter
