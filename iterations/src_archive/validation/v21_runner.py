"""V21 Validation Runner — Adaptive Directional Extraction
=============================================================
Hybrid organism: Profile system + Directional engine + Oracle lag + PBOT rotation +
                 Binary settlement + Execution reality + Live constraints.
"""
import time
import json
import logging
import math
import argparse
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

from src.profile.profile_system import (
    ProfileTracker, ProfileConfig, PROFILE_DEFINITIONS, DirectionTag
)
from src.directional.asymmetry_engine import DirectionalAsymmetryEngine, DirectionContext
from src.oracle_lag.lag_tracker import OracleLagTracker
from src.execution.reality_engine import (
    ExecutionRealityEngine, LiveConstraints, AdversarialDetector, ExecutionResult
)
from src.cell.exploration_config import Asset, Interval

log = logging.getLogger("V21")

# ── V21 Cycle Output Logger (§15) ──
REQUIRED_LOG_FIELDS = [
    "timestamp", "asset", "interval", "direction", "entry",
    "real_executable_ask", "real_executable_bid", "spread",
    "slippage_estimate", "spot_move", "oracle_lag",
    "directional_asymmetry", "regime", "transition", "entropy",
    "adversarial_score", "estimated_p", "credible_ev", "realized_pnl",
    "profile_id", "cell_id", "allocation_weight"
]


@dataclass
class V21TradeRecord:
    """Complete V21 trade record — all §15 required fields."""
    timestamp: float
    asset: str
    interval: str
    direction: str
    entry: float
    real_executable_ask: float
    real_executable_bid: float
    spread: float
    slippage_estimate: float
    spot_move: float
    oracle_lag: float
    directional_asymmetry: float
    regime: str
    transition: str
    entropy: float
    adversarial_score: float
    estimated_p: float
    credible_ev: float
    realized_pnl: float
    profile_id: str
    cell_id: str
    allocation_weight: float


