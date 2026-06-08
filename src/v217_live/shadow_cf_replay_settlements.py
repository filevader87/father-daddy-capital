#!/usr/bin/env python3
"""
Shadow Counterfactual Settlement Replay
=========================================
Repairs shadow counterfactual resolution by:
1. Backfilling missing fields in existing shadow events
2. Polling Gamma API for expired market resolution
3. Computing binary settlement + hypothetical PnL
4. Writing settlement records + evaluation

LIVE BOT UNCHANGED. This only resolves shadow counterfactuals.

Directive §19: SHADOW_SETTLEMENT_BROKEN → REPAIR_REQUIRED
"""
import json
import time
import re
import sys
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter, defaultdict

# ─── Paths ───
BASE = Path("/home/naq1987s/father-daddy-capital")
EVENTS_FILE = BASE / "output/v2171_live/shadow_counterfactual_events.jsonl"
SETTLEMENTS_FILE = BASE / "output/v2171_live/shadow_counterfactual_settlements.jsonl"
STATE_FILE = BASE / "output/v2171_live/shadow_counterfactual_state.json"
EVAL_FILE = BASE / "output/v2171_live/shadow_counterfactual_evaluation.json"
AUDIT_FILE = BASE / "output/v2171_live/shadow_counterfactual_replay_audit.json"

GAMMA_URL = "https://gamma-api.polymarket.com"
POSITION_SIZE_USD = 1.00
SLIPPAGE_PCT = 0.02
GRACE_PERIOD_SEC = 300  # 5 min after expiry before attempting resolution

# ─── Gamma API Resolution ───

def fetch_market_resolution(slug: str, condition_id: str) -> dict:
    """
    Resolve market outcome via Gamma API.
    Returns dict with: resolved (bool), winner (str), resolution_confidence (str),
                       settlement_error (bool), settlement_error_reason (str)
    """
    errors = []

    # ─── Method 1: slug exact match ───
    try:
        url = f"{GAMMA_URL}/markets?slug={slug}&limit=5"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc-shadow-cf/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            markets = json.loads(r.read())
        if markets:
            m = markets[0]
            # Check by condition_id match
            api_cid = m.get("conditionId", "")
            if api_cid and api_cid.lower() == condition_id.lower():
                return _parse_market_outcome(m, slug)
            # If condition_id doesn't match, it's ambiguous
            if api_cid:
                errors.append(f"condition_id mismatch: expected={condition_id[:16]}... got={api_cid[:16]}...")
                # Continue to method 2
    except Exception as e:
        errors.append(f"slug query failed: {e}")

    # ─── Method 2: condition_id exact match ───
    try:
        url = f"{GAMMA_URL}/markets?condition_id={condition_id}&limit=5"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc-shadow-cf/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            markets = json.loads(r.read())
        if markets:
            # Check for multiple matches (ambiguity)
            if len(markets) > 1:
                # Filter to exact slug match
                slug_matches = [m for m in markets if m.get("slug") == slug]
                if len(slug_matches) == 1:
                    return _parse_market_outcome(slug_matches[0], slug)
                errors.append(f"condition_id returns {len(markets)} markets, slug not uniquely matched")
            else:
                return _parse_market_outcome(markets[0], slug)
    except Exception as e:
        errors.append(f"condition_id query failed: {e}")

    # ─── Method 3: Try closed=true slug query ───
    try:
        url = f"{GAMMA_URL}/markets?closed=true&slug={slug}&limit=5"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc-shadow-cf/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            markets = json.loads(r.read())
        if markets:
            m = markets[0]
            api_cid = m.get("conditionId", "")
            if not api_cid or api_cid.lower() == condition_id.lower():
                return _parse_market_outcome(m, slug)
            errors.append(f"closed slug condition_id mismatch")
    except Exception as e:
        errors.append(f"closed slug query failed: {e}")

    # All methods failed
    return {
        "resolved": False,
        "winner": "UNKNOWN",
        "resolution_confidence": "LOW",
        "settlement_error": True,
        "settlement_error_reason": "; ".join(errors) if errors else "all resolution methods failed",
    }


