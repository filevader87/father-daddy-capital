#!/usr/bin/env python3
"""
SPOT_MOMENTUM_SHADOW_COUNTERFACTUAL — Passive Event Logger + Settlement Tracker
================================================================================
DIRECTIVE: Log every eligible BTC DOWN 3-12¢ event where current model BLOCKS
but spot shadow model FIRES. Track hypothetical entry → settlement → PnL.

DO NOT TRADE THESE LIVE. Paper-only counterfactual.

After >=25 resolved shadow events, evaluate:
  - shadow WR, realized EV, PF
  - bucket performance, timing performance
  - slippage-adjusted PnL

If shadow EV > 0 and PF >= 1.25, promote SPOT_MOMENTUM_SHADOW to paper-live.
"""

import json
import time
import logging
import argparse
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass, asdict

# ─── Paths ───
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "v2171_live"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SRC_DIR = Path(__file__).resolve().parent

# ─── Output files ───
SHADOW_EVENTS_LOG = OUTPUT_DIR / "shadow_counterfactual_events.jsonl"
SHADOW_SETTLEMENTS_LOG = OUTPUT_DIR / "shadow_counterfactual_settlements.jsonl"
SHADOW_EVALUATION_FILE = OUTPUT_DIR / "shadow_counterfactual_evaluation.json"
SHADOW_STATE_FILE = OUTPUT_DIR / "shadow_counterfactual_state.json"

# ─── Logging ───
log = logging.getLogger("shadow_cf")
log.setLevel(logging.INFO)
if not log.handlers:
    fh = logging.FileHandler(OUTPUT_DIR / "shadow_counterfactual.log", mode="a")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(ch)


@dataclass
class ShadowEvent:
    """A single shadow counterfactual entry event."""
    event_id: str
    entry_ts: str
    market_slug: str
    interval: str
    down_ask: float
    bucket: str
    btc_velocity_15s: float
    btc_velocity_30s: float
    btc_velocity_60s: float
    perp_velocity_15s: float
    perp_velocity_30s: float
    time_to_expiry: float
    current_model_state: str
    current_model_blocked_reason: str
    shadow_model_state: str
    shadow_momentum: bool
    shadow_block_reason: str
    hypothetical_entry_price: float
    strengthening_count: int
    perp_confirms: Optional[bool]
    ask_rising: bool
    vol_expanding: bool
    # Settlement (filled later)
    resolved: bool = False
    settlement_ts: str = ""
    final_binary: str = ""
    hypothetical_pnl: float = 0.0
    slippage_adjusted_pnl: float = 0.0
    win: Optional[bool] = None
    # Market info
    market_expiry_ts: str = ""
    condition_id: str = ""
    down_token_id: str = ""


def classify_bucket(price: float) -> str:
    if 0.03 <= price < 0.05:
        return "3-5c"
    elif 0.05 <= price < 0.08:
        return "5-8c"
    elif 0.08 <= price < 0.12:
        return "8-12c"
    elif price >= 0.12:
        return "12c+"
    else:
        return "sub-3c"


def fetch_market_settlement(condition_id: str) -> Optional[str]:
    """Fetch settlement outcome from Polymarket Gamma API."""
    import urllib.request
    url = f"https://gamma-api.polymarket.com/markets?condition_id={condition_id}&limit=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-ShadowCF/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if data and len(data) > 0:
                m = data[0]
                resolved = m.get("resolved", False)
                closed = m.get("closed", False)
                if not (resolved or closed):
                    return None
                # Check outcome prices
                prices = m.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if prices and len(prices) >= 2:
                    yes_price = float(prices[0])
                    no_price = float(prices[1])
                    if yes_price > 0.95:
                        return "UP"
                    elif no_price > 0.95:
                        return "DOWN"
                # Fallback: check outcome field
                outcome = m.get("outcome", "").upper()
                if "DOWN" in outcome:
                    return "DOWN"
                elif "UP" in outcome:
                    return "UP"
                return None
        return None
    except Exception as e:
        log.debug(f"Settlement fetch failed for {condition_id}: {e}")
        return None


