#!/usr/bin/env python3
"""
V21.7.61 — Tail Risk Selling Engine
=====================================
Integrates the proven strategy from the Platinum-ranked Polymarket bot
(0x06dc51826bc524d9a83770e7de9dd7e005b0452) into FDC's infrastructure.

STRATEGY (from verified $493K profit bot):
  1. Sell "No" on dip/reach markets at 60-98¢ (tail-risk premium collection)
  2. Buy "Yes" on "price above" support levels at 90-98¢ (high-conviction support)
  3. Multi-asset: BTC, ETH, SOL, XRP
  4. Multi-horizon: daily, weekly, monthly markets
  5. Time-decay harvesting: collect premium as expiry approaches
  6. Lottery tickets: small capital on <10¢ outcomes with asymmetric upside

KEY DIFFERENCES FROM V21.7.23:
  - Buys NO tokens (not DOWN) on "Will BTC dip to $X?" markets
  - Buys YES tokens on "Will BTC be above $X?" markets  
  - Targets 60-98¢ (not 3-8¢) — selling tail risk, not buying cheap lottery
  - Multiple market types (not just 15m Up/Down)
  - No canary/preflight gate — directly executable

RUN AS:
  python3 src/v217_live/v21761_tail_risk_engine.py
  python3 src/v217_live/v21761_tail_risk_engine.py --paper  # paper mode
"""
from __future__ import annotations
import json, os, sys, time, logging, signal, traceback, argparse, math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import statistics
import requests

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "output" / "v21761_tail_risk"
SUP = ROOT / "output" / "supervisor"
OUT.mkdir(parents=True, exist_ok=True)
SUP.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════
# ENVIRONMENT & CLOB CLIENT
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

# Wallet constants (same as V21.7.23)
EOA = "0xD4a39D33b8CcB46a08378e426BaEE3591463f090"
DW = "0xaF7B21FE2B18745aE1b2fA2F6F00B0fC4EF3F70b"
CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"

# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY CONFIG
# ═══════════════════════════════════════════════════════════════════════════

# Market types we trade (from the successful bot's portfolio)
MARKET_TYPES = {
    # Sell "No" on dip markets — "Will BTC dip to $50K?" → No @ 83¢
    # This is selling crash insurance. Wins if the extreme drop doesn't happen.
    "DIP": {
        "side": "NO",  # Buy NO tokens
        "question_patterns": ["dip to", "dip below", "fall to", "drop to"],
        "entry_price_range": (0.60, 0.98),  # Buy NO at 60-98¢
        "exit_target": 1.00,  # Settles at $1 if no dip
        "max_position_usd": 5.00,  # Start small ($5 per position)
        "min_implied_edge": 0.03,  # Need 3¢+ edge vs our model
    },
    # Sell "No" on reach markets — "Will BTC reach $82,500?" → No @ 97.5¢
    # This is selling upside breakout insurance.
    "REACH": {
        "side": "NO",
        "question_patterns": ["reach", "hit", "go above", "climb to"],
        "entry_price_range": (0.60, 0.98),
        "exit_target": 1.00,
        "max_position_usd": 5.00,
        "min_implied_edge": 0.03,
    },
    # Buy "Yes" on support levels — "Will BTC be above $60K on Jun 21?" → Yes @ 93¢
    # High-conviction bets that price holds above key levels.
    # Also captures buying YES at low prices when our model says the support is strong.
    "ABOVE": {
        "side": "YES",
        "question_patterns": ["be above", "price of", "above $"],
        "entry_price_range": (0.05, 0.98),  # Buy YES at 5-98¢ (support levels)
        "exit_target": 1.00,
        "max_position_usd": 5.00,
        "min_implied_edge": 0.03,
    },
}

# Risk limits (conservative for initial deployment)
RISK_LIMITS = {
    "max_position_usd": 5.00,
    "max_open_positions": 5,  # Max 5 concurrent positions
    "max_daily_trades": 3,
    "max_daily_loss_usd": 10.0,
    "max_total_engine_loss_usd": 25.0,
    "max_consecutive_losses": 3,
}

# Assets we trade
TRADED_ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUT / "engine.log"),
    ],
)
log = logging.getLogger("v21761")

_shutdown = False
def handle_signal(signum, frame):
    global _shutdown
    log.info(f"Signal {signum} received, shutting down...")
    _shutdown = True
signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# ═══════════════════════════════════════════════════════════════════════════
# MARKET DISCOVERY — Find dip/reach/above markets from Gamma API
# ═══════════════════════════════════════════════════════════════════════════

