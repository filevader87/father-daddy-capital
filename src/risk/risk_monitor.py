"""
Risk Monitor
-----------
This module implements the risk monitoring system.
"""

import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime
import asyncio
from enum import Enum
import json
from pathlib import Path
from src.config import TradingConfig

# Load configuration
config = TradingConfig.load_from_file()

class RiskLevel(Enum):
    """Risk level classifications."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

@dataclass
class RiskAlert:
    """Risk alert data structure."""
    timestamp: datetime
    symbol: str
    risk_level: RiskLevel
    metric: str
    value: float
    threshold: float
    message: str
    action_taken: Optional[str] = None

class RiskMonitor:
    """Risk monitoring system for tracking and analyzing risk metrics."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the risk monitor.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        self.config = self._load_config(config)
        self.logger = logging.getLogger("RiskMonitor")
        self.metrics = {}
        self.alerts = []
        self.active_alerts: Dict[str, List[RiskAlert]] = {}
        self.alert_history: List[RiskAlert] = []
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}
        self.risk_metrics: Dict[str, Dict[str, float]] = {}
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate risk monitoring configuration."""
        default_config = {
            'monitoring': {
                'check_interval': 60,
                'alert_threshold': 0.8,
                'metrics_interval': 300,
                'log_level': 'INFO'
            },
            'risk_metrics': {
                'var_confidence': 0.95,
                'max_var': 0.02,
                'max_volatility': 0.3,
                'min_sharpe': 1.0
            },
            'position_limits': {
                'max_position_size': config.MAX_POSITION_SIZE,
                'max_leverage': config.MAX_LEVERAGE,
                'max_drawdown': config.MAX_DRAWDOWN,
                'max_correlation': 0.7
            }
        }
        
        if config:
            default_config.update(config)
        return default_config
        
    async def start_monitoring(self, symbol: str):
        """Start monitoring a symbol."""
        if symbol in self.monitoring_tasks:
            return
            
        self.monitoring_tasks[symbol] = asyncio.create_task(
            self._monitor_symbol(symbol)
        )
        
    async def stop_monitoring(self, symbol: str):
        """Stop monitoring a symbol."""
        if symbol in self.monitoring_tasks:
            self.monitoring_tasks[symbol].cancel()
            del self.monitoring_tasks[symbol]
            
    async def _monitor_symbol(self, symbol: str):
        """Monitor risk metrics for a symbol."""
        while True:
            try:
                await self._check_risk_metrics(symbol)
                await asyncio.sleep(60)  # Check every minute
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error monitoring {symbol}: {e}")
                await asyncio.sleep(60)
                
    async def _check_risk_metrics(self, symbol: str):
        """Check risk metrics and generate alerts."""
        metrics = self._get_current_metrics(symbol)
        self.risk_metrics[symbol] = metrics
        
        for metric, value in metrics.items():
            if metric in self.config['risk_metrics']:
                risk_level = self._determine_risk_level(metric, value)
                if risk_level != RiskLevel.LOW:
                    await self._generate_alert(symbol, metric, value, risk_level)
                    
    def _get_current_metrics(self, symbol: str) -> Dict[str, float]:
        """Get current risk metrics for a symbol."""
        # This should be implemented to get real metrics from your risk manager
        return {
            'position_risk': 0.15,
            'drawdown': 0.08,
            'volatility': 0.25,
            'concentration': 0.2,
            'correlation': 0.6
        }
        
    def _determine_risk_level(self, metric: str, value: float) -> RiskLevel:
        """Determine risk level based on metric value."""
        thresholds = self.config['risk_metrics']
        
        if value >= thresholds['critical']:
            return RiskLevel.CRITICAL
        elif value >= thresholds['high']:
            return RiskLevel.HIGH
        elif value >= thresholds['medium']:
            return RiskLevel.MEDIUM
        else:
            return RiskLevel.LOW
            
    async def _generate_alert(self, 
                            symbol: str,
                            metric: str,
                            value: float,
                            risk_level: RiskLevel):
        """Generate a risk alert."""
        alert = RiskAlert(
            timestamp=datetime.now(),
            symbol=symbol,
            risk_level=risk_level,
            metric=metric,
            value=value,
            threshold=self.config['risk_metrics'][risk_level.value],
            message=f"{metric} risk level: {risk_level.value} (value: {value:.2f})"
        )
        
        # Add to active alerts
        if symbol not in self.active_alerts:
            self.active_alerts[symbol] = []
        self.active_alerts[symbol].append(alert)
        
        # Add to history
        self.alert_history.append(alert)
        
        # Take action based on risk level
        await self._handle_risk_alert(alert)
        
    async def _handle_risk_alert(self, alert: RiskAlert):
        """Handle a risk alert with appropriate actions."""
        if alert.risk_level == RiskLevel.CRITICAL:
            alert.action_taken = "Position reduced by 50%"
            # Implement critical risk actions
        elif alert.risk_level == RiskLevel.HIGH:
            alert.action_taken = "Position reduced by 25%"
            # Implement high risk actions
        elif alert.risk_level == RiskLevel.MEDIUM:
            alert.action_taken = "Monitoring increased"
            # Implement medium risk actions
            
    def get_active_alerts(self, symbol: Optional[str] = None) -> List[RiskAlert]:
        """Get active alerts for a symbol or all symbols."""
        if symbol:
            return self.active_alerts.get(symbol, [])
        return [alert for alerts in self.active_alerts.values() for alert in alerts]
        
    def get_alert_history(self, 
                         symbol: Optional[str] = None,
                         start_time: Optional[datetime] = None,
                         end_time: Optional[datetime] = None) -> List[RiskAlert]:
        """Get alert history with optional filters."""
        history = self.alert_history
        
        if symbol:
            history = [alert for alert in history if alert.symbol == symbol]
            
        if start_time:
            history = [alert for alert in history if alert.timestamp >= start_time]
            
        if end_time:
            history = [alert for alert in history if alert.timestamp <= end_time]
            
        return history
        
    def get_risk_metrics(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Get current risk metrics."""
        if symbol:
            return self.risk_metrics.get(symbol, {})
        return self.risk_metrics
        
    def clear_alerts(self, symbol: Optional[str] = None):
        """Clear alerts for a symbol or all symbols."""
        if symbol:
            self.active_alerts.pop(symbol, None)
        else:
            self.active_alerts.clear()
            
    def save_alert_history(self, path: str):
        """Save alert history to a file."""
        with open(path, 'w') as f:
            json.dump(
                [alert.__dict__ for alert in self.alert_history],
                f,
                default=str
            )
            
    def load_alert_history(self, path: str):
        """Load alert history from a file."""
        with open(path) as f:
            alerts = json.load(f)
            self.alert_history = [
                RiskAlert(
                    timestamp=datetime.fromisoformat(alert['timestamp']),
                    symbol=alert['symbol'],
                    risk_level=RiskLevel(alert['risk_level']),
                    metric=alert['metric'],
                    value=alert['value'],
                    threshold=alert['threshold'],
                    message=alert['message'],
                    action_taken=alert.get('action_taken')
                )
                for alert in alerts
            ] 