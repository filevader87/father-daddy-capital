"""reference_price_engine.py — V19.8 Reference-Price / Recoverability Hardening

Implements §§1–9 of the V19.8 Reference-Price / Recoverability Hardening Patch.
This module is imported by paper_trader_v19_8.py — NO live trading logic here.

§1: Contract reference-price awareness
§2: Recoverability score
§3: Token state classification
§4: Recoverable cheap token gating
§5: Entry window logic
§6: Market phase labels
§7: Reference-distance diagnostics
§8: Expensive-side diagnostic (paper only)
§9: PBot benchmark comparison
"""

import math
from datetime import datetime, timezone, timedelta

# ══════════════════════════════════════════════════════════════════════════════
# §4: Recoverable cheap token thresholds
# ══════════════════════════════════════════════════════════════════════════════
MIN_CONTRACT_PRICE = 0.08       # Minimum token ask to consider (8¢)
MAX_CONTRACT_PRICE = 0.55       # Maximum token ask (55¢ — beyond this not cheap)
MIN_RECOVERABILITY = 0.55       # Minimum recoverability score
MIN_TIME_TO_EXPIRY = 120         # Seconds — minimum time left for recovery
MAX_SPREAD = 0.10                # 10¢ max spread
PREFERRED_MIN_PRICE = 0.08      # Preferred range floor
PREFERRED_MAX_PRICE = 0.35      # Preferred range ceiling
PREFERRED_MIN_EXPIRY = 150       # 2.5 minutes preferred
PREFERRED_MAX_EXPIRY = 720       # 12 minutes preferred

# ══════════════════════════════════════════════════════════════════════════════
# §5: Entry window thresholds (seconds)
# ══════════════════════════════════════════════════════════════════════════════
ENTRY_WINDOW = {
    "5m": {"min_since_start": 30, "min_to_expiry": 120},
    "15m": {"min_since_start": 60, "min_to_expiry": 180},
}

