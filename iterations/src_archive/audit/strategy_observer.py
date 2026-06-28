#!/usr/bin/env python3
"""
FDC Strategy Observer Bot
=========================
Reviews and audits all trading bot strategies, suggests optimization
methodologies, and tracks strategy performance over time.

Analyzes:
  - Entry signal quality (edge distribution, signal-to-trade ratio)
  - Exit/ settlement logic (slippage, time stop effectiveness)
  - Risk management (position sizing, circuit breakers, drawdown control)
  - Market selection (which assets/cities/markets are profitable)
  - Strategy decay (is edge shrinking over time?)
  - Calibration (model probability vs actual hit rate)
  - Portfolio diversification (concentration risk)
  - Execution quality (fill rates, rejection reasons)

Outputs:
  - Strategy audit report with scored dimensions
  - Optimization recommendations ranked by expected impact
  - Performance trajectory (improving/stable/decaying)

Usage:
  python3 src/audit/strategy_observer.py              # one-shot audit
  python3 src/audit/strategy_observer.py --monitor      # continuous (every 30min)
  python3 src/audit/strategy_observer.py --bot weather  # audit specific bot
  python3 src/audit/strategy_observer.py --bot canary    # audit specific bot
"""
from __future__ import annotations
import json, math, os, sys, time, statistics
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict, Counter
import random

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "output" / "strategy_observer"
OUT.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════
# DATA LOADERS
# ═══════════════════════════════════════════════════════════════

def load_weather_trades() -> List[Dict]:
    """Load weather bot LIVE trades from JSONL — REAL PM orders only.
    Filters out paper entries (no order_id or empty order_id)."""
    f = ROOT / "output" / "weather_bot" / "v2_1_live_trades.jsonl"
    if not f.exists():
        return []
    trades = []
    for line in f.read_text().splitlines():
        if line.strip():
            try:
                t = json.loads(line)
                # Only include real PM orders: must have non-empty order_id
                oid = t.get("order_id", "")
                if oid and len(str(oid)) > 10:
                    trades.append(t)
            except:
                pass
    return trades

def load_canary_trades() -> List[Dict]:
    """Load canary LIVE resolved positions (paper/live separated V21.7.65+)."""
    f = ROOT / "output" / "v21762_scalper_canary" / "live_resolved.jsonl"
    if not f.exists():
        # Fallback: check V21.7.17 canary positions (older path)
        f = ROOT / "output" / "v21717_live_bridge" / "canary_positions.jsonl"
        if not f.exists():
            return []
    trades = []
    for line in f.read_text().splitlines():
        if line.strip():
            try:
                trades.append(json.loads(line))
            except:
                pass
    return trades

def load_canary_orders() -> List[Dict]:
    """Load canary live orders (includes rejected entries)."""
    f = ROOT / "output" / "v21762_scalper_canary" / "live_positions.jsonl"
    if not f.exists():
        # Fallback: check V21.7.17 canary orders (older path)
        f = ROOT / "output" / "v21717_live_bridge" / "canary_orders.jsonl"
        if not f.exists():
            return []
    trades = []
    for line in f.read_text().splitlines():
        if line.strip():
            try:
                trades.append(json.loads(line))
            except:
                pass
    return trades

def load_canary_heartbeat() -> Dict:
    """Load latest canary heartbeat."""
    f = ROOT / "output" / "v21762_scalper_canary" / "heartbeat.jsonl"
    if not f.exists():
        return {}
    lines = f.read_text().splitlines()
    for line in reversed(lines):
        if line.strip():
            try:
                return json.loads(line)
            except:
                pass
    return {}

def load_weather_state() -> Dict:
    """Load weather bot state. Uses live state if available, falls back to paper state."""
    for fname in ["v2_1_live_state.json", "v2_1_state.json"]:
        f = ROOT / "output" / "weather_bot" / fname
        if f.exists():
            return json.loads(f.read_text())
    return {}

# ═══════════════════════════════════════════════════════════════
# ANALYSIS DIMENSIONS
# ═══════════════════════════════════════════════════════════════

@dataclass
class DimensionScore:
    name: str
    score: float  # 0-100
    grade: str    # A-F
    findings: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)

def grade_from_score(score: float) -> str:
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 70: return "C"
    if score >= 60: return "D"
    return "F"

