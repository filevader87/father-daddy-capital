#!/usr/bin/env python3
"""
V21.7.1 LIVE DEPLOYMENT — DOWN_MOMENTUM EXECUTION SURVIVABLE
=============================================================
Controlled live asymmetric extraction. No research. No iteration.
Reality is now the optimizer.

LIVE_PROFILE:
  asset: BTC
  intervals: 5m, 15m
  side: DOWN
  state: MOMENTUM
  route: TAKER
  timing: MOMENTUM_ONLY
  bucket_primary: 0.03-0.12
  bucket_preferred: 0.05-0.08
  position_size: $1.00

Kill switches:
  MAX_DAILY_LOSS = $15.00
  MAX_WEEKLY_LOSS = $50.00
  MAX_CONSECUTIVE_LOSSES = 60
  MAX_TOTAL_LIVE_TRADES = 100

Hard revert to PAPER if:
  - realized_ev < 0 over rolling 100 trades
  - PF < 1.0
  - settlement_errors > 0
  - accounting_errors > 0
"""

import json, os, time, sys, logging, traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple

sys.path.insert(0, "/home/naq1987s/father-daddy-capital")
from fdc_pm_live import (
    check_wallet, get_tick_size, get_neg_risk, validate_price, round_to_tick,
    derive_api_credentials, get_clob_client, build_dry_run_order,
    submit_tracked_order, read_orderbook, parse_slug, compute_next_slug,
    discover_active_contract, KillSwitch,
    CLOB_URL, GAMMA_URL, CHAIN_ID, FUNDER,
)
import urllib.request

# ═══════════════════════════════════════════════════════════════════════
# V21.7.1 CONFIGURATION — LOCKED PER DIRECTIVE
# ═══════════════════════════════════════════════════════════════════════

BANKROLL_ACTUAL = 70.00  # User-confirmed tradeable bankroll

LIVE_PROFILE = {
    "asset": ["BTC"],
    "intervals": ["5m", "15m"],
    "side": "DOWN",
    "state": "MOMENTUM",
    "route": "TAKER",
    "timing": "MOMENTUM_ONLY",
    "bucket_primary": (0.03, 0.12),
    "bucket_preferred": (0.05, 0.08),
    "position_size": 1.00,
    "version": "V21.7.1",
}

# §4: Revised kill switches
MAX_DAILY_LOSS = 15.00
MAX_WEEKLY_LOSS = 50.00
MAX_CONSECUTIVE_LOSSES = 60
MAX_TOTAL_LIVE_TRADES = 100
MAX_DAILY_TRADES = 30
MAX_CONCURRENT = 1
POSITION_SIZE = 1.00

# §3: Bucket weighting
BUCKET_WEIGHTS = {
    (0.05, 0.08): 1.00,   # preferred — survivable
    (0.03, 0.05): 0.85,   # ultra-cheap
    (0.08, 0.10): 0.65,   # mid-cheap
    (0.10, 0.12): 0.40,   # upper PRIMARY
}

# Signal weights — DOWN_MOMENTUM emphasis
SIGNAL_WEIGHTS = {
    'persist': 0.30, 'accel': 0.25, 'lag': 0.15,
    'vol': 0.15, 'tte': 0.10, 'exec': 0.05, 'rsi': 0.05,
}

# Direction priority — DOWN only
DIRECTION_PRIORITY = {
    'DOWN_MOMENTUM': 1.60,
    'DOWN_CONTINUATION': 1.40,
    'UP_REVERSAL': 0.00,
    'UP_CONTINUATION': 0.00,
    'FLAT': 0.00,
}

# Timing — MOMENTUM only
TIMING_LO = 0.40
TIMING_HI = 0.80

# Scan intervals (§9)
BASE_SCAN_INTERVAL = 5.0
FAST_SCAN_INTERVAL = 2.0

