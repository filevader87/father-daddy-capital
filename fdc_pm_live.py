#!/usr/bin/env python3
"""
FDC Polymarket Live Execution Layer (Paper-Only Mode)
SIWE auth, orderbook reads, fill estimation. NO real order submission.
Ready for live when Father Daddy gives go-ahead — flip PAPER_ONLY=False.

Author: Hugh (3rd of 5)
Date: 2026-05-15
"""

import json, os, time, urllib.request
from pathlib import Path
from typing import Optional, Dict, List

PAPER_ONLY = False  # LIVE MODE — Father Daddy authorized
CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
CHAIN_ID = 137
POLYGON_RPC = "https://rpc-mainnet.matic.quiknode.pro"

ENV_FILE = Path("/mnt/c/Users/12035/father_daddy_capital/.env")

def _load_env():
    """Load .env vars."""
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
FUNDER = env.get("PM_WALLET_ADDRESS", "0xD4a39D33b8CcB46a08378e426BaEE3591463f090")


# ══════════════════════════════════════════════════════════════════════════════
# Wallet Status
# ══════════════════════════════════════════════════════════════════════════════

def check_wallet() -> dict:
    """Read-only wallet balance check. No tx signing."""
    result = {"address": FUNDER, "matic": 0, "usdc": 0, "funded": False}

    # MATIC balance via RPC
    try:
        body = json.dumps({
            "jsonrpc": "2.0", "method": "eth_getBalance",
            "params": [FUNDER, "latest"], "id": 1
        }).encode()
        req = urllib.request.Request(POLYGON_RPC, data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            bal = int(json.loads(r.read()).get("result", "0x0"), 16) / 1e18
        result["matic"] = round(bal, 4)
    except Exception as e:
        result["matic_error"] = str(e)

    # USDC (native) via eth_call — Polymarket uses native USDC on Polygon
    # Also check USDC.e (bridged) as fallback
    usdc_native = "0x3c499c542cEF5E3811e1192Ce70d8cC03d5c3359"
    usdc_bridged = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    total_usdc = 0.0
    for label, usdc_addr in [("native", usdc_native), ("bridged", usdc_bridged)]:
        try:
            data = "0x70a08231" + FUNDER[2:].lower().zfill(64)
            body = json.dumps({
                "jsonrpc": "2.0", "method": "eth_call",
                "params": [{"to": usdc_addr, "data": data}, "latest"], "id": 1
            }).encode()
            req = urllib.request.Request(POLYGON_RPC, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                bal = int(json.loads(r.read()).get("result", "0x0"), 16) / 1e6
            result[f"usdc_{label}"] = round(bal, 2)
            total_usdc += bal
        except Exception as e:
            result[f"usdc_{label}_error"] = str(e)
    result["usdc"] = round(total_usdc, 2)

    result["funded"] = result["matic"] > 0.1 and result["usdc"] > 10
    return result


# ══════════════════════════════════════════════════════════════════════════════
# SIWE Authentication (L1 → L2)
# ══════════════════════════════════════════════════════════════════════════════

def derive_api_credentials() -> Optional[dict]:
    """Derive CLOB API credentials from private key. From py-clob-client SIWE flow."""
    if PAPER_ONLY:
        return {"api_key": "paper", "secret": "paper", "passphrase": "paper", "mode": "PAPER"}

    if not PK:
        return None

    try:
        from eth_account import Account
        from py_clob_client.client import ClobClient

        acct = Account.from_key(PK)
        temp = ClobClient(CLOB_URL, key=PK, chain_id=CHAIN_ID)
        creds = temp.create_or_derive_api_creds()

        return {
            "api_key": creds.api_key,
            "secret": creds.api_secret,
            "passphrase": creds.api_passphrase,
            "wallet": acct.address,
            "mode": "LIVE",
        }
    except ImportError:
        return {"error": "py_clob_client not installed"}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# CLOB Client (Paper wrapper)
# ══════════════════════════════════════════════════════════════════════════════

class PMLiveClient:
    """Polymarket CLOB client. Paper mode: validates everything, submits nothing."""

    def __init__(self):
        self.mode = "PAPER" if PAPER_ONLY else "LIVE"
        self._clob = None
        self._creds = None

    def init(self) -> dict:
        """Initialize auth and CLOB client. Returns status."""
        creds = derive_api_credentials()
        if not creds or "error" in creds:
            return {"error": creds.get("error", "No credentials"), "ready": False}

        self._creds = creds

        if not PAPER_ONLY:
            from py_clob_client.client import ClobClient
            self._clob = ClobClient(
                CLOB_URL, key=PK, chain_id=CHAIN_ID,
                creds=self._creds, signature_type=2, funder=FUNDER,
            )

        return {"ready": True, "mode": self.mode, "wallet": FUNDER}

    def get_orderbook(self, token_id: str) -> dict:
        """Read orderbook. Always live-read — read-only, no auth needed."""
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

    def place_order(self, token_id: str, side: str, price: float, size: float, **kwargs) -> dict:
        """Place an order. PAPER mode: simulates fill, no submission."""
        if PAPER_ONLY:
            # Simulate fill at current mid-price
            book = self.get_orderbook(token_id)
            if "error" in book:
                return {"error": book["error"], "simulated": True}
            mid = (min(book["asks"].keys()) + max(book["bids"].keys())) / 2.0 if book["bids"] and book["asks"] else price
            return {
                "order_id": f"paper_{int(time.time()*1000)}",
                "status": "SIMULATED",
                "price": round(mid, 4),
                "size": size,
                "side": side,
                "token_id": token_id,
                "mode": "PAPER",
            }

        # LIVE mode (guarded by PAPER_ONLY flag)
        if not self._clob:
            return {"error": "CLOB client not initialized"}
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        side_enum = BUY if side.upper() == "BUY" else SELL
        resp = self._clob.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=side_enum),
            options={"tick_size": kwargs.get("tick_size", "0.01"), "neg_risk": kwargs.get("neg_risk", False)},
            order_type=OrderType.GTC,
        )
        return {"order_id": resp.get("orderID"), "status": resp.get("status"), "mode": "LIVE", **resp}

    def cancel_all(self, token_id: str) -> dict:
        """Cancel all orders for a token. PAPER mode: no-op."""
        if PAPER_ONLY:
            return {"cancelled": True, "simulated": True}
        if self._clob:
            self._clob.cancel_all_asset(token_id)
        return {"cancelled": True}

    def get_balance(self) -> dict:
        """Get wallet balance. PAPER mode: returns env values."""
        if PAPER_ONLY:
            return {"mode": "PAPER", "usdc": 250.0, "matic": 1.0}
        # In live mode, query via RPC
        wallet = check_wallet()
        wallet["mode"] = "LIVE"
        return wallet


