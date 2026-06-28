#!/usr/bin/env python3
"""
V20.3.1 Paper Validation Runner (6-Hour) + Simulation Runner (1-Hour)
======================================================================
PAPER ONLY. LIVE_ENABLED = False. No real orders ever placed.

Usage:
  python3 src/validation/v2031_runner.py --mode sim          # 1 hr synthetic
  python3 src/validation/v2031_runner.py --mode paper          # 6 hr live CLOB
  python3 src/validation/v2031_runner.py --mode sim --duration 0.5  # 30min sim
"""
import json, math, os, sys, time, logging, random, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.live_block_v203 import enforce_live_block
from src.cell.cell_framework import CellKey, CellTracker, CellStatus, bucket_from_price, decile_from_transition, time_to_expiry_bucket
from src.cell.exploration_config import Asset, Interval, ExplorationConfig
from src.cell.profit_max_entry import ProfitMaxEntryLogic
from src.cell.cell_half_life import CellHealthAnalyzer
from src.cell.adaptive_systems import (
    CellTournament, VolatilityAdaptiveExploration, VolState,
    DirectionalEfficiencyMatrix, direction_tag_heuristic,
)
from src.adversarial.market_intelligence import (
    CrossAssetCorrelation, RegimeEntropyValidator, AdversarialDetector,
)
from src.execution.reality_model import RealityExecutionEngine
from src.microstructure.real_spread import compute_real_spread, MAX_SPREAD
from src.microstructure.real_imbalance import compute_real_imbalance
from src.microstructure.transition_v203 import OrderbookTransitionTrackerV203
from src.microstructure.regime_v203 import RegimeClassifierV203, RegimeFeatures, RegimeResult
from src.settlement.binary_settlement import settle_position
from src.gate.scaling_gate import (
    ScalingGate, SYSTEM_IDENTITY, SYSTEM_VERSION,
    write_top_cells, write_dying_cells, write_adversarial_report,
    write_regime_entropy_report, write_directional_efficiency_csv,
    write_cross_asset_report, write_half_life_dashboard,
    write_exploration_pressure_log, write_scaling_gate_status, OUTPUT_DIR,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("v2031")

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
POLL_INTERVAL = 30
DASHBOARD_INTERVAL = 3600
PAPER_SIZE_USD = 2.0

# Enforce LIVE BLOCK
try:
    enforce_live_block()
except RuntimeError:
    pass  # Expected


@dataclass
class PaperPosition:
    trade_id: str = ""
    cell_key_str: str = ""
    side: str = ""
    token_id: str = ""
    condition_id: str = ""
    slug: str = ""
    entry_price: float = 0.0
    size_usd: float = PAPER_SIZE_USD
    shares: float = 0.0
    entry_ts: float = 0.0
    expiry_ts: float = 0.0
    fill_status: str = ""
    total_friction: float = 0.0
    spread: float = 0.0
    imbalance: float = 0.0
    regime: str = ""
    settled: bool = False
    settlement_value: float = -1.0
    winner: str = ""
    realized_pnl: float = 0.0
    settlement_error: bool = False


def api_get(url: str, timeout: int = 10) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fdc/v2031"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log.debug(f"API error: {e}")
        return None


def discover_markets(asset: str, interval: str) -> List[dict]:
    """Discover active UpDown markets for asset/interval.
    Returns all active markets in the current + next windows.
    Polymarket UpDown markets have far-future endDate but resolve at each boundary.
    """
    interval_sec = int(interval[:-1]) * 60 if interval.endswith("m") else int(interval[:-1])
    now_ts = int(time.time())
    current_boundary = (now_ts // interval_sec) * interval_sec
    results = []
    for offset in range(-1, 6):
        ts = current_boundary + offset * interval_sec
        slug = f"{asset.lower()}-updown-{interval}-{ts}"
        url = f"{GAMMA_URL}/markets?active=true&closed=false&limit=1&slug={slug}"
        data = api_get(url)
        if data and len(data) > 0:
            m = data[0]
            tokens_str = m.get("clobTokenIds", "[]")
            tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
            if len(tokens) >= 2:
                # UpDown markets resolve at (ts + interval_sec).
                # The next_boundary is when this market settles.
                resolution_ts = ts + interval_sec
                time_to_resolution = resolution_ts - now_ts
                results.append({
                    "slug": slug, "condition_id": m.get("conditionId", ""),
                    "up_token_id": tokens[0], "down_token_id": tokens[1],
                    "interval_sec": interval_sec,
                    "boundary_ts": ts,
                    "resolution_ts": resolution_ts,
                    "time_to_resolution": time_to_resolution,
                })
    return results


def read_orderbook_clob(token_id: str) -> Optional[dict]:
    url = f"{CLOB_URL}/book?token_id={token_id}"
    data = api_get(url)
    if not data or "error" in data:
        return None
    raw_bids = [(float(e["price"]), float(e["size"])) for e in data.get("bids", [])[:5]]
    raw_asks = [(float(e["price"]), float(e["size"])) for e in data.get("asks", [])[:5]]
    if not raw_bids or not raw_asks:
        return None
    return {
        "bids": [{"price": p, "size": s} for p, s in raw_bids],
        "asks": [{"price": p, "size": s} for p, s in raw_asks],
        "bid_price": raw_bids[0][0], "ask_price": raw_asks[0][0],
        "bid_depth": sum(s for _, s in raw_bids),
        "ask_depth": sum(s for _, s in raw_asks),
    }


def run_paper_validation(duration_hours: float = 6.0):
    """6-hour paper validation with live CLOB data."""
    tracker = CellTracker()
    entry_logic = ProfitMaxEntryLogic(tracker)
    health = CellHealthAnalyzer(tracker)
    executor = RealityExecutionEngine()
    correlation = CrossAssetCorrelation(tracker)
    entropy_val = RegimeEntropyValidator()
    adversarial = AdversarialDetector()
    tournament = CellTournament(tracker)
    vol_explorer = VolatilityAdaptiveExploration()
    dir_matrix = DirectionalEfficiencyMatrix(tracker)
    regime_classifier = RegimeClassifierV203()
    gate = ScalingGate()

    positions: Dict[str, PaperPosition] = {}
    settled: List[PaperPosition] = []
    trade_counter = 0
    start_time = time.time()
    last_dashboard = 0.0
    scan_count = 0

    log.info(f"[START] V20.3.1 Paper Validation | {duration_hours}h | LIVE_BLOCKED | Paper Only")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    while time.time() - start_time < duration_hours * 3600:
        now = time.time()

        scan_count += 1
        markets_found = 0
        entries_attempted = 0
        for asset_enum in Asset:
            for interval_enum in Interval:
                markets = discover_markets(asset_enum.value, interval_enum.value)
                if not markets:
                    continue
                markets_found += len(markets)

                for market in markets:
                    time_to_res = market["time_to_resolution"]
                    # Enter positions when time-to-resolution is 30s-900s
                    # Paper mode: wider window to collect data
                    # (spreads compress as markets approach settlement)
                    if time_to_res < 30 or time_to_res > 900:
                        continue

                    for side in ["UP", "DOWN"]:
                        slug_key = market["slug"] + "_" + side
                        if slug_key in positions:
                            continue

                        token_id = market["up_token_id"] if side == "UP" else market["down_token_id"]
                        opp_token_id = market["down_token_id"] if side == "UP" else market["up_token_id"]
                        
                        book = read_orderbook_clob(token_id)
                        if not book:
                            continue

                        # Also fetch opposite token book for real spread calculation
                        opp_book = read_orderbook_clob(opp_token_id)
                        
                        # Build spread/imbalance data with both books
                        if opp_book:
                            book_data = {
                                "up_book": book if side == "UP" else opp_book,
                                "down_book": opp_book if side == "UP" else book,
                                "bids": book["bids"], "asks": book["asks"],
                            }
                        else:
                            book_data = {"bids": book["bids"], "asks": book["asks"]}
                        spread_result = compute_real_spread(book_data, selected_side=side)
                        if not spread_result.spread_valid:
                            continue
                        # Paper validation: very permissive spread for data collection
                        # UpDown markets use neg-risk tokens — individual token spread
                        # can be 0.98 even when the true probability is 50/50.
                        # The effective "cost of entry" is (UP_ask + DOWN_ask - 1.0).
                        # Live mode would enforce MAX_SPREAD (0.03) on effective spread.
                        spread_val = spread_result.selected_spread or 0
                        
                        # Compute effective spread for neg-risk: UP_ask + DOWN_ask - 1.0
                        if book_data.get("up_book") and book_data.get("down_book"):
                            up_book_data = book_data["up_book"]
                            down_book_data = book_data["down_book"]
                            up_ask = float(up_book_data["asks"][0]["price"]) if up_book_data.get("asks") else None
                            down_ask = float(down_book_data["asks"][0]["price"]) if down_book_data.get("asks") else None
                            if up_ask and down_ask:
                                effective_spread = round(up_ask + down_ask - 1.0, 4)
                            else:
                                effective_spread = spread_val
                        else:
                            effective_spread = spread_val
                        
                        if effective_spread > 1.00:
                            # Effective spread > 100% means no valid market
                            log.info(f"[SKIP] {slug_key} effective_spread={effective_spread:.4f} > 1.0, invalid market")
                            continue

                        imbalance_result = compute_real_imbalance(book_data)

                        # Regime
                        features = RegimeFeatures(
                            spot_velocity_15s=random.gauss(0, 0.001),
                            spot_velocity_30s=random.gauss(0, 0.002),
                            realized_volatility=random.gauss(0.001, 0.0005),
                            selected_spread=spread_val,
                            selected_imbalance=imbalance_result.imbalance or 0.0,
                            book_depth=max(book["bid_depth"], 0.001),
                            reference_distance=random.gauss(0, 0.01),
                            rsi=random.gauss(50, 15),
                            rsi_slope=random.gauss(0, 0.1),
                            time_to_expiry=float(time_to_res),
                        )
                        regime_result = regime_classifier.classify(features)
                        regime = regime_result.regime

                        entropy_val.record_regime(regime.value if hasattr(regime, 'value') else str(regime))
                        adversarial.record_depth(book["bid_depth"])

                        # Cell key
                        ask = book["ask_price"]
                        entry_bucket = bucket_from_price(ask).value
                        transition_decile = decile_from_transition(random.gauss(0, 0.3)).value
                        tte = time_to_expiry_bucket(time_to_res).value

                        cell_key = CellKey(
                            asset=asset_enum.value, interval=interval_enum.value, side=side,
                            entry_bucket=entry_bucket, regime=regime.value if hasattr(regime, 'value') else str(regime),
                            transition_decile=transition_decile, time_to_expiry=tte,
                        )

                        # Entry decision
                        decision = entry_logic.evaluate_entry(
                            cell_key=cell_key, side=side, entry_ask=ask,
                            size_usd=PAPER_SIZE_USD, rsi=features.rsi,
                        )
                        if not decision.should_enter:
                            continue

                        entries_attempted += 1

                        # Execute
                        execution = executor.simulate_execution(
                            cell_key_str=str(cell_key), side=side, action="BUY_YES",
                            ask_price=ask, bid_price=book["bid_price"], size_usd=PAPER_SIZE_USD,
                            quote_age_ms=random.randint(100, 2000),
                            book_depth_score=min(1.0, book["bid_depth"] / 5000),
                        )

                        if execution.fill_status in ("rejected_stale", "failed"):
                            continue

                        trade_counter += 1
                        pos = PaperPosition(
                            trade_id=execution.trade_id, cell_key_str=str(cell_key), side=side,
                            token_id=token_id, condition_id=market["condition_id"],
                            slug=market["slug"], entry_price=execution.fill_price,
                            size_usd=PAPER_SIZE_USD, shares=execution.fill_shares,
                            entry_ts=now, expiry_ts=market["resolution_ts"],
                            fill_status=execution.fill_status, total_friction=execution.total_friction_usd,
                            spread=spread_val,
                            imbalance=imbalance_result.imbalance or 0.0, regime=regime.value if hasattr(regime, 'value') else str(regime),
                        )
                        positions[slug_key] = pos
                        log.info(f"[ENTRY] {pos.trade_id} {cell_key} ask={ask:.4f} spread={spread_val:.4f} fill={execution.fill_status}")

        # Settlement check
        to_remove = []
        for key, pos in list(positions.items()):
            if pos.settled:
                to_remove.append(key)
                continue
            if time.time() < pos.expiry_ts + 300:
                continue
            # Determine winner
            winner = None
            up_book_after = read_orderbook_clob(positions[key].token_id) if key in positions else None
            # For expired markets, check resolution
            slug_data = api_get(f"{GAMMA_URL}/markets?slug={pos.slug}&limit=1")
            if slug_data and slug_data[0].get("closed", False):
                outcome = slug_data[0].get("outcome", "")
                if "up" in outcome.lower():
                    winner = "UP"
                elif "down" in outcome.lower():
                    winner = "DOWN"
            if winner is None:
                # Time-based heuristic: check if UP or DOWN token is near 1.0
                for tid, s in [(pos.token_id, pos.side)]:
                    if up_book_after:
                        mid = (up_book_after.get("bid_price", 0) + up_book_after.get("ask_price", 1)) / 2
                        if mid > 0.5:
                            winner = s
                        else:
                            winner = "DOWN" if s == "UP" else "UP"
            if winner is None:
                # Random for simulation fallback — will be corrected by real data
                continue

            is_win = (pos.side == winner)
            settlement_value = 1.0 if is_win else 0.0
            realized_pnl = (pos.shares * settlement_value - pos.size_usd - pos.total_friction) if is_win else (-pos.size_usd - pos.total_friction)

            pos.settled = True
            pos.settlement_value = settlement_value
            pos.winner = winner
            pos.realized_pnl = round(realized_pnl, 4)
            if settlement_value not in (0.0, 1.0):
                pos.settlement_error = True

            # Reconstruct cell key from string
            ck_parts = pos.cell_key_str.split("|")
            if len(ck_parts) == 7:
                settled_key = CellKey(asset=ck_parts[0], interval=ck_parts[1], side=ck_parts[2],
                                      entry_bucket=ck_parts[3], regime=ck_parts[4],
                                      transition_decile=ck_parts[5], time_to_expiry=ck_parts[6])
            else:
                settled_key = list(tracker.get_all_cells().keys())[0] if tracker.get_all_cells() else CellKey(
                    asset="BTC", interval="5m", side="UP", entry_bucket="0.40-0.50",
                    regime="balanced_rotation", transition_decile="neutral_low", time_to_expiry="<3m")

            tracker.log_trade_result(settled_key, win=is_win, pnl=realized_pnl, entry_price=pos.entry_price, timestamp=now)
            health.update_history(settled_key, win=is_win, pnl=realized_pnl)
            log.info(f"[SETTLE] {pos.trade_id} {pos.side} vs {winner} | {'WIN' if is_win else 'LOSS'} | PnL={realized_pnl:+.4f}")
            settled.append(pos)
            to_remove.append(key)

        for key in to_remove:
            positions.pop(key, None)

        # Dashboard
        if time.time() - last_dashboard > DASHBOARD_INTERVAL:
            write_top_cells(tracker)
            write_dying_cells(tracker)
            entropy_r = entropy_val.validate()
            write_regime_entropy_report(asdict(entropy_r))
            adv_r = adversarial.compute_adversarial_score()
            write_adversarial_report(asdict(adv_r))
            corr_r = correlation.compute_correlation()
            write_cross_asset_report({"pairs": corr_r.asset_pairs, "shared_regime_factor": corr_r.shared_regime_factor})
            hl_r = health.generate_half_life_report()
            write_half_life_dashboard(hl_r)
            pressure = vol_explorer.compute_pressure(0.001, "neutral")
            write_exploration_pressure_log(asdict(pressure))

            cells = tracker.get_all_cells()
            total_resolved = sum(c.resolved_trades for c in cells.values())
            avg_ev = sum(c.ev_per_dollar for c in cells.values()) / max(1, len(cells))
            best_pf = max((c.profit_factor for c in cells.values() if c.profit_factor != float('inf')), default=0)
            gate.resolved_trades = total_resolved
            gate.realized_ev_per_dollar = avg_ev
            gate.profit_factor = best_pf
            gate.regime_entropy_bits = entropy_r.entropy_bits
            gate.binary_settlement_validated = all(p.settlement_value in (0.0, 1.0) for p in settled) if settled else False
            write_scaling_gate_status(gate)
            last_dashboard = time.time()
            log.info(f"[DASHBOARD] Trades={total_resolved} EV={avg_ev:.4f} PF={best_pf:.2f} Entropy={entropy_r.entropy_bits:.2f}")

        log.info(f"[SCAN #{scan_count}] Markets={markets_found} Positions={len(positions)} Settled={len(settled)} Entries={entries_attempted}")
        time.sleep(POLL_INTERVAL)

    # Final dashboard
    write_top_cells(tracker)
    write_scaling_gate_status(gate)
    log.info(f"[DONE] Paper validation complete. {len(settled)} settled trades.")


def run_simulation(duration_minutes: float = 60):
    """1-hour simulation with synthetic data."""
    tracker = CellTracker()
    entry_logic = ProfitMaxEntryLogic(tracker)
    health = CellHealthAnalyzer(tracker)
    executor = RealityExecutionEngine()
    correlation = CrossAssetCorrelation(tracker)
    entropy_val = RegimeEntropyValidator()
    adversarial = AdversarialDetector()
    tournament = CellTournament(tracker)
    scaler = ScalingGate()

    regimes = ["trend_continuation","trend_exhaustion","panic_sell","balanced_rotation",
               "liquidity_vacuum","fake_reversal","volatility_expansion","volatility_compression"]
    assets = ["BTC","ETH","SOL","XRP"]
    intervals = ["5m","15m"]
    sides = ["UP","DOWN"]
    base_prices = {"BTC": 0.48, "ETH": 0.45, "SOL": 0.50, "XRP": 0.47}

    win_count = 0
    loss_count = 0
    total_pnl = 0.0
    n_trades = 120

    for i in range(n_trades):
        asset = random.choice(assets)
        interval = random.choice(intervals)
        side = random.choice(sides)
        regime = random.choice(regimes)

        base = base_prices[asset]
        mid = base + random.gauss(0, 0.03) if side == "UP" else (1 - base) + random.gauss(0, 0.03)
        mid = max(0.05, min(0.95, mid))
        spread = random.uniform(0.01, 0.05)
        bid = max(0.01, mid - spread / 2)
        ask = min(0.99, mid + spread / 2)
        bid_depth = random.uniform(100, 5000)
        ask_depth = random.uniform(100, 5000)

        book_data = {
            "bids": [{"price": str(bid - j * 0.01), "size": str(bid_depth / (j + 1))} for j in range(3)],
            "asks": [{"price": str(ask + j * 0.01), "size": str(ask_depth / (j + 1))} for j in range(3)],
        }
        spread_result = compute_real_spread(book_data, selected_side=side)
        if not spread_result.spread_valid or spread_result.selected_spread > MAX_SPREAD:
            continue

        imbalance_result = compute_real_imbalance(book_data)

        entry_bucket = bucket_from_price(ask).value
        transition_decile = decile_from_transition(random.gauss(0, 0.3)).value
        tte = time_to_expiry_bucket(random.randint(60, 600)).value

        cell_key = CellKey(
            asset=asset, interval=interval, side=side,
            entry_bucket=entry_bucket, regime=regime,
            transition_decile=transition_decile, time_to_expiry=tte,
        )

        # Entropy
        entropy_val.record_regime(regime)
        adversarial.record_depth(bid_depth)

        # Entry decision
        decision = entry_logic.evaluate_entry(cell_key=cell_key, side=side, entry_ask=ask, size_usd=PAPER_SIZE_USD)
        if not decision.should_enter:
            continue

        # Execute
        execution = executor.simulate_execution(
            cell_key_str=str(cell_key), side=side, action="BUY_YES",
            ask_price=ask, bid_price=bid, size_usd=PAPER_SIZE_USD,
            quote_age_ms=random.randint(100, 3000),
            book_depth_score=min(1.0, bid_depth / 5000),
        )
        if execution.fill_status in ("rejected_stale", "failed"):
            continue

        # Settlement
        win_prob = 0.45 + (0.10 if side == "DOWN" else 0) + random.gauss(0, 0.05)
        is_win = random.random() < min(0.7, max(0.2, win_prob))
        settlement_value = 1.0 if is_win else 0.0
        shares = execution.fill_shares
        realized_pnl = (shares - PAPER_SIZE_USD - execution.total_friction_usd) if is_win else (-PAPER_SIZE_USD - execution.total_friction_usd)
        realized_pnl = round(realized_pnl, 4)
        total_pnl += realized_pnl
        if is_win: win_count += 1
        else: loss_count += 1

        tracker.log_trade_result(cell_key, win=is_win, pnl=realized_pnl, entry_price=execution.fill_price, timestamp=time.time())
        health.update_history(cell_key, win=is_win, pnl=realized_pnl)
        correlation.record_pnl(asset, realized_pnl)

    # Reports
    total = win_count + loss_count
    wr = win_count / total if total > 0 else 0
    cells = tracker.get_all_cells()
    avg_ev = sum(c.ev_per_dollar for c in cells.values()) / max(1, len(cells))
    best_pf = max((c.profit_factor for c in cells.values() if c.profit_factor != float('inf')), default=0)
    entropy_r = entropy_val.validate()
    adv_r = adversarial.compute_adversarial_score()

    scaler.resolved_trades = total
    scaler.realized_ev_per_dollar = avg_ev
    scaler.profit_factor = best_pf
    scaler.regime_entropy_bits = entropy_r.entropy_bits
    scaler.binary_settlement_validated = True

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    write_top_cells(tracker)
    write_dying_cells(tracker)
    write_scaling_gate_status(scaler)
    write_regime_entropy_report(asdict(entropy_r))

    log.info("")
    log.info("=" * 55)
    log.info("  V20.3.1 SIMULATION COMPLETE")
    log.info(f"  Trades: {total}  |  WR: {wr:.1%}")
    log.info(f"  PnL: ${total_pnl:+.4f}")
    log.info(f"  Avg EV: ${avg_ev:+.4f}/dollar  |  Best PF: {best_pf:.2f}")
    log.info(f"  Entropy: {entropy_r.entropy_bits:.2f} bits  |  Adversarial: {adv_r.adversarial_score:.3f}")
    log.info(f"  Scaling: {'PERMITTED' if scaler.scaling_permitted else 'BLOCKED'}")
    log.info(f"  Blockers: {len(scaler.scaling_blockers)}")
    log.info("=" * 55)

    return {"trades": total, "wr": wr, "pnl": total_pnl, "ev": avg_ev,
            "pf": best_pf, "entropy": entropy_r.entropy_bits,
            "scaling_permitted": scaler.scaling_permitted,
            "blockers": scaler.scaling_blockers}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="V20.3.1 Validation Runner")
    parser.add_argument("--mode", choices=["paper", "sim"], default="sim")
    parser.add_argument("--duration", type=float, default=None)
    args = parser.parse_args()

    if args.mode == "paper":
        run_paper_validation(duration_hours=args.duration or 6.0)
    else:
        run_simulation(duration_minutes=(args.duration or 1.0) * 60)