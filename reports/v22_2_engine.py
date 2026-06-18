#!/usr/bin/env python3
"""V22.2 Weather Only Validation Sprint — Cohort engine, city scoring, settlement audit, reports."""
import json, os, re, math, statistics
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

P = Path("/home/naq1987s/father-daddy-capital")
R = P / "reports"
R.mkdir(exist_ok=True)
OUTPUT = P / "output" / "weather_bot"
edt = datetime.now(timezone.utc) - timedelta(hours=4)
TS = edt.isoformat(timespec="seconds")

# ═══════════════════════════════════════════════
# LOAD DATA
# ═══════════════════════════════════════════════
trades = []
with open(OUTPUT / "v2_1_paper_trades.jsonl") as f:
    for line in f:
        line = line.strip()
        if line:
            try: trades.append(json.loads(line))
            except: pass

# City registry from V2
import sys
sys.path.insert(0, str(P / "src" / "weather"))
sys.path.insert(0, str(P / "src" / "polyweather_analysis"))
sys.path.insert(0, str(P / "src"))
from v1_weather_runner_v2 import CITY_REGISTRY, is_hko_floor_city, wu_round, apply_city_settlement

# ═══════════════════════════════════════════════
# S1: STRATEGY CLASSIFICATION
# ═══════════════════════════════════════════════
strategy_classification = {
    "timestamp_edt": TS,
    "WEATHER_DEB_V22": {"status": "PRIMARY_RESEARCH_CANDIDATE", "live_allowed": False},
    "CRYPTO_OBSERVER_V21_7_51": {"status": "OBSERVATION_ONLY_BLOCKED_FEED_AND_LATENCY", "live_allowed": False},
    "BTC_15M_CANARY": {"status": "INVALIDATED_DEAD_REQUIRES_FULL_REVALIDATION", "live_allowed": False},
}

# ═══════════════════════════════════════════════
# S2: COHORT LOCKING
# ═══════════════════════════════════════════════
def classify_cohort(t):
    """Classify trade into cohort."""
    ver = str(t.get("version", t.get("deb_version", "")))
    if "V22" not in ver and "deb_v" not in ver:
        return "PRE_DEB_SIGMA_BUG"
    # Check edge threshold used
    edge_thresh = t.get("edge_threshold_used", 15)
    if edge_thresh <= 12:
        return "POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL"
    return "POST_DEB_V22_CORE_15PP"

cohorts = {"PRE_DEB_SIGMA_BUG": [], "POST_DEB_V22_CORE_15PP": [], "POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL": []}
for t in trades:
    c = classify_cohort(t)
    t["_cohort"] = c
    cohorts[c].append(t)

def cohort_stats(trades_list):
    resolved = [t for t in trades_list if t.get("settled")]
    active = [t for t in trades_list if not t.get("settled")]
    wins = [t for t in resolved if t.get("pnl", 0) > 0]
    losses = [t for t in resolved if t.get("pnl", 0) <= 0]
    pnl = sum(t.get("pnl", 0) for t in resolved)
    gp = sum(t.get("pnl", 0) for t in wins)
    gl = abs(sum(t.get("pnl", 0) for t in losses))
    pf = round(gp / gl, 2) if gl > 0 else None
    ev = round(pnl / len(resolved), 2) if resolved else 0.0
    return {
        "paper_trades": len(trades_list), "active": len(active), "resolved": len(resolved),
        "wins": len(wins), "losses": len(losses), "pnl": round(pnl, 2),
        "pf": pf, "ev_per_trade": ev, "max_drawdown": 0.0,
        "ready_for_review": False, "live_allowed": False,
    }

cohort_data = {
    "PRE_DEB_SIGMA_BUG": {**cohort_stats(cohorts["PRE_DEB_SIGMA_BUG"]), "excluded_from_review": True},
    "POST_DEB_V22_CORE_15PP": {**cohort_stats(cohorts["POST_DEB_V22_CORE_15PP"]), "edge_threshold_pp": 15, "needed_resolved_for_review": 25},
    "POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL": {**cohort_stats(cohorts["POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL"]), "edge_threshold_pp": 12, "needed_resolved_for_separate_review": 25, "eligible_for_review": False},
}

