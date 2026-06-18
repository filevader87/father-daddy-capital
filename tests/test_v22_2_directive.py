#!/usr/bin/env python3
"""V22.2 Directive — Unit tests for cohort locking, city scoring, settlement audit, review gate."""
import json
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
R = PROJECT / "reports"


class TestCapitalLocks(unittest.TestCase):
    def test_weather_live_blocked(self):
        src = (PROJECT / "src/weather/v1_weather_runner_v21.py").read_text()
        self.assertIn("WEATHER_BOT_LIVE_BLOCKED = True", src)

    def test_halt_config_live_disabled(self):
        cfg = json.load(open(PROJECT / "output/weather_bot/v2_3_halt_config.json"))
        self.assertTrue(cfg.get("disable_weather_live"))

    def test_canary_invalidated(self):
        gate = json.load(open(PROJECT / "output/v21720_canary/btc15m_canary_execution_gate.json"))
        self.assertNotIn("ARMED_LIVE_STRESS", gate.get("classification", ""))


class TestStrategyClassification(unittest.TestCase):
    def test_classification_exists(self):
        data = json.load(open(R / "V22.2_STRATEGY_CLASSIFICATION.json"))
        self.assertEqual(data["WEATHER_DEB_V22"]["status"], "PRIMARY_RESEARCH_CANDIDATE")
        self.assertEqual(data["CRYPTO_OBSERVER_V21_7_51"]["status"], "OBSERVATION_ONLY_BLOCKED_FEED_AND_LATENCY")
        self.assertEqual(data["BTC_15M_CANARY"]["status"], "INVALIDATED_DEAD_REQUIRES_FULL_REVALIDATION")
        self.assertFalse(data["WEATHER_DEB_V22"]["live_allowed"])
        self.assertFalse(data["CRYPTO_OBSERVER_V21_7_51"]["live_allowed"])
        self.assertFalse(data["BTC_15M_CANARY"]["live_allowed"])


class TestCohortLocking(unittest.TestCase):
    def test_cohorts_exist(self):
        data = json.load(open(R / "V22.2_COHORT_DATA.json"))
        self.assertIn("PRE_DEB_SIGMA_BUG", data["cohorts"])
        self.assertIn("POST_DEB_V22_CORE_15PP", data["cohorts"])
        self.assertIn("POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL", data["cohorts"])

    def test_pre_deb_excluded(self):
        data = json.load(open(R / "V22.2_COHORT_DATA.json"))
        self.assertTrue(data["cohorts"]["PRE_DEB_SIGMA_BUG"]["excluded_from_review"])

    def test_pre_deb_5_resolved(self):
        data = json.load(open(R / "V22.2_COHORT_DATA.json"))
        self.assertEqual(data["cohorts"]["PRE_DEB_SIGMA_BUG"]["resolved"], 5)

    def test_core_15pp_edge_threshold(self):
        data = json.load(open(R / "V22.2_COHORT_DATA.json"))
        self.assertEqual(data["cohorts"]["POST_DEB_V22_CORE_15PP"]["edge_threshold_pp"], 15)

    def test_exp_12pp_edge_threshold(self):
        data = json.load(open(R / "V22.2_COHORT_DATA.json"))
        self.assertEqual(data["cohorts"]["POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL"]["edge_threshold_pp"], 12)

    def test_core_and_exp_not_ready(self):
        data = json.load(open(R / "V22.2_COHORT_DATA.json"))
        self.assertFalse(data["cohorts"]["POST_DEB_V22_CORE_15PP"]["ready_for_review"])
        self.assertFalse(data["cohorts"]["POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL"]["ready_for_review"])


class TestLowNoiseCityEligibility(unittest.TestCase):
    def test_whitelist_exists(self):
        data = json.load(open(R / "V22.2_LOW_NOISE_CITY_ELIGIBILITY.json"))
        self.assertIsInstance(data["low_noise_city_whitelist"], list)
        self.assertGreater(len(data["low_noise_city_whitelist"]), 0)

    def test_rejected_cities_exist(self):
        data = json.load(open(R / "V22.2_LOW_NOISE_CITY_ELIGIBILITY.json"))
        self.assertIsInstance(data["rejected_cities"], dict)

    def test_london_scored(self):
        data = json.load(open(R / "V22.2_LOW_NOISE_CITY_ELIGIBILITY.json"))
        self.assertIn("london", data["city_scores"])

    def test_high_risk_cities_rejected(self):
        data = json.load(open(R / "V22.2_LOW_NOISE_CITY_ELIGIBILITY.json"))
        # Chicago, Munich, Mexico City, Chongqing are high-risk
        for city in ["chicago", "munich", "mexico city", "chongqing"]:
            if city in data["rejected_cities"]:
                self.assertIn("HIGH_RISK", data["rejected_cities"][city])


class TestJournalPatch(unittest.TestCase):
    def test_patched_trades_file_exists(self):
        f = PROJECT / "output/weather_bot/v22_2_patched_trades.jsonl"
        self.assertTrue(f.exists())

    def test_patched_trades_have_journal_status(self):
        f = PROJECT / "output/weather_bot/v22_2_patched_trades.jsonl"
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    t = json.loads(line)
                    self.assertIn("journal_patch_status", t)
                    self.assertIn(t["journal_patch_status"],
                                  ["ORIGINAL", "BACKFILLED_VERIFIED", "BACKFILLED_INFERRED", "UNKNOWN_NOT_REVIEW_ELIGIBLE"])

    def test_pre_deb_trades_not_review_eligible(self):
        data = json.load(open(R / "V22.2_COHORT_DATA.json"))
        # All 16 trades are pre-DEB, so all should be UNKNOWN_NOT_REVIEW_ELIGIBLE
        self.assertEqual(data["journal_patch_summary"]["UNKNOWN_NOT_REVIEW_ELIGIBLE"], 16)


