#!/usr/bin/env python3
"""
V21.7.8 Scalper Paper-Live Simulator
=====================================
Profile: DOWN_lag_bucket_03_05
Mode: PAPER_LIVE_SIM — NO REAL ORDERS

Validates whether the strongest V21.7.7 subprofile survives
real live bid/ask, quote age, depth, spread, exit liquidity.

ISOLATED from convex bot. Separate state/reports.
"""

import json, os, time, sys, logging, traceback, gc, signal
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict
import urllib.request

# ═══════════════════════════════════════════════════════════════════════
# §17: MODE INTEGRITY
# ═══════════════════════════════════════════════════════════════════════
EXECUTION_MODE = "PAPER_LIVE_SIM"
REAL_ORDER_SUBMISSION_ENABLED = False
CLOB_TRADE_SUBMISSION_DISABLED = True
WALLET_REQUIRED = False

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT_DIR = BASE / "output" / "v2178_scalper_paper_live"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# §3: Profile constraints
ALLOWED_ASSETS = ["BTC", "ETH", "SOL", "XRP"]
ALLOWED_SIDE = "DOWN"
BUCKET_LO = 0.03
BUCKET_HI = 0.05
POSITION_SIZE = 1.0
MAX_OPEN = 1
MAX_DAILY_LOSS = 5.0
MAX_WEEKLY_LOSS = 15.0
MAX_DAILY_TRADES = 10

# §10: Exit params
TP_ABS_LO, TP_ABS_HI = 0.015, 0.030
TP_REL_LO, TP_REL_HI = 0.35, 0.80
SL_ABS_LO, SL_ABS_HI = 0.010, 0.020
MAX_HOLD_S = 60
FORCE_EXIT_BEFORE_EXPIRY_S = 45

# §7: Entry gates
MAX_QUOTE_AGE_MS = 1000
MAX_SPREAD = 0.02
MIN_TTE_S = 60

# §16: Runtime limits
MAX_EXITS = 100
MAX_RUNTIME_H = 24
SCAN_INTERVAL_S = 12  # 10-15s per §directive

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(OUT_DIR / "sim.log"),
                              logging.StreamHandler()])
log = logging.getLogger("v2178")


# ═══════════════════════════════════════════════════════════════════════
# LIVE DATA FETCHES
# ═══════════════════════════════════════════════════════════════════════

def fetch_json(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-v2178"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.debug(f"fetch err {url}: {e}")
        return None


def fetch_orderbook(token_id):
    url = f"{CLOB_URL}/book?token_id={token_id}"
    data = fetch_json(url)
    if not data:
        return None
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    if not bids or not asks:
        return None
    # CLOB API returns asks DESCENDING, bids ASCENDING — sort for best prices
    sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
    sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 1)))
    best_bid = float(sorted_bids[0].get("price", 0)) if sorted_bids else 0
    best_ask = float(sorted_asks[0].get("price", 0)) if sorted_asks else 0
    bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
    ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
    return dict(best_bid=best_bid, best_ask=best_ask,
                bid_depth=bid_depth, ask_depth=ask_depth,
                spread=round(best_ask - best_bid, 4),
                timestamp_ms=int(time.time() * 1000))


def fetch_5m_markets(asset):
    slug = f"{asset.lower()}-updown-5m"
    url = f"{GAMMA_URL}/markets?limit=20&slug={slug}&active=true"
    data = fetch_json(url)
    markets = []
    if data and isinstance(data, list):
        for m in data:
            tokens = m.get("tokens", [])
            if len(tokens) < 2:
                continue
            markets.append(dict(
                condition_id=m.get("condition_id", ""),
                slug=m.get("slug", slug),
                tokens=tokens,
                end_date=m.get("end_date_iso", ""),
                asset=asset,
            ))
    return markets


def fetch_spot(asset):
    sym_map = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={sym_map.get(asset, asset+'USDT')}"
    data = fetch_json(url)
    return float(data["price"]) if data and "price" in data else None


# ═══════════════════════════════════════════════════════════════════════
# SPOT TRACKER + LAG DETECTION
# ═══════════════════════════════════════════════════════════════════════

