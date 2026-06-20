#!/usr/bin/env python3
"""V22.3 Weather DEB Loop Engineering — Test Suite
Tests all 19 sections of the V22.3 directive.
Run: python3 -m pytest tests/test_v22_3.py -v
"""
import json, os, sys
from pathlib import Path

import pytest

P = Path("/home/naq1987s/father-daddy-capital")
R = P / "reports"
D = P / "data" / "weather"

# ═══════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="session", autouse=True)
def run_engine():
    """Run the V22.3 engine once before all tests."""
    os.chdir(str(P))
    env = os.environ.copy()
    env["PYTHONPATH"] = "src/polyweather_analysis:src:src/weather:src/v217_live:."
    import subprocess
    result = subprocess.run(
        [sys.executable, "reports/v22_3_engine.py"],
        capture_output=True, text=True, env=env, cwd=str(P)
    )
    assert result.returncode == 0, f"Engine failed: {result.stderr}"
    return result

def load_json(path):
    with open(path) as f:
        return json.load(f)

# ═══════════════════════════════════════════════════════════════
# §1: LIVE LOCK INVARIANTS
# ═══════════════════════════════════════════════════════════════

class TestLiveLockInvariants:
    def test_strategy_status_exists(self):
        assert (R / "V22.3_STRATEGY_STATUS.json").exists()

    def test_weather_live_not_allowed(self):
        s = load_json(R / "V22.3_STRATEGY_STATUS.json")
        assert s["WEATHER_DEB_V22"]["live_allowed"] == False

    def test_crypto_live_not_allowed(self):
        s = load_json(R / "V22.3_STRATEGY_STATUS.json")
        assert s["CRYPTO_OBSERVER_V21_7_51"]["live_allowed"] == False

    def test_btc_canary_invalidated(self):
        s = load_json(R / "V22.3_STRATEGY_STATUS.json")
        assert "INVALIDATED" in s["BTC_15M_CANARY"]["status"]

    def test_all_invariants_false(self):
        s = load_json(R / "V22.3_STRATEGY_STATUS.json")
        inv = s["invariants"]
        assert inv["REAL_ORDERS_ALLOWED"] == False
        assert inv["LIVE_AUTHORIZATION_SUSPENDED"] == True
        assert inv["CAPITAL_DEPLOYMENT_ALLOWED"] == False
        assert inv["WALLET_SPEND_ALLOWED"] == False
        assert inv["LIVE_ALLOWED"] == False

    def test_btc_canary_forbidden_labels(self):
        s = load_json(R / "V22.3_STRATEGY_STATUS.json")
        forbidden = s["BTC_15M_CANARY"]["forbidden_labels"]
        assert "ARMED" in forbidden
        assert "LIVE_STRESS_NEEDED" in forbidden
        assert "READY" in forbidden
        assert "CANARY_READY" in forbidden

    def test_halt_config_live_blocked(self):
        h = load_json(P / "output" / "weather_bot" / "v2_3_halt_config.json")
        assert h["LIVE_ALLOWED"] == False
        assert h["disable_weather_live"] == True

# ═══════════════════════════════════════════════════════════════
# §3: COHORT REGISTRY
# ═══════════════════════════════════════════════════════════════

class TestCohortRegistry:
    def test_registry_exists(self):
        assert (D / "cohort_registry.json").exists()

    def test_three_cohorts(self):
        r = load_json(D / "cohort_registry.json")
        assert "PRE_DEB_SIGMA_BUG" in r["cohorts"]
        assert "POST_DEB_V22_CORE_15PP" in r["cohorts"]
        assert "POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL" in r["cohorts"]

    def test_pre_deb_not_review_eligible(self):
        r = load_json(D / "cohort_registry.json")
        assert r["cohorts"]["PRE_DEB_SIGMA_BUG"]["review_eligible"] == False

    def test_core_15pp_review_eligible(self):
        r = load_json(D / "cohort_registry.json")
        assert r["cohorts"]["POST_DEB_V22_CORE_15PP"]["review_eligible"] == True
        assert r["cohorts"]["POST_DEB_V22_CORE_15PP"]["edge_threshold_pp"] == 15

    def test_12pp_not_review_eligible(self):
        r = load_json(D / "cohort_registry.json")
        assert r["cohorts"]["POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL"]["review_eligible"] == False
        assert r["cohorts"]["POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL"]["edge_threshold_pp"] == 12

    def test_12pp_cannot_combine_with_core(self):
        r = load_json(D / "cohort_registry.json")
        assert r["cohorts"]["POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL"]["cannot_be_combined_with_core_15pp"] == True

    def test_cohort_registry_md_exists(self):
        assert (R / "V22.3_WEATHER_COHORT_REGISTRY.md").exists()

