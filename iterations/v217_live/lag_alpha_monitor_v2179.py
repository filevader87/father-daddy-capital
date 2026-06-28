#!/usr/bin/env python3
"""
V21.7.9 Lag Alpha Monitor — §7/8
=================================
Detects whether external exchange prices move before
Polymarket token prices reprice. Shadow-only diagnostics.

Reads from the shared QuoteCache. No orders.
"""

import json, time, logging, asyncio, signal, sys
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "v2179_ws"
OUT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(OUT / "lag_alpha.log"),
                              logging.StreamHandler()])
log = logging.getLogger("lag2179")

# §8: Lag event thresholds
MIN_EXTERNAL_MOVE_BPS = 40
MIN_CONFIRMING_SOURCES = 2
REPRICE_WINDOW_MS = 3000
MIN_TTE_S = 60
MAX_SPREAD = 0.03
MIN_DEPTH_USD = 1.0


class LagAlphaMonitor:
    def __init__(self, cache, check_interval_s=2):
        self.cache = cache
        self.interval = check_interval_s
        self.baselines = {}  # asset -> last_stable_mid
        self.move_tracking = {}  # asset -> {start_ms, start_mid, move_bps, confirming}
        self.pm_baselines = {}  # token_id -> last_price
        self.events = []
        self.total_checks = 0
        self.total_lag_detected = 0
        self.start_time = time.time()

    def check_asset(self, asset: str):
        """Check one asset for external move + PM repricing lag."""
        self.total_checks += 1
        now_ms = int(time.time() * 1000)

        snap = self.cache.get_external_snapshot(asset)
        if not snap or not snap.get("sources"):
            return

        # Get fresh sources only
        fresh = {s: q for s, q in snap["sources"].items() if not q.get("stale")}
        if len(fresh) < MIN_CONFIRMING_SOURCES:
            return

        # Compute cross-exchange median mid
        mids = [q["mid"] for q in fresh.values() if q.get("mid", 0) > 0]
        if not mids:
            return
        current_mid = float(np.median(mids))

        # Baseline tracking
        prev = self.baselines.get(asset)
        if prev is None:
            self.baselines[asset] = current_mid
            self.move_tracking.pop(asset, None)
            return

        move_bps = abs(current_mid - prev) / prev * 10000 if prev > 0 else 0

        # ── No significant move → reset baseline ──
        if move_bps < MIN_EXTERNAL_MOVE_BPS / 2:
            self.baselines[asset] = current_mid
            self.move_tracking.pop(asset, None)
            return

        # ── Significant move detected ──
        if move_bps >= MIN_EXTERNAL_MOVE_BPS:
            confirming = len(fresh)
            direction = "DOWN" if current_mid < prev else "UP"

            if asset not in self.move_tracking:
                self.move_tracking[asset] = dict(
                    start_ms=now_ms, start_mid=prev,
                    move_bps=move_bps, confirming=confirming,
                    direction=direction, peak_mid=current_mid,
                    peak_ms=now_ms,
                )
            else:
                tr = self.move_tracking[asset]
                tr["move_bps"] = move_bps
                tr["confirming"] = confirming
                tr["direction"] = direction
                # Track peak
                if direction == "DOWN" and current_mid < tr["peak_mid"]:
                    tr["peak_mid"] = current_mid
                    tr["peak_ms"] = now_ms
                elif direction == "UP" and current_mid > tr["peak_mid"]:
                    tr["peak_mid"] = current_mid
                    tr["peak_ms"] = now_ms

            # ── Check Polymarket repricing ──
            self._check_pm_repricing(asset, now_ms)

        else:
            # Move fading — check if PM has caught up, then close event
            tr = self.move_tracking.get(asset)
            if tr:
                self._finalize_event(asset, now_ms, "move_faded")

    def _check_pm_repricing(self, asset: str, now_ms: int):
        """Check if Polymarket tokens have repriced after external move."""
        tr = self.move_tracking.get(asset)
        if not tr:
            return

        direction = tr["direction"]
        # Look for relevant PM tokens (DOWN tokens for DOWN direction, UP for UP)
        # In practice: DOWN external move = DOWN token should appreciate
        target_side = "Down" if direction == "DOWN" else "Up"

        # Get all PM books from cache
        # We need to find tokens for this asset — check cache
        pm_data = {}
        with self.cache._lock:
            for tid, book in self.cache._pm.items():
                if book.get("asset", "") == asset or asset in book.get("slug", ""):
                    pm_data[tid] = book

        if not pm_data:
            # No PM data for this asset yet
            return

        for tid, book in pm_data.items():
            pm_price = book.get("best_bid", 0)
            if pm_price <= 0:
                continue

            prev_pm = self.pm_baselines.get(tid)
            if prev_pm is None:
                self.pm_baselines[tid] = pm_price
                continue

            pm_move = (pm_price - prev_pm) / prev_pm * 100 if prev_pm > 0 else 0

            # Expected: external DOWN move → DOWN token goes UP (more likely to settle 1.0)
            expected_pm_move = (direction == "DOWN" and pm_price > prev_pm) or \
                               (direction == "UP" and pm_price < prev_pm)

            if expected_pm_move and pm_move > 1:
                # PM has repriced — record lag event
                delay_ms = now_ms - tr["start_ms"]
                self._record_event(asset, tid, tr, book, delay_ms, pm_price,
                                   prev_pm, True, now_ms)
                self.pm_baselines[tid] = pm_price
            elif now_ms - tr["start_ms"] > REPRICE_WINDOW_MS:
                # PM hasn't repriced within window — this IS the lag alpha
                delay_ms = now_ms - tr["start_ms"]
                self._record_event(asset, tid, tr, book, delay_ms, pm_price,
                                   prev_pm, True, now_ms, lag_confirmed=True)

            # Update PM baseline
            self.pm_baselines[tid] = pm_price

    def _record_event(self, asset, token_id, tr, book, delay_ms,
                      pm_after, pm_before, reprice_detected, now_ms,
                      lag_confirmed=False):
        """Record a lag event per §7."""
        # Entry bucket classification
        pm_ask = book.get("best_ask", 0)
        if 0.03 <= pm_ask <= 0.05:
            bucket = "03_05"
        elif 0.05 < pm_ask <= 0.12:
            bucket = "05_12"
        elif 0.12 < pm_ask <= 0.20:
            bucket = "12_20"
        else:
            bucket = "outside"

        spread = book.get("spread", 0)
        depth = book.get("bid_depth", 0) + book.get("ask_depth", 0)
        tte = 300  # Approximate for 5m markets

        event = dict(
            event_id=f"LAG-{int(now_ms)}-{abs(hash(token_id)) % 10000:04d}",
            asset=asset, interval="5m",
            external_move_start_ms=tr["start_ms"],
            external_move_end_ms=tr["peak_ms"],
            external_move_bps=round(tr["move_bps"], 1),
            external_sources_confirming=tr["confirming"],
            external_direction=tr["direction"],
            polymarket_token_id=token_id,
            polymarket_token_before=round(pm_before, 4),
            polymarket_token_after=round(pm_after, 4),
            polymarket_reprice_start_ms=tr["start_ms"],
            polymarket_reprice_end_ms=now_ms if reprice_detected else 0,
            repricing_delay_ms=delay_ms,
            quote_age_ms=book.get("book_age_ms", 0),
            spread=round(spread, 4),
            depth=round(depth, 6),
            time_to_expiry_s=tte,
            side="DOWN" if tr["direction"] == "DOWN" else "UP",
            entry_bucket=bucket,
            lag_confirmed=lag_confirmed or (delay_ms > 200 and not reprice_detected),
            best_ask=pm_ask,
            best_bid=book.get("best_bid", 0),
            bid_depth=round(book.get("bid_depth", 0), 6),
            ask_depth=round(book.get("ask_depth", 0), 6),
        )
        self.events.append(event)
        self.total_lag_detected += 1

        # Append to JSONL
        with open(OUT / "lag_alpha_events.jsonl", "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

        log.info(f"LAG: {event['event_id']} {asset} {tr['direction']} "
                 f"move={tr['move_bps']:.0f}bps delay={delay_ms}ms "
                 f"lag={event['lag_confirmed']} bucket={bucket}")

        # Reset tracking
        self.move_tracking.pop(asset, None)

    def _finalize_event(self, asset, now_ms, reason):
        tr = self.move_tracking.get(asset)
        if not tr:
            return
        self.move_tracking.pop(asset, None)

    def generate_report(self):
        """§7: lag_alpha_report.json"""
        n = len(self.events)
        runtime_h = (time.time() - self.start_time) / 3600
        if n > 0:
            delays = [e["repricing_delay_ms"] for e in self.events]
            moves = [e["external_move_bps"] for e in self.events]
            lag_ok = [e for e in self.events if e["lag_confirmed"]]
            buckets = defaultdict(int)
            for e in self.events:
                buckets[e["entry_bucket"]] += 1
            directions = defaultdict(int)
            for e in self.events:
                directions[e["side"]] += 1
            report = dict(
                timestamp=datetime.now(timezone.utc).isoformat(),
                total_events=n, total_checks=self.total_checks,
                lag_confirmed_count=len(lag_ok),
                runtime_hours=round(runtime_h, 2),
                median_repricing_delay_ms=int(np.median(delays)),
                p95_repricing_delay_ms=int(np.percentile(delays, 95)),
                median_external_move_bps=round(float(np.median(moves)), 1),
                bucket_breakdown=dict(buckets),
                direction_breakdown=dict(directions),
                events_per_hour=round(n / max(runtime_h, 0.01), 2),
            )
        else:
            report = dict(
                timestamp=datetime.now(timezone.utc).isoformat(),
                total_events=0, total_checks=self.total_checks,
                lag_confirmed_count=0,
                runtime_hours=round(runtime_h, 2),
                note="No lag events detected yet — market may be flat",
            )
        with open(OUT / "lag_alpha_report.json", "w") as f:
            json.dump(report, f, indent=2)
        log.info(f"Lag report: {n} events, {self.total_checks} checks, "
                 f"{runtime_h:.1f}h runtime")
        return report


async def run_monitor(cache):
    """Main loop for lag alpha monitor."""
    monitor = LagAlphaMonitor(cache)
    log.info("Lag Alpha Monitor v2179 STARTING")
    log.info(f"Check interval: {monitor.interval}s")
    log.info(f"Min external move: {MIN_EXTERNAL_MOVE_BPS}bps")
    log.info(f"Min confirming sources: {MIN_CONFIRMING_SOURCES}")

    while True:
        for asset in ["BTC", "ETH", "SOL", "XRP"]:
            try:
                monitor.check_asset(asset)
            except Exception as e:
                log.error(f"Lag check {asset}: {e}")

        await asyncio.sleep(monitor.interval)

        # Periodic report
        if monitor.total_checks % 150 == 0 and monitor.total_checks > 0:
            monitor.generate_report()


if __name__ == "__main__":
    # Standalone test — needs a cache with data
    from quote_cache import QuoteCache
    cache = QuoteCache()
    log.info("Running standalone — feed layer must be running separately")
    try:
        asyncio.run(run_monitor(cache))
    except KeyboardInterrupt:
        pass