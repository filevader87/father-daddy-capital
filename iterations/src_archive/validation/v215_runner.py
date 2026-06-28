"""V21.5 Opportunity Extraction Runner — Patched
===================================================
Continuous probabilistic opportunity extraction.
Score everything, reject little, rank aggressively, execute selectively.

§1-17 V21.5 Opportunity Extraction Directive implemented:
- §4: Entry timing (structure formation, wait for asymmetry)
- §5: Market phase weighting (40-80% boost, final 120s boost, early penalty)
- §6: Directional persistence scoring (velocity, candle, consecutives, distance, approach)
- §7: Side selection (score decides, no ideology)
- §8: Execution reality (binary settlement, executable ask, spread/slippage)
- §9: Adversarial assumption
- §15: Dynamic opportunity ranking (not whitelist)
- §16: Full output spec per scan
"""
from __future__ import annotations
import csv
import json
import logging
import math
import time
import argparse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.scoring.opportunity_scorer import OpportunityScore, OpportunityRanker
from src.scoring.entry_timing import EntryTimingEngine, EntryWindow
from src.directional.asymmetry_engine import DirectionalAsymmetryEngine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
INTERVALS = ["5m", "15m"]
INTERVAL_SECS = {"5m": 300, "15m": 900}

# Hard constraints (§14)
MAX_POSITION = 2.0
MAX_CONCURRENT = 1
MAX_DAILY_LOSS = 10.0
MAX_WEEKLY_LOSS = 30.0
MAX_DAILY_TRADES = 20
FORCED_SHUTDOWN = True

# §4: Paper participation mode — force top-ranked entry every 5-10 minutes
PAPER_FORCE_INTERVAL = 300  # seconds (5 min minimum between forced entries)
PAPER_FORCE_MAX_PER_HOUR = 6