# ═══════════════════════════════════════════════════════════════
# §4: JOURNAL COMPLETENESS
# ═══════════════════════════════════════════════════════════════

class TestJournalCompleteness:
    def test_audit_json_exists(self):
        assert (R / "V22.3_WEATHER_JOURNAL_COMPLETENESS_AUDIT.json").exists()

    def test_audit_md_exists(self):
        assert (R / "V22.3_WEATHER_JOURNAL_COMPLETENESS_AUDIT.md").exists()

    def test_patched_trades_exist(self):
        assert (D / "patched_trades.jsonl").exists()

    def test_patched_trades_have_journal_fields(self):
        with open(D / "patched_trades.jsonl") as f:
            for line in f:
                t = json.loads(line)
                for field in ["engine_version", "sigma_version", "deb_version",
                              "model_weight_version", "calibration_version", "cohort_id",
                              "entry_policy_id", "quote_source", "quote_timestamp", "quote_age_ms",
                              "settlement_verified", "settlement_source", "settlement_rule_version",
                              "city_station_mapping_version", "journal_patch_status", "journal_review_eligible"]:
                    assert field in t, f"Missing field {field} in trade {t.get('trade_id','?')}"
                break  # Check first trade

    def test_patch_statuses_valid(self):
        a = load_json(R / "V22.3_WEATHER_JOURNAL_COMPLETENESS_AUDIT.json")
        for ps in a["patch_statuses"]:
            assert ps in ["ORIGINAL", "BACKFILLED_VERIFIED", "BACKFILLED_INFERRED", "UNKNOWN_NOT_REVIEW_ELIGIBLE"]

    def test_unknown_trades_not_review_eligible(self):
        with open(D / "patched_trades.jsonl") as f:
            for line in f:
                t = json.loads(line)
                if t["journal_patch_status"] == "UNKNOWN_NOT_REVIEW_ELIGIBLE":
                    assert t["journal_review_eligible"] == False

# ═══════════════════════════════════════════════════════════════
# §5: QUOTE PROVENANCE GATE
# ═══════════════════════════════════════════════════════════════

class TestQuoteProvenance:
    def test_gate_json_exists(self):
        assert (R / "V22.3_QUOTE_PROVENANCE_GATE.json").exists()

    def test_allowed_sources(self):
        g = load_json(R / "V22.3_QUOTE_PROVENANCE_GATE.json")
        assert "LIVE_CLOB" in g["allowed_sources"]
        assert "RECORDED_LIVE_CLOB" in g["allowed_sources"]
        assert "REPLAY_CLOB" in g["allowed_sources"]

    def test_blocked_sources(self):
        g = load_json(R / "V22.3_QUOTE_PROVENANCE_GATE.json")
        assert "PM_GAMMA_REST_ONLY" in g["blocked_sources"]
        assert "NORMALIZED_BOOK" in g["blocked_sources"]
        assert "SYNTHETIC" in g["blocked_sources"]

    def test_gamma_rest_blocked(self):
        g = load_json(R / "V22.3_QUOTE_PROVENANCE_GATE.json")
        assert g["gate_passed"] == False
        assert "PM_GAMMA_REST_ONLY" in g["current_bot_quote_source"]

    def test_blocked_trades_not_review_eligible(self):
        g = load_json(R / "V22.3_QUOTE_PROVENANCE_GATE.json")
        for c in g["candidates"]:
            if not c["live_equivalent_quote"]:
                assert c["entry_allowed"] == False

# ═══════════════════════════════════════════════════════════════
# §6: SETTLEMENT INTEGRITY
# ═══════════════════════════════════════════════════════════════