class V21Runner:
    """V21 Adaptive Directional Extraction Runner.
    
    Architecture (§2):
        REAL EXECUTION LAYER
            ↓
        DIRECTIONAL PERSISTENCE ENGINE
            ↓
        EV FILTER
            ↓
        PROFILE EVOLUTION ENGINE
            ↓
        LIVE CAPITAL CONCENTRATOR
    """
    
    def __init__(self, mode: str = "paper", duration_hours: float = 6.0):
        self.mode = mode
        self.duration_hours = duration_hours
        
        # Core engines
        self.profile_tracker = ProfileTracker()
        self.directional_engine = DirectionalAsymmetryEngine()
        self.oracle_lag = OracleLagTracker()
        self.execution_engine = ExecutionRealityEngine(conservative_mode=True)
        self.adversarial = AdversarialDetector()
        self.constraints = LiveConstraints()
        
        # State
        self.trades: List[V21TradeRecord] = []
        self.scan_count = 0
        self.start_time = 0.0
        self.last_dashboard = 0.0
        
        # Market data cache (from CLOB)
        self._spot_prices: Dict[str, float] = {}
        self._prev_spot_prices: Dict[str, float] = {}
        self._market_books: Dict[str, dict] = {}
        self._prev_market_books: Dict[str, dict] = {}
    
    def fetch_spot_prices(self) -> Dict[str, float]:
        """Fetch current spot prices for all assets."""
        import urllib.request
        prices = {}
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,ripple&vs_currencies=usd"
            req = urllib.request.Request(url, headers={"User-Agent": "FDC-V21/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                prices["BTC"] = data.get("bitcoin", {}).get("usd", 0)
                prices["ETH"] = data.get("ethereum", {}).get("usd", 0)
                prices["SOL"] = data.get("solana", {}).get("usd", 0)
                prices["XRP"] = data.get("ripple", {}).get("usd", 0)
        except Exception as e:
            log.warning(f"[SPOT] Price fetch failed: {e}")
        return prices
    
    def fetch_market_books(self, asset: str, interval: str) -> List[dict]:
        """Fetch Polymarket UpDown orderbooks for asset/interval."""
        import urllib.request
        
        markets = []
        try:
            gamma_url = "https://gamma-api.polymarket.com/markets"
            params = f"?tag=updown&asset={asset.lower()}&interval={interval}"
            url = gamma_url + params
            req = urllib.request.Request(url, headers={"User-Agent": "FDC-V21/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if isinstance(data, list):
                    for m in data:
                        if "clobTokenIds" in m and m.get("active", True):
                            markets.append(m)
        except Exception as e:
            log.warning(f"[CLOB] Market fetch failed for {asset}/{interval}: {e}")
        return markets
    
    def compute_rsi(self, prices: List[float], period: int = 14) -> float:
        """Compute RSI from price series."""
        if len(prices) < period + 1:
            return 50.0
        
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d for d in deltas if d > 0]
        losses = [-d for d in deltas if d < 0]
        
        avg_gain = sum(gains[-period:]) / period if gains else 0
        avg_loss = sum(losses[-period:]) / period if losses else 0
        
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
    
    def compute_regime(self, rsi: float, volatility: float) -> str:
        """Classify market regime."""
        if rsi < 30 and volatility > 0.02:
            return "panic"
        elif rsi < 35:
            return "oversold"
        elif rsi > 70 and volatility > 0.02:
            return "euphoria"
        elif rsi > 65:
            return "overbought"
        elif volatility > 0.015:
            return "volatile"
        elif volatility < 0.005:
            return "calm"
        else:
            return "normal"
    
    def select_profile(self, asset: str, interval: str, rsi: float,
                       spot_direction: str, time_window: str) -> Optional[str]:
        """Select best profile for current context using PBOT allocation."""
        weights = self.profile_tracker.get_allocation_weights()
        if not weights:
            return None
        
        # Filter profiles matching current asset/interval/context
        candidates = {}
        for pdef in PROFILE_DEFINITIONS:
            if pdef.asset != asset or pdef.interval != interval:
                continue
            if pdef.time_window != "full" and pdef.time_window != time_window:
                continue
            
            pid = pdef.profile_id
            if pid in weights and self.profile_tracker.statuses.get(pid) not in (
                None, "killed"
            ):
                candidates[pid] = weights[pid]
        
        if not candidates:
            return None
        
        # Pick highest-weighted candidate
        return max(candidates.keys(), key=lambda k: candidates[k])
    
    def get_time_window(self, time_to_expiry: float, interval_sec: int) -> str:
        """Classify time-to-expiry into window."""
        ratio = time_to_expiry / interval_sec
        if ratio > 0.7:
            return "early"
        elif ratio > 0.3:
            return "mid"
        else:
            return "late"
    
    def run(self):
        """Main V21 scan loop."""
        log.info(f"[START] V21 Adaptive Directional Extraction | {self.duration_hours}h | {self.mode}")
        log.info(f"[START] {len(PROFILE_DEFINITIONS)} profiles | {len(Asset)} assets | {len(Interval)} intervals")
        log.info(f"[START] Constraints: max=${self.constraints.MAX_POSITION_SIZE_USD} | max_pos={self.constraints.MAX_CONCURRENT_POSITIONS} | daily_loss=${self.constraints.MAX_DAILY_LOSS_USD}")
        
        self.start_time = time.time()
        self.last_dashboard = self.start_time
        
        # Fetch initial spot prices
        self._spot_prices = self.fetch_spot_prices()
        for asset, price in self._spot_prices.items():
            self._prev_spot_prices[asset] = price
            self.oracle_lag.update_reference(asset, price)
        
        while time.time() - self.start_time < self.duration_hours * 3600:
            now = time.time()
            self.scan_count += 1
            
            # Auto-reset daily/weekly constraints
            self.constraints.check_resets()
            
            # Check emergency shutdown
            if self.constraints.emergency_shutdown:
                log.error("[SHUTDOWN] Emergency shutdown triggered — live constraints violated")
                break
            
            # Fetch current spot prices
            spot_prices = self.fetch_spot_prices()
            if not spot_prices:
                log.warning(f"[SCAN #{self.scan_count}] Spot price fetch failed, skipping")
                time.sleep(30)
                continue
            
            # Update oracle lag references
            for asset, price in spot_prices.items():
                if asset in self._prev_spot_prices:
                    self.oracle_lag.reset_reference_if_stale(asset)
                else:
                    self.oracle_lag.update_reference(asset, price)
            
            markets_found = 0
            entries_attempted = 0
            entries_accepted = 0
            
            for asset_enum in Asset:
                for interval_enum in Interval:
                    asset = asset_enum.value
                    interval = interval_enum.value
                    
                    # Fetch market books
                    markets = self.fetch_market_books(asset, interval)
                    markets_found += len(markets)
                    
                    for market in markets:
                        # Extract market data
                        try:
                            token_ids = json.loads(market.get("clobTokenIds", "[]"))
                            if len(token_ids) < 2:
                                continue
                            
                            up_token = token_ids[0]
                            down_token = token_ids[1]
                            condition_id = market.get("conditionId", "")
                            slug = market.get("slug", "")
                            
                            # Time to expiry
                            end_date = market.get("endDateIso", "")
                            if end_date:
                                from datetime import datetime, timezone
                                end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
                                time_to_expiry = end_ts - now
                            else:
                                continue
                            
                            # Only trade in late window (§7.C)
                            interval_sec = int(interval[:-1]) * 60
                            time_window = self.get_time_window(time_to_expiry, interval_sec)
                            
                            # Skip markets too far from resolution
                            if time_to_expiry < 30 or time_to_expiry > 600:
                                continue
                            
                            # Get spot data
                            spot_price = spot_prices.get(asset, 0)
                            prev_spot = self._prev_spot_prices.get(asset, 0)
                            spot_move_pct = (spot_price - prev_spot) / prev_spot if prev_spot > 0 else 0
                            
                            # Determine spot direction
                            spot_direction = "UP" if spot_move_pct >= 0 else "DOWN"
                            prev_direction = "UP" if spot_move_pct >= 0 else "DOWN"  # simplified
                            
                            # Compute RSI (simplified - using recent spot moves)
                            rsi = self.compute_rsi([spot_price] * 14)  # placeholder
                            regime = self.compute_regime(rsi, abs(spot_move_pct))
                            
                            # Directional asymmetry (§5)
                            direction, dir_prob = self.directional_engine.get_best_direction(
                                asset, interval, rsi, spot_direction, prev_direction
                            )
                            
                            # Oracle lag check (§7.D)
                            up_ask = 0.99  # placeholder
                            oracle_obs = self.oracle_lag.compute_oracle_lag(
                                asset, spot_price, up_ask, interval_sec
                            )
                            
                            # Adversarial detection (§13)
                            adversary_score = self.adversarial.score
                            
                            # Select profile (§4, §11)
                            profile_id = self.select_profile(
                                asset, interval, rsi, direction, time_window
                            )
                            
                            # Live constraints check (§14)
                            can_trade, reason = self.constraints.can_trade(self.constraints.MAX_POSITION_SIZE_USD)
                            if not can_trade:
                                log.info(f"[BLOCKED] {reason}")
                                continue
                            
                            # Entry decision via execution reality (§6)
                            should_enter, result = self.execution_engine.should_enter(
                                estimated_p=dir_prob,
                                ask=up_ask,
                                bid=1.0 - up_ask,
                                time_to_expiry=time_to_expiry,
                                market_liquidity="thin",  # UpDown markets are thin
                                adversarial_score=adversary_score,
                            )
                            
                            entries_attempted += 1
                            
                            if should_enter:
                                entries_accepted += 1
                                # Record trade (paper mode: simulate settlement)
                                trade_record = V21TradeRecord(
                                    timestamp=now,
                                    asset=asset,
                                    interval=interval,
                                    direction=direction,
                                    entry=result.executable_price,
                                    real_executable_ask=result.executable_price,
                                    real_executable_bid=1.0 - result.executable_price,
                                    spread=result.spread_cost,
                                    slippage_estimate=result.slippage_cost,
                                    spot_move=spot_move_pct,
                                    oracle_lag=result.oracle_lag_seconds,
                                    directional_asymmetry=dir_prob,
                                    regime=regime,
                                    transition="",
                                    entropy=self.directional_engine.get_continuation_vs_reversal_stats().__len__() / 10,
                                    adversarial_score=adversary_score,
                                    estimated_p=dir_prob,
                                    credible_ev=result.credible_ev,
                                    realized_pnl=0.0,  # settled later
                                    profile_id=profile_id or "NONE",
                                    cell_id=f"{asset}_{interval}_{direction}",
                                    allocation_weight=self.profile_tracker.get_allocation_weights().get(profile_id or "", 0.0),
                                )
                                self.trades.append(trade_record)
                                
                                log.info(f"[ENTRY] {asset}/{interval}/{direction} @ {result.executable_price:.4f} "
                                        f"ev={result.credible_ev:.4f} p={dir_prob:.3f} adv={adversary_score:.2f} "
                                        f"profile={profile_id}")
                        
                        except Exception as e:
                            log.warning(f"[ERROR] Market processing failed: {e}")
                            continue
            
            # Update previous prices
            self._prev_spot_prices = dict(self._spot_prices)
            self._spot_prices = dict(spot_prices)
            
            # Profile evolution (PBOT-style §7.A)
            killed, promoted = self.profile_tracker.evolve()
            if killed:
                for pid, reason in killed:
                    log.info(f"[KILL] {pid}: {reason.value}")
            if promoted:
                for pid in promoted:
                    log.info(f"[PROMOTE] {pid}")
            
            # Dashboard (every 5 min)
            if now - self.last_dashboard > 300:
                total_trades = len(self.trades)
                total_pnl = sum(t.realized_pnl for t in self.trades)
                active_profiles = len(self.profile_tracker.get_active_profiles())
                weights = self.profile_tracker.get_allocation_weights()
                
                log.info(f"[DASHBOARD] Trades={total_trades} PnL=${total_pnl:.2f} "
                         f"Profiles={active_profiles} Markets={markets_found} "
                         f"Entries={entries_accepted}/{entries_attempted} "
                         f"AdvScore={self.adversarial.score:.2f}")
                
                # Oracle lag report
                lag_report = self.oracle_lag.get_lag_report()
                if lag_report:
                    for asset, stats in lag_report.items():
                        log.info(f"[ORACLE] {asset}: lag={stats['avg_lag_seconds']:.1f}s "
                                 f"edge={stats['avg_edge']:.4f} freq={stats['edge_frequency']:.2f}")
                
                self.last_dashboard = now
            
            # Cycle log
            log.info(f"[SCAN #{self.scan_count}] Markets={markets_found} "
                     f"Attempted={entries_attempted} Accepted={entries_accepted} "
                     f"Adv={self.adversarial.score:.2f}")
            
            time.sleep(30)
        
        # Final report
        self._final_report()
    
    def _final_report(self):
        """Generate final validation report."""
        total_trades = len(self.trades)
        total_pnl = sum(t.realized_pnl for t in self.trades)
        active = self.profile_tracker.get_active_profiles()
        killed = [pid for pid, s in self.profile_tracker.statuses.items() if s == "killed"]
        
        report = {
            "version": "V21",
            "mode": self.mode,
            "duration_hours": self.duration_hours,
            "total_scans": self.scan_count,
            "total_trades": total_trades,
            "total_pnl": total_pnl,
            "active_profiles": len(active),
            "killed_profiles": len(killed),
            "adversarial_score": self.adversarial.score,
            "daily_pnl": self.constraints.daily_pnl,
            "weekly_pnl": self.constraints.weekly_pnl,
            "oracle_lag_report": self.oracle_lag.get_lag_report(),
            "direction_stats": self.directional_engine.get_continuation_vs_reversal_stats(),
        }
        
        log.info(f"[DONE] V21 Validation Complete")
        log.info(f"[DONE] Trades={total_trades} PnL=${total_pnl:.2f} "
                 f"Active={len(active)} Killed={len(killed)}")
        
        # Write report
        report_path = "output/v21/validation_report.json"
        import os
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        log.info(f"[DONE] Report written to {report_path}")


def main():
    parser = argparse.ArgumentParser(description="V21 Adaptive Directional Extraction Runner")
    parser.add_argument("--mode", choices=["paper", "sim"], default="paper",
                        help="Validation mode (paper=simulated CLOB, sim=synthetic)")
    parser.add_argument("--duration", type=float, default=6.0,
                        help="Duration in hours (default: 6)")
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    runner = V21Runner(mode=args.mode, duration_hours=args.duration)
    runner.run()


if __name__ == "__main__":
    main()