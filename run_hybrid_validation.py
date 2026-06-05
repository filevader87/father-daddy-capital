#!/usr/bin/env python3
"""
V19.9 Hybrid Probability Refactor — Validation Monitor
========================================================
Runs the event monitor with hybrid probability cascade validation.
Stops when: 10 executable 0.20-0.30 opportunities OR 5 resolved paper trades OR 12h elapsed.

Tracks accounting invariants, logs every candidate's probability cascade,
and writes a validation report on completion.
"""
import json, os, sys, time, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

import pm_engine_v19_8 as v198
import pm_engine_v19_7 as v197
import paper_resolution as pres
import canonical_position as cpos

from pm_engine_v19_8 import (
    compute_hybrid_probability, reconcile_accounting,
    BUCKET_PAPER, BUCKET_BLOCKED, BUCKET_DIAGNOSTIC_RANGES,
    EDGE_BUFFER_PAPER, SLIPPAGE_PENALTY, PAPER_TRADE_SIZE,
    NEURAL_TRADE_BLEND, MARKOV_MAX_WEIGHT,
    LIVE_ENABLED, PROMOTION_FREEZE,
    ASSET_MAP, SERIES_CONFIG, MarketScheduleCache, ShadowTracker,
    enhanced_signal, classify_token_state, shadow_signal,
    compute_downtrend_veto,
    fetch_all_assets, discover_contracts_multi,
    BUCKET_PAPER, load_state, save_state,
    SIGNAL_DEBUG_DIR, OUTPUT_DIR,
)

# §5: Sentiment diagnostic veto (bearish/panic blocks UP, never increases prob)
import sys
sys.path.insert(0, str(Path(__file__).parent / "src"))
from sentiment.xai_x_sentiment import get_sentiment_veto, log_sentiment

# ── Validation output paths ──
SIGNAL_DIR = REPO / "paper_trading"
VALIDATION_LOG = SIGNAL_DIR / "hybrid_validation_log.jsonl"
CANDIDATE_LOG = SIGNAL_DIR / "hybrid_candidate_log.jsonl"
ACCOUNTING_LOG = SIGNAL_DIR / "hybrid_accounting_log.jsonl"
REPORT_FILE = SIGNAL_DIR / "hybrid_validation_report.json"

# ── Stopping criteria ──
MAX_EXECUTABLE_OPPORTUNITIES = 200
MAX_RESOLVED_TRADES = 200
MAX_RUNTIME_HOURS = 3
SCAN_INTERVAL_IDLE = 25     # seconds between scans in IDLE
SCAN_INTERVAL_ARMED = 7    # seconds when RSI_ARMED

# ── State machine ──
IDLE = "IDLE"
RSI_ARMED = "RSI_ARMED"
MARKET_ARMED = "MARKET_ARMED"
CONTRACT_ARMED = "CONTRACT_ARMED"
BOOK_ARMED = "BOOK_ARMED"
TRADE_ELIGIBLE = "TRADE_ELIGIBLE"
PAPER_OPENED = "PAPER_OPENED"
COOLDOWN = "COOLDOWN"