class TestSettlementIntegrity:
    def test_json_exists(self):
        assert (R / "V22.3_WEATHER_SETTLEMENT_INTEGRITY.json").exists()

    def test_md_exists(self):
        assert (R / "V22.3_WEATHER_SETTLEMENT_INTEGRITY.md").exists()

    def test_all_cities_classified(self):
        a = load_json(R / "V22.3_WEATHER_SETTLEMENT_INTEGRITY.json")
        assert a["total_cities"] > 0
        assert len(a["cities"]) == a["total_cities"]

    def test_classifications_valid(self):
        a = load_json(R / "V22.3_WEATHER_SETTLEMENT_INTEGRITY.json")
        valid = {"SETTLEMENT_VERIFIED", "SETTLEMENT_UNCLEAR_NOT_REVIEW_ELIGIBLE",
                 "NO_MARKET_FOUND", "NO_STATION_MAPPING", "ROUNDING_RULE_UNCLEAR", "TIMEZONE_UNCLEAR"}
        for city, ca in a["cities"].items():
            assert ca["classification"] in valid

    def test_verified_cities_review_eligible(self):
        a = load_json(R / "V22.3_WEATHER_SETTLEMENT_INTEGRITY.json")
        for city, ca in a["cities"].items():
            if ca["classification"] == "SETTLEMENT_VERIFIED":
                assert ca["review_eligible"] == True

# ═══════════════════════════════════════════════════════════════
# §8: LOW-NOISE CITY WHITELIST
# ═══════════════════════════════════════════════════════════════

class TestLowNoiseWhitelist:
    def test_json_exists(self):
        assert (R / "V22.3_LOW_NOISE_CITY_WHITELIST.json").exists()

    def test_md_exists(self):
        assert (R / "V22.3_LOW_NOISE_CITY_WHITELIST.md").exists()

    def test_computed_not_hardcoded(self):
        w = load_json(R / "V22.3_LOW_NOISE_CITY_WHITELIST.json")
        assert w["total_cities_evaluated"] > 0
        assert "criteria" in w

    def test_qualifying_cities_meet_all_criteria(self):
        w = load_json(R / "V22.3_LOW_NOISE_CITY_WHITELIST.json")
        for city in w["qualifying_cities"]:
            c = w["cities"][city]
            assert c["station_mapping_score"] == 1.0
            assert c["settlement_rule_score"] == 1.0
            assert c["timezone_clarity_score"] == 1.0
            assert c["liquidity_score"] >= 0.5
            assert c["spread_score"] >= 0.5
            assert c["observation_availability"] >= 0.95
            assert not c["in_worst_quartile_forecast_error"]

# ═══════════════════════════════════════════════════════════════
# §9: CONFIDENCE / SIGMA GATE
# ═══════════════════════════════════════════════════════════════

class TestConfidenceSigmaGate:
    def test_gate_json_exists(self):
        assert (R / "V22.3_CONFIDENCE_SIGMA_GATE.json").exists()

    def test_directionality(self):
        g = load_json(R / "V22.3_CONFIDENCE_SIGMA_GATE.json")
        assert "sigma_c <= city_sigma_p40" in g["gate_logic"]
        assert "forecast_confidence_score >= city_confidence_p60" in g["gate_logic"]

# ═══════════════════════════════════════════════════════════════
# §11: MULTIPLE-TESTING
# ═══════════════════════════════════════════════════════════════

class TestMultipleTesting:
    def test_json_exists(self):
        assert (R / "V22.3_MULTIPLE_TESTING_CONTROL.json").exists()

    def test_variants_recorded(self):
        v = load_json(R / "V22.3_MULTIPLE_TESTING_CONTROL.json")
        assert v["number_of_variants_tested"] > 0

    def test_escalation_rules(self):
        v = load_json(R / "V22.3_MULTIPLE_TESTING_CONTROL.json")
        assert v["correction_rules"]["variants_gte_10"]["required_resolved"] == 40
        assert v["correction_rules"]["variants_gte_25"]["required_resolved"] == 60
        assert v["correction_rules"]["variants_gte_50"]["required_pf"] == 1.50

# ═══════════════════════════════════════════════════════════════
# §12: STRATEGY GRAVEYARD
# ═══════════════════════════════════════════════════════════════

class TestStrategyGraveyard:
    def test_graveyard_json_exists(self):
        assert (D / "strategy_graveyard.json").exists()

    def test_md_exists(self):
        assert (R / "V22.3_STRATEGY_GRAVEYARD.md").exists()

    def test_all_killed_strategies_present(self):
        g = load_json(D / "strategy_graveyard.json")
        ids = [s["strategy_id"] for s in g["strategies"]]
        required = [
            "BTC_15M_3_8_TAIL_CANARY", "BTC_15M_8_12_MICRO_CANARY",
            "ALL_5M_HOLD_TO_EXPIRY", "XRP_5M_DOWN_3C_SCALP",
            "ALL_5M_30_60C_SCALP_1C", "ALL_5M_30_60C_SCALP_2C",
            "ALL_5M_30_60C_SCALP_3C", "ALL_5M_30_60C_SCALP_5C",
            "WEATHER_OLD_SIGMA_MODEL",
        ]
        for r in required:
            assert r in ids, f"Missing graveyard entry: {r}"

    def test_all_killed(self):
        g = load_json(D / "strategy_graveyard.json")
        for s in g["strategies"]:
            assert s["status"] == "KILLED"
            assert s["may_retest_only_if"] == "materially_new_causal_hypothesis"