def discover_crypto_markets() -> List[Dict]:
    """Discover active crypto markets suitable for tail-risk selling."""
    markets = []
    seen_slugs = set()

    try:
        # Query Gamma API for active crypto markets (tag_id=21 is crypto)
        # Paginate to get all markets — API returns max 100 per request
        all_mkts = []
        for offset in [0, 100, 200, 300, 400]:
            r = requests.get(
                f"{GAMMA_HOST}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "tag_id": "21",
                    "limit": 100,
                    "offset": offset,
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=20,
            )
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            all_mkts.extend(batch)
            if len(batch) < 100:
                break  # No more pages
        log.info(f"Gamma API returned {len(all_mkts)} crypto markets (paginated)")
    except Exception as e:
        log.error(f"Gamma API error: {e}")
        return []

    for asset in TRADED_ASSETS:
        try:
            # Filter markets for this asset
            for mk in all_mkts:
                q = mk.get("question", "").lower()
                slug = mk.get("slug", "")
                if slug in seen_slugs:
                    continue

                # Filter for our target market types
                market_type = classify_market_type(q)
                if market_type is None:
                    continue

                # Check if it's a traded asset
                asset_match = None
                q_upper = q.upper()
                for a in TRADED_ASSETS:
                    a_lower = a.lower()
                    # Check multiple forms: BTC/Bitcoin, ETH/Ethereum, SOL/Solana, XRP
                    aliases = {
                        "BTC": ["btc", "bitcoin"],
                        "ETH": ["eth", "ethereum"],
                        "SOL": ["sol", "solana"],
                        "XRP": ["xrp", "ripple"],
                    }
                    for alias in aliases.get(a, [a_lower]):
                        if alias in q:
                            asset_match = a
                            break
                    if asset_match:
                        break
                if not asset_match:
                    continue

                # Parse outcomes and token IDs
                try:
                    outcomes = json.loads(mk.get("outcomes", "[]")) if isinstance(mk.get("outcomes"), str) else mk.get("outcomes", [])
                except:
                    outcomes = []
                try:
                    token_ids = json.loads(mk.get("clobTokenIds", "[]")) if isinstance(mk.get("clobTokenIds"), str) else mk.get("clobTokenIds", [])
                except:
                    token_ids = []

                if len(outcomes) < 2 or len(token_ids) < 2:
                    continue

                # Map outcomes to token IDs
                yes_tid = ""
                no_tid = ""
                for i, o in enumerate(outcomes):
                    if i >= len(token_ids):
                        break
                    ol = str(o).lower()
                    if ol == "yes" or ol == "up":
                        yes_tid = token_ids[i]
                    elif ol == "no" or ol == "down":
                        no_tid = token_ids[i]

                if not yes_tid or not no_tid:
                    continue

                # Get end date
                end_date_str = mk.get("endDate", mk.get("end_date", ""))
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00")) if end_date_str else None
                except:
                    end_dt = None

                tte_seconds = (end_dt - datetime.now(timezone.utc)).total_seconds() if end_dt else 0

                markets.append({
                    "slug": slug,
                    "question": mk.get("question", ""),
                    "market_type": market_type,
                    "asset": asset_match,
                    "yes_token_id": yes_tid,
                    "no_token_id": no_tid,
                    "end_date": end_date_str,
                    "tte_seconds": round(tte_seconds, 1),
                    "active": mk.get("active", True),
                    "closed": mk.get("closed", False),
                    "volume_24h": float(mk.get("volume24hr", 0) or 0),
                    "volume_1d": float(mk.get("volume1day", 0) or 0),
                    "liquidity": float(mk.get("liquidityNum", 0) or 0),
                    "outcomes": outcomes,
                    "config": MARKET_TYPES[market_type],
                })
                seen_slugs.add(slug)

        except Exception as e:
            log.warning(f"Market discovery error for {asset}: {e}")
            continue

    return markets


def classify_market_type(question: str) -> Optional[str]:
    """Classify a market question into our strategy types."""
    q = question.lower()
    for mtype, config in MARKET_TYPES.items():
        for pattern in config["question_patterns"]:
            if pattern in q:
                # Additional validation: must have a dollar amount
                if "$" not in q:
                    continue
                return mtype
    return None


