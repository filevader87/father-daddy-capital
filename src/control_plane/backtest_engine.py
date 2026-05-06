import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from prometheus_client import Counter, Histogram, Gauge
from concurrent.futures import ThreadPoolExecutor
from functools import partial

# Prometheus metrics
backtest_duration = Histogram('backtest_duration_seconds', 'Time taken for backtesting')
portfolio_metrics = Gauge('portfolio_metrics', 'Portfolio performance metrics', ['metric'])
trade_metrics = Counter('trade_metrics_total', 'Trading metrics', ['type'])
calculation_time = Histogram('calculation_time_seconds', 'Time taken for calculations', ['operation'])

@dataclass
class BacktestResults:
    """Container for backtest results."""
    portfolio_values: np.ndarray
    positions: np.ndarray
    trades: pd.DataFrame
    returns: np.ndarray
    sharpe_ratio: float
    max_drawdown: float
    total_return: float
    trade_count: int
    win_rate: float

class VectorizedBacktester:
    """
    High-performance backtesting engine using NumPy vectorization.
    """
    
    def __init__(self,
                 initial_capital: float = 100000.0,
                 commission: float = 0.001,
                 risk_free_rate: float = 0.02,
                 max_position_size: float = 1.0):
        """
        Initialize the backtester.
        
        Args:
            initial_capital: Starting capital
            commission: Trading commission (percentage)
            risk_free_rate: Annual risk-free rate
            max_position_size: Maximum position size (-1 to 1)
        """
        self.initial_capital = initial_capital
        self.commission = commission
        self.risk_free_rate = risk_free_rate
        self.max_position_size = max_position_size
        
    def _calculate_signals(self,
                          data: pd.DataFrame,
                          weights: Dict[str, float]) -> np.ndarray:
        """
        Calculate trading signals using vectorized operations.
        
        Args:
            data: Market data with indicators
            weights: Indicator weights
            
        Returns:
            Array of trading signals (-1 to 1)
        """
        with calculation_time.labels(operation='signals').time():
            # Vectorized signal calculation
            signals = np.zeros(len(data))
            
            # VWAP signals
            vwap_signal = (data['close'].values > data['vwap'].values).astype(float)
            signals += weights['vwap'] * vwap_signal
            
            # ADX signals (vectorized trend strength)
            adx_signal = (data['adx'].values > 25).astype(float)
            signals += weights['adx'] * adx_signal
            
            # RSI signals (vectorized overbought/oversold)
            rsi = data['rsi'].values
            rsi_signal = np.where(rsi < 30, 1, np.where(rsi > 70, -1, 0))
            signals += weights['rsi'] * rsi_signal
            
            # MACD signals (vectorized crossovers)
            macd_signal = (data['macd'].values > data['macd_signal'].values).astype(float)
            signals += weights['macd'] * macd_signal
            
            # Normalize signals to [-1, 1]
            signals = np.clip(signals, -1, 1)
            
            return signals
            
    def _calculate_position_sizes(self,
                                signals: np.ndarray,
                                volatility: np.ndarray) -> np.ndarray:
        """
        Calculate position sizes with volatility scaling.
        
        Args:
            signals: Trading signals
            volatility: Historical volatility
            
        Returns:
            Array of position sizes
        """
        with calculation_time.labels(operation='positions').time():
            # Scale positions by volatility
            vol_scale = 0.2 / volatility  # Target 20% annualized volatility
            positions = signals * vol_scale
            
            # Apply position limits
            positions = np.clip(positions, -self.max_position_size, self.max_position_size)
            
            return positions
            
    def _calculate_trades(self,
                         positions: np.ndarray,
                         prices: np.ndarray) -> Tuple[pd.DataFrame, np.ndarray]:
        """
        Calculate trades and costs from position changes.
        
        Args:
            positions: Position sizes
            prices: Asset prices
            
        Returns:
            DataFrame of trades and array of costs
        """
        with calculation_time.labels(operation='trades').time():
            # Calculate position changes
            position_changes = np.diff(positions, prepend=0)
            
            # Calculate trade sizes and costs
            trade_sizes = np.abs(position_changes)
            trade_costs = trade_sizes * prices * self.commission
            
            # Create trade DataFrame
            trades = pd.DataFrame({
                'size': position_changes[position_changes != 0],
                'price': prices[position_changes != 0],
                'cost': trade_costs[position_changes != 0]
            })
            
            # Update trade metrics
            trade_metrics.labels(type='buys').inc(len(trades[trades['size'] > 0]))
            trade_metrics.labels(type='sells').inc(len(trades[trades['size'] < 0]))
            
            return trades, trade_costs
            
    def _calculate_returns(self,
                          positions: np.ndarray,
                          price_returns: np.ndarray,
                          costs: np.ndarray) -> np.ndarray:
        """
        Calculate strategy returns including costs.
        
        Args:
            positions: Position sizes
            price_returns: Asset returns
            costs: Trading costs
            
        Returns:
            Array of strategy returns (length: n-2 due to alignment)
        """
        with calculation_time.labels(operation='returns').time():
            # Calculate position returns (use positions from t-1 for returns at t)
            position_returns = positions[:-1] * price_returns[1:]  # Length: n-1
            
            # Subtract costs (align with position returns)
            strategy_returns = position_returns[:-1] - costs[1:-1]  # Length: n-2
            
            return strategy_returns
            
    def _calculate_portfolio_metrics(self,
                                   returns: np.ndarray) -> Tuple[float, float, float]:
        """
        Calculate portfolio performance metrics.
        
        Args:
            returns: Strategy returns
            
        Returns:
            Tuple of (sharpe_ratio, max_drawdown, total_return)
        """
        with calculation_time.labels(operation='metrics').time():
            # Calculate Sharpe ratio
            excess_returns = returns - self.risk_free_rate / 252
            sharpe = np.sqrt(252) * np.mean(excess_returns) / np.std(returns)
            
            # Calculate drawdown using cumulative returns
            cum_returns = np.cumprod(1 + np.nan_to_num(returns, 0))  # Replace NaN with 0
            peak = np.maximum.accumulate(cum_returns)
            drawdowns = (cum_returns - peak) / peak  # This gives negative values for drawdowns
            max_drawdown = float(np.min(drawdowns))  # Most negative value
            
            # Calculate total return
            total_return = np.prod(1 + np.nan_to_num(returns, 0)) - 1
            
            # Update portfolio metrics
            portfolio_metrics.labels(metric='sharpe_ratio').set(sharpe)
            portfolio_metrics.labels(metric='max_drawdown').set(max_drawdown)
            portfolio_metrics.labels(metric='total_return').set(total_return)
            
            return sharpe, max_drawdown, total_return
    
    def run_backtest(self,
                     data: pd.DataFrame,
                     weights: Dict[str, float]) -> BacktestResults:
        """
        Run vectorized backtest.
        
        Args:
            data: Market data with indicators
            weights: Indicator weights
            
        Returns:
            BacktestResults object
        """
        with backtest_duration.time():
            # Calculate signals
            signals = self._calculate_signals(data, weights)
            
            # Calculate volatility (20-day rolling)
            returns = data['close'].pct_change()
            volatility = returns.rolling(20).std() * np.sqrt(252)
            
            # Calculate positions
            positions = self._calculate_position_sizes(signals, volatility.values)
            
            # Calculate trades and costs
            trades, costs = self._calculate_trades(positions, data['close'].values)
            
            # Calculate strategy returns
            strategy_returns = self._calculate_returns(
                positions,
                returns.values,
                costs
            )
            
            # Calculate portfolio values
            portfolio_values = np.zeros(len(data))
            portfolio_values[0] = self.initial_capital
            portfolio_values[1:] = self.initial_capital * np.cumprod(
                1 + np.concatenate([[0], strategy_returns])
            )
            
            # Calculate performance metrics
            sharpe, max_drawdown, total_return = self._calculate_portfolio_metrics(
                strategy_returns
            )
            
            # Calculate win rate
            winning_trades = len(trades[trades['size'] * trades['price'].diff() > 0])
            win_rate = winning_trades / len(trades) if len(trades) > 0 else 0
            
            return BacktestResults(
                portfolio_values=portfolio_values,
                positions=positions,
                trades=trades,
                returns=strategy_returns,
                sharpe_ratio=float(sharpe),
                max_drawdown=float(max_drawdown),
                total_return=float(total_return),
                trade_count=len(trades),
                win_rate=float(win_rate)
            ) 