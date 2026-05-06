"""
Vectorized Feature Engineering Module
------------------------------------
This module provides high-performance feature engineering using vectorized operations
to replace Python loops with Pandas/NumPy operations for maximum performance.
"""

import numpy as np
import pandas as pd
from typing import List, Dict, Union, Optional, Tuple
import logging
from functools import lru_cache
import warnings

warnings.filterwarnings('ignore', category=FutureWarning)
logger = logging.getLogger(__name__)

class VectorizedFeatureEngineer:
    """High-performance feature engineering using vectorized operations."""
    
    def __init__(self, 
                 window_sizes: List[int] = [5, 10, 20, 50],
                 indicators: List[str] = ['sma', 'ema', 'rsi', 'macd', 'bbands', 'stoch', 'adx'],
                 normalization: bool = True,
                 fill_method: str = 'ffill'):
        """Initialize vectorized feature engineer.
        
        Args:
            window_sizes: List of window sizes for technical indicators
            indicators: List of technical indicators to compute
            normalization: Whether to normalize features
            fill_method: Method to fill missing values
        """
        self.window_sizes = window_sizes
        self.indicators = indicators
        self.normalization = normalization
        self.fill_method = fill_method
        self._validate_inputs()
    
    def _validate_inputs(self):
        """Validate initialization parameters."""
        valid_indicators = ['sma', 'ema', 'rsi', 'macd', 'bbands', 'stoch', 'adx', 'atr', 'cci', 'williams_r']
        for indicator in self.indicators:
            if indicator not in valid_indicators:
                raise ValueError(f"Invalid indicator: {indicator}. Must be one of {valid_indicators}")
        
        if self.fill_method not in ['ffill', 'bfill', 'zero', 'interpolate']:
            raise ValueError("fill_method must be one of: 'ffill', 'bfill', 'zero', 'interpolate'")
    
    def process(self, data: Union[pd.DataFrame, np.ndarray]) -> pd.DataFrame:
        """Process market data and extract features using vectorized operations.
        
        Args:
            data: Market data as DataFrame or numpy array
                 If DataFrame, expected columns: ['open', 'high', 'low', 'close', 'volume']
                 If numpy array, expected shape: (n_samples, n_features)
        
        Returns:
            pd.DataFrame: Processed features
        """
        # Convert numpy array to DataFrame if needed
        if isinstance(data, np.ndarray):
            data = pd.DataFrame(data, columns=['open', 'high', 'low', 'close', 'volume'])
        
        # Validate input data
        required_columns = ['open', 'high', 'low', 'close', 'volume']
        if not all(col in data.columns for col in required_columns):
            raise ValueError(f"Input data must contain columns: {required_columns}")
        
        # Create copy to avoid modifying original data
        df = data.copy()
        
        # Calculate all features using vectorized operations
        features_df = self._calculate_all_features_vectorized(df)
        
        # Handle missing values
        features_df = self._handle_missing_values_vectorized(features_df)
        
        # Normalize if requested
        if self.normalization:
            features_df = self._normalize_features_vectorized(features_df)
        
        return features_df
    
    def _calculate_all_features_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate all features using vectorized operations."""
        features_list = []
        
        # Price-based features (vectorized)
        price_features = self._calculate_price_features_vectorized(df)
        features_list.append(price_features)
        
        # Volume-based features (vectorized)
        volume_features = self._calculate_volume_features_vectorized(df)
        features_list.append(volume_features)
        
        # Technical indicators (vectorized)
        for indicator in self.indicators:
            if indicator == 'sma':
                sma_features = self._calculate_sma_vectorized(df)
                features_list.append(sma_features)
            elif indicator == 'ema':
                ema_features = self._calculate_ema_vectorized(df)
                features_list.append(ema_features)
            elif indicator == 'rsi':
                rsi_features = self._calculate_rsi_vectorized(df)
                features_list.append(rsi_features)
            elif indicator == 'macd':
                macd_features = self._calculate_macd_vectorized(df)
                features_list.append(macd_features)
            elif indicator == 'bbands':
                bb_features = self._calculate_bollinger_bands_vectorized(df)
                features_list.append(bb_features)
            elif indicator == 'stoch':
                stoch_features = self._calculate_stochastic_vectorized(df)
                features_list.append(stoch_features)
            elif indicator == 'adx':
                adx_features = self._calculate_adx_vectorized(df)
                features_list.append(adx_features)
            elif indicator == 'atr':
                atr_features = self._calculate_atr_vectorized(df)
                features_list.append(atr_features)
            elif indicator == 'cci':
                cci_features = self._calculate_cci_vectorized(df)
                features_list.append(cci_features)
            elif indicator == 'williams_r':
                williams_features = self._calculate_williams_r_vectorized(df)
                features_list.append(williams_features)
        
        # Combine all features
        if features_list:
            return pd.concat(features_list, axis=1)
        else:
            return pd.DataFrame(index=df.index)
    
    def _calculate_price_features_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate price-based features using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        # Returns (vectorized)
        features['returns'] = df['close'].pct_change()
        features['log_returns'] = np.log(df['close'] / df['close'].shift(1))
        
        # Price ranges (vectorized)
        features['high_low_ratio'] = df['high'] / df['low']
        features['close_open_ratio'] = df['close'] / df['open']
        features['price_range'] = (df['high'] - df['low']) / df['close']
        
        # Price momentum (vectorized)
        for window in self.window_sizes:
            features[f'momentum_{window}'] = df['close'] / df['close'].shift(window) - 1
            features[f'volatility_{window}'] = features['returns'].rolling(window).std()
        
        # Price levels (vectorized)
        features['price_position'] = (df['close'] - df['low']) / (df['high'] - df['low'])
        
        return features
    
    def _calculate_volume_features_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate volume-based features using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        # Volume momentum (vectorized)
        features['volume_momentum'] = df['volume'].pct_change()
        
        # Volume moving averages (vectorized)
        for window in self.window_sizes:
            features[f'volume_sma_{window}'] = df['volume'].rolling(window).mean()
            features[f'volume_ratio_{window}'] = df['volume'] / features[f'volume_sma_{window}']
        
        # Volume-price relationship (vectorized)
        returns = df['close'].pct_change()
        features['volume_price_trend'] = (df['volume'] * returns).rolling(20).sum()
        
        # Volume volatility (vectorized)
        features['volume_volatility'] = df['volume'].rolling(20).std() / df['volume'].rolling(20).mean()
        
        return features
    
    def _calculate_sma_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Simple Moving Averages using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        for window in self.window_sizes:
            features[f'sma_{window}'] = df['close'].rolling(window).mean()
            features[f'sma_ratio_{window}'] = df['close'] / features[f'sma_{window}']
        
        return features
    
    def _calculate_ema_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Exponential Moving Averages using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        for window in self.window_sizes:
            features[f'ema_{window}'] = df['close'].ewm(span=window).mean()
            features[f'ema_ratio_{window}'] = df['close'] / features[f'ema_{window}']
        
        return features
    
    def _calculate_rsi_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate RSI using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        # Calculate price changes (vectorized)
        delta = df['close'].diff()
        
        for window in self.window_sizes:
            # Separate gains and losses (vectorized)
            gains = delta.where(delta > 0, 0)
            losses = -delta.where(delta < 0, 0)
            
            # Calculate average gains and losses (vectorized)
            avg_gains = gains.rolling(window).mean()
            avg_losses = losses.rolling(window).mean()
            
            # Calculate RS and RSI (vectorized)
            rs = avg_gains / avg_losses
            features[f'rsi_{window}'] = 100 - (100 / (1 + rs))
        
        return features
    
    def _calculate_macd_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate MACD using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        # Calculate EMAs (vectorized)
        ema12 = df['close'].ewm(span=12).mean()
        ema26 = df['close'].ewm(span=26).mean()
        
        # Calculate MACD line (vectorized)
        features['macd_line'] = ema12 - ema26
        
        # Calculate signal line (vectorized)
        features['macd_signal'] = features['macd_line'].ewm(span=9).mean()
        
        # Calculate histogram (vectorized)
        features['macd_histogram'] = features['macd_line'] - features['macd_signal']
        
        # Calculate MACD ratio (vectorized)
        features['macd_ratio'] = features['macd_line'] / features['macd_signal']
        
        return features
    
    def _calculate_bollinger_bands_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Bollinger Bands using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        for window in self.window_sizes:
            # Calculate moving average and standard deviation (vectorized)
            ma = df['close'].rolling(window).mean()
            std = df['close'].rolling(window).std()
            
            # Calculate bands (vectorized)
            features[f'bb_upper_{window}'] = ma + (std * 2)
            features[f'bb_lower_{window}'] = ma - (std * 2)
            features[f'bb_middle_{window}'] = ma
            
            # Calculate bandwidth and %B (vectorized)
            features[f'bb_bandwidth_{window}'] = (features[f'bb_upper_{window}'] - features[f'bb_lower_{window}']) / ma
            features[f'bb_percent_b_{window}'] = (df['close'] - features[f'bb_lower_{window}']) / (features[f'bb_upper_{window}'] - features[f'bb_lower_{window}'])
        
        return features
    
    def _calculate_stochastic_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Stochastic Oscillator using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        for window in self.window_sizes:
            # Calculate highest high and lowest low (vectorized)
            highest_high = df['high'].rolling(window).max()
            lowest_low = df['low'].rolling(window).min()
            
            # Calculate %K (vectorized)
            features[f'stoch_k_{window}'] = 100 * (df['close'] - lowest_low) / (highest_high - lowest_low)
            
            # Calculate %D (vectorized)
            features[f'stoch_d_{window}'] = features[f'stoch_k_{window}'].rolling(3).mean()
        return features
    
    def _calculate_adx_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate ADX using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        # Calculate True Range (vectorized)
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift(1))
        low_close = np.abs(df['low'] - df['close'].shift(1))
        true_range = np.maximum(high_low, np.maximum(high_close, low_close))
        
        # Calculate Directional Movement (vectorized)
        up_move = df['high'] - df['high'].shift(1)
        down_move = df['low'].shift(1) - df['low']
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
        
        for window in self.window_sizes:
            # Calculate smoothed values (vectorized)
            atr = true_range.rolling(window).mean()
            plus_di = 100 * pd.Series(plus_dm).rolling(window).mean() / atr
            minus_di = 100 * pd.Series(minus_dm).rolling(window).mean() / atr
            
            # Calculate ADX (vectorized)
            dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
            features[f'adx_{window}'] = dx.rolling(window).mean()
        
        return features
    
    def _calculate_atr_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Average True Range using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        # Calculate True Range (vectorized)
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift(1))
        low_close = np.abs(df['low'] - df['close'].shift(1))
        true_range = np.maximum(high_low, np.maximum(high_close, low_close))
        
        for window in self.window_sizes:
            features[f'atr_{window}'] = true_range.rolling(window).mean()
        
        return features
    
    def _calculate_cci_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Commodity Channel Index using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        for window in self.window_sizes:
            # Calculate typical price (vectorized)
            typical_price = (df['high'] + df['low'] + df['close']) / 3
            
            # Calculate moving average and mean deviation (vectorized)
            ma = typical_price.rolling(window).mean()
            mean_deviation = np.abs(typical_price - ma).rolling(window).mean()
            
            # Calculate CCI (vectorized)
            features[f'cci_{window}'] = (typical_price - ma) / (0.015 * mean_deviation)
        
        return features
    
    def _calculate_williams_r_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Williams %R using vectorized operations."""
        features = pd.DataFrame(index=df.index)
        
        for window in self.window_sizes:
            # Calculate highest high and lowest low (vectorized)
            highest_high = df['high'].rolling(window).max()
            lowest_low = df['low'].rolling(window).min()
            
            # Calculate Williams %R (vectorized)
            features[f'williams_r_{window}'] = -100 * (highest_high - df['close']) / (highest_high - lowest_low)
        
        return features
    
    def _handle_missing_values_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Handle missing values using vectorized operations."""
        if self.fill_method == 'ffill':
            return df.fillna(method='ffill')
        elif self.fill_method == 'bfill':
            return df.fillna(method='bfill')
        elif self.fill_method == 'interpolate':
            return df.interpolate(method='linear')
        else:  # zero
            return df.fillna(0)
    
    def _normalize_features_vectorized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize features using vectorized operations."""
        # Z-score normalization (vectorized)
        mean = df.mean()
        std = df.std()
        
        # Avoid division by zero
        std = std.replace(0, 1)
        
        return (df - mean) / std
    
    def get_feature_names(self) -> List[str]:
        """Get list of feature names."""
        names = []
        
        # Price features
        names.extend(['returns', 'log_returns', 'high_low_ratio', 'close_open_ratio', 'price_range', 'price_position'])
        for window in self.window_sizes:
            names.extend([f'momentum_{window}', f'volatility_{window}'])
        
        # Volume features
        names.extend(['volume_momentum', 'volume_price_trend', 'volume_volatility'])
        for window in self.window_sizes:
            names.extend([f'volume_sma_{window}', f'volume_ratio_{window}'])
        
        # Technical indicators
        for indicator in self.indicators:
            if indicator == 'sma':
                for window in self.window_sizes:
                    names.extend([f'sma_{window}', f'sma_ratio_{window}'])
            elif indicator == 'ema':
                for window in self.window_sizes:
                    names.extend([f'ema_{window}', f'ema_ratio_{window}'])
            elif indicator == 'rsi':
                for window in self.window_sizes:
                    names.append(f'rsi_{window}')
            elif indicator == 'macd':
                names.extend(['macd_line', 'macd_signal', 'macd_histogram', 'macd_ratio'])
            elif indicator == 'bbands':
                for window in self.window_sizes:
                    names.extend([f'bb_upper_{window}', f'bb_lower_{window}', f'bb_middle_{window}', 
                                f'bb_bandwidth_{window}', f'bb_percent_b_{window}'])
            elif indicator == 'stoch':
                for window in self.window_sizes:
                    names.extend([f'stoch_k_{window}', f'stoch_d_{window}'])
            elif indicator == 'adx':
                for window in self.window_sizes:
                    names.append(f'adx_{window}')
            elif indicator == 'atr':
                for window in self.window_sizes:
                    names.append(f'atr_{window}')
            elif indicator == 'cci':
                for window in self.window_sizes:
                    names.append(f'cci_{window}')
            elif indicator == 'williams_r':
                for window in self.window_sizes:
                    names.append(f'williams_r_{window}')
        
        return names

# Global instance for performance
_vectorized_feature_engineer = None

def get_vectorized_feature_engineer() -> VectorizedFeatureEngineer:
    """Get or create global vectorized feature engineer instance."""
    global _vectorized_feature_engineer
    
    if _vectorized_feature_engineer is None:
        _vectorized_feature_engineer = VectorizedFeatureEngineer()
    
    return _vectorized_feature_engineer 