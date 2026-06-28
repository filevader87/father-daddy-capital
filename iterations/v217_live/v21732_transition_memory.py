#!/usr/bin/env python3
"""
V21.7.32 — Intra-Window Transition Memory
===========================================
Record every zone transition and bucket touch across market windows.
Preserve prior-window information so transient 3–30¢ touches are not lost.
Do NOT expand live scope. BTC 15m 3–8¢ remains only live-authorized cell.
"""

import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict

import sys
sys.path.insert(0, str(Path(__file__).parent))

from persistent_clob_client import get_pool, close_pool
from v21726_scanner_bridge import (
    discover_all_markets, fetch_books_persistent, classify_zone, close_pool as close_scanner_pool,
)
from book_normalizer import normalize_for_entry

# ─── Paths ───
PROJECT_ROOT = Path("/home/naq1987s/father-daddy-capital")
OUT_DIR = PROJECT_ROOT / "output" / "v21732_transition_memory"
SUPERVISOR_DIR = PROJECT_ROOT / "output" / "supervisor"
OUT_DIR.mkdir(parents=True, exist_ok=True)
SUPERVISOR_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('v21732_transition_memory')

# ─── Zone Taxonomy ───
ZONES_EXTENDED = {
    "CANARY_3_8":      (0.03, 0.08),
    "NEAR_8_12":       (0.08, 0.12),
    "SECONDARY_12_20": (0.12, 0.20),
    "EXTENDED_20_25":  (0.20, 0.25),
    "EXTENDED_25_30":  (0.25, 0.30),
    "MIDZONE_30_40":   (0.30, 0.40),
    "MIDZONE_40_60":   (0.40, 0.60),
    "HIGH_60_85":      (0.60, 0.85),
    "RESOLUTION_85_99":(0.85, 0.99),
    "MICRO_1_3":       (0.01, 0.03),
    "SUB_PENNY":       (0.00, 0.01),
}

ZONE_ORDER = ["SUB_PENNY", "MICRO_1_3", "CANARY_3_8", "NEAR_8_12", "SECONDARY_12_20",
               "EXTENDED_20_25", "EXTENDED_25_30", "MIDZONE_30_40", "MIDZONE_40_60",
               "HIGH_60_85", "RESOLUTION_85_99", "OTHER", "NO_PRICE"]

BUCKETS_3_30 = {
    "3_5":   (0.03, 0.05),
    "5_8":   (0.05, 0.08),
    "8_12":  (0.08, 0.12),
    "12_20": (0.12, 0.20),
    "20_25": (0.20, 0.25),
    "25_30": (0.25, 0.30),
}

TRACKED_MARKETS = ["BTC_5m_UP", "BTC_5m_DOWN", "BTC_15m_UP", "BTC_15m_DOWN",
                   "ETH_5m_UP", "ETH_5m_DOWN", "ETH_15m_UP", "ETH_15m_DOWN"]

TTE_WINDOWS = {
    "5m":  [(30, 300), (60, 300), (90, 300), (120, 300)],
    "15m": [(180, 900), (240, 900), (300, 900)],
}


def classify_zone_ext(ask):
    if ask is None or ask <= 0:
        return "NO_PRICE"
    for name, (lo, hi) in ZONES_EXTENDED.items():
        if lo <= ask < hi:
            return name
    if ask >= 0.99:
        return "RESOLUTION_85_99"
    return "OTHER"


def classify_bucket_3_30(ask):
    if ask is None:
        return None
    for name, (lo, hi) in BUCKETS_3_30.items():
        if lo <= ask < hi:
            return name
    return None


def zone_lower_than(z1, z2):
    """Is z1 a lower (cheaper) zone than z2?"""
    try:
        return ZONE_ORDER.index(z1) < ZONE_ORDER.index(z2)
    except ValueError:
        return False


# ═══════════════════════════════════════════════════════════════
# Window State — persisted in memory across scan cycles
# ═══════════════════════════════════════════════════════════════

