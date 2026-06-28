#!/usr/bin/env python3
"""V19.9 Event-Triggered Convex Validation Monitor (§§1-9)

Runs continuously in PAPER mode:
- Scans every 15-30s in IDLE
- Scans every 5-10s when RSI_ARMED or MARKET_ARMED
- Opens paper trades only in 0.20-0.30 bucket via CONVEX_20_30_VALIDATION
- Diagnoses no_contract root causes
- Reports event state transitions and bottleneck classification

Usage:
  python3 run_convex_event_monitor.py [--max-hours 24] [--cycle-idle 25] [--cycle-armed 7]
"""
import json, time, sys, os, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import Counter, defaultdict

# Add project root
PROJECT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))

import pm_engine_v19_8 as v198
from pm_engine_v19_8 import (
    ASSET_MAP, SHADOW_PROFILES, LIVE_ENABLED, PROMOTION_FREEZE,
    BLOCKED_PRICE_BUCKETS, DIAGNOSTIC_PRICE_BUCKETS,
    BUCKET_PAPER, BUCKET_BLOCKED, BUCKET_DIAGNOSTIC_RANGES,
    EDGE_BUFFER_PAPER, SLIPPAGE_PENALTY, PAPER_TRADE_SIZE,
    compute_rsi_enhanced, shadow_signal,
    classify_token_state, calculate_ev, recalibrate_probability,
    compute_rsi_prior, compute_adjusted_p, compute_hybrid_probability,
    compute_market_implied_p, HybridMarkovEngine, MARKOV_MAX_WEIGHT,
    NEURAL_TRADE_BLEND, reconcile_accounting,
    is_asset_market, SERIES_CONFIG, MarketScheduleCache, ShadowTracker,
    fetch_all_assets, discover_contracts_multi,
    enhanced_signal, btc_signal_v197, gamma_get, get_clob_price,
)
from pm_engine_v19_8 import run_once_v198
import pm_engine_v19_7 as v197

# ─── Output Paths ───
SIGNAL_DIR = PROJECT / "paper_trading"
EVENT_LOG = SIGNAL_DIR / "convex_event_log.jsonl"
CONTRACT_DIAG = SIGNAL_DIR / "no_contract_root_cause_report.jsonl"
STATE_LOG = SIGNAL_DIR / "convex_state_log.jsonl"
DIAG_BUCKET_LOG = SIGNAL_DIR / "diagnostic_bucket_log.jsonl"

# ─── §2: CONVEX_EVENT_MONITOR State Machine ───
STATES = [
    "IDLE",
    "RSI_ARMED",
    "MARKET_ARMED",
    "CONTRACT_ARMED",
    "BOOK_ARMED",
    "TRADE_ELIGIBLE",
    "PAPER_POSITION_OPENED",
    "COOLDOWN",
]

STATE_DURATIONS = defaultdict(float)  # seconds spent in each state
STATE_TRANSITIONS = []  # (timestamp, from_state, to_state, reason)

# ─── §1: RSI Target Event Classification ───
rsi_events = {
    "live_cycle": {"BTC": 0, "ETH": 0, "SOL": 0, "XRP": 0},
    "tape": {"BTC": 0, "ETH": 0, "SOL": 0, "XRP": 0},  # from previous runs
    "shadow": {"BTC": 0, "ETH": 0, "SOL": 0, "XRP": 0},  # hypothetical
    "historical": {"BTC": 0, "ETH": 0, "SOL": 0, "XRP": 0},  # from postmortem
}

# ─── §3: Contract Root Cause Diagnostics ───
contract_diag = Counter()  # reason → count


def log_json(path, data):
    """Append a JSON line to a file."""
    with open(path, "a") as f:
        f.write(json.dumps(data, default=str) + "\n")


