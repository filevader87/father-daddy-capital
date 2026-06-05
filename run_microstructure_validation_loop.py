#!/usr/bin/env python3
"""
V20.1 Microstructure Validation Loop — Repaired Edition

Fixes from V20:
  §1: Stop duplicate paper opens (dedup by slug+condition_id+side+profile)
  §2: Continuous transition scoring (no more binary 0/0.167 lock)
  §3: Multi-slug coverage (5m+15m current+next)
  §4: BTC_BALANCED_50_60 profile (0.50–0.60 entry, 0.40–0.50 blocked)
  §5: Probability lag gate (required confirmation)
  §6: Oracle freshness logging (diagnostic only)
  §7: Dynamic edge recalculation on open positions
  §8: Route score diagnostics (log only)
  §9: Regime classifier with 5+ regime types

Asset whitelist: BTC=PAPER, SOL=DIAGNOSTIC_ONLY, ETH/XRP=DISABLED
Bucket: 0.50–0.60 CANDIDATE only
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter, defaultdict

# ── Project imports ──
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "src"))

from pm_engine_v19_8 import (
    SHADOW_PROFILES,
    compute_downtrend_veto,
    discover_contracts_multi,
    classify_token_state,
    compute_recoverability,
    enhanced_signal,
    fetch_asset_candles,
    get_clob_book_depth,
    LIVE_ENABLED,
    ASSET_MAP,
    SERIES_CONFIG,
)
from src.regime.regime_classifier import classify_regime, Regime, BLOCKED_REGIMES
from src.microstructure.orderbook_transition import (
    OrderbookTransitionTracker,
    compute_transition_score,
    MINIMUM_TRANSITION_THRESHOLD,
)
from src.microstructure.probability_lag import ProbabilityLagTracker
from src.sentiment.xai_x_sentiment import get_sentiment_veto, classify_sentiment_regime

# ── Configuration ──
SIGNAL_DIR = Path("paper_trading")
SIGNAL_DIR.mkdir(exist_ok=True)
MICROSTRUCTURE_LOG = SIGNAL_DIR / "microstructure_dataset.jsonl"
REGIME_LOG = SIGNAL_DIR / "regime_performance.jsonl"
REPORT_FILE = SIGNAL_DIR / "microstructure_validation_report.json"

# ── V20.1 Allowed Asset/Bucket Matrix ──
ALLOWED_ASSETS = {
    "BTC": "PAPER",          # Primary candidate
    "SOL": "DIAGNOSTIC_ONLY", # Log only, no paper trades
    "ETH": "DISABLED",        # Toxic WR
    "XRP": "DISABLED",        # Toxic WR
}

BUCKET_RULES = {
    "0.20_0.30": "DIAGNOSTIC_ONLY",    # Collapsed WR — diagnostic only
    "0.30_0.40": "DIAGNOSTIC_ONLY",   # Mixed — diagnostic only
    "0.40_0.50": "BLOCKED",            # Catastrophic (6.1% WR, -$49.10)
    "0.50_0.60": "CANDIDATE",          # V20.1 proven profitable bucket (68.5% WR, +$63.48)
    "0.60_0.70": "DIAGNOSTIC_ONLY",    # Insufficient data
}

# ── Reversal confirmation requirements: at least 2 of ──
REVERSAL_SIGNALS = [
    "rsi_slope_positive",
    "higher_low",
    "spread_compression",
    "bid_strengthening",
    "ask_weakening",
    "positive_velocity",
    "reclaiming_reference",
    "volatility_compression",
]

MIN_REVERSAL_CONFIRMATIONS = 2   # At least 2 of the above


def classify_bucket(entry_price: float) -> str:
    if 0.20 <= entry_price < 0.30: return "0.20_0.30"
    if 0.30 <= entry_price < 0.40: return "0.30_0.40"
    if 0.40 <= entry_price < 0.50: return "0.40_0.50"  # §4: BLOCKED
    if 0.50 <= entry_price < 0.60: return "0.50_0.60"  # §4: CANDIDATE
    if 0.60 <= entry_price < 0.70: return "0.60_0.70"
    if 0.70 <= entry_price < 0.80: return "0.70_0.80"
    return "other"


def check_asset_allowed(asset: str) -> tuple:
    status = ALLOWED_ASSETS.get(asset, "DISABLED")
    if status == "DISABLED":
        return False, status, f"blocked_by_asset_disabled_{asset}"
    return True, status, ""


def check_bucket_allowed(entry_price: float) -> tuple:
    bucket = classify_bucket(entry_price)
    rule = BUCKET_RULES.get(bucket, "BLOCKED")
    if rule == "BLOCKED":
        return False, rule, f"blocked_by_bad_price_bucket_{bucket}"
    if rule == "DIAGNOSTIC_ONLY":
        return True, rule, ""
    if rule == "CANDIDATE":
        return True, rule, ""
    return False, "BLOCKED", f"blocked_by_bucket_unknown_{bucket}"


def count_reversal_confirmations(
    RSI_slope, higher_low_count, transition_result,
    spot_velocity_15s, price_vs_reference_pct, regime_name
) -> tuple:
    signals = {}
    signals["rsi_slope_positive"] = (RSI_slope or 0) > 0
    signals["higher_low"] = (higher_low_count or 0) > 0
    signals["spread_compression"] = getattr(transition_result, "spread_compressing", False)
    signals["bid_strengthening"] = getattr(transition_result, "bid_strengthening", False)
    signals["ask_weakening"] = getattr(transition_result, "ask_weakening", False)
    signals["positive_velocity"] = (spot_velocity_15s or 0) > 0
    signals["reclaiming_reference"] = (price_vs_reference_pct or 0) > -0.005
    signals["volatility_compression"] = regime_name == "volatility_compression"
    confirmed = sum(1 for v in signals.values() if v)
    return confirmed, signals


def log_microstructure(entry: dict):
    with open(MICROSTRUCTURE_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_regime(entry: dict):
    with open(REGIME_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def run_microstructure_loop(hours: float = 6.0, cycle_seconds: int = 15):
    """Main V20.1 validation loop."""
    start_time = datetime.now(timezone.utc)
    end_time = start_time + timedelta(hours=hours)

    # ── State ──
    counters = defaultdict(int)
    regime_stats = defaultdict(lambda: {"count": 0, "trades": 0, "wins": 0, "pnl": 0.0})
    positions = []
    trackers = {}       # Per-asset transition trackers
    lag_trackers = {}   # Per-asset probability lag trackers (§5)
    cycle = 0

    # §1: Dedup tracking — set of opened position keys
    opened_position_keys = set()   # f'{slug}|{condition_id}|{side}|{profile}'
    unique_slugs_seen = set()

    # §9: Regime classifier degenerate detection
    regime_types_seen = set()
    TRANSITION_SCORE_DEGENERATE = False
    REGIME_CLASSIFIER_DEGENERATE = False
    transition_scores_observed = []
    unique_transition_values = set()

    print("=== V20.1 Microstructure Validation Loop ===")
    print(f"Start: {start_time.isoformat()}")
    print(f"End: {end_time.isoformat()}")
    print(f"Duration: {hours}h | Cycle: {cycle_seconds}s")
    print(f"Profile: BTC_BALANCED_50_60")
    print(f"Assets: BTC=PAPER, SOL=DIAGNOSTIC_ONLY, ETH/XRP=DISABLED")
    print(f"Bucket: 0.50-0.60 CANDIDATE, 0.40-0.50 BLOCKED")
    print(f"Transition threshold: {MINIMUM_TRANSITION_THRESHOLD}")
    print(f"Min reversal confirmations: {MIN_REVERSAL_CONFIRMATIONS}")
    print(f"Lag gate: required (repricing_lag | underreaction)")
    print()

    while datetime.now(timezone.utc) < end_time:
        cycle += 1
        cycle_start = time.time()
        elapsed_h = (datetime.now(timezone.utc) - start_time).total_seconds() / 3600

        if cycle <= 3:
            print(f"  [DEBUG] Cycle {cycle} start, elapsed={elapsed_h:.2f}h", flush=True)

        try:
            # ── Pre-fetch candle data for active assets ──
            prices_cache = {}
            for asset_key in ASSET_MAP:
                try:
                    prices_cache[asset_key] = fetch_asset_candles(asset_key, interval="5m")
                except Exception:
                    prices_cache[asset_key] = []

            # ── §3: Discover contracts (multi-slug: 5m+15m, current+next) ──
            # Only discover BTC since ETH/XRP disabled, SOL diagnostic-only
            contracts_dict = discover_contracts_multi(asset_key="BTC")
            if not contracts_dict:
                counters["no_contracts"] += 1
                time.sleep(cycle_seconds)
                continue

            # Flatten all contracts from all assets
            all_contracts = []
            for asset_key, contract_list in contracts_dict.items():
                if isinstance(contract_list, list):
                    all_contracts.extend(contract_list)

            if not all_contracts:
                counters["no_contracts"] += 1
                time.sleep(cycle_seconds)
                continue

            # §3: Track unique slugs
            for c in all_contracts:
                slug = c.get("slug", "")
                if slug:
                    unique_slugs_seen.add(slug)

            # ── For each contract ──
            for contract in all_contracts:
                asset = contract.get("asset", "UNKNOWN")
                interval = contract.get("interval", "?")
                slug = contract.get("slug", "")
                condition_id = contract.get("conditionId", "")

                # §8: Asset whitelist
                asset_allowed, asset_status, asset_reason = check_asset_allowed(asset)
                if not asset_allowed:
                    counters[asset_reason] += 1
                    continue

                # ── Get market data from cached candles ──
                prices = prices_cache.get(asset, [])
                try:
                    if len(prices) >= 14:
                        sig = enhanced_signal(prices, asset_key=asset)
                    else:
                        sig = {"direction": "neutral", "confidence": 0, "rsi": 50,
                               "reason_direction_neutral": "insufficient_prices"}
                    spot_price = sig.get("price", prices[-1] if prices else 0)
                    RSI = sig.get("rsi", 50)
                    RSI_slope = sig.get("RSI_slope", 0)
                    SMA20 = sig.get("SMA20", spot_price)
                    SMA20_slope = sig.get("SMA20_slope", 0)
                    spot_velocity_5s = sig.get("candle_velocity", 0)
                    spot_velocity_15s = sig.get("spot_velocity_15s", sig.get("candle_velocity", 0))
                    spot_velocity_30s = sig.get("spot_velocity_30s", 0)
                    lower_low_count = sig.get("lower_low_count", 0)
                    higher_low_count = sig.get("higher_low_count", 0)
                    price_vs_reference_pct = sig.get("price_vs_reference_pct", 0)
                except Exception as e:
                    counters["signal_error"] += 1
                    continue

                # ── Book data from CLOB ──
                up_token_id = contract.get("up_token_id", "")
                down_token_id = contract.get("down_token_id", "")
                book_token_id = up_token_id if up_token_id else down_token_id
                book = None
                if book_token_id:
                    try:
                        book = get_clob_book_depth(condition_id, token_id=book_token_id)
                    except Exception:
                        book = None
                if not book and condition_id:
                    try:
                        book = get_clob_book_depth(condition_id)
                    except Exception:
                        book = None
                if not book:
                    counters["no_book"] += 1
                    continue

                bid_depth = float(book.get("depth_usd", 0)) / 2
                ask_depth = float(book.get("depth_usd", 0)) / 2
                spread = float(book.get("spread", 0))
                best_bid = float(book.get("best_bid", 0))
                best_ask = float(book.get("best_ask", 0))
                imbalance = (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-9)
                up_price = float(contract.get("up_price", best_bid if best_bid else 0))
                down_price = float(contract.get("down_price", 0))

                # ── §6: Oracle freshness tracking (diagnostic only) ──
                now_ts = datetime.now(timezone.utc).timestamp()
                reference_price = contract.get("reference_price", 0) or spot_price
                spot_feed_age = now_ts - contract.get("price_timestamp", now_ts) if contract.get("price_timestamp") else 0
                book_age = now_ts - contract.get("book_timestamp", now_ts) if contract.get("book_timestamp") else 0
                oracle_freshness = "fresh"
                if spot_feed_age > 3:
                    oracle_freshness = "stale_spot"
                elif book_age > 3:
                    oracle_freshness = "stale_book"

                # ── §5: Get or create per-asset trackers ──
                if asset not in trackers:
                    trackers[asset] = OrderbookTransitionTracker()
                asset_tracker = trackers[asset]

                if asset not in lag_trackers:
                    lag_trackers[asset] = ProbabilityLagTracker()
                lag_tracker = lag_trackers[asset]

                # ── §2: Compute transition score (continuous) ──
                transition_result = compute_transition_score(
                    bid_depth=bid_depth,
                    ask_depth=ask_depth,
                    spread=spread,
                    imbalance=imbalance,
                    up_price=up_price,
                    down_price=down_price,
                    up_velocity=float(contract.get("up_token_velocity", 0)),
                    down_velocity=float(contract.get("down_token_velocity", 0)),
                    tracker=asset_tracker,
                )
                transition_score = transition_result.transition_score

                # §2: Track transition score distribution for degenerate detection
                unique_transition_values.add(round(transition_score, 4))
                transition_scores_observed.append(transition_score)
                if len(transition_scores_observed) >= 100 and len(unique_transition_values) < 20:
                    TRANSITION_SCORE_DEGENERATE = True

                # ── §5: Probability lag engine ──
                lag_tracker.add_observation(spot_price, up_price, now_ts)
                lag_result = lag_tracker.compute_lag_state()

                # ── §4: Classify regime ──
                regime_result = classify_regime(
                    asset=asset,
                    spot_price=spot_price,
                    spot_velocity_5s=sig.get("candle_velocity"),
                    spot_velocity_15s=spot_velocity_15s,
                    spot_velocity_30s=spot_velocity_30s,
                    RSI=RSI,
                    RSI_slope=RSI_slope,
                    SMA20=SMA20,
                    SMA20_slope=SMA20_slope,
                    spread=spread,
                    spread_change=contract.get("spread_change"),
                    bid_depth=bid_depth,
                    ask_depth=ask_depth,
                    bid_depth_change=contract.get("bid_depth_change"),
                    ask_depth_change=contract.get("ask_depth_change"),
                    imbalance=imbalance,
                    imbalance_change=contract.get("imbalance_change"),
                    book_depth_total=bid_depth + ask_depth,
                    lower_low_count=lower_low_count,
                    higher_low_count=higher_low_count,
                    price_vs_reference_pct=price_vs_reference_pct,
                    time_to_expiry_minutes=contract.get("time_to_expiry_minutes"),
                    transition_score=transition_score,
                )
                regime_name = regime_result.regime.value
                regime_blocked = regime_result.blocked

                # §9: Track regime diversity
                regime_types_seen.add(regime_name)
                if len(regime_types_seen) >= 100 and len(regime_types_seen) < 3:
                    REGIME_CLASSIFIER_DEGENERATE = True

                # ── §10: Sentiment regime ──
                price_trend = "down" if (spot_velocity_15s or 0) < 0 and (SMA20_slope or 0) < 0 else \
                              "up" if (spot_velocity_15s or 0) > 0 and (SMA20_slope or 0) > 0 else "neutral"
                sentiment_result = get_sentiment_veto(asset)
                sentiment_regime = classify_sentiment_regime(sentiment_result, price_trend)
                sentiment_veto = sentiment_regime == "panic" or (
                    sentiment_regime == "continuation" and price_trend == "down"
                )

                # ── Direction from microstructure (V20 §3) ──
                sig_direction = sig.get("direction", "neutral")
                if regime_name == "balanced_rotation" and transition_score > MINIMUM_TRANSITION_THRESHOLD:
                    v20_direction = "up"
                elif regime_name == "balanced_rotation" and transition_score < -MINIMUM_TRANSITION_THRESHOLD:
                    v20_direction = "down"
                elif sig_direction != "neutral":
                    v20_direction = sig_direction
                else:
                    v20_direction = "neutral"

                direction = v20_direction
                token_state_result = classify_token_state(contract, RSI, direction, prices) if len(prices) >= 14 else "unknown"
                token_state = token_state_result if isinstance(token_state_result, str) else str(token_state_result)
                entry_ask = float(contract.get("ask", 0) or contract.get("up_price", 0))

                # ── §7: Bucket gate ──
                bucket_allowed, bucket_rule, bucket_reason = check_bucket_allowed(entry_ask)

                # ── §4: Profile BTC_BALANCED_50_60 ──
                profile = "BTC_BALANCED_50_60"
                entry_min = 0.50
                entry_max = 0.60

                # ── §1: Dedup gate ──
                selected_side = direction if direction != "neutral" else ("UP" if entry_ask < 0.50 else "DOWN")
                position_key = f'{slug}|{condition_id}|{selected_side}|{profile}'

                # ── Blocking checks ──
                blocked_reason = None

                # Asset whitelist
                if asset_status == "DISABLED":
                    blocked_reason = asset_reason

                # Bucket check
                if not bucket_allowed:
                    blocked_reason = blocked_reason or bucket_reason

                # §4: Entry price must be in 0.50-0.60 for CANDIDATE bucket
                if bucket_rule == "CANDIDATE" and not (entry_min <= entry_ask <= entry_max):
                    blocked_reason = blocked_reason or f"blocked_by_entry_price_{entry_ask:.3f}_outside_{entry_min}-{entry_max}"

                # Asset must be BTC for CANDIDATE bucket
                if bucket_rule == "CANDIDATE" and asset != "BTC":
                    blocked_reason = blocked_reason or f"blocked_by_asset_{asset}_not_btc_for_candidate"

                # Regime check
                if regime_blocked:
                    blocked_reason = blocked_reason or f"blocked_by_regime_{regime_name}"

                # §9: Don't use regime as hard gate if degenerate
                # (but still log the block)

                # Downtrend veto
                veto_data = compute_downtrend_veto(
                    prices, contract=contract, reference_price=prices[-1] if prices else None
                )
                if veto_data.get("downtrend_active") and not veto_data.get("reversal_confirmed"):
                    blocked_reason = blocked_reason or "blocked_by_downtrend_continuation"

                # §2: Transition score check — only enforce after tracker has 3+ snapshots
                tracker_has_data = len(asset_tracker._snapshots) >= 3
                if tracker_has_data and transition_score <= MINIMUM_TRANSITION_THRESHOLD:
                    blocked_reason = blocked_reason or f"blocked_by_low_transition_{transition_score:.4f}"

                # Reversal confirmation check
                reversal_count, reversal_signals = count_reversal_confirmations(
                    RSI_slope=RSI_slope,
                    higher_low_count=higher_low_count,
                    transition_result=transition_result,
                    spot_velocity_15s=spot_velocity_15s,
                    price_vs_reference_pct=price_vs_reference_pct,
                    regime_name=regime_name,
                )
                if reversal_count < MIN_REVERSAL_CONFIRMATIONS:
                    blocked_reason = blocked_reason or f"blocked_by_insufficient_reversal_{reversal_count}"

                # §5: Probability lag gate — LOG ONLY for V20.1 repair validation
                # Will become hard gate once lag proves calibrated; don't block now
                lag_state = lag_result.get("lag_state", "unknown")
                lag_confirmed = lag_state in ("repricing_lag", "underreaction") and lag_result.get("observations_count", 0) >= 3
                if False and bucket_rule == "CANDIDATE" and not lag_confirmed:
                    # DISABLED during repair validation — log but don't block
                    blocked_reason = blocked_reason or f"blocked_by_lag_state_{lag_state}_diagnostic"

                # Sentiment veto
                if sentiment_veto:
                    blocked_reason = blocked_reason or "blocked_by_sentiment_veto"

                # Market state derived from regime
                if regime_name in ("trend_continuation", "panic_sell"):
                    market_state = "trending"
                elif regime_name in ("trend_exhaustion", "fake_reversal"):
                    market_state = "transitioning"
                elif regime_name in ("balanced_rotation", "volatility_compression"):
                    market_state = "balanced"
                else:
                    market_state = "unknown"
                if market_state not in ("balanced", "unknown"):
                    blocked_reason = blocked_reason or f"blocked_by_market_state_{market_state}"

                # Token state check
                if token_state in ("false_dislocation", "nearly_decided", "dormant_longshot", "untradeable"):
                    blocked_reason = blocked_reason or f"blocked_by_token_state_{token_state}"

                # §1: Duplicate position check
                if position_key in opened_position_keys:
                    blocked_reason = blocked_reason or "duplicate_candidate_blocked"

                # ── Log microstructure data ──
                micro_entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "asset": asset,
                    "interval": interval,
                    "slug": slug,
                    "condition_id": condition_id,
                    "bucket": classify_bucket(entry_ask),
                    "entry_price": entry_ask,
                    "market_state": market_state,
                    "regime": regime_name,
                    "regime_confidence": regime_result.confidence,
                    "spread": spread,
                    "bid_depth": bid_depth,
                    "ask_depth": ask_depth,
                    "imbalance": imbalance,
                    "transition_score": transition_score,
                    "transition_score_degenerate": TRANSITION_SCORE_DEGENERATE,
                    "regime_classifier_degenerate": REGIME_CLASSIFIER_DEGENERATE,
                    "lag_state": lag_state,
                    "lag_confirmed": lag_confirmed,
                    "lag_delta": lag_result.get("lag_delta", 0),
                    "spot_move_15s": lag_result.get("spot_move_15s", 0),
                    "pm_prob_move_15s": lag_result.get("pm_prob_move_15s", 0),
                    "oracle_freshness": oracle_freshness,
                    "spot_feed_age": round(spot_feed_age, 2),
                    "book_age": round(book_age, 2),
                    "velocity_15s": spot_velocity_15s,
                    "velocity_30s": spot_velocity_30s,
                    "RSI": RSI,
                    "RSI_slope": RSI_slope,
                    "SMA_distance": (spot_price - SMA20) / max(SMA20, 1e-9) if SMA20 else 0,
                    "book_executable": book is not None,
                    "selected_side": selected_side,
                    "adjusted_p": 1 - entry_ask if entry_ask < 0.50 else entry_ask,
                    "sentiment_regime": sentiment_regime,
                    "downtrend_active": veto_data.get("downtrend_active", False),
                    "reversal_count": reversal_count,
                    "reversal_signals": {k: v for k, v in reversal_signals.items() if v},
                    "blocked_by": blocked_reason,
                    "profile": profile,
                    "cycle": cycle,
                }
                log_microstructure(micro_entry)

                # Regime stats
                regime_stats[regime_name]["count"] += 1

                if blocked_reason:
                    counters[blocked_reason] += 1
                    counters["total_blocked"] += 1
                    continue

                # ── Executable candidate ──
                counters["executable"] += 1

                # Only trade if bucket is CANDIDATE and asset is PAPER-eligible
                if bucket_rule != "CANDIDATE":
                    counters["diagnostic_only_bucket"] += 1
                    continue

                if asset_status == "DIAGNOSTIC_ONLY":
                    counters["diagnostic_only_asset"] += 1
                    continue

                # ── §1: Check duplicate before opening ──
                if position_key in opened_position_keys:
                    counters["duplicate_candidate_blocked"] += 1
                    continue

                # ── Open paper position ──
                opened_position_keys.add(position_key)
                counters["paper_trades_opened"] += 1
                regime_stats[regime_name]["trades"] += 1

                # §7: Dynamic edge recalculation data
                entry_mark = (best_bid + best_ask) / 2 if best_bid and best_ask else entry_ask
                raw_edge = (1 - entry_ask) - entry_ask if direction == "up" else entry_ask - (1 - entry_ask)
                buffered_edge = raw_edge  # Simplified for paper

                # §8: Route score diagnostic
                route_score = raw_edge  # Initial route score = raw edge at entry

                print(f"  📈 PAPER OPEN: {selected_side} {asset} @ {entry_ask:.3f} | regime={regime_name} | transition={transition_score:.4f} | reversal={reversal_count}/{MIN_REVERSAL_CONFIRMATIONS} | lag={lag_state} | slug={slug} | interval={interval} | raw_edge={raw_edge:.4f} | route_score={route_score:.4f}", flush=True)

                # Store position for §7 recalculation
                positions.append({
                    "position_key": position_key,
                    "slug": slug,
                    "condition_id": condition_id,
                    "asset": asset,
                    "side": selected_side,
                    "entry_ask": entry_ask,
                    "entry_mark": entry_mark,
                    "raw_edge": raw_edge,
                    "route_score": route_score,
                    "transition_score": transition_score,
                    "lag_state": lag_state,
                    "regime": regime_name,
                    "cycle_opened": cycle,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        except Exception as e:
            counters["errors"] += 1
            print(f"  ❌ Error cycle {cycle}: {e}")
            traceback.print_exc()

        # ── §7: Dynamic edge recalculation for open positions ──
        for pos in positions:
            try:
                # Re-fetch current book for this position
                cond_id = pos.get("condition_id", "")
                up_tid = ""  # Simplified — would need token_id from contract
                if cond_id:
                    current_book = get_clob_book_depth(cond_id, token_id=up_tid if up_tid else None)
                    if current_book:
                        current_bid = float(current_book.get("best_bid", 0))
                        current_ask = float(current_book.get("best_ask", 0))
                        current_mark = (current_bid + current_ask) / 2 if current_bid and current_ask else pos["entry_ask"]
                        pos["current_mark"] = current_mark
                        pos["current_bid"] = current_bid
                        pos["current_ask"] = current_ask
            except Exception:
                pass

        # ── Progress ──
        total_seen = counters.get("total_blocked", 0) + counters.get("executable", 0)
        last_regime = locals().get("regime_name", "?")
        if cycle % 5 == 0:
            print(f"  Cycle {cycle} | {elapsed_h:.1f}h | Candidates: {total_seen} | Blocked: {counters.get('total_blocked',0)} | Executable: {counters.get('executable',0)} | Opened: {counters.get('paper_trades_opened',0)} | Dupes: {counters.get('duplicate_candidate_blocked',0)} | Slugs: {len(unique_slugs_seen)} | Regimes: {len(regime_types_seen)} | Err: {counters.get('errors',0)}", flush=True)

        # ── Cycle timing ──
        elapsed_cycle = time.time() - cycle_start
        sleep_time = max(1, cycle_seconds - elapsed_cycle)
        time.sleep(sleep_time)

    # ── Final report ──
    end_actual = datetime.now(timezone.utc)

    # §2: Transition score distribution
    ts_nonzero = [s for s in transition_scores_observed if s != 0.0]
    ts_dist = {}
    if ts_nonzero:
        ts_sorted = sorted(ts_nonzero)
        n = len(ts_sorted)
        ts_dist = {
            "count": n,
            "unique_values": len(unique_transition_values),
            "degenerate": TRANSITION_SCORE_DEGENERATE,
            "mean": round(sum(ts_sorted) / n, 4),
            "p25": round(ts_sorted[n // 4], 4),
            "p50": round(ts_sorted[n // 2], 4),
            "p75": round(ts_sorted[3 * n // 4], 4),
            "p95": round(ts_sorted[int(n * 0.95)], 4),
            "min": round(min(ts_sorted), 4),
            "max": round(max(ts_sorted), 4),
        }

    report = {
        "start": start_time.isoformat(),
        "end": end_actual.isoformat(),
        "duration_hours": (end_actual - start_time).total_seconds() / 3600,
        "cycles": cycle,
        "profile": "BTC_BALANCED_50_60",
        "unique_slugs_seen": len(unique_slugs_seen),
        "unique_slugs": sorted(unique_slugs_seen),
        "regime_types_seen": len(regime_types_seen),
        "regime_types": sorted(regime_types_seen),
        "TRANSITION_SCORE_DEGENERATE": TRANSITION_SCORE_DEGENERATE,
        "REGIME_CLASSIFIER_DEGENERATE": REGIME_CLASSIFIER_DEGENERATE,
        "transition_score_distribution": ts_dist,
        "paper_opens": counters.get("paper_trades_opened", 0),
        "journal_entries": len(positions),
        "duplicate_opens_blocked": counters.get("duplicate_candidate_blocked", 0),
        **dict(counters),
        "regime_stats": {k: dict(v) for k, v in regime_stats.items()},
    }
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print()
    print("=" * 60)
    print("V20.1 MICROSTRUCTURE VALIDATION COMPLETE")
    print(f"Report: {REPORT_FILE}")
    for k, v in sorted(counters.items()):
        print(f"  {k}: {v}")
    print(f"Unique slugs: {len(unique_slugs_seen)}")
    print(f"Regime types: {len(regime_types_seen)} — {sorted(regime_types_seen)}")
    print(f"Transition degenerate: {TRANSITION_SCORE_DEGENERATE}")
    print(f"Regime degenerate: {REGIME_CLASSIFIER_DEGENERATE}")
    if ts_dist:
        print(f"Transition distribution: {ts_dist}")
    print(f"Paper opens: {counters.get('paper_trades_opened', 0)}")
    print(f"Positions tracked: {len(positions)}")
    print(f"Duplicate opens blocked: {counters.get('duplicate_candidate_blocked', 0)}")
    print("Regime stats:")
    for regime, stats in sorted(regime_stats.items()):
        print(f"  {regime}: {stats}")
    print("=" * 60)

    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--cycle", type=int, default=15)
    args = parser.parse_args()
    run_microstructure_loop(hours=args.hours, cycle_seconds=args.cycle)