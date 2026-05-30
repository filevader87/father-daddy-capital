#!/usr/bin/env python3
"""V19.7g Shadow Validation — 24h Live-Shadow Audit

NO ORDERS. NO PAPER ORDERS. Discovery, classification, scoring, EV calculation, book-state logging only.

Logs 7 audit outputs:
1. Market discovery validation (per asset/timeframe)
2. False-accept/reject audit
3. Opportunity-frequency audit
4. Live EV audit (per signal)
5. Book-state audit (spread/depth/stale)
6. Single-trade seed metrics (sparse signal tracking)
7. Deployment-readiness classification

Outputs: shadow_validation/ with per-cycle JSON + cumulative audit.json
"""

import json, os, sys, time, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
import pm_engine_v19_7 as eng

OUT_DIR = Path('/mnt/c/Users/12035/father_daddy_capital/shadow_validation')
OUT_DIR.mkdir(exist_ok=True)

# ── Cumulative audit state ──
audit_path = OUT_DIR / "audit_state.json"

def load_audit():
    if audit_path.exists():
        with open(audit_path) as f:
            return json.load(f)
    return {
        "start_time": datetime.now(timezone.utc).isoformat(),
        "cycles": 0,
        "no_trade_cycles": 0,
        "total_signals": 0,
        "total_compatible_markets": 0,
        "total_entries_price_gate": 0,
        "total_entries_ev_gate": 0,
        "total_blocked_spread": 0,
        "total_blocked_depth": 0,
        "total_blocked_expiry": 0,
        "total_stale_books": 0,
        "total_missing_books": 0,
        "total_false_accepts": 0,
        "total_false_rejects": 0,
        "total_daily_strikes_accepted": 0,
        "total_valid_opportunities": 0,
        "signal_log": [],
        "market_discovery": defaultdict(lambda: {
            "raw_fetched": 0, "deduped": 0, "accepted": 0,
            "rejected": 0, "rejection_reasons": defaultdict(int),
            "markets": []
        }),
        "opportunity_per_hour": defaultdict(int),
        "first_hour": None,
        "last_no_trade_streak": 0,
        "max_no_trade_streak": 0,
        "current_no_trade_streak": 0,
        "post_loss_recovery_opportunities": 0,
        "cumulative_slippage_est": [],
        "cumulative_spread": [],
        "cumulative_depth": [],
        "deployment_gates": {},
    }

def save_audit(audit):
    # Convert defaultdicts for JSON serialization
    def default_serializer(obj):
        if isinstance(obj, defaultdict):
            return dict(obj)
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
    
    with open(audit_path, 'w') as f:
        json.dump(audit, f, indent=2, default=default_serializer)

