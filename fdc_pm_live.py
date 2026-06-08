#!/usr/bin/env python3
"""
FDC Polymarket Live Execution Layer — V20.1 Pre-Live Plumbing
8 modules: wallet validation, tick cache, auth, dry-run, heartbeat, redemption, slug rotation, safety
LIVE_ENABLED=False until all plumbing gates pass.

Author: Hugh (3rd of 5)
Date: 2026-06-04
"""

import json, os, sys, time, urllib.request, hashlib, logging, re, math
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

# §MCP: Add v217_live to path for MCP bridge
sys.path.insert(0, str(Path(__file__).resolve().parent / "src" / "v217_live"))

# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

LIVE_ENABLED = False          # HARD LOCK — flip True only after plumbing report passes
PAPER_ONLY = True             # Mirror flag for backward compat — must match LIVE_ENABLED

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
CHAIN_ID = 137
POLYGON_RPC = "https://rpc-mainnet.matic.quiknode.pro"

# Contract addresses (Polygon)
USDC_E_ADDR    = "[REDACTED_USDCe]"
USDC_NATIVE_ADDR = "[REDACTED_USDC]"
CTF_EXCHANGE   = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEGRISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

ENV_FILE = Path("/mnt/c/Users/12035/father_daddy_capital/.env")

# MICRO_VALIDATION hard stops
MAX_DAILY_LOSS = 10.0      # $10
MAX_WEEKLY_LOSS = 30.0      # $30
MAX_CONCURRENT = 1
MIN_BANKROLL = 5.0          # halt below $5
FIXED_SIZE = 2.0            # $2 per trade
BUCKET_RANGE = (0.50, 0.60) # BTC only
MAX_TRADES = 30             # hard stop after 30 trades

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fdc_live")

def _load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

env = _load_env()
PK = env.get("PM_WALLET_PRIVATE_KEY", "")
FUNDER = env.get("PM_WALLET_ADDRESS", "[REDACTED_EOA]")


# ══════════════════════════════════════════════════════════════════════════════
# Module 1: Wallet / Collateral Validation
# ══════════════════════════════════════════════════════════════════════════════

