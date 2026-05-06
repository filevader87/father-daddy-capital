"""
Quantitative Base Agent
----------------------
This module implements the base class for quantitative trading agents.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
import numpy as np
import pandas as pd
from src.config import TradingConfig
from src.agents.trading.base_agent import BaseTradingAgent
from abc import ABC, abstractmethod
from scipy import stats
import talib as ta

# Load configuration
config = TradingConfig.load_from_file()

class QuantBaseAgent(BaseTradingAgent):
    """Base class for quantitative trading agents."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the quantitative base agent.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        super().__init__(config)
        self.logger = logging.getLogger("QuantBaseAgent")
        self.positions = {}
        self.trades = []
        self.position = 0
        self.cash = config.get('initial_cash', 100000)
        self.portfolio_value = self.cash
        self.alpha_factors: Dict[str, float] = {}
        self.risk_factors: Dict[str, float] = {}
        self.market_regime: str = 'neutral'
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate quantitative agent configuration."""
        default_config = {
            'quantitative': {
                'feature_set': 'full',
                'normalization': 'zscore',
                'lookback_period': 100,
                'prediction_horizon': 1
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
    
    @abstractmethod
    def calculate_signal(self, data: pd.DataFrame) -> float:
        """Calculate the trading signal with advanced features."""
        pass
    
    def calculate_alpha_factors(self, data: pd.DataFrame) -> Dict[str, float]:
        """Calculate various alpha factors."""
        factors = {}
        
        # Momentum factors
        factors['momentum'] = self._calculate_momentum_factor(data)
        factors['acceleration'] = self._calculate_acceleration_factor(data)
        
        # Volatility factors
        factors['volatility'] = self._calculate_volatility_factor(data)
        factors['volatility_skew'] = self._calculate_volatility_skew(data)
        
        # Volume factors
        factors['volume_trend'] = self._calculate_volume_trend(data)
        factors['volume_volatility'] = self._calculate_volume_volatility(data)
        
        # Price action factors
        factors['price_momentum'] = self._calculate_price_momentum(data)
        factors['price_reversal'] = self._calculate_price_reversal(data)
        
        return factors
    
    def calculate_risk_factors(self, data: pd.DataFrame) -> Dict[str, float]:
        """Calculate various risk factors."""
        factors = {}
        
        # Market risk
        factors['beta'] = self._calculate_beta(data)
        factors['correlation'] = self._calculate_correlation(data)
        
        # Volatility risk
        factors['volatility_risk'] = self._calculate_volatility_risk(data)
        factors['tail_risk'] = self._calculate_tail_risk(data)
        
        # Liquidity risk
        factors['liquidity_risk'] = self._calculate_liquidity_risk(data)
        factors['slippage_risk'] = self._calculate_slippage_risk(data)
        
        return factors
    
    def detect_market_regime(self, data: pd.DataFrame) -> str:
        """Detect current market regime using multiple indicators."""
        volatility = data['close'].pct_change().std() * np.sqrt(252)
        trend = self._calculate_trend_strength(data)
        volume = self._calculate_volume_regime(data)
        
        if volatility > 0.3 and trend < 0.2:
            return 'high_volatility'
        elif volatility < 0.15 and trend > 0.7:
            return 'trending'
        elif volume > 0.8:
            return 'high_volume'
        else:
            return 'neutral'
    
    def calculate_position_size(self, signal: float, data: pd.DataFrame) -> float:
        """Calculate position size using Kelly Criterion and risk factors."""
        # Get alpha and risk factors
        alpha_factors = self.calculate_alpha_factors(data)
        risk_factors = self.calculate_risk_factors(data)
        
        # Calculate Kelly fraction
        win_rate = self._calculate_win_rate()
        win_loss_ratio = self._calculate_win_loss_ratio()
        kelly_fraction = win_rate - ((1 - win_rate) / win_loss_ratio)
        
        # Adjust for risk factors
        risk_adjustment = 1 - np.mean(list(risk_factors.values()))
        position_size = kelly_fraction * risk_adjustment * self.portfolio_value
        
        # Apply position limits
        max_position = self.config.get('max_position_size', 1000)
        position_size = min(position_size, max_position)
        
        return position_size if signal > 0 else -position_size
    
    def _calculate_momentum_factor(self, data: pd.DataFrame) -> float:
        """Calculate momentum factor using multiple timeframes."""
        returns = data['close'].pct_change()
        momentum = {}
        
        for window in [5, 10, 20, 50]:
            momentum[window] = returns.rolling(window).mean()
            
        return np.mean(list(momentum.values()))
    
    def _calculate_volatility_factor(self, data: pd.DataFrame) -> float:
        """Calculate volatility factor using GARCH-like approach."""
        returns = data['close'].pct_change()
        volatility = returns.rolling(20).std()
        volatility_ratio = volatility / volatility.rolling(50).mean()
        return float(volatility_ratio.iloc[-1])
    
    def _calculate_beta(self, data: pd.DataFrame) -> float:
        """Calculate beta using rolling regression."""
        if 'market_returns' not in data.columns:
            return 1.0
            
        returns = data['close'].pct_change()
        market_returns = data['market_returns']
        
        beta = returns.rolling(60).cov(market_returns) / market_returns.rolling(60).var()
        return float(beta.iloc[-1])
    
    def _calculate_tail_risk(self, data: pd.DataFrame) -> float:
        """Calculate tail risk using extreme value theory."""
        returns = data['close'].pct_change().dropna()
        tail_index = stats.genextreme.fit(returns)[0]
        return float(abs(tail_index))
    
    def _calculate_win_rate(self) -> float:
        """Calculate historical win rate."""
        if not self.trades:
            return 0.5
            
        profitable_trades = sum(1 for trade in self.trades if trade['pnl'] > 0)
        return profitable_trades / len(self.trades)
    
    def _calculate_win_loss_ratio(self) -> float:
        """Calculate average win to loss ratio."""
        if not self.trades:
            return 1.0
            
        winning_trades = [trade['pnl'] for trade in self.trades if trade['pnl'] > 0]
        losing_trades = [abs(trade['pnl']) for trade in self.trades if trade['pnl'] < 0]
        
        if not winning_trades or not losing_trades:
            return 1.0
            
        avg_win = np.mean(winning_trades)
        avg_loss = np.mean(losing_trades)
        return avg_win / avg_loss if avg_loss != 0 else 1.0
    
    def get_performance_metrics(self) -> Dict[str, float]:
        """Calculate advanced performance metrics."""
        if not self.trades:
            return {
                'total_return': 0.0,
                'sharpe_ratio': 0.0,
                'sortino_ratio': 0.0,
                'max_drawdown': 0.0,
                'calmar_ratio': 0.0,
                'information_ratio': 0.0,
                'win_rate': 0.0,
                'profit_factor': 0.0
            }
            
        # Calculate returns
        portfolio_values = pd.Series([t['portfolio_value'] for t in self.trades])
        returns = portfolio_values.pct_change().dropna()
        
        # Basic metrics
        total_return = (portfolio_values.iloc[-1] / portfolio_values.iloc[0]) - 1
        sharpe_ratio = np.sqrt(252) * returns.mean() / returns.std() if returns.std() != 0 else 0
        
        # Sortino ratio
        downside_returns = returns[returns < 0]
        sortino_ratio = np.sqrt(252) * returns.mean() / downside_returns.std() if len(downside_returns) > 0 else 0
        
        # Maximum drawdown
        peak = portfolio_values.expanding(min_periods=1).max()
        drawdown = (portfolio_values - peak) / peak
        max_drawdown = drawdown.min()
        
        # Calmar ratio
        calmar_ratio = total_return / abs(max_drawdown) if max_drawdown != 0 else 0
        
        # Information ratio (if market returns available)
        information_ratio = 0
        if 'market_returns' in self.trades[0]:
            market_returns = pd.Series([t['market_returns'] for t in self.trades])
            excess_returns = returns - market_returns
            information_ratio = np.sqrt(252) * excess_returns.mean() / excess_returns.std() if excess_returns.std() != 0 else 0
        
        # Win rate and profit factor
        profitable_trades = sum(1 for t in self.trades if t['pnl'] > 0)
        win_rate = profitable_trades / len(self.trades)
        
        gross_profit = sum(t['pnl'] for t in self.trades if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in self.trades if t['pnl'] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss != 0 else 0
        
        return {
            'total_return': total_return,
            'sharpe_ratio': sharpe_ratio,
            'sortino_ratio': sortino_ratio,
            'max_drawdown': max_drawdown,
            'calmar_ratio': calmar_ratio,
            'information_ratio': information_ratio,
            'win_rate': win_rate,
            'profit_factor': profit_factor
        } 