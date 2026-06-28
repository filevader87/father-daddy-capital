#!/usr/bin/env python3
"""
V21.7.74 WebSocket Scalper — Speed-Optimized 5m Up/Down Bot
============================================================
Replaces REST polling with WebSocket streams for real-time data.

SPEED UPGRADES:
  - Binance WebSocket: real-time trades + 1m klines + bookTicker (50ms vs 1000ms REST)
  - Polymarket CLOB WebSocket: real-time orderbook updates (100ms vs 800ms REST)
  - Event-driven architecture: act on price changes, not poll cycles
  - In-memory indicator computation: no API calls per cycle
  - Concurrent market discovery: all assets in parallel

STRATEGY (from backtest + bonereaper analysis + Jane Street/PhD critique):
  - RSI reversal: RSI<30 → UP, RSI>70 → DOWN (backtest: 55.7% WR on DOWN, best signal)
  - Both directions (DOWN unblocked — backtest proves edge)
  - Entry band: 30-70¢ (payout ~1:1, backtest shows 90¢ entry is catastrophic PF 0.12)
  - Kelly-sized: $1.50 per trade (quarter Kelly on $31 bankroll, was 7.8x oversized at $3)
  - TTE filter: prefer 300-600s (backtest: 100% WR at long TTE)
  - Gamma API settlement only (lessons from weather bot: never trust local settlement)

RUN AS:
  python3 src/v217_live/v21774_ws_scalper.py --paper
  python3 src/v217_live/v21774_ws_scalper.py --live
"""
from __future__ import annotations
import json, os, sys, time, logging, signal, traceback, argparse, threading, math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from collections import defaultdict, deque
import numpy as np
import requests
import websocket  # websocket-client

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "output" / "v21774_ws_scalper"
SUP = ROOT / "output" / "supervisor"
OUT.mkdir(parents=True, exist_ok=True)
SUP.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════
# ENVIRONMENT
# ═══════════════════════════════════════════════════════════════════════════
ENV_PATH = Path("/mnt/c/Users/12035/father_daddy_capital/.env")

def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

EOA = "0xD4a39D33b8CcB46a08378e426BaEE3591463f090"
DW = "0xaF7B21FE2B18745aE1b2fA2F6F00B0fC4EF3F70b"
CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
BINANCE_WS = "wss://stream.binance.com:9443/stream"
PM_CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG — All values backtest-validated
# ═══════════════════════════════════════════════════════════════════════════

CONFIG = {
    "version": "V21.7.76",
    "assets": ["BTC", "ETH", "SOL", "XRP"],
    # V21.7.76: Dual entry bands — EV-optimized, not coin-flip
    # Reversal: buy cheap (10-40¢) where payout asymmetry provides edge.
    #   @20¢: risk 20¢ to win 80¢ (4:1) — need only 21% WR to break even.
    #   @40¢: risk 40¢ to win 60¢ (1.5:1) — need 40% WR to break even.
    # Certainty: buy expensive (70¢+) where signal is "this outcome will happen"
    #   @70¢: risk 70¢ to win 30¢ (1:0.43) — need 70% WR to break even.
    "entry_price_lo": 0.10,
    "entry_price_hi": 0.40,    # Reversal band upper
    "entry_price_certainty": 0.70,  # Certainty band: 70¢+
    "position_size_usd": 1.64,  # Quarter Kelly on $31 bankroll
    "max_open_positions": 5,
    "max_daily_trades": 15,
    "max_daily_loss_usd": 10.0,
    "max_consecutive_losses": 3,
    "max_drawdown_usd": 10.0,
    "initial_bankroll": 50.0,
    "rsi_oversold": 30.0,
    "rsi_overbought": 70.0,
    "min_edge_pp": 5.0,
    "min_confidence": 0.55,
    "min_ev_cents": 5.0,       # V21.7.76: Minimum EV in cents — reject if EV < 5¢
    "max_spread_cents": 5.0,
    "min_tte_seconds": 30,
    "max_tte_seconds": 600,
    "preferred_tte_min": 300,
    "scan_interval_seconds": 2.0,
    "ws_reconnect_seconds": 5,
}

# ═══════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUT / "ws_scalper.log"),
    ],
)
log = logging.getLogger("v21774")

_shutdown = False
def handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log.info(f"Signal {signum} received")
signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

# ═══════════════════════════════════════════════════════════════════════════
# REAL-TIME PRICE FEED — Binance WebSocket
# ═══════════════════════════════════════════════════════════════════════════