def _parse_market_outcome(market: dict, slug: str) -> dict:
    """Parse a Gamma market object to determine binary outcome."""
    # Check resolution status
    is_resolved = market.get("resolved", False) or market.get("closed", False)
    outcome_prices = market.get("outcomePrices", "")
    question = market.get("question", "")

    if not is_resolved and not outcome_prices:
        return {
            "resolved": False,
            "winner": "PENDING",
            "resolution_confidence": "MEDIUM",
            "settlement_error": False,
            "settlement_error_reason": "market not yet resolved",
        }

    # Parse outcome prices
    if isinstance(outcome_prices, str):
        try:
            prices = json.loads(outcome_prices)
        except (json.JSONDecodeError, TypeError):
            prices = []
    elif isinstance(outcome_prices, list):
        prices = outcome_prices
    else:
        prices = []

    # Parse outcome labels
    outcomes_str = market.get("outcomes", "")
    if isinstance(outcomes_str, str):
        try:
            outcomes = json.loads(outcomes_str)
        except (json.JSONDecodeError, TypeError):
            outcomes = []
    elif isinstance(outcomes_str, list):
        outcomes = outcomes_str
    else:
        outcomes = []

    if len(prices) >= 2 and len(outcomes) >= 2:
        # Binary market: higher price wins
        p_up = float(prices[0]) if prices[0] else 0.0
        p_down = float(prices[1]) if prices[1] else 0.0

        # For resolved markets, winner settles to 1.0
        if is_resolved:
            if p_up >= 0.99:
                winner = "UP"
            elif p_down >= 0.99:
                winner = "DOWN"
            elif p_up > p_down:
                winner = "UP"  # best guess
            elif p_down > p_up:
                winner = "DOWN"  # best guess
            else:
                return {
                    "resolved": False,
                    "winner": "AMBIGUOUS",
                    "resolution_confidence": "LOW",
                    "settlement_error": True,
                    "settlement_error_reason": f"outcome prices ambiguous: up={p_up:.4f} down={p_down:.4f}",
                }
        else:
            # Not resolved yet
            return {
                "resolved": False,
                "winner": "PENDING",
                "resolution_confidence": "LOW",
                "settlement_error": False,
                "settlement_error_reason": "market closed but not resolved",
            }
    else:
        # Try to infer from question text
        q_lower = question.lower()
        if "down" in q_lower and "above" not in q_lower:
            # "Will BTC go down?" style
            if is_resolved:
                # Check if market description gives the answer
                pass
        return {
            "resolved": False,
            "winner": "UNKNOWN",
            "resolution_confidence": "LOW",
            "settlement_error": True,
            "settlement_error_reason": f"insufficient outcome data: prices={prices} outcomes={outcomes}",
        }

    confidence = "HIGH" if is_resolved else "MEDIUM"
    return {
        "resolved": True,
        "winner": winner,
        "resolution_confidence": confidence,
        "settlement_error": False,
        "settlement_error_reason": "",
    }


def fetch_clob_resolution(slug: str, condition_id: str, down_token_id: str) -> dict:
    """
    Alternative resolution via CLOB API: check if DOWN token price is 0 or 1.
    This is the most reliable binary settlement check.
    """
    try:
        # Polymarket CLOB markets endpoint
        url = f"https://clob.polymarket.com/markets/{condition_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "fdc-shadow-cf/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())

        # Check resolution
        question = data.get("question", "")
        is_resolved = data.get("resolved", False)
        closed = data.get("closed", False)
        tokens = data.get("tokens", [])

        # Find DOWN token outcome
        for t in tokens:
            if t.get("token_id") == down_token_id:
                outcome = t.get("outcome", "")
                winner = t.get("winner", None)
                if winner is True:
                    return {"resolved": True, "winner": "DOWN",
                            "resolution_confidence": "HIGH",
                            "settlement_error": False, "settlement_error_reason": ""}
                elif winner is False:
                    return {"resolved": True, "winner": "UP",
                            "resolution_confidence": "HIGH",
                            "settlement_error": False, "settlement_error_reason": ""}

        # Try outcome prices
        outcome_prices = data.get("outcomePrices", "")
        if isinstance(outcome_prices, str):
            try:
                prices = json.loads(outcome_prices)
            except:
                prices = []

        return {
            "resolved": False, "winner": "PENDING",
            "resolution_confidence": "LOW",
            "settlement_error": False,
            "settlement_error_reason": "CLOB: winner not yet determined",
        }
    except Exception as e:
        return {
            "resolved": False, "winner": "UNKNOWN",
            "resolution_confidence": "LOW",
            "settlement_error": True,
            "settlement_error_reason": f"CLOB query failed: {e}",
        }