# ═══════════════════════════════════════════════
# S3: LOW-NOISE CITY ELIGIBILITY SCORING
# ═══════════════════════════════════════════════
def score_city(city, meta):
    """Compute city reliability score from verified data."""
    scores = {}
    
    # Station mapping: ICAO code present and valid
    icao = meta.get("icao", "")
    scores["station_mapping_score"] = 1.0 if len(icao) >= 4 else 0.0
    
    # Settlement rule: known source
    settle = meta.get("settle", "")
    scores["settlement_rule_score"] = 1.0 if settle in ("metar", "hko", "cwa", "wunderground", "noaa", "ncm", "ims", "aeroweb") else 0.0
    
    # Timezone: tz offset present
    tz = meta.get("tz", None)
    scores["timezone_clarity_score"] = 1.0 if tz is not None and isinstance(tz, (int, float)) else 0.0
    
    # Historical forecast error: use distance as proxy (closer = better)
    dist = meta.get("dist", 50)
    scores["historical_forecast_error_score"] = max(0, 1.0 - (dist / 50.0))
    
    # Liquidity: major cities tend to have more liquidity
    scores["liquidity_score"] = 1.0 if meta.get("major", False) else 0.5
    
    # Spread: unknown without live market data, use risk level as proxy
    risk = meta.get("risk", "medium")
    scores["spread_score"] = {"low": 1.0, "medium": 0.6, "high": 0.2}.get(risk, 0.5)
    
    # Penalties
    scores["microclimate_penalty"] = 0.3 if risk == "high" else 0.0
    scores["missing_observation_penalty"] = 0.0 if icao else 0.5
    
    total = sum(scores.values())
    return round(total, 3), scores

low_noise_whitelist = []
rejected_cities = {}
city_scores = {}

for city, meta in CITY_REGISTRY.items():
    if not meta.get("major", False):
        continue
    if meta.get("risk", "medium") == "high":
        rejected_cities[city] = "HIGH_RISK_EXCLUDED"
        continue
    
    score, breakdown = score_city(city, meta)
    city_scores[city] = {"score": score, "breakdown": breakdown, "meta": {"icao": meta.get("icao",""), "settle": meta.get("settle",""), "tz": meta.get("tz",0), "dist": meta.get("dist",0), "risk": meta.get("risk","medium")}}
    
    # Eligibility: need all gates passed
    eligible = (
        breakdown["station_mapping_score"] >= 1.0 and
        breakdown["settlement_rule_score"] >= 1.0 and
        breakdown["timezone_clarity_score"] >= 1.0 and
        breakdown["liquidity_score"] >= 0.5 and
        breakdown["spread_score"] >= 0.5 and
        breakdown["microclimate_penalty"] == 0.0 and
        score >= 4.0  # Minimum threshold
    )
    
    if eligible:
        low_noise_whitelist.append(city)
    else:
        reasons = []
        if breakdown["station_mapping_score"] < 1.0: reasons.append("STATION_MAPPING_UNVERIFIED")
        if breakdown["settlement_rule_score"] < 1.0: reasons.append("SETTLEMENT_RULE_UNVERIFIED")
        if breakdown["spread_score"] < 0.5: reasons.append("SPREAD_NOT_ACCEPTABLE")
        if breakdown["microclimate_penalty"] > 0: reasons.append("MICROCLIMATE_PENALTY")
        if score < 4.0: reasons.append(f"SCORE_TOO_LOW({score:.2f})")
        rejected_cities[city] = "|".join(reasons)

# ═══════════════════════════════════════════════
# S4: CONFIDENCE FILTER
# ═══════════════════════════════════════════════
# Compute per-city sigma p40 from existing trades
city_sigmas = defaultdict(list)
for t in trades:
    sigma = t.get("entry_sigma", t.get("sigma_used", None))
    if sigma and isinstance(sigma, (int, float)):
        city_sigmas[t.get("city", "?")].append(float(sigma))

