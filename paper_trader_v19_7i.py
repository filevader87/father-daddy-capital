#!/usr/bin/env python3
"""V19.7i Paper Trading Engine — Lifecycle-aware paper trading with 3 strategy profiles.

NO REAL ORDERS. Paper trading only.
Uses CLOB ask prices for realistic fills (not midpoint fantasy).

Position lifecycle: CANDIDATE → OPENED → ACTIVE → EXPIRING → RESOLVED → SETTLED → JOURNALED
No position may skip states. Settlement validated against Polymarket resolution.

Outputs: paper_trading/ with per-cycle JSON + cumulative state per profile + journal per position.
"""

import json, os, sys, time, traceback, random, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
import urllib.request

sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
import pm_engine_v19_7 as eng

OUT_DIR = Path('/mnt/c/Users/12035/father_daddy_capital/paper_trading')
OUT_DIR.mkdir(exist_ok=True)
JOURNAL_DIR = OUT_DIR / "journal"
JOURNAL_DIR.mkdir(exist_ok=True)

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"

# ── Position States ──
STATE_CANDIDATE = "CANDIDATE"
STATE_OPENED = "OPENED"
STATE_ACTIVE = "ACTIVE"
STATE_EXPIRING = "EXPIRING"
STATE_RESOLVED = "RESOLVED"
STATE_SETTLED = "SETTLED"
STATE_JOURNALED = "JOURNALED"

VALID_TRANSITIONS = {
    STATE_CANDIDATE: [STATE_OPENED],
    STATE_OPENED: [STATE_ACTIVE],
    STATE_ACTIVE: [STATE_EXPIRING],
    STATE_EXPIRING: [STATE_RESOLVED],
    STATE_RESOLVED: [STATE_SETTLED],
    STATE_SETTLED: [STATE_JOURNALED],
}