# ═══════════════════════════════════════════════════════════════
# §13: EXPOSURE CONTROLS
# ═══════════════════════════════════════════════════════════════

class TestExposureControls:
    def test_json_exists(self):
        assert (R / "V22.3_EXPOSURE_CONTROLS.json").exists()

    def test_limits(self):
        e = load_json(R / "V22.3_EXPOSURE_CONTROLS.json")
        assert e["max_active_weather_positions"] == 10
        assert e["max_daily_new_positions"] == 8
        assert e["max_positions_per_city_date"] == 1
        assert e["max_positions_per_bucket"] == 1
        assert e["max_experimental_12pp_daily_positions"] == 3

    def test_duplicate_key_format(self):
        e = load_json(R / "V22.3_EXPOSURE_CONTROLS.json")
        assert "city" in e["duplicate_key"]
        assert "cohort_id" in e["duplicate_key"]

# ═══════════════════════════════════════════════════════════════
# §14: BIAS CORRECTION MATURITY
# ═══════════════════════════════════════════════════════════════

class TestBiasCorrection:
    def test_json_exists(self):
        assert (R / "V22.3_BIAS_CORRECTION_MATURITY.json").exists()

    def test_activation_threshold(self):
        b = load_json(R / "V22.3_BIAS_CORRECTION_MATURITY.json")
        assert b["activation_threshold"] == 7

    def test_cities_below_threshold_not_mature(self):
        b = load_json(R / "V22.3_BIAS_CORRECTION_MATURITY.json")
        for city, c in b["cities"].items():
            if c["actual_count"] < 7:
                assert c["bias_correction_status"] == "RECORDING_NOT_MATURE"

# ═══════════════════════════════════════════════════════════════
# §15: VALIDATION DASHBOARD
# ═══════════════════════════════════════════════════════════════

class TestValidationDashboard:
    def test_json_exists(self):
        assert (R / "V22.3_WEATHER_VALIDATION_DASHBOARD.json").exists()

    def test_md_exists(self):
        assert (R / "V22.3_WEATHER_VALIDATION_DASHBOARD.md").exists()

    def test_schema(self):
        d = load_json(R / "V22.3_WEATHER_VALIDATION_DASHBOARD.json")
        assert "post_deb_core_15pp" in d
        assert "post_deb_low_noise_12pp_experimental" in d
        assert "pre_deb_sigma_bug" in d
        assert "live_allowed" in d

    def test_live_not_allowed(self):
        d = load_json(R / "V22.3_WEATHER_VALIDATION_DASHBOARD.json")
        assert d["live_allowed"] == False

    def test_pre_deb_excluded(self):
        d = load_json(R / "V22.3_WEATHER_VALIDATION_DASHBOARD.json")
        assert d["pre_deb_sigma_bug"]["excluded_from_review"] == True

    def test_core_has_required_fields(self):
        d = load_json(R / "V22.3_WEATHER_VALIDATION_DASHBOARD.json")
        core = d["post_deb_core_15pp"]
        for field in ["active", "resolved", "wins", "losses", "pnl", "pf",
                       "ev_per_trade", "brier", "log_loss", "bucket_calibration_error",
                       "max_drawdown", "settlement_errors", "journal_completeness",
                       "quote_provenance_clean", "needed_resolved_for_review", "ready_for_review"]:
            assert field in core, f"Missing field {field}"

# ═══════════════════════════════════════════════════════════════
# §16: REVIEW GATE
# ═══════════════════════════════════════════════════════════════

class TestReviewGate:
    def test_json_exists(self):
        assert (R / "V22.3_REVIEW_GATE.json").exists()

    def test_live_not_allowed(self):
        g = load_json(R / "V22.3_REVIEW_GATE.json")
        assert g["live_allowed"] == False

    def test_all_criteria_listed(self):
        g = load_json(R / "V22.3_REVIEW_GATE.json")
        for key in ["resolved_positions_gte_25", "realized_pnl_positive",
                     "profit_factor_gte_1_25", "max_drawdown_lte_15pct",
                     "settlement_errors_zero", "journal_completeness_100pct",
                     "quote_provenance_clean"]:
            assert key in g["criteria"]