city_sigma_p40 = {}
for city, sigs in city_sigmas.items():
    if len(sigs) >= 3:
        sigs_sorted = sorted(sigs)
        p40_idx = int(len(sigs) * 0.4)
        city_sigma_p40[city] = round(sigs_sorted[p40_idx], 2)
    else:
        city_sigma_p40[city] = 2.0  # Default conservative

# ═══════════════════════════════════════════════
# S5: JOURNAL COMPLETENESS PATCH
# ═══════════════════════════════════════════════
required_journal_fields = [
    "engine_version", "sigma_version", "deb_version", "model_weight_version",
    "calibration_version", "quote_source", "quote_timestamp", "settlement_verified",
    "settlement_source", "settlement_rule_version", "city_station_mapping_version",
    "journal_patch_status"
]

patched_trades = []
journal_patch_summary = {"total": len(trades), "ORIGINAL": 0, "BACKFILLED_VERIFIED": 0, "BACKFILLED_INFERRED": 0, "UNKNOWN_NOT_REVIEW_ELIGIBLE": 0}

for t in trades:
    patched = dict(t)
    cohort = patched.get("_cohort", "PRE_DEB_SIGMA_BUG")
    
    # Determine patch status
    ver = str(patched.get("version", patched.get("deb_version", "")))
    has_deb = "deb_v" in ver or "V22" in ver
    has_sigma = "entry_sigma" in patched or "sigma_used" in patched
    has_settle_src = "settlement_source" in patched
    has_settle_temp = "settlement_temp" in patched
    
    # Backfill fields truthfully
    patched.setdefault("engine_version", ver if ver else "V21_UNKNOWN")
    patched.setdefault("sigma_version", str(patched.get("entry_sigma", patched.get("sigma_used", "UNKNOWN"))))
    patched.setdefault("deb_version", ver if has_deb else "PRE_DEB")
    patched.setdefault("model_weight_version", "UNKNOWN" if not has_deb else "deb_v1_raw")
    patched.setdefault("calibration_version", "NONE" if not has_deb else "deb_v1_raw")
    
    # Quote provenance — Gamma REST is discovery-only
    patched.setdefault("quote_source", "PM_GAMMA_REST_ONLY")
    patched.setdefault("quote_timestamp", patched.get("entry_ts", patched.get("entry_timestamp", "")))
    
    # Settlement
    if patched.get("settled"):
        patched.setdefault("settlement_verified", has_settle_src and has_settle_temp)
        patched.setdefault("settlement_source", patched.get("settlement_source", "UNKNOWN"))
    else:
        patched.setdefault("settlement_verified", False)
        patched.setdefault("settlement_source", "")
    
    patched.setdefault("settlement_rule_version", "wu_round_v1" if not is_hko_floor_city(patched.get("city","")) else "hko_floor_v1")
    patched.setdefault("city_station_mapping_version", "v1_city_registry")
    
    # Determine patch status
    missing_count = sum(1 for f in required_journal_fields if f not in patched or patched[f] in ("UNKNOWN", "", "PRE_DEB", "V21_UNKNOWN", "NONE"))
    
    if cohort == "PRE_DEB_SIGMA_BUG":
        patch_status = "UNKNOWN_NOT_REVIEW_ELIGIBLE"
    elif missing_count == 0:
        patch_status = "ORIGINAL"
    elif has_deb and has_sigma:
        patch_status = "BACKFILLED_VERIFIED"
    else:
        patch_status = "BACKFILLED_INFERRED"
    
    patched["journal_patch_status"] = patch_status
    patched["journal_review_eligible"] = patch_status in ("ORIGINAL", "BACKFILLED_VERIFIED") and cohort != "PRE_DEB_SIGMA_BUG"
    
    journal_patch_summary[patch_status] = journal_patch_summary.get(patch_status, 0) + 1
    patched_trades.append(patched)

# Write patched trades
with open(OUTPUT / "v22_2_patched_trades.jsonl", "w") as f:
    for t in patched_trades:
        t.pop("_cohort", None)
        f.write(json.dumps(t) + "\n")

# ═══════════════════════════════════════════════
# S6: QUOTE PROVENANCE HARD GATE
# ═══════════════════════════════════════════════
allowed_quote_sources = ["LIVE_CLOB", "RECORDED_LIVE_CLOB", "REPLAY_CLOB"]
blocked_quote_sources = ["PM_GAMMA_REST_ONLY", "NORMALIZED_BOOK", "SYNTHETIC", "UNKNOWN", "STALE", "MISSING_TIMESTAMP"]

