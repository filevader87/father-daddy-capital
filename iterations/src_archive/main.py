"""
Father Daddy Capital - Main Entry Point
--------------------------------------
Unified entry point for all trading modes
"""

import asyncio
import argparse
import logging
import signal
import sys
from pathlib import Path
from typing import Dict, Any

# Add src to path
sys.path.append(str(Path(__file__).parent))

from src.config.unified_config import UnifiedConfigManager
from src.agents.adaptive_agent import AdaptiveTradingAgent
from src.market_data.yfinance_provider import YFinanceMarketDataProvider
from src.utils.logger import get_logger
from src.monitoring.system_monitor import system_monitor

logger = get_logger(__name__)

class TradingSystem:
    """Main trading system orchestrator."""
    
    def __init__(self, config_path: str):
        """Initialize trading system.
        
        Args:
            config_path: Path to configuration file
        """
        self.config_manager = UnifiedConfigManager(config_path)
        self.config = self.config_manager.get_trading_config()
        self.agent_config = self.config_manager.get_agent_config()
        
        # Initialize components
        self.agent = AdaptiveTradingAgent(
            asset_classes=['crypto', 'stocks']  # Support multiple asset classes
        )
        
        self.monitor = system_monitor
        self.market_data_provider = YFinanceMarketDataProvider(self.config.symbols)
        self.running = False
        
        # Setup logging
        self._setup_logging()
        
        logger.info(f"Trading system initialized in {self.config.mode} mode")
    
    def _setup_logging(self):
        """Setup logging configuration."""
        log_level = self.config_manager.get('monitoring.log_level', 'INFO')
        logging.basicConfig(
            level=getattr(logging, log_level.upper()),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('logs/trading.log'),
                logging.StreamHandler()
            ]
        )
    
    async def start(self):
        """Start the trading system."""
        try:
            self.running = True
            logger.info("Starting trading system...")
            
            # Start monitoring
            await self.monitor.start()
            
            # Setup signal handlers
            self._setup_signal_handlers()
            
            # Start main trading loop
            await self._trading_loop()
            
        except Exception as e:
            logger.error(f"Error starting trading system: {e}")
            await self.stop()
            raise
    
    async def stop(self):
        """Stop the trading system."""
        if not self.running:
            return
        
        try:
            self.running = False
            logger.info("Stopping trading system...")
            
            # Stop monitoring
            await self.monitor.stop()
            
            # Log final performance metrics
            metrics = self.agent.get_performance_metrics()
            logger.info(f"Final performance metrics: {metrics}")
            
            logger.info("Trading system stopped successfully")
            
        except Exception as e:
            logger.error(f"Error stopping trading system: {e}")
    
    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}")
            asyncio.create_task(self.stop())
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
    async def _trading_loop(self):
        """Main trading loop."""
        logger.info("Starting main trading loop...")
        
        while self.running:
            try:
                # Get market data (placeholder - would integrate with data providers)
                market_data = await self._get_market_data()
                
                if market_data:
                    # Process market data and generate signals
                    signals = await self.agent.process_market_data(market_data)
                    
                    if signals:
                        logger.info(f"Generated {len(signals)} trading signals")
                        
                        # Execute signals
                        trades = await self.agent.execute_signals(signals)
                        
                        if trades:
                            logger.info(f"Executed {len(trades)} trades")
                    
                    # Log portfolio status and adaptive metrics
                    metrics = self.agent.get_adaptive_metrics()
                    logger.info(f"Portfolio value: ${metrics['portfolio_value']:.2f}")
                    logger.info(f"Regime stability: {metrics['regime_stability']:.2f}")
                    logger.info(f"Adaptive confidence threshold: {metrics['adaptive_parameters']['confidence_threshold']:.2f}")
                
                # Wait before next iteration
                await asyncio.sleep(60)  # 1 minute intervals
                
            except Exception as e:
                logger.error(f"Error in trading loop: {e}")
                await asyncio.sleep(10)  # Wait before retrying
    
    async def _get_market_data(self) -> Dict[str, Any]:
        """Get normalized OHLCV market data for configured symbols."""
        return await asyncio.to_thread(self.market_data_provider.fetch_all)
    
    def get_status(self) -> Dict[str, Any]:
        """Get system status."""
        return {
            'running': self.running,
            'mode': self.config.mode,
            'adaptive_agent': True,
            'asset_classes': ['crypto', 'stocks'],
            'portfolio': self.agent.get_adaptive_metrics()
        }

async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Father Daddy Capital Trading System')
    parser.add_argument('--mode', choices=['paper', 'live', 'backtest'], 
                       help='Trading mode')
    parser.add_argument('--config', default='config/trading.yaml',
                       help='Configuration file path')
    parser.add_argument('--verbose', action='store_true',
                       help='Verbose logging')
    
    args = parser.parse_args()
    
    try:
        # Initialize and start trading system
        system = TradingSystem(args.config)
        
        # Override mode if specified
        if args.mode:
            system.config_manager._config['trading']['mode'] = args.mode
            system.config = system.config_manager.get_trading_config()
        
        await system.start()
        
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