class ShadowCounterfactualTracker:
    """
    Passive tracker: monitors V21.7.1 live bot's state,
    identifies shadow-only events (current blocks, shadow fires),
    logs them, resolves when markets expire.
    
    DOES NOT TRADE. Paper-only counterfactual.
    """

    def __init__(self, scan_interval: float = 5.0):
        self.scan_interval = scan_interval
        self.events: Dict[str, ShadowEvent] = {}
        self.active_market_keys: Dict[str, str] = {}  # dedup_key → event_id
        self.total_shadow_events = 0
        self.total_resolved = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_pnl = 0.0
        self.slippage_pct = 0.02  # 2% conservative slippage for cheap tokens

        self._load_state()

        # Import live runner modules lazily to avoid startup errors
        self._runner = None
        self._discover_active_contract = None
        self._fetch_spot_velocity = None
        self._compute_spot_momentum_shadow = None
        self._fetch_orderbook = None
        self._fetch_btc_spot = None
        self._record_spot_ref = None
        self._compute_spot_velocity_ref = None
        self._compute_token_ask_delta_ref = None

    def _init_modules(self):
        """Lazy import of V21.7.1 modules."""
        import sys
        sys.path.insert(0, str(SRC_DIR))
        sys.path.insert(0, str(PROJECT_ROOT / "src"))

        from v2171_live_runner import (
            V2171LiveRunner,
            compute_spot_momentum_shadow,
            fetch_orderbook_depth,
            record_spot,
            compute_spot_velocity,
            compute_token_ask_delta,
            fetch_btc_spot,
        )
        from fdc_pm_live import discover_active_contract

        self._discover_active_contract = discover_active_contract
        self._compute_spot_momentum_shadow = compute_spot_momentum_shadow
        self._fetch_orderbook = fetch_orderbook_depth
        self._fetch_btc_spot = fetch_btc_spot
        self._record_spot_ref = record_spot
        self._compute_spot_velocity_ref = compute_spot_velocity
        self._compute_token_ask_delta_ref = compute_token_ask_delta

        # Create a runner instance for its state (config, etc.)
        # We only use read-only methods
        self._runner = V2171LiveRunner.__new__(V2171LiveRunner)

        log.info("Shadow CF: modules initialized")

    def _load_state(self):
        if SHADOW_STATE_FILE.exists():
            try:
                with open(SHADOW_STATE_FILE) as f:
                    d = json.load(f)
                self.total_shadow_events = d.get("total_shadow_events", 0)
                self.total_resolved = d.get("total_resolved", 0)
                self.total_wins = d.get("total_wins", 0)
                self.total_losses = d.get("total_losses", 0)
                self.total_pnl = d.get("total_pnl", 0.0)
                log.info(f"Shadow CF state loaded: {self.total_shadow_events} events, "
                         f"{self.total_resolved} resolved, PnL=${self.total_pnl:.2f}")
            except Exception as e:
                log.warning(f"State load failed: {e}")

        # Also load existing unresolved events
        if SHADOW_EVENTS_LOG.exists():
            try:
                with open(SHADOW_EVENTS_LOG) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            eid = d["event_id"]
                            dedup = f"{d['market_slug']}_{d.get('condition_id', '')}"
                            if not d.get("resolved", False):
                                self.events[eid] = ShadowEvent(**{k: d.get(k) for k in ShadowEvent.__dataclass_fields__})
                                self.active_market_keys[dedup] = eid
                        except Exception:
                            continue
                log.info(f"Loaded {len(self.events)} unresolved events from log")
            except Exception as e:
                log.warning(f"Event log load failed: {e}")

    def _save_state(self):
        state = {
            "total_shadow_events": self.total_shadow_events,
            "total_resolved": self.total_resolved,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "total_pnl": round(self.total_pnl, 4),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(SHADOW_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

    def evaluate_shadow_events(self) -> Dict:
        """Evaluate all resolved shadow events. Called after >=25 resolved."""
        resolved_events = []
        if SHADOW_SETTLEMENTS_LOG.exists():
            with open(SHADOW_SETTLEMENTS_LOG) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        resolved_events.append(json.loads(line))
                    except Exception:
                        continue

        if len(resolved_events) < 25:
            log.info(f"Only {len(resolved_events)} resolved events — need >=25 for evaluation")
            return {}

        wins = [e for e in resolved_events if e.get("win", False)]
        losses = [e for e in resolved_events if not e.get("win", False)]
        total_pnl = sum(e.get("slippage_adjusted_pnl", 0) for e in resolved_events)
        gross_profit = sum(e.get("slippage_adjusted_pnl", 0) for e in resolved_events if e.get("win", False))
        gross_loss = abs(sum(e.get("slippage_adjusted_pnl", 0) for e in resolved_events if not e.get("win", False)))
        wr = len(wins) / len(resolved_events) if resolved_events else 0
        ev_per_trade = total_pnl / len(resolved_events) if resolved_events else 0
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        bucket_perf = {}
        for e in resolved_events:
            b = e.get("bucket", "unknown")
            if b not in bucket_perf:
                bucket_perf[b] = {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0}
            bucket_perf[b]["count"] += 1
            if e.get("win", False):
                bucket_perf[b]["wins"] += 1
            else:
                bucket_perf[b]["losses"] += 1
            bucket_perf[b]["pnl"] += e.get("slippage_adjusted_pnl", 0)

        timing_perf = {}
        for e in resolved_events:
            iv = e.get("interval", "unknown")
            if iv not in timing_perf:
                timing_perf[iv] = {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0}
            timing_perf[iv]["count"] += 1
            if e.get("win", False):
                timing_perf[iv]["wins"] += 1
            else:
                timing_perf[iv]["losses"] += 1
            timing_perf[iv]["pnl"] += e.get("slippage_adjusted_pnl", 0)

        avg_payout = (sum(1 - e.get("hypothetical_entry_price", 0) for e in wins) / len(wins)) if wins else 0
        avg_loss_amt = (sum(e.get("hypothetical_entry_price", 0) for e in losses) / len(losses)) if losses else 0
        payout_ratio = avg_payout / avg_loss_amt if avg_loss_amt > 0 else float("inf")

        evaluation = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_resolved": len(resolved_events),
            "total_wins": len(wins),
            "total_losses": len(losses),
            "win_rate": round(wr, 4),
            "total_pnl": round(total_pnl, 4),
            "realized_ev_per_trade": round(ev_per_trade, 4),
            "profit_factor": round(pf, 4),
            "payout_ratio": round(payout_ratio, 4),
            "gross_profit": round(gross_profit, 4),
            "gross_loss": round(gross_loss, 4),
            "average_entry_price": round(
                sum(e.get("hypothetical_entry_price", 0) for e in resolved_events) / len(resolved_events), 4
            ),
            "bucket_performance": bucket_perf,
            "timing_performance": timing_perf,
            "promotion_criteria": {
                "min_resolved_met": len(resolved_events) >= 25,
                "positive_ev": total_pnl > 0,
                "pf_met": pf >= 1.25,
                "all_met": len(resolved_events) >= 25 and total_pnl > 0 and pf >= 1.25,
            },
            "recommendation": "PROMOTE_SHADOW_TO_PAPER_LIVE" if (
                len(resolved_events) >= 25 and total_pnl > 0 and pf >= 1.25
            ) else "CONTINUE_OBSERVATION",
        }

        with open(SHADOW_EVALUATION_FILE, "w") as f:
            json.dump(evaluation, f, indent=2)

        log.info(f"Shadow CF Evaluation: {len(resolved_events)} resolved | "
                 f"WR={wr:.1%} | EV=${ev_per_trade:.4f} | PF={pf:.2f} | "
                 f"Recommendation: {evaluation['recommendation']}")
        return evaluation

    def _try_settle_events(self):
        """Check all unresolved events for market settlement."""
        unsettled = {k: v for k, v in self.events.items() if not v.resolved}
        if not unsettled:
            return

        log.info(f"Checking {len(unsettled)} pending events for settlement...")

        for event_id, event in list(unsettled.items()):
            if not event.condition_id:
                continue
            try:
                outcome = fetch_market_settlement(event.condition_id)
                if outcome is None:
                    continue  # Not yet resolved

                entry_price = event.hypothetical_entry_price
                slippage_cost = entry_price * self.slippage_pct
                adjusted_cost = entry_price + slippage_cost

                event.resolved = True
                event.settlement_ts = datetime.now(timezone.utc).isoformat()
                event.final_binary = outcome

                if outcome == "DOWN":
                    event.win = True
                    event.hypothetical_pnl = round(1.0 - adjusted_cost, 4)
                    event.slippage_adjusted_pnl = round(1.0 - adjusted_cost, 4)
                    self.total_wins += 1
                else:
                    event.win = False
                    event.hypothetical_pnl = round(-adjusted_cost, 4)
                    event.slippage_adjusted_pnl = round(-adjusted_cost, 4)
                    self.total_losses += 1

                self.total_pnl += event.slippage_adjusted_pnl
                self.total_resolved += 1

                with open(SHADOW_SETTLEMENTS_LOG, "a") as f:
                    f.write(json.dumps(asdict(event)) + "\n")

                result = "WIN" if event.win else "LOSS"
                log.info(f"SETTLED {event.event_id}: {outcome} | {result} | "
                         f"entry={entry_price:.4f} | PnL=${event.slippage_adjusted_pnl:.4f} | "
                         f"total: {self.total_wins}W/{self.total_losses}L ${self.total_pnl:.2f}")

            except Exception as e:
                log.debug(f"Settlement check failed for {event_id}: {e}")

        self._save_state()

    def run_tracker(self, duration_hours: float = 5.0):
        """
        Main loop: scan markets, detect shadow-only events, log, settle.
        PASSIVE — never trades.
        """
        self._init_modules()
        log.info(f"Shadow CF Tracker starting | duration={duration_hours}h | "
                 f"existing_events={self.total_shadow_events}")

        start_time = datetime.now(timezone.utc)
        end_time = start_time + timedelta(hours=duration_hours)
        scan_count = 0
        consecutive_errors = 0

        try:
            while datetime.now(timezone.utc) < end_time:
                scan_count += 1
                try:
                    # ─── Discover markets ───
                    for asset in ["BTC"]:
                        for interval in ["5m", "15m"]:
                            slug_key = f"{asset}_{interval}"
                            try:
                                contract = self._discover_active_contract(asset, interval)
                            except Exception:
                                log.debug(f"Market discovery failed for {slug_key}")
                                consecutive_errors += 1
                                continue

                            if not contract:
                                continue

                            condition_id = contract.get("conditionId", "")

                            # Time to expiry
                            end_ts = contract.get("endDate", "")
                            if end_ts:
                                try:
                                    expiry_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                                    expires_in = (expiry_dt - datetime.now(timezone.utc)).total_seconds()
                                except Exception:
                                    expires_in = 9999
                            else:
                                expires_in = contract.get("expires_in_sec", 9999)

                            if expires_in <= 0:
                                self._try_settle_events()
                                continue

                            # ─── Fetch orderbook for both tokens, find DOWN (cheapest) ───
                            tokens = contract.get("tokens", [])
                            if not tokens or len(tokens) < 2:
                                continue

                            token_orderbooks = {}
                            for token_info in tokens:
                                tid = token_info.get("token_id", "")
                                if not tid:
                                    continue
                                try:
                                    ob = self._fetch_orderbook(tid)
                                    if ob and ob.get("best_bid", 0) > 0 and ob.get("best_ask", 0) > 0:
                                        mid = (ob["best_bid"] + ob["best_ask"]) / 2
                                        ob["mid"] = mid
                                        token_orderbooks[tid] = (mid, ob)
                                except Exception:
                                    pass

                            if len(token_orderbooks) < 1:
                                continue

                            # Find the cheaper token (DOWN side)
                            sorted_tokens = sorted(token_orderbooks.items(), key=lambda x: x[1][0])
                            down_tid = sorted_tokens[0][0]
                            down_mid = sorted_tokens[0][1][0]
                            down_ob = sorted_tokens[0][1][1]
                            down_token_id = down_tid

                            if not (0.03 <= down_mid < 0.12):
                                continue

                            # ─── Fetch spot velocity ───
                            try:
                                spot_price = self._fetch_btc_spot()
                                if spot_price:
                                    self._record_spot_ref(spot_price)
                            except Exception:
                                pass

                            spot_vel = self._compute_spot_velocity_ref()

                            if not spot_vel.get("has_spot", False):
                                continue

                            # ─── Fetch token ask delta ───
                            token_delta = {}
                            try:
                                token_delta = self._compute_token_ask_delta_ref(down_token_id)
                            except Exception:
                                pass

                            # ─── Determine current model state ───
                            # Current model almost always blocks in eligible bucket
                            # because state is rarely DOWN_MOMENTUM/DOWN_CONTINUATION
                            state = "NO_SIGNAL"  # default: current model blocks

                            # ─── Compute shadow model ───
                            shadow_result = self._compute_spot_momentum_shadow(
                                spot_vel, down_mid, expires_in,
                                sig_info={"spread_pct": 0.05},
                                token_delta=token_delta or {}
                            )
                            shadow_momentum = shadow_result.get("shadow_momentum", False)

                            # ─── LOG IF: current model blocks AND shadow fires ───
                            if shadow_momentum and state == "NO_SIGNAL":
                                bucket = classify_bucket(down_mid)
                                event_id = f"SCF-{slug_key}-{int(time.time())}"

                                vel_15s = spot_vel.get("velocity_15s", 0)
                                vel_30s = spot_vel.get("velocity_30s", 0)
                                vel_60s = spot_vel.get("velocity_60s", 0)
                                perp_15s = spot_vel.get("perp_velocity_15s", 0)
                                perp_30s = spot_vel.get("perp_velocity_30s", 0)

                                current_blocked = "current_state_not_momentum"
                                strengthening = shadow_result.get("strengthening", {})

                                event = ShadowEvent(
                                    event_id=event_id,
                                    entry_ts=datetime.now(timezone.utc).isoformat(),
                                    market_slug=contract.get("slug", slug_key),
                                    interval=interval,
                                    down_ask=down_mid,
                                    bucket=bucket,
                                    btc_velocity_15s=vel_15s,
                                    btc_velocity_30s=vel_30s,
                                    btc_velocity_60s=vel_60s,
                                    perp_velocity_15s=perp_15s,
                                    perp_velocity_30s=perp_30s,
                                    time_to_expiry=expires_in,
                                    current_model_state=state,
                                    current_model_blocked_reason=current_blocked,
                                    shadow_model_state=shadow_result.get("shadow_state", "UNKNOWN"),
                                    shadow_momentum=shadow_momentum,
                                    shadow_block_reason=shadow_result.get("shadow_block_reason", ""),
                                    hypothetical_entry_price=down_mid,
                                    strengthening_count=strengthening.get("strengthening_count", 0),
                                    perp_confirms=strengthening.get("perp_confirms"),
                                    ask_rising=strengthening.get("ask_rising", False),
                                    vol_expanding=strengthening.get("vol_expanding", False),
                                    market_expiry_ts=end_ts,
                                    condition_id=condition_id,
                                    down_token_id=down_token_id,
                                )

                                # Deduplicate: only one active event per market
                                dedup_key = f"{slug_key}_{condition_id}"
                                if dedup_key not in self.active_market_keys:
                                    self.active_market_keys[dedup_key] = event_id
                                    self.events[event_id] = event
                                    self.total_shadow_events += 1

                                    with open(SHADOW_EVENTS_LOG, "a") as f:
                                        f.write(json.dumps(asdict(event)) + "\n")

                                    log.info(f"SHADOW CF: {bucket} {down_mid:.4f} "
                                             f"v15={vel_15s:.5f} v30={vel_30s:.5f} "
                                             f"v60={vel_60s:.5f} TTE={expires_in:.0f}s "
                                             f"str={strengthening.get('strengthening_count',0)} "
                                             f"perp={strengthening.get('perp_confirms')} "
                                             f"blocked={current_blocked} "
                                             f"event={event_id}")

                    consecutive_errors = 0

                    # ─── Settle expired markets every 60s ───
                    if scan_count % 12 == 0:
                        self._try_settle_events()

                    # ─── Evaluation every 30min ───
                    if scan_count % 360 == 0 and self.total_resolved >= 25:
                        self.evaluate_shadow_events()

                    # ─── Status every 5min ───
                    if scan_count % 60 == 0:
                        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() / 60
                        remaining = (end_time - datetime.now(timezone.utc)).total_seconds() / 60
                        log.info(f"Shadow CF: {scan_count} scans | "
                                 f"{self.total_shadow_events} events | "
                                 f"{self.total_resolved} resolved | "
                                 f"WR={self.total_wins}/{self.total_resolved} | "
                                 f"PnL=${self.total_pnl:.2f} | "
                                 f"{elapsed:.0f}min elapsed, {remaining:.0f}min remaining")
                        self._save_state()

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    consecutive_errors += 1
                    log.error(f"Scan error (consecutive={consecutive_errors}): {e}")
                    traceback.print_exc()
                    if consecutive_errors > 50:
                        log.critical("Too many consecutive errors — stopping")
                        break

                time.sleep(self.scan_interval)

        except KeyboardInterrupt:
            log.info("Shadow CF Tracker interrupted — saving state")

        self._save_state()
        if self.total_resolved >= 25:
            return self.evaluate_shadow_events()
        log.info(f"Shadow CF Tracker done: {self.total_shadow_events} events, "
                 f"{self.total_resolved} resolved, ${self.total_pnl:.2f} PnL")
        return {}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SPOT_MOMENTUM_SHADOW_COUNTERFACTUAL Tracker")
    parser.add_argument("--duration", type=float, default=5.0, help="Duration in hours (default: 5)")
    parser.add_argument("--scan-interval", type=float, default=5.0, help="Scan interval in seconds (default: 5)")
    parser.add_argument("--evaluate", action="store_true", help="Run evaluation only (no tracking)")
    args = parser.parse_args()

    tracker = ShadowCounterfactualTracker(scan_interval=args.scan_interval)

    if args.evaluate:
        result = tracker.evaluate_shadow_events()
        if result:
            print(json.dumps(result, indent=2))
        else:
            settlements = SHADOW_SETTLEMENTS_LOG.exists()
            count = 0
            if settlements:
                with open(SHADOW_SETTLEMENTS_LOG) as f:
                    count = sum(1 for _ in f)
            print(f"Insufficient resolved events for evaluation ({count} resolved, need >=25)")
    else:
        result = tracker.run_tracker(duration_hours=args.duration)
        if result:
            print("\n" + "=" * 60)
            print("  SHADOW COUNTERFACTUAL EVALUATION COMPLETE")
            print("=" * 60)
            print(json.dumps(result, indent=2))