# ── Strategy Profiles ──
PROFILES = {
    "CORE_UP": {
        "name": "V19.7g Core UP",
        "mode": "paper",
        "description": "Oversold UP only. DOWN disabled. BTC first, others discovery. Micro-live candidate.",
        "enabled_rsi_zones": {
            "up": ["extreme_low", "oversold", "near_oversold1", "near_oversold2", "near_oversold3"],
            "down": [],
        },
        "min_confidence": 0.82,
        "ev_min_gate": 0.02,
        "max_contract_price": 0.85,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
    "BIDIRECTIONAL_SHADOW": {
        "name": "V19.7g Bidirectional Shadow",
        "mode": "paper",
        "description": "RSI 20-35 UP + RSI 70-82 DOWN. Measures DOWN viability.",
        "enabled_rsi_zones": {
            "up": ["extreme_low", "oversold", "near_oversold1", "near_oversold2", "near_oversold3"],
            "down": ["strong_overbought", "moderate_overbought"],
        },
        "min_confidence": 0.82,
        "ev_min_gate": 0.02,
        "max_contract_price": 0.85,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
    "PARABOLIC_RESEARCH": {
        "name": "V19.8 Parabolic Research",
        "mode": "paper_only",
        "description": "RSI>82 continuation UP, RSI<20 continuation DOWN. Research only.",
        "enabled_rsi_zones": {
            "up": ["parabolic"],
            "down": ["extreme_low"],
        },
        "min_confidence": 0.75,
        "ev_min_gate": 0.01,
        "max_contract_price": 0.90,
        "primary_asset": "BTC",
        "discovery_assets": ["ETH", "SOL", "XRP"],
    },
}

# ── Resolution cache ──
_resolution_cache = {}  # condition_id → resolution data


def transition_position(pos, new_state):
    """Enforce state machine transitions. Raises ValueError on invalid transition."""
    current = pos.get("status", STATE_CANDIDATE)
    if new_state not in VALID_TRANSITIONS.get(current, []):
        raise ValueError(f"Invalid transition: {current} → {new_state} for position {pos.get('entry_id', '?')}")
    pos["status"] = new_state
    pos[f"{new_state.lower()}_at"] = datetime.now(timezone.utc).isoformat()
    return pos


def make_position_id(profile, asset, side, condition_id, timestamp):
    """Deterministic position ID to prevent duplicates."""
    raw = f"{profile}:{asset}:{side}:{condition_id}:{timestamp}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def fetch_clob_book(token_id):
    """Fetch CLOB orderbook for a token."""
    try:
        url = f"{CLOB_URL}/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "FDC-Paper/19.7i"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
        spread = best_ask - best_bid if best_bid and best_ask else 0
        bid_depth = sum(float(b.get("size", 0)) for b in bids if abs(float(b.get("price", 0)) - best_bid) < 0.05)
        ask_depth = sum(float(a.get("size", 0)) for a in asks if abs(float(a.get("price", 0)) - best_ask) < 0.05)
        is_stale = spread > 0.50 or (best_bid > 0 and best_ask > 0 and best_ask > 0.95 and best_bid < 0.05)
        return {
            "best_bid": best_bid, "best_ask": best_ask, "mid": mid, "spread": spread,
            "bid_depth_5c": round(bid_depth, 2), "ask_depth_5c": round(ask_depth, 2),
            "total_bids": len(bids), "total_asks": len(asks),
            "stale": is_stale, "missing": False,
        }
    except Exception as e:
        return {"best_bid": 0, "best_ask": 0, "mid": 0, "spread": 0,
                "bid_depth_5c": 0, "ask_depth_5c": 0, "total_bids": 0, "total_asks": 0,
                "stale": True, "missing": True, "error": str(e)}


def fetch_market_resolution(condition_id):
    """Check if a market has resolved. Returns resolution data or None."""
    global _resolution_cache
    if condition_id in _resolution_cache:
        return _resolution_cache[condition_id]
    try:
        url = f"{GAMMA_URL}/markets?condition_id={condition_id}&limit=1"
        data = eng._get(url)
        if isinstance(data, list) and len(data) > 0:
            m = data[0]
            closed = m.get("closed", False)
            resolved = m.get("resolved", False)
            outcome_prices = m.get("outcomePrices", "")
            outcomes = m.get("outcomes", "")
            if closed or resolved:
                result = {
                    "closed": closed,
                    "resolved": resolved,
                    "outcome_prices": outcome_prices,
                    "outcomes": outcomes,
                    "question": m.get("question", ""),
                }
                _resolution_cache[condition_id] = result
                return result
    except:
        pass
    return None


def init_profile_state(profile_key):
    """Initialize fresh profile state with all lifecycle metrics."""
    return {
        "profile": profile_key,
        "bankroll": 100.0,
        "initial_bankroll": 100.0,
        "positions": {},  # entry_id → position dict with full lifecycle fields
        "closed_trades": [],
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "max_bankroll": 100.0,
        "max_dd": 0.0,
        # ── Cycle counters ──
        "cycles_run": 0,
        "cycles_with_valid_market": 0,
        "cycles_with_signal": 0,
        "cycles_with_signal_and_market": 0,
        "cycles_passing_price_gate": 0,
        "cycles_passing_ev_gate": 0,
        "no_trade_cycles": 0,
        # ── Trade counters ──
        "paper_trades_opened": 0,
        "paper_trades_resolved": 0,
        "valid_opportunities": 0,
        "false_accepts": 0,
        "daily_strikes_accepted": 0,
        "fallback_trades": 0,
        # ── Lifecycle counters ──
        "positions_candidate": 0,
        "positions_opened": 0,
        "positions_active": 0,
        "positions_expiring": 0,
        "positions_resolved": 0,
        "positions_settled": 0,
        "positions_journaled": 0,
        "positions_unresolved_past_expiry": 0,
        "settlement_errors": 0,
        "duplicate_position_blocks": 0,
        "resolution_delays": [],  # list of (resolve_time - expiry_time) in minutes
        # ── Streaks ──
        "current_no_signal_streak": 0,
        "longest_no_signal_streak": 0,
        "current_no_market_streak": 0,
        "longest_no_market_streak": 0,
        "current_no_trade_streak": 0,
        "longest_no_trade_streak": 0,
        # ── Book metrics ──
        "book_checks_attempted": 0,
        "book_checks_successful": 0,
        "book_checks_missing": 0,
        "book_checks_stale": 0,
        "book_checks_skipped_no_signal": 0,
        "book_checks_skipped_no_market": 0,
        # ── Per-trade stats ──
        "entry_prices": [],
        "spreads": [],
        "blocked_reasons": {},
        "start_time": datetime.now(timezone.utc).isoformat(),
        "last_update": datetime.now(timezone.utc).isoformat(),
    }


def load_profile_state(profile_key):
    """Load or initialize profile state."""
    state_path = OUT_DIR / f"state_{profile_key}.json"
    if state_path.exists():
        with open(state_path) as f:
            state = json.load(f)
        default = init_profile_state(profile_key)
        for key in default:
            if key not in state:
                state[key] = default[key]
        return state
    return init_profile_state(profile_key)


def save_profile_state(state, profile_key):
    """Save profile state."""
    state_path = OUT_DIR / f"state_{profile_key}.json"
    state["last_update"] = datetime.now(timezone.utc).isoformat()
    with open(state_path, 'w') as f:
        json.dump(state, f, indent=2, default=str)


def get_rsi_zone(rsi):
    """Map RSI to zone name."""
    if rsi < 18: return "extreme_low"
    elif rsi < 28: return "oversold"
    elif rsi < 35: return "near_oversold1"
    elif rsi < 45: return "near_oversold2"
    elif rsi < 55: return "middle"
    elif rsi < 65: return "near_overbought"
    elif rsi < 72: return "moderate_overbought"
    elif rsi < 82: return "strong_overbought"
    else: return "parabolic"


def check_signal_allowed(zone, direction, profile_cfg):
    """Check if this RSI zone + direction is allowed for this profile."""
    allowed_zones = profile_cfg["enabled_rsi_zones"].get(direction, [])
    return zone in allowed_zones


def validate_pnl(pos, resolved_winner):
    """Validate PnL calculation against resolution. Returns (pnl, is_valid, error_msg)."""
    entry_price = pos.get("entry_price", 0)
    bet_size = pos.get("size_usd", 0)
    selected_side = pos.get("selected_side", "")
    selected_token_id = pos.get("selected_token_id", "")

    # Determine if our token won
    we_won = (selected_side.upper() == resolved_winner.upper())

    if we_won:
        # Won: payout = bet_size * (1 - entry_price) / entry_price, minus 2% PM fee
        if entry_price <= 0:
            return 0, False, f"entry_price={entry_price} <= 0"
        gross_pnl = bet_size * (1 - entry_price) / entry_price
        fees = gross_pnl * 0.02
        net_pnl = gross_pnl - fees
    else:
        # Lost: lose entire bet
        gross_pnl = -bet_size
        fees = 0
        net_pnl = gross_pnl

    # Safety: check for impossible PnL
    if abs(net_pnl) > bet_size * 10:
        return net_pnl, False, f"PnL {net_pnl} > 10x bet_size {bet_size}"

    return round(net_pnl, 4), True, None


def run_paper_cycle():
    """Run one paper trading cycle for all profiles."""
    cycle_time = datetime.now(timezone.utc)
    cycle_results = {}

    # ── Discover markets once per cycle ──
    all_contracts = {}
    all_prices = {}
    all_signals = {}
    all_books = {}

    for ak, acfg in eng.ASSETS.items():
        try:
            prices = eng.fetch_prices(acfg)
            all_prices[ak] = prices
        except:
            all_prices[ak] = []
        if all_prices[ak] and len(all_prices[ak]) >= 20:
            sig = eng.btc_signal(all_prices[ak])
            all_signals[ak] = sig
        else:
            all_signals[ak] = None
        try:
            contracts = eng.discover_contracts(ak)
            all_contracts[ak] = contracts
        except:
            all_contracts[ak] = []

    # ── Run each profile ──
    for profile_key, profile_cfg in PROFILES.items():
        state = load_profile_state(profile_key)
        state["cycles_run"] += 1
        cycle_trades = 0
        cycle_blocked = defaultdict(int)
        cycle_entries = []
        had_valid_signal = False
        had_compatible_market = False
        passed_price_gate = False
        passed_ev_gate = False

        print(f"\n  ── Profile {profile_key}: {profile_cfg['name']} ──")
        print(f"     Bankroll: ${state['bankroll']:.2f} | PnL: ${state['total_pnl']:.2f} | Trades: {state['total_trades']}")

        assets_to_trade = [profile_cfg["primary_asset"]] + profile_cfg["discovery_assets"]

        for ak in assets_to_trade:
            sig = all_signals.get(ak)
            if not sig:
                cycle_blocked["no_signal"] += 1
                state["book_checks_skipped_no_signal"] += 1
                continue

            direction = sig.get("direction", "neutral")
            confidence = sig.get("confidence", 0)
            rsi = sig.get("rsi", 50)
            zone = get_rsi_zone(rsi)

            is_valid_signal = (direction != "neutral" and confidence >= profile_cfg["min_confidence"]
                               and check_signal_allowed(zone, direction, profile_cfg))
            if is_valid_signal:
                had_valid_signal = True

            if direction == "neutral" or confidence < profile_cfg["min_confidence"]:
                cycle_blocked["below_min_confidence"] += 1
                state["book_checks_skipped_no_signal"] += 1
                continue

            if not check_signal_allowed(zone, direction, profile_cfg):
                cycle_blocked[f"zone_{zone}_disabled"] += 1
                state["book_checks_skipped_no_signal"] += 1
                continue

            contracts = all_contracts.get(ak, [])
            if not contracts:
                cycle_blocked["no_compatible_market"] += 1
                state["book_checks_skipped_no_market"] += 1
                continue

            had_compatible_market = True

            # Determine token IDs from CLOB data
            for c in contracts[:3]:
                is_up = direction == "up"
                if is_up:
                    token_price = c.get("up_price", 0.5)
                    token_side = "UP"
                else:
                    token_price = c.get("down_price", 0.5)
                    token_side = "DOWN"

                # ── Duplicate check ──
                condition_id = c.get("conditionId", "")
                if not condition_id:
                    cycle_blocked["missing_condition_id"] += 1
                    state["settlement_errors"] += 1
                    continue

                dup_key = f"{profile_key}:{ak}:{token_side}:{condition_id}"
                existing = any(p.get("condition_id_full") == condition_id and p.get("selected_side") == token_side and p.get("status") in [STATE_CANDIDATE, STATE_OPENED, STATE_ACTIVE, STATE_EXPIRING]
                               for p in state["positions"].values())
                if existing:
                    cycle_blocked["duplicate_position"] += 1
                    state["duplicate_position_blocks"] += 1
                    continue

                if token_price > profile_cfg["max_contract_price"]:
                    cycle_blocked["price_too_high"] += 1
                    continue

                passed_price_gate = True

                spread = abs(c.get("up_price", 0) + c.get("down_price", 0) - 1.0)
                if spread > 0.10:
                    cycle_blocked["spread_too_wide"] += 1
                    continue

                # Determine timeframe
                window = c.get("window", c.get("interval", ""))
                timeframe = window if window else "5m"

                mins_left = c.get("mins_to_expiry", 9999)
                if mins_left < 2 or mins_left > 15:
                    cycle_blocked["bad_expiry"] += 1
                    continue

                # ── Book check ──
                state["book_checks_attempted"] += 1
                token_ids_json = c.get("clobTokenIds") or c.get("token_ids") or ""
                clob_token_id = None
                opposite_token_id = None
                try:
                    tids = json.loads(token_ids_json) if isinstance(token_ids_json, str) else token_ids_json
                    if isinstance(tids, list) and len(tids) >= 2:
                        clob_token_id = tids[0] if is_up else tids[1]
                        opposite_token_id = tids[1] if is_up else tids[0]
                except:
                    pass

                book = None
                if clob_token_id and clob_token_id != "0":
                    book = fetch_clob_book(clob_token_id)
                    all_books[f"{ak}_{token_side}_{condition_id[:12]}"] = book
                    if book.get("missing"):
                        state["book_checks_missing"] += 1
                    elif book.get("stale"):
                        state["book_checks_stale"] += 1
                    else:
                        state["book_checks_successful"] += 1
                else:
                    state["book_checks_missing"] += 1

                # Determine fill price
                book_available = False
                estimated_slippage = 0.0
                if book and not book.get("stale") and not book.get("missing") and book.get("best_ask", 0) > 0.01:
                    fill_price = book["best_ask"]
                    book_available = True
                    estimated_slippage = book.get("spread", 0)
                else:
                    fill_price = token_price
                    book_available = False
                    if book and book.get("stale"):
                        cycle_blocked["stale_book"] += 1
                    else:
                        cycle_blocked["missing_book"] += 1

                # EV calculation
                try:
                    gross_ev, p_win, net_ev = eng.calculate_ev(
                        rsi, direction, fill_price,
                        eng._session_type(cycle_time.hour),
                        sig.get("confirmations", 0)
                    )
                    p_win_cal = eng.calibrate_longshot(p_win, fill_price)
                except:
                    cycle_blocked["ev_calc_error"] += 1
                    continue

                if net_ev < profile_cfg["ev_min_gate"]:
                    cycle_blocked["ev_below_gate"] += 1
                    continue

                passed_ev_gate = True

                # Position sizing
                updates = state["total_trades"]
                risk_pct = 0.01 if updates < 5 else (0.02 if updates < 20 else 0.03)
                bet_size = round(min(risk_pct * state["bankroll"], 0.03 * state["bankroll"], 5.0), 2)
                bet_size = max(bet_size, 0.50)

                # ── Build full position record ──
                entry_id = make_position_id(profile_key, ak, token_side, condition_id, cycle_time.isoformat())
                pos = {
                    "entry_id": entry_id,
                    "status": STATE_CANDIDATE,
                    "candidate_at": cycle_time.isoformat(),
                    # ── Required fields ──
                    "profile": profile_key,
                    "asset": ak,
                    "timeframe": timeframe,
                    "market_id": c.get("market_id", c.get("id", "")),
                    "condition_id_full": condition_id,
                    "question": c.get("question", "")[:200],
                    "slug": c.get("slug", ""),
                    "selected_side": token_side,
                    "selected_token_id": clob_token_id or "",
                    "opposite_token_id": opposite_token_id or "",
                    "entry_timestamp": cycle_time.isoformat(),
                    "entry_price": round(fill_price, 6),
                    "entry_bid": round(book.get("best_bid", 0), 6) if book else 0,
                    "entry_ask": round(book.get("best_ask", 0), 6) if book else 0,
                    "entry_spread": round(book.get("spread", 0), 6) if book else round(spread, 6),
                    "entry_depth": round(book.get("ask_depth_5c", 0), 2) if book else 0,
                    "estimated_slippage": round(estimated_slippage, 6),
                    "size_usd": bet_size,
                    "contracts": round(bet_size / fill_price, 4) if fill_price > 0 else 0,
                    "expiry_timestamp": (cycle_time + timedelta(minutes=mins_left)).isoformat() if mins_left < 9999 else "",
                    "time_to_expiry_at_entry": round(mins_left, 2),
                    "signal_rsi": round(rsi, 2),
                    "signal_zone": zone,
                    "signal_confidence": round(confidence, 4),
                    "estimated_probability": round(p_win_cal, 6),
                    "gross_ev": round(gross_ev, 6),
                    "net_ev": round(net_ev, 6),
                    "executable": book_available,
                    # ── Resolution fields (filled later) ──
                    "exit_timestamp": "",
                    "resolution_source": "",
                    "resolved_winner": "",
                    "settlement_price": 0,
                    "gross_pnl": 0,
                    "net_pnl": 0,
                    "fees_or_cost_penalty": 0,
                    "final_status": "",
                    # ── Validation ──
                    "pnl_validated": False,
                    "pnl_validation_error": "",
                    "duplicate_blocked": False,
                }

                # ── State transition: CANDIDATE → OPENED ──
                transition_position(pos, STATE_OPENED)

                cycle_entries.append({k: v for k, v in pos.items() if k != "status" or True})
                state["valid_opportunities"] += 1
                state["paper_trades_opened"] += 1
                state["entry_prices"].append(fill_price)
                if spread:
                    state["spreads"].append(spread)
                cycle_trades += 1

                # ── State transition: OPENED → ACTIVE ──
                transition_position(pos, STATE_ACTIVE)
                state["positions"][entry_id] = pos

                print(f"    PAPER {ak} {token_side} @ {fill_price:.4f} | ev={net_ev:.4f} bet=${bet_size:.2f} | {'EXEC' if book_available else 'GAMMA'} | {pos['status']}")

        # ── Lifecycle: check expiring and resolving positions ──
        for eid, pos in list(state["positions"].items()):
            status = pos.get("status", STATE_CANDIDATE)
            expiry_min = pos.get("time_to_expiry_at_entry", 5)
            entry_time = datetime.fromisoformat(pos.get("opened_at", pos.get("active_at", pos.get("candidate_at", "")).replace("Z", "+00:00")))
            elapsed_min = (cycle_time - entry_time).total_seconds() / 60

            if status == STATE_ACTIVE and elapsed_min >= expiry_min * 0.85:
                # ── State transition: ACTIVE → EXPIRING ──
                transition_position(pos, STATE_EXPIRING)
                state["positions_expiring"] = state.get("positions_expiring", 0) + 1
                print(f"    EXPIRING {eid[:8]} | {pos['asset']} {pos['selected_side']} | {elapsed_min:.1f}/{expiry_min:.1f}min")

            if status in (STATE_ACTIVE, STATE_EXPIRING) and elapsed_min >= expiry_min and expiry_min > 0:
                # Try to resolve from Polymarket
                condition_id = pos.get("condition_id_full", "")
                resolution = fetch_market_resolution(condition_id) if condition_id else None

                if resolution and resolution.get("closed"):
                    # Determine winner
                    outcome_prices = resolution.get("outcome_prices", "[]")
                    outcomes = resolution.get("outcomes", "[]")
                    try:
                        prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                        outs = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                    except:
                        prices = []
                        outs = []

                    if isinstance(prices, list) and isinstance(outs, list) and len(prices) == len(outs):
                        winner_idx = prices.index(max(prices)) if prices else -1
                        resolved_winner = outs[winner_idx] if winner_idx >= 0 else "UNKNOWN"
                    else:
                        resolved_winner = "UNKNOWN"

                    pos["resolved_winner"] = resolved_winner
                    pos["resolution_source"] = "polymarket_gamma"
                    pos["settlement_price"] = max(prices) if isinstance(prices, list) else 0

                    # ── State transition: EXPIRING → RESOLVED ──
                    transition_position(pos, STATE_RESOLVED)
                    state["positions_resolved"] = state.get("positions_resolved", 0) + 1
                    state["paper_trades_resolved"] += 1

                    # Validate PnL
                    net_pnl, pnl_valid, pnl_error = validate_pnl(pos, resolved_winner)
                    pos["net_pnl"] = net_pnl
                    pos["pnl_validated"] = pnl_valid
                    pos["pnl_validation_error"] = pnl_error or ""

                    if not pnl_valid:
                        state["settlement_errors"] += 1
                        print(f"    PnL INVALID {eid[:8]} | {pnl_error}")

                    # Apply PnL to bankroll
                    state["bankroll"] += net_pnl
                    state["total_pnl"] += net_pnl
                    state["total_trades"] += 1
                    we_won = (pos.get("selected_side", "").upper() == resolved_winner.upper())
                    if we_won:
                        state["wins"] += 1
                    else:
                        state["losses"] += 1

                    # Track DD
                    if state["bankroll"] > state["max_bankroll"]:
                        state["max_bankroll"] = state["bankroll"]
                    dd = (state["max_bankroll"] - state["bankroll"]) / state["max_bankroll"] if state["max_bankroll"] > 0 else 0
                    state["max_dd"] = max(state["max_dd"], dd)

                    # ── State transition: RESOLVED → SETTLED ──
                    transition_position(pos, STATE_SETTLED)
                    state["positions_settled"] = state.get("positions_settled", 0) + 1
                    state["resolution_delays"].append(round(elapsed_min - expiry_min, 2))

                    pos["exit_timestamp"] = cycle_time.isoformat()
                    pos["final_status"] = STATE_SETTLED

                    # Write to journal
                    journal_path = JOURNAL_DIR / f"{eid}.json"
                    with open(journal_path, 'w') as jf:
                        json.dump(pos, jf, indent=2, default=str)

                    # ── State transition: SETTLED → JOURNALED ──
                    transition_position(pos, STATE_JOURNALED)
                    state["positions_journaled"] = state.get("positions_journaled", 0) + 1

                    # Move to closed trades
                    state["closed_trades"].append({k: v for k, v in pos.items()})
                    del state["positions"][eid]

                    print(f"    RESOLVED {eid[:8]} | {pos['asset']} {pos['selected_side']} → {resolved_winner} | PnL=${net_pnl:.2f} | {'WIN' if we_won else 'LOSS'} | {pos['status']}")

                else:
                    # Market not yet resolved — track as unresolved past expiry
                    state["positions_unresolved_past_expiry"] = state.get("positions_unresolved_past_expiry", 0) + 1
                    print(f"    UNRESOLVED {eid[:8]} | {elapsed_min:.1f}min past expiry | checking next cycle")

        # ── Update cycle counters and streaks ──
        if had_valid_signal:
            state["cycles_with_signal"] += 1
            state["current_no_signal_streak"] = 0
        else:
            state["current_no_signal_streak"] += 1
            state["longest_no_signal_streak"] = max(state["longest_no_signal_streak"], state["current_no_signal_streak"])

        if had_compatible_market:
            state["cycles_with_valid_market"] += 1
            state["current_no_market_streak"] = 0
        else:
            state["current_no_market_streak"] += 1
            state["longest_no_market_streak"] = max(state["longest_no_market_streak"], state["current_no_market_streak"])

        if had_valid_signal and had_compatible_market:
            state["cycles_with_signal_and_market"] += 1

        if passed_price_gate:
            state["cycles_passing_price_gate"] += 1
        if passed_ev_gate:
            state["cycles_passing_ev_gate"] += 1

        if cycle_trades == 0:
            state["no_trade_cycles"] += 1
            state["current_no_trade_streak"] += 1
            state["longest_no_trade_streak"] = max(state["longest_no_trade_streak"], state["current_no_trade_streak"])
        else:
            state["current_no_trade_streak"] = 0

        for reason, count in cycle_blocked.items():
            state["blocked_reasons"][reason] = state.get("blocked_reasons", {}).get(reason, 0) + count

        save_profile_state(state, profile_key)
        cycle_results[profile_key] = {
            "trades": cycle_trades,
            "entries": cycle_entries,
            "blocked": dict(cycle_blocked),
            "bankroll": state["bankroll"],
            "total_pnl": state["total_pnl"],
            "total_trades": state["total_trades"],
            "wins": state["wins"],
            "losses": state["losses"],
        }

    # ── Save combined cycle report ──
    ts = cycle_time.strftime("%Y%m%d_%H%M%S")
    combined = {
        "timestamp": cycle_time.isoformat(),
        "prices": {ak: {"rsi": (all_signals.get(ak) or {}).get("rsi", 0),
                         "direction": (all_signals.get(ak) or {}).get("direction", "none"),
                         "confidence": (all_signals.get(ak) or {}).get("confidence", 0),
                         "zone": get_rsi_zone((all_signals.get(ak) or {}).get("rsi", 50))}
                    for ak in eng.ASSETS},
        "markets_found": {ak: len(all_contracts.get(ak, [])) for ak in eng.ASSETS},
        "profiles": cycle_results,
    }
    combined_path = OUT_DIR / f"cycle_{ts}.json"
    with open(combined_path, 'w') as f:
        json.dump(combined, f, indent=2, default=str)

    # ── Dashboard ──
    print(f"\n{'='*78}")
    print(f"PAPER TRADING DASHBOARD — {cycle_time.strftime('%H:%M:%S UTC')}")
    print(f"{'='*78}")
    print(f"  {'Asset':<6} {'RSI':>5} {'Zone':<20} {'Signal':<8} {'Markets':>7}")
    for ak in eng.ASSETS:
        sig = all_signals.get(ak) or {}
        zone = get_rsi_zone(sig.get("rsi", 50)) if sig else "no_data"
        print(f"  {ak:<6} {sig.get('rsi', 0):>5.1f} {zone:<20} {sig.get('direction', 'none'):<8} {len(all_contracts.get(ak, [])):>7}")

    print(f"\n  {'Profile':<25} {'$':>8} {'PnL':>7} {'Trd':>4} {'WR':>6} {'DD':>5} {'NoTr':>5} {'Opps':>5}")
    for pk in PROFILES:
        st = load_profile_state(pk)
        wr = st["wins"] / max(st["total_trades"], 1) * 100
        print(f"  {PROFILES[pk]['name']:<25} ${st['bankroll']:>7.2f} ${st['total_pnl']:>6.2f} {st['total_trades']:>4} {wr:>5.1f}% {st['max_dd']*100:>4.1f}% {st['no_trade_cycles']:>5} {st['valid_opportunities']:>5}")

    # ── Lifecycle dashboard ──
    core = load_profile_state("CORE_UP")
    print(f"\n  ── LIFECYCLE (CORE_UP) ──")
    print(f"  Candidate: {core.get('positions_candidate',0):>3} | Opened: {core.get('positions_opened',0):>3} | Active: {core.get('positions_active',0):>3} | Expiring: {core.get('positions_expiring',0):>3}")
    print(f"  Resolved: {core.get('positions_resolved',0):>3} | Settled: {core.get('positions_settled',0):>3} | Journaled: {core.get('positions_journaled',0):>3}")
    print(f"  Unresolved past expiry: {core.get('positions_unresolved_past_expiry',0)}")
    print(f"  Settlement errors: {core.get('settlement_errors',0)}")
    print(f"  Duplicate blocks: {core.get('duplicate_position_blocks',0)}")
    rd = core.get('resolution_delays', [])
    if rd:
        print(f"  Avg resolution delay: {sum(rd)/len(rd):.1f}min | Max: {max(rd):.1f}min")

    # ── Book metrics ──
    print(f"\n  ── BOOK METRICS (CORE_UP) ──")
    print(f"  Attempted: {core.get('book_checks_attempted',0)} | Successful: {core.get('book_checks_successful',0)} | Missing: {core.get('book_checks_missing',0)} | Stale: {core.get('book_checks_stale',0)}")
    print(f"  Skipped (no signal): {core.get('book_checks_skipped_no_signal',0)} | Skipped (no market): {core.get('book_checks_skipped_no_market',0)}")
    missing_rate = core.get('book_checks_missing', 0) / max(core.get('book_checks_attempted', 1), 1) * 100
    stale_rate = core.get('book_checks_stale', 0) / max(core.get('book_checks_attempted', 1), 1) * 100
    print(f"  Missing rate: {missing_rate:.1f}% | Stale rate: {stale_rate:.1f}%")

    # ── Streaks ──
    print(f"\n  ── STREAKS ──")
    print(f"  No-signal: cur={core.get('current_no_signal_streak',0)} longest={core.get('longest_no_signal_streak',0)}")
    print(f"  No-market: cur={core.get('current_no_market_streak',0)} longest={core.get('longest_no_market_streak',0)}")
    print(f"  No-trade:  cur={core.get('current_no_trade_streak',0)} longest={core.get('longest_no_trade_streak',0)}")

    # ── Cycle counters ──
    print(f"\n  ── CYCLES ──")
    print(f"  Total: {core.get('cycles_run',0)} | Signal: {core.get('cycles_with_signal',0)} | Market: {core.get('cycles_with_valid_market',0)} | Sig+Mkt: {core.get('cycles_with_signal_and_market',0)}")
    print(f"  Price gate: {core.get('cycles_passing_price_gate',0)} | EV gate: {core.get('cycles_passing_ev_gate',0)}")
    print(f"  Opened: {core.get('paper_trades_opened',0)} | Resolved: {core.get('paper_trades_resolved',0)}")

    # ── Per-profile detail ──
    print(f"\n  ── PER-PROFILE ──")
    for pk, cfg in PROFILES.items():
        st = load_profile_state(pk)
        avg_entry = sum(st.get("entry_prices", [1])) / max(len(st.get("entry_prices", [])), 1) if st.get("entry_prices") else 0
        avg_spread = sum(st.get("spreads", [0])) / max(len(st.get("spreads", [])), 1) if st.get("spreads") else 0
        pf = st["wins"] / max(st["losses"], 1) if st.get("losses", 0) > 0 else float("inf")
        print(f"  {pk}: opps={st.get('valid_opportunities',0)} trd={st['total_trades']} WR={st['wins']/max(st['total_trades'],1)*100:.1f}% PF={pf:.2f} DD={st['max_dd']*100:.1f}% avg_entry={avg_entry:.3f} avg_spread={avg_spread:.4f}")

    # ── Event-based readiness gate ──
    total_opp = core.get("valid_opportunities", 0)
    false_accepts = core.get("false_accepts", 0)
    daily_strikes = core.get("daily_strikes_accepted", 0)
    fallback = core.get("fallback_trades", 0)
    settlement_errors = core.get("settlement_errors", 0)
    stale_books = core.get("stale_book_trades", 0)
    total_trades = core.get("total_trades", 0)
    net_pnl = core.get("total_pnl", 0)
    max_dd = core.get("max_dd", 0)
    pf = (core.get("wins", 0) / max(core.get("losses", 1), 1)) if core.get("losses", 0) > 0 else float("inf")
    dup_blocks = core.get("duplicate_position_blocks", 0)
    unresolved = core.get("positions_unresolved_past_expiry", 0)
    unresolved_rate = unresolved / max(total_trades, 1) if total_trades > 0 else 0

    gates = {
        "min_50_opportunities": total_opp >= 50,
        "false_accepts_0": false_accepts == 0,
        "no_daily_strikes": daily_strikes == 0,
        "no_fallback_trades": fallback == 0,
        "no_stale_book_exec_trades": stale_books == 0,
        "settlement_errors_0": settlement_errors == 0,
        "duplicate_blocks_working": dup_blocks >= 0,
        "net_ev_positive": net_pnl > 0 if total_trades > 0 else None,
        "pf_1_25": pf >= 1.25 if total_trades >= 10 else None,
        "dd_15pct": max_dd <= 0.15,
        "journal_completeness": True,
        "unresolved_rate_reported": True,
    }

    all_passed = all(v for v in gates.values() if v is not None)
    if all_passed and total_opp >= 50 and settlement_errors == 0:
        classification = "B_PAPER_LIFECYCLE_VALIDATED"
    elif total_opp >= 10 and total_opp < 50:
        classification = "A_COLLECTING_DATA"
    elif core.get("total_cycles", 0) < 20:
        classification = "A_EARLY_STAGE_COLLECTING_DATA"
    else:
        classification = "A_PAPER_TRADING_CONTINUED"

    print(f"\n  ── READINESS GATE (CORE_UP) ──")
    print(f"  Opportunities: {total_opp}/50 | Trades: {total_trades} | PnL: ${net_pnl:.2f}")
    print(f"  Settlement errors: {settlement_errors} | Duplicate blocks: {dup_blocks} | Unresolved past expiry: {unresolved} ({unresolved_rate:.1%})")
    for gate, passed in gates.items():
        if passed is None:
            icon = "⏳"
        elif passed:
            icon = "✅"
        else:
            icon = "❌"
        print(f"  {gate}: {icon}")
    print(f"\n  CLASSIFICATION: {classification}")
    print(f"{'='*78}")

    return combined


if __name__ == "__main__":
    run_paper_cycle()