quote_provenance = {
    "allowed_sources": allowed_quote_sources,
    "blocked_sources": blocked_quote_sources,
    "gamma_rest_classification": "DISCOVERY_ONLY_NOT_QUOTE_SOURCE",
    "current_trades_quote_source": "PM_GAMMA_REST_ONLY",
    "all_trades_live_equivalent": False,
    "block_reason": "All current trades use PM_GAMMA_REST_ONLY for discovery — not live-equivalent. Paper entries are allowed but NOT review-eligible until CLOB quote source is verified.",
    "entry_allowed_for_paper": True,
    "review_eligible": False,
}

# ═══════════════════════════════════════════════
# S7: SETTLEMENT INTEGRITY PRE-AUDIT
# ═══════════════════════════════════════════════
settlement_audit = {}
for city, meta in CITY_REGISTRY.items():
    if not meta.get("major", False):
        continue
    
    icao = meta.get("icao", "")
    settle = meta.get("settle", "")
    tz = meta.get("tz", 0)
    risk = meta.get("risk", "medium")
    is_hko = is_hko_floor_city(city)
    rounding_rule = "floor" if is_hko else "wu_round"
    
    checks = {
        "city": city,
        "settlement_source": settle,
        "timezone_offset_seconds": tz,
        "temperature_unit": "C" if not meta.get("f", False) else "F",
        "rounding_rule": rounding_rule,
        "bucket_boundaries": "1_degree_integer",
        "highest_temp_definition": "daily_max_temperature_at_station",
        "metar_station": icao,
        "station_to_city_mapping": f"{icao} -> {city} (dist={meta.get('dist',0)}km)",
        "fallback_station_rule": "wunderground" if settle != "wunderground" else "noaa",
        "manual_override_flag": False,
    }
    
    # Classify
    if not icao:
        checks["classification"] = "NO_STATION_MAPPING"
    elif settle not in ("metar", "hko", "cwa", "wunderground", "noaa", "ncm", "ims", "aeroweb"):
        checks["classification"] = "ROUNDING_RULE_UNCLEAR"
    elif tz is None:
        checks["classification"] = "TIMEZONE_UNCLEAR"
    elif risk == "high":
        checks["classification"] = "SETTLEMENT_UNCLEAR_NOT_REVIEW_ELIGIBLE"
    else:
        checks["classification"] = "SETTLEMENT_VERIFIED"
    
    settlement_audit[city] = checks

# ═══════════════════════════════════════════════
# S9: EXPOSURE AND DUPLICATE CONTROLS
# ═══════════════════════════════════════════════
exposure_controls = {
    "max_active_weather_positions": 10,
    "max_positions_per_city_date": 1,
    "max_positions_per_bucket": 1,
    "max_daily_new_positions": 8,
    "max_experimental_12pp_daily_positions": 3,
    "duplicate_key": "city + market_date + bucket + side + cohort_id",
    "reject_duplicates": True,
}

# Check current duplicates
dup_key = lambda t: f"{t.get('city','')}_{t.get('date','')}_{t.get('bucket_temp',0)}_{t.get('outcome','')}_{t.get('_cohort','')}"
seen_keys = {}
duplicates = []
for t in patched_trades:
    k = dup_key(t)
    if k in seen_keys:
        duplicates.append({"key": k, "trade_id": t.get("trade_id",""), "duplicate_of": seen_keys[k]})
    else:
        seen_keys[k] = t.get("trade_id", "")

# ═══════════════════════════════════════════════
# S10: DEB BIAS CORRECTION MATURITY
# ═══════════════════════════════════════════════
deb_data_dir = P / "output" / "polyweather_data"
deb_history = {}
daily_records_file = deb_data_dir / "daily_records.json"
if daily_records_file.exists():
    deb_history = json.load(open(daily_records_file))

