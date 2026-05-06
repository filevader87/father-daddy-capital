#!/usr/bin/env python3
"""
Initialize Paper Trading Environment
----------------------------------
This script sets up the paper trading environment with all necessary components.
"""

import os
import sys
import logging
import logging.config
import yaml
import json
from pathlib import Path
from datetime import datetime
import shutil

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from src.config import TradingConfig
from src.risk.risk_manager import RiskManager
from src.utils.portfolio import PortfolioManager
from src.trading.trading_engine import TradingEngine

def setup_logging():
    """Set up logging configuration."""
    log_dir = project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    
    # Load logging config
    with open(project_root / "config" / "logging_config.yaml") as f:
        log_config = yaml.safe_load(f)
    
    # Update log file paths with current date
    current_date = datetime.now().strftime("%Y%m%d")
    for handler in log_config["handlers"].values():
        if "filename" in handler:
            handler["filename"] = handler["filename"].format(date=current_date)
    
    logging.config.dictConfig(log_config)
    return logging.getLogger(__name__)

def setup_directories():
    """Create necessary directories."""
    directories = [
        "logs",
        "data",
        "data/market_data",
        "data/positions",
        "data/trades",
        "data/backtests",
        "state"
    ]
    
    for directory in directories:
        (project_root / directory).mkdir(exist_ok=True)

def load_config():
    """Load configuration using centralized config loader."""
    try:
        from src.config.loader import ConfigLoader
        loader = ConfigLoader()
        config = loader.load_all()
        
        # Validate required environment variables
        required_vars = [
            "EMAIL_USER",
            "EMAIL_RECIPIENT",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "SLACK_WEBHOOK_URL"
        ]
        
        missing_vars = [var for var in required_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
        
        return config
    except Exception as e:
        logger.error(f"Failed to load config: {str(e)}")
        raise

def initialize_components(config):
    """Initialize trading components."""
    # Initialize risk manager
    risk_manager = RiskManager(config)
    
    # Initialize portfolio manager
    portfolio_manager = PortfolioManager(config)
    
    # Initialize trading engine
    trading_engine = TradingEngine(config)
    
    return risk_manager, portfolio_manager, trading_engine

def main():
    """Main initialization function."""
    logger = setup_logging()
    logger.info("Starting paper trading environment initialization")
    
    try:
        # Setup directories
        setup_directories()
        logger.info("Created necessary directories")
        
        # Load configuration
        config = load_config()
        logger.info("Loaded and validated configuration")
        
        # Initialize components
        risk_manager, portfolio_manager, trading_engine = initialize_components(config)
        logger.info("Initialized trading components")
        
        # Save initial state
        initial_state = {
            "timestamp": datetime.now().isoformat(),
            "environment": "paper_trading",
            "initial_balance": config["paper_trading"]["initial_balance"],
            "trading_pairs": config["exchange"]["trading_pairs"]
        }
        
        state_file = project_root / "state" / "initial_state.json"
        with open(state_file, "w") as f:
            json.dump(initial_state, f, indent=2)
        
        logger.info("Paper trading environment initialized successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize paper trading environment: {str(e)}")
        raise

if __name__ == "__main__":
    main() 