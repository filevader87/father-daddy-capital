"""
Monitoring Module
---------------
This module contains monitoring and logging tools for the trading system.
"""

import os
import json
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from threading import RLock
from collections import defaultdict
import requests
from src.logger import logger
from src.config import TradingConfig

class MonitoringSystem:
    """System monitoring and alerting."""
    
    def __init__(self):
        """Initialize monitoring system."""
        self.metrics: Dict[str, List[float]] = defaultdict(list)
        self.alerts: List[Dict[str, Any]] = []
        self.lock = RLock()
        self.alert_webhook: Optional[str] = os.getenv("ALERT_WEBHOOK")
        self.config = TradingConfig.load_from_file()
        
    def record_metric(self, name: str, value: float) -> None:
        """Record a metric value."""
        with self.lock:
            self.metrics[name].append(value)
            if len(self.metrics[name]) > 1000:  # Keep last 1000 measurements
                self.metrics[name] = self.metrics[name][-1000:]
            
            # Check thresholds
            self.check_metrics(name)
    
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
    
    def create_alert(self, level: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Create and send an alert."""
        alert = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message,
            "data": data or {}
        }
        
        with self.lock:
            self.alerts.append(alert)
            if len(self.alerts) > 1000:  # Keep last 1000 alerts
                self.alerts = self.alerts[-1000:]
        
        # Log alert
        logger.warning(f"Alert: {message}")
        
        # Send to webhook if configured
        if self.alert_webhook:
            try:
                requests.post(self.alert_webhook, json=alert)
            except Exception as e:
                logger.error(f"Failed to send alert to webhook: {e}")
    
    def get_recent_alerts(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Get recent alerts."""
        cutoff = datetime.now() - timedelta(hours=hours)
        with self.lock:
            return [
                alert for alert in self.alerts
                if datetime.fromisoformat(alert["timestamp"]) > cutoff
            ]

    def _calculate_error_rate(self) -> float:
        with self.lock:
            request_count = len(self.metrics.get("request_count", []))
            if request_count == 0:
                return 0.0
            return len(self.metrics.get("error_count", [])) / request_count
    
    def check_metrics(self, name: str) -> None:
        """Check metrics against thresholds."""
        stats = self.get_metric_stats(name)
        if not stats:
            return
            
        # Check error rate
        if name == "error_rate" and stats["mean"] > 0.1:  # 10% error rate
            self.create_alert(
                "error",
                f"High error rate detected: {stats['mean']:.2%}",
                {"metric": name, "stats": stats}
            )
        
        # Check latency
        if name == "latency" and stats["p95"] > 1000:  # 1 second
            self.create_alert(
                "warning",
                f"High latency detected: p95={stats['p95']:.2f}ms",
                {"metric": name, "stats": stats}
            )
        
        # Check circuit breaker activations
        if name == "circuit_breaker_activations" and stats["mean"] > 5:
            self.create_alert(
                "error",
                f"Multiple circuit breaker activations: {stats['mean']:.1f}",
                {"metric": name, "stats": stats}
            )
        
        # Check price impact
        if name == "price_impact" and stats["mean"] > 0.01:  # 1%
            self.create_alert(
                "warning",
                f"High price impact detected: {stats['mean']:.2%}",
                {"metric": name, "stats": stats}
            )
    
    def save_state(self, file_path: str = "state/monitoring_state.json") -> None:
        """Save monitoring state to file."""
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            state = {
                "metrics": self.metrics,
                "alerts": self.alerts,
                "last_save": datetime.now().isoformat()
            }
            with open(file_path, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.error(f"Failed to save monitoring state: {e}")
    
    def load_state(self, file_path: str = "state/monitoring_state.json") -> None:
        """Load monitoring state from file."""
        try:
            if os.path.exists(file_path):
                with open(file_path, "r") as f:
                    state = json.load(f)
                    self.metrics = defaultdict(list, state["metrics"])
                    self.alerts = state["alerts"]
        except Exception as e:
            logger.error(f"Failed to load monitoring state: {e}")

# Create global monitoring instance
monitoring = MonitoringSystem()

# Export monitoring system
__all__ = ["monitoring", "MonitoringSystem"]

# This file makes the directory a Python package 
