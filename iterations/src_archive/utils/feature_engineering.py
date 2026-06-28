import numpy as np
import pandas as pd
import ta
from typing import List, Dict, Union, Optional

class FeatureEngineer:
    def __init__(
        self,
        window_sizes: List[int] = [5, 10, 20],
        indicators: List[str] = ['sma', 'ema', 'rsi', 'macd'],
        normalization: bool = True,
        fill_method: str = 'ffill'
    ):
        """
        Initialize the feature engineering pipeline.
        
        Args:
            window_sizes: List of window sizes for technical indicators
            indicators: List of technical indicators to compute
            normalization: Whether to normalize features
            fill_method: Method to fill missing values ('ffill', 'bfill', or 'zero')
        """
        self.window_sizes = window_sizes
        self.indicators = indicators
        self.normalization = normalization
        self.fill_method = fill_method
        
        # Validate inputs
        self._validate_inputs()
    
    def _validate_inputs(self):
        """Validate initialization parameters."""
        valid_indicators = ['sma', 'ema', 'rsi', 'macd', 'bbands', 'stoch']
        for indicator in self.indicators:
            if indicator not in valid_indicators:
                raise ValueError(f"Invalid indicator: {indicator}. Must be one of {valid_indicators}")
        
        if self.fill_method not in ['ffill', 'bfill', 'zero']:
            raise ValueError("fill_method must be one of: 'ffill', 'bfill', 'zero'")
    
    def process(self, data: Union[pd.DataFrame, np.ndarray]) -> pd.DataFrame:
        """
        Process market data and extract features.
        
        Args:
            data: Market data as DataFrame or numpy array
                 If DataFrame, expected columns: ['open', 'high', 'low', 'close', 'volume']
                 If numpy array, expected shape: (n_samples, n_features)
        
        Returns:
            pd.DataFrame: Processed features
        """
        # Convert numpy array to DataFrame if needed
        if isinstance(data, np.ndarray):
            data = pd.DataFrame(data)
        
        # Check for missing values
        if data.isnull().any().any():
            raise ValueError("Input data contains missing values")
        
        features = []
        
        # Technical indicators
        for window in self.window_sizes:
            if 'sma' in self.indicators:
                features.append(ta.trend.sma_indicator(data['close'], window=window))
            if 'ema' in self.indicators:
                features.append(ta.trend.ema_indicator(data['close'], window=window))
            if 'rsi' in self.indicators:
                features.append(ta.momentum.rsi(data['close'], window=window))
        
        if 'macd' in self.indicators:
            macd = ta.trend.MACD(data['close'])
            features.extend([
                macd.macd(),
                macd.macd_signal(),
                macd.macd_diff()
            ])
        
        if 'bbands' in self.indicators:
            bb = ta.volatility.BollingerBands(data['close'])
            features.extend([
                bb.bollinger_hband(),
                bb.bollinger_lband(),
                bb.bollinger_mavg()
            ])
        
        if 'stoch' in self.indicators:
            stoch = ta.momentum.StochasticOscillator(
                data['high'],
                data['low'],
                data['close']
            )
            features.extend([
                stoch.stoch(),
                stoch.stoch_signal()
            ])
        
        # Combine features
        feature_df = pd.concat(features, axis=1)
        
        # Fill missing values
        if self.fill_method == 'ffill':
            feature_df = feature_df.fillna(method='ffill')
        elif self.fill_method == 'bfill':
            feature_df = feature_df.fillna(method='bfill')
        else:  # zero
            feature_df = feature_df.fillna(0)
        
        # Normalize if requested
        if self.normalization:
            feature_df = (feature_df - feature_df.mean()) / feature_df.std()
        
        return feature_df
    
    def get_feature_names(self) -> List[str]:
        """Get list of feature names."""
        names = []
        for window in self.window_sizes:
            if 'sma' in self.indicators:
                names.append(f'sma_{window}')
            if 'ema' in self.indicators:
                names.append(f'ema_{window}')
            if 'rsi' in self.indicators:
                names.append(f'rsi_{window}')
        
        if 'macd' in self.indicators:
            names.extend(['macd', 'macd_signal', 'macd_diff'])
        
        if 'bbands' in self.indicators:
            names.extend(['bb_high', 'bb_low', 'bb_ma'])
        
        if 'stoch' in self.indicators:
            names.extend(['stoch', 'stoch_signal'])
        
        return names

def micro_feature(order_book):
    """Extract micro-level feature from order book data."""
    return float(np.mean(order_book))

def macro_feature(fed_rates):
    """Extract macro-level feature from Federal Reserve rates."""
    return float(0.5 * fed_rates)

def sentiment_feature(tweet_sentiment):
    """Extract and normalize sentiment feature from tweet data."""
    return float(np.clip(tweet_sentiment, -1, 1)) 