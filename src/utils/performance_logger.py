from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from threading import Lock
from collections import defaultdict
import json
import os
from .logger import get_logger

log = get_logger(__name__)

class PerformanceLogger:
    """Performance logging and tracking."""
    
    def __init__(self):
        """Initialize performance logger."""
        self.metrics: Dict[str, List[float]] = defaultdict(list)
        self.trades: List[Dict[str, Any]] = []
        self.lock = Lock()
        self.state_file = "state/performance_state.json"
        self.load_state()
        
    def log_trade(self, symbol: str, action: str, qty: float, price: float, notional: float, reward: float) -> None:
        """Log trade execution details.
        
        Args:
            symbol (str): Trading symbol
            action (str): Trade action (buy/sell)
            qty (float): Trade quantity
            price (float): Execution price
            notional (float): Trade notional value
            reward (float): Trade PnL
        """
        trade = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "action": action,
            "qty": qty,
            "price": price,
            "notional": notional,
            "reward": reward
        }
        
        with self.lock:
            self.trades.append(trade)
            if len(self.trades) > 1000:  # Keep last 1000 trades
                self.trades = self.trades[-1000:]
            
            # Update metrics
            self.record_metric("trade_pnl", reward)
            self.record_metric("trade_notional", notional)
            
        log.info(f"{action} {qty} {symbol} @ {price}, pnl={reward}")
        
    def record_metric(self, name: str, value: float) -> None:
        """Record a metric value."""
        with self.lock:
            self.metrics[name].append(value)
            if len(self.metrics[name]) > 1000:  # Keep last 1000 measurements
                self.metrics[name] = self.metrics[name][-1000:]
    
    def get_metric_stats(self, name: str) -> Dict[str, float]:
        """Get statistics for a metric."""
        with self.lock:
            values = self.metrics[name]
            if not values:
                return {}
            sorted_values = sorted(values)
            return {
                "min": min(values),
                "max": max(values),
                "mean": sum(values) / len(values),
                "p95": sorted_values[int(len(values) * 0.95)],
                "p99": sorted_values[int(len(values) * 0.99)]
            }
    
    def get_recent_trades(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Get recent trades."""
        cutoff = datetime.now() - timedelta(hours=hours)
        with self.lock:
            return [
                trade for trade in self.trades
                if datetime.fromisoformat(trade["timestamp"]) > cutoff
            ]
    
    def save_state(self) -> None:
        """Save performance state to file."""
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            state = {
                "metrics": self.metrics,
                "trades": self.trades,
                "last_save": datetime.now().isoformat()
            }
            with open(self.state_file, "w") as f:
                json.dump(state, f)
        except Exception as e:
            log.error(f"Failed to save performance state: {e}")
    
    def load_state(self) -> None:
        """Load performance state from file."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    state = json.load(f)
                    self.metrics = defaultdict(list, state["metrics"])
                    self.trades = state["trades"]
        except Exception as e:
            log.error(f"Failed to load performance state: {e}")

# Create global performance logger instance
performance_logger = PerformanceLogger()

# Export functions and classes
__all__ = ["log_trade", "performance_logger", "PerformanceLogger"] 