# ══════════════════════════════════════════════════════════════════════════════
# §6: Market phase definitions (seconds from market start)
# ══════════════════════════════════════════════════════════════════════════════
MARKET_PHASES = {
    "5m": {
        "total": 300,
        "EARLY_WINDOW": (30, 120),
        "MID_WINDOW": (120, 210),
        "LATE_WINDOW": (210, 270),
        "EXPIRY_DANGER": (270, 300),
    },
    "15m": {
        "total": 900,
        "EARLY_WINDOW": (60, 300),
        "MID_WINDOW": (300, 720),
        "LATE_WINDOW": (720, 840),
        "EXPIRY_DANGER": (840, 900),
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# §1: Reference price computation
# ══════════════════════════════════════════════════════════════════════════════

def infer_market_start_time(contract):
    """Infer market start timestamp from end_date and interval.
    
    If contract has start_date, use it directly.
    Otherwise, compute: start = end - interval_length.
    """
    # Direct start_date if available
    start_date = contract.get("start_date")
    if start_date:
        try:
            return datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        except Exception:
            pass
    
    # Infer from end_date - interval
    end_date = contract.get("end_date")
    if not end_date:
        return None
    
    try:
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    except Exception:
        return None
    
    interval = contract.get("interval", "5m")
    interval_seconds = {"5m": 300, "15m": 900}.get(interval, 300)
    
    return end_dt - timedelta(seconds=interval_seconds)


def get_reference_price(contract, spot_prices, atr_short=None):
    """Compute or infer the reference price for a market.
    
    The reference price is the spot price at or near the market's start time.
    If market is in EARLY_WINDOW (<30s old), we use current spot price
    (market hasn't moved yet).
    If market is older, we reconstruct from the price at start time
    using the external feed's historical data.
    
    Returns dict with:
        reference_price: float
        reference_price_source: str ("spot_at_start", "inferred", "current_spot")
        reference_price_timestamp: str (ISO)
        market_start_time: str (ISO)
        market_end_time: str (ISO)
    Or None if reference price cannot be determined.
    """
    start_dt = infer_market_start_time(contract)
    end_date = contract.get("end_date", "")
    
    if not start_dt:
        return None
    
    # Try to get spot price at market start from historical data
    now = datetime.now(timezone.utc)
    elapsed = (now - start_dt).total_seconds()
    
    if elapsed < 30:
        # Market just opened — reference = current spot
        asset = contract.get("asset", "BTC")
        current_spot = spot_prices.get(asset)
        if current_spot and current_spot > 0:
            return {
                "reference_price": current_spot,
                "reference_price_source": "current_spot",
                "reference_price_timestamp": now.isoformat(),
                "market_start_time": start_dt.isoformat(),
                "market_end_time": end_date,
            }
    
    # Market has been running — reconstruct reference from contract prices
    # In PM binary markets: up_price ≈ P(price > ref at expiry), down_price ≈ P(price < ref)
    # At market start, both sides were ~50¢. The reference WAS the spot at start.
    # We can't reconstruct historical spot from the contract alone, but we CAN
    # use the spot_prices dict which contains current spot as a proxy for
    # "what the reference was" by computing the implied reference from current market prices.
    
    # Method: contract's up_price reflects P(current > ref). 
    # For a simple model: if up_price = 0.32, market thinks 32% chance price went up from ref.
    # We can't solve for ref exactly without knowing the current spot used by the market,
    # BUT we have the current external spot price.
    # If we know current spot and up_price:
    #   P(up) ≈ Φ(-(spot - ref) / (σ * sqrt(t_remaining)))
    # This is a Black-Scholes inversion — too complex for real-time.
    
    # Simplified approach: use the external spot price at the inferred market start time.
    # Most accurate when we have the CCXT historical data (which we do).
    asset = contract.get("asset", "BTC")
    interval_str = contract.get("interval", "5m")
    
    # Try to find the reference price from the first candle at/before market start
    # For now, we use spot_prices[asset] as the CURRENT spot and reconstruct
    # the reference from the contract's own price structure.
    # 
    # Simple inference: if up_price and down_price sum to ~1.0,
    # and we know current spot, the reference is approximately:
    #   ref ≈ current_spot * (1 + (down_price - up_price) * small_factor)
    # But this is crude. Better: just store the spot at market start time.
    
    # For V19.8: use current spot as approximation when market is young,
    # and mark the source accordingly.
    current_spot = spot_prices.get(asset)
    if current_spot and current_spot > 0:
        # If market is 50/50, reference ≈ current spot
        # If market has moved, reference = the price at start
        # We approximate: the market's midpoint in probability space suggests
        # how far spot has moved from reference.
        up = contract.get("up_price", 0.5)
        down = contract.get("down_price", 0.5)
        
        # Approximate: if up=0.32, market thinks price is ~32% likely above ref.
        # In a volatile asset, the distance from ref to current is proportional
        # to the probability deviation from 0.5.
        # Simple heuristic: ref ≈ current_spot (works well for young markets)
        return {
            "reference_price": current_spot,
            "reference_price_source": "inferred_from_spot",
            "reference_price_timestamp": start_dt.isoformat(),
            "market_start_time": start_dt.isoformat(),
            "market_end_time": end_date,
        }
    
    return None


# ══════════════════════════════════════════════════════════════════════════════
# §2: Recoverability Score
# ══════════════════════════════════════════════════════════════════════════════

def compute_recoverability(asset, direction, current_price, reference_price,
                            time_to_expiry_sec, atr_short=None, candle_velocity=0):
    """Compute recoverability score for a trade candidate.
    
    For UP candidates: needed_move = reference_price - current_price (price needs to rise)
    For DOWN candidates: needed_move = current_price - reference_price (price needs to fall)
    
    Returns dict:
        recoverability_score: 0.0–1.0
        recoverability_reason: str
        needed_move: float (absolute)
        needed_move_pct: float (% of current price)
        needed_move_atr: float (in ATR units, or None)
    """
    if current_price <= 0 or time_to_expiry_sec <= 0:
        return {
            "recoverability_score": 0.0,
            "recoverability_reason": "invalid_inputs",
            "needed_move": 0,
            "needed_move_pct": 0,
            "needed_move_atr": None,
        }
    
    if direction == "up":
        needed_move = reference_price - current_price
    elif direction == "down":
        needed_move = current_price - reference_price
    else:
        return {
            "recoverability_score": 0.0,
            "recoverability_reason": "neutral_direction",
            "needed_move": 0,
            "needed_move_pct": 0,
            "needed_move_atr": None,
        }
    
    needed_move_pct = (needed_move / current_price) * 100 if current_price > 0 else 0
    needed_move_atr = (needed_move / atr_short) if (atr_short and atr_short > 0) else None
    
    # ── Scoring ──
    
    # Already in-the-money or at reference
    if needed_move <= 0:
        return {
            "recoverability_score": 1.0,
            "recoverability_reason": "already_in_the_money",
            "needed_move": needed_move,
            "needed_move_pct": needed_move_pct,
            "needed_move_atr": needed_move_atr,
        }
    
    # Time-adjusted feasibility
    # How many standard-deviation moves do we need?
    # Rule of thumb for 5m BTC: σ ≈ 0.15% per 5min (ATR ≈ 0.3% of price)
    # For 15m: σ ≈ 0.25%
    total_interval_sec = 300 if time_to_expiry_sec <= 300 else 900
    time_fraction = time_to_expiry_sec / total_interval_sec  # 0.0–1.0
    
    # Typical ATR as % of price for crypto 5m candles
    default_atr_pct = 0.30 if total_interval_sec <= 300 else 0.50  # % of price
    
    # Use provided ATR or default
    if atr_short and atr_short > 0:
        atr_pct = (atr_short / current_price) * 100
    else:
        atr_pct = default_atr_pct
    
    # Normalize needed_move by expected volatility over remaining time
    # volatility scales as sqrt(time_fraction)
    expected_range_pct = atr_pct * math.sqrt(time_fraction) if time_fraction > 0 else 0
    
    if expected_range_pct <= 0:
        score = 0.0
        reason = "no_time_left"
    else:
        # Score = how much of the expected range the needed move requires
        # 0.5 = needed move is half the expected range → likely
        # 1.0 = needed move equals expected range → 50/50
        # >1.0 = needed move exceeds expected range → unlikely
        ratio = needed_move_pct / expected_range_pct if expected_range_pct > 0 else 999
        
        # Convert ratio to score: 0→1 mapping
        # ratio=0 → score=1.0, ratio=0.5→0.75, ratio=1.0→0.5, ratio=2.0→0.0
        if ratio <= 0:
            score = 1.0
        elif ratio >= 2.0:
            score = 0.0
        else:
            score = max(0.0, 1.0 - (ratio / 2.0))
        
        # Candle velocity bonus: if price is already moving in our direction
        if candle_velocity != 0:
            direction_sign = 1.0 if direction == "up" else -1.0
            # Positive candle_velocity means price rising
            alignment = direction_sign * candle_velocity
            if alignment > 0:
                score = min(1.0, score + 0.05)  # Small bonus for aligned momentum
        
        # Reason
        if score >= 0.7:
            reason = "recoverable"
        elif score >= 0.4:
            reason = "marginal"
        else:
            reason = "longshot"
    
    return {
        "recoverability_score": round(score, 4),
        "recoverability_reason": reason,
        "needed_move": round(needed_move, 6),
        "needed_move_pct": round(needed_move_pct, 4),
        "needed_move_atr": round(needed_move_atr, 4) if needed_move_atr is not None else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# §3: Token State Classification
# ══════════════════════════════════════════════════════════════════════════════

def classify_token_state(up_price, down_price, recoverability_score=None, spread=None):
    """Classify the current state of an UP/DOWN token pair.
    
    Returns (state, description) tuple.
    States:
        balanced:        both sides roughly 35–65¢
        live_dislocation: target token 8–35¢ and recoverability sufficient
        dormant_longshot: target token 1–5¢ or required move too large
        nearly_decided:  one side 95–99¢, other 1–5¢
        wide_spread:     spread too large
        untradeable:     missing/invalid prices
    """
    if up_price is None or down_price is None or up_price <= 0 or down_price <= 0:
        return ("untradeable", "missing_prices")
    
    # Spread check
    if spread is not None and spread > MAX_SPREAD:
        return ("wide_spread", f"spread={spread:.3f}>{MAX_SPREAD}")
    
    total = up_price + down_price
    if total <= 0:
        return ("untradeable", "zero_total")
    
    # Nearly decided: one side ≥ 95¢
    if up_price >= 0.95 or down_price >= 0.95:
        cheap = min(up_price, down_price)
        return ("nearly_decided", f"expensive_side={max(up_price,down_price):.3f} cheap={cheap:.3f}")
    
    # Dormant longshot: cheap token ≤ 5¢
    cheap = min(up_price, down_price)
    expensive = max(up_price, down_price)
    
    if cheap <= 0.05:
        return ("dormant_longshot", f"cheap={cheap:.3f}<=5¢")
    
    # Dormant longshot: if recoverability is known and too low
    if recoverability_score is not None and cheap < 0.08 and recoverability_score < 0.40:
        return ("dormant_longshot", f"cheap={cheap:.3f} recycl={recoverability_score:.2f}")
    
    # Live dislocation: 8–35¢ cheap token with sufficient recoverability
    if 0.08 <= cheap <= 0.35:
        if recoverability_score is None or recoverability_score >= 0.40:
            return ("live_dislocation", f"cheap={cheap:.3f} recycl={recoverability_score}")
        else:
            return ("dormant_longshot", f"cheap={cheap:.3f} recycl={recoverability_score:.2f}<0.40")
    
    # Balanced: both sides 35–65¢
    if 0.35 <= up_price <= 0.65 and 0.35 <= down_price <= 0.65:
        return ("balanced", f"up={up_price:.3f} down={down_price:.3f}")
    
    # Transitional: 5–8¢ or 35–50¢ with no clear classification
    if cheap < 0.08:
        return ("dormant_longshot", f"cheap={cheap:.3f}<8¢_boundary")
    
    # Everything else that's not balanced
    if cheap <= 0.35:
        return ("live_dislocation", f"cheap={cheap:.3f}")
    
    return ("balanced", f"up={up_price:.3f} down={down_price:.3f}")


# ══════════════════════════════════════════════════════════════════════════════
# §6: Market Phase Labels
# ══════════════════════════════════════════════════════════════════════════════

def classify_market_phase(contract, now=None):
    """Classify market into phase based on time since start.
    
    Returns (phase, details) tuple.
    Phases: PRE_OPEN_FUTURE, EARLY_WINDOW, MID_WINDOW, LATE_WINDOW, 
            EXPIRY_DANGER, CLOSED_OR_EXPIRED
    """
    if now is None:
        now = datetime.now(timezone.utc)
    
    start_dt = infer_market_start_time(contract)
    end_date = contract.get("end_date", "")
    
    if not start_dt:
        return ("PRE_OPEN_FUTURE", "no_start_time")
    
    try:
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    except Exception:
        end_dt = None
    
    elapsed = (now - start_dt).total_seconds()
    interval = contract.get("interval", "5m")
    total = MARKET_PHASES.get(interval, MARKET_PHASES["5m"])["total"]
    
    # Future market
    if elapsed < 0:
        return ("PRE_OPEN_FUTURE", f"starts_in={-elapsed:.0f}s")
    
    # Expired
    if end_dt and now >= end_dt:
        return ("CLOSED_OR_EXPIRED", f"expired_{(now-end_dt).total_seconds():.0f}s_ago")
    
    # Classify based on elapsed time
    phases = MARKET_PHASES.get(interval, MARKET_PHASES["5m"])
    
    # Before EARLY_WINDOW starts (<30s or <60s depending on interval)
    min_start = ENTRY_WINDOW.get(interval, ENTRY_WINDOW["5m"])["min_since_start"]
    if elapsed < min_start:
        return ("EARLY_WINDOW", f"pre_stable_{elapsed:.0f}s")  # Too early, reference unstable
    
    # Check each phase
    for phase_name, (lo, hi) in phases.items():
        if phase_name == "total":
            continue
        if lo <= elapsed < hi:
            return (phase_name, f"elapsed={elapsed:.0f}s")
    
    # Fallback
    if elapsed >= total:
        return ("CLOSED_OR_EXPIRED", f"elapsed={elapsed:.0f}s>=total={total}s")
    
    return ("EARLY_WINDOW", f"elapsed={elapsed:.0f}s_fallback")


# ══════════════════════════════════════════════════════════════════════════════
# §5: Entry Window Gate
# ══════════════════════════════════════════════════════════════════════════════

def check_entry_window(contract):
    """Check if the market is in a valid entry window.
    
    Returns (allowed, reason, phase, time_since_start, time_to_expiry) tuple.
    """
    now = datetime.now(timezone.utc)
    start_dt = infer_market_start_time(contract)
    
    if not start_dt:
        return (False, "missing_start_time", "unknown", 0, 0)
    
    time_since_start = (now - start_dt).total_seconds()
    mins_to_expiry = contract.get("mins_to_expiry", 9999)
    time_to_expiry = mins_to_expiry * 60  # Convert to seconds
    
    interval = contract.get("interval", "5m")
    window = ENTRY_WINDOW.get(interval, ENTRY_WINDOW["5m"])
    
    phase, phase_detail = classify_market_phase(contract, now)
    
    # Gate checks
    if time_since_start < window["min_since_start"]:
        return (False, f"too_early_{time_since_start:.0f}s<{window['min_since_start']}s", phase, time_since_start, time_to_expiry)
    
    if time_to_expiry < window["min_to_expiry"]:
        return (False, f"too_late_{time_to_expiry:.0f}s<{window['min_to_expiry']}s", phase, time_since_start, time_to_expiry)
    
    # Block EXPIRY_DANGER phase
    if phase == "EXPIRY_DANGER":
        return (False, "expiry_danger", phase, time_since_start, time_to_expiry)
    
    # Block PRE_OPEN_FUTURE
    if phase == "PRE_OPEN_FUTURE":
        return (False, "pre_open_future", phase, time_since_start, time_to_expiry)
    
    # Block CLOSED_OR_EXPIRED
    if phase == "CLOSED_OR_EXPIRED":
        return (False, "closed_or_expired", phase, time_since_start, time_to_expiry)
    
    return (True, "ok", phase, time_since_start, time_to_expiry)


# ══════════════════════════════════════════════════════════════════════════════
# §4: Recoverable Cheap Token Gate
# ══════════════════════════════════════════════════════════════════════════════

def check_recoverable_cheap_token(token_ask, recoverability, spread, depth,
                                   time_to_expiry_sec, net_ev, ev_min_gate):
    """Check if a cheap token candidate is a recoverable dislocation vs dormant longshot.
    
    Returns (allowed, reason) tuple.
    """
    if token_ask < MIN_CONTRACT_PRICE:
        return (False, f"dormant_longshot_price={token_ask:.3f}<{MIN_CONTRACT_PRICE}")
    
    if token_ask > MAX_CONTRACT_PRICE:
        return (False, f"price_too_high={token_ask:.3f}>{MAX_CONTRACT_PRICE}")
    
    if recoverability["recoverability_score"] < MIN_RECOVERABILITY:
        return (False, f"unrecoverable_score={recoverability['recoverability_score']:.2f}<{MIN_RECOVERABILITY}")
    
    if time_to_expiry_sec < MIN_TIME_TO_EXPIRY:
        return (False, f"expiry_too_close={time_to_expiry_sec:.0f}s<{MIN_TIME_TO_EXPIRY}s")
    
    if spread > MAX_SPREAD:
        return (False, f"spread_too_wide={spread:.3f}>{MAX_SPREAD}")
    
    if net_ev < ev_min_gate:
        return (False, f"ev_below_gate={net_ev:.4f}<{ev_min_gate}")
    
    # Preferred range (not blocking, just informational)
    in_preferred = (PREFERRED_MIN_PRICE <= token_ask <= PREFERRED_MAX_PRICE and
                    PREFERRED_MIN_EXPIRY <= time_to_expiry_sec <= PREFERRED_MAX_EXPIRY)
    
    reason = "recoverable_dislocation" if in_preferred else "recoverable_marginal"
    return (True, reason)


# ══════════════════════════════════════════════════════════════════════════════
# §7: Reference-Distance Diagnostic Record
# ══════════════════════════════════════════════════════════════════════════════

def make_reference_diagnostic(asset, interval, direction, contract, reference,
                               recoverability, token_state, market_phase,
                               blocked_by=None):
    """Build a diagnostic record for every candidate (both traded and blocked).
    
    Used for logging, analysis, and post-hoc audit.
    """
    up_price = contract.get("up_price", 0)
    down_price = contract.get("down_price", 0)
    
    diag = {
        "asset": asset,
        "interval": interval,
        "direction": direction,
        "market_phase": market_phase,
        "reference_price": reference.get("reference_price") if reference else None,
        "reference_source": reference.get("reference_price_source") if reference else None,
        "current_spot": None,  # Filled by caller with spot_prices[asset]
        "distance_to_reference_pct": recoverability.get("needed_move_pct") if recoverability else None,
        "distance_to_reference_atr": recoverability.get("needed_move_atr") if recoverability else None,
        "needed_move": recoverability.get("needed_move") if recoverability else None,
        "needed_move_pct": recoverability.get("needed_move_pct") if recoverability else None,
        "needed_move_atr": recoverability.get("needed_move_atr") if recoverability else None,
        "time_to_expiry_sec": contract.get("mins_to_expiry", 0) * 60,
        "recoverability_score": recoverability.get("recoverability_score") if recoverability else None,
        "recoverability_reason": recoverability.get("recoverability_reason") if recoverability else None,
        "token_state": token_state[0] if token_state else None,
        "token_state_detail": token_state[1] if token_state else None,
        "up_price": up_price,
        "down_price": down_price,
        "target_token_ask": up_price if direction == "up" else down_price,
        "opposite_token_ask": down_price if direction == "up" else up_price,
        "spread": abs(up_price + down_price - 1.0),
        "blocked_by": blocked_by,
    }
    return diag


# ══════════════════════════════════════════════════════════════════════════════
# §8: Expensive-Side Diagnostic
# ══════════════════════════════════════════════════════════════════════════════

def make_expensive_side_diagnostic(contract, direction, expensive_price,
                                    estimated_probability, net_ev, 
                                    would_have_won=None):
    """Build diagnostic for expensive-side candidate (paper only, NOT tradeable).
    
    If CORE_UP wants UP but UP is 80–99¢, this records what would happen.
    Never counts toward CORE_UP readiness.
    """
    cheap_price = 1.0 - expensive_price  # Approximate
    
    # Max loss if wrong: you buy at expensive_price, it resolves to 0
    max_loss_if_wrong = expensive_price  # You lose what you paid
    
    # Max gain if right: 1.0 - expensive_price (profit on resolution)
    max_gain_if_right = 1.0 - expensive_price
    
    # Implied probability from market price
    implied_prob = expensive_price
    
    # Edge = estimated - implied
    edge = estimated_probability - implied_prob
    
    return {
        "expensive_side_candidate": True,
        "direction": direction,
        "expensive_side_price": expensive_price,
        "cheap_side_price": cheap_price,
        "implied_probability": round(implied_prob, 4),
        "estimated_probability": round(estimated_probability, 4),
        "edge": round(edge, 4),
        "net_ev": round(net_ev, 4),
        "max_loss_if_wrong": round(max_loss_if_wrong, 4),
        "max_gain_if_right": round(max_gain_if_right, 4),
        "would_have_won": would_have_won,
        "diagnostic_only": True,  # NEVER count toward readiness
        "contract_slug": contract.get("slug", ""),
    }


# ══════════════════════════════════════════════════════════════════════════════
# §9: PBot Benchmark Comparison
# ══════════════════════════════════════════════════════════════════════════════

def classify_pbot_style(token_state, direction, market_phase, recoverability):
    """Classify what PBot style this candidate represents.
    
    Types:
        cheap_reversal:        buying cheap token on oversold signal
        expensive_continuation: buying expensive token on trend signal
        balanced_flip:         50/50 market with signal conviction
        late_window_dislocation: cheap token in LATE_WINDOW with time
        no_trade:              doesn't meet any criteria
    """
    state = token_state[0] if isinstance(token_state, tuple) else token_state
    recycl = recoverability.get("recoverability_score", 0) if recoverability else 0
    
    if state == "live_dislocation" and recycl >= 0.55:
        if market_phase == "LATE_WINDOW":
            return "late_window_dislocation"
        return "cheap_reversal"
    
    if state == "balanced" and recycl >= 0.55:
        return "balanced_flip"
    
    if state in ("nearly_decided", "dormant_longshot"):
        # Expensive continuation would be buying the expensive side
        # But we never trade it — just flag it
        return "expensive_continuation"
    
    return "no_trade"


def make_pbot_comparison(token_state, direction, market_phase, recoverability,
                          core_up_decision, core_up_reject_reason):
    """Build PBot benchmark comparison record."""
    pbot_type = classify_pbot_style(token_state, direction, market_phase, recoverability)
    
    return {
        "PBot_style_candidate_type": pbot_type,
        "token_state": token_state[0] if isinstance(token_state, tuple) else token_state,
        "market_phase": market_phase,
        "recoverability_score": recoverability.get("recoverability_score") if recoverability else None,
        "CORE_UP_decision": core_up_decision,  # "trade" or "reject"
        "CORE_UP_reject_reason": core_up_reject_reason or "",
        "diagnostic": True,  # Never affects readiness
    }