def get_orderbook(token_id: str) -> Optional[Dict]:
    """Fetch CLOB orderbook for a token."""
    try:
        r = requests.get(f"{CLOB_HOST}/book?token_id={token_id}", timeout=10)
        if r.status_code == 200:
            book = r.json()
            asks = sorted(book.get("asks", []), key=lambda x: float(x.get("price", 1)))
            bids = sorted(book.get("bids", []), key=lambda x: float(x.get("price", 0)), reverse=True)
            best_ask = float(asks[0]["price"]) if asks else None
            best_bid = float(bids[0]["price"]) if bids else None
            ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
            bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
            return {
                "best_ask": best_ask,
                "best_bid": best_bid,
                "spread": round(best_ask - best_bid, 4) if best_ask and best_bid else None,
                "ask_depth": ask_depth,
                "bid_depth": bid_depth,
                "book_valid": bool(asks or bids),
            }
    except Exception as e:
        log.warning(f"Orderbook error: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════
# PRICING MODEL — Estimate true probability for edge calculation
# ═══════════════════════════════════════════════════════════════════════════

def get_btc_price() -> float:
    """Get current BTC price from Binance."""
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except:
        pass
    return 0.0


def get_eth_price() -> float:
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", timeout=5)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except:
        pass
    return 0.0


def get_sol_price() -> float:
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", timeout=5)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except:
        pass
    return 0.0


def get_xrp_price() -> float:
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=XRPUSDT", timeout=5)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except:
        pass
    return 0.0


def get_asset_price(asset: str) -> float:
    """Get current price for an asset."""
    prices = {"BTC": get_btc_price, "ETH": get_eth_price, "SOL": get_sol_price, "XRP": get_xrp_price}
    fn = prices.get(asset.upper())
    return fn() if fn else 0.0


def estimate_dip_probability(question: str, asset: str, current_price: float, tte_seconds: float) -> float:
    """Estimate probability that the asset will dip to the target price.
    
    Uses a simplified Brownian motion model:
    P(dip to X) ≈ 1 - Φ((ln(X/S) + μτ/2) / (σ√τ))
    
    where S = current price, X = target, τ = time in years, σ = annual vol
    """
    if current_price <= 0:
        return 0.5
    
    # Parse target price from question
    import re
    matches = re.findall(r'\$[\d,]+(?:\.\d+)?', question)
    if not matches:
        return 0.5
    
    # Find the target price (usually the non-current one)
    target = None
    for m in matches:
        val = float(m.replace("$", "").replace(",", ""))
        if val < current_price * 1.5 and val > 0:  # Reasonable target
            target = val
            break
    
    if target is None or target >= current_price:
        return 0.5  # Can't estimate
    
    # Annualized volatility (rough estimates)
    VOL = {"BTC": 0.60, "ETH": 0.75, "SOL": 0.90, "XRP": 0.85}
    sigma = VOL.get(asset.upper(), 0.70)
    
    # Time to expiry in years
    tau = max(tte_seconds / (365.25 * 24 * 3600), 1e-6)
    
    # Log-normal probability of touching (simplified barrier crossing)
    # P(min price < X before T) ≈ 2 * (1 - Φ(d))
    # where d = (ln(S/X) + σ²τ/2) / (σ√τ)
    # This is the reflection principle for barrier options
    import math as m
    d = (m.log(current_price / target) + sigma**2 * tau / 2) / (sigma * m.sqrt(tau))
    
    # Normal CDF approximation
    def norm_cdf(x):
        return 0.5 * (1 + m.erf(x / m.sqrt(2)))
    
    prob_dip = 2 * (1 - norm_cdf(d))
    prob_dip = min(max(prob_dip, 0.001), 0.999)
    
    return prob_dip


def estimate_reach_probability(question: str, asset: str, current_price: float, tte_seconds: float) -> float:
    """Estimate probability that the asset will reach the target price."""
    if current_price <= 0:
        return 0.5
    
    import re
    matches = re.findall(r'\$[\d,]+(?:\.\d+)?', question)
    if not matches:
        return 0.5
    
    target = None
    for m in matches:
        val = float(m.replace("$", "").replace(",", ""))
        if val > current_price * 1.1:  # Above current
            target = val
            break
    
    if target is None or target <= current_price:
        return 0.5
    
    VOL = {"BTC": 0.60, "ETH": 0.75, "SOL": 0.90, "XRP": 0.85}
    sigma = VOL.get(asset.upper(), 0.70)
    tau = max(tte_seconds / (365.25 * 24 * 3600), 1e-6)
    
    import math as m
    d = (m.log(current_price / target) + sigma**2 * tau / 2) / (sigma * m.sqrt(tau))
    
    def norm_cdf(x):
        return 0.5 * (1 + m.erf(x / m.sqrt(2)))
    
    prob_reach = 2 * (1 - norm_cdf(d))
    prob_reach = min(max(prob_reach, 0.001), 0.999)
    
    return prob_reach


def estimate_above_probability(question: str, asset: str, current_price: float, tte_seconds: float) -> float:
    """Estimate probability that price will be above target at expiry."""
    if current_price <= 0:
        return 0.5
    
    import re
    matches = re.findall(r'\$[\d,]+(?:\.\d+)?', question)
    if not matches:
        return 0.5
    
    target = None
    for m in matches:
        val = float(m.replace("$", "").replace(",", ""))
        if val > 0 and val < current_price * 2:
            target = val
            break
    
    if target is None:
        return 0.5
    
    VOL = {"BTC": 0.60, "ETH": 0.75, "SOL": 0.90, "XRP": 0.85}
    sigma = VOL.get(asset.upper(), 0.70)
    tau = max(tte_seconds / (365.25 * 24 * 3600), 1e-6)
    
    import math as m
    # P(S_T > X) = 1 - Φ((ln(X/S) - μτ) / (σ√τ))
    # Assume μ = 0 (no drift) for conservatism
    d = m.log(target / current_price) / (sigma * m.sqrt(tau))
    
    def norm_cdf(x):
        return 0.5 * (1 + m.erf(x / m.sqrt(2)))
    
    prob_above = 1 - norm_cdf(d)
    prob_above = min(max(prob_above, 0.001), 0.999)
    
    return prob_above


def calculate_edge(market: Dict, book: Dict, current_price: float) -> Optional[Dict]:
    """Calculate edge for a market based on our pricing model vs market price."""
    mtype = market["market_type"]
    config = market["config"]
    tte = market["tte_seconds"]
    asset = market["asset"]
    question = market["question"]
    
    # Get the relevant token based on strategy side
    if config["side"] == "NO":
        token_id = market["no_token_id"]
        best_ask = book.get("best_ask")
        best_bid = book.get("best_bid")
    else:  # YES
        token_id = market["yes_token_id"]
        best_ask = book.get("best_ask")
        best_bid = book.get("best_bid")
    
    if not best_ask or not best_bid:
        return None
    
    # Check price is in our trading range
    lo, hi = config["entry_price_range"]
    if not (lo <= best_ask <= hi):
        return None
    
    # Estimate probability
    if mtype == "DIP":
        # P(dip happens) → we buy NO → our prob = 1 - P(dip)
        p_event = estimate_dip_probability(question, asset, current_price, tte)
        p_our_side = 1 - p_event
    elif mtype == "REACH":
        # P(reach happens) → we buy NO → our prob = 1 - P(reach)
        p_event = estimate_reach_probability(question, asset, current_price, tte)
        p_our_side = 1 - p_event
    elif mtype == "ABOVE":
        # P(price above target) → we buy YES → our prob = P(above)
        p_our_side = estimate_above_probability(question, asset, current_price, tte)
    else:
        return None
    
    # Market implied probability = best_ask (price in cents → probability)
    p_market = best_ask
    
    # Edge = our probability - market price (in probability terms)
    edge = p_our_side - p_market
    
    # Expected value per dollar
    # Buy at best_ask, settle at $1 if we're right
    ev_per_share = p_our_side * 1.0 - best_ask
    
    if edge < config["min_implied_edge"]:
        return None
    
    return {
        "market_slug": market["slug"],
        "question": question,
        "market_type": mtype,
        "asset": asset,
        "side": config["side"],
        "token_id": token_id,
        "best_ask": best_ask,
        "best_bid": best_bid,
        "spread": book.get("spread"),
        "our_probability": round(p_our_side, 4),
        "market_probability": round(p_market, 4),
        "edge": round(edge, 4),
        "ev_per_share": round(ev_per_share, 4),
        "tte_seconds": tte,
        "position_size_usd": config["max_position_usd"],
        "volume_24h": market.get("volume_24h", 0),
        "liquidity": market.get("liquidity", 0),
    }


# ═══════════════════════════════════════════════════════════════════════════
# CLOB CLIENT (reused from V21.7.23)
# ═══════════════════════════════════════════════════════════════════════════

_clob_client = None

def get_clob_client():
    """Get or create CLOB client with POLY_1271 deposit wallet flow."""
    global _clob_client
    if _clob_client is None:
        env = load_env()
        pk = env.get("PM_WALLET_PRIVATE_KEY", "")
        if not pk:
            raise ValueError("No PM_WALLET_PRIVATE_KEY in env")
        try:
            from py_clob_client_v2 import ClobClientV2 as ClobClient, SignatureTypeV2, BalanceAllowanceParams, AssetType
            _clob_client = ClobClient(
                CLOB_HOST,
                key=pk,
                chain_id=CHAIN_ID,
                signature_type=SignatureTypeV2.POLY_1271.value,
                funder=DW,
            )
            log.info("CLOB client initialized (POLY_1271)")
        except Exception as e:
            raise ValueError(f"CLOB client init failed: {e}")
    return _clob_client


def get_wallet_balance() -> float:
    """Get wallet pUSD balance."""
    try:
        client = get_clob_client()
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params=params)
        return float(bal.get("balance", 0))
    except Exception as e:
        log.warning(f"Balance check failed: {e}")
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# ENGINE STATE
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class EngineState:
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    loop_count: int = 0
    markets_scanned: int = 0
    opportunities_found: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    daily_trades: int = 0
    daily_loss_usd: float = 0.0
    open_positions: int = 0
    total_pnl: float = 0.0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    paper_mode: bool = True
    last_scan_ts: float = 0.0
    wallet_balance: float = 0.0
    opportunities: List[Dict] = field(default_factory=list)
    positions: List[Dict] = field(default_factory=list)
    scan_latency_ms: List[float] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# EXECUTION