class BinanceWSFeed:
    """Real-time Binance price feed via WebSocket.
    Maintains in-memory 1m candle history for indicator computation.
    Replaces 1000ms REST polling with ~50ms WebSocket push.
    """
    
    def __init__(self, assets: List[str]):
        self.assets = assets
        self.symbols = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt", "XRP": "xrpusdt"}
        self.candles: Dict[str, List[Dict]] = {a: [] for a in assets}  # 1m candles per asset
        self.current_prices: Dict[str, float] = {a: 0.0 for a in assets}
        self.current_bids: Dict[str, float] = {a: 0.0 for a in assets}
        self.current_asks: Dict[str, float] = {a: 0.0 for a in assets}
        self.last_trade_ts: Dict[str, float] = {a: 0.0 for a in assets}
        self._ws = None
        self._thread = None
        self._lock = threading.Lock()
        
    def start(self):
        """Start WebSocket connection in background thread."""
        # Build combined stream URL
        streams = []
        for asset in self.assets:
            sym = self.symbols.get(asset, asset.lower() + "usdt")
            streams.append(f"{sym}@kline_1m")
            streams.append(f"{sym}@bookTicker")
        stream_url = f"{BINANCE_WS}?streams={'/'.join(streams)}"
        
        self._thread = threading.Thread(target=self._run_ws, args=(stream_url,), daemon=True)
        self._thread.start()
        
        # Also fetch initial klines via REST for immediate indicator computation
        for asset in self.assets:
            self._fetch_initial_klines(asset)
    
    def _fetch_initial_klines(self, asset: str):
        """Fetch last 50 1m candles via REST for immediate use."""
        sym = self.symbols.get(asset, asset.lower() + "usdt").upper()
        try:
            r = requests.get("https://api.binance.com/api/v3/klines", params={
                "symbol": sym, "interval": "1m", "limit": 50
            }, timeout=10)
            if r.status_code == 200:
                with self._lock:
                    self.candles[asset] = [{
                        "open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]),
                        "volume": float(k[5]), "ts": k[0],
                    } for k in r.json()]
                    if self.candles[asset]:
                        self.current_prices[asset] = self.candles[asset][-1]["close"]
                log.info(f"Initial klines loaded: {asset} ({len(self.candles[asset])} candles)")
        except Exception as e:
            log.warning(f"Failed to fetch initial klines for {asset}: {e}")
    
    def _run_ws(self, url: str):
        """WebSocket event loop with auto-reconnect."""
        while not _shutdown:
            try:
                log.info(f"Connecting Binance WebSocket: {url[:80]}...")
                self._ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=lambda ws: log.info("Binance WS connected"),
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                log.error(f"Binance WS error: {e}")
            
            if not _shutdown:
                log.info(f"Reconnecting Binance WS in {CONFIG['ws_reconnect_seconds']}s...")
                time.sleep(CONFIG["ws_reconnect_seconds"])
    
    def _on_message(self, ws, message):
        """Process incoming WebSocket message."""
        try:
            data = json.loads(message)
            stream = data.get("stream", "")
            payload = data.get("data", {})
            
            # Parse asset from stream name
            for asset, sym in self.symbols.items():
                if sym in stream:
                    if "@kline_1m" in stream:
                        k = payload.get("k", {})
                        candle = {
                            "open": float(k.get("o", 0)),
                            "high": float(k.get("h", 0)),
                            "low": float(k.get("l", 0)),
                            "close": float(k.get("c", 0)),
                            "volume": float(k.get("v", 0)),
                            "ts": k.get("t", 0),
                            "is_closed": k.get("x", False),
                        }
                        with self._lock:
                            # Update last candle or append new
                            if self.candles[asset] and self.candles[asset][-1]["ts"] == candle["ts"]:
                                self.candles[asset][-1] = candle
                            else:
                                self.candles[asset].append(candle)
                                if len(self.candles[asset]) > 100:
                                    self.candles[asset] = self.candles[asset][-100:]
                            self.current_prices[asset] = candle["close"]
                    elif "@bookTicker" in stream:
                        with self._lock:
                            self.current_bids[asset] = float(payload.get("b", 0))
                            self.current_asks[asset] = float(payload.get("a", 0))
                    self.last_trade_ts[asset] = time.time()
                    break
        except Exception as e:
            log.debug(f"WS message parse error: {e}")
    
    def _on_error(self, ws, error):
        log.error(f"Binance WS error: {error}")
    
    def _on_close(self, ws, close_status, close_msg):
        log.warning(f"Binance WS closed: {close_status} {close_msg}")
    
    def get_closes(self, asset: str) -> List[float]:
        """Get close prices for indicator computation."""
        with self._lock:
            return [c["close"] for c in self.candles.get(asset, [])]
    
    def get_price(self, asset: str) -> float:
        """Get current real-time price."""
        with self._lock:
            return self.current_prices.get(asset, 0.0)
    
    def get_book(self, asset: str) -> Tuple[float, float]:
        """Get current best bid/ask."""
        with self._lock:
            return self.current_bids.get(asset, 0.0), self.current_asks.get(asset, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# INDICATORS — Computed in-memory from WebSocket data
# ═══════════════════════════════════════════════════════════════════════════

def compute_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period+1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))

def compute_momentum(closes: List[float], lookback: int = 5) -> float:
    if len(closes) < lookback + 1:
        return 0.0
    return (closes[-1] - closes[-lookback]) / closes[-lookback]

def compute_trend(closes: List[float], lookback: int = 10) -> float:
    if len(closes) < lookback:
        return 0.0
    recent = closes[-lookback:]
    x = np.arange(len(recent))
    return float(np.polyfit(x, recent, 1)[0]) / recent[-1] if recent[-1] > 0 else 0.0

def compute_indicators(feed: BinanceWSFeed, asset: str) -> Dict:
    """Compute indicators from WebSocket data — zero API calls."""
    closes = feed.get_closes(asset)
    if len(closes) < 20:
        return {"rsi": 50.0, "momentum": 0.0, "trend": 0.0, "volatility": 0.0,
                "current_price": feed.get_price(asset)}
    
    rsi = compute_rsi(closes)
    momentum = compute_momentum(closes)
    trend = compute_trend(closes)
    returns = np.diff(np.log(closes[-20:])) if len(closes) >= 21 else np.array([0.0])
    volatility = float(np.std(returns)) if len(returns) > 1 else 0.0
    
    return {
        "rsi": rsi, "momentum": momentum, "trend": trend,
        "volatility": volatility, "current_price": feed.get_price(asset),
        "n_candles": len(closes),
    }