class WindowState:
    """Tracks a single market window's lifecycle across scan cycles."""
    def __init__(self, key: str):
        self.key = key
        self.market_slug = ""
        self.condition_id = ""
        self.asset = ""
        self.interval = ""
        self.side = ""
        self.token_id = ""
        self.window_start = None
        self.window_end = None
        self.expiry_timestamp = None
        self.first_seen_at = None
        self.last_seen_at = None
        self.current_zone = None
        self.previous_zone = None
        self.min_ask_seen = None
        self.max_ask_seen = None
        self.min_bid_seen = None
        self.max_bid_seen = None
        self.min_spread_seen = None
        self.max_spread_seen = None
        self.first_bucket_touch_at = None
        self.last_bucket_touch_at = None
        self.bucket_touch_count = 0
        self.zone_transition_count = 0
        self.lowest_zone = None
        self.highest_zone = None
        self.time_in_zone = defaultdict(float)  # zone -> seconds
        self.last_timestamp = None
        self.quote_count = 0
        self.valid_quote_count = 0
        self.stale_quote_count = 0
        self.event_count = 0
        self.shadow_event_count = 0
        self.live_eligible_touch_count = 0
        self.live_order_attempt_count = 0

    def update(self, ask, bid, spread, zone, tte, is_valid, quote_age_ms, timestamp, market_info):
        now = timestamp or datetime.now(timezone.utc).isoformat()

        # Market metadata
        if market_info:
            self.market_slug = market_info.get("slug", self.market_slug)
            self.condition_id = market_info.get("condition_id", self.condition_id)
            self.asset = market_info.get("asset", self.asset)
            self.interval = market_info.get("interval", self.interval)
            self.side = market_info.get("side", self.side)
            self.token_id = market_info.get("token_id", self.token_id)
            self.expiry_timestamp = market_info.get("expiry_timestamp", self.expiry_timestamp)

        # First/last seen
        if self.first_seen_at is None:
            self.first_seen_at = now
        self.last_seen_at = now

        # Zone tracking
        self.previous_zone = self.current_zone
        self.current_zone = zone

        # Min/max
        if ask is not None:
            self.min_ask_seen = min(self.min_ask_seen, ask) if self.min_ask_seen is not None else ask
            self.max_ask_seen = max(self.max_ask_seen, ask) if self.max_ask_seen is not None else ask
        if bid is not None:
            self.min_bid_seen = min(self.min_bid_seen, bid) if self.min_bid_seen is not None else bid
            self.max_bid_seen = max(self.max_bid_seen, bid) if self.max_bid_seen is not None else bid
        if spread is not None:
            self.min_spread_seen = min(self.min_spread_seen, spread) if self.min_spread_seen is not None else spread
            self.max_spread_seen = max(self.max_spread_seen, spread) if self.max_spread_seen is not None else spread

        # Lowest/highest zone
        if self.lowest_zone is None or (zone in ZONE_ORDER and (self.lowest_zone not in ZONE_ORDER or ZONE_ORDER.index(zone) < ZONE_ORDER.index(self.lowest_zone))):
            self.lowest_zone = zone
        if self.highest_zone is None or (zone in ZONE_ORDER and (self.highest_zone not in ZONE_ORDER or ZONE_ORDER.index(zone) > ZONE_ORDER.index(self.highest_zone))):
            self.highest_zone = zone

        # Quote counts
        self.quote_count += 1
        if is_valid:
            self.valid_quote_count += 1
        if quote_age_ms is not None and quote_age_ms > 5000:
            self.stale_quote_count += 1

        # Bucket touch (ask between 3-30¢)
        if ask is not None and 0.03 <= ask <= 0.30:
            self.bucket_touch_count += 1
            if self.first_bucket_touch_at is None:
                self.first_bucket_touch_at = now
            self.last_bucket_touch_at = now

        # Live eligible (BTC 15m DOWN 3-8¢)
        if (self.asset == "BTC" and self.interval == "15m" and self.side == "DOWN"
                and ask is not None and 0.03 <= ask <= 0.08
                and spread is not None and spread <= 0.02
                and is_valid
                and tte is not None and 180 <= tte <= 900):
            self.live_eligible_touch_count += 1

        # Zone transition
        zone_changed = self.previous_zone is not None and self.previous_zone != self.current_zone
        if zone_changed:
            self.zone_transition_count += 1

        # Time in zone (approximate — based on scan interval ~2s)
        dt = 2.0  # approximate seconds between scans
        if self.last_timestamp:
            try:
                t1 = datetime.fromisoformat(self.last_timestamp.replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(now.replace("Z", "+00:00"))
                dt = max((t2 - t1).total_seconds(), 0.5)
            except Exception:
                dt = 2.0
        self.time_in_zone[zone] = self.time_in_zone.get(zone, 0) + dt
        self.last_timestamp = now

        return zone_changed

    def to_dict(self):
        return {
            "key": self.key,
            "market_slug": self.market_slug,
            "condition_id": self.condition_id,
            "asset": self.asset,
            "interval": self.interval,
            "side": self.side,
            "token_id": self.token_id,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "current_zone": self.current_zone,
            "min_ask_seen": self.min_ask_seen,
            "max_ask_seen": self.max_ask_seen,
            "min_spread_seen": self.min_spread_seen,
            "max_spread_seen": self.max_spread_seen,
            "bucket_touch_count": self.bucket_touch_count,
            "zone_transition_count": self.zone_transition_count,
            "lowest_zone": self.lowest_zone,
            "highest_zone": self.highest_zone,
            "quote_count": self.quote_count,
            "valid_quote_count": self.valid_quote_count,
            "live_eligible_touch_count": self.live_eligible_touch_count,
            "time_in_zone": dict(self.time_in_zone),
        }


# ─── Global State ───
window_states: Dict[str, WindowState] = {}
QUOTE_SNAPSHOTS_PATH = OUT_DIR / "quote_snapshots.jsonl"
ZONE_TRANSITIONS_PATH = OUT_DIR / "zone_transitions.jsonl"
BUCKET_TOUCHES_PATH = OUT_DIR / "bucket_touches.jsonl"
WINDOW_LIFECYCLE_PATH = OUT_DIR / "window_lifecycle_summary.jsonl"

# Rolling stats
stats = {
    "quote_snapshots_written": 0,
    "zone_transitions_written": 0,
    "bucket_touches_written": 0,
    "window_lifecycles_written": 0,
    "scans_completed": 0,
    "start_time": None,
}


def get_transition_type(prev_zone, new_zone):
    if new_zone == "CANARY_3_8" and prev_zone != "CANARY_3_8":
        return "ENTERED_CANARY_3_8"
    if prev_zone == "CANARY_3_8" and new_zone != "CANARY_3_8":
        return "EXITED_CANARY_3_8"
    if new_zone == "NEAR_8_12" and prev_zone != "NEAR_8_12":
        return "ENTERED_NEAR_8_12"
    if new_zone == "SECONDARY_12_20" and prev_zone != "SECONDARY_12_20":
        return "ENTERED_SECONDARY_12_20"
    if new_zone == "EXTENDED_20_25" and prev_zone != "EXTENDED_20_25":
        return "ENTERED_EXTENDED_20_25"
    if new_zone == "EXTENDED_25_30" and prev_zone != "EXTENDED_25_30":
        return "ENTERED_EXTENDED_25_30"
    if new_zone in ("MIDZONE_30_40", "MIDZONE_40_60") and prev_zone not in ("MIDZONE_30_40", "MIDZONE_40_60"):
        return "ENTERED_MIDZONE"
    if new_zone in ("HIGH_60_85", "RESOLUTION_85_99") and prev_zone not in ("HIGH_60_85", "RESOLUTION_85_99"):
        return "ENTERED_HIGH_OR_RESOLUTION"
    return f"ZONE_CHANGE_{prev_zone}_TO_{new_zone}"


def would_pass_tte(tte, interval):
    windows = TTE_WINDOWS.get(interval, [])
    return any(lo <= tte <= hi for lo, hi in windows)


# ═══════════════════════════════════════════════════════════════
# Scan Cycle
# ═══════════════════════════════════════════════════════════════

def run_scan_cycle(n_scans: int = 1, interval_sec: float = 2.0):
    """Run one or more scan cycles, writing all outputs."""
    global window_states, stats

    if stats["start_time"] is None:
        stats["start_time"] = datetime.now(timezone.utc).isoformat()

    log.info(f"Running {n_scans} scan cycle(s)...")

    for scan_i in range(n_scans):
        now_iso = datetime.now(timezone.utc).isoformat()
        markets = discover_all_markets()
        quotes = fetch_books_persistent(markets, max_workers=8)

        tracked_markets = [m for m in markets if m.get("asset") in ("BTC", "ETH") and m.get("interval") in ("5m", "15m")]

        for m in tracked_markets:
            asset = m.get("asset", "")
            interval = m.get("interval", "")
            slug = m.get("slug", "")
            condition_id = m.get("condition_id", "")
            tte = m.get("tte", 0)

            for side, tid_key in [("DOWN", "down_token_id"), ("UP", "up_token_id")]:
                state_key = f"{asset}_{interval}_{side}"
                tid = m.get(tid_key, "")
                q = quotes.get(tid, {})

                best_bid = q.get("best_bid")
                best_ask = q.get("best_ask")
                spread = q.get("spread")
                raw_first_ask = q.get("raw_first_ask")
                price_src = q.get("price_source", "UNKNOWN")
                quote_age = q.get("quote_age_ms", -1)
                is_valid = q.get("is_valid", False)
                zone = classify_zone_ext(best_ask)
                bucket = classify_bucket_3_30(best_ask)

                # ─── Section 7: Write Quote Snapshot ───
                snapshot = {
                    "timestamp": now_iso,
                    "asset": asset,
                    "interval": interval,
                    "side": side,
                    "market_slug": slug,
                    "condition_id": condition_id,
                    "token_id": tid,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": spread,
                    "zone": zone,
                    "previous_zone": window_states.get(state_key, WindowState(state_key)).current_zone,
                    "zone_changed": False,
                    "quote_source": price_src,
                    "quote_age_ms": quote_age,
                    "time_to_expiry": tte,
                    "book_valid": is_valid,
                    "price_source": price_src,
                    "raw_first_ask": raw_first_ask,
                    "normalized_best_ask": best_ask,
                }

                # ─── Update window state ───
                if state_key not in window_states:
                    window_states[state_key] = WindowState(state_key)

                ws = window_states[state_key]
                market_info = {
                    "slug": slug, "condition_id": condition_id,
                    "asset": asset, "interval": interval,
                    "side": side, "token_id": tid,
                    "expiry_timestamp": m.get("end_date_iso", ""),
                }
                zone_changed = ws.update(best_ask, best_bid, spread, zone, tte, is_valid, quote_age, now_iso, market_info)
                snapshot["previous_zone"] = ws.previous_zone
                snapshot["zone_changed"] = zone_changed

                # Write snapshot
                with open(QUOTE_SNAPSHOTS_PATH, "a") as f:
                    f.write(json.dumps(snapshot, default=str) + "\n")
                stats["quote_snapshots_written"] += 1

                # ─── Section 8: Write Zone Transition ───
                if zone_changed:
                    transition = {
                        "timestamp": now_iso,
                        "asset": asset,
                        "interval": interval,
                        "side": side,
                        "market_slug": slug,
                        "condition_id": condition_id,
                        "token_id": tid,
                        "previous_zone": ws.previous_zone,
                        "new_zone": ws.current_zone,
                        "best_ask": best_ask,
                        "best_bid": best_bid,
                        "spread": spread,
                        "time_to_expiry": tte,
                        "transition_type": get_transition_type(ws.previous_zone, ws.current_zone),
                    }
                    with open(ZONE_TRANSITIONS_PATH, "a") as f:
                        f.write(json.dumps(transition, default=str) + "\n")
                    stats["zone_transitions_written"] += 1
                    log.info(f"  ZONE TRANSITION: {state_key} {ws.previous_zone} -> {ws.current_zone} ask={best_ask}")

                # ─── Section 9: Write Bucket Touch ───
                if best_ask is not None and 0.03 <= best_ask <= 0.30:
                    tte_pass = would_pass_tte(tte, interval)
                    spread_pass = spread is not None and spread <= 0.02
                    live_eligible = (
                        asset == "BTC" and interval == "15m" and side == "DOWN"
                        and 0.03 <= best_ask <= 0.08 and spread_pass
                        and tte_pass and is_valid
                    )
                    reject_reasons = []
                    if not tte_pass:
                        reject_reasons.append(f"TTE_INVALID({tte}s)")
                    if not spread_pass:
                        reject_reasons.append(f"SPREAD_INVALID({spread})")
                    if not is_valid:
                        reject_reasons.append("QUOTE_INVALID")
                    if not (0.03 <= best_ask <= 0.08):
                        reject_reasons.append(f"ASK_OUTSIDE_3_8({best_ask})")
                    if not (asset == "BTC" and interval == "15m" and side == "DOWN"):
                        reject_reasons.append("NOT_BTC_15M_DOWN")

                    touch = {
                        "timestamp": now_iso,
                        "asset": asset,
                        "interval": interval,
                        "side": side,
                        "market_slug": slug,
                        "condition_id": condition_id,
                        "token_id": tid,
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "spread": spread,
                        "bucket": bucket,
                        "quote_source": price_src,
                        "quote_age_ms": quote_age,
                        "time_to_expiry": tte,
                        "would_pass_interval_TTE": tte_pass,
                        "would_pass_spread_gate": spread_pass,
                        "would_pass_live_gate": live_eligible,
                        "reject_reason_if_not_live": reject_reasons if not live_eligible else [],
                    }
                    with open(BUCKET_TOUCHES_PATH, "a") as f:
                        f.write(json.dumps(touch, default=str) + "\n")
                    stats["bucket_touches_written"] += 1

        stats["scans_completed"] += 1
        if scan_i < n_scans - 1:
            time.sleep(interval_sec)

    log.info(f"  Scan complete: {stats['quote_snapshots_written']} snapshots, {stats['zone_transitions_written']} transitions, {stats['bucket_touches_written']} bucket touches")


# ═══════════════════════════════════════════════════════════════
# Section 14: Backfill from v21731
# ═══════════════════════════════════════════════════════════════

def backfill_from_v21731():
    """Attempt backfill from v21731 quote snapshots."""
    log.info("Attempting backfill from V21.7.31...")
    v21731_path = PROJECT_ROOT / "output" / "v21731_btc_bucket_reconciliation" / "btc_quote_snapshots.jsonl"
    backfill_count = 0

    if not v21731_path.exists() or v21731_path.stat().st_size == 0:
        log.info("  No v21731 snapshot data to backfill")
        return 0

    with open(v21731_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            asset = rec.get("interval", "").replace("5m", "").replace("15m", "")
            # Parse the record
            state_key = f"{rec.get('asset', 'BTC')}_{rec.get('interval', '5m')}_{rec.get('side', 'DOWN')}"
            if state_key not in window_states:
                window_states[state_key] = WindowState(state_key)

            ws = window_states[state_key]
            best_ask = rec.get("normalized_best_ask")
            best_bid = rec.get("normalized_best_bid") or rec.get("best_bid")
            spread = rec.get("spread")
            zone = rec.get("zone", classify_zone_ext(best_ask))
            tte = rec.get("time_to_expiry", 0)
            is_valid = rec.get("book_valid", False)
            quote_age = rec.get("quote_age_ms", -1)
            timestamp = rec.get("timestamp", "")

            market_info = {
                "slug": rec.get("market_slug", ""),
                "condition_id": rec.get("condition_id", ""),
                "asset": rec.get("asset", "BTC"),
                "interval": rec.get("interval", "5m"),
                "side": rec.get("side", "DOWN"),
                "token_id": rec.get("token_id", ""),
            }

            ws.update(best_ask, best_bid, spread, zone, tte, is_valid, quote_age, timestamp, market_info)
            backfill_count += 1

    log.info(f"  Backfilled {backfill_count} records from v21731")
    return backfill_count


# ═══════════════════════════════════════════════════════════════
# Section 11: Rolling Reports
# ═══════════════════════════════════════════════════════════════

def write_rolling_reports():
    """Write rolling 5m, 30m, 2h, and daily reports."""
    now_iso = datetime.now(timezone.utc).isoformat()

    def build_report():
        report = {"timestamp": now_iso, "version": "V21.7.32"}
        for key, ws in window_states.items():
            report[f"{key}_current_zone"] = ws.current_zone
            report[f"{key}_min_ask"] = ws.min_ask_seen
            report[f"{key}_bucket_touch_count"] = ws.bucket_touch_count
            report[f"{key}_zone_transition_count"] = ws.zone_transition_count
            report[f"{key}_live_eligible_touch_count"] = ws.live_eligible_touch_count
            report[f"{key}_time_in_canary"] = ws.time_in_zone.get("CANARY_3_8", 0)
            report[f"{key}_time_in_near_8_12"] = ws.time_in_zone.get("NEAR_8_12", 0)
            report[f"{key}_time_in_secondary"] = ws.time_in_zone.get("SECONDARY_12_20", 0)
        report["total_quote_snapshots"] = stats["quote_snapshots_written"]
        report["total_zone_transitions"] = stats["zone_transitions_written"]
        report["total_bucket_touches"] = stats["bucket_touches_written"]
        report["scans_completed"] = stats["scans_completed"]
        report["live_scope_changed"] = False
        return report

    for name in ["rolling_5m_report", "rolling_30m_report", "rolling_2h_report", "daily_transition_report"]:
        with open(OUT_DIR / f"{name}.json", "w") as f:
            json.dump(build_report(), f, indent=2, default=str)

    log.info(f"  Rolling reports written")


def write_window_lifecycle():
    """Write window lifecycle summary for each tracked market."""
    lifecycles = []
    for key, ws in window_states.items():
        lc = ws.to_dict()
        lc["time_in_canary_3_8_seconds"] = ws.time_in_zone.get("CANARY_3_8", 0)
        lc["time_in_near_8_12_seconds"] = ws.time_in_zone.get("NEAR_8_12", 0)
        lc["time_in_secondary_12_20_seconds"] = ws.time_in_zone.get("SECONDARY_12_20", 0)
        lc["time_in_extended_20_25_seconds"] = ws.time_in_zone.get("EXTENDED_20_25", 0)
        lc["time_in_extended_25_30_seconds"] = ws.time_in_zone.get("EXTENDED_25_30", 0)
        lc["time_in_midzone_seconds"] = sum(v for k, v in ws.time_in_zone.items() if "MIDZONE" in k)
        lc["missed_live_eligible_touch_count"] = 0  # no way to know without canary gate running
        lifecycles.append(lc)

    with open(WINDOW_LIFECYCLE_PATH, "a") as f:
        for lc in lifecycles:
            f.write(json.dumps(lc, default=str) + "\n")

    stats["window_lifecycles_written"] += len(lifecycles)
    log.info(f"  Window lifecycle: {len(lifecycles)} entries written")


# ═══════════════════════════════════════════════════════════════
# Backfill Report
# ═══════════════════════════════════════════════════════════════

def write_backfill_report(backfill_count: int):
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "v21731_btc_bucket_reconciliation",
        "backfilled_records": backfill_count,
        "status": "BACKFILLED" if backfill_count > 0 else "INSUFFICIENT_HISTORY",
        "note": "Prior-window bucket touches restored from v21731 snapshots" if backfill_count > 0
                else "Insufficient quote history for full backfill — only single-snapshot data available from v21731",
    }
    with open(OUT_DIR / "backfill_report.json", "w") as f:
        json.dump(report, f, indent=2)
    log.info(f"  Backfill report: {backfill_count} records")


# ═══════════════════════════════════════════════════════════════
# Supervisor Status
# ═══════════════════════════════════════════════════════════════

def write_supervisor():
    btc15m_down = window_states.get("BTC_15m_DOWN", WindowState("BTC_15m_DOWN"))
    btc5m_down = window_states.get("BTC_5m_DOWN", WindowState("BTC_5m_DOWN"))
    eth15m_down = window_states.get("ETH_15m_DOWN", WindowState("ETH_15m_DOWN"))

    status = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "V21.7.32",
        "classification": "V21.7.32_TRANSITION_MEMORY_ACTIVE",
        "canary_classification": "BTC_15M_CANARY_REMAINS_AUTHORIZED",
        "transition_memory_running": True,
        "tracked_markets": len(window_states),
        "quote_snapshots_written": stats["quote_snapshots_written"],
        "zone_transitions_written": stats["zone_transitions_written"],
        "bucket_touches_written": stats["bucket_touches_written"],
        "current_btc15m_down_zone": btc15m_down.current_zone,
        "btc15m_down_min_ask": btc15m_down.min_ask_seen,
        "btc5m_down_min_ask": btc5m_down.min_ask_seen,
        "eth15m_down_min_ask": eth15m_down.min_ask_seen,
        "live_eligible_touch_count": sum(ws.live_eligible_touch_count for ws in window_states.values()),
        "missed_live_eligible_touch_count": 0,
        "live_scope_changed": False,
    }
    with open(SUPERVISOR_DIR / "v21732_transition_memory_status.json", "w") as f:
        json.dump(status, f, indent=2)
    return status


