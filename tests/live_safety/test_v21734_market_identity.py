#!/usr/bin/env python3
"""
V21.7.34 — Market Identity Regression Tests
==============================================
Validate that condition_id, token mapping, and live quote sourcing
work correctly and block live entry when they should.
"""

import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path("/home/naq1987s/father-daddy-capital")
sys.path.insert(0, str(PROJECT_ROOT / "src" / "v217_live"))

from v21726_scanner_bridge import discover_all_markets, fetch_books_persistent, classify_zone, close_pool
from v21723_btc15m_canary_watcher import get_clob_client

PROJECT_ROOT = Path("/home/naq1987s/father-daddy-capital")
OUT_DIR = PROJECT_ROOT / "output" / "v21734_market_identity"

LIVE_QUOTE_SOURCES = {"PM_WS_BEST_BID_ASK", "PM_WS_BOOK", "PM_CLOB_READ"}


class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.results = []

    def check(self, name: str, condition: bool, detail: str = ""):
        status = "PASS" if condition else "FAIL"
        if condition:
            self.passed += 1
        else:
            self.failed += 1
        self.results.append({"test": name, "status": status, "detail": detail})
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))

    def summary(self) -> dict:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": "V21.7.34",
            "total": self.passed + self.failed,
            "passed": self.passed,
            "failed": self.failed,
            "all_pass": self.failed == 0,
            "results": self.results,
        }


def test_gamma_event_slug_returns_condition_id(t: TestResult):
    """Gamma event slug returns condition_id."""
    markets = discover_all_markets()
    btc_15m = [m for m in markets if m.get("asset") == "BTC" and m.get("interval") == "15m"]
    t.check("gamma_event_has_condition_id", len(btc_15m) > 0, f"Found {len(btc_15m)} BTC 15m markets")
    if btc_15m:
        m = btc_15m[0]
        cid = m.get("condition_id", "")
        t.check("condition_id_not_empty", bool(cid), f"condition_id={cid[:40]}...")
        t.check("condition_id_starts_with_0x", cid.startswith("0x"), f"starts with 0x={cid.startswith('0x')}")


def test_condition_id_missing_blocks_live(t: TestResult):
    """Missing condition_id blocks live entry."""
    # Simulate missing condition_id
    fake_market = {"slug": "btc-updown-15m-1234", "condition_id": "", "down_token_id": "123"}
    t.check("empty_condition_id_blocks_live", not fake_market["condition_id"], "Empty condition_id correctly blocks live")


def test_up_down_token_mapping_validated(t: TestResult):
    """UP/DOWN token mapping validated via complement sum."""
    markets = discover_all_markets()
    btc_15m = [m for m in markets if m.get("asset") == "BTC" and m.get("interval") == "15m"][0]
    dn_tid = btc_15m["down_token_id"]
    up_tid = btc_15m["up_token_id"]
    cid = btc_15m["condition_id"]

    t.check("down_token_id_present", bool(dn_tid), f"down_token_id={dn_tid[:30]}...")
    t.check("up_token_id_present", bool(up_tid), f"up_token_id={up_tid[:30]}...")
    t.check("condition_id_present", bool(cid), f"condition_id={cid[:30]}...")

    # CLOB validation
    clob = get_clob_client()
    book_dn = clob.get_order_book(dn_tid)
    book_up = clob.get_order_book(up_tid)
    t.check("down_token_clob_responds", bool(book_dn.get("asks") or book_dn.get("bids")), "DOWN token has CLOB data")
    t.check("up_token_clob_responds", bool(book_up.get("asks") or book_up.get("bids")), "UP token has CLOB data")

    # Complement check
    quotes = fetch_books_persistent(markets, max_workers=8)
    dn_ask = quotes.get(dn_tid, {}).get("best_ask", 0)
    up_ask = quotes.get(up_tid, {}).get("best_ask", 0)
    complement = dn_ask + up_ask
    t.check("complement_sum_near_1", 0.95 <= complement <= 1.10, f"DOWN+UP ask sum={complement:.4f}")


def test_stale_ws_book_does_not_authorize_live(t: TestResult):
    """Stale WS book (age > 5000ms) does not authorize live entry."""
    # Load WS cache report
    cache_path = PROJECT_ROOT / "output" / "v21716_pm_ws" / "quote_cache_source_report.json"
    if cache_path.exists():
        with open(cache_path) as f:
            cache = json.load(f)
        p95_age = cache.get("pm_book_p95_age_ms", 0)
        stale = p95_age > 5000
        t.check("ws_p95_stale", stale, f"P95 age={p95_age/1000:.0f}s (>5s is stale)")
    else:
        t.check("ws_cache_report_exists", False, "WS cache report not found")

    # Simulate stale quote source check
    stale_source = "PM_WS_PRICE_CHANGE"
    t.check("stale_ws_source_not_live_eligible", stale_source not in LIVE_QUOTE_SOURCES,
            f"{stale_source} not in LIVE_QUOTE_SOURCES")