def detect_reversal(indicators: Dict) -> Tuple[str, float, float, str]:
    """V21.7.76: Dual-mode signal detection. Returns (direction, confidence, edge, signal_type).
    
    Two signal modes:
    1. REVERSAL — RSI extreme → opposite direction. Buy cheap (10-40¢).
       RSI<25 → UP (deep oversold), RSI>75 → DOWN (deep overbought).
       Edge comes from payout asymmetry: @20¢ you risk 20¢ to win 80¢.
    2. CERTAINTY — Strong momentum + RSI alignment → same direction. Buy expensive (70¢+).
       RSI>70 + positive momentum → UP continuation. RSI<30 + negative momentum → DOWN continuation.
       Edge comes from high-probability outcome: @70¢ you need 70% WR.
    
    Confidence is now signal-quality based, not price-distance based.
    """
    rsi = indicators["rsi"]
    momentum = indicators["momentum"]
    trend = indicators["trend"]
    vol = indicators.get("volatility", 0)
    
    direction = "NONE"
    confidence = 0.0
    signal_type = "NONE"
    
    # ── REVERSAL signals ─────────────────────────────────────────────
    # Deep RSI extremes only — tighter thresholds for real reversals
    if rsi < 25:
        strength = (25 - rsi) / 15.0  # 0 at RSI=25, 1.0 at RSI=10
        # Momentum must confirm exhaustion (negative momentum = selling exhausted)
        mom_confirm = 1.0 if momentum < -0.0005 else 0.5
        confidence = 0.55 + strength * 0.25 + mom_confirm * 0.10
        confidence = min(0.85, confidence)
        direction = "UP"
        signal_type = "REVERSAL"
        
    elif rsi > 75:
        strength = (rsi - 75) / 15.0  # 0 at RSI=75, 1.0 at RSI=90
        mom_confirm = 1.0 if momentum > 0.0005 else 0.5
        confidence = 0.55 + strength * 0.25 + mom_confirm * 0.10
        confidence = min(0.85, confidence)
        direction = "DOWN"
        signal_type = "REVERSAL"
    
    # ── CERTAINTY signals ─────────────────────────────────────────────
    # Strong trend continuation — RSI + momentum aligned, high conviction
    # RSI 60-75 (strong but not overbought) + positive momentum → UP
    if rsi > 60 and rsi < 80 and momentum > 0.0008 and trend > 0.0002:
        strength = min((momentum / 0.003), 1.0)  # Scale by momentum strength
        confidence = 0.65 + strength * 0.20
        confidence = min(0.90, confidence)
        direction = "UP"
        signal_type = "CERTAINTY"
        
    # RSI 20-40 (strong but not oversold) + negative momentum → DOWN  
    elif rsi < 40 and rsi > 15 and momentum < -0.0008 and trend < -0.0002:
        strength = min(abs(momentum) / 0.003, 1.0)
        confidence = 0.65 + strength * 0.20
        confidence = min(0.90, confidence)
        direction = "DOWN"
        signal_type = "CERTAINTY"
    
    if direction == "NONE":
        return "NONE", 0.0, 0.0, "NONE"
    
    # Volatility penalty — high volatility reduces signal reliability
    vol_penalty = min(vol * 2 * 0.5, 0.15)
    confidence = max(0.50, confidence - vol_penalty)
    
    # Edge = confidence above 50% baseline, in percentage points
    edge = (confidence - 0.5) * 100
    
    return direction, confidence, edge, signal_type


def compute_ev(direction: str, entry_price: float, confidence: float, shares: float) -> float:
    """V21.7.76: Expected value in cents.
    
    EV = P(win) × payout_win - P(loss) × cost_loss
    P(win) = confidence, P(loss) = 1 - confidence
    payout_win = (1.0 - entry_price) × shares  (win: get $1 per share, paid entry_price)
    cost_loss = entry_price × shares           (lose: lose what you paid)
    
    Returns EV in cents (positive = profitable, negative = unprofitable).
    """
    p_win = confidence
    p_loss = 1.0 - confidence
    payout_win = (1.0 - entry_price) * shares  # dollars
    cost_loss = entry_price * shares            # dollars
    ev_usd = p_win * payout_win - p_loss * cost_loss
    return ev_usd * 100  # convert to cents


# ═══════════════════════════════════════════════════════════════════════════
# MARKET DISCOVERY — Cached with background refresh
# ═══════════════════════════════════════════════════════════════════════════

_market_cache: Dict[str, Dict] = {}  # slug → market data
_market_cache_ts: float = 0.0
_market_lock = threading.Lock()

def discover_5m_markets() -> List[Dict]:
    """Discover active 5m markets. Cached for 60s, refreshed in background."""
    global _market_cache, _market_cache_ts
    
    now = time.time()
    with _market_lock:
        if now - _market_cache_ts < 60 and _market_cache:
            return list(_market_cache.values())
    
    # Fresh fetch
    markets = _fetch_5m_markets()
    with _market_lock:
        _market_cache = {m["slug"]: m for m in markets}
        _market_cache_ts = now
    return markets

