"""
Unified Configuration Module
--------------------------
This module provides a centralized configuration system for the entire application.
It consolidates settings from various sources into a single, type-safe interface.
"""

import os
import json
import yaml
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field, fields
from pathlib import Path


def _coerce_dataclass(cls, values: Dict[str, Any]):
    """Instantiate a dataclass while ignoring unknown keys from config files."""
    if not isinstance(values, dict):
        return cls()
    valid_keys = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in values.items() if k in valid_keys})

@dataclass
class SystemConfig:
    """System configuration settings."""
    name: str = "Father Daddy Capital Trading System"
    version: str = "1.0.0"
    environment: str = "production"
    debug: bool = False

@dataclass
class EventBusConfig:
    """Event bus configuration settings."""
    max_queue_size: int = 10000
    max_history_size: int = 1000
    error_handling: Dict[str, Any] = field(default_factory=lambda: {
        "retry_attempts": 3,
        "retry_delay": 1.0
    })

@dataclass
class LoggingConfig:
    """Logging configuration settings."""
    level: str = "INFO"
    file_rotation: Dict[str, Any] = field(default_factory=lambda: {
        "max_bytes": 10485760,
        "backup_count": 5
    })
    console_output: bool = True
    file_output: bool = True
    paths: Dict[str, str] = field(default_factory=lambda: {
        "main": "logs/father_daddy_{date}.log",
        "errors": "logs/errors_{date}.log",
        "trades": "logs/trades_{date}.log"
    })

@dataclass
class MonitoringConfig:
    """Monitoring configuration settings."""
    log_level: str = "INFO"
    metrics_enabled: bool = True
    health_check_interval: int = 300
    metrics_collection_interval: int = 60
    prometheus_port: int = 8000
    update_interval: int = 60
    metrics_retention: int = 7
    alert_thresholds: Dict[str, float] = field(default_factory=lambda: {
        "cpu_usage": 80,
        "memory_usage": 85,
        "disk_usage": 90,
        "latency": 1000
    })

@dataclass
class AgentsConfig:
    """Agents configuration settings."""
    strategy_type: str = "momentum"
    asset_class: str = "crypto"
    parallel_processing: bool = True
    max_workers: int = 4
    memory_optimization: bool = True
    signal_threshold: float = 0.7
    signal_cooldown: int = 300
    momentum: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "config_file": "config/momentum_agent_config.json"
    })
    sentiment: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "config_file": "config/sentiment_agent_config.json"
    })

@dataclass
class DataConfig:
    """Data configuration settings."""
    storage: Dict[str, Any] = field(default_factory=lambda: {
        "type": "local",
        "path": "data/",
        "retention_days": 30
    })
    sources: Dict[str, Any] = field(default_factory=lambda: {
        "market_data": {
            "provider": "yfinance",
            "update_interval": 60
        },
        "news": {
            "provider": "newsapi",
            "update_interval": 300
        }
    })

@dataclass
class BacktestingConfig:
    """Backtesting configuration settings."""
    enabled: bool = True
    data_path: str = "backtests/"
    default_period: Dict[str, str] = field(default_factory=lambda: {
        "start": "2020-01-01",
        "end": "2023-12-31"
    })
    metrics: List[str] = field(default_factory=lambda: [
        "sharpe_ratio",
        "sortino_ratio",
        "max_drawdown",
        "win_rate"
    ])
    simulation_mode: bool = True
    backtest_days: int = 30
    initial_balance: int = 100000

@dataclass
class NotificationsConfig:
    """Notifications configuration settings."""
    discord: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "webhook_url": "${DISCORD_WEBHOOK_URL}",
        "channels": {
            "alerts": "trading-alerts",
            "performance": "trading-performance"
        }
    })
    email: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": False,
        "smtp_server": "${SMTP_SERVER}",
        "smtp_port": "${SMTP_PORT}",
        "sender": "${EMAIL_SENDER}",
        "recipients": []
    })