def _rpc_call(method: str, params: list) -> dict:
    """Raw JSON-RPC call to Polygon."""
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(POLYGON_RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _erc20_balance(token_addr: str, wallet: str) -> float:
    """Read ERC20 balanceOf via eth_call. MCP onchain first, raw RPC fallback."""
    # §MCP: Try Bankless onchain MCP for richer data
    try:
        from mcp_client_bridge import _mcp_crypto, _init_mcp_bridge
        if _mcp_crypto is None:
            _init_mcp_bridge()
        if _mcp_crypto is not None:
            import asyncio
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(
                _mcp_crypto.call("onchain", "get_token_balances_on_network",
                                 {"address": wallet, "network": "polygon"})
            )
            loop.close()
            if isinstance(result, dict):
                # Try to extract USDC balance from result
                for token in result.get("tokens", result.get("balances", [])):
                    if isinstance(token, dict) and token.get("address", "").lower() == token_addr.lower():
                        return float(token.get("balance", 0)) / 1e6
    except Exception:
        pass  # Fall through to raw RPC
    
    # Fallback: raw RPC eth_call
    data = "0x70a08231" + wallet[2:].lower().zfill(64)
    resp = _rpc_call("eth_call", [{"to": token_addr, "data": data}, "latest"])
    raw = resp.get("result", "0x0")
    if raw in ("0x", "0x0", ""):
        return 0.0
    return int(raw, 16) / 1e6  # USDC has 6 decimals

def _erc20_allowance(token_addr: str, wallet: str, spender: str) -> float:
    """Read ERC20 allowance via eth_call."""
    # allowance(address owner, address spender) = 0xdd62ed3e
    data = "0xdd62ed3e" + wallet[2:].lower().zfill(64) + spender[2:].lower().zfill(64)
    resp = _rpc_call("eth_call", [{"to": token_addr, "data": data}, "latest"])
    raw = resp.get("result", "0x0")
    if raw in ("0x", "0x0", ""):
        return 0.0
    return int(raw, 16) / 1e6

def check_wallet() -> dict:
    """Full wallet + collateral + allowance check."""
    result = {"address": FUNDER, "matic": 0, "usdc_native": 0, "usdc_bridged": 0,
              "usdc_total": 0, "allowance_exchange": 0, "allowance_negrisk": 0,
              "funded": False, "collateral_ready": False}

    # MATIC balance
    try:
        resp = _rpc_call("eth_getBalance", [FUNDER, "latest"])
        raw = resp.get("result", "0x0")
        result["matic"] = round(int(raw, 16) / 1e18, 4) if raw not in ("0x", "0x0", "") else 0.0
    except Exception as e:
        result["matic_error"] = str(e)

    # USDC balances
    for label, addr in [("native", USDC_NATIVE_ADDR), ("bridged", USDC_E_ADDR)]:
        try:
            bal = _erc20_balance(addr, FUNDER)
            result[f"usdc_{label}"] = round(bal, 2)
            result["usdc_total"] += bal
        except Exception as e:
            result[f"usdc_{label}_error"] = str(e)
    result["usdc_total"] = round(result["usdc_total"], 2)

    # Allowances — USDC (native 0x...3359) to CTF Exchange and NegRisk Exchange
    # Polymarket uses native USDC ([REDACTED_USDC]) as collateral
    try:
        result["allowance_exchange"] = round(_erc20_allowance(USDC_NATIVE_ADDR, FUNDER, CTF_EXCHANGE), 2)
    except Exception as e:
        result["allowance_exchange_error"] = str(e)
    try:
        result["allowance_negrisk"] = round(_erc20_allowance(USDC_NATIVE_ADDR, FUNDER, NEGRISK_EXCHANGE), 2)
    except Exception as e:
        result["allowance_negrisk_error"] = str(e)

    # Funded = has MATIC for gas + has USDC
    result["usdc"] = round(result["usdc_total"], 2)  # backward compat

    result["funded"] = result["matic"] > 0.1 and result["usdc_total"] > 1

    # Collateral ready = funded + allowance > 0 to BOTH exchanges
    # BTC up/down markets use negRisk=True, need NegRisk allowance
    result["collateral_ready"] = (
        result["funded"] and
        result["usdc_total"] >= 10 and
        result["allowance_exchange"] > 0 and
        result["allowance_negrisk"] > 0
    )

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Module 2: Tick Size + NegRisk Cache
# ══════════════════════════════════════════════════════════════════════════════

_tick_cache: Dict[str, str] = {}   # token_id → tick_size string ("0.01")
_neg_risk_cache: Dict[str, bool] = {}  # token_id → bool
_cache_ts: Dict[str, float] = {}   # token_id → timestamp

TICK_CACHE_TTL = 3600  # 1 hour

def get_tick_size(token_id: str) -> str:
    """Get tick size for a token, caching results."""
    now = time.time()
    if token_id in _tick_cache and (now - _cache_ts.get(token_id, 0)) < TICK_CACHE_TTL:
        return _tick_cache[token_id]

    try:
        from py_clob_client.client import ClobClient
        c = ClobClient(CLOB_URL, chain_id=CHAIN_ID)
        ts = c.get_tick_size(token_id)
        _tick_cache[token_id] = ts
        _cache_ts[token_id] = now
        return ts
    except Exception as e:
        log.warning(f"tick_size fetch failed for {token_id[:16]}...: {e}")
        return "0.01"  # safe default for BTC up/down

def get_neg_risk(token_id: str) -> bool:
    """Get negRisk flag for a token, caching results."""
    now = time.time()
    if token_id in _neg_risk_cache and (now - _cache_ts.get(token_id, 0)) < TICK_CACHE_TTL:
        return _neg_risk_cache[token_id]

    try:
        from py_clob_client.client import ClobClient
        c = ClobClient(CLOB_URL, chain_id=CHAIN_ID)
        nr = c.get_neg_risk(token_id)
        _neg_risk_cache[token_id] = nr
        _cache_ts[token_id] = now
        return nr
    except Exception as e:
        log.warning(f"neg_risk fetch failed for {token_id[:16]}...: {e}")
        return False  # safe default

def round_to_tick(price: float, tick_size: str) -> float:
    """Round price to nearest tick_size increment."""
    ts = float(tick_size)
    rounded = round(price / ts) * ts
    decimals = len(tick_size.rstrip('0').split('.')[-1]) if '.' in tick_size else 0
    return round(rounded, decimals)

def validate_price(price: float, tick_size: str) -> bool:
    """Verify price conforms to tick_size."""
    ts = float(tick_size)
    return abs(price - round_to_tick(price, tick_size)) < 1e-9


# ══════════════════════════════════════════════════════════════════════════════
# Module 3: Auth — Derive-First, No Repeated Startup Spam
# ══════════════════════════════════════════════════════════════════════════════

_cached_creds: Optional[dict] = None
_clob_client = None

def derive_api_credentials() -> Optional[dict]:
    """Derive CLOB API credentials from private key. Derive-first only."""
    global _cached_creds

    if _cached_creds:
        return _cached_creds

    if not PK:
        return {"error": "No PK in env", "ready": False}

    try:
        from eth_account import Account
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        acct = Account.from_key(PK)
        log.info(f"Auth: derived wallet {acct.address}")

        temp = ClobClient(CLOB_URL, key=PK, chain_id=CHAIN_ID)
        creds = temp.create_or_derive_api_creds()

        _cached_creds = {
            "api_key": creds.api_key,
            "secret": creds.api_secret,
            "passphrase": creds.api_passphrase,
            "wallet": acct.address,
            "mode": "LIVE",
        }
        log.info("Auth: credentials derived successfully")
        return _cached_creds

    except ImportError as e:
        return {"error": f"Missing dependency: {e}", "ready": False}
    except Exception as e:
        log.error(f"Auth failed: {e}")
        return {"error": str(e), "ready": False}

def get_clob_client():
    """Get or create authenticated CLOB client. Auth failure blocks live mode."""
    global _clob_client

    if _clob_client:
        return _clob_client

    if not LIVE_ENABLED:
        return None  # Paper mode

    creds = derive_api_credentials()
    if not creds or "error" in creds:
        log.error(f"Cannot create CLOB client: {creds}")
        return None

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    api_creds = ApiCreds(
        api_key=creds["api_key"],
        api_secret=creds["secret"],
        api_passphrase=creds["passphrase"],
    )
    _clob_client = ClobClient(
        CLOB_URL, key=PK, chain_id=CHAIN_ID,
        creds=api_creds, signature_type=2, funder=FUNDER,
    )
    return _clob_client


# ══════════════════════════════════════════════════════════════════════════════
# Module 4: Live Order Dry-Run Builder
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class OrderSpec:
    """Validated order specification ready for submission."""
    token_id: str
    side: str           # "BUY" or "SELL"
    price: float
    size: float
    tick_size: str
    neg_risk: bool
    rounded_price: float = 0.0
    price_conforms: bool = False
    wallet_usdc: float = 0.0
    allowance: float = 0.0
    cost_estimate: float = 0.0
    valid: bool = False
    errors: list = field(default_factory=list)

    def __post_init__(self):
        self.rounded_price = round_to_tick(self.price, self.tick_size)
        self.price_conforms = validate_price(self.price, self.tick_size)
        self.cost_estimate = self.rounded_price * self.size

        # Validate
        if self.side not in ("BUY", "SELL"):
            self.errors.append(f"Invalid side: {self.side}")
        if not self.price_conforms:
            self.errors.append(f"Price {self.price} does not conform to tick_size {self.tick_size}")
        if self.size <= 0:
            self.errors.append(f"Size must be > 0, got {self.size}")
        if self.price <= 0 or self.price >= 1:
            self.errors.append(f"Price must be in (0, 1), got {self.price}")
        if self.side == "BUY" and self.cost_estimate > self.wallet_usdc:
            self.errors.append(f"Cost ${self.cost_estimate:.2f} exceeds wallet USDC ${self.wallet_usdc:.2f}")
        if self.side == "BUY" and self.allowance < self.cost_estimate:
            self.errors.append(f"Allowance ${self.allowance:.2f} insufficient for cost ${self.cost_estimate:.2f}")
        self.valid = len(self.errors) == 0


def build_dry_run_order(token_id: str, side: str, price: float, size: float,
                        wallet_usdc: float = None, allowance: float = None) -> OrderSpec:
    """Build and validate an order WITHOUT submitting it."""
    ts = get_tick_size(token_id)
    nr = get_neg_risk(token_id)

    if wallet_usdc is None or allowance is None:
        w = check_wallet()
        wallet_usdc = w.get("usdc_total", 0)
        allowance = w.get("allowance_exchange", 0) if not nr else w.get("allowance_negrisk", 0)

    spec = OrderSpec(
        token_id=token_id,
        side=side.upper(),
        price=price,
        size=size,
        tick_size=ts,
        neg_risk=nr,
        wallet_usdc=wallet_usdc,
        allowance=allowance,
    )
    return spec


# ══════════════════════════════════════════════════════════════════════════════
# Module 5: Heartbeat / Order-State Tracking
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TrackedOrder:
    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    status: str = "live"      # live | matched | delayed | unmatched | failed | cancelled
    created_at: float = field(default_factory=time.time)
    last_heartbeat: float = 0.0
    heartbeat_id: Optional[str] = None

ORDER_STATES = {"live", "matched", "delayed", "unmatched", "failed", "cancelled"}
TERMINAL_STATES = {"matched", "failed", "cancelled"}

_open_orders: Dict[str, TrackedOrder] = {}  # order_id → TrackedOrder
_last_order_ts: Dict[str, float] = {}         # token_id+side → timestamp of last order

def submit_tracked_order(spec: OrderSpec) -> dict:
    """Submit order with dedup guard and state tracking."""
    # Dedup check — refuse if same token+side has an open (non-terminal) order
    dedup_key = f"{spec.token_id[:16]}_{spec.side}"
    now = time.time()
    last_ts = _last_order_ts.get(dedup_key, 0)
    existing = _open_orders.get(dedup_key)
    if existing and existing.status not in TERMINAL_STATES:
        return {"error": f"DUPLICATE_ORDER: {spec.side} order already pending for {spec.token_id[:16]}... (status={existing.status})",
                "dedup_key": dedup_key}

    if not LIVE_ENABLED:
        # Paper simulation — order is immediately "matched" but still tracked for dedup
        book = read_orderbook(spec.token_id)
        mid = 0.5
        if book and "bids" in book and book["bids"] and "asks" in book and book["asks"]:
            best_bid = max(book["bids"].keys()) if book["bids"] else 0.5
            best_ask = min(book["asks"].keys()) if book["asks"] else 0.5
            mid = (best_bid + best_ask) / 2

        order_id = f"paper_{int(time.time()*1000)}"
        _open_orders[dedup_key] = TrackedOrder(
            order_id=order_id, token_id=spec.token_id, side=spec.side,
            price=spec.rounded_price, size=spec.size, status="matched",
        )
        _last_order_ts[dedup_key] = now

        return {
            "order_id": order_id, "status": "SIMULATED", "mode": "PAPER",
            "price": round(mid, 4), "size": spec.size, "side": spec.side,
            "token_id": spec.token_id, "cost": round(mid * spec.size, 4),
        }

    # Live submission
    client = get_clob_client()
    if not client:
        return {"error": "CLOB client not available — auth failure blocks live mode"}

    from py_clob_client.clob_types import OrderArgs, OrderType

    order_args = OrderArgs(
        token_id=spec.token_id, price=spec.rounded_price,
        size=spec.size, side=spec.side,
    )
    options = {"tick_size": spec.tick_size, "neg_risk": spec.neg_risk}

    try:
        resp = client.create_and_post_order(order_args, options=options, order_type=OrderType.GTC)
        order_id = resp.get("orderID", resp.get("id", "unknown"))
        order = TrackedOrder(
            order_id=order_id, token_id=spec.token_id, side=spec.side,
            price=spec.rounded_price, size=spec.size, status="live",
        )
        _open_orders[dedup_key] = order
        _last_order_ts[dedup_key] = now
        return {"order_id": order_id, "status": resp.get("status", "live"), "mode": "LIVE", **resp}
    except Exception as e:
        log.error(f"Order submission failed: {e}")
        return {"error": str(e)}

def check_order_state(order_id: str) -> Optional[str]:
    """Poll order state from CLOB. Returns status string."""
    if not LIVE_ENABLED:
        order = next((o for o in _open_orders.values() if o.order_id == order_id), None)
        return order.status if order else None

    client = get_clob_client()
    if not client:
        return None

    try:
        resp = client.get_order(order_id)
        return resp.get("status", "unknown") if isinstance(resp, dict) else str(resp)
    except Exception as e:
        log.warning(f"Order state check failed for {order_id}: {e}")
        return None

def send_heartbeat(client) -> bool:
    """Send heartbeat to keep CLOB session alive. Returns success."""
    try:
        client.post_heartbeat(None)
        return True
    except Exception as e:
        log.warning(f"Heartbeat failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Module 6: Redemption Path
# ══════════════════════════════════════════════════════════════════════════════

def detect_redeemable_positions(condition_id: str = None) -> list:
    """Scan wallet for winning conditional tokens that can be redeemed."""
    result = []

    if not LIVE_ENABLED:
        log.info("Redemption scan: PAPER mode — checking conditional token balances")
        # In paper mode, read conditional token balance from RPC
        # For now, return empty — redemption testing requires live mode
        return result

    client = get_clob_client()
    if not client:
        return [{"error": "No CLOB client for redemption scan"}]

    try:
        # Get balance/allowance via SDK
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        # Check COLLATERAL balance
        bal_resp = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        log.info(f"Collateral balance: {bal_resp}")

        # Check CONDITIONAL token balances if we have condition_ids
        # This requires knowing which condition_ids we traded
        # For now, we'll check per-condition-id in redeem_winning_position()
        return result

    except Exception as e:
        log.error(f"Redemption scan failed: {e}")
        return [{"error": str(e)}]


def redeem_winning_position(condition_id: str, manual_override: bool = False) -> dict:
    """Redeem a winning position. Requires manual flag — DO NOT auto-redeem."""
    if not manual_override:
        return {"error": "Redemption requires manual_override=True", "status": "BLOCKED"}

    if not LIVE_ENABLED:
        return {"error": "Redemption requires LIVE_ENABLED=True", "status": "BLOCKED"}

    client = get_clob_client()
    if not client:
        return {"error": "No CLOB client", "status": "ERROR"}

    # Read winning token balance BEFORE
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))

    ct_contract = w3.eth.contract(
        address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
        # Minimal ABI for balanceOf
    )
    # This is a placeholder — actual redemption requires:
    # 1. CT Exchange merge or redeem call
    # 2. Proper ABI encoding
    # 3. Gas estimation
    log.info(f"Redemption of {condition_id}: LIVE mode required, manual override confirmed")

    return {
        "status": "REQUIRES_ONCHAIN_TX",
        "condition_id": condition_id,
        "note": "Use metamask or web3.py to call CTFExchange.redeemPositions()",
        "contract": CTF_EXCHANGE if not get_neg_risk(condition_id) else NEGRISK_EXCHANGE,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Module 7: Slug Parser / Auto-Rotation
# ══════════════════════════════════════════════════════════════════════════════

SLUG_PATTERN = re.compile(r'^(btc|eth|sol|xrp)-updown-(\d+m)-(\d+)$')

def parse_slug(slug: str) -> Optional[dict]:
    """Parse 'btc-updown-5m-1780620000' into structured data."""
    m = SLUG_PATTERN.match(slug)
    if not m:
        return None
    asset, interval, ts = m.groups()
    return {
        "asset": asset.upper(),
        "interval": interval,
        "expiry_ts": int(ts),
        "interval_sec": int(interval[:-1]) * 60,
        "slug": slug,
    }

def compute_next_slug(slug: str) -> Optional[str]:
    """Compute next slug in sequence for auto-rotation."""
    parsed = parse_slug(slug)
    if not parsed:
        return None
    next_ts = parsed["expiry_ts"] + parsed["interval_sec"]
    return f"{parsed['asset'].lower()}-updown-{parsed['interval']}-{next_ts}"

def discover_active_contract(asset: str = "BTC", interval: str = "5m") -> Optional[dict]:
    """Discover the current active contract via Gamma API exact slug lookup."""
    try:
        # Compute the current or next expiry timestamp
        interval_sec = int(interval[:-1]) * 60  # e.g., "5m" -> 300
        now_ts = int(time.time())
        # Round down to nearest interval boundary
        current_boundary = (now_ts // interval_sec) * interval_sec
        # Try current and next 3 windows
        for offset in range(4):
            ts = current_boundary + offset * interval_sec
            slug = f"{asset.lower()}-updown-{interval}-{ts}"
            url = f"{GAMMA_URL}/markets?active=true&closed=false&limit=1&slug={slug}"
            req = urllib.request.Request(url, headers={"User-Agent": "fdc/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    markets = json.loads(r.read())
                if markets:
                    m = markets[0]
                    cid = m.get("conditionId", "")
                    tokens_str = m.get("clobTokenIds", "[]")
                    tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
                    neg_risk = m.get("negRisk", False)
                    question = m.get("question", "")
                    expires_in = ts + interval_sec - now_ts

                    # Enrich with tick_size and neg_risk from CLOB
                    token_data = []
                    for tid in tokens:
                        token_data.append({
                            "token_id": tid,
                            "tick_size": get_tick_size(tid),
                            "neg_risk": get_neg_risk(tid),
                        })

                    return {
                        "slug": slug,
                        "conditionId": cid,
                        "question": question,
                        "negRisk": neg_risk,
                        "tokens": token_data,
                        "expires_in_sec": max(0, expires_in),
                    }
            except Exception:
                continue

        # Fallback: discover from existing engine
        try:
            from importlib import import_module
            pm_engine = import_module("pm_engine_v19_8")
            if hasattr(pm_engine, "discover_contracts_multi"):
                contracts = pm_engine.discover_contracts_multi(asset_key=asset.upper())
                if asset in contracts and contracts[asset]:
                    c = contracts[asset][0]
                    # Enrich tokens
                    for key in ["up_token_id", "down_token_id"]:
                        tid = c.get(key, "")
                        if tid:
                            c[f"{key}_tick_size"] = get_tick_size(tid)
                            c[f"{key}_neg_risk"] = get_neg_risk(tid)
                    return c
        except Exception as e:
            log.warning(f"Engine fallback failed: {e}")

    except Exception as e:
        log.error(f"Contract discovery failed: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# General: Orderbook Read
# ══════════════════════════════════════════════════════════════════════════════

def read_orderbook(token_id: str) -> dict:
    """Read orderbook. No auth needed."""
    try:
        url = f"{CLOB_URL}/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return {
            "bids": {float(e["price"]): float(e["size"]) for e in data.get("bids", [])},
            "asks": {float(e["price"]): float(e["size"]) for e in data.get("asks", [])},
            "tick_size": data.get("tick_size", "0.01"),
            "min_size": float(data.get("min_order_size", 5)),
        }
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# Module 8: Kill Switch + Safety
# ══════════════════════════════════════════════════════════════════════════════

class KillSwitch:
    """Emergency circuit breaker with daily/weekly loss limits."""

    def __init__(self, max_daily_loss: float = MAX_DAILY_LOSS,
                 max_weekly_loss: float = MAX_WEEKLY_LOSS,
                 max_concurrent: int = MAX_CONCURRENT):
        self.max_daily_loss = max_daily_loss
        self.max_weekly_loss = max_weekly_loss
        self.max_concurrent = max_concurrent
        self.daily_pnl: Dict[str, float] = {}
        self.weekly_pnl: float = 0.0
        self.trade_count: int = 0
        self.halted = False
        self.halt_reason = ""
        self.open_positions: int = 0
        self.error_counts: Dict[str, int] = {}  # error_type → count

    def check(self, capital: float, today: str, daily_pnl: float) -> tuple:
        """Check safety limits. Returns (allowed: bool, reason: str)."""
        if self.halted:
            return False, f"PERMANENTLY HALTED: {self.halt_reason}"

        # Daily loss check
        if daily_pnl < -self.max_daily_loss:
            self.halted = True
            self.halt_reason = f"Daily loss ${daily_pnl:+.2f} exceeds ${self.max_daily_loss} limit"
            return False, self.halt_reason

        # Weekly loss check
        if self.weekly_pnl < -self.max_weekly_loss:
            self.halted = True
            self.halt_reason = f"Weekly loss ${self.weekly_pnl:+.2f} exceeds ${self.max_weekly_loss} limit"
            return False, self.halt_reason

        # Trade count limit
        if self.trade_count >= MAX_TRADES:
            self.halted = True
            self.halt_reason = f"Trade count {self.trade_count} >= {MAX_TRADES} limit"
            return False, self.halt_reason

        # Concurrent positions
        if self.open_positions >= self.max_concurrent:
            return False, f"Max concurrent positions ({self.max_concurrent}) reached"

        # Minimum capital
        if capital < MIN_BANKROLL:
            self.halted = True
            self.halt_reason = f"Capital ${capital:.2f} below minimum ${MIN_BANKROLL}"
            return False, self.halt_reason

        # Account for settlement / accounting errors → forced shutdown
        for etype, count in self.error_counts.items():
            if count >= 3:
                self.halted = True
                self.halt_reason = f"Forced shutdown: {etype} error count {count} >= 3"
                return False, self.halt_reason

        return True, "OK"

    def record_error(self, error_type: str):
        """Track errors. 3 same-type errors = forced shutdown."""
        self.error_counts[error_type] = self.error_counts.get(error_type, 0) + 1

    def record_trade(self, pnl: float = 0):
        self.trade_count += 1
        self.weekly_pnl += pnl

    def reset(self):
        """Reset kill switch (requires manual intervention)."""
        self.halted = False
        self.halt_reason = ""
        self.error_counts = {}


# ══════════════════════════════════════════════════════════════════════════════
# PMLiveClient — Unified Interface
# ══════════════════════════════════════════════════════════════════════════════

class PMLiveClient:
    """Polymarket CLOB client with full plumbing."""

    def __init__(self):
        self.mode = "PAPER" if not LIVE_ENABLED else "LIVE"
        self._clob = None
        self._creds = None
        self.kill_switch = KillSwitch()

    def init(self) -> dict:
        """Initialize auth and CLOB client. Returns status."""
        if not LIVE_ENABLED:
            self._creds = {"mode": "PAPER"}
            return {"ready": True, "mode": "PAPER", "wallet": FUNDER}

        creds = derive_api_credentials()
        if not creds or "error" in creds:
            log.error(f"Auth failed: {creds}")
            return {"error": creds.get("error", "No credentials"), "ready": False}

        self._creds = creds
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        api_creds = ApiCreds(
            api_key=creds["api_key"],
            api_secret=creds["secret"],
            api_passphrase=creds["passphrase"],
        )
        self._clob = ClobClient(
            CLOB_URL, key=PK, chain_id=CHAIN_ID,
            creds=api_creds, signature_type=2, funder=FUNDER,
        )
        return {"ready": True, "mode": "LIVE", "wallet": FUNDER}

    def get_orderbook(self, token_id: str) -> dict:
        return read_orderbook(token_id)

    def place_order(self, token_id: str, side: str, price: float, size: float,
                    tick_size: str = None, neg_risk: bool = None) -> dict:
        """Place order with full validation."""
        # Resolve tick_size and neg_risk if not provided
        if tick_size is None:
            tick_size = get_tick_size(token_id)
        if neg_risk is None:
            neg_risk = get_neg_risk(token_id)

        # Build validated order spec
        w = check_wallet()
        wallet_usdc = w.get("usdc_total", 0)
        allowance = w.get("allowance_negrisk" if neg_risk else "allowance_exchange", 0)

        spec = OrderSpec(
            token_id=token_id, side=side.upper(), price=price, size=size,
            tick_size=tick_size, neg_risk=neg_risk,
            wallet_usdc=wallet_usdc, allowance=allowance,
        )

        if not spec.valid:
            return {"error": "Order validation failed", "errors": spec.errors, "spec": str(spec)}

        # Kill switch check
        ok, reason = self.kill_switch.check(wallet_usdc, datetime.now().strftime("%Y-%m-%d"), 0)
        if not ok:
            return {"error": f"Kill switch: {reason}"}

        # Submit
        return submit_tracked_order(spec)

    def cancel_all(self, token_id: str) -> dict:
        if not LIVE_ENABLED or not self._clob:
            return {"cancelled": True, "simulated": True}
        try:
            self._clob.cancel_all_asset(token_id)
            return {"cancelled": True}
        except Exception as e:
            return {"error": str(e)}

    def get_balance(self) -> dict:
        if not LIVE_ENABLED:
            return {"mode": "PAPER", "usdc": 250.0, "matic": 1.0}
        return check_wallet()


# ══════════════════════════════════════════════════════════════════════════════
# Quick Self-Test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== FDC V20.1 Pre-Live Plumbing Self-Test ===\n")

    # 1. Wallet
    print("--- Gate 1: Wallet / Collateral ---")
    wallet = check_wallet()
    print(f"  Address:    {wallet['address']}")
    print(f"  MATIC:      {wallet.get('matic', wallet.get('matic_error','err'))}")
    print(f"  USDC native: {wallet.get('usdc_native', 'err')}")
    print(f"  USDC bridged: {wallet.get('usdc_bridged', 'err')}")
    print(f"  USDC total: {wallet.get('usdc_total', 'err')}")
    print(f"  Allowance CTF: ${wallet.get('allowance_exchange', 'err')}")
    print(f"  Allowance NegRisk: ${wallet.get('allowance_negrisk', 'err')}")
    print(f"  Funded: {wallet['funded']}")
    print(f"  TRADABLE_COLLATERAL_READY: {wallet['collateral_ready']}")

    # 2. Tick + neg_risk cache
    print("\n--- Gate 2: Tick Size + NegRisk Cache ---")
    test_token = "53810201272415740015105366781214569611243436922063608287914417650375537878356"
    ts = get_tick_size(test_token)
    nr = get_neg_risk(test_token)
    print(f"  Test token tick_size: {ts}")
    print(f"  Test token neg_risk: {nr}")
    print(f"  Price 0.55 round: {round_to_tick(0.555, ts)}")
    print(f"  Price 0.555 conforms: {validate_price(0.555, ts)}")
    print(f"  Price 0.55 conforms: {validate_price(0.55, ts)}")

    # 3. Auth
    print("\n--- Gate 3: Auth (derive-first) ---")
    creds = derive_api_credentials()
    if "error" in creds:
        print(f"  FAILED: {creds['error']}")
    else:
        print(f"  Mode: {creds.get('mode')}")
        print(f"  Wallet: {creds.get('wallet')}")
        print(f"  Key: {creds['api_key'][:8]}...")

    # 4. Dry-run order
    print("\n--- Gate 4: Live Order Dry-Run ---")
    spec = build_dry_run_order(test_token, "BUY", 0.55, 2.0)
    print(f"  Token: {spec.token_id[:16]}...")
    print(f"  Side: {spec.side}")
    print(f"  Price: {spec.price} → rounded: {spec.rounded_price}")
    print(f"  Size: {spec.size}")
    print(f"  Tick: {spec.tick_size} | NegRisk: {spec.neg_risk}")
    print(f"  Cost estimate: ${spec.cost_estimate:.2f}")
    print(f"  Valid: {spec.valid}")
    if spec.errors:
        for e in spec.errors:
            print(f"  ERROR: {e}")

    # 5. Slug parser
    print("\n--- Gate 5: Slug Parser ---")
    test_slug = "btc-updown-5m-1780620300"
    parsed = parse_slug(test_slug)
    print(f"  Slug: {test_slug}")
    print(f"  Parsed: {parsed}")
    next_slug = compute_next_slug(test_slug)
    print(f"  Next slug: {next_slug}")

    # 6. Kill switch
    print("\n--- Gate 6: Kill Switch ---")
    ks = KillSwitch()
    ok, reason = ks.check(50.0, "2026-06-04", -5.0)
    print(f"  Normal: {'✅' if ok else '🛑'} {reason}")
    ok, reason = ks.check(50.0, "2026-06-04", -15.0)
    print(f"  Daily breach: {'✅' if ok else '🛑'} {reason}")
    ks.reset()
    ks.record_error("settlement")
    ks.record_error("settlement")
    ks.record_error("settlement")
    ok, reason = ks.check(50.0, "2026-06-04", 0)
    print(f"  3x settlement error: {'✅' if ok else '🛑'} {reason}")

    print("\n=== Self-Test Complete ===")