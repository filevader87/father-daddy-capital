#!/usr/bin/env python3
"""
FDC V20.1 MICRO VALIDATION — Live Trading
==========================================
BTC only | 0.50-0.60 bucket | $2 fixed size | 1 concurrent position
Hard stops: $10 daily loss, $30 weekly loss, forced shutdown on errors

LIVE_MODE = MICRO_VALIDATION
- $50 bankroll
- $2 fixed trade size
- Max 1 open position at a time
- Stop after 20-30 trades OR hard stop triggers
- Forced shutdown on: accounting failure, settlement error, duplicate open

Author: Hugh (3rd of 5) for Father Daddy Capital
"""

import json
import os
import sys
import time
import traceback
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "src"))

# ── V20.1 Engine Components ──
from pm_engine_v19_8 import (
    SHADOW_PROFILES,
    compute_downtrend_veto,
    discover_contracts_multi,
    classify_token_state,
    compute_recoverability,
    enhanced_signal,
    fetch_asset_candles,
    get_clob_book_depth,
    ASSET_MAP,
    SERIES_CONFIG,
)
from src.regime.regime_classifier import classify_regime, Regime, BLOCKED_REGIMES
from src.microstructure.orderbook_transition import (
    OrderbookTransitionTracker,
    compute_transition_score,
    MINIMUM_TRANSITION_THRESHOLD,
)
from src.microstructure.probability_lag import ProbabilityLagTracker
from src.sentiment.xai_x_sentiment import get_sentiment_veto, classify_sentiment_regime
from fdc_pm_live import PMLiveClient, KillSwitch, check_wallet

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║ HARDCODED MICRO VALIDATION PARAMS — DO NOT MODIFY WITHOUT FATHER DADDY'S   ║
# ║ DIRECT ORDERS                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

LIVE_MODE = "MICRO_VALIDATION"
MIN_TRADES = 20
MAX_TRADES = 30
BANKROLL = 50.0
TRADE_SIZE = 2.0
MAX_CONCURRENT = 1
MAX_DAILY_LOSS = 10.0
MAX_WEEKLY_LOSS = 30.0
CYCLE_SECONDS = 15

# ── V20.1 Profile: BTC_BALANCED_50_60 ──
ASSET_WHITELIST = {"BTC": True}  # BTC ONLY. No ETH, no SOL, no XRP.
CANDIDATE_BUCKET = (0.50, 0.60)  # Entry window
BLOCKED_BUCKET = (0.40, 0.50)    # Toxic WR — BLOCKED
MIN_TRANSITION = MINIMUM_TRANSITION_THRESHOLD
MIN_REVERSAL = 2