class SpotTracker:
    def __init__(self, window=120):
        self.window = window
        self.prices = defaultdict(list)

    def update(self, asset, price):
        now = time.time()
        self.prices[asset].append((now, price))
        cutoff = now - self.window
        self.prices[asset] = [(t, p) for t, p in self.prices[asset] if t > cutoff]

    def detect_reversal(self, asset, min_move=40, min_snap=25):
        pts = self.prices.get(asset, [])
        if len(pts) < 5:
            return dict(reversal=False, direction=None, snapback_bps=0, move_bps=0)
        ts = np.array([p[0] for p in pts])
        pr = np.array([p[1] for p in pts])
        mask = ts >= (ts[-1] - 60)
        wp = pr[mask]
        if len(wp) < 5:
            return dict(reversal=False, direction=None, snapback_bps=0, move_bps=0)
        cur, base = wp[-1], wp[0]
        if base <= 0:
            return dict(reversal=False, direction=None, snapback_bps=0, move_bps=0)

        result = dict(reversal=False, direction=None, snapback_bps=0, move_bps=0)
        # DOWN reversal: pump→rollover
        mx_i = np.argmax(wp)
        if mx_i < len(wp) - 3:
            ep = wp[mx_i]
            mb = (ep - base) / base * 10000
            sb = (ep - cur) / ep * 10000
            if mb >= min_move and sb >= min_snap:
                result = dict(reversal=True, direction="DOWN", snapback_bps=sb, move_bps=mb)
        # UP reversal: dump→bounce
        mn_i = np.argmin(wp)
        if mn_i < len(wp) - 3:
            ep = wp[mn_i]
            mb = (base - ep) / base * 10000
            sb = (cur - ep) / ep * 10000
            if mb >= min_move and sb >= min_snap:
                if not result["reversal"] or sb > result["snapback_bps"]:
                    result = dict(reversal=True, direction="UP", snapback_bps=sb, move_bps=mb)
        return result

    def compute_lag(self, asset, pm_price, pm_ts_ms):
        rev = self.detect_reversal(asset)
        if not rev["reversal"]:
            return dict(lag_confirmed=False, reprice_delay_ms=0, lag_edge_bps=0,
                        external_move_bps=0, external_direction="")
        now_ms = int(time.time() * 1000)
        delay = max(0, now_ms - pm_ts_ms)
        expected = rev["snapback_bps"] * 0.5
        lag_ok = (expected > 20 and delay >= 200
                  and rev["direction"] == "DOWN")
        return dict(lag_confirmed=lag_ok, reprice_delay_ms=delay,
                    lag_edge_bps=max(0, expected * 0.5),
                    external_move_bps=rev["move_bps"],
                    external_snapback_bps=rev["snapback_bps"],
                    external_direction=rev["direction"])


# ═══════════════════════════════════════════════════════════════════════
# PAPER POSITION + SIMULATOR
# ═══════════════════════════════════════════════════════════════════════

class PaperPosition:
    def __init__(self, eid, entry_price, entry_ts, shares, expiry_ts,
                 asset, token_id, tp, sl):
        self.event_id = eid
        self.entry_price = entry_price
        self.entry_ts_ms = entry_ts
        self.shares = shares
        self.expiry_ts_ms = expiry_ts
        self.asset = asset
        self.token_id = token_id
        self.tp_price = tp
        self.sl_price = sl
        self.force_exit_ts = expiry_ts - FORCE_EXIT_BEFORE_EXPIRY_S * 1000
        self.max_exit_ts = entry_ts + MAX_HOLD_S * 1000
        self.exit_limit_ts = min(self.force_exit_ts, self.max_exit_ts)
        self.closed = False
        self.exit_reason = ""
        self.exit_price = 0.0
        self.exit_success = False
        self.exit_failure_reason = ""

    def check_exit(self, best_bid, bid_depth, now_ms, spread):
        if self.closed:
            return None
        # Timeout / forced exit
        if now_ms > self.exit_limit_ts:
            reason = "pre_expiry_forced" if now_ms > self.force_exit_ts else "timeout"
            fail = "NO_EXIT_DEPTH" if bid_depth < self.shares else ""
            return dict(reason=reason, price=best_bid, success=(best_bid > self.entry_price and not fail),
                        failure=fail)
        # Take profit
        if best_bid >= self.tp_price:
            fail = "NO_EXIT_DEPTH" if bid_depth < self.shares else ""
            return dict(reason="take_profit", price=best_bid, success=(not fail), failure=fail)
        # Stop loss
        if best_bid <= self.sl_price:
            fail = "NO_EXIT_DEPTH" if bid_depth < self.shares else ""
            return dict(reason="stop_loss", price=best_bid, success=False, failure=fail)
        # Bid evaporated check
        if best_bid <= 0.001:
            return dict(reason="stop_loss", price=0.001, success=False, failure="BID_EVAPORATED")
        return None


