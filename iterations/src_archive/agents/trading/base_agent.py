"""
Base Trading Agent
-----------------
This module implements the base class for all trading agents.
"""

import logging
from abc import ABC, abstractmethod
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional, Tuple, Union
from datetime import datetime
from src.config import TradingConfig

# Load configuration
config = TradingConfig.load_from_file()

class BaseTradingAgent:
    """Base class for all trading agents."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the base trading agent.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        self.config = self._load_config(config)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.positions = {}
        self.trades = []
        self.position = 0
        self.cash = self.config['initial_cash'] if 'initial_cash' in self.config else 100000
        self.portfolio_value = self.cash
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate agent configuration."""
        base_config = TradingConfig.load_from_file()
        default_config = {
            'trading': {
                'max_position_size': base_config.MAX_POSITION_SIZE,
                'min_position_size': 0.001,
                'risk_per_trade': 0.01,
                'max_leverage': base_config.MAX_LEVERAGE
            },
            'risk_management': {
                'max_drawdown': base_config.MAX_DRAWDOWN,
                'max_position_risk': base_config.MAX_POSITION_RISK,
                'max_daily_trades': base_config.MAX_DAILY_TRADES,
                'max_daily_loss': base_config.MAX_DAILY_LOSS
            },
            'market_conditions': {
                'min_volume': 100000,
                'min_price': 1.0,
                'max_spread': base_config.MAX_SPREAD,
                'min_liquidity': base_config.MIN_LIQUIDITY
            }
        }
        
        if config:
            default_config.update(config)
        return default_config
    
    @abstractmethod
    def calculate_signal(self, data: pd.DataFrame) -> float:
        """Calculate the trading signal.
        
        Args:
            data (pd.DataFrame): Market data with OHLCV and indicators
            
        Returns:
            float: Signal value between -1 and 1
        """
        pass
    
    def calculate_position_size(self, signal: float, data: pd.DataFrame) -> float:
        """Calculate the position size based on the signal strength.
        
        Args:
            signal (float): Trading signal between -1 and 1
            data (pd.DataFrame): Market data
            
        Returns:
            float: Position size in base currency
        """
        max_position = self.config['trading']['max_position_size']
        risk_per_trade = self.config['trading']['risk_per_trade']
        
        # Scale position size by signal strength and available capital
        position_size = abs(signal) * max_position
        position_size = min(position_size, self.portfolio_value * risk_per_trade)
        
        return position_size if signal > 0 else -position_size
    
    def calculate_risk_metrics(self, data: pd.DataFrame) -> Tuple[float, float]:
        """Calculate risk metrics for position sizing.
        
        Args:
            data (pd.DataFrame): Market data
            
        Returns:
            Tuple[float, float]: (volatility, correlation)
        """
        returns = data['close'].pct_change().dropna()
        volatility = returns.std() * np.sqrt(252)  # Annualized volatility
        
        # Calculate correlation with market (if available)
        correlation = 0.0
        if 'market_returns' in data.columns:
            correlation = returns.corr(data['market_returns'])
            
        return volatility, correlation
    
    def update_portfolio(self, price: float, timestamp: str) -> None:
        """Update portfolio value based on current position and price.
        
        Args:
            price (float): Current market price
            timestamp (str): Current timestamp
        """
        if self.position != 0:
            pnl = self.position * price
            self.portfolio_value = self.cash + pnl
            
            self.trades.append({
                'timestamp': timestamp,
                'price': price,
                'position': self.position,
                'portfolio_value': self.portfolio_value
            })
    
    def get_performance_metrics(self) -> Dict[str, float]:
        """Calculate performance metrics for the agent.
        
        Returns:
            Dict[str, float]: Dictionary of performance metrics
        """
        if not self.trades:
            return {
                'total_return': 0.0,
                'sharpe_ratio': 0.0,
                'max_drawdown': 0.0,
                'win_rate': 0.0
            }
            
        # Calculate returns
        portfolio_values = pd.Series([t['portfolio_value'] for t in self.trades])
        returns = portfolio_values.pct_change().dropna()
        
        # Calculate metrics
        total_return = (portfolio_values.iloc[-1] / portfolio_values.iloc[0]) - 1
        sharpe_ratio = np.sqrt(252) * returns.mean() / returns.std() if returns.std() != 0 else 0
        
        # Calculate maximum drawdown
        peak = portfolio_values.expanding(min_periods=1).max()
        drawdown = (portfolio_values - peak) / peak
        max_drawdown = drawdown.min()
        
        # Calculate win rate
        profitable_trades = sum(1 for i in range(1, len(self.trades))
                              if self.trades[i]['portfolio_value'] > self.trades[i-1]['portfolio_value'])
        win_rate = profitable_trades / (len(self.trades) - 1) if len(self.trades) > 1 else 0
        
        return {
            'total_return': total_return,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': max_drawdown,
            'win_rate': win_rate
        }
    
    def reset(self) -> None:
        """Reset the agent's state."""
        self.position = 0
        self.cash = self.config['initial_cash'] if 'initial_cash' in self.config else 100000
        self.portfolio_value = self.cash
        self.trades = [] 


BaseAgent = BaseTradingAgent