# ── State ──
SIGNAL_DIR = Path("paper_trading")
SIGNAL_DIR.mkdir(exist_ok=True)
MICRO_LOG = SIGNAL_DIR / "micro_validation_log.jsonl"
MICRO_REPORT = SIGNAL_DIR / "micro_validation_report.json"
POSITIONS_FILE = SIGNAL_DIR / "micro_positions.json"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║ HARD STOPS — FORCE KILL ON VIOLATION                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class HardStops:
    """Non-negotiable circuit breakers. Violation = immediate shutdown."""

    def __init__(self):
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        self.trade_count = 0
        self.open_positions = 0
        self.halted = False
        self.halt_reason = ""
        self.start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.start_week = datetime.now(timezone.utc).isocalendar()[1]
        self.all_trades = []

    def check(self, pnl_delta: float = 0.0) -> tuple:
        """Check all hard stops. Returns (allowed: bool, reason: str)."""
        if self.halted:
            return False, f"HALTED: {self.halt_reason}"

        # Update running PnL
        self.daily_pnl += pnl_delta
        self.weekly_pnl += pnl_delta

        # Check date rollover
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.start_date:
            self.daily_pnl = pnl_delta  # Reset daily
            self.start_date = today

        # ── Forced stop: daily loss ──
        if self.daily_pnl <= -MAX_DAILY_LOSS:
            self.halted = True
            self.halt_reason = f"daily_loss_{self.daily_pnl:+.2f}_exceeds_{MAX_DAILY_LOSS}"
            return False, self.halt_reason

        # ── Forced stop: weekly loss ──
        if self.weekly_pnl <= -MAX_WEEKLY_LOSS:
            self.halted = True
            self.halt_reason = f"weekly_loss_{self.weekly_pnl:+.2f}_exceeds_{MAX_WEEKLY_LOSS}"
            return False, self.halt_reason

        # ── Forced stop: max trades ──
        if self.trade_count >= MAX_TRADES:
            self.halted = True
            self.halt_reason = f"max_trades_reached_{self.trade_count}"
            return False, self.halt_reason

        # ── Force stop: max concurrent positions ──
        if self.open_positions > MAX_CONCURRENT:
            self.halted = True
            self.halt_reason = f"concurrent_positions_{self.open_positions}_exceeds_{MAX_CONCURRENT}"
            return False, self.halt_reason

        return True, "OK"

    def on_accounting_failure(self, detail: str):
        """FORCE SHUTDOWN on accounting failure."""
        self.halted = True
        self.halt_reason = f"accounting_failure: {detail}"

    def on_settlement_error(self, detail: str):
        """FORCE SHUTDOWN on settlement error."""
        self.halted = True
        self.halt_reason = f"settlement_error: {detail}"

    def on_duplicate_open(self, detail: str):
        """FORCE SHUTDOWN on duplicate open."""
        self.halted = True
        self.halt_reason = f"duplicate_open: {detail}"


class MicroPositionTracker:
    """Track open and closed positions with full audit trail."""

    def __init__(self):
        self.open_positions = {}   # position_key -> dict
        self.closed_positions = []  # list of dicts
        self.bankroll = BANKROLL
        self.open_count = 0

    def open_position(self, key, entry) -> bool:
        """Open a position. Returns True on success, False on duplicate."""
        if key in self.open_positions:
            return False
        if len(self.open_positions) >= MAX_CONCURRENT:
            return False
        self.open_positions[key] = entry
        self.open_count += 1
        return True

    def close_position(self, key, close_price, close_time) -> dict | None:
        """Close position and compute PnL. Returns None on error."""
        if key not in self.open_positions:
            return None
        pos = self.open_positions.pop(key)
        entry_price = pos["entry_ask"]
        side = pos["side"]

        # PnL calculation for binary options
        if side == "UP":
            # Bought UP token at entry_price, closes at close_price
            payout = TRADE_SIZE * close_price / max(entry_price, 0.01)
            pnl = payout - TRADE_SIZE
        else:  # DOWN
            close_price_down = 1.0 - close_price  # Down token settles at 1 - up_price
            entry_down = 1.0 - entry_price
            payout = TRADE_SIZE * close_price_down / max(entry_down, 0.01)
            pnl = payout - TRADE_SIZE

        result = {
            **pos,
            "close_price": close_price,
            "close_time": close_time,
            "pnl": round(pnl, 4),
            "pnl_dollars": round(pnl, 2),
            "cumulative_pnl": round(self.bankroll - BANKROLL + pnl, 2),
        }
        self.bankroll += pnl
        self.closed_positions.append(result)
        return result


