from dataclasses import dataclass
from enum import Enum
import numpy as np
import pandas as pd
from typing import Dict, Optional, Tuple
from prometheus_client import Gauge, Counter, Histogram

# Prometheus metrics
regime_changes = Counter('market_regime_changes_total', 'Total number of market regime changes', ['from_regime', 'to_regime'])
regime_duration = Gauge('market_regime_duration_seconds', 'Duration of current market regime', ['regime'])
regime_confidence = Gauge('market_regime_confidence', 'Confidence in current regime classification', ['regime'])
regime_metrics = Gauge('market_regime_metrics', 'Various regime detection metrics', ['metric'])

class MarketRegime(Enum):
    """Market regime classification."""
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"

@dataclass
class RegimeMetrics:
    """Container for regime detection metrics."""
    adx: float
    volatility: float
    trend_strength: float
    range_strength: float
    momentum: float

class MarketRegimeDetector:
    """
    Advanced market regime detection system that combines multiple technical indicators
    to classify market conditions and adapt trading strategies accordingly.
    """
    
    def __init__(self, 
                 adx_window: int = 14,
                 volatility_window: int = 20,
                 momentum_window: int = 10,
                 adx_threshold: float = 25.0,
                 volatility_threshold: float = 0.02,
                 regime_change_threshold: float = 0.7):
        """
        Initialize the regime detector with configurable parameters.
        
        Args:
            adx_window: Period for ADX calculation
            volatility_window: Period for volatility calculation
            momentum_window: Period for momentum calculation
            adx_threshold: Threshold for trend detection
            volatility_threshold: Threshold for volatility regime
            regime_change_threshold: Confidence threshold for regime changes
        """
        self.adx_window = adx_window
        self.volatility_window = volatility_window
        self.momentum_window = momentum_window
        self.adx_threshold = adx_threshold
        self.volatility_threshold = volatility_threshold
        self.regime_change_threshold = regime_change_threshold
        
        self.current_regime = MarketRegime.UNKNOWN
        self.regime_start_time = pd.Timestamp.now()
        
    def calculate_metrics(self, data: pd.DataFrame) -> RegimeMetrics:
        """
        Calculate all metrics needed for regime detection.
        
        Args:
            data: DataFrame with OHLCV data
            
        Returns:
            RegimeMetrics object containing all calculated metrics
        """
        # Calculate ADX
        high = data['high']
        low = data['low']
        close = data['close']
        
        # True Range
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.DataFrame({'tr1': tr1, 'tr2': tr2, 'tr3': tr3}).max(axis=1)
        
        # Directional Movement
        plus_dm = high - high.shift(1)
        minus_dm = low.shift(1) - low
        plus_dm = plus_dm.where(plus_dm > 0, 0)
        minus_dm = minus_dm.where(minus_dm > 0, 0)
        
        # Smoothed TR and DM
        tr_smooth = tr.rolling(window=self.adx_window).mean()
        plus_dm_smooth = plus_dm.rolling(window=self.adx_window).mean()
        minus_dm_smooth = minus_dm.rolling(window=self.adx_window).mean()
        
        # Directional Indicators
        pdi = 100 * plus_dm_smooth / tr_smooth
        mdi = 100 * minus_dm_smooth / tr_smooth
        adx = 100 * abs(pdi - mdi) / (pdi + mdi)
        
        # Volatility (normalized Bollinger Band width)
        std = close.rolling(window=self.volatility_window).std()
        mean = close.rolling(window=self.volatility_window).mean()
        volatility = std / mean
        
        # Trend Strength (based on linear regression)
        x = np.arange(len(close[-self.volatility_window:]))
        y = close[-self.volatility_window:].values
        trend_strength = np.abs(np.corrcoef(x, y)[0, 1])
        
        # Range Strength (based on mean reversion)
        zscore = (close - mean) / std
        range_strength = 1 - np.abs(zscore.iloc[-1])
        
        # Momentum
        momentum = (close - close.shift(self.momentum_window)) / close.shift(self.momentum_window)
        momentum = momentum.iloc[-1]
        
        metrics = RegimeMetrics(
            adx=adx.iloc[-1],
            volatility=volatility.iloc[-1],
            trend_strength=trend_strength,
            range_strength=range_strength,
            momentum=momentum
        )
        
        # Update Prometheus metrics
        regime_metrics.labels(metric='adx').set(metrics.adx)
        regime_metrics.labels(metric='volatility').set(metrics.volatility)
        regime_metrics.labels(metric='trend_strength').set(metrics.trend_strength)
        regime_metrics.labels(metric='range_strength').set(metrics.range_strength)
        regime_metrics.labels(metric='momentum').set(metrics.momentum)
        
        return metrics
    
    def detect_regime(self, data: pd.DataFrame) -> Tuple[MarketRegime, float]:
        """
        Detect the current market regime using multiple indicators.
        
        Args:
            data: DataFrame with OHLCV data
            
        Returns:
            Tuple of (MarketRegime, confidence)
        """
        metrics = self.calculate_metrics(data)
        
        # Initialize regime probabilities
        regime_probs = {
            MarketRegime.TRENDING_UP: 0.0,
            MarketRegime.TRENDING_DOWN: 0.0,
            MarketRegime.RANGING: 0.0,
            MarketRegime.VOLATILE: 0.0
        }
        
        # Trend detection
        if metrics.adx > self.adx_threshold:
            if metrics.momentum > 0:
                regime_probs[MarketRegime.TRENDING_UP] = min(1.0, metrics.trend_strength * 1.5)
            else:
                regime_probs[MarketRegime.TRENDING_DOWN] = min(1.0, metrics.trend_strength * 1.5)
                
        # Range detection
        if metrics.volatility < self.volatility_threshold:
            regime_probs[MarketRegime.RANGING] = min(1.0, metrics.range_strength * 1.2)
            
        # Volatility detection
        if metrics.volatility > self.volatility_threshold * 2:
            regime_probs[MarketRegime.VOLATILE] = min(1.0, metrics.volatility / (self.volatility_threshold * 3))
            
        # Select regime with highest probability
        new_regime = max(regime_probs.items(), key=lambda x: x[1])
        confidence = new_regime[1]
        
        # Only change regime if confidence exceeds threshold
        if confidence > self.regime_change_threshold and new_regime[0] != self.current_regime:
            regime_changes.labels(
                from_regime=self.current_regime.value,
                to_regime=new_regime[0].value
            ).inc()
            self.current_regime = new_regime[0]
            self.regime_start_time = pd.Timestamp.now()
            
        # Update monitoring metrics
        duration = (pd.Timestamp.now() - self.regime_start_time).total_seconds()
        regime_duration.labels(regime=self.current_regime.value).set(duration)
        regime_confidence.labels(regime=self.current_regime.value).set(confidence)
        
        return self.current_regime, confidence
    
    def get_regime_parameters(self, regime: MarketRegime) -> Dict:
        """
        Get optimal strategy parameters for the current regime.
        
        Args:
            regime: Current market regime
            
        Returns:
            Dictionary of strategy parameters
        """
        params = {
            MarketRegime.TRENDING_UP: {
                'stop_loss': 0.02,
                'take_profit': 0.04,
                'position_size': 1.0,
                'use_trailing_stop': True
            },
            MarketRegime.TRENDING_DOWN: {
                'stop_loss': 0.02,
                'take_profit': 0.04,
                'position_size': 0.8,
                'use_trailing_stop': True
            },
            MarketRegime.RANGING: {
                'stop_loss': 0.01,
                'take_profit': 0.02,
                'position_size': 0.5,
                'use_trailing_stop': False
            },
            MarketRegime.VOLATILE: {
                'stop_loss': 0.03,
                'take_profit': 0.05,
                'position_size': 0.3,
                'use_trailing_stop': True
            }
        }
        
        return params.get(regime, params[MarketRegime.VOLATILE]) 