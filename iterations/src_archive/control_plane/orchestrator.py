"""
System Orchestrator
------------------
This module implements the core orchestration system for managing trading components.
"""

import os
import logging
from typing import Dict, Any, List, Optional
import pandas as pd
from datetime import datetime, timedelta
import json
import ccxt
from dotenv import load_dotenv
import time

from ..agents.trading.decision_fusion_engine import DecisionFusionEngine
from ..agents.data.preprocess_data import DataPreprocessor
from src.config import TradingConfig

# Load configuration
config = TradingConfig.load_from_file()

class Orchestrator:
    """Core orchestration system for managing trading components."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the orchestrator.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        self.config = self._load_config(config)
        self.logger = logging.getLogger("Orchestrator")
        self.components = {}
        self.status = {}
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate orchestration configuration."""
        default_config = {
            'components': {
                'risk_manager': {
                    'enabled': True,
                    'check_interval': 60,
                    'alert_threshold': 0.8
                },
                'trading_engine': {
                    'enabled': True,
                    'max_parallel_trades': config.MAX_PARALLEL_TRADES,
                    'execution_delay': 1.0
                },
                'market_data': {
                    'enabled': True,
                    'update_interval': 1.0,
                    'max_retries': 3
                },
                'monitoring': {
                    'enabled': True,
                    'metrics_interval': 60,
                    'log_level': 'INFO'
                }
            },
            'system': {
                'startup_timeout': 300,
                'shutdown_timeout': 60,
                'health_check_interval': 30
            }
        }
        
        if config:
            default_config.update(config)
        return default_config
        
    def _setup_logging(self) -> None:
        """Setup logging configuration."""
        log_level = os.getenv('LOG_LEVEL', 'INFO')
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('logs/trading.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def _initialize_exchange(self) -> ccxt.Exchange:
        """Initialize exchange connection.
        
        Returns:
            ccxt.Exchange: Exchange instance
        """
        # Get exchange config with defaults
        exchange_config = self.config.get('exchange', {})
        exchange_id = exchange_config.get('name', 'binance')
        
        try:
            exchange_class = getattr(ccxt, exchange_id)
            
            # Create exchange instance with API keys if available
            exchange = exchange_class({
                'apiKey': os.getenv('EXCHANGE_API_KEY', ''),
                'secret': os.getenv('EXCHANGE_SECRET_KEY', ''),
                'enableRateLimit': True
            })
            
            # Test connection
            exchange.load_markets()
            self.logger.info(f"Successfully connected to {exchange_id}")
            
            return exchange
            
        except Exception as e:
            self.logger.error(f"Failed to initialize exchange: {str(e)}")
            # Return a mock exchange for testing
            class MockExchange:
                def fetch_ohlcv(self, symbol, timeframe, limit=100):
                    # Generate mock data
                    import numpy as np
                    import pandas as pd
                    from datetime import datetime, timedelta
                    
                    now = datetime.now()
                    dates = [now - timedelta(minutes=i) for i in range(limit)]
                    dates.reverse()
                    
                    data = {
                        'timestamp': [d.timestamp() * 1000 for d in dates],
                        'open': np.random.normal(100, 1, limit),
                        'high': np.random.normal(101, 1, limit),
                        'low': np.random.normal(99, 1, limit),
                        'close': np.random.normal(100, 1, limit),
                        'volume': np.random.normal(1000, 100, limit)
                    }
                    
                    return list(zip(*[data[k] for k in ['timestamp', 'open', 'high', 'low', 'close', 'volume']]))
                    
                def create_order(self, symbol, type, side, amount, price=None):
                    return {'id': 'mock_order', 'status': 'closed'}
                    
            self.logger.warning("Using mock exchange for testing")
            return MockExchange()
        
    def fetch_market_data(self, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        """Fetch market data from exchange.
        
        Args:
            symbol (str): Trading pair symbol
            timeframe (str): Timeframe for candlesticks
            limit (int): Number of candlesticks to fetch
            
        Returns:
            pd.DataFrame: Market data
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            
            # Convert to DataFrame
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # Convert timestamp to datetime
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            return df
            
        except Exception as e:
            self.logger.error(f"Error fetching market data: {str(e)}")
            return pd.DataFrame()
            
    def process_market_data(self, data: pd.DataFrame) -> pd.DataFrame:
        """Process market data using the preprocessor.
        
        Args:
            data (pd.DataFrame): Raw market data
            
        Returns:
            pd.DataFrame: Processed data with indicators
        """
        try:
            return self.preprocessor.prepare_data(data)
        except Exception as e:
            self.logger.error(f"Error processing market data: {e}")
            return pd.DataFrame()
            
    def execute_trade(self, symbol: str, side: str, amount: float, price: float) -> bool:
        """Execute a trade on the exchange.
        
        Args:
            symbol (str): Trading pair symbol
            side (str): 'buy' or 'sell'
            amount (float): Amount to trade
            price (float): Limit price
            
        Returns:
            bool: True if trade was successful
        """
        try:
            order = self.exchange.create_order(
                symbol=symbol,
                type='limit',
                side=side,
                amount=amount,
                price=price
            )
            
            self.logger.info(f"Executed {side} order: {order}")
            return True
        except Exception as e:
            self.logger.error(f"Error executing trade: {e}")
            return False
            
    def manage_positions(self, symbol: str, current_price: float) -> None:
        """Manage open positions.
        
        Args:
            symbol (str): Trading pair symbol
            current_price (float): Current market price
        """
        if symbol in self.active_positions:
            position = self.active_positions[symbol]
            
            # Check take profit
            if position['side'] == 'buy' and current_price >= position['take_profit']:
                self.execute_trade(symbol, 'sell', position['amount'], current_price)
                del self.active_positions[symbol]
                
            # Check stop loss
            elif position['side'] == 'buy' and current_price <= position['stop_loss']:
                self.execute_trade(symbol, 'sell', position['amount'], current_price)
                del self.active_positions[symbol]
                
            # Similar checks for short positions
            elif position['side'] == 'sell' and current_price <= position['take_profit']:
                self.execute_trade(symbol, 'buy', position['amount'], current_price)
                del self.active_positions[symbol]
                
            elif position['side'] == 'sell' and current_price >= position['stop_loss']:
                self.execute_trade(symbol, 'buy', position['amount'], current_price)
                del self.active_positions[symbol]
                
    def run_trading_loop(self) -> None:
        """Run the main trading loop."""
        self.is_trading = True
        self.logger.info("Starting trading loop...")
        
        try:
            while self.is_trading:
                for symbol in self.config.get('symbols', []):
                    try:
                        # Fetch and process market data
                        data = self.fetch_market_data(
                            symbol,
                            self.config.get('timeframe', '1m')
                        )
                        
                        if data.empty:
                            self.logger.warning(f"No data available for {symbol}")
                            continue
                            
                        processed_data = self.process_market_data(data)
                        
                        if processed_data.empty:
                            self.logger.warning(f"Failed to process data for {symbol}")
                            continue
                            
                        # Get trading signal
                        signal = self.agent.calculate_signal(processed_data)
                        
                        # Calculate position size and targets
                        if abs(signal) > self.config.get('confidence_threshold', 0.5):
                            position_size = self.agent.calculate_position_size(signal, processed_data)
                            take_profit, stop_loss = self.agent.calculate_dynamic_targets(signal, processed_data)
                            
                            current_price = processed_data['close'].iloc[-1]
                            
                            # Execute trade if conditions are met
                            if position_size != 0 and symbol not in self.active_positions:
                                side = 'buy' if signal > 0 else 'sell'
                                if self.execute_trade(symbol, side, abs(position_size), current_price):
                                    self.active_positions[symbol] = {
                                        'side': side,
                                        'amount': abs(position_size),
                                        'entry_price': current_price,
                                        'take_profit': current_price * (1 + take_profit) if side == 'buy' else current_price * (1 - take_profit),
                                        'stop_loss': current_price * (1 - stop_loss) if side == 'buy' else current_price * (1 + stop_loss)
                                    }
                        
                        # Manage existing positions
                        self.manage_positions(symbol, current_price)
                        
                    except Exception as e:
                        self.logger.error(f"Error processing {symbol}: {str(e)}")
                        continue
                        
                # Sleep for the specified interval
                time.sleep(self.config.get('interval_seconds', 1))
                
        except Exception as e:
            self.logger.error(f"Fatal error in trading loop: {str(e)}")
        finally:
            self.is_trading = False
            self.logger.info("Trading loop stopped")
            
    def stop_trading(self) -> None:
        """Stop the trading loop."""
        self.is_trading = False
        self.logger.info("Stopping trading loop...") 