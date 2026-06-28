#!/usr/bin/env python3
"""
FDC Kalshi Live Trading Engine
CFTC-regulated prediction markets. No geoblock, fiat deposits, RSA auth.

Modules:
  - KalshiAuth:   RSA key management, session init
  - MarketScanner: Discover high-liquidity events/markets
  - OrderManager:  Place, cancel, track orders
  -KillSwitch:     Circuit breaker (max daily loss, max drawdown)
  - KalshiEngine:  Main loop orchestrator

Author: Hugh (3rd of 5)
Date: 2026-05-20
"""

import json, os, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend

# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════

PAPER_ONLY = True  # Flip to False when funded + account verified
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
PROJECT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital")
ENV_FILE = PROJECT_DIR / ".env"
PRIVATE_KEY_FILE = PROJECT_DIR / "kalshi_private_key.pem"

# Risk parameters
MAX_POSITION_PER_MARKET = 500.0    # $ max per market
MAX_DAILY_LOSS = 50.0              # $ max daily loss
MAX_DRAWDOWN_PCT = 0.40           # 40% drawdown halt
MIN_LIQUIDITY = 1000.0            # $ min volume to trade
MIN_SPREAD_CENTS = 5              # 5¢ min spread for edge
MAX_MARKETS_OPEN = 5              # Max concurrent positions


def _load_env() -> dict:
    """Load .env vars."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


# ══════════════════════════════════════════════════════════════════════════════
# Auth
# ══════════════════════════════════════════════════════════════════════════════

class KalshiAuth:
    """Manage Kalshi RSA key authentication and session."""

    def __init__(self):
        self.key_id: Optional[str] = None
        self.private_key = None
        self.user_id: Optional[str] = None
        self._client = None

    def load_keys(self) -> dict:
        """Load RSA private key + key_id from .env and PEM file."""
        env = _load_env()
        self.key_id = env.get("KALSHI_KEY_ID", "")

        if not self.key_id:
            return {"error": "KALSHI_KEY_ID not set in .env (get from Kalshi dashboard after account creation)", "ready": False}

        if not PRIVATE_KEY_FILE.exists():
            return {"error": f"Private key file not found: {PRIVATE_KEY_FILE}", "ready": False}

        try:
            pem_data = PRIVATE_KEY_FILE.read_bytes()
            self.private_key = serialization.load_pem_private_key(
                pem_data, password=None, backend=default_backend()
            )
        except Exception as e:
            return {"error": f"Failed to load private key: {e}", "ready": False}

        self.user_id = env.get("KALSHI_USER_ID", "")

        return {
            "ready": True,
            "key_id": self.key_id[:8] + "...",
            "user_id": self.user_id[:8] + "..." if self.user_id else "not set",
            "key_type": "RSA-4096",
        }

    def get_client(self):
        """Get authenticated KalshiClient instance."""
        if self._client:
            return self._client

        if not self.key_id or not self.private_key:
            raise RuntimeError("Auth not initialized. Call load_keys() first.")

        from kalshi_client.client import KalshiClient
        self._client = KalshiClient(
            key_id=self.key_id,
            private_key=self.private_key,
            exchange_api_base=KALSHI_API_BASE,
            rate_limit=5,
        )
        return self._client


# ══════════════════════════════════════════════════════════════════════════════
# Market Scanner
# ══════════════════════════════════════════════════════════════════════════════

class MarketScanner:
    """Discover and filter Kalshi markets for trading opportunities."""

    def __init__(self, auth: KalshiAuth):
        self.auth = auth
        self._last_scan = None
        self._markets_cache: List[dict] = []

    def scan_markets(self, categories: List[str] = None, min_liquidity: float = MIN_LIQUIDITY) -> List[dict]:
        """Scan for tradeable markets. Public access — no auth needed.
        
        Uses /events?with_nested_markets=true to get volume data,
        then flattens all binary markets with liquidity.
        """
        import urllib.request

        # Strategy: fetch events with nested markets to get volume data
        # The /markets endpoint alone returns provisional markets with 0 volume
        all_markets = []
        cursor = None

        for _ in range(10):  # Max 10 pages of events
            url = f"{KALSHI_API_BASE}/events?limit=200&status=open&with_nested_markets=true"
            if categories:
                for cat in (categories or []):
                    url += f"&category={cat.replace(' ', '%20')}"
            if cursor:
                url += f"&cursor={cursor}"

            try:
                req = urllib.request.Request(url, headers={"User-Agent": "fdc/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
            except Exception as e:
                print(f"[Scanner] Error fetching events: {e}")
                break

            events = data.get("events", [])
            if not events:
                break

            for ev in events:
                for m in ev.get("markets", []):
                    m["_event_ticker"] = ev.get("event_ticker", "")
                    m["_event_category"] = ev.get("category", "")
                    m["_event_title"] = ev.get("title", "")
                    all_markets.append(m)

            cursor = data.get("cursor")
            if not cursor:
                break

        # Also fetch standalone markets (not nested in events)
        m_cursor = None
        for _ in range(5):
            url = f"{KALSHI_API_BASE}/markets?limit=200&status=open"
            if m_cursor:
                url += f"&cursor={m_cursor}"

            try:
                req = urllib.request.Request(url, headers={"User-Agent": "fdc/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
            except Exception as e:
                break

            for m in data.get("markets", []):
                # Only add if not already included from events
                ticker = m.get("ticker", "")
                if not any(em.get("ticker") == ticker for em in all_markets):
                    m["_event_ticker"] = m.get("event_ticker", "")
                    m["_event_category"] = m.get("category", "")
                    m["_event_title"] = m.get("title", "")
                    all_markets.append(m)

            m_cursor = data.get("cursor")
            if not m_cursor:
                break

        self._markets_cache = all_markets
        self._last_scan = datetime.now(timezone.utc)
        return self._filter_markets(all_markets, min_liquidity)

    def _filter_markets(self, markets: List[dict], min_liquidity: float) -> List[dict]:
        """Filter markets by liquidity, spread, and tradeability."""
        tradeable = []
        for m in markets:
            # Skip multivariate/complex markets — focus on binary
            if m.get("market_type") != "binary":
                continue
            # Skip provisional markets
            if m.get("is_provisional"):
                continue

            ticker = m.get("ticker", "")
            yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
            yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
            volume = float(m.get("volume_fp", 0) or 0)
            oi = float(m.get("open_interest_fp", 0) or 0)
            liquidity = float(m.get("liquidity_dollars", 0) or 0)

            # Must have both bid and ask (active market)
            if yes_bid <= 0 or yes_ask <= 0:
                continue

            spread = (yes_ask - yes_bid) * 100  # in cents
            mid = (yes_bid + yes_ask) / 2

            # Filter by liquidity — use best available metric
            total_liquidity = max(volume, oi, liquidity)
            if total_liquidity < min_liquidity:
                continue

            tradeable.append({
                "ticker": ticker,
                "title": m.get("_event_title", m.get("title", ""))[:80],
                "subtitle": m.get("subtitle", m.get("yes_sub_title", ""))[:60],
                "category": m.get("_event_category", m.get("event_ticker", "").split("-")[0] if "-" in m.get("event_ticker", "") else "unknown"),
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "mid": round(mid, 4),
                "spread_cents": round(spread, 1),
                "volume": volume,
                "open_interest": oi,
                "liquidity": liquidity,
                "close_time": m.get("close_time", ""),
                "expiration_time": m.get("expiration_time", ""),
                "raw": m,
            })

        # Sort by volume descending (most liquid first)
        tradeable.sort(key=lambda x: x["volume"], reverse=True)
        return tradeable

    def scan_events(self, categories: List[str] = None) -> List[dict]:
        """Scan for events with nested markets. Public access."""
        import urllib.request

        events = []
        cursor = None

        for _ in range(5):
            url = f"{KALSHI_API_BASE}/events?limit=200&status=open"
            if categories:
                for cat in categories:
                    url += f"&category={cat.replace(' ', '%20')}"
            if cursor:
                url += f"&cursor={cursor}"

            try:
                req = urllib.request.Request(url, headers={"User-Agent": "fdc/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
            except Exception as e:
                print(f"[Scanner] Error fetching events: {e}")
                break

            page = data.get("events", [])
            if not page:
                break
            events.extend(page)
            cursor = data.get("cursor")
            if not cursor:
                break

        return events

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Get orderbook for a specific market. Public access."""
        import urllib.request

        url = f"{KALSHI_API_BASE}/markets/{ticker}/orderbook?depth={depth}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fdc/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            
            # Kalshi returns orderbook_fp with dollar-denominated prices
            ob = data.get("orderbook_fp", {})
            bids = [[float(p), float(s)] for p, s in ob.get("yes_dollars", [])]  # yes side
            asks = [[float(p), float(s)] for p, s in ob.get("no_dollars", [])]   # no side
            
            return {
                "ticker": ticker,
                "bids": bids,  # yes bids: [price, size]
                "asks": asks,  # no asks: [price, size]
                "raw": data,
            }
        except Exception as e:
            return {"error": str(e), "ticker": ticker}


