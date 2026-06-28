"""Oracle Lag Exploitation Module — §7.D V21
================================================
Continuously compares external spot movement against Polymarket repricing speed.
Delayed repricing = tradeable edge.
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from collections import deque
import time
import math


@dataclass
class OracleLagObservation:
    """A single oracle lag measurement."""
    timestamp: float
    asset: str
    spot_price: float
    spot_move_pct: float        # % move from reference
    contract_implied_p: float   # Polymarket implied probability
    expected_p: float           # What probability SHOULD be given spot move
    lag_seconds: float          # Estimated repricing delay
    lag_edge: float             # Edge from lag (expected_p - contract_implied_p)


@dataclass
class OracleLagStats:
    """Statistics for oracle lag by asset."""
    total_observations: int = 0
    avg_lag_seconds: float = 0.0
    max_lag_seconds: float = 0.0
    avg_edge: float = 0.0
    max_edge: float = 0.0
    edge_frequency: float = 0.0  # % of observations with edge > threshold
    last_spot_price: float = 0.0
    last_contract_p: float = 0.0
    reference_price: float = 0.0  # Price at start of measurement window
    
    # Rolling statistics
    recent_lags: deque = field(default_factory=lambda: deque(maxlen=100))
    recent_edges: deque = field(default_factory=lambda: deque(maxlen=100))


class OracleLagTracker:
    """Tracks repricing lag between spot and Polymarket contracts.
    
    The core insight: Polymarket UpDown contracts often lag spot moves,
    especially in the final 60-120 seconds. This creates a window where
    the contract implied probability hasn't caught up to reality.
    
    Methodology:
    1. Track external spot price movement from a reference point
    2. Compare against Polymarket contract implied probability  
    3. Compute expected probability from spot delta
    4. Edge = expected_p - contract_implied_p
    5. Only trade when edge > credible_threshold
    """
    
    EDGE_THRESHOLD = 0.05   # Minimum edge to consider (5% probability gap)
    LAG_WINDOW_SEC = 120    # Maximum lag to track (2 minutes)
    REFERENCE_RESET_SEC = 300  # Reset reference price every 5 minutes
    
    def __init__(self):
        self.stats: Dict[str, OracleLagStats] = {}
        self.observations: List[OracleLagObservation] = []
        
        # Reference prices — reset periodically
        self._reference_prices: Dict[str, float] = {}
        self._reference_times: Dict[str, float] = {}
    
    def update_reference(self, asset: str, spot_price: float):
        """Set or reset the reference price for lag calculation."""
        now = time.time()
        last_time = self._reference_times.get(asset, 0)
        
        if asset not in self._reference_prices or (now - last_time) > self.REFERENCE_RESET_SEC:
            self._reference_prices[asset] = spot_price
            self._reference_times[asset] = now
        
        if asset not in self.stats:
            self.stats[asset] = OracleLagStats()
        self.stats[asset].last_spot_price = spot_price
    
    def compute_oracle_lag(self, asset: str, spot_price: float,
                           contract_implied_p: float,
                           interval_sec: int = 300) -> Optional[OracleLagObservation]:
        """Compute current oracle lag and edge.
        
        Args:
            asset: BTC, ETH, SOL, XRP
            spot_price: Current external spot price
            contract_implied_p: Polymarket UP token price (0-1)
            interval_sec: Contract interval in seconds (300 or 900)
        
        Returns:
            OracleLagObservation with edge, or None if reference not set.
        """
        if asset not in self._reference_prices:
            self.update_reference(asset, spot_price)
            return None
        
        reference = self._reference_prices[asset]
        spot_move_pct = (spot_price - reference) / reference if reference > 0 else 0
        
        # Estimate expected probability from spot delta
        # For BTC: ~0.5% move → high probability of direction continuing
        # Simple model: probability follows sigmoid of normalized move
        # normalized_move = spot_move * sqrt(time_remaining / interval)
        now = time.time()
        time_elapsed = now - self._reference_times.get(asset, now)
        time_remaining_ratio = max(0.01, 1.0 - min(1.0, time_elapsed / interval_sec))
        
        # Scale move by time remaining (later = more certain)
        effective_move = spot_move_pct * (1.0 + (1.0 - time_remaining_ratio))
        
        # Sigmoid mapping: 0 move → 0.5 probability, large move → near 1.0
        # BTC 5m: 0.1% move → ~55% probability
        # BTC 5m: 0.5% move → ~75% probability  
        # BTC 5m: 1.0% move → ~90% probability
        k = 500  # Sensitivity parameter
        expected_p = 1.0 / (1.0 + math.exp(-k * effective_move))
        expected_p = max(0.01, min(0.99, expected_p))
        
        # Compute edge
        lag_edge = expected_p - contract_implied_p
        
        # Estimate repricing lag
        # If edge > 0, market hasn't caught up → lag exists
        # Approximate lag as proportional to edge magnitude
        lag_seconds = abs(lag_edge) * interval_sec * 2 if abs(lag_edge) > self.EDGE_THRESHOLD else 0
        
        obs = OracleLagObservation(
            timestamp=now,
            asset=asset,
            spot_price=spot_price,
            spot_move_pct=spot_move_pct,
            contract_implied_p=contract_implied_p,
            expected_p=expected_p,
            lag_seconds=lag_seconds,
            lag_edge=lag_edge,
        )
        
        # Update stats
        stats = self.stats[asset]
        stats.total_observations += 1
        stats.recent_lags.append(lag_seconds)
        stats.recent_edges.append(lag_edge)
        stats.avg_lag_seconds = sum(stats.recent_lags) / len(stats.recent_lags)
        stats.max_lag_seconds = max(stats.max_lag_seconds, lag_seconds)
        stats.avg_edge = sum(stats.recent_edges) / len(stats.recent_edges)
        stats.max_edge = max(stats.max_edge, lag_edge)
        stats.edge_frequency = sum(1 for e in stats.recent_edges if abs(e) > self.EDGE_THRESHOLD) / len(stats.recent_edges)
        stats.last_contract_p = contract_implied_p
        
        self.observations.append(obs)
        if len(self.observations) > 10000:
            self.observations = self.observations[-10000:]
        
        return obs
    
    def get_lag_report(self) -> Dict[str, Dict]:
        """Get oracle lag statistics for all assets."""
        report = {}
        for asset, stats in self.stats.items():
            report[asset] = {
                "total_observations": stats.total_observations,
                "avg_lag_seconds": round(stats.avg_lag_seconds, 2),
                "max_lag_seconds": round(stats.max_lag_seconds, 2),
                "avg_edge": round(stats.avg_edge, 4),
                "max_edge": round(stats.max_edge, 4),
                "edge_frequency": round(stats.edge_frequency, 3),
                "last_spot": round(stats.last_spot_price, 2),
                "last_contract_p": round(stats.last_contract_p, 4),
                "reference_price": round(self._reference_prices.get(asset, 0), 2),
            }
        return report
    
    def should_trade_oracle_lag(self, asset: str, config_threshold: float = 0.05) -> Tuple[bool, str, float]:
        """Determine if oracle lag creates a tradeable edge.
        
        Returns:
            (should_trade, direction, edge_magnitude)
        """
        stats = self.stats.get(asset)
        if not stats or stats.total_observations < 5:
            return False, "NONE", 0.0
        
        if not stats.recent_edges:
            return False, "NONE", 0.0
        
        recent_avg_edge = sum(list(stats.recent_edges)[-5:]) / 5
        
        if abs(recent_avg_edge) > config_threshold:
            direction = "UP" if recent_avg_edge > 0 else "DOWN"
            return True, direction, abs(recent_avg_edge)
        
        return False, "NONE", abs(recent_avg_edge)
    
    def reset_reference_if_stale(self, asset: str):
        """Reset reference price if stale."""
        now = time.time()
        last_time = self._reference_times.get(asset, 0)
        if (now - last_time) > self.REFERENCE_RESET_SEC:
            if asset in self._reference_prices:
                self._reference_prices[asset] = self.stats[asset].last_spot_price
                self._reference_times[asset] = now