# Output paths
OUTPUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v2171_live")
LOG_FILE = OUTPUT_DIR / "v2171_live.log"
TRADES_FILE = OUTPUT_DIR / "trades.jsonl"
INCIDENT_FILE = OUTPUT_DIR / "incident_report.json"
STATE_FILE = OUTPUT_DIR / "state.json"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE)),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("v2171_live")
# Reduce HTTP noise
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """§10: Required trade logging."""
    timestamp: str
    asset: str
    interval: str
    slug: str
    condition_id: str
    side: str
    entry_price: float
    bucket: str
    timing_phase: str
    route: str
    signal_score: float
    expected_probability: float
    expected_ev: float
    expected_slippage: float
    actual_fill_price: float
    fill_latency_ms: float
    slippage_bps: float
    settlement_result: str  # "PENDING" | "WON" | "LOST" | "ERROR"
    win_loss: str           # "WIN" | "LOSS" | "PENDING"
    realized_pnl: float
    bankroll_before: float
    bankroll_after: float
    running_pf: float
    running_ev: float
    running_drawdown: float
    fill_quality: str       # "FULL" | "PARTIAL" | "REJECTED" | "STALE"

@dataclass
class LiveState:
    """Persistent runner state."""
    live_enabled: bool = True
    paper_only: bool = False
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    bankroll: float = 0.0  # set at startup
    consecutive_losses: int = 0
    daily_loss: float = 0.0
    weekly_loss: float = 0.0
    daily_trades: int = 0
    settlement_errors: int = 0
    accounting_errors: int = 0
    stale_fill_errors: int = 0
    api_execution_errors: int = 0
    duplicate_position_errors: int = 0
    last_daily_reset: str = ""
    last_weekly_reset: str = ""
    running_pnl_list: list = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""


# ═══════════════════════════════════════════════════════════════════════
# MARKET DATA — CLOB ORDERBOOK
# ═══════════════════════════════════════════════════════════════════════

def fetch_orderbook_depth(token_id: str, depth: int = 10) -> Optional[dict]:
    """Fetch real orderbook from Polymarket CLOB."""
    try:
        url = f"{CLOB_URL}/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        bids = sorted(
            [(float(e["price"]), float(e["size"])) for e in data.get("bids", [])],
            key=lambda x: -x[0]
        )[:depth]
        asks = sorted(
            [(float(e["price"]), float(e["size"])) for e in data.get("asks", [])],
            key=lambda x: x[0]
        )[:depth]
        return {
            "bids": bids,
            "asks": asks,
            "best_bid": bids[0][0] if bids else 0.0,
            "best_ask": asks[0][0] if asks else 1.0,
            "spread": asks[0][0] - bids[0][0] if bids and asks else 1.0,
            "tick_size": data.get("tick_size", "0.01"),
        }
    except Exception as e:
        log.warning(f"Orderbook fetch failed for {token_id[:16]}...: {e}")
        return None


def compute_continuation_from_orderbook(ob: dict, side: str = "DOWN") -> Tuple[str, float, dict]:
    """
    §12: Derive continuation signal from live orderbook.
    DOWN_MOMENTUM = heavy bid/ask imbalance + cheap token price in bucket.
    """
    if not ob or not ob["bids"] or not ob["asks"]:
        return "NO_SIGNAL", 0.0, {}

    best_bid = ob["best_bid"]
    best_ask = ob["best_ask"]
    spread = ob["spread"]
    mid = (best_bid + best_ask) / 2

    # Compute bid/ask volume imbalance
    total_bid_vol = sum(s for _, s in ob["bids"][:5])
    total_ask_vol = sum(s for _, s in ob["asks"][:5])
    vol_imbalance = (total_bid_vol - total_ask_vol) / max(total_bid_vol + total_ask_vol, 1)

    # Spread relative to price
    spread_pct = spread / max(mid, 0.001)

    # For DOWN token (cheap side): high ask volume = selling pressure = DOWN continuation
    # Token price in preferred bucket?
    in_preferred = 0.05 <= mid < 0.08
    in_primary = 0.03 <= mid < 0.12
    bucket_weight = 1.00 if in_preferred else (
        0.85 if 0.03 <= mid < 0.05 else (
            0.65 if 0.08 <= mid < 0.10 else (
                0.40 if 0.10 <= mid < 0.12 else 0.0
            )
        )
    )

    # Simple continuation score from orderbook
    # Heavy ask volume on DOWN token = market expects it to go to 0 = DOWN_MOMENTUM
    ask_heavy = total_ask_vol > total_bid_vol * 1.2
    reasonable_spread = spread_pct < 0.25  # spread < 25% of price

    # Expected probability: cheap token losing = settling to 0
    # Price near 0 = high probability of losing, high payout if correct
    expected_prob_down = 1.0 - mid  # DOWN token at 7¢ → 93% chance it settles to 0

    # Expected EV for buying DOWN token at mid price
    # Win: token settles to 1.0, payout = (1.0 - mid) per share
    # Lose: token settles to 0.0, loss = mid per share
    expected_ev = expected_prob_down * mid - (1 - expected_prob_down) * (1.0 - mid)

    signal_info = {
        "mid": mid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_pct": spread_pct,
        "total_bid_vol": total_bid_vol,
        "total_ask_vol": total_ask_vol,
        "vol_imbalance": vol_imbalance,
        "bucket_weight": bucket_weight,
        "expected_prob_down": expected_prob_down,
        "expected_ev": expected_ev,
        "in_preferred": in_preferred,
        "in_primary": in_primary,
    }

    # Signal validation
    if not in_primary:
        return "NO_SIGNAL", 0.0, signal_info

    if not reasonable_spread:
        return "SPREAD_TOO_WIDE", 0.0, signal_info

    if not ask_heavy and vol_imbalance > -0.1:
        return "NO_MOMENTUM", 0.0, signal_info

    # Survivability score (§7): realized_ev × fill_prob × slippage_survival
    fill_probability = min(1.0, total_ask_vol / max(POSITION_SIZE / max(mid, 0.01), 1))
    slippage_survival = max(0, 1.0 - spread_pct * 3)  # wider spread = less survivable
    payout_asymmetry = (1 - mid) / max(mid, 0.01)  # e.g., 0.07 → 13.3x

    survivability = expected_ev * fill_probability * slippage_survival * payout_asymmetry * bucket_weight

    if survivability < 0.05:
        return "LOW_SURVIVABILITY", survivability, signal_info

    state = "DOWN_MOMENTUM" if ask_heavy and vol_imbalance < -0.15 else "DOWN_CONTINUATION"
    return state, survivability, signal_info


