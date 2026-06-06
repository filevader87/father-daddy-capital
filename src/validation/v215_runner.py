"""V21.5 Opportunity Extraction Runner
=======================================
Continuous probabilistic opportunity extraction.
Score everything, reject little, rank aggressively, execute selectively.
15-second scan cycle, 4 assets × 2 intervals = 8+ market universes.
"""

from __future__ import annotations
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

        # Output
        self.output_dir = Path("output/v215")
        self.output_dir.mkdir(parents=True, exist_ok=True)

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

        # TTE scoring
        pct_elapsed = 1.0 - (tte / INTERVAL_SECS[interval])
        data['pct_elapsed'] = pct_elapsed

        return data

    def score_market(self, market: dict, data: dict) -> list[OpportunityScore]:
        """Score both UP and DOWN for a single market."""
        scores = []
        tte = data['tte']
        interval = data['interval']
        asset = data['asset']

        for direction in ["UP", "DOWN"]:
            # Entry price for this direction
            entry_price = data['up_ask'] if direction == "UP" else data['down_ask']

            # Directional score from asymmetry engine
            dir_score = self.ranker.score_directional(
                data['spot_delta'], direction
            )

            # Momentum score
            momentum_score = self.ranker.score_momentum(
                data.get('price_velocity', 0.0),
                data.get('volume', 0.0)
            )

            # Lag score from oracle data
            lag_score = self.ranker.score_lag(data.get('oracle_lag', 0.0))

            # Volatility score
            vol_score = self.ranker.score_volatility(
                data.get('volatility', 0.01), 0.01
            )

            # Time-to-expiry score
            tte_score = self.ranker.score_tte(tte, interval)

            # Execution score based on spread
            exec_score = self.ranker.score_execution(data['effective_spread'])

            # Cross-asset confirmation
            cross_score = self.ranker.score_cross_asset(asset)

            # RSI context score (soft, 5% weight)
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
        lag_boost = lag_score * 0.10
        tte_adj = (tte_score - 0.5) * 0.05
        est = base + lag_boost + tte_adj
        return max(0.05, min(0.95, est))

    def check_constraints(self) -> bool:
        """Hard constraint check (§14)."""
        if abs(self.daily_pnl) >= MAX_DAILY_LOSS:
            logger.warning(f"[SHUTDOWN] Daily loss limit reached: ${self.daily_pnl:.2f}")
            return False
        if abs(self.weekly_pnl) >= MAX_WEEKLY_LOSS:
            logger.warning(f"[SHUTDOWN] Weekly loss limit reached: ${self.weekly_pnl:.2f}")
            return False
        if self.daily_trade_count >= MAX_DAILY_TRADES:
            logger.info(f"[LIMIT] Daily trade limit reached: {self.daily_trade_count}")
            return False
        return True

    def run_scan(self) -> dict:
        """Execute one scan cycle."""
        self.scan_count += 1
        markets = self.discover_markets()

        all_candidates = []
        for market in markets:
            data = self.fetch_market_data(market)
            scores = self.score_market(market, data)
            all_candidates.extend(scores)

        # Rank all opportunities
        ranked, executable = self.ranker.rank_opportunities(all_candidates)

        # Try to execute top candidates
        entries_this_scan = 0
        for opp in executable[:1]:  # Max 1 per scan (MAX_CONCURRENT=1)
            # Timing assessment
            timing = self.timing.assess(
                time_to_expiry=opp.time_to_expiry,
                interval=opp.interval,
                price_directional_delta=0.0,
                oracle_lag=opp.lag_score * 0.05,
                adversarial_score=opp.adversarial_score,
            )

            if not timing.should_enter:
                logger.info(
                    f"[SKIP-TIMING] {opp.market_slug} {opp.direction} "
                    f"window={timing.window_name} priority={timing.entry_priority:.2f} "
                    f"reason={timing.reason}"
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
                    f"spread={opp.spread:.4f}"
                )
            else:
                logger.info(
                    f"[QUEUE] {opp.market_slug} {opp.direction} "
                    f"EV={opp.credible_ev:.4f} "
                    f"composite={opp.composite_score:.3f} "
                    f"window={timing.window_name}"
                )

        # Dashboard log
        logger.info(
            f"[SCAN #{self.scan_count}] "
            f"Markets={len(markets)} "
            f"Candidates={len(all_candidates)} "
            f"Executable={len(executable)} "
            f"Accepted={self.entries_accepted} "
            f"PnL=${self.settled_pnl:.2f} "
            f"Adv={self.adversarial_score:.2f}"
        )

        # Log top 5 opportunities
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
                f"decision={opp.execution_decision}"
            )

        return {
            'scan': self.scan_count,
            'markets': len(markets),
            'candidates': len(all_candidates),
            'executable': len(executable),
            'entries': entries_this_scan,
        }

    def run(self):
        """Main loop — continuous 15-second scan for duration_hours."""
        logger.info(f"[START] V21.5 Opportunity Extraction | {self.duration_hours}h | {self.mode}")
        logger.info(f"[START] {len(ASSETS)} assets × {len(INTERVALS)} intervals = {len(ASSETS)*len(INTERVALS)} market universes")
        logger.info(f"[START] Constraints: max=${MAX_POSITION} | max_pos={MAX_CONCURRENT} | daily_loss=${MAX_DAILY_LOSS}")

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
            f"{self.entries_accepted} entries in {self.scan_count} scans."
        )


def main():
    parser = argparse.ArgumentParser(description="V21.5 Opportunity Extraction Runner")
    parser.add_argument("--mode", default="paper", choices=["paper", "live"])
    parser.add_argument("--duration", type=float, default=5.0, help="Duration in hours")
    args = parser.parse_args()

    runner = V215Runner(mode=args.mode, duration_hours=args.duration)
    runner.run()


if __name__ == "__main__":
    main()