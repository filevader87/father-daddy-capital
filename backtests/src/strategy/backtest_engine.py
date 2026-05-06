import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from .data_handler import MarketDataHandler
from .indicators import EnhancedIndicators
from .position_sizing import PositionSizer
from .risk_management import RiskManager
from .performance_monitor import PerformanceMonitor

class OptimizedBacktestEngine:
    """High-performance backtesting engine using NumPy operations."""
    
    def __init__(self, initial_capital: float = 100000.0):
        self.initial_capital = initial_capital
        self.data_handler = MarketDataHandler()
        self.indicators = EnhancedIndicators()
        self.position_sizer = PositionSizer()
        self.risk_manager = RiskManager()
        self.performance_monitor = PerformanceMonitor()
        
        # Performance tracking arrays
        self._positions = None
        self._equity = None
        self._trades = []
    
    def preload_market_data(self, data: pd.DataFrame, symbol: str) -> None:
        """Preload market data for faster backtesting."""
        self.data_handler.preload_data(data, symbol)
    
    def _initialize_arrays(self, data_length: int) -> None:
        """Initialize NumPy arrays for tracking positions and equity."""
        self._positions = np.zeros(data_length)
        self._equity = np.full(data_length, self.initial_capital)
        self._trades = []
    
    def _calculate_signals(self, data: np.ndarray, 
                          macro_metrics: Optional[Dict] = None,
                          sentiment_score: Optional[float] = None,
                          macro_risk_threshold: float = 0.7,
                          sentiment_threshold: float = 0.3) -> np.ndarray:
        """
        Calculate trading signals using vectorized operations.
        Returns array of -1 (short), 0 (neutral), 1 (long)
        """
        # Convert structured array to DataFrame for indicator calculations
        df = pd.DataFrame(data)
        
        # Calculate indicators (vectorized)
        df = self.indicators.calculate_vwap(df)
        df = self.indicators.calculate_adx(df)
        df = self.indicators.calculate_rsi(df)
        df = self.indicators.calculate_ml_score(df)
        
        # Generate base signals using NumPy operations
        long_signal = (df['vwap_signal'] & df['adx_signal'] & df['rsi_oversold']).values
        short_signal = (df['vwap_signal'] & df['adx_signal'] & df['rsi_overbought']).values
        
        # Initialize signals array
        signals = np.zeros(len(data))
        signals[long_signal] = 1
        signals[short_signal] = -1
        
        # Apply macro filtering if available
        if macro_metrics is not None:
            recession_risk = macro_metrics.get('recession_risk', 0.0)
            if recession_risk > macro_risk_threshold:
                # Reduce position sizes during high risk periods
                signals = signals * (1 - (recession_risk - macro_risk_threshold))
        
        # Apply sentiment filtering if available
        if sentiment_score is not None:
            if abs(sentiment_score) > sentiment_threshold:
                # Align signals with strong sentiment
                sentiment_alignment = np.sign(sentiment_score)
                # Reduce signals that go against sentiment
                signals = np.where(
                    np.sign(signals) == sentiment_alignment,
                    signals,
                    signals * (1 - abs(sentiment_score))
                )
        
        return signals
    
    def _calculate_position_sizes(self, data: np.ndarray, signals: np.ndarray,
                                macro_metrics: Optional[Dict] = None) -> np.ndarray:
        """Calculate position sizes using vectorized operations."""
        df = pd.DataFrame(data)
        
        # Calculate base position sizes
        df = self.position_sizer.calculate_position_size(df, self.initial_capital)
        df = self.position_sizer.adjust_for_drawdown(df)
        
        # Apply macro-based position scaling if available
        if macro_metrics is not None:
            market_health = macro_metrics.get('market_health', 0.5)
            risk_appetite = macro_metrics.get('risk_appetite', 0.5)
            
            # Scale positions based on market conditions
            macro_scale = (market_health + risk_appetite) / 2
            df['position_size'] = df['position_size'] * macro_scale
        
        # Apply signals to position sizes
        position_sizes = df['position_size'].values * signals
        return position_sizes
    
    def _apply_risk_management(self, data: np.ndarray, positions: np.ndarray,
                             macro_metrics: Optional[Dict] = None) -> np.ndarray:
        """Apply risk management rules using vectorized operations."""
        df = pd.DataFrame(data)
        
        # Apply standard risk management
        df = self.risk_manager.apply_risk_management(df)
        
        # Adjust positions based on risk signals
        adjusted_positions = positions.copy()
        adjusted_positions[~df['risk_signal'].values] = 0
        
        # Apply stop-loss
        long_stops = positions > 0 & df['long_stop_hit'].values
        short_stops = positions < 0 & df['short_stop_hit'].values
        adjusted_positions[long_stops | short_stops] = 0
        
        # Apply macro-based risk adjustments if available
        if macro_metrics is not None:
            recession_risk = macro_metrics.get('recession_risk', 0.0)
            # Tighten risk management during high-risk periods
            if recession_risk > 0.5:
                # Scale down positions based on recession risk
                adjusted_positions = adjusted_positions * (1 - (recession_risk - 0.5))
        
        return adjusted_positions
    
    def _calculate_returns(self, data: np.ndarray, positions: np.ndarray) -> np.ndarray:
        """Calculate returns using vectorized operations."""
        price_returns = np.diff(data['close']) / data['close'][:-1]
        price_returns = np.insert(price_returns, 0, 0)  # Add 0 at the beginning
        
        # Calculate position returns
        position_returns = positions * price_returns
        
        return position_returns
    
    def run_backtest(self, symbol: str,
                    macro_metrics: Optional[Dict] = None,
                    sentiment_score: Optional[float] = None,
                    macro_risk_threshold: float = 0.7,
                    sentiment_threshold: float = 0.3) -> pd.DataFrame:
        """
        Run the backtest using optimized NumPy operations.
        
        Args:
            symbol: Trading symbol
            macro_metrics: Optional macroeconomic metrics
            sentiment_score: Optional market sentiment score
            macro_risk_threshold: Threshold for macro risk filtering
            sentiment_threshold: Threshold for sentiment filtering
            
        Returns:
            pd.DataFrame: Backtest results
        """
        # Get data
        data = self.data_handler.get_data_window(
            symbol, 0, self.data_handler.get_data_length(symbol)
        )
        
        # Initialize tracking arrays
        self._initialize_arrays(len(data))
        
        # Calculate signals with macro and sentiment filtering
        signals = self._calculate_signals(
            data,
            macro_metrics=macro_metrics,
            sentiment_score=sentiment_score,
            macro_risk_threshold=macro_risk_threshold,
            sentiment_threshold=sentiment_threshold
        )
        
        # Calculate position sizes with macro adjustment
        position_sizes = self._calculate_position_sizes(data, signals, macro_metrics)
        
        # Apply risk management with macro consideration
        self._positions = self._apply_risk_management(data, position_sizes, macro_metrics)
        
        # Calculate returns
        returns = self._calculate_returns(data, self._positions)
        
        # Update equity curve
        self._equity[1:] = self.initial_capital * (1 + np.cumsum(returns[1:]))
        
        # Create results DataFrame
        results = pd.DataFrame({
            'timestamp': data['timestamp'],
            'close': data['close'],
            'position': self._positions,
            'equity': self._equity,
            'returns': returns
        })
        
        # Add macro and sentiment metrics if available
        if macro_metrics is not None:
            for key, value in macro_metrics.items():
                results[f'macro_{key}'] = value
        if sentiment_score is not None:
            results['sentiment_score'] = sentiment_score
        
        # Calculate and log performance metrics
        metrics = self.performance_monitor.calculate_trade_metrics(results)
        self.performance_monitor.log_performance(results, metrics, symbol)
        
        return results
    
    def get_performance_summary(self, results: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Generate performance summary."""
        metrics = self.performance_monitor.calculate_trade_metrics(results)
        return self.performance_monitor.generate_summary_report(results, metrics, symbol)
    
    def plot_results(self, results: pd.DataFrame) -> None:
        """Plot backtest results using matplotlib."""
        try:
            import matplotlib.pyplot as plt
            
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
            
            # Plot price and equity
            ax1.plot(results['timestamp'], results['close'], label='Price')
            ax1.set_title('Price Chart')
            ax1.legend()
            
            ax2.plot(results['timestamp'], results['equity'], label='Equity')
            ax2.set_title('Equity Curve')
            ax2.legend()
            
            plt.tight_layout()
            plt.show()
            
        except ImportError:
            print("Matplotlib is required for plotting. Please install it first.") 