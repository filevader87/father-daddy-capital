#!/usr/bin/env python3
"""V22.1 Directive — Unit tests for accounting, provenance, and reporting modules."""
import json
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import unittest

PROJECT = Path(__file__).resolve().parent.parent


class TestCapitalLockInvariants(unittest.TestCase):
    """S0: Live lock invariants must be enforced."""

    def test_weather_live_blocked_in_source(self):
        src = (PROJECT / "src/weather/v1_weather_runner_v21.py").read_text()
        self.assertIn("WEATHER_BOT_LIVE_BLOCKED = True", src)

    def test_halt_config_disables_weather_live(self):
        cfg = json.load(open(PROJECT / "output/weather_bot/v2_3_halt_config.json"))
        self.assertTrue(cfg.get("disable_weather_live"))

    def test_canary_real_orders_not_allowed(self):
        gate = json.load(open(PROJECT / "output/v21720_canary/btc15m_canary_execution_gate.json"))
        self.assertFalse(gate.get("real_orders_allowed"))

    def test_canary_invalidated(self):
        gate = json.load(open(PROJECT / "output/v21720_canary/btc15m_canary_execution_gate.json"))
        self.assertIn("INVALIDATED", gate.get("classification", ""))


class TestWeatherAccountingSplit(unittest.TestCase):
    """S3: Pre-DEB and post-DEB must be separated."""

    def test_accounting_split_exists(self):
        f = PROJECT / "reports/V22.1_WEATHER_ACCOUNTING_SPLIT.json"
        self.assertTrue(f.exists())
        data = json.load(open(f))
        self.assertIn("weather_accounting", data)
        self.assertIn("pre_deb_sigma_bug", data["weather_accounting"])
        self.assertIn("post_deb_v22", data["weather_accounting"])

    def test_pre_deb_excluded_from_promotion(self):
        data = json.load(open(PROJECT / "reports/V22.1_WEATHER_ACCOUNTING_SPLIT.json"))
        self.assertTrue(data["weather_accounting"]["pre_deb_sigma_bug"]["excluded_from_deb_promotion"])

    def test_pre_deb_has_5_resolved(self):
        data = json.load(open(PROJECT / "reports/V22.1_WEATHER_ACCOUNTING_SPLIT.json"))
        self.assertEqual(data["weather_accounting"]["pre_deb_sigma_bug"]["resolved"], 5)

    def test_pre_deb_0_wins_5_losses(self):
        data = json.load(open(PROJECT / "reports/V22.1_WEATHER_ACCOUNTING_SPLIT.json"))
        self.assertEqual(data["weather_accounting"]["pre_deb_sigma_bug"]["wins"], 0)
        self.assertEqual(data["weather_accounting"]["pre_deb_sigma_bug"]["losses"], 5)

    def test_post_deb_not_ready_for_review(self):
        data = json.load(open(PROJECT / "reports/V22.1_WEATHER_ACCOUNTING_SPLIT.json"))
        self.assertFalse(data["weather_accounting"]["post_deb_v22"]["ready_for_review"])


class TestCryptoSalvageAudit(unittest.TestCase):
    """S9: Crypto observer salvage audit exists with real counts."""

    def test_salvage_audit_exists(self):
        f = PROJECT / "reports/V22.1_CRYPTO_OBSERVER_SALVAGE_AUDIT.json"
        self.assertTrue(f.exists())
        data = json.load(open(f))
        self.assertEqual(data["classification"], "OBSERVATION_ONLY_NOT_STRATEGY")

    def test_touches_count_positive(self):
        data = json.load(open(PROJECT / "reports/V22.1_CRYPTO_OBSERVER_SALVAGE_AUDIT.json"))
        self.assertGreater(data["counts"]["total_touches"], 0)

    def test_zero_tier1_escalations(self):
        data = json.load(open(PROJECT / "reports/V22.1_CRYPTO_OBSERVER_SALVAGE_AUDIT.json"))
        self.assertEqual(data["counts"]["tier_1_escalations"], 0)

    def test_p50_too_slow(self):
        data = json.load(open(PROJECT / "reports/V22.1_CRYPTO_OBSERVER_SALVAGE_AUDIT.json"))
        self.assertTrue(data["answers"]["p50_too_slow_for_1s"])