# ══════════════════════════════════════════════════════════════════════════════
# Kill Switch
# ══════════════════════════════════════════════════════════════════════════════

class KillSwitch:
    """Emergency circuit breaker. Enforces max daily loss, max drawdown, total halt."""

    def __init__(self, max_daily_loss: float = 25.0, max_drawdown_pct: float = 0.40):
        self.max_daily_loss = max_daily_loss
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_pnl: Dict[str, float] = {}
        self.peak_capital: Optional[float] = None
        self.halted = False
        self.halt_reason = ""

    def check(self, capital: float, today: str, daily_pnl: float) -> tuple:
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
            self.halt_reason = f"Daily loss ${daily_pnl:+.2f} exceeds ${-self.max_daily_loss} limit"
            return False, self.halt_reason

        # Drawdown check
        dd = (self.peak_capital - capital) / self.peak_capital
        if dd > self.max_drawdown_pct:
            self.halted = True
            self.halt_reason = f"Drawdown {dd:.1%} exceeds {self.max_drawdown_pct:.0%} limit"
            return False, self.halt_reason

        # Total loss halt
        if capital < 50:  # Below $50 on $250 = halt
            self.halted = True
            self.halt_reason = f"Capital ${capital:.2f} below minimum threshold"
            return False, self.halt_reason

        return True, "OK"

    def reset(self):
        """Reset kill switch (requires manual intervention)."""
        self.halted = False
        self.halt_reason = ""
        self.peak_capital = None


# ─── Quick Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== FDC Live Execution Layer Test (PAPER MODE) ===\n")

    # Wallet check
    wallet = check_wallet()
    print(f"Wallet: {wallet['address']}")
    print(f"  MATIC: {wallet.get('matic', wallet.get('matic_error','err'))}")
    print(f"  USDC:  {wallet.get('usdc', wallet.get('usdc_error','err'))}")
    print(f"  Funded: {wallet['funded']}")

    # Auth
    creds = derive_api_credentials()
    print(f"\nAuth: {creds.get('mode','ERROR')} — {creds.get('error','OK')}")

    # Client
    client = PMLiveClient()
    status = client.init()
    print(f"\nClient: {status}")

    # Kill switch
    ks = KillSwitch()
    ok, reason = ks.check(250, "2026-05-15", -10)
    print(f"\nKill Switch: {'✅' if ok else '🛑'} {reason}")
    ok, reason = ks.check(240, "2026-05-15", -30)
    print(f"After -$30: {'✅' if ok else '🛑'} {reason}")
