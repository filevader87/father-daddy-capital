# V21.7.52 — Weather Bot Live-Readiness Audit Report

**Classification:** P0 weather strategy audit / plumbing validation
**Date:** 2026-06-16T11:38:32.866664+00:00
**Directive:** V21.7.52
**Status:** WEATHER_LIVE_BLOCKED_PENDING_EVIDENCE

---

## 1. Executive Summary

The weather bot is **NOT ready for live trading**. The prior 0W/5L result (now 1W/9L with open positions) was caused by a **catastrophic forecast model error**: the probability model used a fixed sigma of 0.3°C when actual forecast errors ranged from 3.1°C to 7.2°C — a 10-40x understatement of uncertainty. This made the bot believe P(≥threshold) was 95-99% for trades where the true probability was near the market price.

**The bot's plumbing works.** Market discovery, forecast ingestion, settlement, and question parsing all function correctly. The failure is in the **probability model**, not the infrastructure.

### Key Findings
- **Root Cause:** FORECAST_MODEL_ERROR (sigma=0.3°C vs actual errors 3-12°C)
- **Infrastructure:** Market discovery ✅, Forecast ingestion ✅, Question parsing ✅, Settlement ✅
- **Probability Model:** BROKEN — treats ensemble mean as near-certain
- **Edge Model:** OVERSTATED — claimed 73pp avg edge, realized -100pp
- **Live Readiness:** 3/12 gates passed — **BLOCKED**

---

## 2. Current Status

| Metric | Value |
|---|---|
| Weather Mode | WEATHER_DAILY_PAPER_CALIBRATION |
| Live Allowed | **false** |
| Temperature Entries | HALTED (since V21.7.14) |
| Settled Trades | 5 (0 wins, 5 losses) |
| Net PnL | -$7.60 |
| Open Positions | 5 (additional -$7.6 at risk) |
| Bankroll | $9.64 (started at $20, 52% drawdown) |
| Consecutive Losses | 5 |
| Temperature Quarantine | Until directive lifted |

---

## 3. Architecture Inventory

- **Modules:** 4 source files
- **City Registry:** 50 cities
- **Risk Profiles:** ['low', 'medium', 'high']
- **Forecast Sources:** Open-Meteo (point + ensemble), Weather Underground (settlement)
- **Settlement Sources:** METAR, WU, NOAA, HKO, CWA, IMS, NCM, AeroWeb
- **Output Files:** 20 weather bot outputs

### Module Breakdown
- **v1_weather_runner.py** (40KB): 23 functions
- **v1_weather_runner_v2.py** (64KB): 30 functions
- **v1_weather_runner_v21.py** (41KB): 18 functions
- **v2_3_rain_shadow_cell.py** (31KB): 7 functions


---

## 4. Market Discovery

- **Temperature Markets:** Discovered successfully for major cities via Gamma API slug lookup
- **Rain Markets:** Discovery infrastructure exists (v2_3_rain_shadow_cell)
- **Discovery Method:** Direct slug lookup (`highest-temperature-in-{city}-on-{date}`)
- **Resolution Sources:** Weather Underground, METAR, NOAA — all linked via CITY_REGISTRY
- **Market Structure:** Polymarket neg_risk events with temperature bucket outcomes (1°C buckets)

---

## 5. Forecast Sources

- **Primary:** Open-Meteo forecast API (free, no auth)
- **Ensemble:** Open-Meteo ensemble API (30 members, free, no auth)
- **Latency:** ~200-500ms per request
- **Coverage:** 50 cities in CITY_REGISTRY
- **CRITICAL ISSUE:** Ensemble data (30 members with spread) is available but **NOT USED** in probability model
  - Model uses fixed sigma=0.3°C instead of ensemble spread
  - Ensemble spread would provide city-specific, date-specific uncertainty estimates

---

## 6. Probability Model — THE CRITICAL FAILURE

### What the model does:
1. Fetches ensemble mean from Open-Meteo
2. Applies Gaussian CDF: P(≥bucket) = 1 - Φ((forecast - bucket) / sigma)
3. Uses **fixed sigma = 0.3°C** for ALL cities, ALL dates
4. Computes edge = (forecast_prob - market_prob) × 100

### Why this fails:
- sigma=0.3°C means the model thinks forecast is accurate within ±0.3°C
- Actual errors: Amsterdam 7.2°C, Moscow 8°C, Helsinki 10°C, London 5.2°C
- The model claimed P(≥22°C Amsterdam) = 99% when actual was 13°C
- This is a **10-40x understatement of uncertainty**

