#!/usr/bin/env python3
"""
Run Paper Trading System
----------------------
This script runs the paper trading system with all components.

ENVIRONMENT: PAPER TRADING
USAGE: python scripts/run_paper_trading.py
PURPOSE: Simulate trading without real money execution
FEATURES: Full trading logic, risk management, portfolio tracking
"""

import os
import sys
import logging
import logging.config
import yaml
import json
import asyncio
from pathlib import Path
from datetime import datetime
import signal
import psutil

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from src.config import TradingConfig
from src.risk.risk_manager import RiskManager
from src.utils.portfolio import PortfolioManager
from src.trading.trading_engine import TradingEngine
from src.utils.notifier import Notifier

class PaperTradingSystem:
    """Paper trading system coordinator."""
    
    def __init__(self, config):
        """Initialize the paper trading system."""
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.running = False
        
        # Initialize components
        self.risk_manager = RiskManager(config)
        self.portfolio_manager = PortfolioManager(config)
        self.trading_engine = TradingEngine(config)
        self.notifier = Notifier(config)
        
    async def start(self):
        """Start the paper trading system."""
        self.logger.info("Starting paper trading system")
        self.running = True
        
        try:
            # Start components
            await self.risk_manager.start()
            await self.portfolio_manager.start()
            await self.trading_engine.start()
            await self.notifier.start()
            
            # Send startup notification
            await self.notifier.send_notification(
                "Paper Trading System Started",
                f"Initial balance: ${self.config['paper_trading']['initial_balance']}\n"
                f"Trading pairs: {', '.join(self.config['exchange']['trading_pairs'])}"
            )
            
            # Main trading loop
            while self.running:
                try:
                    # Check system health
                    await self._check_system_health()
                    
                    # Process market data
                    await self._process_market_data()
                    
                    # Execute trading logic
                    await self._execute_trading_logic()
                    
                    # Update portfolio
                    await self._update_portfolio()
                    
                    # Sleep for next iteration
                    await asyncio.sleep(self.config['trading']['interval_seconds'])
                    
                except Exception as e:
                    self.logger.error(f"Error in trading loop: {str(e)}")
                    await self.notifier.send_notification(
                        "Trading System Error",
                        f"Error occurred: {str(e)}"
                    )
                    
        except Exception as e:
            self.logger.error(f"Failed to start paper trading system: {str(e)}")
            raise
        finally:
            await self.stop()
    
    async def stop(self):
        """Stop the paper trading system."""
        self.logger.info("Stopping paper trading system")
        self.running = False
        
        try:
            # Stop components
            await self.risk_manager.stop()
            await self.portfolio_manager.stop()
            await self.trading_engine.stop()
            await self.notifier.stop()
            
            # Send shutdown notification
            await self.notifier.send_notification(
                "Paper Trading System Stopped",
                "System shutdown complete"
            )
            
        except Exception as e:
            self.logger.error(f"Error stopping paper trading system: {str(e)}")
            raise
    
    async def _check_system_health(self):
        """Check system health and send alerts if needed."""
        # Check CPU usage
        cpu_usage = self._get_cpu_usage()
        if cpu_usage > self.config['monitoring']['alerts']['cpu_threshold']:
            await self.notifier.send_notification(
                "High CPU Usage Alert",
                f"CPU usage: {cpu_usage}%"
            )
        
        # Check memory usage
        memory_usage = self._get_memory_usage()
        if memory_usage > self.config['monitoring']['alerts']['memory_threshold']:
            await self.notifier.send_notification(
                "High Memory Usage Alert",
                f"Memory usage: {memory_usage}%"
            )
    
    async def _process_market_data(self):
        """Process market data and update state."""
        # Fetch market data for all configured trading pairs
        market_data = await self.trading_engine.fetch_market_data(
            pairs=self.config['exchange']['trading_pairs']
        )
        # Store for downstream logic
        self.latest_market_data = market_data
    
    async def _execute_trading_logic(self):
        """Execute trading logic based on signals."""
        # Generate buy/sell/hold signals based on latest market data
        signals = await self.trading_engine.generate_signals(self.latest_market_data)
        # Execute each signal (paper-trade)
        for signal in signals:
            await self.trading_engine.execute_signal(signal)
    
    async def _update_portfolio(self):
        """Update portfolio state and metrics."""
        # Refresh portfolio state (fills, positions)
        await self.portfolio_manager.refresh_positions()
        # Recalculate P&L and performance metrics
        performance = await self.portfolio_manager.recalculate_metrics()
        # Persist trade logs for risk auditing
        self.risk_manager.record_trade_log(performance)
        # Check risk thresholds and fire alerts
        alerts = self.risk_manager.check_alerts(performance)
        for alert in alerts:
            await self.notifier.send_notification(alert)
    
    def _get_cpu_usage(self):
        """Get current CPU usage percentage."""
        return psutil.cpu_percent(interval=None)
    
    def _get_memory_usage(self):
        """Get current memory usage percentage."""
        return psutil.virtual_memory().percent

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

def load_config():
    """Load configuration using centralized config loader."""
    try:
        from src.config.loader import ConfigLoader
        loader = ConfigLoader()
        return loader.load_all()
    except Exception as e:
        logger.error(f"Failed to load config: {str(e)}")
        raise

async def main():
    """Main function to run the paper trading system."""
    logger = setup_logging()
    logger.info("Starting paper trading system")
    
    try:
        # Load configuration
        config = load_config()
        
        # Create and start paper trading system
        system = PaperTradingSystem(config)
        
        # Set up signal handlers
        def signal_handler(sig, frame):
            logger.info("Received shutdown signal")
            asyncio.create_task(system.stop())
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Start the system
        await system.start()
        
    except Exception as e:
        logger.error(f"Failed to run paper trading system: {str(e)}")
        raise

if __name__ == "__main__":
    asyncio.run(main()) 