def log_entry(entry: dict):
    """Append to micro validation log."""
    with open(MICRO_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def save_positions(tracker: MicroPositionTracker):
    """Persist positions to disk."""
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bankroll": round(tracker.bankroll, 2),
        "open": {k: v for k, v in tracker.open_positions.items()},
        "closed": tracker.closed_positions,
        "open_count": tracker.open_count,
    }
    with open(POSITIONS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def check_bucket(entry_price: float) -> tuple:
    """Classify and gate bucket. Returns (allowed, rule, reason)."""
    if CANDIDATE_BUCKET[0] <= entry_price < CANDIDATE_BUCKET[1]:
        return True, "CANDIDATE", ""
    if BLOCKED_BUCKET[0] <= entry_price < BLOCKED_BUCKET[1]:
        return False, "BLOCKED", f"blocked_by_toxic_bucket_{BLOCKED_BUCKET[0]:.1f}_{BLOCKED_BUCKET[1]:.1f}"
    return False, "OTHER", f"blocked_by_bucket_outside_range_{entry_price:.3f}"


def count_reversal_confirmations(
    RSI_slope, higher_low_count, transition_result,
    spot_velocity_15s, price_vs_reference_pct, regime_name
) -> tuple:
    signals = {}
    signals["rsi_slope_positive"] = (RSI_slope or 0) > 0
    signals["higher_low"] = (higher_low_count or 0) > 0
    signals["spread_compression"] = getattr(transition_result, "spread_compressing", False)
    signals["bid_strengthening"] = getattr(transition_result, "bid_strengthening", False)
    signals["ask_weakening"] = getattr(transition_result, "ask_weakening", False)
    signals["positive_velocity"] = (spot_velocity_15s or 0) > 0
    signals["reclaiming_reference"] = (price_vs_reference_pct or 0) > -0.005
    signals["volatility_compression"] = regime_name == "volatility_compression"
    confirmed = sum(1 for v in signals.values() if v)
    return confirmed, {k: v for k, v in signals.items() if v}


def run_micro_validation(duration_hours: float = 1.0, cycle_seconds: int = CYCLE_SECONDS):
    """Main MICRO_VALIDATION loop. Runs for duration_hours or until hard stop."""

    start_time = datetime.now(timezone.utc)
    end_time = start_time + timedelta(hours=duration_hours)

    # ── Initialize subsystems ──
    hard_stops = HardStops()
    pos_tracker = MicroPositionTracker()
    kill_switch = KillSwitch(max_daily_loss=MAX_DAILY_LOSS, max_weekly_loss=MAX_WEEKLY_LOSS, max_concurrent=MAX_CONCURRENT)
    client = PMLiveClient()
    client_status = client.init()

    trackers = {}       # Per-asset transition trackers
    lag_trackers = {}   # Per-asset probability lag trackers (diagnostic)
    counters = defaultdict(int)
    opened_position_keys = set()
    unique_slugs_seen = set()

    # Sentiment disabled per directive
    USE_SENTIMENT = False  # DO NOT ADD SENTIMENT
    # Lag gate disabled per directive
    USE_LAG_GATE = False   # DO NOT RE-ENABLE LAG GATE

    print("=" * 70)
    print("FDC V20.1 — MICRO VALIDATION LIVE TRADING")
    print("=" * 70)
    print(f"Mode:      {LIVE_MODE}")
    print(f"Bankroll:  ${BANKROLL:.0f}")
    print(f"Trade size: ${TRADE_SIZE:.0f} fixed")
    print(f"Max open:  {MAX_CONCURRENT}")
    print(f"BTC only:  {list(ASSET_WHITELIST.keys())}")
    print(f"Bucket:    {CANDIDATE_BUCKET[0]:.2f}-{CANDIDATE_BUCKET[1]:.2f} CANDIDATE")
    print(f"Blocked:   {BLOCKED_BUCKET[0]:.2f}-{BLOCKED_BUCKET[1]:.2f}")
    print(f"Daily loss cap:  ${MAX_DAILY_LOSS:.0f}")
    print(f"Weekly loss cap: ${MAX_WEEKLY_LOSS:.0f}")
    print(f"Target trades:   {MIN_TRADES}-{MAX_TRADES}")
    print(f"Lag gate:        DISABLED")
    print(f"Sentiment:       DISABLED")
    print(f"Adaptive route:  DISABLED")
    print(f"Start: {start_time.isoformat()}")
    print(f"End:   {end_time.isoformat()}")
    print("=" * 70)
    print()

    # Check wallet
    wallet = check_wallet()
    print(f"Wallet: {wallet.get('address', '?')}")
    print(f"  USDC: ${wallet.get('usdc', 0):.2f}")
    print(f"  MATIC: {wallet.get('matic', 0):.4f}")
    print(f"  Funded: {wallet.get('funded', False)}")
    if wallet.get("usdc", 0) < 10:
        print(f"\n⚠️  USDC balance low (${wallet.get('usdc', 0):.2f}). Waiting for funding.")
        print(f"   Deposit USDC to {wallet.get('address', '?')} to begin live trading.")
        print(f"   Running in SIMULATION/PAPER mode until funded.\n")
    print()

    funded = wallet.get("usdc", 0) >= 10
    cycle = 0

    while datetime.now(timezone.utc) < end_time:
        # ── Hard stop check ──
        allowed, reason = hard_stops.check()
        if not allowed:
            print(f"\n🛑 HARD STOP: {reason}")
            print("SHUTTING DOWN IMMEDIATELY.")
            break

        # Check kill switch
        ks_ok, ks_reason = kill_switch.check(pos_tracker.bankroll, start_time.strftime("%Y-%m-%d"), hard_stops.daily_pnl)
        if not ks_ok:
            print(f"\n🛑 KILL SWITCH: {ks_reason}")
            hard_stops.halted = True
            hard_stops.halt_reason = ks_reason
            break

        # ── Target trades check ──
        if hard_stops.trade_count >= MAX_TRADES:
            print(f"\n✅ MAX TRADES REACHED ({MAX_TRADES}). Shutting down.")
            break

        cycle += 1
        cycle_start = time.time()
        elapsed_h = (datetime.now(timezone.utc) - start_time).total_seconds() / 3600

        if cycle <= 3 or cycle % 10 == 0:
            print(f"  [Cycle {cycle}] elapsed={elapsed_h:.2f}h | trades={hard_stops.trade_count} | PnL=${hard_stops.daily_pnl:+.2f} | open={len(pos_tracker.open_positions)} | bankroll=${pos_tracker.bankroll:.2f}", flush=True)

        try:
            # ── Pre-fetch candle data ──
            prices_cache = {}
            for asset_key in ASSET_MAP:
                try:
                    prices_cache[asset_key] = fetch_asset_candles(asset_key, interval="5m")
                except Exception:
                    prices_cache[asset_key] = []

            # ── Discover BTC contracts ──
            contracts_dict = discover_contracts_multi(asset_key="BTC")
            if not contracts_dict:
                counters["no_contracts"] += 1
                time.sleep(cycle_seconds)
                continue

            all_contracts = []
            for asset_key, contract_list in contracts_dict.items():
                if isinstance(contract_list, list):
                    all_contracts.extend(contract_list)

            if not all_contracts:
                counters["no_contracts"] += 1
                time.sleep(cycle_seconds)
                continue

            for contract in all_contracts:
                asset = contract.get("asset", "UNKNOWN")
                slug = contract.get("slug", "")
                condition_id = contract.get("conditionId", "")
                interval = contract.get("interval", "?")

                # ── BTC ONLY ──
                if asset not in ASSET_WHITELIST or not ASSET_WHITELIST[asset]:
                    counters[f"blocked_asset_{asset}"] += 1
                    continue

                unique_slugs_seen.add(slug)

                # ── Market data ──
                prices = prices_cache.get(asset, [])
                if len(prices) < 14:
                    counters["insufficient_prices"] += 1
                    continue

                try:
                    sig = enhanced_signal(prices, asset_key=asset)
                except Exception as e:
                    counters["signal_error"] += 1
                    continue

                spot_price = sig.get("price", 0)
                RSI = sig.get("rsi", 50)
                RSI_slope = sig.get("RSI_slope", 0)
                SMA20 = sig.get("SMA20", spot_price)
                SMA20_slope = sig.get("SMA20_slope", 0)
                spot_velocity_5s = sig.get("candle_velocity", 0)
                spot_velocity_15s = sig.get("spot_velocity_15s", 0)
                spot_velocity_30s = sig.get("spot_velocity_30s", 0)
                lower_low_count = sig.get("lower_low_count", 0)
                higher_low_count = sig.get("higher_low_count", 0)
                price_vs_reference_pct = sig.get("price_vs_reference_pct", 0)

                direction = sig.get("direction", "neutral")
                confidence = sig.get("confidence", 0)

                # ── Confidence gate ──
                if confidence < 0.15:
                    counters["low_confidence"] += 1
                    continue

                # ── Neutral direction gate ──
                if direction == "neutral":
                    counters["neutral_direction"] += 1
                    continue

                # ── Book data ──
                up_token_id = contract.get("up_token_id", "")
                down_token_id = contract.get("down_token_id", "")
                book_token_id = up_token_id if up_token_id else down_token_id
                book = None
                if book_token_id:
                    try:
                        book = get_clob_book_depth(condition_id, token_id=book_token_id)
                    except Exception:
                        book = None
                if not book and condition_id:
                    try:
                        book = get_clob_book_depth(condition_id)
                    except Exception:
                        book = None
                if not book:
                    counters["no_book"] += 1
                    continue

                bid_depth = float(book.get("depth_usd", 0)) / 2
                ask_depth = float(book.get("depth_usd", 0)) / 2
                spread = float(book.get("spread", 0))
                best_bid = float(book.get("best_bid", 0))
                best_ask = float(book.get("best_ask", 0))
                imbalance = (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-9)
                up_price = float(contract.get("up_price", 0) or 0)
                down_price = float(contract.get("down_price", 0) or 0)

                # ── Transition tracker ──
                if asset not in trackers:
                    trackers[asset] = OrderbookTransitionTracker()
                asset_tracker = trackers[asset]

                transition_result = compute_transition_score(
                    bid_depth=bid_depth, ask_depth=ask_depth,
                    spread=spread, imbalance=imbalance,
                    up_price=up_price, down_price=down_price,
                    up_velocity=float(contract.get("up_token_velocity", 0)),
                    down_velocity=float(contract.get("down_token_velocity", 0)),
                    tracker=asset_tracker,
                )
                transition_score = transition_result.transition_score

                # ── Regime classifier ──
                regime_result = classify_regime(
                    asset=asset, spot_price=spot_price,
                    spot_velocity_5s=sig.get("candle_velocity"),
                    spot_velocity_15s=spot_velocity_15s,
                    spot_velocity_30s=spot_velocity_30s,
                    RSI=RSI, RSI_slope=RSI_slope,
                    SMA20=SMA20, SMA20_slope=SMA20_slope,
                    spread=spread,
                    spread_change=contract.get("spread_change"),
                    bid_depth=bid_depth, ask_depth=ask_depth,
                    bid_depth_change=contract.get("bid_depth_change"),
                    ask_depth_change=contract.get("ask_depth_change"),
                    imbalance=imbalance,
                    imbalance_change=contract.get("imbalance_change"),
                    book_depth_total=bid_depth + ask_depth,
                    lower_low_count=lower_low_count,
                    higher_low_count=higher_low_count,
                    price_vs_reference_pct=price_vs_reference_pct,
                    time_to_expiry_minutes=contract.get("time_to_expiry_minutes"),
                    transition_score=transition_score,
                )
                regime_name = regime_result.regime.value
                regime_blocked = regime_result.blocked

                # ── Entry price & bucket gate ──
                entry_ask = float(contract.get("ask", 0) or contract.get("up_price", 0))
                bucket_ok, bucket_rule, bucket_reason = check_bucket(entry_ask)
                if not bucket_ok:
                    counters[bucket_reason] += 1
                    continue

                # ── Direction from microstructure ──
                if regime_name == "balanced_rotation" and transition_score > MIN_TRANSITION:
                    v20_direction = "up"
                elif regime_name == "balanced_rotation" and transition_score < -MIN_TRANSITION:
                    v20_direction = "down"
                elif direction != "neutral":
                    v20_direction = direction
                else:
                    counters["neutral_direction_final"] += 1
                    continue

                selected_side = v20_direction.upper()
                profile = "BTC_BALANCED_50_60"

                # ── Dedup key ──
                position_key = f"{slug}|{condition_id}|{selected_side}|{profile}"
                if position_key in opened_position_keys:
                    counters["duplicate_blocked"] += 1
                    continue

                # ── Regime gate ──
                if regime_blocked:
                    counters[f"blocked_regime_{regime_name}"] += 1
                    continue

                # ── Market state gate ──
                if regime_name in ("trend_continuation", "panic_sell"):
                    market_state = "trending"
                elif regime_name in ("trend_exhaustion", "fake_reversal"):
                    market_state = "transitioning"
                elif regime_name in ("balanced_rotation", "volatility_compression"):
                    market_state = "balanced"
                else:
                    market_state = "unknown"

                if market_state not in ("balanced", "unknown"):
                    counters[f"blocked_market_state_{market_state}"] += 1
                    continue

                # ── Downtrend veto ──
                veto_data = compute_downtrend_veto(
                    prices, contract=contract, reference_price=prices[-1] if prices else None
                )
                if veto_data.get("downtrend_active") and not veto_data.get("reversal_confirmed"):
                    counters["blocked_downtrend"] += 1
                    continue

                # ── Transition score gate (after 3 snapshots) ──
                if len(asset_tracker._snapshots) >= 3 and transition_score <= MIN_TRANSITION:
                    counters[f"blocked_low_transition"] += 1
                    continue

                # ── Reversal confirmation gate ──
                reversal_count, reversal_signals = count_reversal_confirmations(
                    RSI_slope, higher_low_count, transition_result,
                    spot_velocity_15s, price_vs_reference_pct, regime_name,
                )
                if reversal_count < MIN_REVERSAL:
                    counters["insufficient_reversal"] += 1
                    continue

                # ── Token state gate ──
                token_state = classify_token_state(contract, RSI, v20_direction, prices) if len(prices) >= 14 else "unknown"
                if isinstance(token_state, str) and token_state in ("false_dislocation", "nearly_decided", "dormant_longshot", "untradeable"):
                    counters[f"blocked_token_state_{token_state}"] += 1
                    continue

                # ── PROBABILITY LAG GATE: DISABLED PER DIRECTIVE ──
                # DO NOT RE-ENABLE LAG GATE

                # ── SENTIMENT: DISABLED PER DIRECTIVE ──
                # DO NOT ADD SENTIMENT

                # ── CONCURRENT POSITION CHECK ──
                if len(pos_tracker.open_positions) >= MAX_CONCURRENT:
                    counters["max_concurrent_blocked"] += 1
                    continue

                # ═══════════════════════════════════════════════════════════════
                # ALL GATES PASSED — EXECUTE TRADE
                # ═══════════════════════════════════════════════════════════════
                counters["executable"] += 1

                # Duplicate open check (safety)
                if not pos_tracker.open_position(position_key, {
                    "slug": slug,
                    "condition_id": condition_id,
                    "up_token_id": up_token_id,
                    "down_token_id": down_token_id,
                    "asset": asset,
                    "side": selected_side,
                    "entry_ask": entry_ask,
                    "entry_mark": (best_bid + best_ask) / 2 if best_bid and best_ask else entry_ask,
                    "raw_edge": (1 - entry_ask) - entry_ask if selected_side == "UP" else entry_ask - (1 - entry_ask),
                    "transition_score": transition_score,
                    "reversal_count": reversal_count,
                    "regime": regime_name,
                    "market_state": market_state,
                    "rsi": RSI,
                    "confidence": confidence,
                    "cycle_opened": cycle,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "size": TRADE_SIZE,
                    "profile": profile,
                }):
                    # DUPLICATE OPEN — FORCE SHUTDOWN
                    hard_stops.on_duplicate_open(position_key)
                    break

                opened_position_keys.add(position_key)
                hard_stops.trade_count += 1
                counters["trades_opened"] += 1

                # ── Place order ──
                if funded:
                    # LIVE mode — place real order via CLOB
                    token_id = up_token_id if selected_side == "UP" else down_token_id
                    order_result = client.place_order(
                        token_id=token_id,
                        side="BUY",
                        price=entry_ask,
                        size=TRADE_SIZE,
                        tick_size="0.01",
                    )
                    order_status = order_result.get("status", "UNKNOWN")
                    order_id = order_result.get("order_id", "?")
                else:
                    # PAPER mode — simulate
                    order_status = "SIMULATED"
                    order_id = f"paper_{int(time.time()*1000)}"

                print(f"  📈 TRADE #{hard_stops.trade_count}: {selected_side} {asset} @ {entry_ask:.3f} | size=${TRADE_SIZE} | regime={regime_name} | transition={transition_score:.4f} | reversal={reversal_count}/{MIN_REVERSAL} | lag=DISABLED | sentiment=DISABLED | {order_status} id={order_id} | slug={slug}", flush=True)

                log_entry({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "event": "OPEN",
                    "trade_num": hard_stops.trade_count,
                    "side": selected_side,
                    "asset": asset,
                    "entry_price": entry_ask,
                    "size": TRADE_SIZE,
                    "regime": regime_name,
                    "market_state": market_state,
                    "transition_score": transition_score,
                    "reversal_count": reversal_count,
                    "rsi": RSI,
                    "confidence": confidence,
                    "slug": slug,
                    "condition_id": condition_id,
                    "position_key": position_key,
                    "order_status": order_status,
                    "order_id": order_id,
                    "bankroll": round(pos_tracker.bankroll, 2),
                    "cycle": cycle,
                    "profile": profile,
                })

                # ── Check if we hit min trades ──
                if hard_stops.trade_count >= MAX_TRADES:
                    print(f"\n✅ MAX TRADES REACHED ({MAX_TRADES}).")
                    break

        except Exception as e:
            counters["errors"] += 1
            print(f"  ❌ Error cycle {cycle}: {e}")
            traceback.print_exc()

            # Accounting failure = forced shutdown
            hard_stops.on_accounting_failure(str(e))
            break

        # ── Periodic position settlement check ──
        for key in list(pos_tracker.open_positions.keys()):
            pos = pos_tracker.open_positions[key]
            try:
                now_ts = datetime.now(timezone.utc)
                slug = pos.get("slug", "")
                settled = False
                close_mark = pos["entry_ask"]
                settle_reason = "timed_out"

                # ── Check if 5m/15m contract has expired via slug timestamp ──
                # Slug format: btc-updown-5m-{unix_ts} or btc-updown-15m-{unix_ts}
                try:
                    slug_parts = slug.split("-")
                    if len(slug_parts) >= 4:
                        contract_expiry_ts = int(slug_parts[-1])
                        expiry_dt = datetime.fromtimestamp(contract_expiry_ts, tz=timezone.utc)
                        if now_ts >= expiry_dt:
                            settled = True
                            settle_reason = "contract_expired"
                            # For expired binary options: if won, settle at ~1.0 (profit side); if lost, ~0.0
                            # Use current book if available, otherwise estimate
                except (ValueError, IndexError):
                    pass

                # ── Also check book for current mark ──
                cond_id = pos.get("condition_id", "")
                current_book = None
                if cond_id:
                    token_id = pos.get("up_token_id", "") or None
                    try:
                        current_book = get_clob_book_depth(cond_id, token_id=token_id)
                    except Exception:
                        current_book = None

                if settled and current_book:
                    current_bid = float(current_book.get("best_bid", 0))
                    current_ask = float(current_book.get("best_ask", 0))
                    if current_bid > 0 and current_ask > 0:
                        close_mark = (current_bid + current_ask) / 2

                time_open = (now_ts - datetime.fromisoformat(pos["timestamp"].replace("Z", "+00:00"))).total_seconds() / 60

                # Close if: contract expired OR held > 30 min
                if settled or time_open > 30:
                    if not settled and time_open > 30:
                        settle_reason = "max_hold_exceeded"
                        if current_book:
                            current_bid = float(current_book.get("best_bid", 0))
                            current_ask = float(current_book.get("best_ask", 0))
                            close_mark = (current_bid + current_ask) / 2 if current_bid and current_ask else pos["entry_ask"]

                    close_result = pos_tracker.close_position(key, close_mark, now_ts.isoformat())
                    if close_result:
                        pnl = close_result["pnl_dollars"]
                        hard_stops.check(pnl)
                        counters["trades_closed"] += 1
                        counters["pnl_total"] = counters.get("pnl_total", 0) + pnl
                        print(f"  💰 CLOSE: {pos['side']} {pos['asset']} @ {close_mark:.3f} | PnL=${pnl:+.2f} | cum=${hard_stops.daily_pnl:+.2f} | reason={settle_reason} | hold={time_open:.1f}m", flush=True)
                        log_entry({
                            "timestamp": now_ts.isoformat(),
                            "event": "CLOSE",
                            "side": pos["side"],
                            "asset": pos["asset"],
                            "entry_price": pos["entry_ask"],
                            "close_price": close_mark,
                            "pnl": pnl,
                            "hold_minutes": round(time_open, 1),
                            "settle_reason": settle_reason,
                            "cumulative_pnl": round(hard_stops.daily_pnl, 2),
                            "bankroll": round(pos_tracker.bankroll, 2),
                        })
                    else:
                        # Position already gone (race condition) — this is an accounting failure
                        hard_stops.on_accounting_failure(f"close_position returned None for {key}")

            except Exception as e:
                counters["settlement_errors"] += 1
                print(f"  ⚠️ Settlement error: {e}")
                traceback.print_exc()
                # Settlement error = forced shutdown
                hard_stops.on_settlement_error(str(e))
                break

        # ── Save state ──
        save_positions(pos_tracker)

        # ── Cycle timing ──
        elapsed_cycle = time.time() - cycle_start
        sleep_time = max(1, cycle_seconds - elapsed_cycle)
        time.sleep(sleep_time)

    # ═══════════════════════════════════════════════════════════════════════════════
    # FINAL REPORT
    # ═══════════════════════════════════════════════════════════════════════════════
    end_actual = datetime.now(timezone.utc)
    report = {
        "mode": LIVE_MODE,
        "start": start_time.isoformat(),
        "end": end_actual.isoformat(),
        "duration_hours": (end_actual - start_time).total_seconds() / 3600,
        "cycles": cycle,
        "bankroll_start": BANKROLL,
        "bankroll_end": round(pos_tracker.bankroll, 2),
        "pnl_total": round(hard_stops.daily_pnl, 2),
        "trades_opened": hard_stops.trade_count,
        "trades_closed": counters.get("trades_closed", 0),
        "halts": hard_stops.halted,
        "halt_reason": hard_stops.halt_reason,
        "hard_stops_config": {
            "max_daily_loss": MAX_DAILY_LOSS,
            "max_weekly_loss": MAX_WEEKLY_LOSS,
            "max_concurrent": MAX_CONCURRENT,
            "max_trades": MAX_TRADES,
            "trade_size": TRADE_SIZE,
            "bankroll": BANKROLL,
        },
        "unique_slugs": len(unique_slugs_seen),
        "positions_closed": pos_tracker.closed_positions,
        "counters": dict(counters),
    }

    with open(MICRO_REPORT, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print()
    print("=" * 70)
    print("MICRO VALIDATION COMPLETE")
    print(f"Mode: {LIVE_MODE}")
    print(f"Duration: {(end_actual - start_time).total_seconds() / 3600:.2f}h")
    print(f"Trades: {hard_stops.trade_count} opened, {counters.get('trades_closed', 0)} closed")
    print(f"PnL: ${hard_stops.daily_pnl:+.2f}")
    print(f"Bankroll: ${BANKROLL:.0f} → ${pos_tracker.bankroll:.2f}")
    print(f"Halted: {hard_stops.halted} ({hard_stops.halt_reason or 'N/A'})")
    print(f"Unique slugs: {len(unique_slugs_seen)}")
    print(f"Report: {MICRO_REPORT}")
    print("=" * 70)

    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="FDC V20.1 Micro Validation Live Trading")
    parser.add_argument("--hours", type=float, default=1.0, help="Duration in hours")
    parser.add_argument("--cycle", type=int, default=CYCLE_SECONDS, help="Cycle interval in seconds")
    args = parser.parse_args()
    run_micro_validation(duration_hours=args.hours, cycle_seconds=args.cycle)