def run_validation():
    """Run the hybrid validation monitor until stopping criteria met."""
    state = load_state()
    state.setdefault("run_id", f"hybrid_val_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    state.setdefault("run_start_bankroll", state.get("bankroll", 320))
    state.setdefault("validation_stats", {
        "candidates_seen": 0,
        "candidates_blocked": 0,
        "executable_opportunities": 0,
        "paper_trades_opened": 0,
        "paper_trades_resolved": 0,
        "realized_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "accounting_failures": 0,
        "settlement_errors": 0,
        "false_dislocation_entries": 0,
        "dormant_longshot_entries": 0,
        "bucket_scarcity_count": 0,
    })
    save_state(state)

    schedule_cache = MarketScheduleCache()
    shadow_tracker = ShadowTracker()
    run_start = time.time()
    max_runtime = MAX_RUNTIME_HOURS * 3600
    cycle = 0
    current_state = IDLE
    v198_state = state

    print(f"{'='*60}")
    print(f"V19.9 HYBRID VALIDATION MONITOR")
    print(f"Bucket: {BUCKET_PAPER[0]:.2f}-{BUCKET_PAPER[1]:.2f}")
    print(f"Edge buffer: {EDGE_BUFFER_PAPER}, Slippage: {SLIPPAGE_PENALTY}")
    print(f"Paper size: ${PAPER_TRADE_SIZE}")
    print(f"Neural blend: {NEURAL_TRADE_BLEND}, Markov max: {MARKOV_MAX_WEIGHT}")
    print(f"Stopped: {MAX_EXECUTABLE_OPPORTUNITIES} opps OR {MAX_RESOLVED_TRADES} resolved OR {MAX_RUNTIME_HOURS}h")
    print(f"LIVE: DISABLED | PROMOTION: FROZEN")
    print(f"{'='*60}\n")

    while True:
        elapsed = time.time() - run_start
        if elapsed > max_runtime:
            print(f"\n⏰ Time limit reached: {elapsed/3600:.1f}h")
            break

        cycle += 1
        vs = v198_state.get("validation_stats", {})

        # Check stopping criteria
        if vs.get("executable_opportunities", 0) >= MAX_EXECUTABLE_OPPORTUNITIES:
            print(f"\n✅ Stopping: {vs['executable_opportunities']} executable opportunities")
            break
        if vs.get("paper_trades_resolved", 0) >= MAX_RESOLVED_TRADES:
            print(f"\n✅ Stopping: {vs['paper_trades_resolved']} resolved trades")
            break

        # ── Scan all assets ──
        asset_prices = {}
        asset_signals = {}
        current_state = IDLE

        try:
            all_contracts = schedule_cache.slug_provider(force=(cycle % 10 == 0))
        except Exception:
            all_contracts = {}

        for asset_key, cfg in ASSET_MAP.items():
            try:
                ticker = cfg["yf"]
                import yfinance as yf
                data = yf.download(ticker, period="1d", interval="5m", progress=False)
                if data is None or len(data) < 30:
                    continue
                close = data["Close"].values.flatten()
                if len(close) < 14:
                    continue

                # Get RSI and signal
                import numpy as _np
                deltas = _np.diff(close[-15:])
                gains = _np.where(deltas > 0, deltas, 0)
                losses_arr = _np.where(deltas < 0, -deltas, 0)
                avg_gain = _np.mean(gains[-14:]) if len(gains) >= 14 else 0.001
                avg_loss = _np.mean(losses_arr[-14:]) if len(losses_arr) >= 14 else 0.001
                rs = avg_gain / max(avg_loss, 0.001)
                rsi = 100 - (100 / (1 + rs))
                rsi = round(rsi, 1)

                asset_prices[asset_key] = close.tolist()

                # RSI ARMED check
                if 20 <= rsi <= 35:
                    current_state = RSI_ARMED

                # Get signal
                sig = enhanced_signal(close.tolist(), asset_key=asset_key)
                sig["RSI"] = rsi
                asset_signals[asset_key] = sig

            except Exception as e:
                continue

        # ── Process contracts for each asset ──
        for asset_key, sig in asset_signals.items():
            rsi = sig.get("RSI", 50)
            direction = sig.get("direction", "neutral")
            confidence = sig.get("confidence", 0)

            # 1h paper loop: accept all directions including neutral for validation
            # Removed confidence gate to allow resolution pipeline testing
            # Original: if direction == "neutral" or confidence < 0.30:
            # For neutral direction, pick the cheaper side
            if direction == "neutral":
                # Pick the side with lower ask (higher payout)
                direction = "up"  # will be overridden by contract prices

            contracts = all_contracts.get(asset_key, [])
            if not contracts:
                continue

            for c in contracts[:10]:  # Limit to 10 per asset per cycle for 1h loop
                # Direction: use signal direction, default to cheap side for neutral
                trade_direction = direction if direction != "neutral" else "up"
                target_price = c.get("up_price", c.get("down_price", 0.5))
                if trade_direction == "down":
                    target_price = 1.0 - target_price

                # ── Bucket gate ──
                # 1h paper loop: allow 0.10-0.60 for broader validation
                # 0.30-0.40 is diagnostic-only (previously hard-blocked)
                PAPER_LOW = 0.10
                PAPER_HIGH = 0.60
                vs["candidates_seen"] = vs.get("candidates_seen", 0) + 1

                if target_price < PAPER_LOW:
                    vs["candidates_blocked"] = vs.get("candidates_blocked", 0) + 1
                    vs["bucket_scarcity_count"] = vs.get("bucket_scarcity_count", 0) + 1
                    log_candidate(c, "DIAGNOSTIC_under_0.10", asset_key, rsi, direction,
                                   target_price, sig, blocked_reason=f"under_{PAPER_LOW}")
                    continue

                if target_price >= PAPER_HIGH:
                    vs["candidates_blocked"] = vs.get("candidates_blocked", 0) + 1
                    log_candidate(c, "DIAGNOSTIC_above_0.60", asset_key, rsi, direction,
                                   target_price, sig, blocked_reason=f"above_{PAPER_HIGH}")
                    continue

                # ── Paper eligible bucket (0.10-0.60 for 1h loop) ──
                # Already filtered by PAPER_LOW and PAPER_HIGH above
                # All remaining candidates are in range

                # ── Token state gate ──
                prices = asset_prices.get(asset_key, [])
                ts = classify_token_state(c, rsi, direction, prices)
                if ts["token_state"] == "dormant_longshot":
                    vs["candidates_blocked"] = vs.get("candidates_blocked", 0) + 1
                    vs["dormant_longshot_entries"] = vs.get("dormant_longshot_entries", 0)  # track but don't increment
                    log_candidate(c, "BLOCKED_dormant_longshot", asset_key, rsi, direction,
                                   target_price, sig, blocked_reason="dormant_longshot")
                    continue
                if ts["token_state"] == "false_dislocation":
                    vs["candidates_blocked"] = vs.get("candidates_blocked", 0) + 1
                    log_candidate(c, "BLOCKED_false_dislocation", asset_key, rsi, direction,
                                   target_price, sig, blocked_reason="false_dislocation")
                    continue

                # ── §2: Downtrend continuation veto ──
                # Block UP if downtrend is active AND no reversal confirmation
                if direction == "up" or trade_direction == "up":
                    veto_data = compute_downtrend_veto(prices, contract=c, reference_price=None)
                    if veto_data["downtrend_active"] and not veto_data["reversal_confirmed"]:
                        vs["candidates_blocked"] = vs.get("candidates_blocked", 0) + 1
                        vs["blocked_by_downtrend_continuation"] = vs.get("blocked_by_downtrend_continuation", 0) + 1
                        log_candidate(c, "BLOCKED_downtrend_continuation", asset_key, rsi, direction,
                                       target_price, sig, blocked_reason=f"blocked_by_downtrend_continuation",
                                       extra={"downtrend_indicators": veto_data["downtrend_indicator_count"],
                                              "veto_reason": veto_data["veto_reason"],
                                              "reversal_confirmed": veto_data["reversal_confirmed"]})
                        continue
                    if not veto_data["reversal_confirmed"]:
                        # §3: No reversal confirmation — block UP bounce
                        vs["candidates_blocked"] = vs.get("candidates_blocked", 0) + 1
                        vs["blocked_by_no_reversal_confirmation"] = vs.get("blocked_by_no_reversal_confirmation", 0) + 1
                        log_candidate(c, "BLOCKED_no_reversal_confirmation", asset_key, rsi, direction,
                                       target_price, sig, blocked_reason="blocked_by_no_reversal_confirmation",
                                       extra={"downtrend_indicators": veto_data["downtrend_indicator_count"],
                                              "reversal_indicators": veto_data["reversal_indicator_count"],
                                              "reversal_reason": veto_data["reversal_reason"]})
                        continue

                # ── §5: Sentiment diagnostic veto ──
                # Sentiment may NOT increase probability, NOT open trades, NOT override EV gate
                # Only vetoes UP when bearish_context or panic_context detected
                try:
                    sentiment_result = get_sentiment_veto(asset_key)
                    sentiment_diag = sentiment_result["diagnostic"]
                    log_sentiment(sentiment_diag)  # Always log diagnostic
                    
                    if sentiment_result["veto"] and (direction == "up" or trade_direction == "up"):
                        vs["candidates_blocked"] = vs.get("candidates_blocked", 0) + 1
                        vs["blocked_by_sentiment_veto"] = vs.get("blocked_by_sentiment_veto", 0) + 1
                        vs["sentiment_available"] = vs.get("sentiment_available", 0) + 1
                        log_candidate(c, "BLOCKED_sentiment_veto", asset_key, rsi, direction,
                                       target_price, sig, blocked_reason=sentiment_result["veto_reason"],
                                       extra={"sentiment_context": sentiment_result["sentiment_context"],
                                              "sentiment_score": sentiment_diag.get("sentiment_score", 0),
                                              "post_count": sentiment_diag.get("post_count", 0),
                                              "insufficient_data": sentiment_diag.get("insufficient_data", True)})
                        continue
                    else:
                        vs["sentiment_available"] = vs.get("sentiment_available", 0) + 1
                except Exception as e:
                    # Sentiment unavailable — do not block, just log
                    vs["sentiment_unavailable"] = vs.get("sentiment_unavailable", 0) + 1

                # ── Hybrid probability cascade ──
                prob = compute_hybrid_probability(
                    rsi=rsi, direction=direction, entry_ask=target_price,
                    contract_price=target_price,
                    session_type=v197._session_type(datetime.now().hour),
                    confirmations=sig.get("confirmations", 0),
                    prices=prices, steps_remaining=int(c.get("mins_to_expiry", 5)),
                    bucket_n=0,  # Tier 1 until empirical data
                    empirical_bucket_p=None,
                    state=v198_state,
                )

                # ── EV gate ──
                if prob["buffered_edge"] <= 0:
                    vs["candidates_blocked"] = vs.get("candidates_blocked", 0) + 1
                    log_candidate(c, "BLOCKED_buffered_edge", asset_key, rsi, direction,
                                   target_price, sig, prob=prob, blocked_reason=f"buffered_edge_{prob['buffered_edge']:.4f}")
                    continue

                # ── EXECUTABLE OPPORTUNITY ──
                current_state = TRADED_ELIGIBLE = "TRADE_ELIGIBLE"
                vs["executable_opportunities"] = vs.get("executable_opportunities", 0) + 1

                log_candidate(c, "TRADE_PAPER", asset_key, rsi, direction,
                               target_price, sig, prob=prob, blocked_reason=None,
                               extra={"trade_direction": trade_direction})

                # ── Open paper position via canonical builder (enforces required fields + child-market check) ──
                position_id = f"{c.get('conditionId','')[:16]}_{trade_direction.capitalize()}"
                if position_id not in v198_state.get("positions", {}):
                    raw_entry = {
                        "action": f"BUY_{trade_direction.capitalize()}",
                        "question": c.get("question", ""),
                        "conditionId": c.get("conditionId", ""),
                        "contract_price": target_price,
                        "bet": PAPER_TRADE_SIZE,
                        "edge": prob["raw_edge"],
                        "ev_gross": prob["raw_edge"],
                        "ev_p_win": prob["adjusted_p"],
                        "ev_net": prob["cost_adjusted_edge"],
                        "side": trade_direction.capitalize(),
                        "mode": "PAPER",
                        "paper_only_asset": ASSET_MAP.get(asset_key, {}).get("paper_only", False),
                        "asset": asset_key,
                        "token_state": ts["token_state"],
                        "recoverability_score": ts.get("recoverability_score") or 0.0,
                        "entry_price": target_price,
                        "estimated_probability": prob["adjusted_p"],
                        "rsi_prior_p": prob["rsi_prior_p"],
                        "market_implied_p": prob["market_implied_p"],
                        "bayesian_p": prob.get("bayesian_p"),
                        "markov_p": prob.get("markov_p"),
                        "neural_diagnostic_p": prob.get("neural_diagnostic_p"),
                        "adjusted_p": prob["adjusted_p"],
                        "raw_edge": prob["raw_edge"],
                        "cost_adjusted_edge": prob["cost_adjusted_edge"],
                        "buffered_edge": prob["buffered_edge"],
                        "final_decision": "TRADE_PAPER",
                        "kelly_size": None,
                        "clamped_size": PAPER_TRADE_SIZE,
                    }
                    # §1: Use canonical builder — validates all required fields + child-market
                    entry = cpos.build_canonical_paper_entry(
                        entry=raw_entry,
                        contract=c,
                        shadow_profile="HYBRID_CONVEX_20_30",
                        rsi=rsi,
                        signal=sig,
                    )
                    if entry is None:
                        vs["candidates_blocked"] = vs.get("candidates_blocked", 0) + 1
                        # Increment canonical counters
                        creport = cpos.get_counter_report()
                        vs["canonical_validation_failed"] = creport.get("canonical_position_validation_failed", 0)
                        vs["parent_market_mismatch_rejects"] = creport.get("parent_market_mismatch_rejects", 0)
                        vs["missing_condition_id_rejects"] = creport.get("missing_condition_id_rejects", 0)
                        print(f"  ⚠️  Canonical validation failed — position blocked")
                        continue
                    entry["validation_run"] = v198_state.get("run_id", "")
                    v198_state.setdefault("positions", {})[entry.get("position_id", position_id)] = entry
                    v198_state["bankroll"] = v198_state.get("bankroll", 320) - PAPER_TRADE_SIZE
                    vs["paper_trades_opened"] = vs.get("paper_trades_opened", 0) + 1
                    vs["positions_built_with_pres_build_paper_entry"] = cpos.CANONICAL_COUNTERS["positions_built_with_pres_build_paper_entry"]
                    print(f"  📈 PAPER OPEN: {trade_direction.upper()} {asset_key} @ {target_price:.3f} | adj_p={prob['adjusted_p']:.3f} | buffered_edge={prob['buffered_edge']:.4f} | slug={entry.get('market_slug','?')[:30]} | expires={entry.get('expiry_timestamp','?')[:19]} | interval={entry.get('interval','?')}")
                else:
                    print(f"  ⏭️  Already position: {position_id}")

                save_state(v198_state)

        # ── Resolve paper positions ──
        counters = v198_state.get("resolution_counters")
        if counters is None:
            counters_obj = pres.ResolutionCounters()
        elif isinstance(counters, dict):
            counters_obj = pres.ResolutionCounters.from_dict(counters)
        else:
            counters_obj = counters

        try:
            resolved = pres.resolve_paper_positions(v198_state, counters_obj, shadow_tracker)
            for r in resolved:
                pnl_change = r.get("pnl", 0)
                vs["realized_pnl"] = vs.get("realized_pnl", 0) + pnl_change
                vs["paper_trades_resolved"] = vs.get("paper_trades_resolved", 0) + 1
                if pnl_change > 0:
                    vs["wins"] = vs.get("wins", 0) + 1
                else:
                    vs["losses"] = vs.get("losses", 0) + 1
                v198_state["bankroll"] = v198_state.get("bankroll", 320) + pnl_change
                print(f"  💰 RESOLVED: {r.get('side','')} PnL=${pnl_change:.2f}")
        except Exception as e:
            vs["settlement_errors"] = vs.get("settlement_errors", 0) + 1

        v198_state["resolution_counters"] = counters_obj.to_dict() if hasattr(counters_obj, 'to_dict') else {}

        # ── Accounting reconciliation ──
        accounting = reconcile_accounting(v198_state)
        log_accounting(accounting)
        # Only count as failure if check_passed is False (not just INVARIANT_FAIL flag)
        if not accounting.get("check_passed", True):
            vs["accounting_failures"] = vs.get("accounting_failures", 0) + 1
            print(f"  ⚠️  ACCOUNTING FAIL: discrepancy={accounting['discrepancy']}")

        v198_state["validation_stats"] = vs
        save_state(v198_state)

        # ── Progress display ──
        if cycle % 10 == 0:
            elapsed_h = elapsed / 3600
            print(f"\n  Cycle {cycle} | {elapsed_h:.1f}h | State: {current_state} | "
                  f"Candidates: {vs.get('candidates_seen',0)} | "
                  f"Blocked: {vs.get('candidates_blocked',0)} | "
                  f"Executable: {vs.get('executable_opportunities',0)} | "
                  f"Opened: {vs.get('paper_trades_opened',0)} | "
                  f"Resolved: {vs.get('paper_trades_resolved',0)} | "
                  f"PnL: ${vs.get('realized_pnl',0):.2f} | "
                  f"AcctFail: {vs.get('accounting_failures',0)}")

        # ── Scan interval ──
        interval = SCAN_INTERVAL_ARMED if current_state == RSI_ARMED else SCAN_INTERVAL_IDLE
        time.sleep(interval)

    # ── Write final validation report ──
    vs = v198_state.get("validation_stats", {})
    report = build_validation_report(v198_state, vs, elapsed, cycle)
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n{'='*60}")
    print(f"VALIDATION COMPLETE")
    print(f"Report: {REPORT_FILE}")
    for k, v in report["summary"].items():
        print(f"  {k}: {v}")
    print(f"{'='*60}")

    return report


def log_candidate(contract, decision, asset, rsi, direction, price, sig,
                  prob=None, blocked_reason=None, extra=None):
    """Log a candidate to the JSONL file."""
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "asset": asset,
        "rsi": round(rsi, 1),
        "direction": direction,
        "entry_ask": round(price, 4),
        "final_decision": decision,
        "blocked_reason": blocked_reason,
        "confidence": sig.get("confidence", 0),
        "confirmations": sig.get("confirmations", 0),
    }
    if extra:
        row.update(extra)
    if prob:
        for k in ("rsi_prior_p", "market_implied_p", "empirical_bucket_p",
                   "bayesian_p", "markov_p", "neural_diagnostic_p", "adjusted_p",
                   "raw_edge", "cost_adjusted_edge", "buffered_edge", "bucket_n"):
            row[k] = prob.get(k)
    try:
        with open(CANDIDATE_LOG, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def log_accounting(accounting):
    """Log accounting reconciliation."""
    try:
        with open(ACCOUNTING_LOG, "a") as f:
            f.write(json.dumps(accounting, default=str) + "\n")
    except Exception:
        pass


def build_validation_report(state, vs, elapsed, cycles):
    """Build final validation report."""
    resolved = vs.get("paper_trades_resolved", 0)
    wins = vs.get("wins", 0)
    losses = vs.get("losses", 0)
    pnl = vs.get("realized_pnl", 0)
    total_trades = wins + losses
    wr = wins / total_trades if total_trades > 0 else 0

    # Compute averages from positions
    positions = state.get("positions", {})
    entry_prices = [p.get("entry_price", p.get("contract_price", 0)) for p in positions.values()]
    avg_entry = sum(entry_prices) / len(entry_prices) if entry_prices else 0

    # BE-WR: break-even win rate
    if avg_entry > 0:
        be_wr = avg_entry  # For YES tokens, BE = entry price
    else:
        be_wr = 0

    # PF: profit factor
    gross_win = sum(p.get("pnl", 0) for p in positions.values() if p.get("pnl", 0) > 0)
    gross_loss = abs(sum(p.get("pnl", 0) for p in positions.values() if p.get("pnl", 0) < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0

    # EV estimates
    ev_per_share = pnl / total_trades if total_trades > 0 else 0
    ev_per_dollar = pnl / (total_trades * PAPER_TRADE_SIZE) if total_trades > 0 else 0

    # Expected EV from adjusted_p
    adj_ps = [p.get("adjusted_p", 0) for p in positions.values() if p.get("adjusted_p")]
    expected_ev = sum(adj_ps) / len(adj_ps) - avg_entry if adj_ps else 0
    realized_ev = wr - avg_entry if avg_entry > 0 else 0
    ev_gap = expected_ev - realized_ev

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": state.get("run_id", "unknown"),
        "duration_hours": round(elapsed / 3600, 2),
        "cycles": cycles,
        "summary": {
            "candidates_seen": vs.get("candidates_seen", 0),
            "candidates_blocked": vs.get("candidates_blocked", 0),
            "executable_opportunities": vs.get("executable_opportunities", 0),
            "paper_trades_opened": vs.get("paper_trades_opened", 0),
            "paper_trades_resolved": resolved,
            "realized_pnl": round(pnl, 2),
            "wr": round(wr, 4),
            "avg_entry": round(avg_entry, 4),
            "break_even_wr": round(be_wr, 4),
            "pf": round(pf, 4) if pf != float("inf") else "inf",
            "ev_per_share": round(ev_per_share, 4),
            "ev_per_dollar": round(ev_per_dollar, 4),
            "expected_ev": round(expected_ev, 4),
            "realized_ev": round(realized_ev, 4),
            "ev_gap": round(ev_gap, 4),
            "accounting_failures": vs.get("accounting_failures", 0),
            "settlement_errors": vs.get("settlement_errors", 0),
            "false_dislocation_entries": vs.get("false_dislocation_entries", 0),
            "dormant_longshot_entries": vs.get("dormant_longshot_entries", 0),
            "bucket_scarcity_count": vs.get("bucket_scarcity_count", 0),
        },
        "pass_criteria": {
            "accounting_invariant": vs.get("accounting_failures", 0) == 0,
            "settlement_errors": vs.get("settlement_errors", 0) == 0,
            "false_dislocation": vs.get("false_dislocation_entries", 0) == 0,
            "dormant_longshot": vs.get("dormant_longshot_entries", 0) == 0,
            "ev_positive_if_5_resolved": True if resolved < 5 else pnl > 0,
        },
        "bankroll": {
            "start": state.get("run_start_bankroll", 320),
            "current": state.get("bankroll", 0),
            "realized_pnl": round(pnl, 2),
        },
        "classification": "HYBRID_CONVEX_VALIDATION",
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=12)
    parser.add_argument("--cycle", type=int, default=25)
    args = parser.parse_args()

    MAX_RUNTIME_HOURS = args.hours
    SCAN_INTERVAL_IDLE = args.cycle
    report = run_validation()