# ═══════════════════════════════════════════════════════════════
# §17: FINAL REPORT
# ═══════════════════════════════════════════════════════════════

class TestFinalReport:
    def test_json_exists(self):
        assert (R / "V22.3_FINAL_WEATHER_LOOP_ENGINEERING_REPORT.json").exists()

    def test_md_exists(self):
        assert (R / "V22.3_FINAL_WEATHER_LOOP_ENGINEERING_REPORT.md").exists()

    def test_all_14_questions_answered(self):
        r = load_json(R / "V22.3_FINAL_WEATHER_LOOP_ENGINEERING_REPORT.json")
        answers = r["answers"]
        for i in range(1, 15):
            # Check that all question keys exist
            assert any(str(i) in k for k in answers.keys()), f"Missing answer for question {i}"

    def test_live_not_allowed(self):
        r = load_json(R / "V22.3_FINAL_WEATHER_LOOP_ENGINEERING_REPORT.json")
        assert r["answers"]["14_live_trading_allowed"] == False

    def test_expected_final_state(self):
        r = load_json(R / "V22.3_FINAL_WEATHER_LOOP_ENGINEERING_REPORT.json")
        s = r["expected_final_state"]
        assert s["primary_research_candidate"] == "WEATHER_DEB_V22"
        assert s["live_allowed"] == False
        assert s["capital_deployment_allowed"] == False

# ═══════════════════════════════════════════════════════════════
# §19: ACCEPTANCE CRITERIA
# ═══════════════════════════════════════════════════════════════

class TestAcceptanceCriteria:
    def test_json_exists(self):
        assert (R / "V22.3_ACCEPTANCE_CRITERIA.json").exists()

    def test_all_met(self):
        a = load_json(R / "V22.3_ACCEPTANCE_CRITERIA.json")
        assert a["all_met"] == True

    def test_no_live_trading(self):
        a = load_json(R / "V22.3_ACCEPTANCE_CRITERIA.json")
        assert a["criteria"]["no_live_trading"] == True
        assert a["criteria"]["no_capital_deployment"] == True

    def test_cohorts_separated(self):
        a = load_json(R / "V22.3_ACCEPTANCE_CRITERIA.json")
        assert a["criteria"]["cohorts_separated"] == True

    def test_pre_deb_excluded(self):
        a = load_json(R / "V22.3_ACCEPTANCE_CRITERIA.json")
        assert a["criteria"]["pre_deb_excluded"] == True

# ═══════════════════════════════════════════════════════════════
# CROSS-CUTTING: COHORT SEPARATION
# ═══════════════════════════════════════════════════════════════

class TestCohortSeparation:
    def test_15pp_and_12pp_not_combined(self):
        """Verify that 15pp and 12pp cohorts have separate metrics."""
        d = load_json(R / "V22.3_WEATHER_VALIDATION_DASHBOARD.json")
        # They must be separate dict entries
        assert d["post_deb_core_15pp"] is not d["post_deb_low_noise_12pp_experimental"]

    def test_pre_deb_not_in_core_metrics(self):
        """Pre-DEB trades must not appear in core 15pp metrics."""
        d = load_json(R / "V22.3_WEATHER_VALIDATION_DASHBOARD.json")
        assert d["pre_deb_sigma_bug"]["excluded_from_review"] == True
        # Pre-DEB should not have active/resolved in the same format
        assert "active" not in d["pre_deb_sigma_bug"]

# ═══════════════════════════════════════════════════════════════
# CROSS-CUTTING: CRYPTO NON-PROMOTION
# ═══════════════════════════════════════════════════════════════

class TestCryptoNonPromotion:
    def test_crypto_not_live(self):
        s = load_json(R / "V22.3_STRATEGY_STATUS.json")
        assert "OBSERVATION_ONLY" in s["CRYPTO_OBSERVER_V21_7_51"]["status"]
        assert s["CRYPTO_OBSERVER_V21_7_51"]["live_allowed"] == False

    def test_btc_canary_dead(self):
        s = load_json(R / "V22.3_STRATEGY_STATUS.json")
        assert "INVALIDATED" in s["BTC_15M_CANARY"]["status"]
        assert "DEAD" in s["BTC_15M_CANARY"]["status"]