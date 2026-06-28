"""
Unified Configuration Manager
----------------------------
Single configuration system for Father Daddy Capital
"""

import yaml
import os
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

@dataclass
class TradingConfig:
    """Trading configuration dataclass."""
    mode: str
    symbols: list
    timeframes: list
    risk: Dict[str, Any]
    execution: Dict[str, Any]
    portfolio: Dict[str, Any]

@dataclass
class MonitoringConfig:
    """Monitoring configuration dataclass."""
    log_level: str
    metrics_enabled: bool
    health_check_interval: int
    prometheus_port: int
    alerts: Dict[str, Any]

@dataclass
class AgentConfig:
    """Agent configuration dataclass."""
    strategy_type: str
    asset_class: str
    parallel_processing: bool
    max_workers: int
    memory_optimization: bool
    signal_threshold: float
    signal_cooldown: int

class UnifiedConfigManager:
    """Unified configuration manager for the entire system."""
    
    def __init__(self, config_path: Optional[str] = None):
        """Initialize configuration manager.
        
        Args:
            config_path: Path to configuration file. Defaults to config/trading.yaml
        """
        if config_path is None:
            config_path = os.path.join(
                Path(__file__).parent.parent.parent, 
                "config", 
                "trading.yaml"
            )
        
        self.config_path = Path(config_path)
        self._config = {}
        self._load_config()
    
    def _load_config(self) -> None:
        """Load configuration from YAML file."""
        try:
            if not self.config_path.exists():
                raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
            
            with open(self.config_path, 'r') as f:
                self._config = yaml.safe_load(f)
            
            logger.info(f"Configuration loaded from {self.config_path}")
            
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            raise
    
    def reload(self) -> None:
        """Reload configuration from file."""
        self._load_config()
        logger.info("Configuration reloaded")
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value using dot notation.
        
        Args:
            key: Configuration key (e.g., 'trading.risk.max_position_size')
            default: Default value if key not found
            
        Returns:
            Configuration value
        """
        keys = key.split('.')
        value = self._config
        
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default
    
    def get_trading_config(self) -> TradingConfig:
        """Get trading configuration as dataclass."""
        trading = self.get('trading', {})
        return TradingConfig(
            mode=trading.get('mode', 'paper'),
            symbols=trading.get('symbols', []),
            timeframes=trading.get('timeframes', []),
            risk=trading.get('risk', {}),
            execution=trading.get('execution', {}),
            portfolio=trading.get('portfolio', {})
        )
    
    def get_monitoring_config(self) -> MonitoringConfig:
        """Get monitoring configuration as dataclass."""
        monitoring = self.get('monitoring', {})
        return MonitoringConfig(
            log_level=monitoring.get('log_level', 'INFO'),
            metrics_enabled=monitoring.get('metrics_enabled', True),
            health_check_interval=monitoring.get('health_check_interval', 60),
            prometheus_port=monitoring.get('prometheus_port', 8000),
            alerts=monitoring.get('alerts', {})
        )
    
    def get_agent_config(self) -> AgentConfig:
        """Get agent configuration as dataclass."""
        agents = self.get('agents', {})
        return AgentConfig(
            strategy_type=agents.get('strategy_type', 'momentum'),
            asset_class=agents.get('asset_class', 'crypto'),
            parallel_processing=agents.get('parallel_processing', True),
            max_workers=agents.get('max_workers', 4),
            memory_optimization=agents.get('memory_optimization', True),
            signal_threshold=agents.get('signal_threshold', 0.7),
            signal_cooldown=agents.get('signal_cooldown', 300)
        )
    
    def is_paper_trading(self) -> bool:
        """Check if system is in paper trading mode."""
        return self.get('trading.mode') == 'paper'
    
    def is_live_trading(self) -> bool:
        """Check if system is in live trading mode."""
        return self.get('trading.mode') == 'live'
    
    def is_backtest_mode(self) -> bool:
        """Check if system is in backtest mode."""
        return self.get('trading.mode') == 'backtest'
    
    def get_risk_limits(self) -> Dict[str, float]:
        """Get risk management limits."""
        return self.get('trading.risk', {})
    
    def get_symbols(self) -> list:
        """Get trading symbols."""
        return self.get('trading.symbols', [])
    
    def get_timeframes(self) -> list:
        """Get trading timeframes."""
        return self.get('trading.timeframes', [])
    
    def get_initial_cash(self) -> float:
        """Get initial cash amount."""
        return self.get('trading.portfolio.initial_cash', 100000)
    
    def get_max_position_size(self) -> float:
        """Get maximum position size."""
        return self.get('trading.risk.max_position_size', 0.1)
    
    def get_signal_threshold(self) -> float:
        """Get signal confidence threshold."""
        return self.get('agents.signal_threshold', 0.7)
    
    def get_database_config(self) -> Dict[str, Any]:
        """Get database configuration."""
        return self.get('database', {})
    
    def get_api_config(self) -> Dict[str, Any]:
        """Get API configuration."""
        return self.get('api', {})
    
    def get_development_config(self) -> Dict[str, Any]:
        """Get development configuration."""
        return self.get('development', {})

# Global configuration instance
config = UnifiedConfigManager()

# Convenience functions
def get_config() -> UnifiedConfigManager:
    """Get global configuration instance."""
    return config

def reload_config() -> None:
    """Reload global configuration."""
    config.reload()

def get_trading_config() -> TradingConfig:
    """Get trading configuration."""
    return config.get_trading_config()

def get_monitoring_config() -> MonitoringConfig:
    """Get monitoring configuration."""
    return config.get_monitoring_config()

def get_agent_config() -> AgentConfig:
    """Get agent configuration."""
    return config.get_agent_config()