"""
Decision Fusion Engine
--------------------
This module implements the decision fusion engine for combining multiple trading signals.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
import numpy as np
import pandas as pd
from src.config import TradingConfig
from src.agents.trading.base_agent import BaseTradingAgent

# Load configuration
config = TradingConfig.load_from_file()

class DecisionFusionEngine(BaseTradingAgent):
    """Decision fusion engine for combining multiple trading signals."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the decision fusion engine.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        super().__init__(config)
        self.logger = logging.getLogger("DecisionFusionEngine")
        self.positions = {}
        self.trades = []
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate decision fusion configuration."""
        default_config = {
            'fusion': {
                'confidence_threshold': 0.7,
                'weight_decay': 0.95,
                'min_confidence': 0.3,
                'max_agents': 5
            },
            'trading': {
                'max_position_size': config.MAX_POSITION_SIZE,
                'min_position_size': 0.001,
                'risk_per_trade': 0.01,
                'max_leverage': config.MAX_LEVERAGE
            },
            'risk_management': {
                'max_drawdown': config.MAX_DRAWDOWN,
                'max_position_risk': config.MAX_POSITION_RISK,
                'max_daily_trades': config.MAX_DAILY_TRADES,
                'max_daily_loss': config.MAX_DAILY_LOSS
            },
            'market_conditions': {
                'min_volume': 100000,
                'min_price': 1.0,
                'max_spread': config.MAX_SPREAD,
                'min_liquidity': config.MIN_LIQUIDITY
            }
        }
        
        if config:
            default_config.update(config)
        return default_config
        
    def adjust_weights(self, past_trades: List[Dict]) -> None:
        """Adjusts model weighting based on past trade success.
        
        Args:
            past_trades (List[Dict]): List of past trades with performance metrics
        """
        if not past_trades:
            return
            
        try:
            performance = [trade.get('pnl', 0) for trade in past_trades]
            mean_performance = np.mean(performance)
            
            # Adjust weights based on performance
            for signal in self.weights:
                self.weights[signal] = np.clip(
                    self.weights[signal] * (1 + mean_performance),
                    0.01,  # Minimum weight
                    1.0    # Maximum weight
                )
                
            # Normalize weights
            total_weight = sum(self.weights.values())
            self.weights = {k: v/total_weight for k, v in self.weights.items()}
            
        except Exception as e:
            self.logger.error(f"Error adjusting weights: {str(e)}")
            
    def fuse_signals(self, signals: Dict[str, float]) -> Dict[str, float]:
        """Fuse multiple trading signals into a single decision.
        
        Args:
            signals (Dict[str, float]): Dictionary of signals and their values
            
        Returns:
            Dict[str, float]: Fused signal with confidence score
        """
        try:
            # Initialize result
            result = {
                'action': 'hold',
                'confidence': 0.0
            }
            
            # Calculate weighted sum of signals
            weighted_sum = 0.0
            total_weight = 0.0
            
            for signal, value in signals.items():
                if signal in self.weights:
                    weighted_sum += value * self.weights[signal]
                    total_weight += self.weights[signal]
                    
            if total_weight > 0:
                normalized_score = weighted_sum / total_weight
                
                # Determine action based on score
                if normalized_score > 0.5:
                    result['action'] = 'buy'
                elif normalized_score < -0.5:
                    result['action'] = 'sell'
                    
                result['confidence'] = abs(normalized_score)
                
            return result
            
        except Exception as e:
            self.logger.error(f"Error fusing signals: {str(e)}")
            return {'action': 'hold', 'confidence': 0.0}
    
    def calculate_signal(self, data: pd.DataFrame) -> float:
        """Calculate trading signal from market data.
        
        Args:
            data (pd.DataFrame): Processed market data
            
        Returns:
            float: Trading signal between -1 and 1
        """
        try:
            # Calculate various signals
            signals = {
                'momentum': self._calculate_momentum_signal(data),
                'mean_reversion': self._calculate_mean_reversion_signal(data),
                'volume': self._calculate_volume_signal(data),
                'volatility': self._calculate_volatility_signal(data)
            }
            
            # Fuse signals
            result = self.fuse_signals(signals)
            
            # Convert action to signal
            if result['action'] == 'buy':
                return result['confidence']
            elif result['action'] == 'sell':
                return -result['confidence']
            else:
                return 0.0
                
        except Exception as e:
            self.logger.error(f"Error calculating signal: {str(e)}")
            return 0.0
            
    def calculate_position_size(self, signal: float, data: pd.DataFrame) -> float:
        """Calculate position size based on signal strength and risk parameters.
        
        Args:
            signal (float): Trading signal
            data (pd.DataFrame): Market data
            
        Returns:
            float: Position size
        """
        try:
            # Get risk parameters from config
            risk_per_trade = self.config.get('risk_per_trade', 0.01)
            max_position_size = self.config.get('max_position_size', 1.0)
            
            # Calculate position size based on signal strength
            position_size = abs(signal) * max_position_size
            
            # Apply risk management
            position_size = min(position_size, risk_per_trade)
            
            return position_size * (1 if signal > 0 else -1)
            
        except Exception as e:
            self.logger.error(f"Error calculating position size: {str(e)}")
            return 0.0
            
    def calculate_dynamic_targets(self, signal: float, data: pd.DataFrame) -> tuple:
        """Calculate dynamic take profit and stop loss levels.
        
        Args:
            signal (float): Trading signal
            data (pd.DataFrame): Market data
            
        Returns:
            tuple: (take_profit_pct, stop_loss_pct)
        """
        try:
            # Get parameters from config
            base_take_profit = self.config.get('base_take_profit', 0.02)
            base_stop_loss = self.config.get('base_stop_loss', 0.01)
            
            # Adjust based on volatility
            volatility = data['close'].pct_change().std()
            take_profit = base_take_profit * (1 + volatility)
            stop_loss = base_stop_loss * (1 + volatility)
            
            return take_profit, stop_loss
            
        except Exception as e:
            self.logger.error(f"Error calculating targets: {str(e)}")
            return 0.02, 0.01
            
    def _calculate_momentum_signal(self, data: pd.DataFrame) -> float:
        """Calculate momentum signal."""
        try:
            # Simple momentum calculation
            returns = data['close'].pct_change()
            return np.clip(returns.mean() * 10, -1, 1)
        except:
            return 0.0
            
    def _calculate_mean_reversion_signal(self, data: pd.DataFrame) -> float:
        """Calculate mean reversion signal."""
        try:
            # Simple mean reversion calculation
            ma = data['close'].rolling(window=20).mean()
            current_price = data['close'].iloc[-1]
            deviation = (current_price - ma.iloc[-1]) / ma.iloc[-1]
            return np.clip(-deviation * 5, -1, 1)
        except:
            return 0.0
            
    def _calculate_volume_signal(self, data: pd.DataFrame) -> float:
        """Calculate volume signal."""
        try:
            # Simple volume analysis
            volume_ma = data['volume'].rolling(window=20).mean()
            current_volume = data['volume'].iloc[-1]
            volume_ratio = current_volume / volume_ma.iloc[-1]
            return np.clip((volume_ratio - 1) * 0.5, -1, 1)
        except:
            return 0.0
            
    def _calculate_volatility_signal(self, data: pd.DataFrame) -> float:
        """Calculate volatility signal."""
        try:
            # Simple volatility analysis
            returns = data['close'].pct_change()
            volatility = returns.rolling(window=20).std()
            current_volatility = volatility.iloc[-1]
            avg_volatility = volatility.mean()
            return np.clip((avg_volatility - current_volatility) * 5, -1, 1)
        except:
            return 0.0
    