# ─── Field Reconstruction ───

def reconstruct_event(e: dict) -> dict:
    """Backfill missing fields from available data."""
    # selected_side is always DOWN for shadow CF events
    e.setdefault("selected_side", "DOWN")
    e.setdefault("entry_price", e.get("hypothetical_entry_price", e.get("down_ask", 0.0)))
    e.setdefault("entry_bucket", e.get("bucket", "unknown"))

    # opposite_token_id: if we have condition_id we can look it up
    # For now mark as needing resolution
    e.setdefault("opposite_token_id", "")

    # expiry_timestamp from market_slug
    slug = e.get("market_slug", "")
    m = re.search(r"(\d+)$", slug)
    if m:
        exp_ts = int(m.group(1))
        e.setdefault("expiry_timestamp", exp_ts)
        # Also fill market_expiry_ts if empty
        if not e.get("market_expiry_ts"):
            e["market_expiry_ts"] = datetime.fromtimestamp(exp_ts, tz=timezone.utc).isoformat()

    # selected_token_id = down_token_id
    if not e.get("selected_token_id") and e.get("down_token_id"):
        e["selected_token_id"] = e["down_token_id"]

    return e


# ─── PnL Calculation ───

def compute_pnl(entry_price: float, selected_side_wins: bool,
                position_size_usd: float = POSITION_SIZE_USD,
                slippage_pct: float = SLIPPAGE_PCT) -> dict:
    """
    Binary settlement PnL computation.

    DOWN wins -> final_binary = 1.0 -> payout = shares * 1.00
    DOWN loses -> final_binary = 0.0 -> payout = 0.00
    """
    if entry_price <= 0:
        return {"gross_pnl": 0.0, "slippage_adjusted_pnl": 0.0, "shares": 0.0,
                "slippage_adjusted_entry": 0.0}

    shares = position_size_usd / entry_price

    if selected_side_wins:
        payout = shares * 1.00
        gross_pnl = payout - position_size_usd
    else:
        payout = 0.00
        gross_pnl = -position_size_usd

    # Conservative friction-adjusted PnL
    slippage_adjusted_entry = entry_price * (1.0 + slippage_pct)
    shares_after_slippage = position_size_usd / slippage_adjusted_entry

    if selected_side_wins:
        slippage_adjusted_pnl = shares_after_slippage - position_size_usd
    else:
        slippage_adjusted_pnl = -position_size_usd

    return {
        "gross_pnl": round(gross_pnl, 4),
        "slippage_adjusted_pnl": round(slippage_adjusted_pnl, 4),
        "shares": round(shares, 6),
        "slippage_adjusted_entry": round(slippage_adjusted_entry, 6),
        "payout": round(payout, 4),
    }


# ─── Main Replay Logic ───

