from src.utils.market_regime import detect_market_regime
# Placeholder: configuration loader potentially referencing regime analysis

import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass

class ConfigurationError(Exception):
    """Custom exception for configuration errors."""
    pass

@dataclass
class MonitoringConfig:
    log_dir: str
    log_level: str
    metrics_port: int
    prometheus_job: str
    alert_thresholds: Dict[str, float]
    min_regime_confidence: float
    max_drawdown_threshold: float
    strategy_switch_cooldown: int
    performance_alert_threshold: float

@dataclass
class ExecutionConfig:
    max_retries: int
    retry_delay_seconds: int
    timeout_seconds: int
    batch_size: int
    max_orders_per_second: int
    market_data_source: str
    order_types: list

@dataclass
class RiskConfig:
    max_position_size: float
    max_leverage: float
    max_drawdown: float
    position_limits: Dict[str, float]
    stop_loss: Dict[str, Any]
    take_profit: Dict[str, Any]

@dataclass
class PortfolioConfig:
    initial_cash: float
    base_currency: str
    rebalance: Dict[str, Any]

@dataclass
class MarketDataConfig:
    providers: list
    cache: Dict[str, Any]

@dataclass
class ReportingConfig:
    enabled: bool
    frequency: str
    metrics: list
    formats: list
    destinations: list

class ConfigLoader:
    """Configuration loader with validation."""
    
    def __init__(self, config_path: Path):
        """Initialize with config file path."""
        self.config_path = Path(config_path)
        self.config = {}
        self.load()
        
    def load(self) -> None:
        """Load configuration from file."""
        try:
            with open(self.config_path, 'r') as f:
                self.config = yaml.safe_load(f)
                
            # Validate required fields
            required_fields = {
                'monitoring': ['log_dir', 'log_level', 'metrics_port', 'prometheus_job'],
                'execution': ['max_retries', 'retry_delay_seconds', 'timeout_seconds'],
                'risk': ['max_position_size', 'max_leverage', 'max_drawdown'],
                'portfolio': ['initial_cash', 'base_currency'],
                'market_data': ['providers'],
                'reporting': ['enabled', 'frequency']
            }
            
            for section, fields in required_fields.items():
                if section not in self.config:
                    raise ConfigurationError(f"Missing required section: {section}")
                for field in fields:
                    if field not in self.config[section]:
                        raise ConfigurationError(f"Missing required field: {section}.{field}")
                
            # Parse into dataclasses
            self.monitoring = MonitoringConfig(**self.config['monitoring'])
            self.execution = ExecutionConfig(**self.config['execution'])
            self.risk = RiskConfig(**self.config['risk'])
            self.portfolio = PortfolioConfig(**self.config['portfolio'])
            self.market_data = MarketDataConfig(**self.config['market_data'])
            self.reporting = ReportingConfig(**self.config['reporting'])
            
        except (yaml.YAMLError, KeyError) as e:
            raise ConfigurationError(f"Failed to load config: {e}")
            
    def reload(self) -> None:
        """Reload configuration from file."""
        self.load()
        
    def validate(self) -> bool:
        """Validate configuration values."""
        try:
            # Risk validation
            if not 0 < self.risk.max_position_size <= 1:
                return False
            if self.risk.max_leverage < 1:
                return False
                
            # Portfolio validation
            if self.portfolio.initial_cash <= 0:
                return False
                
            # Execution validation
            if self.execution.market_data_source not in ['live', 'backtest']:
                return False
                
            return True
            
        except Exception:
            return False
            
    def get_config(self) -> Dict[str, Any]:
        """Get complete configuration."""
        return self.config