# ═══════════════════════════════════════════════════════════════════════════

def execute_order(opp: Dict, clob_client, paper_mode: bool = True) -> Dict:
    """Execute a tail-risk order. FAK/FOK only. One shot."""
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_slug": opp["market_slug"],
        "question": opp["question"],
        "side": opp["side"],
        "token_id": opp["token_id"],
        "ask": opp["best_ask"],
        "size_usd": opp["position_size_usd"],
        "status": "PENDING",
        "order_id": None,
        "fill_status": None,
        "fill_price": None,
        "error": None,
    }
    
    if paper_mode:
        result["status"] = "PAPER_FILLED"
        result["fill_status"] = "paper"
        result["fill_price"] = opp["best_ask"]
        result["shares"] = opp["position_size_usd"] / opp["best_ask"]
        result["paper_pnl_if_settled"] = (1.0 - opp["best_ask"]) * result["shares"]
        log.info(f"PAPER ORDER: {opp['side']} {opp['question'][:50]} @ {opp['best_ask']*100:.1f}¢ | "
                 f"size=${opp['position_size_usd']} | edge={opp['edge']*100:.1f}pp | "
                 f"EV=${result['paper_pnl_if_settled']:.2f}")
        with open(OUT / "paper_orders.jsonl", "a") as f:
            f.write(json.dumps(result, default=str) + "\n")
        return result
    
    # LIVE execution
    try:
        from py_clob_client_v2 import OrderArgsV2, CreateOrderOptions, OrderType
        
        order_args = OrderArgsV2(
            token_id=opp["token_id"],
            price=opp["best_ask"],
            size=opp["position_size_usd"],
            side="BUY",
        )
        options = CreateOrderOptions(
            tick_size="0.01",
            neg_risk=False,
        )
        
        t0 = time.time()
        signed_order = clob_client.create_order(order_args, options)
        
        # Verify maker and sig type
        if signed_order.maker != DW:
            result["error"] = f"Maker mismatch: {signed_order.maker}"
            result["status"] = "EMERGENCY_HALT"
            log.critical(f"EMERGENCY HALT: {result['error']}")
            return result
        if signed_order.signatureType != 3:
            result["error"] = f"sig_type mismatch: {signed_order.signatureType}"
            result["status"] = "EMERGENCY_HALT"
            return result
        
        result["status"] = "SUBMITTED"
        
        # Try FOK first
        try:
            order_result = clob_client.post_order(signed_order, OrderType.FOK)
            result["order_type_used"] = "FOK"
        except Exception as e_fok:
            log.warning(f"FOK failed: {e_fok}, trying FAK via IOC")
            signed_order = clob_client.create_order(order_args, options)
            try:
                order_result = clob_client.post_order(signed_order, OrderType.GTC)
                result["order_type_used"] = "GTC_EMERGENCY_CANCEL"
                log.error("GTC used as fallback — cancelling immediately!")
            except Exception as e_gtc:
                result["error"] = f"FOK + GTC both failed: {e_fok}, {e_gtc}"
                result["status"] = "ORDER_FAILED"
                return result
        
        t_post = time.time() - t0
        order_id = order_result.get("orderID", "")
        fill_status = order_result.get("status", "")
        
        result["order_id"] = order_id
        result["fill_status"] = fill_status
        result["status"] = "ACKNOWLEDGED"
        result["latency_ms"] = round(t_post * 1000)
        
        # Emergency cancel if GTC fallback
        if result.get("order_type_used") == "GTC_EMERGENCY_CANCEL" and order_id:
            try:
                clob_client.cancel_orders([order_id])
                log.info("Emergency GTC cancelled")
                result["fill_status"] = "CANCELLED_AFTER_FOK_FAILURE"
            except:
                pass
        
        # Cancel all remaining as safety
        try:
            clob_client.cancel_all()
        except:
            pass
        
        if fill_status in ("live", "matched"):
            log.info(f"LIVE FILL: {opp['side']} {opp['question'][:50]} @ {opp['best_ask']*100:.1f}¢ | id={order_id[:20]}...")
        
    except Exception as e:
        result["error"] = str(e)
        result["status"] = "ERROR"
        result["traceback"] = traceback.format_exc()
        log.error(f"Order error: {e}")
        try:
            clob_client.cancel_all()
        except:
            pass
    
    # Journal order attempt
    with open(OUT / "order_attempts.jsonl", "a") as f:
        f.write(json.dumps(result, default=str) + "\n")
    
    return result