# ══════════════════════════════════════════════════════════════════════════════
# Order Manager
# ══════════════════════════════════════════════════════════════════════════════

class OrderManager:
    """Place, track, and cancel Kalshi orders."""

    def __init__(self, auth: KalshiAuth, paper: bool = PAPER_ONLY):
        self.auth = auth
        self.paper = paper
        self._orders: Dict[str, dict] = {}  # order_id -> order state
        self._fills: List[dict] = []

    def place_order(self, ticker: str, side: str, yes_price: int, count: int,
                    action: str = "buy", order_type: str = "limit",
                    expiration_ts: int = None) -> dict:
        """
        Place a Kalshi order.

        Args:
            ticker: Market ticker (e.g. "KXELONMARS-99")
            side: "yes" or "no"
            yes_price: Price in cents (1-99 for yes, or use no_price)
            count: Number of contracts
            action: "buy" or "sell"
            order_type: "limit" or "market"
            expiration_ts: Unix timestamp for GTD orders
        """
        client_order_id = f"fdc_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"

        if self.paper:
            # Simulate fill at mid-price
            scanner = MarketScanner(self.auth)
            book = scanner.get_orderbook(ticker)
            if "error" in book:
                return {"error": book["error"], "simulated": True}

            sim_price = yes_price
            if book.get("bids") and book.get("asks"):
                best_bid = int(book["bids"][0][0]) if book["bids"] else yes_price
                best_ask = int(book["asks"][0][0]) if book["asks"] else yes_price
                sim_price = (best_bid + best_ask) // 2

            order = {
                "order_id": client_order_id,
                "status": "SIMULATED",
                "ticker": ticker,
                "side": side,
                "action": action,
                "yes_price": sim_price,
                "count": count,
                "estimated_cost_cents": sim_price * count if side == "yes" else (100 - sim_price) * count,
                "mode": "PAPER",
            }
            self._orders[client_order_id] = order
            return order

        # LIVE mode
        try:
            client = self.auth.get_client()
        except Exception as e:
            return {"error": f"Auth failed: {e}"}

        order_params = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
            "yes_price": yes_price,
        }
        if expiration_ts:
            order_params["expiration_ts"] = expiration_ts

        try:
            result = client.create_order(**order_params)
            result["mode"] = "LIVE"
            result["client_order_id"] = client_order_id
            self._orders[client_order_id] = result
            return result
        except Exception as e:
            return {"error": str(e), "client_order_id": client_order_id}

    def batch_place_orders(self, orders: List[dict]) -> dict:
        """Place multiple orders at once. Each order dict has same keys as place_order."""
        if self.paper:
            results = []
            for o in orders:
                results.append(self.place_order(**o))
            return {"orders": results, "mode": "PAPER"}

        try:
            client = self.auth.get_client()
        except Exception as e:
            return {"error": f"Auth failed: {e}"}

        try:
            result = client.batch_create_orders(orders)
            return {"result": result, "mode": "LIVE"}
        except Exception as e:
            return {"error": str(e)}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order."""
        if self.paper:
            if order_id in self._orders:
                self._orders[order_id]["status"] = "CANCELLED"
            return {"order_id": order_id, "status": "CANCELLED", "mode": "PAPER"}

        try:
            client = self.auth.get_client()
            result = client.cancel_order(order_id)
            return {"order_id": order_id, "status": "cancelled", "result": result, "mode": "LIVE"}
        except Exception as e:
            return {"error": str(e), "order_id": order_id}

    def cancel_all(self, ticker: str = None) -> dict:
        """Cancel all open orders, optionally filtered by ticker."""
        if self.paper:
            cancelled = 0
            for oid, order in self._orders.items():
                if order.get("status") not in ("CANCELLED", "settled"):
                    if ticker is None or order.get("ticker") == ticker:
                        order["status"] = "CANCELLED"
                        cancelled += 1
            return {"cancelled": cancelled, "mode": "PAPER"}

        try:
            client = self.auth.get_client()
            # Get open orders first
            open_orders = client.get_orders(ticker=ticker) if ticker else client.get_orders()
            order_ids = [o.get("id") for o in open_orders.get("orders", [])]
            if order_ids:
                result = client.batch_cancel_orders(order_ids)
                return {"cancelled": len(order_ids), "result": result, "mode": "LIVE"}
            return {"cancelled": 0, "mode": "LIVE"}
        except Exception as e:
            return {"error": str(e)}

    def get_positions(self) -> dict:
        """Get current positions."""
        if self.paper:
            # Aggregate from simulated orders
            positions = {}
            for oid, order in self._orders.items():
                if order.get("status") == "SIMULATED":
                    ticker = order["ticker"]
                    if ticker not in positions:
                        positions[ticker] = {"count": 0, "total_cost": 0}
                    side = order.get("side", "yes")
                    count = order.get("count", 0)
                    cost = order.get("estimated_cost_cents", 0)
                    if side == "yes":
                        positions[ticker]["count"] += count
                        positions[ticker]["total_cost"] += cost
                    else:
                        positions[ticker]["count"] -= count
                        positions[ticker]["total_cost"] -= cost
            return {"positions": positions, "mode": "PAPER"}

        try:
            client = self.auth.get_client()
            result = client.get_positions(settlement_status="unsettled")
            return {"positions": result, "mode": "LIVE"}
        except Exception as e:
            return {"error": str(e)}

    def get_balance(self) -> dict:
        """Get account balance."""
        if self.paper:
            return {"balance": "PAPER", "mode": "PAPER"}

        try:
            client = self.auth.get_client()
            result = client.get_balance()
            return {"balance": result, "mode": "LIVE"}
        except Exception as e:
            return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# Kill Switch
# ══════════════════════════════════════════════════════════════════════════════

class KillSwitch:
    """Emergency circuit breaker. Enforces max daily loss, max drawdown, total halt."""

    def __init__(self, max_daily_loss: float = MAX_DAILY_LOSS,
                 max_drawdown_pct: float = MAX_DRAWDOWN_PCT):
        self.max_daily_loss = max_daily_loss
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_pnl: Dict[str, float] = {}
        self.peak_capital: Optional[float] = None
        self.halted = False
        self.halt_reason = ""

    def check(self, capital: float, today: str, daily_pnl: float) -> Tuple[bool, str]:
        """Check safety limits. Returns (allowed: bool, reason: str)."""
        if self.halted:
            return False, f"PERMANENTLY HALTED: {self.halt_reason}"

        # Track peak
        if self.peak_capital is None:
            self.peak_capital = capital
        self.peak_capital = max(self.peak_capital, capital)

        # Daily loss check
        if daily_pnl < -self.max_daily_loss:
            self.halted = True
            self.halt_reason = f"Daily loss ${daily_pnl:+.2f} exceeds ${self.max_daily_loss} limit"
            return False, self.halt_reason

        # Drawdown check
        dd = (self.peak_capital - capital) / self.peak_capital
        if dd > self.max_drawdown_pct:
            self.halted = True
            self.halt_reason = f"Drawdown {dd:.1%} exceeds {self.max_drawdown_pct:.0%} limit"
            return False, self.halt_reason

        # Capital floor
        if capital < 50:
            self.halted = True
            self.halt_reason = f"Capital ${capital:.2f} below minimum threshold"
            return False, self.halt_reason

        return True, "OK"

    def reset(self):
        """Reset kill switch (requires manual intervention)."""
        self.halted = False
        self.halt_reason = ""
        self.peak_capital = None


# ══════════════════════════════════════════════════════════════════════════════
# Strategy Signals
# ══════════════════════════════════════════════════════════════════════════════

class StrategyEngine:
    """Evaluate markets and generate trade signals."""

    def __init__(self, min_spread_cents: int = MIN_SPREAD_CENTS,
                 min_edge_pct: float = 0.05):
        self.min_spread_cents = min_spread_cents
        self.min_edge_pct = min_edge_pct

    def evaluate_market(self, market: dict) -> Optional[dict]:
        """
        Evaluate a single market for trading opportunity.
        Returns signal dict if edge found, None otherwise.
        """
        spread = market.get("spread_cents", 0)
        mid = market.get("mid", 0)
        yes_bid = market.get("yes_bid", 0)
        yes_ask = market.get("yes_ask", 0)
        volume = market.get("volume", 0)

        # Must have enough liquidity
        if volume < MIN_LIQUIDITY:
            return None

        # Spread too wide = illiquid, too narrow = no edge
        if spread > 20 or spread < 1:
            return None

        # Direction signals based on price levels
        signals = []

        # Signal: Deep out-of-the-money (cheap yes contracts with potential)
        if yes_bid <= 0.10 and yes_ask <= 0.15 and volume > 5000:
            signals.append({
                "type": "CHEAP_YES",
                "direction": "yes",
                "price": yes_ask,
                "rationale": f"Deep OTM yes at {yes_ask:.2f} with ${volume:.0f} vol",
                "expected_value": round(1.0 - yes_ask, 2),
                "risk_reward": round((1.0 - yes_ask) / yes_ask, 1),
            })

        # Signal: High confidence outcomes (expensive yes, near settlement)
        if yes_bid >= 0.85 and yes_ask <= 0.95 and volume > 10000:
            ev = 1.0 - yes_ask
            rr = round((1.0 - yes_ask) / max(yes_ask, 0.01), 1) if ev > 0 else 0
            signals.append({
                "type": "HIGH_CONFIDENCE",
                "direction": "yes",
                "price": yes_ask,
                "rationale": f"High confidence yes at {yes_ask:.2f} with ${volume:.0f} vol",
                "expected_value": round(ev, 2),
                "risk_reward": rr,
            })

        # Signal: Overpriced (sell no when market panics)
        if yes_bid >= 0.70 and yes_ask >= 0.80:
            no_price = 1.0 - yes_ask
            no_ev = yes_ask  # if yes is overpriced, no has value
            no_rr = round(yes_ask / max(no_price, 0.01), 1) if no_price > 0 else 0
            signals.append({
                "type": "OVERPRICED",
                "direction": "no",
                "price": 100 - int(yes_ask * 100),
                "rationale": f"No side at {1.0 - yes_ask:.2f} when yes at {yes_ask:.2f}",
                "expected_value": round(no_ev, 2),
                "risk_reward": no_rr,
            })

        if not signals:
            return None

        return {
            "ticker": market["ticker"],
            "title": market["title"],
            "mid": mid,
            "volume": volume,
            "signals": signals,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def rank_opportunities(self, markets: List[dict]) -> List[dict]:
        """Scan all markets and rank by signal strength."""
        results = []
        for m in markets:
            signal = self.evaluate_market(m)
            if signal:
                results.append(signal)

        # Sort by volume * risk_reward
        for r in results:
            best_rr = max(s.get("risk_reward", 0) for s in r["signals"])
            r["score"] = r["volume"] * best_rr / 1000

        results.sort(key=lambda x: x["score"], reverse=True)
        return results


# ══════════════════════════════════════════════════════════════════════════════
# BTC Scanner
# ══════════════════════════════════════════════════════════════════════════════

class BTCScanner:
    """Scan Kalshi BTC markets (daily, weekly, monthly, yearly) and find edge."""

    def __init__(self, auth: KalshiAuth):
        self.auth = auth
        self._api = KALSHI_API_BASE

    def scan_btc(self) -> dict:
        """Fetch all BTC-related markets and categorize by timeframe."""
        import urllib.request

        btc_events = []
        all_markets = []
        cursor = None

        for _ in range(30):
            url = f"{self._api}/events?limit=200&status=open&with_nested_markets=true"
            if cursor:
                url += f"&cursor={cursor}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "fdc/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
            except:
                break

            for ev in data.get("events", []):
                title = ev.get("title", "").lower()
                subtitle = ev.get("sub_title", "").lower()
                if "btc" in title or "bitcoin" in title or "btc" in subtitle:
                    btc_events.append(ev)
                    for m in ev.get("markets", []):
                        m["_event_title"] = ev.get("title", "")
                        m["_event_ticker"] = ev.get("event_ticker", "")
                        m["_category"] = ev.get("category", "")
                        all_markets.append(m)

            cursor = data.get("cursor")
            if not cursor:
                break

        # Categorize by timeframe
        intraday = []  # closes < 72h (includes "daily" settlement markets)
        weekly = []    # closes < 8 days
        monthly = []   # closes < 35 days
        yearly = []    # everything else

        now = datetime.now(timezone.utc)

        for m in all_markets:
            yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
            yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
            if yes_bid <= 0 or yes_ask <= 0:
                continue

            vol = float(m.get("volume_fp", 0) or 0)
            oi = float(m.get("open_interest_fp", 0) or 0)
            close_str = m.get("close_time", "") or m.get("expiration_time", "")

            try:
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                hours_left = (close_dt - now).total_seconds() / 3600
            except:
                hours_left = 999999

            # Skip expired markets
            if hours_left < 0.1:
                continue

            entry = {
                "ticker": m.get("ticker", "?"),
                "title": m.get("title", "?")[:70],
                "event": m.get("_event_title", ""),
                "subtitle": m.get("yes_sub_title", m.get("subtitle", ""))[:60],
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "mid": round((yes_bid + yes_ask) / 2, 4),
                "spread_cents": round((yes_ask - yes_bid) * 100, 1),
                "volume": vol,
                "oi": oi,
                "hours_left": round(hours_left, 1),
                "close_time": close_str[:16],
            }

            if hours_left < 72:
                intraday.append(entry)
            elif hours_left < 192:  # 8 days
                weekly.append(entry)
            elif hours_left < 840:  # 35 days
                monthly.append(entry)
            else:
                yearly.append(entry)

        # Sort each by volume
        intraday.sort(key=lambda x: x["volume"], reverse=True)
        weekly.sort(key=lambda x: x["volume"], reverse=True)
        monthly.sort(key=lambda x: x["volume"], reverse=True)
        yearly.sort(key=lambda x: x["volume"], reverse=True)

        return {
            "intraday": intraday,
            "weekly": weekly,
            "monthly": monthly,
            "yearly": yearly,
            "total_markets": len(all_markets),
            "total_events": len(btc_events),
        }

    def best_daily_btc(self, scan_result: dict = None) -> List[dict]:
        """Find the best BTC daily price markets (tightest timeframe)."""
        if scan_result is None:
            scan_result = self.scan_btc()
        intraday = scan_result.get("intraday", [])
        # Filter for liquid, tight-spread markets
        return [m for m in intraday if m["volume"] > 5000 and m["spread_cents"] <= 5]


# ══════════════════════════════════════════════════════════════════════════════
# Daily $20 Scanner (tight timeframe, liquid, tight spread)
# ══════════════════════════════════════════════════════════════════════════════

class DailyTwentyScanner:
    """Find Kalshi markets that can realistically generate $20/day with minimal capital."""

    def __init__(self, auth: KalshiAuth, target_daily: float = 20.0):
        self.auth = auth
        self._api = KALSHI_API_BASE
        self.target_daily = target_daily

    def scan(self, max_hours: float = 168, min_volume: float = 5000) -> dict:
        """Scan all markets closing within max_hours for $20/day opportunities.

        Strategy: Find markets where the capital needed to earn $20 is minimized.
        Capital needed = target_daily / edge_per_contract
        Where edge_per_contract = spread or directional conviction %.
        """
        import urllib.request
        now = datetime.now(timezone.utc)

        all_markets = []
        cursor = None

        for _ in range(30):
            url = f"{self._api}/events?limit=200&status=open&with_nested_markets=true"
            if cursor:
                url += f"&cursor={cursor}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "fdc/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
            except:
                break

            for ev in data.get("events", []):
                for m in ev.get("markets", []):
                    m["_event_title"] = ev.get("title", "")
                    m["_event_ticker"] = ev.get("event_ticker", "")
                    m["_category"] = ev.get("category", "")
                    all_markets.append(m)

            cursor = data.get("cursor")
            if not cursor:
                break

        # Filter and score
        opportunities = []

        for m in all_markets:
            yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
            yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
            if yes_bid <= 0 or yes_ask <= 0:
                continue

            vol = float(m.get("volume_fp", 0) or 0)
            oi = float(m.get("open_interest_fp", 0) or 0)
            if max(vol, oi) < min_volume:
                continue

            close_str = m.get("close_time", "") or m.get("expiration_time", "")
            try:
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                hours_left = (close_dt - now).total_seconds() / 3600
                if hours_left < 0.5 or hours_left > max_hours:
                    continue
            except:
                continue

            spread = (yes_ask - yes_bid) * 100  # cents
            mid = (yes_bid + yes_ask) / 2

            # === Strategy 1: Market-making (flip the spread) ===
            # Capital needed to make $20 per spread fill
            if spread > 0:
                mm_capital = self.target_daily / (spread / 100)  # $ per contract
            else:
                mm_capital = 999999

            # === Strategy 2: Directional bet on near-certain outcomes ===
            # At 0.95+ yes price, EV per contract ≈ (1 - 0.95) = $0.05
            # Need 20/0.05 = $400 in contracts → risk = 400 * 0.95 = $380
            if mid >= 0.90:
                ev_per_contract = 1.0 - mid
                directional_capital = self.target_daily / max(ev_per_contract, 0.001)
                directional_risk = directional_capital * mid
            elif mid <= 0.10:
                ev_per_contract = mid
                directional_capital = self.target_daily / max(ev_per_contract, 0.001)
                directional_risk = directional_capital * mid
            else:
                # Mid-range: need ~10% directional conviction
                ev_per_contract = 0.10
                directional_capital = self.target_daily / ev_per_contract
                directional_risk = directional_capital * 0.5

            # === Strategy 3: Cheap longshots (low risk, high reward) ===
            # At $0.05 per contract, 20 contracts = $1 risk, max win = $20
            if yes_ask <= 0.10 and yes_ask > 0:
                longshot_capital = self.target_daily / (1.0 - yes_ask)
                longshot_risk = longshot_capital * yes_ask
            else:
                longshot_capital = 999999
                longshot_risk = 999999

            # Best strategy for this market
            strategies = []
            if mm_capital <= 2000:
                strategies.append({
                    "type": "MARKET_MAKE",
                    "capital_needed": round(mm_capital, 2),
                    "risk": round(mm_capital, 2),  # worst case
                    "edge_per_contract": round(spread / 100, 4),
                })
            if directional_capital <= 2000 and directional_risk <= 500:
                strategies.append({
                    "type": "DIRECTIONAL",
                    "capital_needed": round(directional_capital, 2),
                    "risk": round(directional_risk, 2),
                    "edge_per_contract": round(ev_per_contract, 4),
                })
            if longshot_capital <= 2000 and longshot_risk <= 50:
                strategies.append({
                    "type": "LONGSHOT",
                    "capital_needed": round(longshot_capital, 2),
                    "risk": round(longshot_risk, 2),
                    "edge_per_contract": round(1.0 - yes_ask, 4),
                })

            if not strategies:
                continue

            # Best strategy = lowest capital needed
            strategies.sort(key=lambda s: s["capital_needed"])
            best = strategies[0]

            opportunities.append({
                "ticker": m.get("ticker", "?")[:45],
                "title": m.get("_event_title", m.get("title", ""))[:65],
                "subtitle": m.get("yes_sub_title", m.get("subtitle", ""))[:45],
                "category": m.get("_category", "?"),
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "mid": round(mid, 4),
                "spread_cents": round(spread, 1),
                "volume": vol,
                "oi": oi,
                "hours_left": round(hours_left, 1),
                "close_time": close_str[:16],
                "best_strategy": best["type"],
                "capital_needed": best["capital_needed"],
                "risk": best["risk"],
                "edge_per_contract": best["edge_per_contract"],
                "strategies": strategies,
                "score": vol / max(best["capital_needed"], 1),  # higher = better
            })

        # Sort by score (volume / capital_needed)
        opportunities.sort(key=lambda x: x["score"], reverse=True)

        # Categorize
        feasible = [o for o in opportunities if o["capital_needed"] <= 500]
        moderate = [o for o in opportunities if 500 < o["capital_needed"] <= 2000]

        return {
            "feasible": feasible,
            "moderate": moderate,
            "total_scanned": len(all_markets),
            "total_qualified": len(opportunities),
        }


# ══════════════════════════════════════════════════════════════════════════════

class KalshiEngine:
    """Main orchestrator. Scans markets, evaluates signals, places orders."""

    def __init__(self, paper: bool = PAPER_ONLY):
        self.paper = paper
        self.auth = KalshiAuth()
        self.scanner = MarketScanner(self.auth)
        self.order_mgr = OrderManager(self.auth, paper=paper)
        self.strategy = StrategyEngine()
        self.kill_switch = KillSwitch()
        self.btc_scanner = BTCScanner(self.auth)
        self.daily20_scanner = DailyTwentyScanner(self.auth)
        self._running = False
        self._log: List[dict] = []

    def _log_msg(self, msg: str, level: str = "INFO"):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "msg": msg,
        }
        self._log.append(entry)
        if len(self._log) > 1000:
            self._log = self._log[-500:]
        prefix = "📉" if "loss" in msg.lower() or "halt" in msg.lower() else "📊"
        print(f"[{entry['ts'][:19]}] [{level}] {prefix} {msg}")

    def init(self) -> dict:
        """Initialize auth and verify connectivity."""
        # Test public API first (no auth needed)
        test_markets = self.scanner.scan_markets(min_liquidity=0)
        if not test_markets and not self.scanner._markets_cache:
            return {"error": "Cannot reach Kalshi API", "ready": False}

        self._log_msg(f"Kalshi API reachable. {len(self.scanner._markets_cache)} total markets cached.")

        # Load auth keys
        auth_status = self.auth.load_keys()
        if auth_status.get("ready"):
            self._log_msg(f"Auth keys loaded: key_id={auth_status.get('key_id')}, type={auth_status.get('key_type')}")
        else:
            self._log_msg(f"Auth not configured: {auth_status.get('error')}. Paper mode only until KALSHI_KEY_ID set.")

        mode = "PAPER" if self.paper else "LIVE"
        self._log_msg(f"Engine initialized in {mode} mode.")

        return {
            "ready": True,
            "mode": mode,
            "api_reachable": True,
            "total_markets": len(self.scanner._markets_cache),
            "auth": auth_status,
        }

    def scan(self, categories: List[str] = None) -> List[dict]:
        """Run a full market scan and return ranked opportunities."""
        self._log_msg("Scanning markets...")
        markets = self.scanner.scan_markets(categories=categories)
        self._log_msg(f"Found {len(markets)} tradeable markets after filtering.")

        opportunities = self.strategy.rank_opportunities(markets)
        self._log_msg(f"Identified {len(opportunities)} trading opportunities.")

        for opp in opportunities[:5]:
            for sig in opp["signals"]:
                self._log_msg(
                    f"  {opp['ticker']}: {sig['type']} {sig['direction']} @ {sig['price']:.2f} "
                    f"(RR: {sig['risk_reward']:.1f}x, Vol: ${opp['volume']:.0f})"
                )

        return opportunities

    def execute_signal(self, signal: dict, ticker: str, count: int = 10) -> dict:
        """Execute a single trade signal."""
        if self.kill_switch.halted:
            return {"error": f"Kill switch active: {self.kill_switch.halt_reason}"}

        # Size check
        est_cost = signal["price"] * count  # in dollars
        if est_cost > MAX_POSITION_PER_MARKET:
            count = int(MAX_POSITION_PER_MARKET / max(signal["price"], 0.01))

        # Convert price to cents for Kalshi API
        yes_price_cents = int(signal["price"] * 100)
        if signal["direction"] == "no":
            yes_price_cents = 100 - yes_price_cents

        self._log_msg(
            f"Placing {signal['direction'].upper()} order: {ticker} x{count} "
            f"@ {signal['price']:.2f} (${est_cost:.2f} est) [{signal['type']}]"
        )

        result = self.order_mgr.place_order(
            ticker=ticker,
            side=signal["direction"],
            yes_price=yes_price_cents,
            count=count,
            action="buy",
        )

        if "error" in result:
            self._log_msg(f"Order failed: {result['error']}", level="ERROR")
        else:
            self._log_msg(f"Order placed: {result.get('order_id', '?')} status={result.get('status', '?')}")

        return result

    def status(self) -> dict:
        """Get engine status summary."""
        capital = 0.0
        daily_pnl = 0.0

        if not self.paper:
            balance = self.order_mgr.get_balance()
            if "error" not in balance:
                # Parse balance from Kalshi response
                capital = float(balance.get("balance", {}).get("value", 0)) / 100

        return {
            "mode": "PAPER" if self.paper else "LIVE",
            "running": self._running,
            "halted": self.kill_switch.halted,
            "halt_reason": self.kill_switch.halt_reason if self.kill_switch.halted else None,
            "capital": capital,
            "daily_pnl": daily_pnl,
            "open_orders": len([o for o in self.order_mgr._orders.values()
                                 if o.get("status") not in ("CANCELLED", "settled", "SIMULATED")]),
            "total_orders": len(self.order_mgr._orders),
            "log_entries": len(self._log),
        }

    def run_loop(self, interval_seconds: int = 60, max_iterations: int = 100,
                 categories: List[str] = None):
        """Main trading loop. Scans, evaluates, and executes."""
        self._running = True
        self._log_msg(f"Starting trading loop (interval={interval_seconds}s, max={max_iterations} iterations)")

        status = self.init()
        if not status.get("ready"):
            self._log_msg(f"Init failed: {status}", level="ERROR")
            return

        for i in range(max_iterations):
            if not self._running:
                self._log_msg("Loop stopped by external signal.")
                break

            # Scan for opportunities
            opportunities = self.scan(categories=categories)

            # Execute top opportunities (max MAX_MARKETS_OPEN concurrent)
            current_positions = self.order_mgr.get_positions()
            open_count = 0
            if isinstance(current_positions.get("positions"), dict):
                open_count = len([p for p in current_positions["positions"].values()
                                  if p.get("count", 0) != 0])

            slots_remaining = MAX_MARKETS_OPEN - open_count

            for opp in opportunities[:slots_remaining]:
                for signal in opp["signals"][:1]:  # Take top signal per market
                    result = self.execute_signal(signal, opp["ticker"])
                    if "error" not in result:
                        break  # One order per market per cycle

            # Kill switch check (with estimated PnL)
            # In paper mode, we track simulated PnL
            # In live mode, we'd query actual balance
            ok, reason = self.kill_switch.check(250.0, datetime.now().strftime("%Y-%m-%d"), 0.0)
            if not ok:
                self._log_msg(f"🛑 KILL SWITCH: {reason}", level="CRITICAL")
                break

            self._log_msg(f"Cycle {i+1}/{max_iterations} complete. Sleeping {interval_seconds}s...")
            time.sleep(interval_seconds)

        self._running = False
        self._log_msg("Trading loop ended.")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="FDC Kalshi Trading Engine")
    parser.add_argument("--live", action="store_true", help="Enable LIVE mode (default: paper)")
    parser.add_argument("--scan", action="store_true", help="Scan markets and show opportunities")
    parser.add_argument("--btc", action="store_true", help="Scan BTC markets (daily/weekly/monthly/yearly)")
    parser.add_argument("--daily20", action="store_true", help="Scan for $20/day tight-timeframe opportunities")
    parser.add_argument("--status", action="store_true", help="Show engine status")
    parser.add_argument("--categories", nargs="+", default=None,
                        help="Filter by categories (Elections, Financials, etc.)")
    parser.add_argument("--loop", action="store_true", help="Run continuous trading loop")
    parser.add_argument("--interval", type=int, default=60, help="Loop interval in seconds")
    parser.add_argument("--iterations", type=int, default=100, help="Max loop iterations")
    args = parser.parse_args()

    engine = KalshiEngine(paper=not args.live)

    if args.btc:
        print("\n₿ Scanning Kalshi BTC markets...")
        btc = engine.btc_scanner.scan_btc()
        for tf in ["intraday", "weekly", "monthly", "yearly"]:
            mkts = btc.get(tf, [])
            label = "INTRADAY (<72h)" if tf == "intraday" else tf.upper()
            print(f"\n{'='*70}")
            print(f"  BTC {label} markets: {len(mkts)}")
            print(f"{'='*70}")
            for m in mkts[:8]:
                print(f"\n  {m['ticker']}")
                print(f"    {m['subtitle'] or m['title']}")
                print(f"    Bid={m['yes_bid']:.3f} Ask={m['yes_ask']:.3f} Spread={m['spread_cents']}¢")
                print(f"    Vol=${m['volume']:,.0f} OI=${m['oi']:,.0f} Closes={m['hours_left']:.0f}h")

        # Best daily BTC
        best_daily = engine.btc_scanner.best_daily_btc(btc)
        if best_daily:
            print(f"\n{'='*70}")
            print(f"  🎯 Best BTC DAILY opportunities ({len(best_daily)} markets)")
            print(f"{'='*70}")
            for m in best_daily[:5]:
                print(f"\n  {m['ticker']}")
                print(f"    {m['subtitle']}")
                print(f"    Mid={m['mid']:.3f} Spread={m['spread_cents']}¢ Vol=${m['volume']:,.0f}")

    if args.daily20:
        print("\n💰 Scanning for $20/day tight-timeframe opportunities...")
        result = engine.daily20_scanner.scan()

        print(f"\nScanned {result['total_scanned']} markets, found {result['total_qualified']} qualified")
        print(f"\n{'='*70}")
        print(f"  ✅ FEASIBLE (capital ≤ $500) — {len(result['feasible'])} opportunities")
        print(f"{'='*70}")
        for o in result["feasible"][:15]:
            print(f"\n  {o['ticker']}")
            print(f"    {o['title'][:60]}")
            if o.get('subtitle'):
                print(f"    {o['subtitle'][:45]}")
            print(f"    Strategy: {o['best_strategy']} | Capital: ${o['capital_needed']:.0f} | Risk: ${o['risk']:.0f}")
            print(f"    Bid={o['yes_bid']:.3f} Ask={o['yes_ask']:.3f} Spread={o['spread_cents']}¢ Vol=${o['volume']:,.0f}")
            print(f"    Closes: {o['hours_left']:.0f}h | Score: {o['score']:.1f}")

        print(f"\n{'='*70}")
        print(f"  ⚠️  MODERATE ($500–$2K capital) — {len(result['moderate'])} opportunities")
        print(f"{'='*70}")
        for o in result["moderate"][:8]:
            print(f"\n  {o['ticker']}")
            print(f"    {o['title'][:60]}")
            print(f"    Strategy: {o['best_strategy']} | Capital: ${o['capital_needed']:.0f} | Risk: ${o['risk']:.0f}")
            print(f"    Closes: {o['hours_left']:.0f}h | Score: {o['score']:.1f}")

    if args.scan or not args.loop:
        # Initialize and scan
        status = engine.init()
        print(f"\n{'='*60}")
        print(f"FDC Kalshi Engine — {status.get('mode', '?')} Mode")
        print(f"{'='*60}")
        print(f"API reachable: {status.get('api_reachable')}")
        print(f"Auth: {status.get('auth', {})}")
        print(f"Total markets: {status.get('total_markets', 0)}")

        # Scan for opportunities
        opportunities = engine.scan(categories=args.categories)
        print(f"\n📊 Found {len(opportunities)} opportunities:")
        for i, opp in enumerate(opportunities[:10]):
            print(f"\n  {i+1}. [{opp['ticker']}] {opp['title']}")
            print(f"     Mid: {opp['mid']:.2f} | Vol: ${opp['volume']:.0f} | Score: {opp['score']:.1f}")
            for sig in opp["signals"]:
                print(f"     → {sig['type']} {sig['direction']} @ {sig['price']:.2f} "
                      f"(RR: {sig['risk_reward']:.1f}x, EV: {sig['expected_value']:.2f})")

    if args.loop:
        engine.run_loop(
            interval_seconds=args.interval,
            max_iterations=args.iterations,
            categories=args.categories,
        )

    if args.status:
        print(f"\n{'='*40}")
        print("Engine Status")
        print(f"{'='*40}")
        s = engine.status()
        for k, v in s.items():
            print(f"  {k}: {v}")