class V215Runner:
    """V21.5 continuous opportunity extraction runner."""

    def __init__(self, mode: str = "paper", duration_hours: float = 5.0):
        self.mode = mode
        self.duration_hours = duration_hours
        self.start_time = time.time()

        # Core systems
        self.directional = DirectionalAsymmetryEngine()
        self.ranker = OpportunityRanker()
        self.timing = EntryTimingEngine()

        # State
        self.scan_count = 0
        self.entries_attempted = 0
        self.entries_accepted = 0
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        self.daily_trade_count = 0
        self.settled_pnl = 0.0

        # §4: Paper participation mode
        self.last_forced_entry_time = 0.0
        self.forced_entries = 0
        self.forced_entry_log: list[dict] = []

        # §9: Output files
        self.output_dir = Path("output/v215")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.output_dir / "top_opportunities.jsonl"
        self.forced_jsonl_path = self.output_dir / "forced_top_ranked_paper_trades.jsonl"
        self.csv_path = self.output_dir / "side_score_distribution.csv"
        self.report_path = self.output_dir / "V21_5_OPPORTUNITY_RANKING_REPORT.md"

        # Initialize CSV
        with open(self.csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'scan', 'asset', 'interval', 'direction',
                             'composite', 'dir_score', 'mom_score', 'lag_score',
                             'vol_score', 'tte_score', 'exec_score', 'cross_score',
                             'rsi_score', 'side_advantage', 'is_top_side',
                             'ev', 'entry_price', 'spread', 'decision'])

        # Spot price tracking for §6 directional persistence
        self._spot_history: dict[str, list[tuple[float, float]]] = {}  # asset → [(ts, price)]
        self._reference_prices: dict[str, float] = {}

        # Adversarial tracking
        self.adversarial_score = 0.0
        self.fake_reversal_count = 0
        self.spread_trap_count = 0
        self.scan_anomalies = 0

    def api_get(self, url: str, timeout: int = 10) -> Optional[dict]:
        """Fetch from Polymarket API."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fdc/v215"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            logger.debug(f"API error: {e}")
            return None

    def discover_markets(self) -> list[dict]:
        """Discover all active UpDown markets via Polymarket Gamma API."""
        markets = []
        now_ts = int(time.time())

        for asset in ASSETS:
            for interval in INTERVALS:
                ival_sec = INTERVAL_SECS[interval]
                current_boundary = (now_ts // ival_sec) * ival_sec

                for offset in range(-1, 6):
                    ts = current_boundary + offset * ival_sec
                    slug = f"{asset.lower()}-updown-{interval}-{ts}"
                    url = f"{GAMMA_URL}/markets?active=true&closed=false&limit=1&slug={slug}"
                    data = self.api_get(url)

                    if data and len(data) > 0:
                        m = data[0]
                        tokens_str = m.get("clobTokenIds", "[]")
                        try:
                            tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
                        except (json.JSONDecodeError, TypeError):
                            continue

                        if len(tokens) >= 2:
                            resolution_ts = ts + ival_sec
                            tte = resolution_ts - now_ts
                            if tte < 10:
                                continue

                            markets.append({
                                'slug': slug,
                                'asset': asset,
                                'interval': interval,
                                'condition_id': m.get('conditionId', ''),
                                'up_token_id': tokens[0],
                                'down_token_id': tokens[1],
                                'interval_sec': ival_sec,
                                'boundary_ts': ts,
                                'resolution_ts': resolution_ts,
                                'time_to_expiry': tte,
                                'epoch': ts,
                            })
        return markets

    def read_orderbook(self, token_id: str) -> Optional[dict]:
        """Read orderbook from Polymarket CLOB."""
        url = f"{CLOB_URL}/book?token_id={token_id}"
        data = self.api_get(url)
        if not data or "error" in data:
            return None

        raw_bids = [(float(e["price"]), float(e["size"])) for e in data.get("bids", [])[:5]]
        raw_asks = [(float(e["price"]), float(e["size"])) for e in data.get("asks", [])[:5]]
        if not raw_bids or not raw_asks:
            return None

        return {
            "bids": [{"price": p, "size": s} for p, s in raw_bids],
            "asks": [{"price": p, "size": s} for p, s in raw_asks],
            "bid_price": raw_bids[0][0],
            "ask_price": raw_asks[0][0],
            "bid_depth": sum(s for _, s in raw_bids),
            "ask_depth": sum(s for _, s in raw_asks),
        }

    def _update_spot_history(self, asset: str, price: float):
        """§6: Track spot price history for directional persistence."""
        now = time.time()
        if asset not in self._spot_history:
            self._spot_history[asset] = []
        self._spot_history[asset].append((now, price))
        # Keep last 120 seconds
        self._spot_history[asset] = [(t, p) for t, p in self._spot_history[asset]
                                       if now - t < 120]
        # Update reference price
        if asset not in self._reference_prices and price > 0:
            self._reference_prices[asset] = price

    def _get_persistence_data(self, asset: str) -> dict:
        """§6: Compute directional persistence metrics from spot history."""
        history = self._spot_history.get(asset, [])
        if len(history) < 2:
            return {
                'candle_direction': 'NEUTRAL',
                'consecutive_moves': 0,
                'velocity_15s': 0.0,
                'velocity_30s': 0.0,
                'velocity_60s': 0.0,
                'distance_from_reference': 0.0,
                'price_approach': 'NEUTRAL',
            }

        now = time.time()
        current_price = history[-1][1]

        # Velocities at multiple horizons (§6)
        def _velocity(history, horizon):
            recent = [(t, p) for t, p in history if now - t <= horizon]
            if len(recent) < 2:
                return 0.0
            dt = recent[-1][0] - recent[0][0]
            dp = recent[-1][1] - recent[0][1]
            return dp / dt if dt > 0 else 0.0

        vel_15 = _velocity(history, 15)
        vel_30 = _velocity(history, 30)
        vel_60 = _velocity(history, 60)

        # Candle direction
        if current_price > history[0][1] * 1.0001:
            candle_dir = "UP"
        elif current_price < history[0][1] * 0.9999:
            candle_dir = "DOWN"
        else:
            candle_dir = "NEUTRAL"

        # Consecutive directional moves
        consec = 0
        last_dir = None
        for i in range(len(history) - 1, 0, -1):
            dp = history[i][1] - history[i-1][1]
            d = "UP" if dp > 0.0001 else ("DOWN" if dp < -0.0001 else "NEUTRAL")
            if last_dir is None:
                last_dir = d
                consec = 1
            elif d == last_dir and d != "NEUTRAL":
                consec += 1
            else:
                break

        # Distance from reference
        ref = self._reference_prices.get(asset, current_price)
        dist = (current_price - ref) / ref if ref > 0 else 0.0

        # Price approach
        if abs(dist) < 0.0001:
            approach = "NEUTRAL"
        elif abs(current_price - history[0][1]) < abs(current_price - ref):
            approach = "TOWARD"  # converging back
        else:
            approach = "AWAY"    # diverging further

        return {
            'candle_direction': candle_dir,
            'consecutive_moves': consec,
            'velocity_15s': vel_15,
            'velocity_30s': vel_30,
            'velocity_60s': vel_60,
            'distance_from_reference': dist,
            'price_approach': approach,
        }

    def fetch_market_data(self, market: dict) -> dict:
        """Fetch real orderbook data from Polymarket CLOB."""
        asset = market['asset']
        interval = market['interval']
        tte = market['time_to_expiry']
        epoch = market.get('epoch', market.get('boundary_ts', 0))
        slug = market['slug']

        data = {
            'slug': slug,
            'asset': asset,
            'interval': interval,
            'tte': tte,
            'epoch': epoch,
            'spot_delta': 0.0,
            'up_ask': 0.99,
            'down_ask': 0.99,
            'up_bid': 0.01,
            'down_bid': 0.01,
            'effective_spread': 0.98,
            'volume': 0.0,
            'rsi': 50.0,
            'recent_deltas': [],
            'oracle_lag': 0.0,
            'orderbook_asymmetry': 0.0,
            'volatility': 0.01,
            'price_velocity': 0.0,
            'momentum_trend': 0.0,
        }

        # Fetch UP token orderbook
        up_book = self.read_orderbook(market.get('up_token_id', ''))
        if up_book:
            data['up_ask'] = up_book['ask_price']
            data['up_bid'] = up_book['bid_price']
            data['volume'] += up_book.get('bid_depth', 0) + up_book.get('ask_depth', 0)
            # Derive spot price from UP bid (UP bid ≈ Prob(UP))
            if up_book['bid_price'] > 0:
                self._update_spot_history(asset, up_book['bid_price'])

        # Fetch DOWN token orderbook
        down_book = self.read_orderbook(market.get('down_token_id', ''))
        if down_book:
            data['down_ask'] = down_book['ask_price']
            data['down_bid'] = down_book['bid_price']
            data['volume'] += down_book.get('bid_depth', 0) + down_book.get('ask_depth', 0)

        # Compute effective spread
        data['effective_spread'] = data['up_ask'] + data['down_ask'] - 1.0
        if data['effective_spread'] < 0:
            data['effective_spread'] = abs(data['up_ask'] - data['down_bid'])

        # Orderbook asymmetry
        if up_book and down_book:
            up_depth = up_book.get('bid_depth', 0) + up_book.get('ask_depth', 0)
            down_depth = down_book.get('bid_depth', 0) + down_book.get('ask_depth', 0)
            total_depth = up_depth + down_depth
            if total_depth > 0:
                data['orderbook_asymmetry'] = (up_depth - down_depth) / total_depth

        # Compute spot delta from history
        persistence = self._get_persistence_data(asset)
        data['spot_delta'] = persistence.get('distance_from_reference', 0.0)
        data['price_velocity'] = persistence.get('velocity_30s', 0.0)
        data['momentum_trend'] = persistence.get('velocity_60s', 0.0)

        # TTE scoring
        pct_elapsed = 1.0 - (tte / INTERVAL_SECS[interval])
        data['pct_elapsed'] = pct_elapsed

        # Store persistence in data for scoring
        data['persistence'] = persistence

        return data

    def score_market(self, market: dict, data: dict) -> list[OpportunityScore]:
        """§1: Score both UP and DOWN for every market."""
        scores = []
        tte = data['tte']
        interval = data['interval']
        asset = data['asset']

        for direction in ["UP", "DOWN"]:
            # Entry price for this direction
            entry_price = data['up_ask'] if direction == "UP" else data['down_ask']

            # §6: Directional persistence scoring
            persistence = data.get('persistence', {})
            dir_score = self.ranker.score_directional(
                data['spot_delta'], direction,
                persistence=persistence if persistence.get('velocity_15s') is not None else None,
            )

            # Momentum score
            momentum_score = self.ranker.score_momentum(
                data.get('price_velocity', 0.0),
                data.get('volume', 0.0)
            )

            # §2: Lag is ONE component, NOT a gate
            lag_score = self.ranker.score_lag(data.get('oracle_lag', 0.0))

            # Volatility score
            vol_score = self.ranker.score_volatility(
                data.get('volatility', 0.01), 0.01
            )

            # §5: TTE with market phase weighting
            tte_score = self.ranker.score_tte(
                tte, interval, data.get('pct_elapsed', 0.0)
            )

            # Execution score based on spread
            exec_score = self.ranker.score_execution(data['effective_spread'])

            # Cross-asset confirmation
            cross_score = self.ranker.score_cross_asset(asset)

            # RSI context score (5% — context only)
            rsi_score = self.ranker.score_rsi_context(
                data.get('rsi', 50.0), direction
            )

            # Match profile
            profile_id = self._match_profile(asset, interval, direction, tte)

            # Estimate probability
            est_prob = self._estimate_probability(
                data, direction, dir_score, lag_score, tte_score
            )

            opp = OpportunityScore(
                market_slug=data['slug'],
                asset=asset,
                interval=interval,
                direction=direction,
                time_to_expiry=tte,
                directional_score=dir_score,
                momentum_score=momentum_score,
                lag_score=lag_score,
                volatility_score=vol_score,
                tte_score=tte_score,
                execution_score=exec_score,
                cross_asset_score=cross_score,
                rsi_context_score=rsi_score,
                estimated_probability=est_prob,
                entry_price=entry_price,
                spread=data['effective_spread'],
                slippage_estimate=0.01,
                adversarial_score=self.adversarial_score,
                profile_id=profile_id,
                cell_id=f"{asset}_{interval}_{direction}_{tte//60}m",
            )

            # §6: Store directional persistence data
            opp.spot_velocity_15s = persistence.get('velocity_15s', 0.0)
            opp.spot_velocity_30s = persistence.get('velocity_30s', 0.0)
            opp.spot_velocity_60s = persistence.get('velocity_60s', 0.0)
            opp.candle_direction = persistence.get('candle_direction', 'NEUTRAL')
            opp.consecutive_directional_moves = persistence.get('consecutive_moves', 0)
            opp.distance_from_reference = persistence.get('distance_from_reference', 0.0)
            opp.price_approach = persistence.get('price_approach', 'NEUTRAL')

            opp.compute_ev()
            scores.append(opp)

        return scores

    def _match_profile(self, asset: str, interval: str,
                       direction: str, tte: float) -> str:
        """Match to the best profile for this market."""
        ival = INTERVAL_SECS[interval]
        pct_elapsed = 1.0 - (tte / ival)

        if pct_elapsed > 0.80:
            return f"{asset}_{interval}_REPRICING_{direction}"
        elif pct_elapsed > 0.40:
            return f"{asset}_{interval}_CONTINUATION_{direction}"
        else:
            return f"{asset}_{interval}_FORMATION_{direction}"

    def _estimate_probability(self, data: dict, direction: str,
                               dir_score: float, lag_score: float,
                               tte_score: float) -> float:
        """Estimate win probability from all signals."""
        base = 0.5 + 0.3 * (dir_score - 0.5)
        lag_boost = lag_score * 0.05  # §2: lag contributes, not gates
        tte_adj = (tte_score - 0.5) * 0.05
        # §7: Side selection influence (orderbook asymmetry)
        asymmetry = data.get('orderbook_asymmetry', 0.0)
        if direction == "UP":
            side_boost = asymmetry * 0.05
        else:
            side_boost = -asymmetry * 0.05
        est = base + lag_boost + tte_adj + side_boost
        return max(0.05, min(0.95, est))

    def check_constraints(self) -> bool:
        """Hard constraint check (§14)."""
        if abs(self.daily_pnl) >= MAX_DAILY_LOSS:
            logger.warning(f"[SHUTDOWN] Daily loss limit: ${self.daily_pnl:.2f}")
            return False
        if abs(self.weekly_pnl) >= MAX_WEEKLY_LOSS:
            logger.warning(f"[SHUTDOWN] Weekly loss limit: ${self.weekly_pnl:.2f}")
            return False
        if self.daily_trade_count >= MAX_DAILY_TRADES:
            logger.info(f"[LIMIT] Daily trade limit: {self.daily_trade_count}")
            return False
        return True

    def _write_jsonl(self, path: Path, records: list[dict]):
        """Append records to JSONL file."""
        with open(path, 'a') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')

    def _write_csv_row(self, row: list):
        """Append row to side score distribution CSV."""
        with open(self.csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)

    def _write_report(self, scan_data: dict):
        """§9: Write ranking report in markdown."""
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        top = scan_data.get('top_20', [])
        top_per_market = scan_data.get('top_per_market', {})

        lines = [
            f"# V21.5 Opportunity Ranking Report",
            f"",
            f"**Generated**: {ts} UTC",
            f"**Scan**: #{self.scan_count}",
            f"**Mode**: {self.mode}",
            f"**Markets scanned**: {scan_data.get('markets', 0)}",
            f"**Candidates scored**: {scan_data.get('candidates', 0)}",
            f"**Executable**: {scan_data.get('executable', 0)}",
            f"**Entries this scan**: {scan_data.get('entries', 0)}",
            f"**Forced paper entries**: {self.forced_entries}",
            f"",
            f"## Top 20 Opportunities",
            f"",
            f"| # | Asset | Int | Dir | Composite | EV | DirSc | MomSc | LagSc | TTESc | ExecSc | SideAdv | Decision |",
            f"|---|-------|-----|-----|-----------|-----|-------|-------|-------|-------|--------|---------|----------|",
        ]
        for i, opp in enumerate(top[:20]):
            lines.append(
                f"| {i+1} | {opp.asset} | {opp.interval} | {opp.direction} | "
                f"{opp.composite_score:.3f} | {opp.credible_ev:.4f} | "
                f"{opp.directional_score:.2f} | {opp.momentum_score:.2f} | "
                f"{opp.lag_score:.2f} | {opp.tte_score:.2f} | "
                f"{opp.execution_score:.2f} | {opp.relative_side_advantage:+.3f} | "
                f"{opp.execution_decision} |"
            )

        lines.append(f"")
        lines.append(f"## Top Side Per Market")
        lines.append(f"")
        for market_key, opps in top_per_market.items():
            if opps:
                best = opps[0]
                lines.append(
                    f"- **{market_key}**: {best.direction} "
                    f"(composite={best.composite_score:.3f}, "
                    f"side_adv={best.relative_side_advantage:+.3f})"
                )

        # Why not traded
        lines.extend([
            f"",
            f"## Why Top Opportunities Were Not Traded",
            f"",
        ])
        for opp in top[:5]:
            reasons = []
            if opp.credible_ev <= 0:
                reasons.append(f"EV≤0 ({opp.credible_ev:.4f})")
            if opp.execution_score < 0.1:
                reasons.append(f"spread too wide ({opp.spread:.4f})")
            if opp.tte_score < 0.3:
                reasons.append("early market window")
            if not opp.is_top_side:
                reasons.append(f"opposite side stronger")
            if opp.adversarial_score > 0.5:
                reasons.append(f"adversarial={opp.adversarial_score:.2f}")
            if not reasons:
                reasons.append("below entry threshold")

            lines.append(
                f"- **{opp.asset}-{opp.interval} {opp.direction}** "
                f"(composite={opp.composite_score:.3f}): {', '.join(reasons)}"
            )

        with open(self.report_path, 'w') as f:
            f.write('\n'.join(lines))

    def run_scan(self) -> dict:
        """Execute one scan cycle — §3: score every market, §15: dynamic ranking."""
        self.scan_count += 1
        markets = self.discover_markets()

        # §1: Score both UP and DOWN for every market
        all_candidates = []
        for market in markets:
            data = self.fetch_market_data(market)
            scores = self.score_market(market, data)
            all_candidates.extend(scores)

        # §3: Rank all opportunities (side selection applied internally)
        ranked, executable = self.ranker.rank_opportunities(all_candidates)

        # §15: Get top side per market
        top_per_market = self.ranker.get_top_per_market(ranked, n=1)

        # §4: Paper participation mode — force top-ranked if no normal trade fires
        now = time.time()
        forced_this_scan = False
        if (self.mode == "paper" and
                len(executable) == 0 and
                ranked and
                now - self.last_forced_entry_time >= PAPER_FORCE_INTERVAL):

            top = ranked[0]
            if top.composite_score >= 0.15 and self.check_constraints():
                top.execution_decision = "FORCED_TOP_RANKED_PAPER"
                self.forced_entries += 1
                self.entries_attempted += 1
                self.daily_trade_count += 1
                self.last_forced_entry_time = now
                forced_this_scan = True

                logger.info(
                    f"[FORCED-PAPER] {top.market_slug} {top.direction} "
                    f"composite={top.composite_score:.3f} EV={top.credible_ev:.4f} "
                    f"dir={top.directional_score:.2f} mom={top.momentum_score:.2f} "
                    f"exec={top.execution_score:.2f} profile={top.profile_id} "
                    f"side_adv={top.relative_side_advantage:+.3f} "
                    f"is_top_side={top.is_top_side}"
                )

                # Log forced entry
                self.forced_entry_log.append(top.to_dict())
                self._write_jsonl(self.forced_jsonl_path, [top.to_dict()])

        # Normal execution
        entries_this_scan = 0
        for opp in executable[:1]:  # Max 1 per scan (MAX_CONCURRENT=1)
            if opp.execution_decision == "FORCED_TOP_RANKED_PAPER":
                continue  # Already handled above

            # Timing assessment with persistence data
            persistence = {
                'velocity_30s': opp.spot_velocity_30s,
                'candle_direction': opp.candle_direction,
            }
            timing = self.timing.assess(
                time_to_expiry=opp.time_to_expiry,
                interval=opp.interval,
                price_directional_delta=opp.spot_velocity_30s,
                oracle_lag=opp.lag_score * 0.05,
                adversarial_score=opp.adversarial_score,
                spot_velocity=opp.spot_velocity_30s,
                no_movement=opp.candle_direction == "NEUTRAL" and opp.consecutive_directional_moves == 0,
            )

            if not timing.should_enter:
                logger.info(
                    f"[SKIP-TIMING] {opp.market_slug} {opp.direction} "
                    f"window={timing.window_name} priority={timing.entry_priority:.2f} "
                    f"final_120s={timing.final_120s} reason={timing.reason}"
                )
                continue

            if not self.check_constraints():
                continue

            self.entries_attempted += 1
            self.daily_trade_count += 1

            if opp.credible_ev > 0.01 or opp.composite_score > 0.30:
                self.entries_accepted += 1
                entries_this_scan += 1
                logger.info(
                    f"[ENTRY] {opp.market_slug} {opp.direction} "
                    f"EV={opp.credible_ev:.4f} "
                    f"composite={opp.composite_score:.3f} "
                    f"window={timing.window_name} "
                    f"priority={timing.entry_priority:.2f} "
                    f"profile={opp.profile_id} "
                    f"spread={opp.spread:.4f} "
                    f"side_adv={opp.relative_side_advantage:+.3f} "
                    f"is_top_side={opp.is_top_side} "
                    f"persistence={opp.candle_direction}/{opp.consecutive_directional_moves}consec "
                    f"approach={opp.price_approach}"
                )
            else:
                logger.info(
                    f"[QUEUE] {opp.market_slug} {opp.direction} "
                    f"EV={opp.credible_ev:.4f} composite={opp.composite_score:.3f} "
                    f"window={timing.window_name}"
                )

        # §16: Full output logging
        # Dashboard
        logger.info(
            f"[SCAN #{self.scan_count}] "
            f"Markets={len(markets)} "
            f"Candidates={len(all_candidates)} "
            f"Executable={len(executable)} "
            f"Accepted={self.entries_accepted} "
            f"ForcedPapers={self.forced_entries} "
            f"PnL=${self.settled_pnl:.2f} "
            f"Adv={self.adversarial_score:.2f}"
        )

        # Top 5 with side selection info
        for i, opp in enumerate(ranked[:5]):
            logger.info(
                f"  [#{i+1}] {opp.asset}-{opp.interval} {opp.direction} "
                f"composite={opp.composite_score:.3f} "
                f"EV={opp.credible_ev:.4f} "
                f"dir={opp.directional_score:.2f} "
                f"mom={opp.momentum_score:.2f} "
                f"lag={opp.lag_score:.2f} "
                f"tte={opp.tte_score:.2f} "
                f"exec={opp.execution_score:.2f} "
                f"side_adv={opp.relative_side_advantage:+.3f} "
                f"top_side={opp.is_top_side} "
                f"decision={opp.execution_decision}"
            )

        # §9: Write JSONL (top 20)
        top_20 = ranked[:20]
        jsonl_records = [opp.to_dict() for opp in top_20]
        # Add timestamp
        ts = datetime.now(timezone.utc).isoformat()
        for r in jsonl_records:
            r['timestamp'] = ts
            r['scan'] = self.scan_count
        self._write_jsonl(self.jsonl_path, jsonl_records)

        # §9: Write CSV rows (all candidates)
        for opp in all_candidates:
            self._write_csv_row([
                ts, self.scan_count, opp.asset, opp.interval, opp.direction,
                f"{opp.composite_score:.4f}", f"{opp.directional_score:.4f}",
                f"{opp.momentum_score:.4f}", f"{opp.lag_score:.4f}",
                f"{opp.volatility_score:.4f}", f"{opp.tte_score:.4f}",
                f"{opp.execution_score:.4f}", f"{opp.cross_asset_score:.4f}",
                f"{opp.rsi_context_score:.4f}", f"{opp.relative_side_advantage:.4f}",
                opp.is_top_side, f"{opp.credible_ev:.6f}",
                f"{opp.entry_price:.4f}", f"{opp.spread:.4f}",
                opp.execution_decision,
            ])

        # Build scan data for report
        scan_data = {
            'markets': len(markets),
            'candidates': len(all_candidates),
            'executable': len(executable),
            'entries': entries_this_scan,
            'top_20': top_20,
            'top_per_market': top_per_market,
        }

        # Write report every 10 scans
        if self.scan_count % 10 == 0:
            self._write_report(scan_data)

        return scan_data

    def run(self):
        """Main loop — continuous 15-second scan for duration_hours."""
        logger.info(f"[START] V21.5 Opportunity Extraction | {self.duration_hours}h | {self.mode}")
        logger.info(f"[START] {len(ASSETS)} assets × {len(INTERVALS)} intervals = {len(ASSETS)*len(INTERVALS)} market universes")
        logger.info(f"[START] Constraints: max=${MAX_POSITION} | max_pos={MAX_CONCURRENT} | daily_loss=${MAX_DAILY_LOSS}")
        logger.info(f"[START] Paper participation: forced entry every {PAPER_FORCE_INTERVAL}s if no normal trade fires")

        duration_secs = self.duration_hours * 3600
        scan_interval = 15  # §3: scan every 10-15 seconds

        while time.time() - self.start_time < duration_secs:
            try:
                self.run_scan()
            except Exception as e:
                logger.error(f"[ERROR] Scan failed: {e}")
                import traceback
                traceback.print_exc()

            time.sleep(scan_interval)

        logger.info(
            f"[DONE] V21.5 complete. "
            f"{self.entries_accepted} entries, {self.forced_entries} forced paper, "
            f"{self.scan_count} scans."
        )

        # Final report
        self._write_report({
            'markets': 0,
            'candidates': 0,
            'executable': 0,
            'entries': 0,
            'top_20': [],
            'top_per_market': {},
        })


def main():
    parser = argparse.ArgumentParser(description="V21.5 Opportunity Extraction Runner")
    parser.add_argument("--mode", default="paper", choices=["paper", "live"])
    parser.add_argument("--duration", type=float, default=5.0, help="Duration in hours")
    args = parser.parse_args()

    runner = V215Runner(mode=args.mode, duration_hours=args.duration)
    runner.run()


if __name__ == "__main__":
    main()