class Simulator:
    def __init__(self):
        self.state = dict(
            mode=EXECUTION_MODE, real_orders_enabled=False,
            profile="DOWN_lag_bucket_03_05",
            entries=0, exits=0, settlements=0,
            bankroll=0.0, daily_pnl=0.0, weekly_pnl=0.0, daily_trades=0,
            last_daily_reset=time.time(), last_weekly_reset=time.time(),
            open_position=None, start_time=time.time(),
            classification="V21.7.8_SCALPER_PAPER_LIVE_SIMULATOR_RUNNING",
        )
        self.spot = SpotTracker()
        self.entries_log = []
        self.exits_log = []
        self.settles_log = []
        self.rejections = Counter()
        self.cycle = 0
        self.shutting_down = False

    def verify_integrity(self):
        checks = [
            ("mode", EXECUTION_MODE == "PAPER_LIVE_SIM"),
            ("real_disabled", not REAL_ORDER_SUBMISSION_ENABLED),
            ("clob_disabled", CLOB_TRADE_SUBMISSION_DISABLED),
            ("size_$1", POSITION_SIZE == 1.0),
            ("max_open_1", MAX_OPEN == 1),
            ("down_only", ALLOWED_SIDE == "DOWN"),
            ("bucket_3_5", BUCKET_LO == 0.03 and BUCKET_HI == 0.05),
        ]
        ok = all(v for _, v in checks)
        if ok:
            log.info("§17 MODE INTEGRITY: PASSED")
        else:
            for k, v in checks:
                if not v:
                    log.error(f"§17 FAILED: {k}")
        return ok

    def reset_daily(self):
        now = time.time()
        if now - self.state["last_daily_reset"] > 86400:
            self.state["daily_pnl"] = 0.0
            self.state["daily_trades"] = 0
            self.state["last_daily_reset"] = now
        if now - self.state["last_weekly_reset"] > 604800:
            self.state["weekly_pnl"] = 0.0
            self.state["last_weekly_reset"] = now

    def check_risk(self):
        self.reset_daily()
        if self.state["daily_pnl"] <= -MAX_DAILY_LOSS:
            return "DAILY_LOSS_LIMIT"
        if self.state["weekly_pnl"] <= -MAX_WEEKLY_LOSS:
            return "WEEKLY_LOSS_LIMIT"
        if self.state["daily_trades"] >= MAX_DAILY_TRADES:
            return "DAILY_TRADE_LIMIT"
        if self.state["open_position"] is not None:
            return "POSITION_OPEN"
        return None

    def scan(self):
        if self.shutting_down:
            return
        self.cycle += 1
        now_ms = int(time.time() * 1000)

        # Spot updates
        for asset in ALLOWED_ASSETS:
            p = fetch_spot(asset)
            if p:
                self.spot.update(asset, p)

        # Check existing position for exit
        pos = self.state["open_position"]
        if pos is not None:
            book = fetch_orderbook(pos.token_id)
            if book:
                result = pos.check_exit(book["best_bid"], book["bid_depth"],
                                        now_ms, book["spread"])
                if result is not None:
                    self._close(pos, result, book, now_ms)
            # If position held too long, force close on next check
            return

        # Risk check
        block = self.check_risk()
        if block:
            self.rejections[block] += 1
            return

        # Search for entries
        for asset in ALLOWED_ASSETS:
            rev = self.spot.detect_reversal(asset)
            if not rev["reversal"] or rev["direction"] != "DOWN":
                self.rejections["NO_REVERSAL"] += 1
                continue

            markets = fetch_5m_markets(asset)
            if not markets:
                self.rejections["NO_MARKET"] += 1
                continue

            for m in markets:
                for tok in m.get("tokens", []):
                    tid = tok.get("token_id", "")
                    if not tid:
                        continue
                    # Only check DOWN tokens (outcome token for DOWN side)
                    outcome = tok.get("outcome", "")
                    if outcome and outcome != "Down" and outcome.lower() != "down":
                        # Check by price instead — DOWN tokens are cheap
                        pass

                    book = fetch_orderbook(tid)
                    if not book:
                        self.rejections["NO_BOOK"] += 1
                        continue

                    ask = book["best_ask"]
                    # DOWN tokens are cheap; skip rich tokens
                    if ask > 0.50:
                        continue

                    # §7: All entry gates
                    if ask < BUCKET_LO or ask > BUCKET_HI:
                        self.rejections["OUTSIDE_BUCKET"] += 1
                        continue
                    if book["spread"] > MAX_SPREAD:
                        self.rejections["SPREAD"] += 1
                        continue

                    lag = self.spot.compute_lag(asset, ask, book["timestamp_ms"])
                    if not lag["lag_confirmed"]:
                        self.rejections["NO_LAG"] += 1
                        continue

                    ask_depth_usd = book["ask_depth"] * ask
                    if ask_depth_usd < POSITION_SIZE:
                        self.rejections["INSUFF_DEPTH"] += 1
                        continue

                    bid_shares = POSITION_SIZE / ask
                    if book["bid_depth"] < bid_shares:
                        self.rejections["NO_EXIT_LIQ"] += 1
                        continue

                    self._open(asset, tid, book, now_ms, lag, rev)
                    return

    def _open(self, asset, token_id, book, now_ms, lag, rev):
        raw = book["best_ask"]
        slip = raw * 0.005
        queue = raw * 0.003
        entry = round(raw + slip + queue, 6)
        shares = POSITION_SIZE / entry
        tp = min(entry + np.random.uniform(TP_ABS_LO, TP_ABS_HI),
                 entry * (1 + np.random.uniform(TP_REL_LO, TP_REL_HI)))
        sl = entry - np.random.uniform(SL_ABS_LO, SL_ABS_HI)
        expiry = now_ms + 300000
        eid = f"SP-{int(now_ms)}-{abs(hash(token_id)) % 10000:04d}"

        pos = PaperPosition(eid, entry, now_ms, shares, expiry,
                            asset, token_id, tp, sl)
        self.state["open_position"] = pos
        self.state["entries"] += 1
        self.state["daily_trades"] += 1

        rec = dict(event_id=eid,
                   timestamp=datetime.fromtimestamp(now_ms/1000, tz=timezone.utc).isoformat(),
                   asset=asset, interval="5m", side="DOWN",
                   entry_bucket="03_05",
                   raw_entry_price=raw,
                   haircut_adjusted_entry_price=entry,
                   spread=book["spread"], depth=round(book["ask_depth"], 6),
                   bid_depth=round(book["bid_depth"], 6),
                   quote_age_ms=round(int(time.time()*1000) - book["timestamp_ms"], 1),
                   tp_price=round(tp, 6), sl_price=round(sl, 6),
                   shares=round(shares, 4),
                   lag_confirmed=lag["lag_confirmed"],
                   lag_edge_bps=lag.get("lag_edge_bps", 0),
                   external_move_bps=lag.get("external_move_bps", 0),
                   external_reversal_direction=lag.get("external_direction", ""),
                   polymarket_reprice_delay_ms=lag.get("reprice_delay_ms", 0))
        self.entries_log.append(rec)
        log.info(f"ENTRY {eid}: {asset} DOWN @ {entry:.4f} TP={tp:.4f} SL={sl:.4f}")

    def _close(self, pos, result, book, now_ms):
        pos.closed = True
        pos.exit_reason = result["reason"]
        pos.exit_price = result.get("price", book["best_bid"])
        pos.exit_success = result.get("success", False)
        pos.exit_failure_reason = result.get("failure", "")

        hold_s = (now_ms - pos.entry_ts_ms) / 1000
        gross = (pos.exit_price - pos.entry_price) * pos.shares
        exit_slip = pos.exit_price * 0.005 * pos.shares
        net = gross - exit_slip
        if pos.exit_failure_reason:
            pos.exit_success = False

        self.state["exits"] += 1
        self.state["bankroll"] = round(self.state["bankroll"] + net, 4)
        self.state["daily_pnl"] = round(self.state["daily_pnl"] + net, 4)
        self.state["weekly_pnl"] = round(self.state["weekly_pnl"] + net, 4)
        self.state["open_position"] = None

        rec = dict(event_id=pos.event_id, entry_timestamp=pos.entry_ts_ms,
                   exit_timestamp=now_ms, hold_seconds=round(hold_s, 2),
                   exit_reason=pos.exit_reason,
                   entry_price=pos.entry_price,
                   exit_bid=book["best_bid"], exit_ask=book["best_ask"],
                   exit_price_used=round(pos.exit_price, 6),
                   exit_depth_available=round(book["bid_depth"], 6),
                   gross_pnl_unit=round(gross, 4),
                   slippage_adjusted_pnl_unit=round(net, 4),
                   exit_success=pos.exit_success,
                   exit_failure_reason=pos.exit_failure_reason)
        self.exits_log.append(rec)
        log.info(f"EXIT {pos.event_id}: {pos.exit_reason} @ {pos.exit_price:.4f} "
                 f"PnL={net:.4f} ok={pos.exit_success}")

    def settle_audit(self):
        for ex in self.exits_log:
            eid = ex["event_id"]
            if any(s["event_id"] == eid for s in self.settles_log):
                continue
            ent = next((e for e in self.entries_log if e["event_id"] == eid), None)
            if not ent:
                continue
            entry_price = ent["haircut_adjusted_entry_price"]
            # Proxy: final book price
            won = False
            binary_pnl = -entry_price
            settle_rec = dict(event_id=eid,
                              resolved_winner="UNKNOWN",
                              binary_win_loss="NO_MARKET_DATA",
                              binary_pnl_if_held=round(binary_pnl, 4),
                              paper_exit_pnl=ex["slippage_adjusted_pnl_unit"],
                              exit_vs_hold_delta=round(
                                  ex["slippage_adjusted_pnl_unit"] - binary_pnl, 4),
                              settlement_error="market_not_resolved_during_run")
            self.settles_log.append(settle_rec)

    def save(self):
        with open(OUT_DIR / "state.json", "w") as f:
            json.dump(self.state, f, indent=2, default=str)
        for name, records in [("paper_entries.jsonl", self.entries_log),
                               ("paper_exits.jsonl", self.exits_log),
                               ("paper_settlements.jsonl", self.settles_log)]:
            with open(OUT_DIR / name, "a") as f:
                for r in records:
                    f.write(json.dumps(r, default=str) + "\n")
        # Clear in-memory logs after save
        self.entries_log.clear()
        self.exits_log.clear()
        self.settles_log.clear()

    def reports(self):
        """Generate §13/14/15 reports."""
        # Read all accumulated exits
        all_exits = []
        ef = OUT_DIR / "paper_exits.jsonl"
        if ef.exists():
            with open(ef) as f:
                for line in f:
                    if line.strip():
                        all_exits.append(json.loads(line))

        n = len(all_exits)
        if n == 0:
            per_r = dict(status="collecting", exits=0, runtime_h=0)
            cls = "INSUFFICIENT_SAMPLE_COLLECTING"
        else:
            pnls = [e["slippage_adjusted_pnl_unit"] for e in all_exits]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            gp = sum(wins); gl = abs(sum(losses))
            pf = gp / max(gl, 0.01)
            ev = sum(pnls) / n
            es = sum(1 for e in all_exits if e["exit_success"])
            exit_rate = es / n * 100
            wr = len(wins) / n * 100
            cumul = np.cumsum(pnls)
            peak = np.maximum.accumulate(cumul)
            max_dd = float(np.min(cumul - peak))
            streak = mx = 0
            for p in pnls:
                if p <= 0: streak += 1; mx = max(mx, streak)
                else: streak = 0
            holds = [e["hold_seconds"] for e in all_exits]
            reasons = Counter(e["exit_reason"] for e in all_exits)
            failures = Counter(e.get("exit_failure_reason", "") for e in all_exits
                              if e.get("exit_failure_reason"))

            all_entries = []
            enf = OUT_DIR / "paper_entries.jsonl"
            if enf.exists():
                with open(enf) as f:
                    for line in f:
                        if line.strip():
                            all_entries.append(json.loads(line))

            qa_viol = sum(1 for e in all_entries
                         if e.get("quote_age_ms", 0) > MAX_QUOTE_AGE_MS)
            settle_errs = sum(1 for s in (self.settles_log or [])
                             if s.get("settlement_error"))
            fail_rate = sum(failures.values()) / n * 100

            per_r = dict(
                timestamp=datetime.now(timezone.utc).isoformat(),
                paper_entries=len(all_entries), paper_exits=n,
                exit_success_rate=round(exit_rate, 2),
                take_profit_rate=round(reasons.get("take_profit", 0)/n*100, 2),
                stop_loss_rate=round(reasons.get("stop_loss", 0)/n*100, 2),
                timeout_rate=round(reasons.get("timeout", 0)/n*100, 2),
                forced_exit_rate=round(reasons.get("pre_expiry_forced", 0)/n*100, 2),
                failed_exit_rate=round(fail_rate, 2),
                WR=round(wr, 2), EV_per_trade=round(ev, 4), PF=round(pf, 4),
                max_loss_streak=mx, max_drawdown=round(max_dd, 4),
                avg_hold_seconds=round(float(np.mean(holds)), 2) if holds else 0,
                median_hold_seconds=round(float(np.median(holds)), 2) if holds else 0,
                avg_spread=round(float(np.mean([e.get("spread",0) for e in all_entries])), 4) if all_entries else 0,
                avg_quote_age_ms=round(float(np.mean([e.get("quote_age_ms",0) for e in all_entries])), 1) if all_entries else 0,
                asset_breakdown=dict(Counter(e.get("asset","") for e in all_entries)),
                exit_reason_breakdown=dict(reasons),
                failure_reason_breakdown=dict(failures),
                rejection_summary=dict(self.rejections.most_common(20)),
                runtime_hours=round((time.time() - self.state["start_time"])/3600, 2),
            )

            # §14/15: Classification
            passed = (n >= 100 and exit_rate >= 85 and ev > 0 and pf >= 1.35
                      and settle_errs == 0 and qa_viol == 0 and fail_rate <= 15)
            if passed:
                cls = "SCALPER_MICRO_LIVE_REVIEW_CANDIDATE"
            elif n < 100:
                cls = "INSUFFICIENT_SAMPLE_COLLECTING"
            else:
                cls = "SCALPER_PAPER_LIVE_REJECTED"

            # Latency report
        lat_r = dict(timestamp=datetime.now(timezone.utc).isoformat(),
                     feed_mode="REST_FALLBACK",
                     live_promotion_eligible=False,
                     total_cycles=self.cycle,
                     note="WebSocket feeds not available; REST fallback only. "
                          "Paper-readiness records REST_FALLBACK per §6.")
        with open(OUT_DIR / "paper_latency_report.json", "w") as f:
            json.dump(lat_r, f, indent=2)

        # Performance report
        with open(OUT_DIR / "paper_performance_report.json", "w") as f:
            json.dump(per_r, f, indent=2, default=str)

        # Readiness report
        best_metrics = per_r if n > 0 else {}
        ready_r = dict(
            timestamp=datetime.now(timezone.utc).isoformat(),
            global_scalper_status="SCALPER_REJECTED",
            profile="DOWN_lag_bucket_03_05",
            paper_live_entries=self.state["entries"],
            paper_live_exits=n,
            classification=cls,
            mode_integrity_passed=True,
            recommended_next_action=(
                "Review for micro-live if candidate" if "CANDIDATE" in cls
                else "Continue collecting — target 100 exits" if "COLLECTING" in cls
                else "Archive scalper — paper-live failed real bid/ask test"
            ))
        with open(OUT_DIR / "paper_readiness.json", "w") as f:
            json.dump(ready_r, f, indent=2)

        log.info(f"Report: cls={cls} entries={self.state['entries']} exits={n} "
                 f"cycle={self.cycle}")

    def run(self):
        if not self.verify_integrity():
            log.error("MODE INTEGRITY FAILED — aborting")
            return

        log.info("V21.7.8 Scalper Paper-Live Simulator STARTING")
        log.info(f"Profile: DOWN_lag_bucket_03_05")
        log.info(f"Mode: {EXECUTION_MODE} (no real orders)")
        log.info(f"Target: {MAX_EXITS} exits or {MAX_RUNTIME_H}h runtime")

        # Register shutdown handler
        def shutdown(sig, frame):
            log.info("Shutdown signal received")
            self.shutting_down = True
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        report_interval = 10  # Report every 10 cycles (~2min)

        while not self.shutting_down:
            runtime_h = (time.time() - self.state["start_time"]) / 3600
            # Check exit conditions
            if self.state["exits"] >= MAX_EXITS:
                log.info(f"Target exits {MAX_EXITS} reached")
                break
            if runtime_h >= MAX_RUNTIME_H:
                log.info(f"Max runtime {MAX_RUNTIME_H}h reached")
                break

            try:
                self.scan()
            except Exception as e:
                log.error(f"Scan error: {e}")
                traceback.print_exc()

            # Log cycle progress
            if self.cycle % 5 == 0:
                log.info(f"Cycle {self.cycle}: entries={self.state['entries']} "
                         f"exits={self.state['exits']} bankroll={self.state['bankroll']:.4f} "
                         f"top_rejections={self.rejections.most_common(3)}")

            # Periodic save + report
            if self.cycle % report_interval == 0:
                self.settle_audit()
                self.save()
                self.reports()

            time.sleep(SCAN_INTERVAL_S)

        # Final save
        self.settle_audit()
        self.save()
        self.reports()
        log.info("V21.7.8 Simulator SHUTDOWN complete")


if __name__ == "__main__":
    sim = Simulator()
    sim.run()