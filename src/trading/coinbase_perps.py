#!/usr/bin/env python3
"""
FDC — Coinbase Advanced Perpetual Futures Connector
=====================================================
Primary exchange for US-based perp trading.

Coinbase Advanced perps:
  - BTC-PERP, ETH-PERP (expanding)
  - Maker: 0.00% / Taker: 0.06% (VIP0)
  - 5x leverage (up to 25x on ADV tier)
  - US-regulated, no geoblock
  - Testnet: api-public.sandbox.pro.coinbase.com

Architecture:
  - Uses CCXT for Coinbase International (perps are on Intl, not Advanced)
  - Coinbase Intl = coinbaseinternational exchange in ccxt
  - Falls back to coinbase for spot data

Author: Hugh (3rd of 5)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger("fdc.coinbase_perps")

REPO = Path("/mnt/c/Users/12035/father_daddy_capital")

# ─── Fee Structure ────────────────────────────────────────────────────────
# Coinbase Advanced (US) perps fee schedule
MAKER_FEE = 0.0000    # 0% maker
TAKER_FEE = 0.0006    # 0.06% taker
FUNDING_RATE_8H = 0.0001  # ~0.01% per 8h (BTC-PERP historical avg)

# ─── Perp Markets Available ────────────────────────────────────────────────
PERP_MARKETS = {
    "BTC-PERP": {
        "symbol": "BTC-PERP",
        "ccxt_symbol": "BTC/USDT:USDT",  # CCXT unified symbol
        "base": "BTC",
        "quote": "USDT",
        "settle": "USDT",
        "max_leverage": 5,    # VIP0
        "contract_size": 0.001,
        "tick_size": 0.1,
        "min_notional": 10.0,
    },
    "ETH-PERP": {
        "symbol": "ETH-PERP",
        "ccxt_symbol": "ETH/USDT:USDT",
        "base": "ETH",
        "quote": "USDT",
        "settle": "USDT",
        "max_leverage": 5,
        "contract_size": 0.01,
        "tick_size": 0.01,
        "min_notional": 10.0,
    },
}

# ─── Order Types ────────────────────────────────────────────────────────────

class OrderSide(Enum):
    BUY = auto()
    SELL = auto()

class OrderType(Enum):
    MARKET = auto()
    LIMIT = auto()
    STOP = auto()

@dataclass
class PerpOrder:
    """Internal order representation."""
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    size: float          # contracts
    price: Optional[float] = None  # None for market
    leverage: int = 5
    stop_price: Optional[float] = None
    status: str = "pending"
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    fee_paid: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

@dataclass
class PerpPosition:
    """Open position state."""
    symbol: str
    side: str        # "long" or "short"
    size: float      # contracts
    entry_price: float
    leverage: int
    unrealized_pnl: float = 0.0
    liquidation_price: float = 0.0
    margin: float = 0.0

@dataclass
class FundingRate:
    """Current funding rate info."""
    symbol: str
    rate: float          # e.g. 0.0001 = 0.01%
    next_funding_ts: float
    estimated_annual: float  # annualized


class CoinbasePerps:
    """
    Coinbase Advanced perpetual futures connector.

    Uses CCXT for exchange connectivity. Supports:
    - Market orders (immediate execution)
    - Limit orders (0% maker fee)
    - Position management
    - Funding rate tracking
    - Account balance/margin queries
    """

    def __init__(
        self,
        api_key: str = None,
        api_secret: str = None,
        passphrase: str = None,
        sandbox: bool = True,
    ):
        self.api_key = api_key or os.getenv("COINBASE_API_KEY", "")
        self.api_secret = api_secret or os.getenv("COINBASE_API_SECRET", "")
        self.passphrase = passphrase or os.getenv("COINBASE_PASSPHRASE", "")
        self.sandbox = sandbox
        self._client = None
        self._connected = False
        self._positions: Dict[str, PerpPosition] = {}
        self._orders: Dict[str, PerpOrder] = {}
        self._funding_rates: Dict[str, FundingRate] = {}
        self._balance: float = 0.0
        self._last_heartbeat: float = 0.0

        # Risk limits
        self.max_leverage = 5        # VIP0
        self.max_position_pct = 0.10  # 10% of bankroll per position

        # Cost model (for simulation)
        self.maker_fee = MAKER_FEE
        self.taker_fee = TAKER_FEE
        self.funding_per_8h = FUNDING_RATE_8H

    @property
    def total_cost_rate(self) -> float:
        """Round-trip cost for a market (taker) order: open + close."""
        return self.taker_fee * 2 + self.funding_per_8h * 0.375  # 3h avg hold

    async def connect(self):
        """Initialize CCXT exchange client."""
        try:
            import ccxt.async_support as ccxt_async
        except ImportError:
            log.error("ccxt not installed. Run: pip install ccxt")
            raise

        # Coinbase International for perps
        exchange_id = "coinbaseinternational"

        # Try to find the exchange class
        exchange_cls = getattr(ccxt_async, exchange_id, None)
        if exchange_cls is None:
            # Fallback to regular coinbase (fewer perp markets)
            log.warning("coinbaseinternational not in ccxt, falling back to coinbase")
            exchange_id = "coinbase"
            exchange_cls = ccxt_async.coinbase

        config = {
            "apiKey": self.api_key or "",
            "secret": self.api_secret or "",
            "password": self.passphrase or "",
            "enableRateLimit": True,
            "timeout": 15000,
        }

        if self.sandbox:
            config["sandbox"] = True
            log.info("Running in SANDBOX mode (testnet)")

        self._client = exchange_cls(config)

        try:
            await self._client.load_markets()
            n_markets = len(self._client.markets)
            log.info(f"Connected to {exchange_id}: {n_markets} markets loaded")
            self._connected = True
            self._last_heartbeat = time.time()
        except Exception as e:
            log.error(f"Failed to connect to {exchange_id}: {e}")
            await self._client.close()
            self._client = None
            raise

    async def close(self):
        """Close exchange connection."""
        if self._client:
            await self._client.close()
            self._client = None
            self._connected = False

    async def get_balance(self) -> float:
        """Get USDT/USDC balance."""
        if not self._client:
            return self._balance
        try:
            bal = await self._client.fetch_balance()
            # Try USDT first, then USDC
            for currency in ["USDT", "USDC", "USD"]:
                if currency in bal and "free" in bal[currency]:
                    self._balance = float(bal[currency]["free"])
                    return self._balance
            return 0.0
        except Exception as e:
            log.warning(f"Balance fetch failed: {e}")
            return self._balance

    async def get_funding_rate(self, symbol: str = "BTC-PERP") -> Optional[FundingRate]:
        """Fetch current funding rate."""
        if not self._client:
            return None

        ccxt_sym = PERP_MARKETS.get(symbol, {}).get("ccxt_symbol", "BTC/USDT:USDT")
        try:
            fr = await self._client.fetch_funding_rate(ccxt_sym)
            rate = float(fr.get("fundingRate", 0))
            next_ts = float(fr.get("fundingDatetime", 0))

            funding = FundingRate(
                symbol=symbol,
                rate=rate,
                next_funding_ts=next_ts,
                estimated_annual=rate * 3 * 365,  # 3x/day * 365
            )
            self._funding_rates[symbol] = funding
            return funding
        except Exception as e:
            log.warning(f"Funding rate fetch failed for {symbol}: {e}")
            return self._funding_rates.get(symbol)

    async def get_ticker(self, symbol: str = "BTC-PERP") -> Dict[str, float]:
        """Get current market data."""
        if not self._client:
            return {}

        ccxt_sym = PERP_MARKETS.get(symbol, {}).get("ccxt_symbol", "BTC/USDT:USDT")
        try:
            ticker = await self._client.fetch_ticker(ccxt_sym)
            return {
                "bid": float(ticker.get("bid", 0)),
                "ask": float(ticker.get("ask", 0)),
                "last": float(ticker.get("last", 0)),
                "volume": float(ticker.get("baseVolume", 0)),
                "change_24h": float(ticker.get("change", 0)),
                "high": float(ticker.get("high", 0)),
                "low": float(ticker.get("low", 0)),
            }
        except Exception as e:
            log.error(f"Ticker fetch failed for {symbol}: {e}")
            return {}

    async def get_orderbook(self, symbol: str = "BTC-PERP", limit: int = 20) -> Dict:
        """Get L2 orderbook depth."""
        if not self._client:
            return {"bids": [], "asks": []}

        ccxt_sym = PERP_MARKETS.get(symbol, {}).get("ccxt_symbol", "BTC/USDT:USDT")
        try:
            ob = await self._client.fetch_order_book(ccxt_sym, limit=limit)
            return {
                "bids": ob.get("bids", [])[:limit],
                "asks": ob.get("asks", [])[:limit],
                "timestamp": ob.get("timestamp", 0),
            }
        except Exception as e:
            log.error(f"Orderbook fetch failed for {symbol}: {e}")
            return {"bids": [], "asks": []}

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        size: float,
        order_type: OrderType = OrderType.MARKET,
        price: Optional[float] = None,
        leverage: int = 5,
        stop_price: Optional[float] = None,
    ) -> Optional[PerpOrder]:
        """
        Place a perp order.

        For limit orders: 0% maker fee (free!)
        For market orders: 0.06% taker fee

        Leverage capped at max_leverage.
        """
        if not self._connected or not self._client:
            log.error("Not connected to exchange")
            return None

        leverage = min(leverage, self.max_leverage)
        ccxt_sym = PERP_MARKETS.get(symbol, {}).get("ccxt_symbol", symbol)
        side_str = "buy" if side == OrderSide.BUY else "sell"

        # Set leverage first
        try:
            await self._client.set_leverage(leverage, ccxt_sym)
        except Exception as e:
            log.warning(f"Leverage set failed (might not be supported): {e}")

        params = {}
        if stop_price:
            params["stopPrice"] = stop_price
            params["triggerPrice"] = stop_price

        try:
            if order_type == OrderType.MARKET:
                result = await self._client.create_order(
                    ccxt_sym, "market", side_str, size, params=params
                )
            elif order_type == OrderType.LIMIT:
                if price is None:
                    log.error("Limit order requires a price")
                    return None
                result = await self._client.create_order(
                    ccxt_sym, "limit", side_str, size, price, params=params
                )
            elif order_type == OrderType.STOP:
                if stop_price is None:
                    log.error("Stop order requires stop_price")
                    return None
                result = await self._client.create_order(
                    ccxt_sym, "stop", side_str, size, stop_price, params=params
                )
            else:
                log.error(f"Unsupported order type: {order_type}")
                return None

            order = PerpOrder(
                order_id=result.get("id", ""),
                symbol=symbol,
                side=side,
                order_type=order_type,
                size=size,
                price=price or float(result.get("average", 0)),
                leverage=leverage,
                stop_price=stop_price,
                status=result.get("status", "unknown"),
                filled_size=float(result.get("filled", 0)),
                avg_fill_price=float(result.get("average", 0) or 0),
                fee_paid=float(result.get("fee", {}).get("cost", 0)),
                created_at=time.time(),
                updated_at=time.time(),
            )
            self._orders[order.order_id] = order
            log.info(f"Order placed: {order.order_id} {side_str} {size} {symbol} @ {price or 'market'}")
            return order

        except Exception as e:
            log.error(f"Order failed: {e}")
            return None

    async def close_position(self, symbol: str, size: Optional[float] = None) -> Optional[PerpOrder]:
        """Close an open position."""
        pos = self._positions.get(symbol)
        if pos is None:
            log.warning(f"No position for {symbol}")
            return None

        close_size = size or pos.size
        close_side = OrderSide.SELL if pos.side == "long" else OrderSide.BUY

        order = await self.place_order(
            symbol=symbol,
            side=close_side,
            size=close_size,
            order_type=OrderType.MARKET,
            leverage=pos.leverage,
        )
        return order

    async def get_positions(self) -> List[PerpPosition]:
        """Fetch open positions."""
        if not self._client:
            return list(self._positions.values())

        try:
            positions = await self._client.fetch_positions()
            self._positions.clear()
            for pos in positions:
                sym = pos.get("symbol", "")
                if float(pos.get("contracts", 0)) > 0:
                    pp = PerpPosition(
                        symbol=sym,
                        side=pos.get("side", "long"),
                        size=float(pos.get("contracts", 0)),
                        entry_price=float(pos.get("entryPrice", 0)),
                        leverage=int(pos.get("leverage", 1)),
                        unrealized_pnl=float(pos.get("unrealizedPnl", 0)),
                        liquidation_price=float(pos.get("liquidationPrice", 0)),
                        margin=float(pos.get("initialMargin", 0)),
                    )
                    self._positions[sym] = pp
            return list(self._positions.values())
        except Exception as e:
            log.warning(f"Position fetch failed: {e}")
            return list(self._positions.values())

    async def heartbeat(self) -> bool:
        """Check exchange connectivity."""
        if not self._client:
            return False
        try:
            await self._client.fetch_ticker("BTC/USDT:USDT")
            self._last_heartbeat = time.time()
            return True
        except Exception as e:
            log.error(f"Heartbeat failed: {e}")
            return False

    def cost_model(self, entry_price: float, size_usd: float, leverage: int,
                   hold_hours: float = 3.0, order_type: str = "market") -> Dict[str, float]:
        """
        Calculate transaction costs for a perp trade using Coinbase fees.

        Returns dict with entry_fee, exit_fee, funding_cost, total_cost, cost_pct.
        """
        fee_rate = self.taker_fee if order_type == "market" else self.maker_fee

        # Entry and exit fees (limit orders are FREE on Coinbase!)
        entry_fee = size_usd * fee_rate
        exit_fee = size_usd * (self.taker_fee if order_type == "limit" else self.taker_fee)

        # Funding cost: rate * position_size * (hold_hours / 8)
        funding_periods = hold_hours / 8.0
        funding_cost = size_usd * self.funding_per_8h * funding_periods

        total_cost = entry_fee + exit_fee + funding_cost
        cost_pct = total_cost / size_usd if size_usd > 0 else 0

        return {
            "entry_fee": round(entry_fee, 4),
            "exit_fee": round(exit_fee, 4),
            "funding_cost": round(funding_cost, 4),
            "total_cost": round(total_cost, 4),
            "cost_pct": round(cost_pct * 100, 4),  # as percentage
        }

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()


# ─── CLI ────────────────────────────────────────────────────────────────────

async def smoke_test():
    """Connect to Coinbase testnet and verify perp markets."""
    print("=" * 60)
    print("  Coinbase Perps — Smoke Test")
    print("=" * 60)

    # Try sandbox first
    connector = CoinbasePerps(sandbox=True)
    try:
        await connector.connect()
        print(f"\n✅ Connected to Coinbase International (sandbox)")

        # Check BTC-PERP
        ticker = await connector.get_ticker("BTC-PERP")
        if ticker:
            print(f"\n📊 BTC-PERP ticker:")
            for k, v in ticker.items():
                print(f"  {k}: {v}")
        else:
            print("\n⚠️  BTC-PERP ticker unavailable (sandbox may have no data)")

        # Funding rate
        fr = await connector.get_funding_rate("BTC-PERP")
        if fr:
            print(f"\n💸 Funding rate: {fr.rate*100:.4f}% ({fr.estimated_annual*100:.1f}% annualized)")
        else:
            print("\n⚠️  Funding rate unavailable in sandbox")

        # Cost model
        cost = connector.cost_model(
            entry_price=80000,
            size_usd=250,
            leverage=5,
            hold_hours=3.0,
            order_type="limit",
        )
        print(f"\n💰 Cost model (limit order, $250, 5x, 3h hold):")
        for k, v in cost.items():
            print(f"  {k}: {v}")

        # Balance
        bal = await connector.get_balance()
        print(f"\n💵 Balance: ${bal:.2f}")

        await connector.close()

    except Exception as e:
        print(f"\n❌ Sandbox connection failed: {e}")
        print("   This likely means no API keys configured yet.")
        print("   Steps to fix:")
        print("   1. Create Coinbase Advanced API key at:")
        print("      https://www.coinbase.com/settings/api")
        print("   2. Add to .env:")
        print("      COINBASE_API_KEY=your_key")
        print("      COINBASE_API_SECRET=your_secret")
        print("      COINBASE_PASSPHRASE=your_passphrase")
        print("   3. Re-run this test")


if __name__ == "__main__":
    import sys
    asyncio.run(smoke_test())