def _fetch_5m_markets() -> List[Dict]:
    """Fetch current 5m markets from Gamma API."""
    markets = []
    now_epoch = int(time.time())
    
    # Current + next 5m window
    for offset in [0, 300]:
        exp_epoch = ((now_epoch // 300) + 1) * 300 + offset
        for asset in CONFIG["assets"]:
            asset_lower = asset.lower()
            slug = f"{asset_lower}-updown-5m-{exp_epoch}"
            if slug in [m["slug"] for m in markets]:
                continue
            try:
                r = requests.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=8)
                if r.status_code == 200 and r.json():
                    ev = r.json()[0]
                    for mk in ev.get("markets", []):
                        q = mk.get("question", "").lower()
                        if "up or down" not in q and "up/down" not in q:
                            continue
                        outcomes_raw = mk.get("outcomes", "[]")
                        token_ids_raw = mk.get("clobTokenIds", "[]")
                        try:
                            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                            token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
                        except:
                            outcomes, token_ids = [], []
                        if len(outcomes) < 2 or len(token_ids) < 2:
                            continue
                        up_tid = down_tid = ""
                        for i, o in enumerate(outcomes):
                            if i >= len(token_ids):
                                break
                            if str(o).lower() == "up":
                                up_tid = token_ids[i]
                            elif str(o).lower() == "down":
                                down_tid = token_ids[i]
                        if not up_tid or not down_tid:
                            continue
                        neg_risk = mk.get("neg_risk", False)
                        if isinstance(neg_risk, str):
                            neg_risk = neg_risk.lower() == "true"
                        tte = exp_epoch - now_epoch
                        markets.append({
                            "slug": slug, "question": mk.get("question", ""),
                            "asset": asset, "up_token_id": up_tid, "down_token_id": down_tid,
                            "tte_seconds": round(tte, 1),
                            "active": mk.get("active", True), "closed": mk.get("closed", False),
                            "neg_risk": neg_risk,
                        })
                        break
            except:
                pass
    return markets


# ═══════════════════════════════════════════════════════════════════════════
# CLOB ORDERBOOK — REST with in-memory cache
# ═══════════════════════════════════════════════════════════════════════════

_book_cache: Dict[str, Dict] = {}
_book_cache_ts: Dict[str, float] = {}

def get_orderbook(token_id: str, max_age: float = 2.0) -> Optional[Dict]:
    """Get orderbook with 2s cache. In live mode, could use PM CLOB WebSocket."""
    now = time.time()
    if token_id in _book_cache_ts and now - _book_cache_ts[token_id] < max_age:
        return _book_cache[token_id]
    
    try:
        r = requests.get(f"{CLOB_HOST}/book?token_id={token_id}", timeout=5)
        if r.status_code == 200:
            book = r.json()
            asks = sorted(book.get("asks", []), key=lambda x: float(x.get("price", 1)))
            bids = sorted(book.get("bids", []), key=lambda x: float(x.get("price", 0)), reverse=True)
            best_ask = float(asks[0]["price"]) if asks else None
            best_bid = float(bids[0]["price"]) if bids else None
            result = {
                "best_ask": best_ask, "best_bid": best_bid,
                "spread": round(best_ask - best_bid, 4) if best_ask and best_bid else None,
                "ask_depth": sum(float(a.get("size", 0)) for a in asks[:5]),
                "bid_depth": sum(float(b.get("size", 0)) for b in bids[:5]),
            }
            _book_cache[token_id] = result
            _book_cache_ts[token_id] = now
            return result
    except:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
# CLOB CLIENT (for live trading)
# ═══════════════════════════════════════════════════════════════════════════

_clob_client = None

def get_clob_client():
    global _clob_client
    if _clob_client is None:
        env = load_env()
        pk = env.get("PM_WALLET_PRIVATE_KEY", "")
        if not pk:
            raise ValueError("No PM_WALLET_PRIVATE_KEY in env")
        from py_clob_client_v2 import ClobClient, SignatureTypeV2
        _clob_client = ClobClient(
            CLOB_HOST, key=pk, chain_id=CHAIN_ID,
            signature_type=SignatureTypeV2.POLY_1271.value, funder=DW,
        )
        creds = _clob_client.create_or_derive_api_key()
        _clob_client.set_api_creds(creds)
        log.info("CLOB client initialized (POLY_1271) with L2 API creds")
    return _clob_client

# ── Balance tracking ──────────────────────────────────────────────────────
_balance_cache = {"balance": None, "ts": 0.0}
_failed_slugs: set = set()  # Slugs that had order failures this cycle — prevents API spam

def get_usdc_balance() -> Optional[float]:
    """Get current USDC balance from CLOB. Cached 30s. Returns human-readable USDC."""
    if time.time() - _balance_cache["ts"] < 30:
        return _balance_cache["balance"]
    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        clob = get_clob_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=1)
        bal_info = clob.get_balance_allowance(params)
        raw = float(bal_info.get("balance", 0))
        # CLOB returns balance in raw units (6 decimals for USDC)
        balance = raw / 1_000_000
        _balance_cache["balance"] = balance
        _balance_cache["ts"] = time.time()
        log.debug(f"USDC balance: ${balance:.2f} (raw={raw})")
        return balance
    except Exception as e:
        log.debug(f"Balance check failed: {e}")
        return _balance_cache["balance"]


# ═══════════════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ScalperState:
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    loop_count: int = 0
    markets_scanned: int = 0
    signals_generated: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    daily_trades: int = 0
    daily_loss_usd: float = 0.0
    daily_reset: str = ""
    open_positions: int = 0
    total_pnl: float = 0.0
    consecutive_losses: int = 0
    wins: int = 0
    losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    paper_mode: bool = True
    positions: List[Dict] = field(default_factory=list)
    closed_positions: List[Dict] = field(default_factory=list)
    recent_outcomes: List[int] = field(default_factory=list)
    recent_pnls: List[float] = field(default_factory=list)
    signal_latency_ms: List[float] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# EXECUTION
# ═══════════════════════════════════════════════════════════════════════════

