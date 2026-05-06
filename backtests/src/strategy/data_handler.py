import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional
from datetime import datetime

class MarketDataHandler:
    """Efficient market data handler using NumPy arrays for faster operations."""
    
    def __init__(self):
        self._data_cache: Dict[str, np.ndarray] = {}
        self._column_map: Dict[str, Dict[str, int]] = {}
        self._symbols: list = []
        
    def preload_data(self, data: pd.DataFrame, symbol: str) -> None:
        """
        Preload market data into memory-efficient NumPy arrays.
        
        Args:
            data: DataFrame with OHLCV data
            symbol: Trading symbol
        """
        # Convert DataFrame to structured numpy array for efficient memory usage
        required_columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        
        # Ensure all required columns exist
        for col in required_columns:
            if col not in data.columns:
                raise ValueError(f"Missing required column: {col}")
        
        # Create column mapping for fast access
        self._column_map[symbol] = {col: idx for idx, col in enumerate(required_columns)}
        
        # Convert to numpy array with optimized dtypes
        structured_data = np.zeros(len(data), dtype=[
            ('timestamp', 'datetime64[ns]'),
            ('open', 'float32'),
            ('high', 'float32'),
            ('low', 'float32'),
            ('close', 'float32'),
            ('volume', 'float32')
        ])
        
        for col in required_columns:
            structured_data[col] = data[col].values
            
        self._data_cache[symbol] = structured_data
        if symbol not in self._symbols:
            self._symbols.append(symbol)
            
    def get_data_window(self, symbol: str, start_idx: int, end_idx: int) -> np.ndarray:
        """
        Get a window of market data efficiently.
        
        Args:
            symbol: Trading symbol
            start_idx: Start index
            end_idx: End index
            
        Returns:
            np.ndarray: Market data window
        """
        if symbol not in self._data_cache:
            raise KeyError(f"Symbol {symbol} not found in data cache")
            
        return self._data_cache[symbol][start_idx:end_idx]
    
    def get_latest_data(self, symbol: str, lookback: int = 1) -> np.ndarray:
        """
        Get the most recent market data.
        
        Args:
            symbol: Trading symbol
            lookback: Number of periods to look back
            
        Returns:
            np.ndarray: Recent market data
        """
        if symbol not in self._data_cache:
            raise KeyError(f"Symbol {symbol} not found in data cache")
            
        data = self._data_cache[symbol]
        return data[-lookback:]
    
    def calculate_indicators(self, symbol: str, window_size: int = 20) -> Dict[str, np.ndarray]:
        """
        Calculate common indicators using vectorized operations.
        
        Args:
            symbol: Trading symbol
            window_size: Rolling window size
            
        Returns:
            Dict[str, np.ndarray]: Dictionary of calculated indicators
        """
        if symbol not in self._data_cache:
            raise KeyError(f"Symbol {symbol} not found in data cache")
            
        data = self._data_cache[symbol]
        close = data['close']
        high = data['high']
        low = data['low']
        volume = data['volume']
        
        # Vectorized calculations
        returns = np.diff(close) / close[:-1]
        returns = np.insert(returns, 0, 0)  # Add 0 at the beginning for alignment
        
        # Volatility (rolling standard deviation)
        volatility = np.array([np.std(returns[max(0, i-window_size):i]) 
                             for i in range(1, len(returns)+1)])
        
        # VWAP
        vwap = np.cumsum(volume * (high + low + close) / 3) / np.cumsum(volume)
        
        # ATR components
        tr1 = high - low
        tr2 = np.abs(high - np.roll(close, 1))
        tr3 = np.abs(low - np.roll(close, 1))
        tr = np.maximum.reduce([tr1, tr2, tr3])
        atr = np.array([np.mean(tr[max(0, i-window_size):i]) 
                       for i in range(1, len(tr)+1)])
        
        return {
            'returns': returns,
            'volatility': volatility,
            'vwap': vwap,
            'atr': atr
        }
    
    def get_symbols(self) -> list:
        """Get list of available symbols."""
        return self._symbols.copy()
    
    def get_data_length(self, symbol: str) -> int:
        """Get the length of available data for a symbol."""
        if symbol not in self._data_cache:
            raise KeyError(f"Symbol {symbol} not found in data cache")
        return len(self._data_cache[symbol])
    
    def clear_cache(self, symbol: Optional[str] = None) -> None:
        """
        Clear data cache for memory management.
        
        Args:
            symbol: Optional symbol to clear specific data
        """
        if symbol is None:
            self._data_cache.clear()
            self._column_map.clear()
            self._symbols.clear()
        else:
            if symbol in self._data_cache:
                del self._data_cache[symbol]
                del self._column_map[symbol]
                self._symbols.remove(symbol) 