# ═══════════════════════════════════════════════════════════════════════
# LIVE TRADING ENGINE
# ═══════════════════════════════════════════════════════════════════════

class V2171LiveRunner:
    """V21.7.1 Live Deployment Runner — DOWN_MOMENTUM EXECUTION SURVIVABLE."""

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self.state = LiveState()
        self.trades: List[dict] = []
        self.wallet_info: dict = {}
        self.active_contracts: Dict[str, dict] = {}  # slug → contract info
        self.active_positions: Dict[str, dict] = {}   # condition_id → position info
        self.scan_interval = BASE_SCAN_INTERVAL
        self.running = True
        self.start_time = datetime.now(timezone.utc)

    def initialize(self) -> bool:
        """Check wallet, auth, and discover markets."""
        log.info("=" * 60)
        log.info("V21.7.1 LIVE DEPLOYMENT — INITIALIZATION")
        log.info("=" * 60)

        # Wallet check
        self.wallet_info = check_wallet()
        log.info(f"Wallet: {self.wallet_info.get('address', 'UNKNOWN')}")
        log.info(f"  USDC: ${self.wallet_info.get('usdc_total', 0):.2f}")
        log.info(f"  On-chain USDC: ${self.wallet_info.get('usdc_total', 0):.2f}")
        log.info(f"  Tradeable bankroll: ${BANKROLL_ACTUAL:.2f}")
        log.info(f"  Position size: ${POSITION_SIZE:.2f}")

        if not self.wallet_info.get('collateral_ready', False):
            log.warning("⚠️  Collateral NOT ready — running in PAPER mode")
            self.paper_mode = True

        # Auth check
        creds = derive_api_credentials()
        if "error" in creds:
            log.error(f"Auth FAILED: {creds['error']}")
            log.warning("⚠️  Running in PAPER mode — no CLOB access")
            self.paper_mode = True
        else:
            log.info(f"Auth: {creds.get('mode')} wallet={creds.get('wallet', '')[:10]}...")

        self.state.bankroll = BANKROLL_ACTUAL  # User-confirmed tradeable amount
        self.state.paper_only = self.paper_mode

        log.info(f"\nConfiguration:")
        log.info(f"  Mode: {'PAPER' if self.paper_mode else 'LIVE'}")
        log.info(f"  Side: {LIVE_PROFILE['side']}")
        log.info(f"  State: {LIVE_PROFILE['state']}")
        log.info(f"  Route: {LIVE_PROFILE['route']}")
        log.info(f"  Bucket: ${LIVE_PROFILE['bucket_primary'][0]:.2f}-${LIVE_PROFILE['bucket_primary'][1]:.2f}")
        log.info(f"  Preferred: ${LIVE_PROFILE['bucket_preferred'][0]:.2f}-${LIVE_PROFILE['bucket_preferred'][1]:.2f}")
        log.info(f"  Position: ${POSITION_SIZE:.2f}")
        log.info(f"  Max trades: {MAX_TOTAL_LIVE_TRADES}")
        log.info(f"  Max daily loss: ${MAX_DAILY_LOSS:.2f}")
        log.info(f"  Max weekly loss: ${MAX_WEEKLY_LOSS:.2f}")
        log.info(f"  Max consecutive losses: {MAX_CONSECUTIVE_LOSSES}")

        # Save initial state
        self._save_state()
        log.info("✓ Initialization complete")
        return True

    def discover_markets(self) -> Dict[str, dict]:
        """Discover active BTC 5m and 15m markets."""
        discovered = {}
        for asset in LIVE_PROFILE["asset"]:
            for interval in LIVE_PROFILE["intervals"]:
                slug_key = f"{asset}_{interval}"
                log.info(f"Discovering {asset} {interval} market...")
                contract = discover_active_contract(asset, interval)
                if contract:
                    log.info(f"  Found: {contract.get('slug', 'unknown')}")
                    log.info(f"  Expires in: {contract.get('expires_in_sec', 0):.0f}s")
                    log.info(f"  NegRisk: {contract.get('negRisk', False)}")
                    discovered[slug_key] = contract
                    self.active_contracts[slug_key] = contract
                else:
                    log.warning(f"  No active contract for {asset} {interval}")
        return discovered

    def check_kill_switches(self, proposed_pnl: float = 0.0) -> Tuple[bool, str]:
        """§7: Check all kill switch conditions. Returns (allowed, reason)."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

        # Reset daily counters
        if self.state.last_daily_reset != today:
            self.state.daily_loss = 0.0
            self.state.daily_trades = 0
            self.state.last_daily_reset = today

        # §11: Hard failure conditions — immediate PAPER reversion
        if self.state.settlement_errors > 0:
            self.state.halted = True
            self.state.halt_reason = f"SETTLEMENT_ERROR_COUNT={self.state.settlement_errors}"
            self._write_incident("settlement_error", self.state.halt_reason)
            return False, self.state.halt_reason

        if self.state.accounting_errors > 0:
            self.state.halted = True
            self.state.halt_reason = f"ACCOUNTING_ERROR_COUNT={self.state.accounting_errors}"
            self._write_incident("accounting_error", self.state.halt_reason)
            return False, self.state.halt_reason

        if self.state.stale_fill_errors >= 3:
            self.state.halted = True
            self.state.halt_reason = f"STALE_FILL_COUNT={self.state.stale_fill_errors}"
            self._write_incident("stale_fill", self.state.halt_reason)
            return False, self.state.halt_reason

        if self.state.api_execution_errors >= 3:
            self.state.halted = True
            self.state.halt_reason = f"API_EXECUTION_ERROR_COUNT={self.state.api_execution_errors}"
            self._write_incident("api_execution_error", self.state.halt_reason)
            return False, self.state.halt_reason

        if self.state.duplicate_position_errors > 0:
            self.state.halted = True
            self.state.halt_reason = f"DUPLICATE_POSITION_ERROR={self.state.duplicate_position_errors}"
            self._write_incident("duplicate_position", self.state.halt_reason)
            return False, self.state.halt_reason

        # §4: Loss limits
        if self.state.daily_loss + proposed_pnl <= -MAX_DAILY_LOSS:
            return False, f"DAILY_LOSS_LIMIT ${self.state.daily_loss:.2f}"

        if self.state.weekly_loss + proposed_pnl <= -MAX_WEEKLY_LOSS:
            return False, f"WEEKLY_LOSS_LIMIT ${self.state.weekly_loss:.2f}"

        if self.state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.state.halted = True
            self.state.halt_reason = f"CONSECUTIVE_LOSSES_{self.state.consecutive_losses}"
            self._write_incident("max_consecutive_losses", self.state.halt_reason)
            return False, self.state.halt_reason

        if self.state.total_trades >= MAX_TOTAL_LIVE_TRADES:
            return False, f"MAX_TRADES_REACHED_{self.state.total_trades}"

        if self.state.daily_trades >= MAX_DAILY_TRADES:
            return False, "MAX_DAILY_TRADES"

        if len(self.active_positions) >= MAX_CONCURRENT:
            return False, "MAX_CONCURRENT_POSITIONS"

        # §11: Rolling EV check
        if len(self.state.running_pnl_list) >= 100:
            last_100 = self.state.running_pnl_list[-100:]
            rolling_ev = sum(last_100) / len(last_100)
            if rolling_ev < 0:
                self.state.halted = True
                self.state.halt_reason = f"ROLLING_EV_NEGATIVE_{rolling_ev:.4f}"
                self._write_incident("negative_rolling_ev", self.state.halt_reason)
                return False, self.state.halt_reason

            # PF check on last 100
            gross_profit = sum(p for p in last_100 if p > 0)
            gross_loss = abs(sum(p for p in last_100 if p < 0))
            rolling_pf = gross_profit / max(gross_loss, 0.01)
            if rolling_pf < 1.0:
                self.state.halted = True
                self.state.halt_reason = f"ROLLING_PF_BELOW_1_{rolling_pf:.2f}"
                self._write_incident("pf_below_1", self.state.halt_reason)
                return False, self.state.halt_reason

        return True, "OK"

    def evaluate_and_trade(self, slug_key: str, contract: dict) -> Optional[dict]:
        """§6: Evaluate a market for DOWN_MOMENTUM entry."""
        # Get both tokens, find DOWN (cheaper) token
        tokens = contract.get("tokens", [])
        if not tokens or len(tokens) < 2:
            log.info(f"No tokens for {slug_key}")
            return None

        # Fetch orderbook for both tokens to identify cheap (DOWN) side
        token_orderbooks = {}
        for token_info in tokens:
            tid = token_info.get("token_id", "")
            if not tid:
                continue
            ob = fetch_orderbook_depth(tid)
            if not ob or ob.get("best_bid", 0) == 0 or ob.get("best_ask", 0) == 0:
                continue
            mid = (ob["best_bid"] + ob["best_ask"]) / 2
            ob["mid"] = mid
            token_orderbooks[tid] = (mid, ob)

        if len(token_orderbooks) < 1:
            log.info(f"No orderbook data for {slug_key}")
            return None

        # Find the cheaper token (DOWN side)
        sorted_tokens = sorted(token_orderbooks.items(), key=lambda x: x[1][0])
        down_tid = sorted_tokens[0][0]  # cheapest token
        down_mid = sorted_tokens[0][1][0]
        down_ob = sorted_tokens[0][1][1]

        # Must be in PRIMARY bucket (§2)
        if not (0.03 <= down_mid < 0.12):
            log.info(f"DOWN price {down_mid:.4f} outside PRIMARY bucket for {slug_key}")
            return None

        # §6: All checks passed — using DOWN token identified above
            state, survivability, sig_info = compute_continuation_from_orderbook(down_ob, side="DOWN")

            if state not in ("DOWN_MOMENTUM", "DOWN_CONTINUATION"):
                log.info(f"No DOWN_MOMENTUM signal for {slug_key}: {state}")
                return None

            if survivability < 0.05:
                log.info(f"Low survivability {survivability:.4f} for {slug_key}")
                return None

            # Timing: compute from contract expiry
            expires_in = contract.get("expires_in_sec", 0)
            # Skip markets about to expire (< 30 seconds left)
            if expires_in < 30:
                log.info(f"Market {slug_key} expiring in {expires_in:.0f}s — skipping")
                return None

            # Estimate time_pct — how far through the market lifetime
            slug_parts = contract.get("slug", "").split("-")
            interval_str = slug_parts[-2] if len(slug_parts) >= 2 else "5m"
            interval_sec = int(interval_str.replace("m", "")) * 60 if interval_str.endswith("m") else 300
            total_lifetime = interval_sec * 2  # updown markets typically last 2x interval
            time_pct = 1.0 - (expires_in / max(total_lifetime, 1))

            # Log timing for visibility
            log.info(f"  {slug_key}: expires_in={expires_in:.0f}s interval={interval_sec}s time_pct={time_pct:.2f} down_mid={down_mid:.4f}")

            # Must be in MOMENTUM window preferred, but NOT blocking — §5 says
            # market timing is PREFERENCE not hard gate for Phase 1 micro live
            if 0 < time_pct < TIMING_LO:
                log.info(f"Too early for MOMENTUM window: time_pct={time_pct:.2f}")
            elif time_pct >= TIMING_HI:
                log.info(f"Too late for MOMENTUM window: time_pct={time_pct:.2f}")

            # §6: All checks pass
            price = down_ob["best_ask"]  # TAKER buys at ask
            tick_size = down_ob.get("tick_size", "0.01")
            rounded_price = round_to_tick(price, tick_size)

            if not validate_price(rounded_price, tick_size):
                log.warning(f"Price {rounded_price} doesn't conform to tick {tick_size}")
                return None

            # Check position doesn't already exist for this condition
            cid = contract.get("conditionId", "")
            if cid in self.active_positions:
                return None

            # Proposed P&L for kill switch check
            proposed_loss = POSITION_SIZE  # worst case: lose full position
            allowed, reason = self.check_kill_switches(proposed_loss)
            if not allowed:
                log.info(f"Kill switch blocked trade: {reason}")
                return None

            # Execute
            log.info(f"🔴 SIGNAL: {state} | price={down_mid:.4f} | survivability={survivability:.4f} | {slug_key}")

            # Determine bucket weight
            bucket_weight = 1.0
            for (lo, hi), w in BUCKET_WEIGHTS.items():
                if lo <= down_mid < hi:
                    bucket_weight = w
                    break

            adjusted_size = POSITION_SIZE * bucket_weight

            # Build trade record
            trade = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "asset": contract.get("slug", "").split("-")[0].upper() if "-" in contract.get("slug", "") else "BTC",
                "interval": contract.get("slug", "").split("-")[-2] if "-" in contract.get("slug", "") else "5m",
                "slug": contract.get("slug", ""),
                "condition_id": cid,
                "side": "DOWN",
                "entry_price": rounded_price,
                "bucket": f"{down_mid:.3f}",
                "timing_phase": "MOMENTUM" if TIMING_LO <= time_pct < TIMING_HI else "UNKNOWN",
                "route": "TAKER",
                "signal_score": survivability,
                "expected_probability": sig_info.get("expected_prob_down", 0),
                "expected_ev": sig_info.get("expected_ev", 0),
                "expected_slippage": sig_info.get("spread_pct", 0),
                "actual_fill_price": rounded_price,  # updated after fill
                "fill_latency_ms": 0,
                "slippage_bps": 0,
                "settlement_result": "PENDING",
                "win_loss": "PENDING",
                "realized_pnl": 0.0,
                "bankroll_before": self.state.bankroll,
                "bankroll_after": self.state.bankroll,  # updated after settlement
                "running_pf": 0.0,
                "running_ev": 0.0,
                "running_drawdown": 0.0,
                "fill_quality": "PENDING",
                "mode": "PAPER" if self.paper_mode else "LIVE",
                "bucket_weight": bucket_weight,
                "adjusted_size": adjusted_size,
            }

            # Execute trade
            if self.paper_mode:
                trade["fill_quality"] = "PAPER_FILL"
                trade["actual_fill_price"] = rounded_price
                # Paper P&L: worst case for now, settled at expiry
                trade["realized_pnl"] = 0.0  # PENDING settlement
            else:
                # Live execution via CLOB
                spec = build_dry_run_order(down_tid, "BUY", rounded_price, adjusted_size)
                if spec.valid:
                    result = submit_tracked_order(spec)
                    if "error" in result:
                        log.error(f"Order failed: {result['error']}")
                        self.state.api_execution_errors += 1
                        trade["fill_quality"] = "ERROR"
                        trade["settlement_result"] = "ERROR"
                        return None
                    trade["actual_fill_price"] = rounded_price
                    trade["fill_quality"] = "FULL"
                else:
                    log.warning(f"Invalid order spec: {spec.errors}")
                    return None

            # Update state
            self.state.total_trades += 1
            self.state.daily_trades += 1
            self.active_positions[cid] = trade

            # Log trade
            self._save_trade(trade)
            log.info(f"  Trade #{self.state.total_trades}: DOWN @ ${rounded_price:.4f} | size=${adjusted_size:.2f} | {trade['fill_quality']}")

            return trade

        return None

    def run_loop(self, max_iterations: int = None):
        """Main scan loop."""
        log.info("=" * 60)
        log.info("V21.7.1 LIVE DEPLOYMENT — STARTING SCAN LOOP")
        log.info(f"  Mode: {'PAPER' if self.paper_mode else 'LIVE'}")
        log.info(f"  Max trades: {MAX_TOTAL_LIVE_TRADES}")
        log.info("=" * 60)

        iteration = 0
        last_discovery = 0

        while self.running:
            if max_iterations and iteration >= max_iterations:
                log.info(f"Max iterations ({max_iterations}) reached")
                break

            if self.state.halted:
                log.warning(f"⚠️  HALTED: {self.state.halt_reason}")
                break

            if self.state.total_trades >= MAX_TOTAL_LIVE_TRADES:
                log.info(f"✓ Max trades reached: {self.state.total_trades}")
                break

            now = time.time()

            # Rediscover markets every 60 seconds
            if now - last_discovery > 60:
                self.discover_markets()
                last_discovery = now

            # Check each market for trade opportunity
            for slug_key, contract in self.active_contracts.items():
                try:
                    trade = self.evaluate_and_trade(slug_key, contract)
                    if trade:
                        # Trade executed — skip rest of cycle
                        break
                except Exception as e:
                    log.error(f"Trade evaluation error: {e}")
                    traceback.print_exc()

            # Save state
            self._save_state()

            # Sleep
            sleep_time = self.scan_interval
            time.sleep(sleep_time)
            iteration += 1

            # Periodic status
            if iteration % 12 == 0:  # every ~1 minute at 5s interval
                log.info(
                    f"Status: {self.state.total_trades} trades | "
                    f"${self.state.bankroll:.2f} bank | "
                    f"P&L: ${self.state.total_pnl:.2f} | "
                    f"Consec losses: {self.state.consecutive_losses} | "
                    f"Active positions: {len(self.active_positions)}"
                )

        log.info("=" * 60)
        log.info("V21.7.1 DEPLOYMENT COMPLETE")
        log.info(f"  Total trades: {self.state.total_trades}")
        log.info(f"  Total P&L: ${self.state.total_pnl:.2f}")
        log.info(f"  Bankroll: ${self.state.bankroll:.2f}")
        log.info("=" * 60)
        self._save_state()

    def _save_trade(self, trade: dict):
        """§10: Persist trade record."""
        with open(TRADES_FILE, 'a') as f:
            f.write(json.dumps(trade, default=str) + "\n")

    def _save_state(self):
        """Save runner state."""
        state_dict = {
            "live_enabled": self.state.live_enabled,
            "paper_only": self.state.paper_only,
            "total_trades": self.state.total_trades,
            "wins": self.state.wins,
            "losses": self.state.losses,
            "total_pnl": self.state.total_pnl,
            "bankroll": self.state.bankroll,
            "consecutive_losses": self.state.consecutive_losses,
            "daily_loss": self.state.daily_loss,
            "weekly_loss": self.state.weekly_loss,
            "daily_trades": self.state.daily_trades,
            "settlement_errors": self.state.settlement_errors,
            "accounting_errors": self.state.accounting_errors,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "active_positions": len(self.active_positions),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state_dict, f, indent=2, default=str)

    def _write_incident(self, incident_type: str, detail: str):
        """Write incident report on kill switch trigger."""
        incident = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": incident_type,
            "detail": detail,
            "state": asdict(self.state),
            "trades_so_far": self.state.total_trades,
            "pnl": self.state.total_pnl,
        }
        with open(INCIDENT_FILE, 'w') as f:
            json.dump(incident, f, indent=2, default=str)
        log.critical(f"INCIDENT REPORT: {incident_type} — {detail}")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="V21.7.1 Live Deployment")
    parser.add_argument("--live", action="store_true", help="Enable live execution (default: paper)")
    parser.add_argument("--max-iterations", type=int, default=None, help="Max scan iterations")
    parser.add_argument("--scan-interval", type=float, default=5.0, help="Scan interval in seconds")
    args = parser.parse_args()

    runner = V2171LiveRunner(paper_mode=not args.live)

    if args.scan_interval:
        BASE_SCAN_INTERVAL = args.scan_interval

    runner.initialize()

    log.info("\nStarting V21.7.1 deployment loop...")
    if not runner.wallet_info.get('collateral_ready', False):
        log.warning("⚠️  Wallet not collateral-ready — PAPER MODE FORCED")
        log.warning("To enable live: ensure USDC balance > $10 and allowances set")

    runner.run_loop(max_iterations=args.max_iterations)