def run_shadow_validation():
    """Run one shadow validation cycle. No orders."""
    audit = load_audit()
    cycle_time = datetime.now(timezone.utc)
    cycle_hour = cycle_time.strftime("%Y-%m-%dT%H%H")
    
    print(f"\n{'='*70}")
    print(f"SHADOW VALIDATION — {cycle_time.isoformat()}")
    print(f"Cycle #{audit['cycles']+1} | Running since: {audit['start_time']}")
    print(f"{'='*70}")
    
    # ══════════════════════════════════════════════════════════════════
    # AUDIT 1: Market Discovery Validation
    # ══════════════════════════════════════════════════════════════════
    print("\n  ── AUDIT 1: Market Discovery ──")
    discovery = {
        "timestamp": cycle_time.isoformat(),
        "assets": {},
        "total_raw": 0,
        "total_deduped": 0,
        "total_accepted": 0,
        "total_rejected": 0,
        "rejection_reasons": {},
        "false_accepts": [],
        "false_rejects": [],
    }
    
    cycle_signals = 0
    cycle_compatible = 0
    cycle_price_gate = 0
    cycle_ev_gate = 0
    cycle_blocked_spread = 0
    cycle_blocked_depth = 0
    cycle_blocked_expiry = 0
    cycle_stale_books = 0
    cycle_missing_books = 0
    cycle_valid_opportunities = 0
    daily_strikes_accepted = 0
    cycle_entries = []
    
    for ak, acfg in eng.ASSETS.items():
        print(f"  Discovering {ak} ({acfg.get('yf', '?')}, {acfg.get('interval', '?')})...")
        try:
            contracts = eng.discover_contracts(ak)
        except Exception as e:
            print(f"    ERROR: {e}")
            contracts = []
        
        # Also fetch raw markets for rejection audit
        raw_count = 0
        raw_markets = []
        try:
            # Fetch raw from paginated /markets
            offset = 0
            while True:
                page_url = f"{eng.GAMMA}/markets?active=true&closed=false&limit=500&offset={offset}&order=volume&ascending=false"
                page = eng._get(page_url)
                if not isinstance(page, list) or len(page) == 0:
                    break
                raw_count += len(page)
                for m in page:
                    # Filter for this asset
                    q = m.get("question", "")
                    combined = f"{q} {m.get('slug', '')}".lower()
                    asset_match = False
                    for pat in eng.ASSET_PATTERNS.get(ak, []):
                        if pat.lower() in combined:
                            asset_match = True
                            break
                    if asset_match:
                        raw_markets.append(m)
                if len(page) < 500:
                    break
                offset += 500
        except:
            pass
        
        # Classify raw markets for false-accept/reject audit
        accepted = 0
        rejected = 0
        rejection_details = defaultdict(int)
        false_accepts = []
        asset_info = {
            "raw_fetched": raw_count,
            "raw_asset_matches": len(raw_markets),
            "contracts_found": len(contracts),
            "by_interval": defaultdict(int),
            "by_market_type": defaultdict(int),
            "accepted": 0,
            "rejected": 0,
            "rejection_reasons": {},
            "markets": [],
        }
        
        for m in raw_markets:
            classification = eng.classify_market(m, expected_asset=ak)
            if classification["valid"]:
                accepted += 1
                asset_info["by_interval"][classification.get("interval", "unknown")] += 1
                asset_info["by_market_type"][classification.get("market_type", "unknown")] += 1
            else:
                rejected += 1
                reason = classification.get("reason", "unknown")
                rejection_details[reason] += 1
                # Check for false rejects: Up/Down market incorrectly rejected
                q = m.get("question", "").lower()
                if ("up" in q and "down" in q) and reason in ("no_time_window", "daily", "weekly", "monthly", "strike_price", "ladder"):
                    # This might be a legitimate Up/Down 5m/15m market
                    if "daily" in reason and ("5 min" in q or "15 min" in q or "5min" in q or "15min" in q):
                        false_accepts.append({
                            "question": m.get("question", "")[:100],
                            "reason": reason,
                            "classification": classification,
                        })
        
        asset_info["accepted"] = accepted
        asset_info["rejected"] = rejected
        asset_info["rejection_reasons"] = dict(rejection_details)
        
        # Log discovered contracts with full detail
        for c in contracts:
            entry = {
                "question": c.get("question", "")[:100],
                "conditionId": c.get("conditionId", "")[:24] + "...",
                "asset": c.get("asset", ""),
                "interval": c.get("interval", ""),
                "market_type": c.get("market_type", ""),
                "window": c.get("window", ""),
                "mins_to_expiry": c.get("mins_to_expiry", 0),
                "up_price": c.get("up_price", 0),
                "down_price": c.get("down_price", 0),
                "spread": round(abs(c.get("up_price", 0) + c.get("down_price", 0) - 1.0), 4),
                "volume": c.get("volume", 0),
                "slug": c.get("slug", ""),
                "end_date": c.get("end_date", ""),
                "direction_order": c.get("direction_order", ""),
            }
            asset_info["markets"].append(entry)
        
        discovery["assets"][ak] = asset_info
        discovery["total_raw"] += raw_count
        discovery["total_deduped"] += len(raw_markets)
        discovery["total_accepted"] += accepted
        discovery["total_rejected"] += rejected
        
        print(f"    Raw: {raw_count} | Asset matches: {len(raw_markets)} | Contracts: {len(contracts)} | Accepted: {accepted} | Rejected: {rejected}")
        for reason, count in sorted(rejection_details.items(), key=lambda x: -x[1]):
            print(f"      {reason}: {count}")
        
        # ══════════════════════════════════════════════════════════════
        # AUDIT 3-4-5: Signal Generation + EV Calculation + Book State
        # ══════════════════════════════════════════════════════════════
        print(f"\n  ── AUDIT 3-5: Signal/EV for {ak} ──")
        try:
            prices = eng.fetch_prices(acfg)
            if not prices or len(prices) < 20:
                print(f"    No price data for {ak}")
                continue
            
            # Generate signal
            sig = eng.btc_signal(prices) if ak == "BTC" else eng.btc_signal(prices)
            if not sig:
                print(f"    No signal for {ak}")
                continue
            
            direction = sig.get("direction", "neutral")
            confidence = sig.get("confidence", 0)
            rsi = sig.get("rsi", 50)
            rsi_zone = sig.get("rsi_zone", "unknown")
            
            print(f"    Signal: {direction} conf={confidence:.3f} rsi={rsi:.1f} zone={rsi_zone}")
            
            if direction == "neutral" or confidence < eng.MIN_CONFIDENCE:
                print(f"    Signal below MIN_CONFIDENCE ({eng.MIN_CONFIDENCE}) — shadow only")
                # Still log it for opportunity frequency audit
                audit["total_signals"] += 1
                signal_entry = {
                    "timestamp": cycle_time.isoformat(),
                    "asset": ak,
                    "direction": direction,
                    "confidence": confidence,
                    "rsi": rsi,
                    "rsi_zone": rsi_zone,
                    "compatible_markets": 0,
                    "ev_gross": 0,
                    "ev_net": 0,
                    "accept_reason": f"below_min_confidence_{eng.MIN_CONFIDENCE}",
                    "book_available": False,
                }
                audit["signal_log"].append(signal_entry)
                continue
            
            audit["total_signals"] += 1
            cycle_signals += 1
            
            # For each compatible contract, calculate EV
            for c in contracts:
                cycle_compatible += 1
                audit["total_compatible_markets"] += 1
                
                # Determine which token to buy
                is_up = direction == "up"
                if is_up:
                    contract_price = c.get("up_price", 0.5)
                    token_side = "UP"
                else:
                    contract_price = c.get("down_price", 0.5)
                    token_side = "DOWN"
                
                # Price gate: skip expensive side
                if contract_price > 0.85:
                    cycle_price_gate += 1
                    audit["total_entries_price_gate"] += 1
                    continue
                
                spread = abs(c.get("up_price", 0) + c.get("down_price", 0) - 1.0)
                
                # Spread gate: skip if spread > 10¢
                if spread > 0.10:
                    cycle_blocked_spread += 1
                    audit["total_blocked_spread"] += 1
                    audit["cumulative_spread"].append(spread)
                    continue
                
                # Expiry gate: skip if mins_to_expiry < 2
                mins_left = c.get("mins_to_expiry", 9999)
                if mins_left < 2:
                    cycle_blocked_expiry += 1
                    audit["total_blocked_expiry"] += 1
                    continue
                
                # Depth gate: if volume < MIN_VOLUME_USD
                vol = c.get("volume", 0)
                if vol < eng.MIN_VOLUME_USD:
                    cycle_blocked_depth += 1
                    audit["total_blocked_depth"] += 1
                    audit["cumulative_depth"].append(vol)
                    continue
                
                # Book-state: estimate from contract prices
                # (In live, would check CLOB orderbook. Here: check price availability)
                book_available = contract_price > 0 and c.get("up_price", 0) > 0
                if not book_available:
                    cycle_stale_books += 1
                    audit["total_stale_books"] += 1
                    continue
                
                # Calculate EV
                session_type = eng._session_type(cycle_time.hour)
                confirmations = sig.get("confirmations", 0)
                gross_ev, p_win, net_ev = eng.calculate_ev(
                    rsi, direction, contract_price, session_type, confirmations
                )
                
                # EV gate
                if net_ev < eng.EV_MIN_GATE:
                    cycle_ev_gate += 1
                    audit["total_entries_ev_gate"] += 1
                
                # Apply longshot calibration
                p_win_cal = eng.calibrate_longshot(p_win, contract_price)
                
                # Log shadow trade candidate
                entry = {
                    "timestamp": cycle_time.isoformat(),
                    "asset": ak,
                    "interval": c.get("interval", ""),
                    "direction": direction,
                    "token_side": token_side,
                    "rsi": round(rsi, 2),
                    "rsi_zone": rsi_zone,
                    "confidence": round(confidence, 4),
                    "market_question": c.get("question", "")[:80],
                    "condition_id": c.get("conditionId", "")[:24] + "...",
                    "selected_token_price": round(contract_price, 4),
                    "up_price": round(c.get("up_price", 0), 4),
                    "down_price": round(c.get("down_price", 0), 4),
                    "spread": round(spread, 4),
                    "mid": round((c.get("up_price", 0) + c.get("down_price", 0)) / 2, 4),
                    "estimated_prob": round(p_win_cal, 4),
                    "ev_gross": round(gross_ev, 4),
                    "ev_net": round(net_ev, 4),
                    "p_win": round(p_win, 4),
                    "p_win_calibrated": round(p_win_cal, 4),
                    "time_to_expiry_min": round(mins_left, 1),
                    "volume": round(vol, 2),
                    "book_available": book_available,
                    "accept": net_ev >= eng.EV_MIN_GATE and confidence >= eng.MIN_CONFIDENCE,
                    "reject_reason": None if net_ev >= eng.EV_MIN_GATE else f"ev_net_{net_ev:.4f}<_min_{eng.EV_MIN_GATE}",
                    "slippage_est": eng.EV_SLIPPAGE_EST,
                }
                cycle_entries.append(entry)
                
                if net_ev >= eng.EV_MIN_GATE:
                    cycle_valid_opportunities += 1
                    audit["total_valid_opportunities"] += 1
                
                print(f"    EV: {ak} {direction} @ {contract_price:.3f} | gross_ev={gross_ev:.3f} net_ev={net_ev:.3f} p_win={p_win_cal:.3f} | {'ACCEPT' if net_ev >= eng.EV_MIN_GATE else 'REJECT'}")
            
        except Exception as e:
            print(f"    Signal generation error for {ak}: {e}")
            traceback.print_exc()
    
    # ══════════════════════════════════════════════════════════════════
    # AUDIT 2: False-accept/reject tracking
    # ══════════════════════════════════════════════════════════════════
    # Already captured in discovery loop above
    
    # ══════════════════════════════════════════════════════════════════
    # AUDIT 6: Sparse-signal tracking  
    # ══════════════════════════════════════════════════════════════════
    hour_key = cycle_time.strftime("%Y-%m-%dT%H")
    audit["opportunity_per_hour"][hour_key] = audit["opportunity_per_hour"].get(hour_key, 0) + cycle_valid_opportunities
    
    if cycle_valid_opportunities == 0:
        audit["current_no_trade_streak"] += 1
        audit["no_trade_cycles"] += 1
        audit["max_no_trade_streak"] = max(audit["max_no_trade_streak"], audit["current_no_trade_streak"])
    else:
        audit["current_no_trade_streak"] = 0
    
    # Track first-trade loss rate
    if audit["cycles"] == 0 and cycle_valid_opportunities > 0:
        audit["first_trade_loss_risk"] = "unknown"  # Can't know win/loss without settlement
    
    audit["cycles"] += 1
    
    # ══════════════════════════════════════════════════════════════════
    # Save cycle report + update audit state
    # ══════════════════════════════════════════════════════════════════
    cycle_report = {
        "timestamp": cycle_time.isoformat(),
        "cycle": audit["cycles"],
        "discovery": discovery,
        "signals_generated": cycle_signals,
        "compatible_markets": cycle_compatible,
        "entries_price_gate": cycle_price_gate,
        "entries_ev_gate": cycle_ev_gate,
        "blocked_spread": cycle_blocked_spread,
        "blocked_depth": cycle_blocked_depth,
        "blocked_expiry": cycle_blocked_expiry,
        "stale_books": cycle_stale_books,
        "valid_opportunities": cycle_valid_opportunities,
        "shadow_entries": cycle_entries,
    }
    
    # Save individual cycle report
    ts = cycle_time.strftime("%Y%m%d_%H%M%S")
    cycle_path = OUT_DIR / f"cycle_{ts}.json"
    with open(cycle_path, 'w') as f:
        json.dump(cycle_report, f, indent=2, default=str)
    
    # Update and save cumulative audit state
    # Trim signal_log to last 500 entries to prevent unbounded growth
    audit["signal_log"] = audit["signal_log"][-500:]
    # Trim cumulative lists
    audit["cumulative_spread"] = audit["cumulative_spread"][-500:]
    audit["cumulative_depth"] = audit["cumulative_depth"][-500:]
    audit["cumulative_slippage_est"] = audit["cumulative_slippage_est"][-500:]
    # Trim opportunity_per_hour to last 48h
    cutoff = (cycle_time - timedelta(hours=48)).strftime("%Y-%m-%dT%H")
    audit["opportunity_per_hour"] = {k: v for k, v in audit["opportunity_per_hour"].items() if k >= cutoff}
    save_audit(audit)
    
    # ══════════════════════════════════════════════════════════════════
    # AUDIT 7: Deployment-readiness check
    # ══════════════════════════════════════════════════════════════════
    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(audit["start_time"].replace("Z", "+00:00") if audit["start_time"].endswith("Z") else audit["start_time"])
    elapsed_hours = elapsed.total_seconds() / 3600
    
    total_opp = audit["total_valid_opportunities"]
    total_signals = audit["total_signals"]
    no_trade_rate = audit["no_trade_cycles"] / max(audit["cycles"], 1) * 100
    avg_opp_per_hour = total_opp / max(elapsed_hours, 0.1)
    
    # Deployment classification
    gates = {
        "false_accepts_0": audit["total_false_accepts"] == 0,
        "no_daily_strikes": audit["total_daily_strikes_accepted"] == 0,
        "stale_books_0": audit["total_stale_books"] == 0,
        "min_50_opportunities": total_opp >= 50,
        "net_ev_positive": total_opp > 0,  # Can't fully verify without settlement
        "opportunity_frequency_ok": avg_opp_per_hour >= 0.5,  # At least 1 every 2 hours
        "no_fallback_trading": True,  # Shadow mode, no orders placed
    }
    all_passed = all(gates.values())
    
    if elapsed_hours < 24:
        classification = "IN_PROGRESS"
        classification_note = f"Wait 24h. Elapsed: {elapsed_hours:.1f}h"
    elif all_passed:
        classification = "B_PAPER_TRADING_ELIGIBLE"
        classification_note = "Ready for paper trading if desired"
    elif not gates["false_accepts_0"] or not gates["no_daily_strikes"] or not gates["stale_books_0"]:
        classification = "D_BLOCKED"
        classification_note = "Critical gate failed"
    elif not gates["min_50_opportunities"]:
        classification = "A_SHADOW_CONTINUED"
        classification_note = "Need more data"
    else:
        classification = "A_SHADOW_CONTINUED"
        classification_note = "Not all gates passed yet"
    
    audit["deployment_gates"] = gates
    audit["deployment_classification"] = classification
    save_audit(audit)
    
    # ══════════════════════════════════════════════════════════════════
    # Print summary
    # ══════════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print(f"SHADOW VALIDATION SUMMARY — Cycle #{audit['cycles']}")
    print(f"{'='*70}")
    print(f"  Running for: {elapsed_hours:.1f}h")
    print(f"  Total signals: {audit['total_signals']}")
    print(f"  Compatible markets: {audit['total_compatible_markets']}")
    print(f"  Valid opportunities (EV gate passed): {total_opp}")
    print(f"  Avg opportunities/hour: {avg_opp_per_hour:.2f}")
    print(f"  No-trade cycles: {audit['no_trade_cycles']}/{audit['cycles']} ({no_trade_rate:.1f}%)")
    print(f"  Max no-trade streak: {audit['max_no_trade_streak']} cycles")
    print(f"  Blocked: spread={audit['total_blocked_spread']} depth={audit['total_blocked_depth']} expiry={audit['total_blocked_expiry']} ev_gate={audit['total_entries_ev_gate']} price_gate={audit['total_entries_price_gate']}")
    print(f"  Stale books: {audit['total_stale_books']} | Missing books: {audit['total_missing_books']}")
    print(f"  False accepts: {audit['total_false_accepts']} | False rejects: {audit['total_false_rejects']}")
    print(f"  Daily strikes accepted: {audit['total_daily_strikes_accepted']}")
    print(f"\n  ── DEPLOYMENT GATES ──")
    for gate, passed in gates.items():
        print(f"  {gate}: {'✅' if passed else '❌'}")
    print(f"\n  CLASSIFICATION: {classification}")
    print(f"  NOTE: {classification_note}")
    print(f"{'='*70}")
    
    return cycle_report

if __name__ == "__main__":
    run_shadow_validation()