class TestCanaryInvalidated(unittest.TestCase):
    """S12: BTC 15m canary promotion language removed."""

    def test_canary_gate_not_armed(self):
        gate = json.load(open(PROJECT / "output/v21720_canary/btc15m_canary_execution_gate.json"))
        self.assertNotIn("ARMED_LIVE_STRESS_NEEDED", gate.get("classification", ""))
        self.assertIn("INVALIDATED", gate.get("classification", ""))


class TestUnifiedEvidenceGate(unittest.TestCase):
    """S14: Unified evidence gate exists."""

    def test_evidence_gate_exists(self):
        f = PROJECT / "reports/V22.1_UNIFIED_EVIDENCE_GATE.json"
        self.assertTrue(f.exists())
        data = json.load(open(f))
        self.assertIn("WEATHER_DEB_V22", data["strategies"])
        self.assertEqual(data["strategies"]["WEATHER_DEB_V22"]["status"], "PRIMARY_RESEARCH_CANDIDATE")
        self.assertFalse(data["strategies"]["WEATHER_DEB_V22"]["live_allowed"])
        self.assertEqual(data["strategies"]["CRYPTO_OBSERVER_V21_7_51"]["status"], "OBSERVATION_ONLY_NOT_STRATEGY")
        self.assertEqual(data["strategies"]["BTC_15M_CANARY"]["status"], "INVALIDATED_PAUSED")


class TestFinalStatus(unittest.TestCase):
    """S15: Final status report exists with correct end state."""

    def test_final_status_exists(self):
        f = PROJECT / "reports/V22.1_FINAL_STATUS_AND_NEXT_ACTIONS.json"
        self.assertTrue(f.exists())
        data = json.load(open(f))
        self.assertFalse(data["end_state"]["LIVE_ALLOWED"])
        self.assertFalse(data["end_state"]["CAPITAL_DEPLOYMENT_ALLOWED"])
        self.assertEqual(data["end_state"]["READY_FOR_REVIEW"], [])
        self.assertEqual(data["end_state"]["PRIMARY_RESEARCH_CANDIDATE"], "WEATHER_DEB_V22")


class TestValidationBoard(unittest.TestCase):
    """S6: Weather validation board exists."""

    def test_board_exists(self):
        f = PROJECT / "reports/V22.1_WEATHER_DEB_VALIDATION_BOARD.json"
        self.assertTrue(f.exists())
        data = json.load(open(f))
        self.assertFalse(data["post_deb_v22"]["ready_for_review"])
        self.assertFalse(data["post_deb_v22"]["live_allowed"])
        self.assertEqual(data["requirements"]["min_resolved"], 25)


class TestScanThroughput(unittest.TestCase):
    """S4: Weather scan throughput increased to 50 cities."""

    def test_max_cities_is_50(self):
        src = (PROJECT / "src/weather/v1_weather_runner_v21.py").read_text()
        self.assertIn("MAX_CITIES_PER_CYCLE = 50", src)


class TestEntryGateLogging(unittest.TestCase):
    """S5: Entry gate block reason logging exists."""

    def test_log_entry_gate_function_exists(self):
        src = (PROJECT / "src/weather/v1_weather_runner_v21.py").read_text()
        self.assertIn("def log_entry_gate", src)
        self.assertIn("block_reason", src)

    def test_block_reasons_defined(self):
        src = (PROJECT / "src/weather/v1_weather_runner_v21.py").read_text()
        for reason in ["NO_MARKET_FOUND", "DEAD_MARKET", "LOW_LIQUIDITY", "NO_BUCKET_EDGE"]:
            self.assertIn(reason, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)