### What should happen:
- Use ensemble spread as dynamic sigma (typically 1-4°C)
- Apply resolution uncertainty penalty (station distance)
- Apply forecast horizon penalty (further = more uncertain)
- Never claim P>90% on weather forecasts

---

## 7. Prior 0W/5L Failure Review

| Trade | City | Forecast | Actual | Error | sigma | Claimed Edge | Result |
|---|---|---|---|---|---|---|---|
| AMS-22Y | Amsterdam | 20.2°C | 13°C | 7.2°C | 0.3 | 93.5pp | LOSS |
| IST-24Y | Istanbul | 23.1°C | 20°C | 3.1°C | 0.3 | 65.5pp | LOSS |
| HEL-23Y | Helsinki | 25°C | 13°C | 12°C | 0.3 | 61pp | LOSS |
| MOS-25Y | Moscow | 27°C | 19°C | 8°C | 0.3 | 96pp | LOSS |
| LON-20Y | London | 18.2°C | 13°C | 5.2°C | 0.3 | 95.5pp | LOSS |

**Pattern:** ALL 5 losses were FORECAST_MODEL_ERROR. The model over-estimated probability by treating the ensemble mean as near-certain.

---

## 8. Settlement and Resolution

- **Settlement Correct:** Yes — all 5 settled trades resolved correctly
- **Settlement Sources:** WU, METAR, NOAA — all matched station data
- **Rounding:** wu_round applied consistently
- **Timezone:** All offsets correct (CITY_REGISTRY maps ICAO → UTC offset)
- **No settlement errors detected**

---

## 9. Live Readiness Gates

| Gate | Required | Actual | Passed |
|---|---|---|---|
| Resolved paper entries ≥ 25 | 25 | 5 | ❌ |
| WR > baseline | >0.50 | 0.0 | ❌ |
| Net EV > 0 | >0 | -$7.60 | ❌ |
| PF ≥ 1.25 | ≥1.25 | 0.0 | ❌ |
| Brier score acceptable | <0.25 | EXTREME | ❌ |
| Forecast source validated | Yes | No (sigma broken) | ❌ |
| Station/timezone validated | Yes | Partial | ❌ |
| Parse errors = 0 | 0 | 0 | ✅ |
| Settlement errors = 0 | 0 | 0 | ✅ |
| Journal completeness | 100% | 100% | ✅ |
| Sigma calibrated | Yes | No | ❌ |
| Edge model validated | Yes | No | ❌ |

**3/12 gates passed. LIVE BLOCKED.**

---

## 10. Blockers

1. **sigma=0.3°C is catastrophically understated** — must use ensemble spread
2. **Edge model claims 73pp avg, realized -100pp** — completely broken
3. **Only 5 resolved paper trades** — need 25+ for statistical significance
4. **0% WR** — no evidence of positive edge
5. **Bankroll -58.5% drawdown** — insufficient capital even if model were fixed
6. **Ensemble spread available but unused** — infrastructure exists, model doesn't use it

---

## 11. Required Fixes Before Live Consideration

1. **Replace fixed sigma with ensemble spread** — use std_dev of 30-member ensemble as dynamic sigma
2. **Cap maximum probability at 85%** — never claim P>85% on weather
3. **Add resolution uncertainty penalty** — +0.5°C sigma per 10km station distance
4. **Add forecast horizon penalty** — +0.5°C sigma per day from forecast to target
5. **Accumulate 25+ resolved paper trades** with new model before evaluating WR
6. **Validate all 50 settlement sources** against actual station data
7. **Re-run hindcast** with ensemble-based sigma

---

## 12. Recommendation

**DO NOT enable live weather trading.**

The weather bot's infrastructure (discovery, ingestion, settlement) works correctly. The probability model is the sole failure point — it treats weather forecasts as near-certain when they are not. Fix the sigma model, accumulate 25+ paper trades with the corrected model, and re-evaluate. Until then:

- WEATHER_MODE = WEATHER_DAILY_PAPER_CALIBRATION
- WEATHER_LIVE_ALLOWED = false
- Temperature entries = HALTED
- Rain entries = BLOCKED

**Sample size needed:** 25+ resolved paper trades with corrected sigma model showing positive EV and PF ≥ 1.25.

---

*Report generated by V21.7.52 Weather Live-Readiness Audit*
*Weather bot infrastructure: WORKING. Probability model: BROKEN. Live: BLOCKED.*