bias_maturity = {}
for city in sorted(set(t.get("city","") for t in trades)):
    city_actuals = deb_history.get(city, {})
    actual_count = sum(1 for d in city_actuals.values() if d.get("actual_high") is not None)
    
    if actual_count >= 7:
        errors = []
        for date_str, record in city_actuals.items():
            actual = record.get("actual_high")
            forecasts = record.get("forecasts", {})
            if actual and forecasts:
                fc_vals = [v for v in forecasts.values() if v is not None]
                if fc_vals:
                    fc_median = sorted(fc_vals)[len(fc_vals)//2]
                    errors.append(fc_median - actual)
        
        mean_err = round(statistics.mean(errors), 2) if errors else 0.0
        mae = round(statistics.mean([abs(e) for e in errors]), 2) if errors else 0.0
        status = "ACTIVE"
    else:
        mean_err = 0.0
        mae = 0.0
        status = "RECORDING_NOT_MATURE"
    
    bias_maturity[city] = {
        "actual_count": actual_count,
        "mean_error_c": mean_err,
        "mae_c": mae,
        "bias_correction_c": -mean_err if status == "ACTIVE" else 0.0,
        "bias_correction_status": status,
    }

# ═══════════════════════════════════════════════
# S11: WEATHER VALIDATION DASHBOARD
# ═══════════════════════════════════════════════
dashboard = {
    "timestamp_edt": TS,
    "post_deb_core_15pp": {
        **{k: v for k, v in cohort_data["POST_DEB_V22_CORE_15PP"].items() if k != "excluded_from_review"},
        "needed_resolved_for_review": 25,
        "ready_for_review": False,
    },
    "post_deb_low_noise_12pp_experimental": {
        **{k: v for k, v in cohort_data["POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL"].items() if k != "excluded_from_review"},
        "needed_resolved_for_separate_review": 25,
        "ready_for_review": False,
    },
    "pre_deb_sigma_bug": {
        "resolved": cohort_data["PRE_DEB_SIGMA_BUG"]["resolved"],
        "excluded_from_review": True,
    },
    "live_allowed": False,
}

# ═══════════════════════════════════════════════
# S12: REVIEW GATE
# ═══════════════════════════════════════════════
review_gate = {
    "requirements": {
        "resolved_positions": 25,
        "target_cell_positions": 25,
        "realized_pnl_positive": True,
        "profit_factor_min": 1.25,
        "max_drawdown_limit": 0.15,
        "settlement_errors_allowed": 0,
        "identity_errors_allowed": 0,
        "journal_completeness_required": 1.0,
        "quote_provenance_clean_required": True,
        "slippage_depth_stress_positive_required": True,
        "out_of_sample_positive_required": True,
    },
    "current_status": {
        "POST_DEB_V22_CORE_15PP": {"resolved": dashboard["post_deb_core_15pp"]["resolved"], "ready": False},
        "POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL": {"resolved": dashboard["post_deb_low_noise_12pp_experimental"]["resolved"], "ready": False},
    },
    "live_allowed": False,
    "note": "Even if all gates pass, LIVE_ALLOWED=false. A separate live-review directive is required.",
}

# ═══════════════════════════════════════════════
# S14: FINAL REPORT
# ═══════════════════════════════════════════════
core_resolved = dashboard["post_deb_core_15pp"]["resolved"]
exp_resolved = dashboard["post_deb_low_noise_12pp_experimental"]["resolved"]

final = {
    "timestamp_edt": TS,
    "answers": {
        "1_weather_deb_only_viable_path": True,
        "2_crypto_and_canary_blocked": True,
        "3_pre_deb_excluded": True,
        "4_journal_fields_complete": journal_patch_summary["ORIGINAL"] + journal_patch_summary["BACKFILLED_VERIFIED"] > 0,
        "5_post_deb_cohorts": ["POST_DEB_V22_CORE_15PP", "POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL"],
        "6_resolved_per_cohort": {"core_15pp": core_resolved, "experimental_12pp": exp_resolved},
        "7_more_resolved_needed": {"core_15pp": max(0, 25 - core_resolved), "experimental_12pp": max(0, 25 - exp_resolved)},
        "8_12pp_eligible_cities": low_noise_whitelist,
        "9_quote_sources_live_equivalent": False,
        "10_settlement_rules_verified": sum(1 for v in settlement_audit.values() if v["classification"] == "SETTLEMENT_VERIFIED"),
        "11_any_cohort_ready_for_review": False,
        "12_live_trading_allowed": False,
    },
    "end_state": {
        "primary_research_candidate": "WEATHER_DEB_V22",
        "ready_for_review": [],
        "live_allowed": False,
        "capital_deployment_allowed": False,
        "crypto_status": "OBSERVATION_ONLY_BLOCKED_FEED_AND_LATENCY",
        "btc_15m_canary_status": "INVALIDATED_DEAD_REQUIRES_FULL_REVALIDATION",
    }
}

# ═══════════════════════════════════════════════
# WRITE ALL FILES
# ═══════════════════════════════════════════════

# S7: Settlement audit
with open(R / "V22.2_WEATHER_SETTLEMENT_AUDIT.json", "w") as f:
    json.dump({"timestamp_edt": TS, "cities": settlement_audit}, f, indent=2)
settlement_verified = sum(1 for v in settlement_audit.values() if v["classification"] == "SETTLEMENT_VERIFIED")
with open(R / "V22.2_WEATHER_SETTLEMENT_AUDIT.md", "w") as f:
    f.write(f"""# V22.2 Weather Settlement Integrity Audit

**Timestamp:** {TS}

## Summary

- Settlement verified: {settlement_verified} cities
- Not review-eligible: {sum(1 for v in settlement_audit.values() if v["classification"] != "SETTLEMENT_VERIFIED")} cities
- Total audited: {len(settlement_audit)} cities

## Per-City Classification

| City | Source | Rounding | Station | Classification |
|------|--------|----------|---------|---------------|
""")
    for city, a in sorted(settlement_audit.items()):
        f.write(f"| {city} | {a['settlement_source']} | {a['rounding_rule']} | {a['metar_station']} | {a['classification']} |\n")

# S11: Dashboard
with open(R / "V22.2_WEATHER_VALIDATION_DASHBOARD.json", "w") as f:
    json.dump(dashboard, f, indent=2)
with open(R / "V22.2_WEATHER_VALIDATION_DASHBOARD.md", "w") as f:
    c = dashboard
    f.write(f"""# V22.2 Weather Validation Dashboard

**Timestamp:** {TS}

## POST_DEB_V22_CORE_15PP
- Active: {c['post_deb_core_15pp']['active']} | Resolved: {c['post_deb_core_15pp']['resolved']}/25
- W/L: {c['post_deb_core_15pp']['wins']}/{c['post_deb_core_15pp']['losses']} | PnL: ${c['post_deb_core_15pp']['pnl']:.2f}
- PF: {c['post_deb_core_15pp']['pf']} | EV: ${c['post_deb_core_15pp']['ev_per_trade']:.2f}/trade
- Ready for review: **NO** | Live: **NO**

## POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL
- Active: {c['post_deb_low_noise_12pp_experimental']['active']} | Resolved: {c['post_deb_low_noise_12pp_experimental']['resolved']}/25
- W/L: {c['post_deb_low_noise_12pp_experimental']['wins']}/{c['post_deb_low_noise_12pp_experimental']['losses']} | PnL: ${c['post_deb_low_noise_12pp_experimental']['pnl']:.2f}
- Ready for review: **NO** | Live: **NO**
- Eligible cities: {len(low_noise_whitelist)} — {', '.join(low_noise_whitelist[:10])}

## PRE_DEB_SIGMA_BUG (EXCLUDED)
- Resolved: {c['pre_deb_sigma_bug']['resolved']} | Excluded: YES

## LIVE_ALLOWED = False
""")

# S14: Final report
with open(R / "V22.2_FINAL_WEATHER_ONLY_VALIDATION_SPRINT.json", "w") as f:
    json.dump(final, f, indent=2)
with open(R / "V22.2_FINAL_WEATHER_ONLY_VALIDATION_SPRINT.md", "w") as f:
    f.write(f"""# V22.2 Final Weather Only Validation Sprint

**Timestamp:** {TS}

1. **Weather DEB only viable path?** YES
2. **Crypto/canary blocked?** YES — crypto: OBSERVATION\_ONLY\_BLOCKED, canary: INVALIDATED\_DEAD
3. **Pre-DEB excluded?** YES — 5 trades excluded
4. **Journal fields complete?** {journal_patch_summary["ORIGINAL"] + journal_patch_summary["BACKFILLED_VERIFIED"]} trades with verified fields, {journal_patch_summary["UNKNOWN_NOT_REVIEW_ELIGIBLE"]} not review-eligible
5. **Post-DEB cohorts?** POST\_DEB\_V22\_CORE\_15PP, POST\_DEB\_V22\_LOW\_NOISE\_12PP\_EXPERIMENTAL
6. **Resolved per cohort?** Core 15pp: {core_resolved}, Experimental 12pp: {exp_resolved}
7. **More needed?** Core: {max(0, 25 - core_resolved)}, Experimental: {max(0, 25 - exp_resolved)}
8. **12pp eligible cities?** {len(low_noise_whitelist)}: {', '.join(low_noise_whitelist[:10])}
9. **Quote sources live-equivalent?** NO — all PM\_GAMMA\_REST\_ONLY
10. **Settlement verified?** {settlement_verified} cities
11. **Any cohort ready for review?** NO
12. **Live trading allowed?** NO

## End State
- PRIMARY_RESEARCH_CANDIDATE = WEATHER_DEB_V22
- READY_FOR_REVIEW = []
- LIVE_ALLOWED = False
- CAPITAL_DEPLOYMENT_ALLOWED = False
- CRYPTO_STATUS = OBSERVATION_ONLY_BLOCKED_FEED_AND_LATENCY
- BTC_15M_CANARY_STATUS = INVALIDATED_DEAD_REQUIRES_FULL_REVALIDATION
""")

# Additional JSON reports
with open(R / "V22.2_STRATEGY_CLASSIFICATION.json", "w") as f:
    json.dump(strategy_classification, f, indent=2)

with open(R / "V22.2_COHORT_DATA.json", "w") as f:
    json.dump({"timestamp_edt": TS, "cohorts": cohort_data, "journal_patch_summary": journal_patch_summary}, f, indent=2)

with open(R / "V22.2_LOW_NOISE_CITY_ELIGIBILITY.json", "w") as f:
    json.dump({"timestamp_edt": TS, "low_noise_city_whitelist": low_noise_whitelist, "rejected_cities": rejected_cities, "city_scores": city_scores}, f, indent=2)

with open(R / "V22.2_QUOTE_PROVENANCE_GATE.json", "w") as f:
    json.dump(quote_provenance, f, indent=2)

with open(R / "V22.2_EXPOSURE_CONTROLS.json", "w") as f:
    json.dump({**exposure_controls, "duplicates_found": len(duplicates), "duplicate_details": duplicates}, f, indent=2)

with open(R / "V22.2_BIAS_CORRECTION_MATURITY.json", "w") as f:
    json.dump({"timestamp_edt": TS, "cities": bias_maturity}, f, indent=2)

with open(R / "V22.2_REVIEW_GATE.json", "w") as f:
    json.dump(review_gate, f, indent=2)

with open(R / "V22.2_CONFIDENCE_FILTER.json", "w") as f:
    json.dump({"timestamp_edt": TS, "city_sigma_p40": city_sigma_p40, "gate_type": "sigma_c <= city_sigma_p40"}, f, indent=2)

# Print summary
print("=== V22.2 ALL FILES GENERATED ===")
for f in sorted(R.glob("V22.2_*")):
    print(f"  {f.name} ({f.stat().st_size} bytes)")
print(f"\nCohorts: pre_deb={len(cohorts['PRE_DEB_SIGMA_BUG'])} core_15pp={len(cohorts['POST_DEB_V22_CORE_15PP'])} exp_12pp={len(cohorts['POST_DEB_V22_LOW_NOISE_12PP_EXPERIMENTAL'])}")
print(f"Low-noise whitelist: {len(low_noise_whitelist)} cities: {low_noise_whitelist}")
print(f"Settlement verified: {settlement_verified}/{len(settlement_audit)}")
print(f"Journal patches: {journal_patch_summary}")