def run_replay():
    print("=" * 70)
    print("SHADOW COUNTERFACTUAL SETTLEMENT REPLAY")
    print("=" * 70)
    print()

    # Load events
    if not EVENTS_FILE.exists():
        print("ERROR: No shadow events file found")
        return

    events = []
    with open(EVENTS_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    print(f"Loaded {len(events)} shadow events")
    print()

    # Reconstruct missing fields
    events = [reconstruct_event(e) for e in events]

    # Categorize events
    resolved_events = []
    pending_events = []
    unrecoverable_events = []
    error_events = []

    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    for e in events:
        exp_ts = e.get("expiry_timestamp", 0)
        if not exp_ts:
            # Try to extract from slug
            slug = e.get("market_slug", "")
            m = re.search(r"(\d+)$", slug)
            if m:
                exp_ts = int(m.group(1))
                e["expiry_timestamp"] = exp_ts
            else:
                e["_category"] = "unrecoverable"
                e["_reason"] = "no expiry timestamp and cannot extract from slug"
                unrecoverable_events.append(e)
                continue

        # Check if enough time has passed for resolution
        time_since_expiry = now_ts - exp_ts
        if time_since_expiry < -300:
            # Market hasn't expired yet
            e["_category"] = "pending"
            e["_reason"] = f"market expires in {-time_since_expiry:.0f}s"
            pending_events.append(e)
            continue

        # Market has expired, attempt resolution
        slug = e.get("market_slug", "")
        condition_id = e.get("condition_id", "")
        down_token_id = e.get("down_token_id", "")

        print(f"Resolving {e['event_id']}: {slug}")

        # Try CLOB API first (most reliable for binary settlement)
        resolution = fetch_clob_resolution(slug, condition_id, down_token_id)

        if resolution.get("resolved") and not resolution.get("settlement_error"):
            e["_resolution"] = resolution
            e["_category"] = "resolved"
            resolved_events.append(e)
            print(f"  → RESOLVED: winner={resolution['winner']} "
                  f"(confidence={resolution['resolution_confidence']})")
        else:
            # Fall back to Gamma API
            resolution = fetch_market_resolution(slug, condition_id)

            if resolution.get("resolved"):
                e["_resolution"] = resolution
                e["_category"] = "resolved"
                resolved_events.append(e)
                print(f"  → RESOLVED via Gamma: winner={resolution['winner']} "
                      f"(confidence={resolution['resolution_confidence']})")
            elif resolution.get("settlement_error"):
                e["_resolution"] = resolution
                e["_category"] = "error"
                e["_reason"] = resolution.get("settlement_error_reason", "unknown")
                error_events.append(e)
                print(f"  → ERROR: {e['_reason']}")
            else:
                # Pending or ambiguous
                e["_resolution"] = resolution
                e["_category"] = "pending"
                e["_reason"] = resolution.get("settlement_error_reason", "pending")
                pending_events.append(e)
                print(f"  → PENDING: {e.get('_reason', 'resolution not available')}")

        # Rate limit
        time.sleep(0.5)

    print()
    print(f"Resolution summary:")
    print(f"  Resolved: {len(resolved_events)}")
    print(f"  Pending:  {len(pending_events)}")
    print(f"  Errors:   {len(error_events)}")
    print(f"  Unrecoverable: {len(unrecoverable_events)}")
    print()

    # ─── Build Settlement Records ───
    settlements = []

    for e in resolved_events:
        res = e.get("_resolution", {})
        winner = res.get("winner", "UNKNOWN")
        selected_side_wins = (e.get("selected_side", "DOWN") == winner)

        if selected_side_wins:
            final_binary = 1.0
        else:
            final_binary = 0.0

        entry_price = e.get("hypothetical_entry_price", e.get("down_ask", 0.0))
        pnl = compute_pnl(entry_price, selected_side_wins)

        settlement = {
            "event_id": e["event_id"],
            "market_slug": e.get("market_slug", ""),
            "condition_id": e.get("condition_id", ""),
            "interval": e.get("interval", ""),
            "entry_ts": e.get("entry_ts", ""),
            "expiry_timestamp": e.get("expiry_timestamp", 0),
            "resolved_at": now.isoformat(),
            "selected_side": e.get("selected_side", "DOWN"),
            "entry_price": entry_price,
            "entry_bucket": e.get("bucket", ""),
            "final_binary": final_binary,
            "resolved_winner": winner,
            "win_loss": "WIN" if selected_side_wins else "LOSS",
            "gross_pnl": pnl["gross_pnl"],
            "slippage_adjusted_pnl": pnl["slippage_adjusted_pnl"],
            "resolution_source": "clob" if res.get("resolution_confidence") == "HIGH" else "gamma",
            "resolution_confidence": res.get("resolution_confidence", "LOW"),
            "settlement_error": res.get("settlement_error", False),
            "settlement_error_reason": res.get("settlement_error_reason", ""),
            "down_ask": e.get("down_ask", 0.0),
            "btc_velocity_15s": e.get("btc_velocity_15s", 0.0),
            "btc_velocity_30s": e.get("btc_velocity_30s", 0.0),
            "btc_velocity_60s": e.get("btc_velocity_60s", 0.0),
            "perp_velocity_15s": e.get("perp_velocity_15s", 0.0),
            "perp_velocity_30s": e.get("perp_velocity_30s", 0.0),
            "current_model_blocked_reason": e.get("current_model_blocked_reason", ""),
            "shadow_model_state": e.get("shadow_model_state", ""),
            "time_to_expiry": e.get("time_to_expiry", 0),
        }
        settlements.append(settlement)

    # Write settlements
    with open(SETTLEMENTS_FILE, "w") as f:
        for s in settlements:
            f.write(json.dumps(s) + "\n")

    # ─── Update State ───
    wins = sum(1 for s in settlements if s["win_loss"] == "WIN")
    losses = sum(1 for s in settlements if s["win_loss"] == "LOSS")
    total_resolved = len(settlements)
    total_pnl = sum(s["gross_pnl"] for s in settlements)
    total_slippage_pnl = sum(s["slippage_adjusted_pnl"] for s in settlements)

    state = {
        "total_shadow_events": len(events),
        "total_resolved": total_resolved,
        "total_wins": wins,
        "total_losses": losses,
        "total_pnl": round(total_pnl, 4),
        "total_slippage_adjusted_pnl": round(total_slippage_pnl, 4),
        "pending_events": len(pending_events),
        "error_events": len(error_events),
        "unrecoverable_events": len(unrecoverable_events),
        "timestamp": now.isoformat(),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    # ─── Build Audit ───
    audit = {
        "audit_ts": now.isoformat(),
        "events_total": len(events),
        "events_resolved": len(resolved_events),
        "events_pending": len(pending_events),
        "events_unrecoverable": len(unrecoverable_events),
        "events_with_settlement_errors": len(error_events),
        "resolved_details": [{"event_id": e["event_id"], "winner": e.get("_resolution", {}).get("winner", "?")}
                             for e in resolved_events],
        "pending_details": [{"event_id": e["event_id"], "reason": e.get("_reason", "")}
                            for e in pending_events],
        "error_details": [{"event_id": e["event_id"], "reason": e.get("_reason", ""),
                           "resolution": e.get("_resolution", {})}
                          for e in error_events],
        "unrecoverable_details": [{"event_id": e["event_id"], "reason": e.get("_reason", "")}
                                  for e in unrecoverable_events],
    }
    with open(AUDIT_FILE, "w") as f:
        json.dump(audit, f, indent=2)

    # ─── Evaluation ───
    print()
    if total_resolved >= 25:
        print("EVALUATION: Sufficient resolved events for evaluation")
        ev_per_trade = total_slippage_pnl / total_resolved if total_resolved > 0 else 0
        win_rate = wins / total_resolved if total_resolved > 0 else 0
        gross_profit = sum(s["slippage_adjusted_pnl"] for s in settlements if s["slippage_adjusted_pnl"] > 0)
        gross_loss = abs(sum(s["slippage_adjusted_pnl"] for s in settlements if s["slippage_adjusted_pnl"] < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Bucket performance
        bucket_perf = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
        for s in settlements:
            b = s.get("entry_bucket", "unknown")
            if s["win_loss"] == "WIN":
                bucket_perf[b]["wins"] += 1
            else:
                bucket_perf[b]["losses"] += 1
            bucket_perf[b]["pnl"] += s["slippage_adjusted_pnl"]

        # Interval performance
        interval_perf = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
        for s in settlements:
            iv = s.get("interval", "unknown")
            if s["win_loss"] == "WIN":
                interval_perf[iv]["wins"] += 1
            else:
                interval_perf[iv]["losses"] += 1
            interval_perf[iv]["pnl"] += s["slippage_adjusted_pnl"]

        # Velocity bucket performance
        vel_perf = {"negative_60s": {"wins": 0, "losses": 0, "pnl": 0.0},
                    "near_zero_60s": {"wins": 0, "losses": 0, "pnl": 0.0},
                    "positive_60s": {"wins": 0, "losses": 0, "pnl": 0.0}}
        for s in settlements:
            v60 = s.get("btc_velocity_60s", 0.0)
            if v60 < -0.02:
                bucket = "negative_60s"
            elif v60 < 0.02:
                bucket = "near_zero_60s"
            else:
                bucket = "positive_60s"
            if s["win_loss"] == "WIN":
                vel_perf[bucket]["wins"] += 1
            else:
                vel_perf[bucket]["losses"] += 1
            vel_perf[bucket]["pnl"] += s["slippage_adjusted_pnl"]

        # Max loss streak
        max_loss_streak = 0
        current_streak = 0
        for s in settlements:
            if s["win_loss"] == "LOSS":
                current_streak += 1
                max_loss_streak = max(max_loss_streak, current_streak)
            else:
                current_streak = 0

        if ev_per_trade > 0 and pf >= 1.25:
            classification = "STATE_GATE_BLINDNESS_CONFIRMED"
            promotion = "SPOT_MOMENTUM_SHADOW_VALIDATED"
            action = "Promote spot/perp-derived momentum classifier to paper-live validation"
        elif ev_per_trade <= 0 or pf < 1.0:
            classification = "CURRENT_STATE_GATE_PROTECTIVE"
            promotion = "SHADOW_MODEL_REJECTED"
            action = "Keep live model unchanged. Do not loosen momentum gate."
        else:
            classification = "INCONCLUSIVE"
            promotion = "HOLD"
            action = "Insufficient signal for either decision"

        evaluation = {
            "evaluation_ts": now.isoformat(),
            "resolved_count": total_resolved,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "gross_pnl": round(total_pnl, 4),
            "slippage_adjusted_pnl": round(total_slippage_pnl, 4),
            "realized_ev_per_trade": round(ev_per_trade, 4),
            "profit_factor": round(pf, 4),
            "max_loss_streak": max_loss_streak,
            "bucket_performance": {k: dict(v) for k, v in bucket_perf.items()},
            "interval_performance": {k: dict(v) for k, v in interval_perf.items()},
            "velocity_bucket_performance": vel_perf,
            "classification": classification,
            "promotion_recommendation": promotion,
            "action": action,
            "pending_events": len(pending_events),
            "error_events": len(error_events),
            "unrecoverable_events": len(unrecoverable_events),
        }
    else:
        print(f"EVALUATION: Only {total_resolved}/25 resolved events. HOLD.")
        evaluation = {
            "evaluation_ts": now.isoformat(),
            "resolved_count": total_resolved,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total_resolved, 4) if total_resolved > 0 else 0,
            "classification": "INSUFFICIENT_SHADOW_SAMPLE",
            "promotion_recommendation": "HOLD",
            "action": "Continue shadow tracker until >=25 resolved events",
            "pending_events": len(pending_events),
            "error_events": len(error_events),
            "unrecoverable_events": len(unrecoverable_events),
            "note": f"Only {total_resolved}/25 resolved. Cannot evaluate gate blindness.",
        }

    with open(EVAL_FILE, "w") as f:
        json.dump(evaluation, f, indent=2)

    print()
    print("=" * 70)
    print("SETTLEMENT REPLAY COMPLETE")
    print("=" * 70)
    print(f"Total events:        {len(events)}")
    print(f"Resolved:            {total_resolved}")
    print(f"Pending:             {len(pending_events)}")
    print(f"Errors:              {len(error_events)}")
    print(f"Unrecoverable:       {len(unrecoverable_events)}")
    print(f"Wins:                {wins}")
    print(f"Losses:              {losses}")
    if total_resolved > 0:
        print(f"Win rate:            {wins/total_resolved*100:.1f}%")
        print(f"Gross PnL:           ${total_pnl:.2f}")
        print(f"Slippage adj PnL:    ${total_slippage_pnl:.2f}")
        print(f"EV/trade:            ${total_slippage_pnl/total_resolved:.4f}")
        print(f"Classification:      {evaluation.get('classification', 'N/A')}")
        print(f"Promotion:           {evaluation.get('promotion_recommendation', 'N/A')}")
    print()
    print(f"Settlements file:    {SETTLEMENTS_FILE}")
    print(f"State file:          {STATE_FILE}")
    print(f"Evaluation file:     {EVAL_FILE}")
    print(f"Audit file:          {AUDIT_FILE}")


if __name__ == "__main__":
    run_replay()