"""
Configuration Loader
------------------
This module implements the configuration loading utilities.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
import json
import yaml
from pathlib import Path
from src.config import TradingConfig
from src.utils.logger import get_logger

# Load configuration
config = TradingConfig.load_from_file()

logger = get_logger(__name__)

class ConfigLoader:
    """Configuration loader for managing configuration files."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the configuration loader.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        self.config = config or TradingConfig.load_from_file()
        self.logger = logging.getLogger("ConfigLoader")
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate configuration loader settings."""
        default_config = {
            'paths': {
                'config_dir': 'config',
                'archive_dir': 'config/archive',
                'backup_dir': 'config/backup'
            },
            'file_types': {
                'json': True,
                'yaml': True,
                'yml': True
            },
            'validation': {
                'required_fields': ['version', 'timestamp'],
                'schema_path': 'config/schema.json'
            }
        }
        
        if config:
            default_config.update(config)
        return default_config

    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key path (e.g., 'api.rate_limits.alpaca')"""
        if isinstance(self.config, TradingConfig):
            value = self.config.get(key, None)
            if value is not None:
                return value
            if default is not None:
                logger.warning(f"Configuration key '{key}' not found, using default value")
                return default
            raise KeyError(f"Configuration key '{key}' not found")

        try:
            value = self.config
            for k in key.split('.'):
                value = value[k]
            return value
        except (KeyError, TypeError):
            if default is not None:
                logger.warning(f"Configuration key '{key}' not found, using default value")
                return default
            raise KeyError(f"Configuration key '{key}' not found")
            
    def get_api_rate_limit(self, service: str) -> int:
        """Get API rate limit for a specific service"""
        return self.get(f"api.rate_limits.{service}")
        
    def get_api_timeout(self, critical: bool = False) -> int:
        """Get API timeout setting"""
        return self.get("api.timeouts.critical" if critical else "api.timeouts.default")
        
    def get_monitoring_threshold(self, metric: str) -> float:
        """Get monitoring threshold for a specific metric"""
        return self.get(f"monitoring.thresholds.{metric}_warning")
        
    def get_trading_risk_limit(self, limit_type: str) -> float:
        """Get trading risk limit"""
        return self.get(f"trading.risk.max_{limit_type}")
        
    def get_market_symbols(self, market_type: str) -> list:
        """Get list of symbols for a specific market type"""
        if not self.get(f"markets.{market_type}.enabled"):
            return []
        return self.get(f"markets.{market_type}.symbols", [])
        
    def is_testing_mode(self) -> bool:
        """Check if system is in testing mode"""
        return self.get("testing.simulation_mode", True)

# Singleton instance
config_loader = ConfigLoader() 