@dataclass
class StrategyAudit:
    bot_name: str
    timestamp: str
    total_trades: int
    dimensions: List[DimensionScore] = field(default_factory=list)
    overall_score: float = 0.0
    overall_grade: str = "F"
    top_recommendations: List[str] = field(default_factory=list)
    performance_trajectory: str = "unknown"  # improving/stable/decaying

# ═══════════════════════════════════════════════════════════════
# WEATHER BOT AUDIT
# ═══════════════════════════════════════════════════════════════

def audit_weather_bot() -> StrategyAudit:
    trades = load_weather_trades()
    state = load_weather_state()
    settled = [t for t in trades if t.get("settled")]
    unsettled = [t for t in trades if not t.get("settled")]

    audit = StrategyAudit(
        bot_name="Weather Bot V21.7.54",
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_trades=len(trades),
    )

    # ─── NO DATA GUARD ───
    if not trades:
        audit.overall_score = 0.0
        audit.overall_grade = "N/A"
        audit.performance_trajectory = "no_data"
        audit.dimensions.append(DimensionScore(
            name="No Data",
            score=0, grade="N/A",
            findings=[
                "Zero trades found in v2_1_live_trades.jsonl",
                "Cannot score strategy with no trade data",
            ],
            recommendations=[
                "Do NOT report any trade count, WR, or PnL until actual trades exist",
            ],
        ))
        return audit

    # ─── 1. ENTRY SIGNAL QUALITY ───
    yes_trades = [t for t in settled if t.get("outcome") == "YES"]
    no_trades = [t for t in settled if t.get("outcome") == "NO"]

    yes_wins = [t for t in yes_trades if t.get("pnl", 0) > 0]
    no_wins = [t for t in no_trades if t.get("pnl", 0) > 0]

    yes_wr = len(yes_wins) / len(yes_trades) * 100 if yes_trades else 0
    no_wr = len(no_wins) / len(no_trades) * 100 if no_trades else 0

    # Edge distribution
    edges = [t.get("best_edge", t.get("edge_pp", 0)) for t in trades if t.get("best_edge") or t.get("edge_pp")]
    avg_edge = statistics.mean(edges) if edges else 0

    findings = []
    recs = []
    score = 70  # base

    if yes_wr < 20 and len(yes_trades) > 5:
        findings.append(f"YES side WR={yes_wr:.1f}% ({len(yes_wins)}/{len(yes_trades)}) — consistently losing")
        recs.append("Consider disabling YES entries entirely or raising YES edge threshold to 40pp+")
        score -= 20

    if no_wr > 50:
        findings.append(f"NO side WR={no_wr:.1f}% — strong edge on NO side")
        score += 10

    if avg_edge > 30:
        findings.append(f"High avg edge={avg_edge:.1f}pp — model disagrees with market significantly")
        recs.append("Verify model isn't overconfident — high edge with low YES WR suggests calibration issue")
        score -= 5

    # Signal-to-trade ratio
    signal_count = len(trades)  # proxy
    if signal_count < 50:
        findings.append(f"Low trade count ({signal_count}) — insufficient sample for statistical confidence")
        recs.append("Need 100+ trades for reliable strategy assessment")
        score -= 10

    audit.dimensions.append(DimensionScore(
        name="Entry Signal Quality", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    # ─── 2. RISK MANAGEMENT ───
    score = 80
    findings = []
    recs = []

    # Position sizing
    position_sizes = [t.get("position_size", t.get("cost_usd", 0)) for t in trades if t.get("position_size") or t.get("cost_usd")]
    if position_sizes:
        avg_size = statistics.mean(position_sizes)
        max_size = max(position_sizes)
        if max_size > avg_size * 3:
            findings.append(f"Position size variance high: avg=${avg_size:.2f} max=${max_size:.2f}")
            recs.append("Standardize position sizing — high variance suggests inconsistent risk allocation")
            score -= 10

    # Circuit breaker
    if state.get("halted"):
        findings.append(f"Bot currently HALTED: {state.get('halt_reason', 'no reason')}")
        score -= 15

    weekly_loss = state.get("weekly_loss", 0)
    if weekly_loss < -15:
        findings.append(f"Weekly loss ${weekly_loss:.2f} approaching -$20 limit")
        recs.append("Consider reducing position sizes when weekly loss exceeds -$10")
        score -= 10

    # Drawdown
    pnls = [t.get("pnl", 0) for t in settled]
    if pnls:
        peak = 0
        cumsum = 0
        max_dd = 0
        for p in pnls:
            cumsum += p
            peak = max(peak, cumsum)
            dd = peak - cumsum
            max_dd = max(max_dd, dd)
        if max_dd > 15:
            findings.append(f"Max drawdown ${max_dd:.2f} — high relative to bankroll")
            recs.append("Implement progressive position sizing: reduce size after consecutive losses")
            score -= 10
        else:
            findings.append(f"Max drawdown ${max_dd:.2f} — controlled")

    audit.dimensions.append(DimensionScore(
        name="Risk Management", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    # ─── 3. CALIBRATION ───
    score = 60
    findings = []
    recs = []

    # Weather bot: forecast_prob = P(YES = this bucket is the max temp)
    # For NO side trades, P(win) = 1 - forecast_prob (bot wins if YES is NOT the max)
    # For YES side trades, P(win) = forecast_prob
    # Compare P(win) against actual hit rate
    model_probs = []
    actual_hits = []
    for t in settled:
        fp = t.get("forecast_prob", None)
        outcome = t.get("outcome", "").upper()
        if fp is not None and outcome:
            if outcome == "NO":
                p_win = 1.0 - fp
            else:  # YES
                p_win = fp
            model_probs.append(p_win)
            actual_hits.append(1 if t.get("pnl", 0) > 0 else 0)

    if len(model_probs) > 5:
        avg_model_prob = statistics.mean(model_probs)
        actual_hit_rate = sum(actual_hits) / len(actual_hits)
        calibration_error = abs(avg_model_prob - actual_hit_rate)

        findings.append(f"Model P(win) avg={avg_model_prob:.1%} vs actual hit rate={actual_hit_rate:.1%}")
        findings.append(f"Calibration error={calibration_error:.1%}")

        if calibration_error > 0.20:
            recs.append("Large calibration gap — model is overconfident. Apply conformal calibration or Platt scaling")
            recs.append("Bucket predictions into deciles and compare predicted vs actual per bucket")
            score -= 20
        elif calibration_error > 0.10:
            recs.append("Moderate calibration error — apply isotonic regression to calibrate")
            score -= 10
        else:
            findings.append("Model is well-calibrated")
            score += 15

        # Brier score
        brier = sum((mp - hit) ** 2 for mp, hit in zip(model_probs, actual_hits)) / len(model_probs)
        findings.append(f"Brier score={brier:.3f} (lower is better, 0=perfect)")
        if brier > 0.25:
            recs.append(f"Brier score {brier:.3f} is high — model predictions add little information")
            score -= 10
    else:
        findings.append("Insufficient data for calibration analysis")

    audit.dimensions.append(DimensionScore(
        name="Calibration", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    # ─── 4. PORTFOLIO DIVERSIFICATION ───
    score = 70
    findings = []
    recs = []

    city_pnl = defaultdict(list)
    for t in settled:
        city = t.get("city", "?")
        city_pnl[city].append(t.get("pnl", 0))

    if city_pnl:
        city_totals = {c: sum(p) for c, p in city_pnl.items()}
        total_pnl = sum(city_totals.values())
        # Concentration: what % of PnL comes from top city
        sorted_cities = sorted(city_totals.items(), key=lambda x: -x[1])
        top_city, top_pnl = sorted_cities[0]
        concentration = abs(top_pnl) / abs(total_pnl) if total_pnl != 0 else 0

        findings.append(f"PnL by city: {dict(sorted_cities[:5])}")
        if concentration > 0.50:
            findings.append(f"High concentration: {top_city} = {concentration:.0%} of PnL")
            recs.append(f"Diversify away from {top_city} — single-city dependency is risky")
            score -= 15

        # Side concentration
        yes_pnl = sum(t.get("pnl", 0) for t in yes_trades)
        no_pnl = sum(t.get("pnl", 0) for t in no_trades)
        if abs(yes_pnl) > 5 and yes_wr < 20:
            findings.append(f"YES side total PnL=${yes_pnl:.2f} — dragging performance")
            recs.append("Consider disabling YES entries or switching to NO-only strategy")
            score -= 10

        if no_pnl > 100:
            findings.append(f"NO side total PnL=${no_pnl:.2f} — primary profit driver")
            recs.append("Optimize NO-side entry criteria: lower edge threshold, increase position size on high-conviction NO")

    audit.dimensions.append(DimensionScore(
        name="Portfolio Diversification", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    # ─── 5. STRATEGY DECAY ───
    score = 75
    findings = []
    recs = []

    if len(settled) >= 10:
        # Split into first half and second half
        mid = len(settled) // 2
        first_half = settled[:mid]
        second_half = settled[mid:]

        first_pnl = sum(t.get("pnl", 0) for t in first_half)
        second_pnl = sum(t.get("pnl", 0) for t in second_half)

        first_wr = sum(1 for t in first_half if t.get("pnl", 0) > 0) / len(first_half) * 100 if first_half else 0
        second_wr = sum(1 for t in second_half if t.get("pnl", 0) > 0) / len(second_half) * 100 if second_half else 0

        findings.append(f"First half: WR={first_wr:.1f}% PnL=${first_pnl:.2f} ({len(first_half)} trades)")
        findings.append(f"Second half: WR={second_wr:.1f}% PnL=${second_pnl:.2f} ({len(second_half)} trades)")

        if second_wr < first_wr - 10:
            findings.append("Strategy DECAYING — WR dropping over time")
            recs.append("Investigate regime change — market may have adapted to the edge")
            recs.append("Re-calibrate model on recent data; consider retiring stale signals")
            score -= 20
        elif second_wr > first_wr + 10:
            findings.append("Strategy IMPROVING — WR increasing over time")
            score += 10
        else:
            findings.append("Strategy STABLE — consistent performance")

        audit.performance_trajectory = (
            "decaying" if second_wr < first_wr - 10 else
            "improving" if second_wr > first_wr + 10 else
            "stable"
        )

    audit.dimensions.append(DimensionScore(
        name="Strategy Decay", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    # ─── 6. SETTLEMENT/EV ANALYSIS ───
    score = 85
    findings = []
    recs = []

    if settled:
        pnls = [t.get("pnl", 0) for t in settled]
        total_pnl = sum(pnls)
        ev = total_pnl / len(settled)

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        avg_win = statistics.mean(wins) if wins else 0
        avg_loss = statistics.mean(losses) if losses else 0

        pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float('inf')

        findings.append(f"EV/trade=${ev:.2f} | PF={pf:.1f} | avgWin=${avg_win:.2f} | avgLoss=${avg_loss:.2f}")

        if ev > 1.0:
            findings.append("Strong positive EV — strategy is profitable per trade")
            score += 5
        elif ev < 0:
            findings.append("NEGATIVE EV — strategy loses money per trade")
            recs.append("URGENT: Review entry criteria, model calibration, and market selection")
            score -= 30

        if pf > 2.0:
            findings.append(f"PF={pf:.1f} — excellent profit factor")
            score += 5
        elif pf < 1.0:
            findings.append(f"PF={pf:.1f} — below 1.0, strategy is unprofitable")
            score -= 20

        # Win/loss asymmetry
        if avg_win > 0 and avg_loss < 0:
            ratio = avg_win / abs(avg_loss)
            findings.append(f"Win/loss ratio={ratio:.1f}x — {'favorable' if ratio > 2 else 'unfavorable' if ratio < 1 else 'neutral'}")
            if ratio > 5:
                recs.append(f"Excellent asymmetry ({ratio:.1f}x) — can afford low WR. Focus on maximizing win size.")
            elif ratio < 1:
                recs.append(f"Poor asymmetry ({ratio:.1f}x) — wins smaller than losses. Need higher WR or larger wins.")

    audit.dimensions.append(DimensionScore(
        name="Settlement & EV", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    return audit

# ═══════════════════════════════════════════════════════════════
# CANARY AUDIT
# ═══════════════════════════════════════════════════════════════

def audit_canary() -> StrategyAudit:
    trades = load_canary_trades()
    orders = load_canary_orders()
    heartbeat = load_canary_heartbeat()

    audit = StrategyAudit(
        bot_name="V21.7.62 Reversal Scalper Canary",
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_trades=len(trades),
    )

    # ─── NO DATA GUARD ───
    # If no trades and no orders, return explicit NO DATA result.
    # Do NOT score dimensions with base scores — that fabricates performance.
    if not trades and not orders:
        audit.overall_score = 0.0
        audit.overall_grade = "N/A"
        audit.performance_trajectory = "no_data"
        audit.dimensions.append(DimensionScore(
            name="No Data",
            score=0, grade="N/A",
            findings=[
                f"Zero trades executed. Zero orders submitted.",
                f"Canary has never fired — preflight gate has not passed.",
            ],
            recommendations=[
                "Canary preflight must pass (PM_WS_BOOK feed required) before any trades can execute",
                "WS feed repair (V21.7.16) must achieve sustained >30min connection lifetime",
                "Do NOT report any trade count, WR, or PnL for canary until actual orders exist",
            ],
        ))
        return audit

    # ─── UNVERIFIED SETTLEMENT CHECK ───
    # Trades may have order_status=ACKNOWLEDGED but settled=null and exit_price=null.
    # These are internally resolved by the bot, not verified against Polymarket settlement.
    # Flag them prominently — PnL numbers from unverified settlements are NOT confirmed.
    settled_verified = [t for t in trades if t.get("settled") is True or t.get("exit_price") is not None]
    settled_unverified = [t for t in trades if t.get("settled") is None or t.get("settled") is False]
    if settled_unverified and not settled_verified:
        unverified_pnl = sum(t.get("pnl", 0) for t in settled_unverified)
        audit.overall_score = 0.0
        audit.overall_grade = "N/A"
        audit.performance_trajectory = "unverified"
        audit.dimensions.append(DimensionScore(
            name="Unverified Settlements",
            score=0, grade="N/A",
            findings=[
                f"{len(settled_unverified)} trades with order_status=ACKNOWLEDGED but settled=null, exit_price=null",
                f"Bot assigned PnL=${unverified_pnl:.2f} internally but settlements NOT verified against Polymarket",
                f"All win/loss/PnL numbers are bot-internal estimates, NOT confirmed results",
                f"Cannot grade strategy until settlements are verified via Gamma API outcomePrices",
            ],
            recommendations=[
                "Implement settlement verification: query Gamma /markets?slug=<slug>&active=false for each expired position",
                "Do NOT report WR or PnL as confirmed until exit_price is populated from Polymarket resolution",
                "These orders were real CLOB submissions (ACKNOWLEDGED) but outcomes are unverified",
            ],
        ))
        return audit

    # ─── 1. ENTRY SIGNAL QUALITY ───
    score = 85
    findings = []
    recs = []

    if trades:
        # Edge distribution
        edges = [t.get("edge_pp", 0) for t in trades if t.get("edge_pp") is not None]
        if edges:
            avg_edge = statistics.mean(edges)
            findings.append(f"Avg edge at entry: {avg_edge:.1f}pp")
            if avg_edge < 5:
                recs.append("Low edge entries — increase min_edge_pp to improve signal quality")
                score -= 10

        # Confidence distribution
        confs = [t.get("confidence", 0) for t in trades if t.get("confidence") is not None]
        if confs:
            avg_conf = statistics.mean(confs)
            findings.append(f"Avg model confidence: {avg_conf:.1%}")

        # RSI at entry
        rsis = [t.get("rsi_at_entry", 0) for t in trades if t.get("rsi_at_entry") is not None]
        if rsis:
            avg_rsi = statistics.mean(rsis)
            findings.append(f"Avg RSI at entry: {avg_rsi:.1f}")
            if avg_rsi < 30:
                recs.append("Entries are in oversold territory — verify this isn't catching falling knives")
            elif avg_rsi > 70:
                recs.append("Entries are in overbought territory — verify reversal timing")

        # Signal-to-trade ratio (signals generated vs trades placed)
        total_signals = heartbeat.get("signals", 0)
        total_orders = heartbeat.get("orders", 0)
        if total_signals > 0:
            ratio = total_orders / total_signals * 100
            findings.append(f"Signal-to-trade ratio: {ratio:.2f}% ({total_orders}/{total_signals})")
            if ratio < 1:
                recs.append(f"Very selective entry ({ratio:.2f}%) — good for quality but may miss opportunities")
            elif ratio > 20:
                recs.append(f"Low selectivity ({ratio:.2f}%) — many signals converted to trades, may be overtrading")

    audit.dimensions.append(DimensionScore(
        name="Entry Signal Quality", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    # ─── 2. RISK MANAGEMENT ───
    score = 85
    findings = []
    recs = []

    # Position sizing
    sizes = [t.get("size_usd", 0) for t in trades if t.get("size_usd")]
    if sizes:
        avg_size = statistics.mean(sizes)
        findings.append(f"Avg position size: ${avg_size:.2f}")
        if avg_size > 10:
            recs.append("Position size >$10 — ensure it matches risk tolerance for live")
            score -= 5

    # Drawdown
    pnls = [t.get("pnl", 0) for t in trades]
    if pnls:
        peak = 0
        cumsum = 0
        max_dd = 0
        for p in pnls:
            cumsum += p
            peak = max(peak, cumsum)
            dd = peak - cumsum
            max_dd = max(max_dd, dd)
        findings.append(f"Max drawdown: ${max_dd:.2f}")
        if max_dd > 20:
            recs.append(f"Max DD ${max_dd:.2f} is high — consider tighter stop or smaller position size")
            score -= 10

    # Consecutive losses
    max_consec = 0
    current_consec = 0
    for p in pnls:
        if p <= 0:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0
    findings.append(f"Max consecutive losses: {max_consec}")
    if max_consec >= 5:
        recs.append("5+ consecutive losses — verify circuit breaker triggers correctly")
        score -= 10

    audit.dimensions.append(DimensionScore(
        name="Risk Management", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    # ─── 3. CALIBRATION ───
    score = 70
    findings = []
    recs = []

    if trades:
        # Use calibrated_prob (actual model output) not confidence (capped proxy)
        model_probs = [t.get("calibrated_prob", t.get("confidence", 0)) for t in trades
                       if t.get("calibrated_prob") is not None or t.get("confidence") is not None]
        actual_hits = [1 if t.get("pnl", 0) > 0 else 0 for t in trades
                      if t.get("calibrated_prob") is not None or t.get("confidence") is not None]

        if len(model_probs) > 10:
            avg_model = statistics.mean(model_probs)
            actual_rate = sum(actual_hits) / len(actual_hits)
            cal_error = abs(avg_model - actual_rate)

            # Check if calibrated_prob or confidence was used
            uses_calibrated = any(t.get("calibrated_prob") is not None for t in trades)
            field_name = "calibrated_prob" if uses_calibrated else "confidence"

            findings.append(f"Model {field_name} avg={avg_model:.1%} vs actual WR={actual_rate:.1%}")
            findings.append(f"Calibration error={cal_error:.1%}")

            if cal_error > 0.15:
                recs.append(f"Model is overconfident — apply isotonic regression or Platt scaling")
                score -= 15
            elif cal_error < 0.05:
                findings.append("Excellent calibration")
                score += 15

            # Brier score
            brier = sum((mp - hit) ** 2 for mp, hit in zip(model_probs, actual_hits)) / len(model_probs)
            findings.append(f"Brier score={brier:.3f}")
        else:
            findings.append("Insufficient data for calibration analysis")

    audit.dimensions.append(DimensionScore(
        name="Calibration", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    # ─── 4. PORTFOLIO DIVERSIFICATION ───
    score = 60
    findings = []
    recs = []

    if trades:
        asset_pnl = defaultdict(list)
        for t in trades:
            asset_pnl[t.get("asset", "?")].append(t.get("pnl", 0))

        for asset, pnls in sorted(asset_pnl.items()):
            wins = sum(1 for p in pnls if p > 0)
            total = sum(pnls)
            wr = wins / len(pnls) * 100 if pnls else 0
            findings.append(f"  {asset}: {len(pnls)} trades, WR={wr:.1f}%, PnL=${total:.2f}")

        # Concentration check
        total_trades = len(trades)
        top_asset = max(asset_pnl.items(), key=lambda x: len(x[1]))
        concentration = len(top_asset[1]) / total_trades * 100
        findings.append(f"Top asset concentration: {top_asset[0]} = {concentration:.0f}% of trades")

        if concentration > 80:
            recs.append(f"Extreme concentration on {top_asset[0]} ({concentration:.0f}%) — single-asset risk")
            recs.append("Expand to other assets (ETH, SOL, XRP) — but verify edge exists first via paper trading")
            score -= 20
        elif concentration > 60:
            findings.append(f"Moderate concentration on {top_asset[0]} ({concentration:.0f}%) — per-asset daily cap active")
            score -= 10

        # Side concentration
        side_pnl = defaultdict(list)
        for t in trades:
            side_pnl[t.get("side", "?")].append(t.get("pnl", 0))
        for side, pnls in sorted(side_pnl.items()):
            wins = sum(1 for p in pnls if p > 0)
            total = sum(pnls)
            wr = wins / len(pnls) * 100 if pnls else 0
            findings.append(f"  {side}: {len(pnls)} trades, WR={wr:.1f}%, PnL=${total:.2f}")

    audit.dimensions.append(DimensionScore(
        name="Portfolio Diversification", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    # ─── 5. STRATEGY DECAY ───
    score = 80
    findings = []
    recs = []

    if len(trades) >= 20:
        mid = len(trades) // 2
        first_half = trades[:mid]
        second_half = trades[mid:]

        first_wr = sum(1 for t in first_half if t.get("pnl", 0) > 0) / len(first_half) * 100 if first_half else 0
        second_wr = sum(1 for t in second_half if t.get("pnl", 0) > 0) / len(second_half) * 100 if second_half else 0
        first_pnl = sum(t.get("pnl", 0) for t in first_half)
        second_pnl = sum(t.get("pnl", 0) for t in second_half)

        findings.append(f"First half: WR={first_wr:.1f}% PnL=${first_pnl:.2f}")
        findings.append(f"Second half: WR={second_wr:.1f}% PnL=${second_pnl:.2f}")

        if second_wr < first_wr - 10:
            findings.append("⚠️ Strategy DECAYING")
            recs.append("Re-evaluate entry criteria — edge may be eroding")
            score -= 20
            audit.performance_trajectory = "decaying"
        elif second_wr > first_wr + 10:
            findings.append("✅ Strategy IMPROVING")
            score += 10
            audit.performance_trajectory = "improving"
        else:
            findings.append("Strategy STABLE")
            audit.performance_trajectory = "stable"

    audit.dimensions.append(DimensionScore(
        name="Strategy Decay", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    # ─── 6. EXECUTION QUALITY ───
    score = 75
    findings = []
    recs = []

    if orders:
        filled = [o for o in orders if o.get("status") == "PAPER_FILLED"]
        rejected = [o for o in orders if o.get("status") != "PAPER_FILLED"]
        fill_rate = len(filled) / len(orders) * 100 if orders else 0

        findings.append(f"Fill rate: {fill_rate:.1f}% ({len(filled)}/{len(orders)})")
        if fill_rate < 50:
            recs.append("Low fill rate — check spread filter and order timing")
            score -= 10

        # Entry price distribution
        entry_prices = [o.get("ask", 0) for o in orders if o.get("ask")]
        if entry_prices:
            findings.append(f"Entry price range: {min(entry_prices):.2f} - {max(entry_prices):.2f}")
            findings.append(f"Avg entry: {statistics.mean(entry_prices):.2f}")
            # All in one bucket?
            buckets = Counter()
            for ep in entry_prices:
                if ep < 0.10: buckets["<10¢"] += 1
                elif ep < 0.30: buckets["10-30¢"] += 1
                elif ep < 0.50: buckets["30-50¢"] += 1
                elif ep < 0.70: buckets["50-70¢"] += 1
                else: buckets["70¢+"] += 1
            findings.append(f"Entry price buckets: {dict(buckets)}")

    audit.dimensions.append(DimensionScore(
        name="Execution Quality", score=score, grade=grade_from_score(score),
        findings=findings, recommendations=recs
    ))

    return audit

# ═══════════════════════════════════════════════════════════════
# OPTIMIZATION RECOMMENDATIONS ENGINE
# ═══════════════════════════════════════════════════════════════

def generate_optimization_plan(audit: StrategyAudit) -> List[Dict]:
    """Generate ranked optimization recommendations."""
    recs = []

    for dim in audit.dimensions:
        for rec in dim.recommendations:
            # Estimate impact based on dimension score
            impact = "HIGH" if dim.score < 60 else "MEDIUM" if dim.score < 80 else "LOW"
            recs.append({
                "dimension": dim.name,
                "recommendation": rec,
                "impact": impact,
                "dimension_score": dim.score,
                "dimension_grade": dim.grade,
            })

    # Sort by impact (HIGH first)
    impact_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    recs.sort(key=lambda x: (impact_order.get(x["impact"], 3), x["dimension_score"]))

    return recs

# ═══════════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════════

def print_audit(audit: StrategyAudit):
    print("╔══════════════════════════════════════════════════════════════╗")
    print(f"║  Strategy Audit: {audit.bot_name:<42s}  ║")
    print(f"║  {audit.timestamp[:19]:<55s}  ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    # Overall
    scores = [d.score for d in audit.dimensions]
    audit.overall_score = statistics.mean(scores) if scores else 0
    audit.overall_grade = grade_from_score(audit.overall_score)

    print(f"Overall Score: {audit.overall_score:.1f}/100 ({audit.overall_grade})")
    print(f"Total Trades Analyzed: {audit.total_trades}")
    print(f"Performance Trajectory: {audit.performance_trajectory.upper()}")
    print()

    # Dimensions
    print("═══ DIMENSION SCORES ═══")
    for dim in audit.dimensions:
        icon = {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴", "F": "⚫"}.get(dim.grade, "⚪")
        print(f"\n  {icon} {dim.name}: {dim.score:.0f}/100 ({dim.grade})")
        for f in dim.findings:
            print(f"    • {f}")
        for r in dim.recommendations:
            print(f"    → {r}")
    print()

    # Optimization plan
    print("═══ OPTIMIZATION PLAN (ranked by impact) ═══")
    recs = generate_optimization_plan(audit)
    if recs:
        for i, rec in enumerate(recs, 1):
            impact_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(rec["impact"], "⚪")
            print(f"  {i}. {impact_icon} [{rec['impact']}] {rec['recommendation']}")
            print(f"     Dimension: {rec['dimension']} ({rec['dimension_grade']})")
    else:
        print("  No recommendations — strategy is performing well across all dimensions")
    print()

    # Methodology suggestions
    print("═══ SUGGESTED OPTIMIZATION METHODOLOGIES ═══")
    methodologies = [
        ("Walk-Forward Optimization", "Re-optimize parameters on rolling 7-day windows, test on next day. Prevents overfitting to historical data."),
        ("Bayesian Hyperparameter Tuning", "Use Optuna or Hyperopt to find optimal RSI thresholds, edge minimums, position sizes. 100 trials with 5-fold time-series CV."),
        ("Regime Detection", "Cluster market conditions (trending/mean-reverting/volatile). Use HMM to detect regime and switch strategy parameters accordingly."),
        ("Ensemble Signal Fusion", "Combine multiple model predictions with weighted voting. Weight by recent per-model performance."),
        ("Progressive Position Sizing", "Kelly criterion with fractional Kelly (1/4) — increase size on winning streaks, decrease on losing. Cap at 2x base size."),
        ("Adversarial Validation", "Train on first 70% of data, test on last 30%. If test performance drops >20%, model is overfit."),
        ("Feature Importance Analysis", "SHAP values on neural network to identify which features drive predictions. Prune noise features."),
        ("Market Microstructure Edge", "Track order book imbalance, trade flow, and spread dynamics as additional entry signals."),
        ("Time-of-Day Patterns", "Analyze if certain hours have higher WR or EV. Restrict trading to high-EV windows."),
        ("Correlation Hedging", "If entering BTC UP and ETH DOWN simultaneously, check if they're correlated and adjust position size."),
    ]
    for name, desc in methodologies:
        print(f"  • {name}: {desc}")
    print()

def save_audit(audit: StrategyAudit):
    report = {
        "timestamp": audit.timestamp,
        "bot_name": audit.bot_name,
        "total_trades": audit.total_trades,
        "overall_score": audit.overall_score,
        "overall_grade": audit.overall_grade,
        "performance_trajectory": audit.performance_trajectory,
        "dimensions": [asdict(d) for d in audit.dimensions],
        "optimization_plan": generate_optimization_plan(audit),
    }
    # Save with timestamp
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    (OUT / f"audit_{audit.bot_name.replace(' ', '_').replace('.', '_')}_{ts}.json").write_text(
        json.dumps(report, indent=2, default=str)
    )
    # Also save latest
    (OUT / f"latest_{audit.bot_name.replace(' ', '_').replace('.', '_').lower()}.json").write_text(
        json.dumps(report, indent=2, default=str)
    )

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="FDC Strategy Observer")
    parser.add_argument("--monitor", action="store_true", help="Continuous monitoring (30min interval)")
    parser.add_argument("--bot", type=str, choices=["weather", "canary", "all"], default="all")
    args = parser.parse_args()

    def run_audit():
        if args.bot in ("weather", "all"):
            print("\n" + "═" * 60)
            print("  WEATHER BOT AUDIT")
            print("═" * 60)
            audit = audit_weather_bot()
            print_audit(audit)
            save_audit(audit)

        if args.bot in ("canary", "all"):
            print("\n" + "═" * 60)
            print("  CANARY SCALPER AUDIT")
            print("═" * 60)
            audit = audit_canary()
            print_audit(audit)
            save_audit(audit)

    if args.monitor:
        print("Starting continuous monitoring (30min interval)...")
        while True:
            run_audit()
            print(f"\n--- Next audit in 30min ---")
            time.sleep(1800)
    else:
        run_audit()

if __name__ == "__main__":
    main()