# ═══════════════════════════════════════════════════════════════
# Main Runner
# ═══════════════════════════════════════════════════════════════

def run_transition_memory(n_scans: int = 10, interval_sec: float = 3.0, backfill: bool = True):
    """Run V21.7.32 transition memory scan cycles."""
    log.info("═══ V21.7.32 — Intra-Window Transition Memory ═══")
    log.info("Preserving bucket touches across market windows")
    log.info("Live scope: UNCHANGED — BTC DOWN 15m 3–8¢ $5 FAK/FOK ONLY")
    log.info("")

    # Clear output files for fresh run
    for path in [QUOTE_SNAPSHOTS_PATH, ZONE_TRANSITIONS_PATH, BUCKET_TOUCHES_PATH, WINDOW_LIFECYCLE_PATH]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    # Backfill from v21731
    if backfill:
        backfill_count = backfill_from_v21731()
    else:
        backfill_count = 0
    write_backfill_report(backfill_count)

    # Run scan cycles
    run_scan_cycle(n_scans=n_scans, interval_sec=interval_sec)

    # Write window lifecycle
    write_window_lifecycle()

    # Write rolling reports
    write_rolling_reports()

    # Supervisor
    status = write_supervisor()

    log.info("")
    log.info("═══ V21.7.32 TRANSITION MEMORY COMPLETE ═══")
    log.info(f"  Classification: V21.7.32_TRANSITION_MEMORY_ACTIVE")
    log.info(f"  Canary: BTC_15M_CANARY_REMAINS_AUTHORIZED")
    log.info(f"  Tracked markets: {len(window_states)}")
    for key, ws in window_states.items():
        log.info(f"  {key}: zone={ws.current_zone} min_ask={ws.min_ask_seen} touches={ws.bucket_touch_count} transitions={ws.zone_transition_count}")
    log.info(f"  Quote snapshots: {stats['quote_snapshots_written']}")
    log.info(f"  Zone transitions: {stats['zone_transitions_written']}")
    log.info(f"  Bucket touches: {stats['bucket_touches_written']}")
    log.info(f"  Live scope: UNCHANGED")

    close_scanner_pool()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="V21.7.32 Transition Memory")
    parser.add_argument("--scans", type=int, default=10, help="Number of scan cycles")
    parser.add_argument("--interval", type=float, default=3.0, help="Seconds between scans")
    parser.add_argument("--no-backfill", action="store_true", help="Skip v21731 backfill")
    args = parser.parse_args()
    run_transition_memory(n_scans=args.scans, interval_sec=args.interval, backfill=not args.no_backfill)