@dataclass
class TradingConfig:
    """Core trading configuration settings."""
    # System configuration
    system: SystemConfig = field(default_factory=SystemConfig)
    event_bus: EventBusConfig = field(default_factory=EventBusConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    
    # Trading parameters
    trading: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "max_open_positions": 10,
        "position_sizing": {
            "max_risk_per_trade": 0.02,
            "max_portfolio_risk": 0.1
        },
        "timeframes": ["1h", "4h", "1d"],
        "trading_hours": {
            "start": "00:00",
            "end": "23:59",
            "timezone": "UTC"
        }
    })
    
    # Risk management
    risk: Dict[str, Any] = field(default_factory=lambda: {
        "max_drawdown": 0.2,
        "max_leverage": 3.0,
        "position_limits": {
            "max_size": 0.1,
            "max_concentration": 0.3
        },
        "volatility_limits": {
            "max_daily": 0.05,
            "max_hourly": 0.02
        }
    })
    
    # API settings
    api: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8000,
        "auth": {
            "enabled": True,
            "jwt_secret": "${JWT_SECRET}",
            "token_expiry": 3600
        },
        "rate_limit": {
            "requests": 100,
            "period": 60
        }
    })

    # Additional components
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    data: DataConfig = field(default_factory=DataConfig)
    backtesting: BacktestingConfig = field(default_factory=BacktestingConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    
    # Market configuration
    markets: Dict[str, Any] = field(default_factory=lambda: {
        "crypto": {
            "enabled": True,
            "symbols": ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "RNDRUSD", "XRPUSD", "ADAUSD"]
        },
        "stocks": {
            "enabled": True,
            "symbols": ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]
        }
    })
    
    # DEX configuration
    dex: Dict[str, Any] = field(default_factory=lambda: {
        "solana": {
            "enabled": True,
            "symbols": ["SOL/USDC", "RAY/USDC", "SRM/USDC"],
            "rpc_url": "https://api.mainnet-beta.solana.com",
            "jupiter_api": "https://quote-api.jup.ag/v4",
            "slippage_bps": 100
        }
    })

    market_data: Dict[str, Any] = field(default_factory=dict)
    database: Dict[str, Any] = field(default_factory=dict)
    development: Dict[str, Any] = field(default_factory=lambda: {
        "debug": False,
        "profiling": False,
        "mock_data": False,
        "dry_run": True,
    })

    @property
    def mode(self) -> str:
        return self.trading.get("mode", "paper")

    @property
    def symbols(self) -> List[str]:
        symbols = self.trading.get("symbols")
        if symbols:
            return symbols
        crypto = self.markets.get("crypto", {}).get("symbols", [])
        stocks = self.markets.get("stocks", {}).get("symbols", [])
        return [*crypto, *stocks]

    @property
    def MAX_POSITION_SIZE(self) -> float:
        return float(
            self.trading.get("risk", {}).get(
                "max_position_size",
                self.risk.get("position_limits", {}).get("max_size", 0.1),
            )
        )

    @property
    def MAX_LEVERAGE(self) -> float:
        return float(
            self.trading.get("risk", {}).get(
                "max_leverage",
                self.risk.get("max_leverage", 2.0),
            )
        )

    @property
    def MAX_DRAWDOWN(self) -> float:
        return float(
            self.trading.get("risk", {}).get(
                "max_drawdown",
                self.risk.get("max_drawdown", 0.15),
            )
        )

    @property
    def MAX_POSITION_RISK(self) -> float:
        return float(self.risk.get("position_limits", {}).get("max_concentration", 0.3))

    @property
    def MAX_DAILY_TRADES(self) -> int:
        return int(self.trading.get("max_daily_trades", 100))

    @property
    def MAX_DAILY_LOSS(self) -> float:
        return float(self.risk.get("max_daily_risk", self.MAX_DRAWDOWN))

    @property
    def MAX_SPREAD(self) -> float:
        return float(self.trading.get("execution", {}).get("slippage_tolerance", 0.001))

    @property
    def MIN_LIQUIDITY(self) -> float:
        return float(self.trading.get("min_liquidity", 100000))

    @property
    def MAX_PARALLEL_TRADES(self) -> int:
        return int(self.agents.max_workers)

    @property
    def MAX_SLIPPAGE(self) -> float:
        return self.MAX_SPREAD

    @property
    def RISK_REBALANCE_EVERY(self) -> int:
        return 60

    @property
    def REPAIR_CHECK_EVERY(self) -> int:
        return 3600

    @property
    def REPAIR_THRESHOLD(self) -> float:
        return 0.1

    @property
    def WEIGHT_MAX(self) -> float:
        return 2.0

    @property
    def LOG_LEVEL(self) -> str:
        return self.logging.level

    def get(self, key: str, default: Any = None) -> Any:
        """Dictionary-style config access with dot-notation support."""
        if hasattr(self, key):
            return getattr(self, key)

        value: Any = self
        for part in key.split("."):
            if isinstance(value, dict):
                if part not in value:
                    return default
                value = value[part]
            elif hasattr(value, part):
                value = getattr(value, part)
            else:
                return default
        return value
    
    @classmethod
    def load_from_file(cls, config_path: str = None) -> 'TradingConfig':
        """Load configuration from file."""
        if config_path is None:
            config_path = os.getenv("CONFIG_PATH", "config/trading.yaml")
        config_path = Path(config_path)
        if not config_path.exists():
            return cls()
            
        with open(config_path, 'r') as f:
            if config_path.suffix == '.json':
                config_data = json.load(f)
            elif config_path.suffix in ['.yaml', '.yml']:
                config_data = yaml.safe_load(f)
            else:
                raise ValueError(f"Unsupported config file format: {config_path.suffix}")
        
        if "trading" in config_data and isinstance(config_data["trading"], dict):
            trading_risk = config_data["trading"].get("risk")
            if trading_risk and "risk" not in config_data:
                config_data["risk"] = trading_risk

        allowed_top_level = {f.name for f in fields(cls)}
        config_data = {k: v for k, v in config_data.items() if k in allowed_top_level}

        # Convert nested dictionaries to appropriate config objects
        if 'system' in config_data:
            config_data['system'] = _coerce_dataclass(SystemConfig, config_data['system'])
        if 'event_bus' in config_data:
            config_data['event_bus'] = _coerce_dataclass(EventBusConfig, config_data['event_bus'])
        if 'logging' in config_data:
            config_data['logging'] = _coerce_dataclass(LoggingConfig, config_data['logging'])
        if 'monitoring' in config_data:
            config_data['monitoring'] = _coerce_dataclass(MonitoringConfig, config_data['monitoring'])
        if 'agents' in config_data:
            config_data['agents'] = _coerce_dataclass(AgentsConfig, config_data['agents'])
        if 'data' in config_data:
            config_data['data'] = _coerce_dataclass(DataConfig, config_data['data'])
        if 'backtesting' in config_data:
            config_data['backtesting'] = _coerce_dataclass(BacktestingConfig, config_data['backtesting'])
        if 'notifications' in config_data:
            config_data['notifications'] = _coerce_dataclass(NotificationsConfig, config_data['notifications'])
                
        return cls(**config_data)
        
    def save_to_file(self, config_path: str = "config/main_config.json") -> None:
        """Save configuration to file."""
        config_path = Path(config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        config_dict = {
            'system': self.system.__dict__,
            'event_bus': self.event_bus.__dict__,
            'logging': self.logging.__dict__,
            'monitoring': self.monitoring.__dict__,
            'trading': self.trading,
            'risk': self.risk,
            'api': self.api,
            'agents': self.agents.__dict__,
            'data': self.data.__dict__,
            'backtesting': self.backtesting.__dict__,
            'notifications': self.notifications.__dict__
        }
        
        with open(config_path, 'w') as f:
            if config_path.suffix == '.json':
                json.dump(config_dict, f, indent=2)
            elif config_path.suffix in ['.yaml', '.yml']:
                yaml.dump(config_dict, f, default_flow_style=False)
            else:
                raise ValueError(f"Unsupported config file format: {config_path.suffix}")
                
    def update_from_env(self) -> None:
        """Update configuration from environment variables."""
        for key, value in os.environ.items():
            if key.startswith('TRADING_'):
                config_key = key[8:].lower()
                if hasattr(self, config_key):
                    setattr(self, config_key, value) 