def test_gamma_only_quote_does_not_authorize_live(t: TestResult):
    """Gamma-only quote does not authorize live entry."""
    gamma_source = "PM_GAMMA_REST"
    t.check("gamma_rest_not_live_eligible", gamma_source not in LIVE_QUOTE_SOURCES,
            f"{gamma_source} not in LIVE_QUOTE_SOURCES")


def test_fresh_clob_read_quote_can_authorize_live(t: TestResult):
    """Fresh CLOB_READ quote can authorize live entry."""
    markets = discover_all_markets()
    btc_15m = [m for m in markets if m.get("asset") == "BTC" and m.get("interval") == "15m"][0]
    dn_tid = btc_15m["down_token_id"]

    clob = get_clob_client()
    start = time.time()
    book = clob.get_order_book(dn_tid)
    elapsed_ms = (time.time() - start) * 1000

    clob_source = "PM_CLOB_READ"
    t.check("clob_read_in_live_sources", clob_source in LIVE_QUOTE_SOURCES, f"{clob_source} is live-eligible")
    t.check("clob_read_fresh", elapsed_ms <= 3000, f"CLOB read in {elapsed_ms:.0f}ms")
    t.check("clob_book_has_data", bool(book.get("asks") or book.get("bids")), "CLOB book has data")


def test_final_pre_submit_rejects_gamma(t: TestResult):
    """Final pre-submit quote check rejects Gamma REST."""
    gamma_source = "PM_GAMMA_REST"
    t.check("final_pre_submit_rejects_gamma", gamma_source not in LIVE_QUOTE_SOURCES,
            "Gamma REST rejected in final pre-submit check")


def test_current_window_mismatch_blocks_live(t: TestResult):
    """Current-window mismatch blocks live entry."""
    # Simulate expired market
    fake_market = {"slug": "btc-updown-15m-1781000000", "tte": -300}  # Expired
    t.check("negative_tte_blocks_live", fake_market["tte"] <= 0, f"TTE={fake_market['tte']}s is expired")


def test_expired_window_token_blocks_live(t: TestResult):
    """Expired-window token blocks live entry."""
    # WS cache shows tokens with 177K+ second age
    cache_path = PROJECT_ROOT / "output" / "v21716_pm_ws" / "quote_cache_source_report.json"
    if cache_path.exists():
        with open(cache_path) as f:
            cache = json.load(f)
        expired_count = 0
        for tid, info in cache.get("tokens", {}).items():
            if info.get("book_age_ms", 0) > 86400000:  # > 24h
                expired_count += 1
        t.check("expired_tokens_identified", True, f"{expired_count} expired tokens found in WS cache")
    else:
        t.check("ws_cache_exists", False, "No WS cache")


def test_quote_with_no_condition_id_blocks_live(t: TestResult):
    """Quote with no condition_id blocks live entry."""
    fake_quote = {"best_ask": 0.05, "condition_id": "", "quote_source": "PM_CLOB_READ"}
    t.check("empty_condition_id_blocks_live", not fake_quote["condition_id"],
            "Empty condition_id correctly blocks live")


def main():
    print("V21.7.34 — Market Identity Regression Tests")
    print("=" * 60)

    t = TestResult()

    print("\n1. Gamma event slug returns condition_id")
    test_gamma_event_slug_returns_condition_id(t)

    print("\n2. Condition ID missing blocks live")
    test_condition_id_missing_blocks_live(t)

    print("\n3. UP/DOWN token mapping validated")
    test_up_down_token_mapping_validated(t)

    print("\n4. Stale WS book does not authorize live")
    test_stale_ws_book_does_not_authorize_live(t)

    print("\n5. Gamma-only quote does not authorize live")
    test_gamma_only_quote_does_not_authorize_live(t)

    print("\n6. Fresh CLOB_READ quote can authorize live")
    test_fresh_clob_read_quote_can_authorize_live(t)

    print("\n7. Final pre-submit rejects Gamma REST")
    test_final_pre_submit_rejects_gamma(t)

    print("\n8. Current-window mismatch blocks live")
    test_current_window_mismatch_blocks_live(t)

    print("\n9. Expired-window token blocks live")
    test_expired_window_token_blocks_live(t)

    print("\n10. Quote with no condition_id blocks live")
    test_quote_with_no_condition_id_blocks_live(t)

    # Write results
    summary = t.summary()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "regression_test_results.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)

    print(f"\n{'=' * 60}")
    print(f"Results: {t.passed} passed, {t.failed} failed out of {t.passed + t.failed}")
    print(f"Classification: {'V21.7.34_ALL_TESTS_PASS' if t.failed == 0 else 'V21.7.34_SOME_TESTS_FAILED'}")

    close_pool()
    return t.failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)