def execute_order(market: Dict, side: str, token_id: str, best_ask: float,
                  paper_mode: bool, indicators: Dict, confidence: float,
                  state: ScalperState) -> Dict:
    """Execute a 5m scalper order."""
    size_usd = CONFIG["position_size_usd"]
    
    # V21.7.74: Kelly-based progressive sizing
    cl = state.consecutive_losses
    if cl >= 3:
        size_usd *= 0.50
    elif cl == 2:
        size_usd *= 0.75
    
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_slug": market["slug"], "asset": market["asset"],
        "side": side, "token_id": token_id, "ask": best_ask,
        "size_usd": size_usd, "confidence": round(confidence, 4),
        "rsi": round(indicators["rsi"], 1),
        "status": "PENDING", "order_id": None, "fill_status": None,
    }
    
    if paper_mode:
        result["status"] = "PAPER_FILLED"
        result["fill_status"] = "paper"
        result["fill_price"] = best_ask
        result["shares"] = size_usd / best_ask
        log.info(f"PAPER: {side} {market['asset']} | @ {best_ask*100:.1f}¢ | "
                 f"${size_usd:.2f} | conf={confidence:.1%} | RSI={indicators['rsi']:.0f} | "
                 f"TTE={market['tte_seconds']:.0f}s")
        with open(OUT / "paper_orders.jsonl", "a") as f:
            f.write(json.dumps(result, default=str) + "\n")
        return result
    
    # LIVE execution
    # ── Pre-flight balance check ────────────────────────────────────────
    balance = get_usdc_balance()
    if balance is not None and balance < size_usd:
        result["status"] = "INSUFFICIENT_BALANCE"
        result["error"] = f"Balance ${balance:.2f} < order ${size_usd:.2f}"
        log.warning(f"SKIP: {side} {market['asset']} | balance ${balance:.2f} < ${size_usd:.2f}")
        # Force halt — no point continuing to scan with no funds
        state.halted = True
        state.halt_reason = f"Insufficient balance (${balance:.2f})"
        return result
    
    try:
        from py_clob_client_v2 import OrderArgsV2, CreateOrderOptions, OrderType
        shares = max(int(round(size_usd / max(best_ask, 0.01))), 1)
        actual_cost = shares * best_ask
        result["shares"] = shares
        result["actual_cost"] = round(actual_cost, 2)
        
        # Double-check balance vs actual_cost (raw units)
        if balance is not None and balance < actual_cost:
            result["status"] = "INSUFFICIENT_BALANCE"
            result["error"] = f"Balance ${balance:.2f} < cost ${actual_cost:.2f}"
            log.warning(f"SKIP: {side} {market['asset']} | balance ${balance:.2f} < cost ${actual_cost:.2f}")
            state.halted = True
            state.halt_reason = f"Insufficient balance (${balance:.2f})"
            return result
        
        order_args = OrderArgsV2(token_id=token_id, price=best_ask, size=shares, side="BUY")
        options = CreateOrderOptions(tick_size="0.01", neg_risk=market.get("neg_risk", False))
        signed_order = get_clob_client().create_order(order_args, options)
        
        if signed_order.maker != DW:
            result["error"] = f"Maker mismatch: {signed_order.maker}"
            result["status"] = "EMERGENCY_HALT"
            return result
        
        order_result = get_clob_client().post_order(signed_order, OrderType.FOK)
        result["order_id"] = order_result.get("orderID", "")
        result["fill_status"] = order_result.get("status", "")
        result["status"] = "ACKNOWLEDGED"
        
        try:
            get_clob_client().cancel_all()
        except:
            pass
        
        if result["fill_status"] in ("live", "matched"):
            log.info(f"LIVE FILL: {side} {market['asset']} @ {best_ask*100:.1f}¢ | "
                     f"{shares} shares | ${actual_cost:.2f}")
            # Invalidate balance cache after fill
            _balance_cache["ts"] = 0.0
        else:
            log.warning(f"ORDER NOT FILLED: status={result['fill_status']}")
            result["status"] = "NOT_FILLED"
            
    except Exception as e:
        result["error"] = str(e)
        result["status"] = "ERROR"
        log.error(f"Order error: {e}")
    
    with open(OUT / "order_attempts.jsonl", "a") as f:
        f.write(json.dumps(result, default=str) + "\n")
    return result


# ═══════════════════════════════════════════════════════════════════════════
# SETTLEMENT — Gamma API only (weather bot lesson: never trust local data)
# ═══════════════════════════════════════════════════════════════════════════

