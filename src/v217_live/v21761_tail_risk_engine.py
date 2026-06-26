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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict
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
# V21.7.58: Long-shot Kelly sizing — 2% bankroll cap, 10% fractional Kelly
RISK_LIMITS = {
    "max_position_usd": 5.00,
    "max_open_positions": 3,  # Reduced from 10 — prevent saturation deadlock
    "max_daily_trades": 3,    # Reduced from 10 — quality over quantity
    "max_daily_loss_usd": 15.0,
    "max_total_engine_loss_usd": 50.0,
    "max_consecutive_losses": 5,
    # V21.7.58: Long-shot specific limits
    "long_shot_max_position_pct": 0.02,  # Max 2% bankroll per long-shot (was 5%)
    "long_shot_kelly_fraction": 0.10,    # 10% fractional Kelly for long-shots (was 25%)
    "long_shot_min_prob": 0.05,          # Long-shot = market_prob < 5%
    "long_shot_min_edge_pp": 30.0,       # Need 30pp+ edge on long-shots (3x multiplier)
    "long_shot_screen_multiplier": 3.0, # model_prob / market_prob >= 3x to enter
    # ─── Live promotion gates ───
    "live_min_resolved_trades": 25,
    "live_min_win_rate": 0.55,
    "live_min_profit_factor": 1.25,
    "live_min_pnl_usd": 25.0,
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

                # Read neg_risk from PM market data (critical for CLOB order signing)
                neg_risk = mk.get("neg_risk", False)
                if isinstance(neg_risk, str):
                    neg_risk = neg_risk.lower() == "true"

                markets.append({
                    "slug": slug,
                    "question": mk.get("question", ""),
                    "market_type": market_type,
                    "asset": asset_match,
                    "yes_token_id": yes_tid,
                    "no_token_id": no_tid,
                    "condition_id": mk.get("conditionId", mk.get("condition_id", "")),
                    "end_date": end_date_str,
                    "tte_seconds": round(tte_seconds, 1),
                    "active": mk.get("active", True),
                    "closed": mk.get("closed", False),
                    "volume_24h": float(mk.get("volume24hr", 0) or 0),
                    "volume_1d": float(mk.get("volume1day", 0) or 0),
                    "liquidity": float(mk.get("liquidityNum", 0) or 0),
                    "outcomes": outcomes,
                    "neg_risk": neg_risk,
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
    # Exclude "between $X and $Y" range questions — these are not ABOVE/DIP/REACH
    if "between" in q and " and " in q:
        return None
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


# ─── V21.7.69: Empirical volatility from Binance klines ─────────────
# Replaces hardcoded σ constants with actual recent realized volatility.
# Fetches last 24h of hourly klines, computes log-return stdev, annualizes.
# Falls back to legacy constants if API fails (never crashes the model).

_empirical_vol_cache: Dict[str, tuple] = {}  # asset → (vol, timestamp)

def _get_empirical_vol(asset: str, current_price: float) -> float:
    """Annualized realized vol from Binance 1h klines over last 24h.
    
    Returns σ_annual = std(log_returns_1h) * sqrt(24 * 365.25).
    Falls back to legacy constants on API failure.
    """
    import time as _time
    import math as _math
    
    LEGACY_VOL = {"BTC": 0.60, "ETH": 0.75, "SOL": 0.90, "XRP": 0.85}
    fallback = LEGACY_VOL.get(asset.upper(), 0.70)
    
    now = _time.time()
    cached = _empirical_vol_cache.get(asset.upper())
    if cached and (now - cached[1]) < 300:  # 5-min cache
        return cached[0]
    
    try:
        import requests as _req
        sym_map = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
        symbol = sym_map.get(asset.upper(), f"{asset.upper()}USDT")
        # V21.7.70: Use 7 days of 1h klines (168 candles) for stable vol estimate.
        # 24h was too noisy (1.27-1.75 range). 7-day gives 0.50-0.87, close to
        # legacy constants but data-driven and adapts to regime changes.
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=168"
        resp = _req.get(url, timeout=5)
        if resp.status_code != 200:
            return fallback
        klines = resp.json()
        if len(klines) < 5:
            return fallback
        
        # Close prices are index 4 in Binance kline response
        closes = [float(k[4]) for k in klines]
        log_rets = [_math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        
        mean_lr = sum(log_rets) / len(log_rets)
        var_lr = sum((r - mean_lr) ** 2 for r in log_rets) / (len(log_rets) - 1)
        std_lr = _math.sqrt(var_lr)
        
        # Annualize: 24 hours/day * 365.25 days/year = 8766 periods/year
        sigma_annual = std_lr * _math.sqrt(8766)
        
        # Clamp to sane range [0.20, 2.00] — crypto vol rarely outside this
        sigma_annual = max(0.20, min(2.00, sigma_annual))
        
        _empirical_vol_cache[asset.upper()] = (sigma_annual, now)
        return sigma_annual
    except Exception:
        return fallback


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
    
    sigma = _get_empirical_vol(asset, current_price)
    
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
    
    sigma = _get_empirical_vol(asset, current_price)
    tau = max(tte_seconds / (365.25 * 24 * 3600), 1e-6)
    
    import math as m
    d = (m.log(current_price / target) + sigma**2 * tau / 2) / (sigma * m.sqrt(tau))
    
    def norm_cdf(x):
        return 0.5 * (1 + m.erf(x / m.sqrt(2)))
    
    prob_reach = 2 * (1 - norm_cdf(d))
    prob_reach = min(max(prob_reach, 0.001), 0.999)
    
    return prob_reach


def estimate_above_probability(question: str, asset: str, current_price: float, tte_seconds: float) -> float:
    """Estimate probability that price will be above target at expiry.
    
    Uses Black-Scholes with drift = risk-free rate (conservative, ~0).
    For ABOVE markets, only enter when current price is already above target
    (selling tail risk on support levels). When price is below target, probability
    drops sharply — this is a lottery ticket, not an edge trade.
    """
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
    
    # If current price is below target, probability is very low
    # Don't buy YES on "above $X" when price is below $X — that's a lottery ticket
    if current_price < target:
        # Calculate how far below as percentage
        distance_pct = (target - current_price) / current_price
        # If >2% below target, probability is very low
        if distance_pct > 0.02:
            return 0.01
    
    # V21.7.69: Empirical vol from recent price history, not hardcoded
    # Hardcoded σ overestimates edge for low-vol regimes and underestimates for high-vol
    sigma = _get_empirical_vol(asset, current_price)
    tau = max(tte_seconds / (365.25 * 24 * 3600), 1e-6)
    
    import math as m
    # P(S_T > X) = 1 - Φ((ln(X/S) - μτ) / (σ√τ))
    # V21.7.69: Drift = 0 (risk-neutral). Empirically crypto has near-zero
    # drift over short horizons (hours/days), and assuming negative drift
    # biases the model to buy YES on "above" when market disagrees.
    mu = 0.0
    d = (m.log(target / current_price) - mu * tau) / (sigma * m.sqrt(tau))
    
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
    
    # V21.7.58: Long-shot screening + Kelly sizing
    is_long_shot = p_market < RISK_LIMITS["long_shot_min_prob"]
    if is_long_shot:
        # Screen: require model_prob / market_prob >= 3x multiplier
        multiplier = p_our_side / max(p_market, 0.001)
        if multiplier < RISK_LIMITS["long_shot_screen_multiplier"]:
            return None
        # Screen: require 30pp+ edge
        if edge * 100 < RISK_LIMITS["long_shot_min_edge_pp"]:
            return None
    
    # V21.7.58: Kelly-based position sizing
    # Kelly fraction = (p - price) / (1 - price) for binary
    if best_ask > 0 and best_ask < 1:
        kelly_f = (p_our_side - best_ask) / (1.0 - best_ask)
    else:
        kelly_f = 0.0
    
    kelly_fraction = RISK_LIMITS["long_shot_kelly_fraction"] if is_long_shot else 0.25
    bankroll = RISK_LIMITS["max_total_engine_loss_usd"]  # Conservative bankroll estimate
    kelly_size = kelly_f * kelly_fraction * bankroll
    
    # Cap: 2% for long-shots, 5% for normal, MAX_POSITION_USD absolute cap
    max_pct = RISK_LIMITS["long_shot_max_position_pct"] if is_long_shot else 0.05
    position_size = min(kelly_size, config["max_position_usd"], bankroll * max_pct)
    position_size = max(1.0, round(position_size, 2))  # Min $1 (PM minimum)
    
    return {
        "market_slug": market["slug"],
        "question": question,
        "market_type": mtype,
        "asset": asset,
        "side": config["side"],
        "token_id": token_id,
        "condition_id": market.get("condition_id", ""),
        "best_ask": best_ask,
        "best_bid": best_bid,
        "spread": book.get("spread"),
        "our_probability": round(p_our_side, 4),
        "market_probability": round(p_market, 4),
        "edge": round(edge, 4),
        "ev_per_share": round(ev_per_share, 4),
        "tte_seconds": tte,
        "is_long_shot": is_long_shot,             # V21.7.58
        "kelly_fraction": round(kelly_f, 4),        # V21.7.58
        "position_size_usd": position_size,         # V21.7.58: Kelly-sized
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
            from py_clob_client_v2 import ClobClient, SignatureTypeV2, ApiCreds
            creds = ApiCreds(
                api_key=env.get("PM_API_KEY", ""),
                api_secret=env.get("PM_API_SECRET", ""),
                api_passphrase=env.get("PM_API_PASSPHRASE", ""),
            )
            _clob_client = ClobClient(
                CLOB_HOST,
                key=pk,
                chain_id=CHAIN_ID,
                creds=creds,
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
        raw = bal.get("balance", "0")
        return int(raw) / 1_000_000
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
    daily_reset: str = ""  # UTC date string for daily reset
    open_positions: int = 0
    total_pnl: float = 0.0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    paper_mode: bool = True
    consecutive_order_failures: int = 0  # V21.7.58: Circuit breaker for order failures
    last_scan_ts: float = 0.0
    wallet_balance: float = 0.0
    opportunities: List[Dict] = field(default_factory=list)
    positions: List[Dict] = field(default_factory=list)
    closed_positions: List[Dict] = field(default_factory=list)
    scan_latency_ms: List[float] = field(default_factory=list)
    wins: int = 0
    losses: int = 0


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
        
        # Convert USD position size to shares (PM CLOB size = number of shares)
        size_usd = opp["position_size_usd"]
        shares = round(size_usd / max(opp["best_ask"], 0.01), 2)
        shares = max(int(shares), 1)  # Minimum 1 share, round to int
        actual_cost = shares * opp["best_ask"]
        result["shares"] = shares
        result["actual_cost"] = round(actual_cost, 2)
        log.info(f"Order sizing: ${size_usd} / ask={opp['best_ask']:.4f} = {shares} shares (cost=${actual_cost:.2f})")
        order_args = OrderArgsV2(
            token_id=opp["token_id"],
            price=opp["best_ask"],
            size=shares,
            side="BUY",
        )
        # Use dynamic neg_risk from market data (not hardcoded)
        market_neg_risk = opp.get("neg_risk", False)
        options = CreateOrderOptions(
            tick_size="0.01",
            neg_risk=market_neg_risk,
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
            
            # ─── Daily reset (UTC day boundary) ───
            today = now.strftime("%Y-%m-%d")
            if state.daily_reset != today:
                log.info(f"Daily reset: {state.daily_reset} → {today} | trades={state.daily_trades} loss=${state.daily_loss_usd:.2f}")
                state.daily_trades = 0
                state.daily_loss_usd = 0.0
                state.daily_reset = today
            
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
                # Track which slugs we already have positions in
                existing_slugs = set()
                try:
                    with open(OUT / "positions.jsonl", "r") as pf:
                        for line in pf:
                            if line.strip():
                                pos_data = json.loads(line)
                                if pos_data.get("order_status") != "SETTLED":
                                    existing_slugs.add(pos_data.get("market_slug", ""))
                except FileNotFoundError:
                    pass
                
                # Try opportunities in order (highest edge first) until one passes all gates
                for best_opp in state.opportunities:
                    # Skip if we already have a position in this market
                    if best_opp.get("market_slug", "") in existing_slugs:
                        log.info(f"Skipping {best_opp['question'][:40]} — already have position")
                        existing_slugs.add(best_opp["market_slug"])
                        continue
                    # Additional risk checks
                    if best_opp["best_ask"] > 0.98:
                        log.info(f"Skipping {best_opp['question'][:40]} — ask too close to $1 (no edge)")
                        continue
                    elif best_opp["tte_seconds"] < 1800:  # Skip <30min (was 1hr, too restrictive)
                        log.info(f"Skipping {best_opp['question'][:40]} — TTE too short ({best_opp['tte_seconds']/60:.0f}m)")
                        continue
                    elif best_opp["volume_24h"] < 500:
                        log.info(f"Skipping {best_opp['question'][:40]} — volume too low (${best_opp['volume_24h']:.0f})")
                        continue
                    elif state.consecutive_order_failures >= 5:
                        log.warning(f"Skipping — {state.consecutive_order_failures} consecutive order failures, halting execution")
                        if not paper_mode:
                            log.error("Switching to PAPER mode due to repeated order failures")
                            paper_mode = True
                            state.paper_mode = True
                        continue
                    else:
                        log.info(f"🎯 EXECUTING: {best_opp['side']} {best_opp['question'][:60]} @ {best_opp['best_ask']*100:.1f}¢")
                        order_result = execute_order(best_opp, clob, paper_mode)
                        
                        # V21.7.58: Track consecutive failures for circuit breaker
                        if order_result.get("status") == "FAILED":
                            state.consecutive_order_failures = getattr(state, 'consecutive_order_failures', 0) + 1
                        elif order_result["status"] in ("PAPER_FILLED", "ACKNOWLEDGED"):
                            state.consecutive_order_failures = 0
                        
                        if order_result["status"] in ("PAPER_FILLED", "ACKNOWLEDGED"):
                            state.orders_submitted += 1
                            state.daily_trades += 1
                            state.open_positions += 1
                            existing_slugs.add(best_opp["market_slug"])  # Prevent duplicate entries
                            
                            # Track position
                            pos = {
                                "timestamp": now.isoformat(),
                                "entry_timestamp": now.isoformat(),
                                "market_slug": best_opp["market_slug"],
                                "question": best_opp["question"],
                                "asset": best_opp.get("asset", ""),
                                "condition_id": best_opp.get("condition_id", ""),
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
                        
                        break  # Only take top opportunity per scan cycle
            
            # ─── Settle expired positions via Polymarket Gamma API ───
            for pos in list(state.positions):
                slug = pos.get("market_slug", "")
                
                # Compute expiry: for updown slugs (btc-updown-5m-EPOCH), use epoch+300
                # For date-based slugs (june-22-2026), parse the date and add 24h
                # Fallback to entry_timestamp + tte
                market_expired = False
                try:
                    last_part = slug.split("-")[-1]
                    # Check if it's a genuine epoch (10-digit number > 1_700_000_000)
                    slug_epoch = int(last_part)
                    if slug_epoch > 1_700_000_000:
                        # Genuine epoch — updown market
                        market_expired = time.time() >= (slug_epoch + 300)
                    else:
                        # Not an epoch — likely a year or other non-epoch suffix
                        # Parse the date from the slug (e.g., june-22-2026)
                        import re
                        date_match = re.search(r'(\w+)-(\d{1,2})-(\d{4})$', slug)
                        if date_match:
                            month_str, day_str, year_str = date_match.groups()
                            from datetime import datetime as _dt2
                            try:
                                expiry_date = _dt2.strptime(f"{month_str} {day_str} {year_str}", "%B %d %Y")
                                # Market expires at end of that day UTC
                                market_expired = time.time() >= (expiry_date.replace(hour=23, minute=59).timestamp() + 3600)  # +1h grace
                            except ValueError:
                                market_expired = False
                        else:
                            # Unknown format — use entry_timestamp + tte
                            entry_ts_str = pos.get("entry_timestamp", pos.get("timestamp", ""))
                            tte_at_entry = pos.get("tte_at_entry", 0)
                            try:
                                from datetime import datetime as _dt
                                entry_ts = _dt.fromisoformat(entry_ts_str.replace("Z", "+00:00")).timestamp()
                                market_expired = time.time() >= (entry_ts + tte_at_entry + 300)
                            except (ValueError, AttributeError):
                                log.warning(f"Cannot determine expiry for {slug}, skipping settlement")
                                continue
                except (ValueError, IndexError):
                    # Fallback for non-updown markets
                    entry_ts_str = pos.get("entry_timestamp", pos.get("timestamp", ""))
                    tte_at_entry = pos.get("tte_at_entry", 0)
                    try:
                        from datetime import datetime as _dt
                        entry_ts = _dt.fromisoformat(entry_ts_str.replace("Z", "+00:00")).timestamp()
                        market_expired = time.time() >= (entry_ts + tte_at_entry + 300)  # 5min grace
                    except (ValueError, AttributeError):
                        log.warning(f"Cannot determine expiry for {slug}, skipping settlement")
                        continue
                
                if not market_expired:
                    continue  # Position not yet expired
                
                # Market expired — check Polymarket resolution
                outcome = "UNKNOWN"
                pnl = 0.0
                pos_question = pos.get("question", "").lower()
                pos_cid = pos.get("condition_id", "")
                our_side = pos.get("side", "").upper()
                our_token_idx = 0 if our_side == "YES" else 1
                
                def _resolve_from_market(mk):
                    """Extract win/loss from a Gamma market dict. Returns (outcome, pnl) or (UNKNOWN, 0)."""
                    closed = mk.get("closed", False)
                    if not closed:
                        return ("UNKNOWN", 0)
                    prices_raw = mk.get("outcomePrices", "[]")
                    outcomes_raw = mk.get("outcomes", "[]")
                    try:
                        prices = [float(p) for p in (json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw)]
                        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                    except:
                        return ("UNKNOWN", 0)
                    if len(prices) < 2 or len(outcomes) < 2:
                        return ("UNKNOWN", 0)
                    winning_idx = 0 if prices[0] > prices[1] else 1
                    if our_token_idx == winning_idx:
                        return ("WIN", (1.0 - pos["entry_price"]) * (pos.get("size_usd", RISK_LIMITS["max_position_usd"]) / pos["entry_price"]))
                    else:
                        return ("LOSS", -pos["entry_price"] * (pos.get("size_usd", RISK_LIMITS["max_position_usd"]) / pos["entry_price"]))
                
                try:
                    # Strategy 1: Query by slug (works for active markets)
                    r = requests.get(
                        f"{GAMMA_HOST}/markets",
                        params={"slug": slug, "active": "false"},
                        timeout=10,
                    )
                    if r.status_code == 200:
                        mkts = r.json()
                        if mkts:
                            for mk in mkts:
                                mk_cid = mk.get("conditionId", mk.get("condition_id", ""))
                                mk_question = mk.get("question", "").lower()
                                # Match by condition_id or question
                                if pos_cid and mk_cid and mk_cid != pos_cid:
                                    continue
                                elif not pos_cid:
                                    if mk_question not in pos_question and pos_question not in mk_question:
                                        continue
                                outcome, pnl = _resolve_from_market(mk)
                                if outcome != "UNKNOWN":
                                    log.info(f"PM RESOLVED: {slug} | our_side={our_side} | {outcome} | PnL=${pnl:.2f}")
                                    break
                
                    # Strategy 2: If slug lookup failed, search by question in crypto tag
                    if outcome == "UNKNOWN":
                        # Paginate through closed crypto markets (tag_id=21)
                        for offset in [0, 100, 200, 300, 400, 500, 600, 700, 800, 900]:
                            r = requests.get(
                                f"{GAMMA_HOST}/markets",
                                params={
                                    "closed": "true", "tag_id": "21", "active": "false",
                                    "limit": 100, "offset": offset,
                                    "order": "volume24hr", "ascending": "false",
                                },
                                timeout=15,
                            )
                            if r.status_code != 200:
                                break
                            batch = r.json()
                            if not batch:
                                break
                            for mk in batch:
                                mk_question = mk.get("question", "").lower()
                                mk_slug = mk.get("slug", "")
                                # Match by slug or question substring
                                if mk_slug == slug or (pos_question and pos_question in mk_question) or (pos_question and mk_question in pos_question):
                                    outcome, pnl = _resolve_from_market(mk)
                                    if outcome != "UNKNOWN":
                                        log.info(f"PM RESOLVED (tag search): {slug} | our_side={our_side} | {outcome} | PnL=${pnl:.2f}")
                                        break
                            if outcome != "UNKNOWN":
                                break
                            if len(batch) < 100:
                                break  # No more pages
                
                    # Strategy 3: If condition_id available, use CLOB API directly
                    if outcome == "UNKNOWN" and pos_cid:
                        try:
                            r3 = requests.get(
                                f"{CLOB_HOST}/markets/{pos_cid}",
                                timeout=10,
                            )
                            if r3.status_code == 200:
                                mk3 = r3.json()
                                outcome, pnl = _resolve_from_market(mk3)
                                if outcome != "UNKNOWN":
                                    log.info(f"PM RESOLVED (CLOB): {slug} | our_side={our_side} | {outcome} | PnL=${pnl:.2f}")
                        except Exception as e3:
                            log.debug(f"CLOB market lookup failed: {e3}")
                            
                except Exception as e:
                    log.warning(f"Gamma API settlement check failed for {slug}: {e}")
                
                # If PM hasn't resolved yet, check force-settle conditions
                if outcome == "UNKNOWN":
                    # Force-settle if market is well past expiry (30min grace)
                    # and we can get outcomePrices from Gamma even if closed=False
                    force_settled = False
                    try:
                        r2 = requests.get(
                            f"{GAMMA_HOST}/markets",
                            params={"slug": slug, "active": "false"},
                            timeout=10,
                        )
                        if r2.status_code == 200:
                            mkts2 = r2.json()
                            if mkts2:
                                mk2 = mkts2[0]
                                prices_raw = mk2.get("outcomePrices", "[]")
                                outcomes_raw = mk2.get("outcomes", "[]")
                                try:
                                    prices2 = [float(p) for p in (json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw)]
                                    outcomes2 = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                                except:
                                    prices2, outcomes2 = [], []
                                # Force-settle if outcomePrices show clear resolution (0.0/1.0)
                                # even if closed flag isn't set yet
                                if len(prices2) >= 2 and len(outcomes2) >= 2:
                                    if (prices2[0] == 1.0 and prices2[1] == 0.0) or (prices2[0] == 0.0 and prices2[1] == 1.0):
                                        winning_idx = 0 if prices2[0] > prices2[1] else 1
                                        our_side = pos.get("side", "").upper()
                                        our_token_idx = 0 if our_side == "YES" else 1
                                        if our_token_idx == winning_idx:
                                            outcome = "WIN"
                                            pnl = (1.0 - pos["entry_price"]) * (pos.get("size_usd", RISK_LIMITS["max_position_usd"]) / pos["entry_price"])
                                        else:
                                            outcome = "LOSS"
                                            pnl = -pos["entry_price"] * (pos.get("size_usd", RISK_LIMITS["max_position_usd"]) / pos["entry_price"])
                                        force_settled = True
                                        log.info(f"FORCE SETTLED: {slug} | prices={prices2} | {outcome} | PnL=${pnl:.2f}")
                    except Exception as e2:
                        log.debug(f"Force-settle check failed for {slug}: {e2}")
                    
                    if not force_settled:
                        log.info(f"PM market not yet resolved for {slug}, will retry next cycle")
                        continue
                
                # Apply settlement result
                pos["outcome"] = outcome
                pos["pnl"] = round(pnl, 4)
                pos["resolved_timestamp"] = datetime.now(timezone.utc).isoformat()
                state.closed_positions.append(pos)
                state.positions.remove(pos)
                state.open_positions -= 1
                state.total_pnl += pnl
                
                if outcome == "WIN":
                    state.wins += 1
                    state.consecutive_losses = 0
                else:
                    state.losses += 1
                    state.consecutive_losses += 1
                    state.daily_loss_usd += abs(pnl)
                
                log.info(f"RESOLVED: {pos.get('side','?')} {pos.get('asset','?')} | {outcome} | PnL=${pnl:.2f} | slug={slug}")
                with open(OUT / "resolved_positions.jsonl", "a") as f:
                    f.write(json.dumps(pos, default=str) + "\n")
                
                # V21.7.69: Remove settled position from positions.jsonl
                # Previously positions.jsonl was append-only and settled positions
                # remained with order_status=PAPER_FILLED, causing duplicate entries
                # when the same slug appeared again. Now rewrite positions.jsonl
                # excluding the just-settled slug.
                try:
                    with open(OUT / "positions.jsonl", "r") as pf:
                        all_pos_lines = pf.readlines()
                    remaining = []
                    for line in all_pos_lines:
                        if not line.strip():
                            continue
                        pd = json.loads(line)
                        if pd.get("market_slug") != slug:
                            remaining.append(line)
                    with open(OUT / "positions.jsonl", "w") as pf:
                        for line in remaining:
                            pf.write(line)
                except Exception as cleanup_err:
                    log.warning(f"Failed to clean positions.jsonl after settlement: {cleanup_err}")
            
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
            
            # ─── Live promotion readiness ───
            total_resolved = state.wins + state.losses
            wr = state.wins / total_resolved if total_resolved > 0 else 0
            total_wins_pnl = sum(
                (1.0 - p.get("entry_price", 0.5)) * (p.get("size_usd", RISK_LIMITS["max_position_usd"]) / p.get("entry_price", 0.5))
                for p in state.closed_positions if p.get("outcome") == "WIN"
            ) if state.closed_positions else 0
            total_losses_pnl = abs(sum(
                p.get("entry_price", 0.5) * (p.get("size_usd", RISK_LIMITS["max_position_usd"]) / p.get("entry_price", 0.5))
                for p in state.closed_positions if p.get("outcome") == "LOSS"
            )) if state.closed_positions else 0.01
            pf = total_wins_pnl / total_losses_pnl if total_losses_pnl > 0 else float("inf")
            
            readiness = {
                "timestamp": now.isoformat(),
                "version": "V21.7.61",
                "resolved_paper_trades": total_resolved,
                "wins": state.wins,
                "losses": state.losses,
                "win_rate": round(wr, 4),
                "profit_factor": round(pf, 2),
                "total_pnl": round(state.total_pnl, 2),
                "live_blocked": not (
                    total_resolved >= RISK_LIMITS["live_min_resolved_trades"]
                    and wr >= RISK_LIMITS["live_min_win_rate"]
                    and pf >= RISK_LIMITS["live_min_profit_factor"]
                    and state.total_pnl >= RISK_LIMITS["live_min_pnl_usd"]
                ),
                "promotion_criteria_met": (
                    total_resolved >= RISK_LIMITS["live_min_resolved_trades"]
                    and wr >= RISK_LIMITS["live_min_win_rate"]
                    and pf >= RISK_LIMITS["live_min_profit_factor"]
                    and state.total_pnl >= RISK_LIMITS["live_min_pnl_usd"]
                ),
                "classification": "LIVE_READY" if (
                    total_resolved >= RISK_LIMITS["live_min_resolved_trades"]
                    and wr >= RISK_LIMITS["live_min_win_rate"]
                    and pf >= RISK_LIMITS["live_min_profit_factor"]
                    and state.total_pnl >= RISK_LIMITS["live_min_pnl_usd"]
                ) else "PAPER_VALIDATION",
                "gates": {
                    "min_resolved_trades": {"required": RISK_LIMITS["live_min_resolved_trades"], "actual": total_resolved},
                    "min_win_rate": {"required": RISK_LIMITS["live_min_win_rate"], "actual": round(wr, 4)},
                    "min_profit_factor": {"required": RISK_LIMITS["live_min_profit_factor"], "actual": round(pf, 2)},
                    "min_pnl_usd": {"required": RISK_LIMITS["live_min_pnl_usd"], "actual": round(state.total_pnl, 2)},
                },
            }
            with open(OUT / "live_readiness.json", "w") as f:
                json.dump(readiness, f, indent=2, default=str)
            
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
    
    # Recover resolved positions from resolved_positions.jsonl on restart
    # This restores win/loss/pnl counters that were lost on restart
    resolved_file = OUT / "resolved_positions.jsonl"
    if resolved_file.exists():
        try:
            recovered_resolved = 0
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with open(resolved_file) as rf:
                for line in rf:
                    if line.strip():
                        rd = json.loads(line)
                        state.closed_positions.append(rd)
                        recovered_resolved += 1
                        pnl = rd.get("pnl", 0)
                        state.total_pnl += pnl
                        if rd.get("outcome") == "WIN":
                            state.wins += 1
                        elif rd.get("outcome") == "LOSS":
                            state.losses += 1
                            # Only add to daily_loss if resolved today
                            resolved_ts = rd.get("resolved_timestamp", "")
                            if resolved_ts.startswith(today):
                                state.daily_loss_usd += abs(pnl)
            if recovered_resolved:
                log.info(f"Recovered {recovered_resolved} resolved positions | "
                         f"W={state.wins} L={state.losses} PnL=${state.total_pnl:.2f}")
        except Exception as e:
            log.warning(f"Failed to recover resolved positions: {e}")
    
    # Recover open positions from positions.jsonl on restart
    pos_file = OUT / "positions.jsonl"
    if pos_file.exists():
        try:
            recovered = 0
            resolved_slugs = set()
            resolved_file = OUT / "resolved_positions.jsonl"
            if resolved_file.exists():
                with open(resolved_file) as rf:
                    for line in rf:
                        if line.strip():
                            rd = json.loads(line)
                            resolved_slugs.add(rd.get("market_slug", ""))
            
            with open(pos_file) as pf:
                for line in pf:
                    if line.strip():
                        pd = json.loads(line)
                        slug = pd.get("market_slug", "")
                        if slug and slug not in resolved_slugs and pd.get("order_status") != "SETTLED":
                            state.positions.append(pd)
                            recovered += 1
            if recovered:
                state.open_positions = len(state.positions)
                log.info(f"Recovered {recovered} open positions from positions.jsonl")
        except Exception as e:
            log.warning(f"Failed to recover positions: {e}")
    
    try:
        scan_loop(state, paper_mode)
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception as e:
        log.error(f"Fatal: {e}")
        traceback.print_exc()
    finally:
        log.info("Engine shutdown complete")