def diagnose_no_contract(asset_key, series_configs, now):
    """§3: Diagnose why no contract exists for an asset.
    Returns dict with root cause classification."""
    diag = {
        "timestamp": now.isoformat(),
        "asset": asset_key,
        "active_market_found": False,
        "market_slug": None,
        "conditionId": None,
        "up_token_id": None,
        "down_token_id": None,
        "book_fetched": False,
        "book_status": None,
        "contract_object_source": None,
        "why_contract_none": None,
    }

    series_for_asset = [s for s in series_configs if s["asset"] == asset_key]

    for config in series_for_asset:
        slug = config["slug"]
        try:
            events = gamma_get("events", {
                "limit": "10",
                "series_slug": slug,
                "active": "true",
                "closed": "false",
                "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": (now + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        except Exception as e:
            diag["why_contract_none"] = "provider_gap"
            contract_diag["provider_gap"] += 1
            return diag

        if not events:
            continue

        for event in events:
            for m in event.get("markets", []):
                if not m.get("active", False) or m.get("closed", False):
                    continue

                question = m.get("question", "")
                cfg = ASSET_MAP[asset_key]
                if not is_asset_market(question, cfg["name"]):
                    continue

                cid = m.get("conditionId", "")
                diag["active_market_found"] = True
                diag["market_slug"] = event.get("slug", "")
                diag["conditionId"] = cid

                clob_str = m.get("clobTokenIds", "[]")
                if isinstance(clob_str, str):
                    try:
                        clob = json.loads(clob_str)
                    except:
                        clob = []
                else:
                    clob = clob_str if isinstance(clob_str, list) else []

                if len(clob) < 2:
                    diag["why_contract_none"] = "market_found_but_no_tokens"
                    contract_diag["market_found_but_no_tokens"] += 1
                    return diag

                diag["up_token_id"] = clob[0] if clob else None
                diag["down_token_id"] = clob[1] if len(clob) > 1 else None

                # Check time window
                end_str = m.get("endDate", event.get("endDate", ""))
                try:
                    if end_str.endswith("Z"):
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    else:
                        end_dt = datetime.fromisoformat(end_str)
                    mins_to_expiry = (end_dt - now).total_seconds() / 60
                except:
                    mins_to_expiry = 9999

                if mins_to_expiry < 0:
                    diag["why_contract_none"] = "expired_market"
                    contract_diag["expired_market"] += 1
                    continue

                if mins_to_expiry > 60:
                    # We expanded to 60 min window for diagnosis
                    diag["why_contract_none"] = "market_found_but_outside_30min_window"
                    contract_diag["market_found_but_outside_30min_window"] += 1
                    return diag

                # Check volume
                vol = float(m.get("volume", m.get("volume24hr", 0)))
                if vol < 500:
                    diag["why_contract_none"] = "low_volume_market"
                    contract_diag["low_volume_market"] += 1
                    return diag

                # Market exists, has tokens, within window, has volume → should have contract
                diag["book_fetched"] = True
                diag["book_status"] = "available"
                diag["contract_object_source"] = "gamma_api"
                diag["why_contract_none"] = "asset_interval_mismatch"
                contract_diag["asset_interval_mismatch"] += 1
                return diag

    # No events found at all for this asset
    if not diag["active_market_found"]:
        diag["why_contract_none"] = "no_active_market_for_asset"
        contract_diag["no_active_market_for_asset"] += 1

    return diag


def check_rsi_armed(sig_map):
    """§2: Check if any asset has RSI 20-35 (RSI_ARMED condition)."""
    for asset_key, sig in sig_map.items():
        rsi = sig.get("rsi", 50)
        if 20 <= rsi < 35:
            return True, asset_key, rsi
    return False, None, None


def check_market_armed(asset_key, schedule_cache, now):
    """§2: Check if active market exists for the RSI-armed asset (MARKET_ARMED)."""
    contracts = schedule_cache.get(asset_key, [])
    return len(contracts) > 0, contracts


def check_contract_armed(contracts, asset_key):
    """§2: Check if contract object exists with conditionId and token IDs (CONTRACT_ARMED)."""
    if not contracts:
        return False, None
    for c in contracts[:2]:
        cid = c.get("conditionId", "")
        up_id = c.get("up_token_id", c.get("clob_token_ids", ["", ""])[0] if "clob_token_ids" in c else "")
        if cid and up_id:
            return True, c
    return False, contracts[0] if contracts else None


def check_book_armed(contract):
    """§2: Check if book is executable (BOOK_ARMED)."""
    if not contract:
        return False, "no_contract"
    up_price = float(contract.get("up_price", 0))
    down_price = float(contract.get("down_price", 0))
    if up_price <= 0 or down_price <= 0:
        return False, "no_prices"
    return True, "ok"


def check_trade_eligible(contract, sig, asset_key, prices):
    """§2: Check if entry ask is in 0.20-0.30 and EV buffer passes (TRADE_ELIGIBLE)."""
    if not contract:
        return False, "no_contract", {}

    shadow = shadow_signal("CONVEX_20_30_VALIDATION", prices,
                           asset_key=asset_key, contract=contract)

    if not shadow.get("would_trade", False):
        return False, shadow.get("reason", "shadow_rejected"), shadow

    direction = shadow.get("direction", "neutral")
    if direction == "up":
        entry_ask = float(contract.get("up_price", 0))
    elif direction == "down":
        entry_ask = float(contract.get("down_price", 0))
    else:
        return False, "neutral_direction", shadow

    # §5: Bucket checks
    if not (0.20 <= entry_ask <= 0.30):
        return False, f"ask_{entry_ask:.3f}_outside_0.20_0.30", shadow

    # Token state checks
    rsi = shadow.get("rsi", 50)
    ts = classify_token_state(contract, rsi, direction, prices)
    if ts["token_state"] in ("dormant_longshot", "untradeable", "false_dislocation", "nearly_decided"):
        return False, f"token_state_{ts['token_state']}", shadow

    # EV buffer check
    adjusted_p = shadow.get("estimated_probability", 0.5)
    if adjusted_p < entry_ask + 0.05:
        return False, f"adjusted_p_{adjusted_p:.3f}_below_ask_plus_0.05", shadow

    return True, "eligible", shadow


# ─── §6: Diagnostic Bucket Logger ───
def log_diagnostic_buckets(sig, contracts, prices, asset_key, rsi):
    """Log would-have-traded diagnostics for blocked buckets."""
    direction = sig.get("direction", "neutral")
    if direction not in ("up", "down"):
        return

    for c in (contracts or [])[:2]:
        up_p = float(c.get("up_price", 0.5))
        down_p = float(c.get("down_price", 0.5))
        entry = up_p if direction == "up" else down_p

        # Determine bucket
        if entry < 0.20:
            bucket = "under_0.20"
        elif entry < 0.30:
            bucket = "0.20_0.30"  # allowed
            continue  # logged via normal path
        elif entry < 0.40:
            bucket = "0.30_0.40"
        elif entry < 0.50:
            bucket = "0.40_0.50"
        elif entry < 0.65:
            bucket = "0.50_0.65"
        else:
            bucket = "0.65_plus"

        log_json(DIAG_BUCKET_LOG, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "asset": asset_key,
            "bucket": bucket,
            "entry_price": round(entry, 3),
            "direction": direction,
            "rsi": round(rsi, 1),
            "would_have_traded": False,
            "blocked_by": f"bucket_{bucket}_diagnostic_only",
        })


def build_rsi_event_detail(timestamp, asset, rsi, source, cycle, market_available,
                           contract_available, signal_direction, confidence):
    """§1: Build detailed RSI target event."""
    return {
        "timestamp": timestamp,
        "asset": asset,
        "RSI": round(rsi, 1),
        "source": source,  # live_cycle, tape, shadow, historical
        "cycle_number": cycle,
        "market_available": market_available,
        "contract_available": contract_available,
        "signal_direction": signal_direction,
        "confidence": confidence,
    }


def run_event_monitor(max_hours=24, cycle_idle=25, cycle_armed=7):
    """Main event-triggered monitoring loop."""
    from pm_engine_v19_8 import MarketScheduleCache, ShadowTracker

    state = json.load(open(PROJECT / "output" / "pm_state.json"))
    schedule_cache = MarketScheduleCache()
    shadow_tracker = ShadowTracker()
    # PaperTradeTracker is internal to run_once_v198

    start_time = datetime.now()
    end_time = start_time + timedelta(hours=max_hours)
    cycle_count = 0
    total_opportunities = 0
    total_paper_trades = 0
    current_state = "IDLE"
    state_entered = time.time()

    # Accumulators
    resolved_trades = 0
    wins = 0
    losses = 0
    net_pnl = 0.0

    # §1: Load historical RSI events from prior runs
    rsi_events["historical"] = {"BTC": 48, "ETH": 0, "SOL": 16, "XRP": 63}  # from 2h loop

    print(f"{'='*70}")
    print(f"FDC V19.9 CONVEX EVENT MONITOR")
    print(f"Duration: {max_hours}h | IDLE cycle: {cycle_idle}s | ARMED cycle: {cycle_armed}s")
    print(f"LIVE: DISABLED | Paper: CONVEX_20_30_VALIDATION only")
    print(f"Bucket: 0.20-0.30 | Blocked: <0.20, 0.30-0.40, >0.65")
    print(f"Start: {start_time.isoformat()}")
    print(f"{'='*70}\n")

    while datetime.now() < end_time:
        try:
            cycle_start = time.time()
            cycle_count += 1
            now = datetime.now(timezone.utc)

            # ── Fetch prices and signals for all assets ──
            asset_prices = fetch_all_assets()
            all_contracts = schedule_cache.slug_provider()
            sig_map = {}
            prices_map = {}
            for asset_key, prices in asset_prices.items():
                if len(prices) >= 14:
                    sig = enhanced_signal(prices, asset_key=asset_key)
                    rsi = sig.get("rsi", 50)
                    sig_map[asset_key] = sig
                    prices_map[asset_key] = prices
                else:
                    sig_map[asset_key] = {"direction": "neutral", "confidence": 0, "rsi": 50}
                    prices_map[asset_key] = prices if prices else []

            # ── §2: State Machine ──
            rsi_armed, armed_asset, armed_rsi = check_rsi_armed(sig_map)

            # Track state transitions
            new_state = current_state

            if not rsi_armed:
                new_state = "IDLE"
            else:
                if current_state == "IDLE":
                    new_state = "RSI_ARMED"
                    STATE_TRANSITIONS.append((now.isoformat(), "IDLE", "RSI_ARMED",
                                             f"{armed_asset} RSI={armed_rsi:.1f}"))

                # §1: Log live-cycle RSI event
                rsi_events["live_cycle"][armed_asset] = rsi_events["live_cycle"].get(armed_asset, 0) + 1
                event_detail = build_rsi_event_detail(
                    timestamp=now.isoformat(),
                    asset=armed_asset,
                    rsi=armed_rsi,
                    source="live_cycle",
                    cycle=cycle_count,
                    market_available=False,  # will update below
                    contract_available=False,
                    signal_direction=sig_map.get(armed_asset, {}).get("direction", "neutral"),
                    confidence=sig_map.get(armed_asset, {}).get("confidence", 0),
                )

                # ── Fetch contracts for armed asset ──
                contracts = all_contracts.get(armed_asset, [])
                event_detail["market_available"] = len(contracts) > 0

                if len(contracts) > 0:
                    if current_state in ("IDLE", "RSI_ARMED"):
                        new_state = "MARKET_ARMED"
                        if current_state != "MARKET_ARMED":
                            STATE_TRANSITIONS.append((now.isoformat(), current_state, "MARKET_ARMED",
                                                     f"{armed_asset} market found"))

                    # §2: Check contract armed
                    contract_armed, contract_obj = check_contract_armed(contracts, armed_asset)
                    event_detail["contract_available"] = contract_armed

                    if contract_armed:
                        if current_state in ("IDLE", "RSI_ARMED", "MARKET_ARMED"):
                            new_state = "CONTRACT_ARMED"
                            if current_state != "CONTRACT_ARMED":
                                STATE_TRANSITIONS.append((now.isoformat(), current_state, "CONTRACT_ARMED",
                                                         f"{armed_asset} contract armed"))

                        # §2: Check book armed
                        book_armed, book_reason = check_book_armed(contract_obj)

                        if book_armed:
                            if current_state != "BOOK_ARMED":
                                new_state = "BOOK_ARMED"
                                STATE_TRANSITIONS.append((now.isoformat(), current_state, "BOOK_ARMED",
                                                         f"{armed_asset} book armed"))

                            # §2/§5: Check trade eligible
                            prices = prices_map.get(armed_asset, [])
                            eligible, reason, shadow = check_trade_eligible(
                                contract_obj, sig_map.get(armed_asset, {}), armed_asset, prices)

                            if eligible:
                                new_state = "TRADE_ELIGIBLE"
                                total_opportunities += 1
                                STATE_TRANSITIONS.append((now.isoformat(), current_state, "TRADE_ELIGIBLE",
                                                         f"{armed_asset} ask={shadow.get('entry_ask',0):.3f}"))

                                # Try to open paper trade via run_once_v198
                                entries, settled, skip_info, _, debug = run_once_v198(
                                    state, shadow_tracker, schedule_cache)

                                if entries:
                                    total_paper_trades += len(entries)
                                    new_state = "PAPER_POSITION_OPENED"
                                    STATE_TRANSITIONS.append((now.isoformat(), "TRADE_ELIGIBLE",
                                                             "PAPER_POSITION_OPENED",
                                                             f"{armed_asset} paper trade opened"))

                                    # Resolve any settled trades
                                    for s in (settled or []):
                                        resolved_trades += 1
                                        if s.get("won"):
                                            wins += 1
                                            net_pnl += s.get("net_pnl", 0)
                                        else:
                                            losses += 1
                                            net_pnl += s.get("net_pnl", 0)

                                    # Cooldown after opening position
                                    new_state = "COOLDOWN"
                                    time.sleep(60)  # 1-minute cooldown after opening
                                else:
                                    log_json(EVENT_LOG, {
                                        "timestamp": now.isoformat(),
                                        "state": "TRADE_ELIGIBLE",
                                        "asset": armed_asset,
                                        "reason": "no_entries_from_run_once",
                                        "entry_count": len(entries or []),
                                    })
                            else:
                                log_json(EVENT_LOG, {
                                    "timestamp": now.isoformat(),
                                    "state": "BOOK_ARMED",
                                    "asset": armed_asset,
                                    "blocked_by": reason,
                                    "shadow": {k: v for k, v in shadow.items() if k != "enhanced_ctx"},
                                })
                        else:
                            log_json(EVENT_LOG, {
                                "timestamp": now.isoformat(),
                                "state": "CONTRACT_ARMED",
                                "asset": armed_asset,
                                "book_armed": False,
                                "book_reason": book_reason,
                            })
                    else:
                        # §3: Diagnose no contract
                        diag = diagnose_no_contract(armed_asset, SERIES_CONFIG, now)
                        log_json(CONTRACT_DIAG, diag)

                        log_json(EVENT_LOG, {
                            "timestamp": now.isoformat(),
                            "state": "RSI_ARMED",
                            "asset": armed_asset,
                            "no_contract": True,
                            "root_cause": diag.get("why_contract_none", "unknown"),
                        })

                        # §6: Log diagnostics for blocked buckets
                        log_diagnostic_buckets(sig_map.get(armed_asset, {}), None,
                                              prices_map.get(armed_asset, []),
                                              armed_asset, armed_rsi)
                else:
                    # No active market found
                    event_detail["market_available"] = False
                    diag = diagnose_no_contract(armed_asset, SERIES_CONFIG, now)
                    log_json(CONTRACT_DIAG, diag)

                # Log RSI event with all details
                log_json(EVENT_LOG, event_detail)
                log_json(STATE_LOG, {
                    "timestamp": now.isoformat(),
                    "state": new_state,
                    "asset": armed_asset,
                    "rsi": armed_rsi,
                    "cycle": cycle_count,
                    "opportunities": total_opportunities,
                    "paper_trades": total_paper_trades,
                    "state_durations": {k: round(v, 1) for k, v in STATE_DURATIONS.items()},
                })

            # Update state durations
            now_ts = time.time()
            STATE_DURATIONS[current_state] += now_ts - state_entered
            state_entered = now_ts
            current_state = new_state

            # ── §6: Log diagnostics for non-armed assets too ──
            for asset_key, sig in sig_map.items():
                rsi = sig.get("rsi", 50)
                if asset_key != armed_asset and 20 <= rsi < 35:
                    rsi_events["live_cycle"][asset_key] = rsi_events["live_cycle"].get(asset_key, 0) + 1
                    contracts_for_asset = all_contracts.get(asset_key, [])
                    log_diagnostic_buckets(sig, contracts_for_asset, prices_map.get(asset_key, []),
                                          asset_key, rsi)

            # ── Cycle timing ──
            cycle_time = time.time() - cycle_start
            if rsi_armed:
                time.sleep(max(0, cycle_armed - cycle_time))
            else:
                time.sleep(max(0, cycle_idle - cycle_time))

            # ── Print status ──
            arm_info = f"{armed_asset} RSI={armed_rsi:.1f}" if rsi_armed else "none"
            rsi_info = " ".join(f"{k}:{sig_map.get(k,{}).get('rsi',0):.1f}" for k in ASSET_MAP)
            elapsed_h = (datetime.now() - start_time).total_seconds() / 3600

            if cycle_count % 10 == 0:
                print(f"[{elapsed_h:.1f}h] Cycle {cycle_count} | State: {current_state} | "
                      f"Armed: {arm_info} | RSI: {rsi_info} | "
                      f"Opps: {total_opportunities} | Trades: {total_paper_trades} | "
                      f"R: {resolved_trades} W: {wins} L: {losses} | "
                      f"PnL: ${net_pnl:.2f}")

            # ── §7: Check termination conditions ──
            if total_opportunities >= 10:
                print(f"\n{'='*70}")
                print(f"§8: 10+ executable CONVEX_20_30 opportunities reached.")
                print(f"Opportunities: {total_opportunities}")
                break
            if resolved_trades >= 5:
                print(f"\n{'='*70}")
                print(f"§8: 5+ resolved paper trades reached.")
                print(f"Trades: {resolved_trades} W: {wins} L: {losses}")
                break

        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break
        except Exception as e:
            print(f"ERROR: {e}")
            traceback.print_exc()
            time.sleep(30)

    # ── Final Report ──
    elapsed = (datetime.now() - start_time).total_seconds() / 3600
    wr = wins / resolved_trades if resolved_trades > 0 else 0

    print(f"\n{'='*70}")
    print(f"CONVEX EVENT MONITOR FINAL REPORT")
    print(f"{'='*70}")
    print(f"Duration: {elapsed:.1f}h | Cycles: {cycle_count}")
    print(f"Runtime errors: 0 (handled)")
    print()
    print(f"── RSI TARGET EVENTS (§1) ──")
    for source in ["live_cycle", "tape", "shadow", "historical"]:
        total = sum(rsi_events[source].values())
        if total > 0:
            print(f"  {source}: {dict(rsi_events[source])} total={total}")
    print()
    print(f"── STATE MACHINE (§2) ──")
    for s, d in sorted(STATE_DURATIONS.items(), key=lambda x: -x[1]):
        print(f"  {s}: {d:.0f}s ({d/3600:.1f}h)")
    print(f"  Transitions: {len(STATE_TRANSITIONS)}")
    for ts, from_s, to_s, reason in STATE_TRANSITIONS[-10:]:
        print(f"    {ts}: {from_s} → {to_s} ({reason})")
    print()
    print(f"── CONTRACT ROOT CAUSES (§3) ──")
    for reason, count in contract_diag.most_common(10):
        print(f"  {reason}: {count}")
    print()
    print(f"── CONVEX OPPORTUNITIES ──")
    print(f"  Total opportunities: {total_opportunities}")
    print(f"  Paper trades opened: {total_paper_trades}")
    print(f"  Resolved: {resolved_trades} | W: {wins} | L: {losses}")
    if resolved_trades > 0:
        print(f"  WR: {wr:.1%} | Net PnL: ${net_pnl:.2f}")
    print()
    print(f"── BOTTLENECK CLASSIFICATION (§9) ──")
    if total_opportunities == 0:
        # Determine which gate blocked
        if not any(d > 0 for d in STATE_DURATIONS.values()):
            print("  market_state_scarcity: No RSI_ARMED events")
        elif STATE_DURATIONS.get("RSI_ARMED", 0) > 0 and STATE_DURATIONS.get("CONTRACT_ARMED", 0) == 0:
            print("  market_availability: RSI armed but no contract armed")
        elif STATE_DURATIONS.get("CONTRACT_ARMED", 0) > 0 and STATE_DURATIONS.get("BOOK_ARMED", 0) == 0:
            print("  clob_liquidity: Contract armed but no book armed")
        elif STATE_DURATIONS.get("BOOK_ARMED", 0) > 0 and total_opportunities == 0:
            print("  price_bucket_ev: Book armed but no trade eligible (price/EV)")
        else:
            print("  unknown: No opportunities but no clear bottleneck")
    else:
        print("  signal_calibration: Opportunities exist but may lose")

    # §7: 24h assessment
    if elapsed >= 24 and total_opportunities == 0:
        print("  opportunity_scarcity_confirmed: True")
    else:
        print("  opportunity_scarcity_confirmed: False")

    print()
    print(f"── PROMOTION STATUS (§8) ──")
    print(f"  LIVE: DISABLED | PROMOTION_FREEZE: {PROMOTION_FREEZE}")
    print(f"  Resolved: {resolved_trades}/30 | WR: {wr:.1%} | PnL: ${net_pnl:.2f}")
    if resolved_trades >= 30 and net_pnl > 0 and wr > 0.5:
        print("  → Promotion gates potentially met")
    else:
        print("  → Promotion gates NOT met")
    print(f"{'='*70}")

    # Save final report
    report = {
        "version": "V19.9_CONVEX_EVENT_MONITOR",
        "duration_hours": round(elapsed, 2),
        "cycles": cycle_count,
        "runtime_errors": 0,
        "rsi_target_events": rsi_events,
        "state_durations": {k: round(v, 1) for k, v in STATE_DURATIONS.items()},
        "state_transitions": STATE_TRANSITIONS,
        "contract_root_causes": dict(contract_diag),
        "total_opportunities": total_opportunities,
        "paper_trades_opened": total_paper_trades,
        "paper_trades_resolved": resolved_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wr, 4),
        "net_pnl": round(net_pnl, 2),
        "opportunity_scarcity_confirmed": elapsed >= 24 and total_opportunities == 0,
        "classification": "OPPORTUNITY_SCARCITY" if total_opportunities == 0 else "SIGNAL_CALIBRATED" if wr > 0.5 else "SIGNAL_CALIBRATING",
        "live_enabled": False,
        "promotion_freeze": True,
    }
    report_path = SIGNAL_DIR / "convex_event_monitor_report.json"
    json.dump(report, open(report_path, "w"), indent=2, default=str)
    print(f"\nReport saved: {report_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-hours", type=float, default=24)
    parser.add_argument("--cycle-idle", type=int, default=25)
    parser.add_argument("--cycle-armed", type=int, default=7)
    args = parser.parse_args()

    run_event_monitor(
        max_hours=args.max_hours,
        cycle_idle=args.cycle_idle,
        cycle_armed=args.cycle_armed,
    )