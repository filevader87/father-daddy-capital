import os
import time
import json
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from threading import Lock
import traceback
from dataclasses import dataclass, asdict
import requests
from src.logger import logger

@dataclass
class Alert:
    """Alert data structure"""
    level: str  # 'info', 'warning', 'error', 'critical'
    message: str
    timestamp: datetime
    source: str
    details: Dict[str, Any]

class MonitoringSystem:
    def __init__(self):
        self.alerts: List[Alert] = []
        self.metrics: Dict[str, List[float]] = {}
        self.lock = Lock()
        self.alert_webhook = os.getenv('ALERT_WEBHOOK_URL')
        self.alert_thresholds = {
            'error_rate': 0.05,  # 5% error rate threshold
            'latency_p95': 0.5,  # 500ms p95 latency threshold
            'circuit_breaker_activations': 3,  # Number of circuit breaker activations
            'high_price_impact': 0.02,  # 2% price impact threshold
        }
    
    def record_metric(self, name: str, value: float) -> None:
        """Record a metric value"""
        with self.lock:
            if name not in self.metrics:
                self.metrics[name] = []
            self.metrics[name].append(value)
            # Keep last 1000 measurements
            if len(self.metrics[name]) > 1000:
                self.metrics[name] = self.metrics[name][-1000:]
    
    def get_metric_stats(self, name: str) -> Dict[str, float]:
        """Get statistics for a metric"""
        with self.lock:
            values = self.metrics.get(name, [])
            if not values:
                return {}
            
            sorted_values = sorted(values)
            return {
                'min': min(values),
                'max': max(values),
                'mean': sum(values) / len(values),
                'p95': sorted_values[int(len(values) * 0.95)],
                'p99': sorted_values[int(len(values) * 0.99)]
            }
    
    def create_alert(
        self,
        level: str,
        message: str,
        source: str,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Create and send an alert"""
        alert = Alert(
            level=level,
            message=message,
            timestamp=datetime.now(),
            source=source,
            details=details or {}
        )
        
        # Add to local alerts
        with self.lock:
            self.alerts.append(alert)
            # Keep last 1000 alerts
            if len(self.alerts) > 1000:
                self.alerts = self.alerts[-1000:]
        
        # Log alert
        logger.log(
            getattr(logger, level.upper()),
            f"Alert: {message} (Source: {source})"
        )
        
        # Send to webhook if configured
        if self.alert_webhook:
            try:
                requests.post(
                    self.alert_webhook,
                    json=asdict(alert),
                    timeout=5
                )
            except Exception as e:
                logger.error(f"Failed to send alert to webhook: {e}")
    
    def check_metrics(self) -> None:
        """Check metrics against thresholds and create alerts if needed"""
        # Check error rate
        error_rate = self._calculate_error_rate()
        if error_rate > self.alert_thresholds['error_rate']:
            self.create_alert(
                'warning',
                f"High error rate detected: {error_rate:.2%}",
                'monitoring',
                {'error_rate': error_rate}
            )
        
        # Check latency
        latency_stats = self.get_metric_stats('order_latency')
        if latency_stats.get('p95', 0) > self.alert_thresholds['latency_p95']:
            self.create_alert(
                'warning',
                f"High latency detected: p95={latency_stats['p95']:.3f}s",
                'monitoring',
                latency_stats
            )
        
        # Check circuit breaker activations
        circuit_breaker_stats = self.get_metric_stats('circuit_breaker_activations')
        if circuit_breaker_stats.get('count', 0) > self.alert_thresholds['circuit_breaker_activations']:
            self.create_alert(
                'error',
                f"Multiple circuit breaker activations: {circuit_breaker_stats['count']}",
                'monitoring',
                circuit_breaker_stats
            )
        
        # Check price impact
        price_impact_stats = self.get_metric_stats('price_impact')
        if price_impact_stats.get('max', 0) > self.alert_thresholds['high_price_impact']:
            self.create_alert(
                'warning',
                f"High price impact detected: {price_impact_stats['max']:.2%}",
                'monitoring',
                price_impact_stats
            )
    
    def _calculate_error_rate(self) -> float:
        """Calculate current error rate"""
        with self.lock:
            total_requests = len(self.metrics.get('request_count', []))
            if total_requests == 0:
                return 0.0
            error_count = len(self.metrics.get('error_count', []))
            return error_count / total_requests
    
    def get_recent_alerts(
        self,
        level: Optional[str] = None,
        source: Optional[str] = None,
        minutes: int = 60
    ) -> List[Alert]:
        """Get recent alerts with optional filtering"""
        cutoff = datetime.now() - timedelta(minutes=minutes)
        with self.lock:
            return [
                alert for alert in self.alerts
                if alert.timestamp >= cutoff
                and (level is None or alert.level == level)
                and (source is None or alert.source == source)
            ]
    
    def save_state(self, filepath: str) -> None:
        """Save monitoring state to file"""
        try:
            state = {
                'metrics': self.metrics,
                'alerts': [asdict(alert) for alert in self.alerts]
            }
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            logger.error(f"Failed to save monitoring state: {e}")
            logger.error(traceback.format_exc())
    
    def load_state(self, filepath: str) -> None:
        """Load monitoring state from file"""
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    state = json.load(f)
                    self.metrics = state.get('metrics', {})
                    self.alerts = [
                        Alert(**alert_data)
                        for alert_data in state.get('alerts', [])
                    ]
        except Exception as e:
            logger.error(f"Failed to load monitoring state: {e}")
            logger.error(traceback.format_exc())

# Global monitoring instance
monitoring = MonitoringSystem() 