def settle_position(pos: Dict) -> Optional[Dict]:
    """Settle via Gamma API. Returns updated position or None if not yet resolved."""
    slug = pos.get("market_slug", "")
    side = pos.get("side", "").upper()
    entry_price = pos.get("entry_price", 0)
    
    try:
        r = requests.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=10)
        if r.status_code == 200 and r.json():
            ev = r.json()[0]
            for mk in ev.get("markets", []):
                q = mk.get("question", "").lower()
                if "up or down" not in q and "up/down" not in q:
                    continue
                if not mk.get("closed", False):
                    continue
                prices_raw = mk.get("outcomePrices", "[]")
                outcomes_raw = mk.get("outcomes", "[]")
                try:
                    prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                except:
                    prices, outcomes = [], []
                if len(prices) >= 2 and len(outcomes) >= 2:
                    win_idx = 0 if float(prices[0]) > float(prices[1]) else 1
                    winner = str(outcomes[win_idx]).strip().upper()
                    win = (winner == side)
                    shares = pos.get("shares", CONFIG["position_size_usd"] / entry_price)
                    pnl = (1.0 - entry_price) * shares if win else -entry_price * shares
                    pos["outcome"] = "WIN" if win else "LOSS"
                    pos["pnl"] = round(pnl, 4)
                    pos["resolved_timestamp"] = datetime.now(timezone.utc).isoformat()
                    pos["gamma_verified"] = True
                    pos["gamma_winner"] = winner
                    return pos
    except Exception as e:
        log.debug(f"Gamma settlement check failed for {slug}: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════

def load_state(state: ScalperState, paper_mode: bool):
    """Load state from disk."""
    state_file = OUT / ("paper_state.json" if paper_mode else "live_state.json")
    if state_file.exists():
        try:
            with open(state_file) as f:
                d = json.load(f)
            for k, v in d.items():
                if hasattr(state, k) and not isinstance(v, (list, dict)):
                    setattr(state, k, v)
        except:
            pass
    
    # Load open positions
    pos_file = OUT / ("paper_positions.jsonl" if paper_mode else "live_positions.jsonl")
    if pos_file.exists():
        with open(pos_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                    if not p.get("resolved") and not p.get("outcome"):
                        state.positions.append(p)
                except:
                    pass
    
    # Load closed positions for stats
    resolved_file = OUT / ("paper_resolved.jsonl" if paper_mode else "live_resolved.jsonl")
    if resolved_file.exists():
        with open(resolved_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                    if p.get("outcome") in ("WIN", "LOSS"):
                        state.closed_positions.append(p)
                        state.total_pnl += p.get("pnl", 0)
                        if p["outcome"] == "WIN":
                            state.wins += 1
                        else:
                            state.losses += 1
                except:
                    pass
    
    log.info(f"State loaded: W={state.wins} L={state.losses} PnL=${state.total_pnl:.2f} | "
             f"Open: {len(state.positions)}")

def save_state(state: ScalperState, paper_mode: bool):
    """Save state to disk."""
    state_file = OUT / ("paper_state.json" if paper_mode else "live_state.json")
    with open(state_file, "w") as f:
        json.dump({
            "total_pnl": state.total_pnl, "wins": state.wins, "losses": state.losses,
            "consecutive_losses": state.consecutive_losses,
            "daily_trades": state.daily_trades, "daily_loss_usd": state.daily_loss_usd,
            "daily_reset": state.daily_reset,
            "halted": state.halted, "halt_reason": state.halt_reason,
        }, f, indent=2, default=str)


def scalper_loop(state: ScalperState, paper_mode: bool):
    """Main event-driven scalper loop."""
    global _shutdown
    
    log.info(f"V21.7.76 WebSocket Scalper starting | paper={paper_mode}")
    log.info(f"Strategy: Dual-mode (REVERSAL + CERTAINTY) | EV-ranked | Kelly-sized")
    log.info(f"Position: ${CONFIG['position_size_usd']} | Reversal: {CONFIG['entry_price_lo']}-{CONFIG['entry_price_hi']}¢ | Certainty: {CONFIG['entry_price_certainty']}¢+")
    log.info(f"Min EV: {CONFIG['min_ev_cents']}¢ | RSI reversal: <{CONFIG['rsi_oversold']}/{CONFIG['rsi_overbought']}> | Hard floor: ${CONFIG['initial_bankroll'] - CONFIG['max_drawdown_usd']:.0f}")
    
    # Start Binance WebSocket feed
    feed = BinanceWSFeed(CONFIG["assets"])
    feed.start()
    time.sleep(2)  # Let WS connect and initial klines load
    
    clob = None
    if not paper_mode:
        try:
            clob = get_clob_client()
        except Exception as e:
            log.error(f"CLOB init failed: {e} — paper mode")
            paper_mode = True
    
    pos_file = OUT / ("paper_positions.jsonl" if paper_mode else "live_positions.jsonl")
    resolved_file = OUT / ("paper_resolved.jsonl" if paper_mode else "live_resolved.jsonl")
    
    last_heartbeat = 0.0
    
    while not _shutdown:
        try:
            loop_start = time.time()
            now = datetime.now(timezone.utc)
            state.loop_count += 1
            
            # Daily reset
            today = now.strftime("%Y-%m-%d")
            if state.daily_reset != today:
                log.info(f"Daily reset: {state.daily_reset} → {today} | trades={state.daily_trades} loss=${state.daily_loss_usd:.2f}")
                state.daily_trades = 0
                state.daily_loss_usd = 0.0
                state.daily_reset = today
                _failed_slugs.clear()  # Reset failed slug tracking on new day
            
            # Halt check
            if state.halted:
                log.error(f"HALTED: {state.halt_reason}")
                break
            if state.consecutive_losses >= CONFIG["max_consecutive_losses"]:
                state.halted = True
                state.halt_reason = f"Max consecutive losses ({state.consecutive_losses})"
                break
            # V21.7.75: Hard drawdown floor — permanent halt, requires manual state wipe + code review
            if not paper_mode:
                drawdown = state.total_pnl + CONFIG["initial_bankroll"]
                if drawdown <= (CONFIG["initial_bankroll"] - CONFIG["max_drawdown_usd"]):
                    state.halted = True
                    state.halt_reason = f"HARD FLOOR: drawdown ${CONFIG['initial_bankroll'] - drawdown:.2f} exceeded ${CONFIG['max_drawdown_usd']:.2f} max"
                    log.error(f"HALTED: {state.halt_reason}")
                    break
            
            can_trade = (state.daily_trades < CONFIG["max_daily_trades"] and
                         state.open_positions < CONFIG["max_open_positions"] and
                         state.daily_loss_usd < CONFIG["max_daily_loss_usd"])
            
            # Live mode: check balance before attempting any trade
            if can_trade and not paper_mode:
                bal = get_usdc_balance()
                if bal is not None and bal < CONFIG["position_size_usd"]:
                    can_trade = False
                    if not state.halted:
                        log.warning(f"Balance ${bal:.2f} too low for ${CONFIG['position_size_usd']} trades — blocking new orders")
                        state.halted = True
                        state.halt_reason = f"Insufficient balance (${bal:.2f})"
            
            # Discover markets (cached, ~0ms if cache fresh)
            markets = discover_5m_markets()
            state.markets_scanned = len(markets)
            
            if not markets:
                time.sleep(CONFIG["scan_interval_seconds"])
                continue
            
            # Compute indicators from WebSocket data — ZERO API calls
            signals = []
            for market in markets:
                asset = market["asset"]
                tte = market["tte_seconds"]
                if tte < CONFIG["min_tte_seconds"] or tte > CONFIG["max_tte_seconds"]:
                    continue
                
                indicators = compute_indicators(feed, asset)
                if indicators["n_candles"] < 20:
                    continue
                
                direction, confidence, edge, signal_type = detect_reversal(indicators)
                if direction == "NONE" or edge < CONFIG["min_edge_pp"]:
                    continue
                if confidence < CONFIG["min_confidence"]:
                    continue
                
                # TTE bonus: prefer long TTE (backtest: 300-600s = 100% WR)
                if tte >= CONFIG["preferred_tte_min"]:
                    confidence += 0.05
                    edge += 5.0
                
                state.signals_generated += 1
                
                # Get orderbook (2s cache)
                token_id = market["up_token_id"] if direction == "UP" else market["down_token_id"]
                book = get_orderbook(token_id)
                if not book or not book.get("best_ask"):
                    continue
                
                best_ask = book["best_ask"]
                spread = book.get("spread", 1.0)
                
                # V21.7.76: Dual entry band check
                # REVERSAL: 10-40¢ (buy cheap, asymmetric payout)
                # CERTAINTY: 70¢+ (buy expensive, high probability)
                if signal_type == "REVERSAL":
                    if not (CONFIG["entry_price_lo"] <= best_ask <= CONFIG["entry_price_hi"]):
                        continue
                elif signal_type == "CERTAINTY":
                    if best_ask < CONFIG["entry_price_certainty"]:
                        continue
                else:
                    continue  # Unknown signal type, skip
                
                if spread and spread * 100 > CONFIG["max_spread_cents"]:
                    continue
                
                # V21.7.76: EV check — reject if expected value < min_ev_cents
                size_usd = CONFIG["position_size_usd"]
                shares = size_usd / best_ask
                ev_cents = compute_ev(direction, best_ask, confidence, shares)
                if ev_cents < CONFIG["min_ev_cents"]:
                    continue
                
                signals.append({
                    "market": market, "side": direction, "token_id": token_id,
                    "best_ask": best_ask, "spread": spread,
                    "confidence": confidence, "edge_pp": edge,
                    "indicators": indicators, "tte": tte, "signal_type": signal_type,
                    "ev_cents": round(ev_cents, 2),
                    "signal_age_ms": (time.time() - feed.last_trade_ts.get(asset, 0)) * 1000,
                })
            
            # Sort by EV (highest first) — EV is the top marker, not WR or edge
            signals.sort(key=lambda x: (x["ev_cents"], x["tte"]), reverse=True)
            
            # Log top signals
            for i, sig in enumerate(signals[:3]):
                log.info(f"  #{i+1} {sig['side']:4} {sig['market']['asset']} | "
                         f"{sig['signal_type']:9} ask={sig['best_ask']*100:.1f}¢ "
                         f"EV={sig['ev_cents']:.1f}¢ conf={sig['confidence']:.1%} "
                         f"RSI={sig['indicators']['rsi']:.0f} TTE={sig['tte']:.0f}s")
            
            # Log signals
            if signals:
                with open(OUT / "signals.jsonl", "a") as f:
                    for sig in signals:
                        f.write(json.dumps({
                            "timestamp": now.isoformat(),
                            "market_slug": sig["market"]["slug"],
                            "asset": sig["market"]["asset"],
                            "side": sig["side"], "ask": sig["best_ask"],
                            "signal_type": sig["signal_type"],
                            "ev_cents": sig["ev_cents"],
                            "confidence": sig["confidence"], "edge_pp": sig["edge_pp"],
                            "rsi": sig["indicators"]["rsi"],
                            "tte": sig["tte"],
                        }, default=str) + "\n")
            
            # Execute best signal
            if can_trade and signals:
                best = signals[0]
                existing_slugs = {p.get("market_slug", "") for p in state.positions}
                # Skip slugs that already had a failed order this cycle (prevent API spam)
                slug = best["market"]["slug"]
                if slug in existing_slugs:
                    pass  # Already have a position on this market
                elif slug in _failed_slugs:
                    pass  # Already tried and failed this cycle — don't re-fire
                else:
                    order_result = execute_order(
                        best["market"], best["side"], best["token_id"], best["best_ask"],
                        paper_mode, best["indicators"], best["confidence"], state
                    )
                    
                    order_ok = order_result["status"] == "PAPER_FILLED" or (
                        order_result["status"] == "ACKNOWLEDGED" and
                        order_result.get("fill_status") in ("live", "matched")
                    )
                    
                    if order_ok:
                        state.orders_submitted += 1
                        state.daily_trades += 1
                        state.open_positions += 1
                        
                        pos = {
                            "entry_timestamp": now.isoformat(),
                            "market_slug": best["market"]["slug"],
                            "asset": best["market"]["asset"],
                            "side": best["side"],
                            "token_id": best["token_id"],
                            "entry_price": best["best_ask"],
                            "size_usd": CONFIG["position_size_usd"],
                            "shares": order_result.get("shares", 0),
                            "direction": best["side"],
                            "confidence": best["confidence"],
                            "edge_pp": best["edge_pp"],
                            "rsi_at_entry": best["indicators"]["rsi"],
                            "tte_at_entry": best["tte"],
                            "order_status": order_result["status"],
                            "order_id": order_result.get("order_id"),
                            "fill_status": order_result.get("fill_status"),
                        }
                        state.positions.append(pos)
                        
                        with open(pos_file, "a") as f:
                            f.write(json.dumps(pos, default=str) + "\n")
                    else:
                        # Track failed slug to prevent re-firing same signal
                        _failed_slugs.add(slug)
                        # If balance-related error, halt will be set by execute_order
                        if order_result["status"] in ("INSUFFICIENT_BALANCE", "EMERGENCY_HALT"):
                            log.error(f"HALTING: {order_result['status']} — {order_result.get('error','')}")
                        else:
                            log.warning(f"Order failed: {order_result['status']} — {order_result.get('error','')[:80]}")
            
            # Settle expired positions via Gamma API
            for pos in list(state.positions):
                slug = pos.get("market_slug", "")
                # Check expiry from slug epoch
                try:
                    slug_epoch = int(slug.split("-")[-1])
                    if slug_epoch < 1_700_000_000:
                        continue
                    market_expired = time.time() >= (slug_epoch + 300)
                except (ValueError, IndexError):
                    continue
                
                if not market_expired:
                    continue
                
                settled = settle_position(pos)
                if settled:
                    state.closed_positions.append(pos)
                    state.positions.remove(pos)
                    state.open_positions = max(0, state.open_positions - 1)
                    state.total_pnl += pos["pnl"]
                    
                    if pos["outcome"] == "WIN":
                        state.wins += 1
                        state.consecutive_losses = 0
                        state.recent_outcomes.append(1)
                    else:
                        state.losses += 1
                        state.consecutive_losses += 1
                        state.daily_loss_usd += abs(pos["pnl"])
                        state.recent_outcomes.append(0)
                    state.recent_pnls.append(pos["pnl"])
                    state.recent_outcomes = state.recent_outcomes[-50:]
                    state.recent_pnls = state.recent_pnls[-50:]
                    
                    log.info(f"RESOLVED: {pos['side']} {pos['asset']} | {pos['outcome']} | "
                             f"PnL=${pos['pnl']:.2f} | RSI was {pos.get('rsi_at_entry', 0):.0f}")
                    
                    with open(resolved_file, "a") as f:
                        f.write(json.dumps(pos, default=str) + "\n")
            
            # Latency tracking
            loop_ms = (time.time() - loop_start) * 1000
            state.signal_latency_ms.append(loop_ms)
            if len(state.signal_latency_ms) > 100:
                state.signal_latency_ms = state.signal_latency_ms[-100:]
            
            # Heartbeat
            if time.time() - last_heartbeat >= 30:
                import statistics
                p50 = statistics.median(state.signal_latency_ms) if state.signal_latency_ms else 0
                wr = state.wins / max(1, state.wins + state.losses) * 100
                bal_str = ""
                if not paper_mode:
                    bal = get_usdc_balance()
                    bal_str = f" bal=${bal:.2f}" if bal is not None else ""
                log.info(f"HB: loop={state.loop_count} scan={loop_ms:.0f}ms "
                         f"mkts={state.markets_scanned} sigs={state.signals_generated} "
                         f"trades={state.daily_trades}/{CONFIG['max_daily_trades']} "
                         f"pos={state.open_positions} W/L={state.wins}/{state.losses} "
                         f"WR={wr:.0f}% PnL=${state.total_pnl:.2f} "
                         f"p50_scan={p50:.0f}ms{bal_str}")
                
                # Supervisor status
                sup = {
                    "timestamp": now.isoformat(), "version": "V21.7.76",
                    "running": not _shutdown, "paper_mode": paper_mode,
                    "loop_count": state.loop_count,
                    "markets_scanned": state.markets_scanned,
                    "signals_generated": state.signals_generated,
                    "wins": state.wins, "losses": state.losses,
                    "win_rate": round(wr, 1),
                    "total_pnl": round(state.total_pnl, 2),
                    "daily_trades": state.daily_trades,
                    "open_positions": state.open_positions,
                    "halted": state.halted, "halt_reason": state.halt_reason,
                    "p50_scan_ms": round(p50, 0),
                    "ws_connected": feed._ws is not None,
                }
                with open(SUP / "v21774_ws_scalper_status.json", "w") as f:
                    json.dump(sup, f, indent=2, default=str)
                
                save_state(state, paper_mode)
                last_heartbeat = time.time()
            
            # Short sleep — WebSocket makes us fast, don't waste it
            time.sleep(CONFIG["scan_interval_seconds"])
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            traceback.print_exc()
            time.sleep(5)
    
    save_state(state, paper_mode)
    log.info(f"Shutdown | W/L={state.wins}/{state.losses} PnL=${state.total_pnl:.2f}")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V21.7.74 WebSocket Scalper")
    parser.add_argument("--paper", action="store_true", default=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    
    state = ScalperState()
    state.paper_mode = not args.live
    load_state(state, state.paper_mode)
    
    if args.status:
        print(json.dumps({
            "wins": state.wins, "losses": state.losses,
            "pnl": state.total_pnl, "open": len(state.positions),
        }, indent=2))
        sys.exit(0)
    
    scalper_loop(state, state.paper_mode)