class TestQuoteProvenanceGate(unittest.TestCase):
    def test_gamma_rest_blocked(self):
        data = json.load(open(R / "V22.2_QUOTE_PROVENANCE_GATE.json"))
        self.assertIn("PM_GAMMA_REST_ONLY", data["blocked_sources"])
        self.assertNotIn("PM_GAMMA_REST_ONLY", data["allowed_sources"])
        self.assertEqual(data["gamma_rest_classification"], "DISCOVERY_ONLY_NOT_QUOTE_SOURCE")
        self.assertFalse(data["all_trades_live_equivalent"])
        self.assertFalse(data["review_eligible"])


class TestSettlementAudit(unittest.TestCase):
    def test_audit_exists(self):
        f = R / "V22.2_WEATHER_SETTLEMENT_AUDIT.json"
        self.assertTrue(f.exists())
        data = json.load(open(f))
        self.assertIn("cities", data)

    def test_settlement_verified_count(self):
        data = json.load(open(R / "V22.2_WEATHER_SETTLEMENT_AUDIT.json"))
        verified = sum(1 for v in data["cities"].values() if v["classification"] == "SETTLEMENT_VERIFIED")
        self.assertGreater(verified, 0)

    def test_each_city_has_required_fields(self):
        data = json.load(open(R / "V22.2_WEATHER_SETTLEMENT_AUDIT.json"))
        for city, a in data["cities"].items():
            self.assertIn("settlement_source", a)
            self.assertIn("rounding_rule", a)
            self.assertIn("metar_station", a)
            self.assertIn("classification", a)


class TestExposureControls(unittest.TestCase):
    def test_controls_exist(self):
        data = json.load(open(R / "V22.2_EXPOSURE_CONTROLS.json"))
        self.assertEqual(data["max_active_weather_positions"], 10)
        self.assertEqual(data["max_positions_per_city_date"], 1)
        self.assertEqual(data["max_daily_new_positions"], 8)
        self.assertEqual(data["max_experimental_12pp_daily_positions"], 3)
        self.assertTrue(data["reject_duplicates"])


class TestBiasCorrectionMaturity(unittest.TestCase):
    def test_maturity_exists(self):
        f = R / "V22.2_BIAS_CORRECTION_MATURITY.json"
        self.assertTrue(f.exists())
        data = json.load(open(f))
        self.assertIn("cities", data)

    def test_all_cities_have_status(self):
        data = json.load(open(R / "V22.2_BIAS_CORRECTION_MATURITY.json"))
        for city, m in data["cities"].items():
            self.assertIn(m["bias_correction_status"], ["RECORDING_NOT_MATURE", "ACTIVE"])


class TestValidationDashboard(unittest.TestCase):
    def test_dashboard_exists(self):
        data = json.load(open(R / "V22.2_WEATHER_VALIDATION_DASHBOARD.json"))
        self.assertIn("post_deb_core_15pp", data)
        self.assertIn("post_deb_low_noise_12pp_experimental", data)
        self.assertIn("pre_deb_sigma_bug", data)
        self.assertFalse(data["live_allowed"])

    def test_core_needs_25(self):
        data = json.load(open(R / "V22.2_WEATHER_VALIDATION_DASHBOARD.json"))
        self.assertEqual(data["post_deb_core_15pp"]["needed_resolved_for_review"], 25)

    def test_exp_needs_25(self):
        data = json.load(open(R / "V22.2_WEATHER_VALIDATION_DASHBOARD.json"))
        self.assertEqual(data["post_deb_low_noise_12pp_experimental"]["needed_resolved_for_separate_review"], 25)


class TestReviewGate(unittest.TestCase):
    def test_gate_exists(self):
        data = json.load(open(R / "V22.2_REVIEW_GATE.json"))
        self.assertEqual(data["requirements"]["resolved_positions"], 25)
        self.assertEqual(data["requirements"]["profit_factor_min"], 1.25)
        self.assertFalse(data["live_allowed"])

    def test_no_cohort_ready(self):
        data = json.load(open(R / "V22.2_REVIEW_GATE.json"))
        for cohort in data["current_status"].values():
            self.assertFalse(cohort["ready"])


class TestFinalReport(unittest.TestCase):
    def test_final_exists(self):
        data = json.load(open(R / "V22.2_FINAL_WEATHER_ONLY_VALIDATION_SPRINT.json"))
        self.assertEqual(data["end_state"]["primary_research_candidate"], "WEATHER_DEB_V22")
        self.assertEqual(data["end_state"]["ready_for_review"], [])
        self.assertFalse(data["end_state"]["live_allowed"])
        self.assertEqual(data["end_state"]["crypto_status"], "OBSERVATION_ONLY_BLOCKED_FEED_AND_LATENCY")
        self.assertEqual(data["end_state"]["btc_15m_canary_status"], "INVALIDATED_DEAD_REQUIRES_FULL_REVALIDATION")

    def test_pre_deb_excluded(self):
        data = json.load(open(R / "V22.2_FINAL_WEATHER_ONLY_VALIDATION_SPRINT.json"))
        self.assertTrue(data["answers"]["3_pre_deb_excluded"])

    def test_no_cohort_ready(self):
        data = json.load(open(R / "V22.2_FINAL_WEATHER_ONLY_VALIDATION_SPRINT.json"))
        self.assertFalse(data["answers"]["11_any_cohort_ready_for_review"])

    def test_live_not_allowed(self):
        data = json.load(open(R / "V22.2_FINAL_WEATHER_ONLY_VALIDATION_SPRINT.json"))
        self.assertFalse(data["answers"]["12_live_trading_allowed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)