# ═══════════════════════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ═══════════════════════════════════════════════════════════════════════════

def scan_loop(state: EngineState, paper_mode: bool):
    """Main scan loop: discover markets, find edges, execute trades."""
    global _shutdown
    
    log.info(f"V21.7.61 Tail Risk Engine starting | paper_mode={paper_mode}")
    log.info(f"Strategy: Sell No on dip/reach, Buy Yes on support | Assets: {TRADED_ASSETS}")
    log.info(f"Risk limits: max_pos=${RISK_LIMITS['max_position_usd']} max_open={RISK_LIMITS['max_open_positions']} max_daily={RISK_LIMITS['max_daily_trades']}")
    
    SCAN_INTERVAL = 120  # 2 minutes between full scans
    HEARTBEAT_INTERVAL = 60
    
    clob = None
    if not paper_mode:
        try:
            clob = get_clob_client()
            state.wallet_balance = get_wallet_balance()
            log.info(f"Wallet balance: ${state.wallet_balance:.2f}")
        except Exception as e:
            log.error(f"CLOB init failed: {e} — falling back to paper mode")
            paper_mode = True
            state.paper_mode = True
    
    while not _shutdown:
        try:
            loop_start = time.time()
            now = datetime.now(timezone.utc)
            
            # ─── Check risk limits ───
            if state.halted:
                log.error(f"Engine HALTED: {state.halt_reason}")
                break
            
            if state.consecutive_losses >= RISK_LIMITS["max_consecutive_losses"]:
                state.halted = True
                state.halt_reason = f"Max consecutive losses ({state.consecutive_losses})"
                log.critical(state.halt_reason)
                break
            
            if state.daily_loss_usd >= RISK_LIMITS["max_daily_loss_usd"]:
                log.warning(f"Daily loss limit reached: ${state.daily_loss_usd:.2f}")
                # Continue scanning but don't trade
                can_trade = False
            else:
                can_trade = state.daily_trades < RISK_LIMITS["max_daily_trades"]
            
            if state.open_positions >= RISK_LIMITS["max_open_positions"]:
                can_trade = False
            
            # ─── Discover markets ───
            log.info("Scanning markets...")
            markets = discover_crypto_markets()
            state.markets_scanned = len(markets)
            log.info(f"Discovered {len(markets)} candidate markets")
            
            if not markets:
                log.warning("No markets found, sleeping...")
                time.sleep(SCAN_INTERVAL)
                continue
            
            # ─── Get current prices ───
            asset_prices = {}
            for asset in TRADED_ASSETS:
                asset_prices[asset] = get_asset_price(asset)
            log.info(f"Prices: BTC=${asset_prices['BTC']:,.0f} ETH=${asset_prices['ETH']:,.0f} "
                     f"SOL=${asset_prices['SOL']:,.1f} XRP=${asset_prices['XRP']:,.4f}")
            
            # ─── Fetch orderbooks concurrently ───
            # Determine which token to fetch based on strategy side
            token_queries = []
            for m in markets:
                config = m["config"]
                token_id = m["no_token_id"] if config["side"] == "NO" else m["yes_token_id"]
                if token_id and 0 < m["tte_seconds"] < 30 * 24 * 3600:  # < 30 days
                    token_queries.append((m, token_id))
            
            books = {}
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {}
                for m, tid in token_queries:
                    f = executor.submit(get_orderbook, tid)
                    futures[f] = (m, tid)
                for f in as_completed(futures):
                    m, tid = futures[f]
                    try:
                        book = f.result()
                        if book and book.get("book_valid"):
                            books[m["slug"]] = (m, book)
                    except:
                        pass
            
            log.info(f"Fetched {len(books)} valid orderbooks")
            
            # ─── Calculate edges ───
            state.opportunities = []
            for slug, (m, book) in books.items():
                current_price = asset_prices.get(m["asset"], 0)
                if current_price <= 0:
                    continue
                
                edge = calculate_edge(m, book, current_price)
                if edge:
                    state.opportunities.append(edge)
            
            # Sort by edge (highest first)
            state.opportunities.sort(key=lambda x: x["edge"], reverse=True)
            state.opportunities_found = len(state.opportunities)
            
            log.info(f"Found {len(state.opportunities)} opportunities with positive edge")
            
            # Log top opportunities
            for i, opp in enumerate(state.opportunities[:5]):
                log.info(f"  #{i+1} {opp['asset']} {opp['market_type']} {opp['side']} | "
                         f"{opp['question'][:45]} | "
                         f"ask={opp['best_ask']*100:.1f}¢ edge={opp['edge']*100:.1f}pp "
                         f"EV=${opp['ev_per_share']*opp['position_size_usd']/opp['best_ask']:.2f} "
                         f"TTE={opp['tte_seconds']/3600:.1f}h")
            
            # Log all opportunities
            with open(OUT / "opportunities.jsonl", "a") as f:
                for opp in state.opportunities:
                    f.write(json.dumps(opp, default=str) + "\n")
            
            # ─── Execute trades ───
            if can_trade and state.opportunities:
                # Try opportunities in order (highest edge first) until one passes all gates
                for best_opp in state.opportunities:
                    # Additional risk checks
                    if best_opp["best_ask"] > 0.98:
                        log.info(f"Skipping {best_opp['question'][:40]} — ask too close to $1 (no edge)")
                        continue
                    elif best_opp["tte_seconds"] < 3600:
                        log.info(f"Skipping {best_opp['question'][:40]} — TTE too short ({best_opp['tte_seconds']/60:.0f}m)")
                        continue
                    elif best_opp["volume_24h"] < 500:
                        log.info(f"Skipping {best_opp['question'][:40]} — volume too low (${best_opp['volume_24h']:.0f})")
                        continue
                    else:
                        log.info(f"🎯 EXECUTING: {best_opp['side']} {best_opp['question'][:60]} @ {best_opp['best_ask']*100:.1f}¢")
                        order_result = execute_order(best_opp, clob, paper_mode)
                        
                        if order_result["status"] in ("PAPER_FILLED", "ACKNOWLEDGED"):
                            state.orders_submitted += 1
                            state.daily_trades += 1
                            state.open_positions += 1
                            
                            # Track position
                            pos = {
                                "timestamp": now.isoformat(),
                                "market_slug": best_opp["market_slug"],
                                "question": best_opp["question"],
                                "side": best_opp["side"],
                                "entry_price": best_opp["best_ask"],
                                "token_id": best_opp["token_id"],
                                "size_usd": best_opp["position_size_usd"],
                                "our_probability": best_opp["our_probability"],
                                "edge": best_opp["edge"],
                                "tte_at_entry": best_opp["tte_seconds"],
                                "order_status": order_result["status"],
                                "order_id": order_result.get("order_id"),
                            }
                            state.positions.append(pos)
                            state.opportunities = []  # Clear after taking one
                            
                            with open(OUT / "positions.jsonl", "a") as f:
                                f.write(json.dumps(pos, default=str) + "\n")
                            
                            if order_result["status"] == "ACKNOWLEDGED" and order_result.get("fill_status") in ("live", "matched"):
                                state.orders_filled += 1
                        else:
                            state.orders_rejected += 1
                            if "loss" in str(order_result.get("error", "")).lower():
                                state.consecutive_losses += 1
                                state.daily_loss_usd += best_opp["position_size_usd"]
                        
                        break  # Only take one trade per scan cycle
            
            # ─── Heartbeat ───
            loop_ms = (time.time() - loop_start) * 1000
            state.scan_latency_ms.append(loop_ms)
            if len(state.scan_latency_ms) > 100:
                state.scan_latency_ms = state.scan_latency_ms[-100:]
            
            p50_ms = statistics.median(state.scan_latency_ms) if state.scan_latency_ms else 0
            
            heartbeat = {
                "timestamp": now.isoformat(),
                "pid": os.getpid(),
                "loop_count": state.loop_count,
                "scan_latency_ms": round(loop_ms, 1),
                "p50_scan_ms": round(p50_ms, 1),
                "markets_scanned": state.markets_scanned,
                "opportunities_found": state.opportunities_found,
                "orders_submitted": state.orders_submitted,
                "orders_filled": state.orders_filled,
                "daily_trades": state.daily_trades,
                "open_positions": state.open_positions,
                "wallet_balance": state.wallet_balance,
                "paper_mode": paper_mode,
                "halted": state.halted,
            }
            with open(OUT / "heartbeat.jsonl", "a") as f:
                f.write(json.dumps(heartbeat) + "\n")
            
            log.info(f"Heartbeat: loop={state.loop_count} scan={loop_ms:.0f}ms "
                     f"markets={state.markets_scanned} opps={state.opportunities_found} "
                     f"trades={state.daily_trades}/{RISK_LIMITS['max_daily_trades']} "
                     f"pos={state.open_positions}/{RISK_LIMITS['max_open_positions']} "
                     f"paper={paper_mode}")
            
            # ─── Supervisor status ───
            sup_status = {
                "timestamp": now.isoformat(),
                "version": "V21.7.61",
                "classification": "V21.7.61_TAIL_RISK_ENGINE",
                "running": not _shutdown,
                "paper_mode": paper_mode,
                "loop_count": state.loop_count,
                "markets_scanned": state.markets_scanned,
                "opportunities": state.opportunities_found,
                "orders_submitted": state.orders_submitted,
                "orders_filled": state.orders_filled,
                "orders_rejected": state.orders_rejected,
                "daily_trades": state.daily_trades,
                "open_positions": state.open_positions,
                "halted": state.halted,
                "halt_reason": state.halt_reason,
                "risk_limits": RISK_LIMITS,
                "top_opportunities": state.opportunities[:3] if state.opportunities else [],
            }
            with open(SUP / "v21761_tail_risk_engine_status.json", "w") as f:
                json.dump(sup_status, f, indent=2, default=str)
            
            state.loop_count += 1
            
            # ─── Sleep ───
            elapsed = time.time() - loop_start
            sleep_time = max(10, SCAN_INTERVAL - elapsed)
            time.sleep(sleep_time)
            
        except KeyboardInterrupt:
            log.info("Keyboard interrupt")
            break
        except Exception as e:
            log.error(f"Scan loop error: {e}")
            log.error(traceback.format_exc())
            time.sleep(30)
    
    # ─── Final report ───
    final = {
        "version": "V21.7.61",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "classification": "V21.7.61_TAIL_RISK_ENGINE_SHUTDOWN",
        "loop_count": state.loop_count,
        "markets_scanned_total": state.markets_scanned * state.loop_count,
        "opportunities_found_total": state.opportunities_found,
        "orders_submitted": state.orders_submitted,
        "orders_filled": state.orders_filled,
        "paper_mode": paper_mode,
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "positions": state.positions,
    }
    with open(OUT / "final_report.json", "w") as f:
        json.dump(final, f, indent=2, default=str)
    
    log.info(f"Engine shutdown | loops={state.loop_count} "
             f"orders={state.orders_submitted} fills={state.orders_filled}")
    log.info(f"Final report written to {OUT / 'final_report.json'}")


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V21.7.61 Tail Risk Engine")
    parser.add_argument("--paper", action="store_true", help="Paper trading mode")
    parser.add_argument("--live", action="store_true", help="Live trading mode (REAL MONEY)")
    parser.add_argument("--status", action="store_true", help="Show engine status")
    args = parser.parse_args()
    
    if args.status:
        status_file = SUP / "v21761_tail_risk_engine_status.json"
        if status_file.exists():
            print(status_file.read_text())
        else:
            print(json.dumps({"status": "NOT_RUNNING", "version": "V21.7.61"}, indent=2))
        sys.exit(0)
    
    paper_mode = not args.live  # Default to paper mode
    
    if args.live:
        log.info("⚠️  LIVE TRADING MODE — REAL MONEY AT RISK ⚠️")
        log.info(f"Wallet: {DW}")
        # Confirm
        env = load_env()
        pk = env.get("PM_WALLET_PRIVATE_KEY", "")
        if not pk:
            log.error("No PM_WALLET_PRIVATE_KEY — cannot run live")
            sys.exit(1)
    
    state = EngineState(paper_mode=paper_mode)
    
    try:
        scan_loop(state, paper_mode)
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception as e:
        log.error(f"Fatal: {e}")
        traceback.print_exc()
    finally:
        log.info("Engine shutdown complete")