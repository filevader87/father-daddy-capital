# src/utils/market_regime.py

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Union, Any
from enum import Enum
from dataclasses import dataclass
from scipy.stats import norm
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from datetime import datetime

class MarketRegime(Enum):
    """Market regime classifications."""
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    CONSOLIDATION = "consolidation"
    NEUTRAL = "neutral"

@dataclass
class RegimeMetrics:
    """Metrics for market regime analysis."""
    trend_strength: float
    volatility: float
    momentum: float
    volume_profile: float
    mean_reversion_score: float
    breakout_score: float
    regime_confidence: float
    regime: MarketRegime

class MarketRegimeDetector:
    """Advanced market regime detection system."""
    
    def __init__(self, 
                 lookback_period: int = 20,
                 volatility_threshold: float = 0.2,
                 trend_threshold: float = 0.6,
                 volume_threshold: float = 1.5):
        self.lookback_period = lookback_period
        self.volatility_threshold = volatility_threshold
        self.trend_threshold = trend_threshold
        self.volume_threshold = volume_threshold
        self.scaler = StandardScaler()
        self.anomaly_detector = IsolationForest(contamination=0.1)
        
        # Regime persistence tracking
        self.regime_history = []
        self.regime_duration = {}
        self.last_regime = None
        self.regime_change_count = 0
        
        # Multi-timeframe analysis
        self.timeframes = ['1h', '4h', '1d']
        self.timeframe_weights = {'1h': 0.3, '4h': 0.4, '1d': 0.3}
        
        # Adaptive threshold tracking
        self.volatility_history = []
        self.trend_history = []
        self.volume_history = []
        self.adaptive_window = 100
        
    def _calculate_trend_metrics(self, prices: np.ndarray) -> Tuple[float, float]:
        """Calculate trend strength and direction."""
        # Linear regression for trend
        x = np.arange(len(prices))
        slope, _ = np.polyfit(x, prices, 1)
        
        # ADX for trend strength
        high = prices + np.abs(np.random.normal(0, 0.1, len(prices)))
        low = prices - np.abs(np.random.normal(0, 0.1, len(prices)))
        close = prices
        
        tr = np.maximum(high - low, np.maximum(np.abs(high - close), np.abs(low - close)))
        atr = np.mean(tr[-self.lookback_period:])
        
        plus_dm = np.where(high[1:] > high[:-1], high[1:] - high[:-1], 0)
        minus_dm = np.where(low[:-1] > low[1:], low[:-1] - low[1:], 0)
        
        plus_di = 100 * np.mean(plus_dm[-self.lookback_period:]) / atr
        minus_di = 100 * np.mean(minus_dm[-self.lookback_period:]) / atr
        
        adx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
        
        return slope, adx
        
    def _calculate_volatility_metrics(self, prices: np.ndarray) -> float:
        """Calculate volatility metrics."""
        returns = np.diff(prices) / prices[:-1]
        volatility = np.std(returns[-self.lookback_period:])
        return volatility
        
    def _calculate_momentum_metrics(self, prices: np.ndarray) -> float:
        """Calculate momentum metrics."""
        # RSI
        delta = np.diff(prices)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        
        avg_gain = np.mean(gain[-self.lookback_period:])
        avg_loss = np.mean(loss[-self.lookback_period:])
        
        rs = avg_gain / avg_loss if avg_loss != 0 else 0
        rsi = 100 - (100 / (1 + rs))
        
        # MACD
        ema12 = pd.Series(prices).ewm(span=12).mean()
        ema26 = pd.Series(prices).ewm(span=26).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9).mean()
        
        momentum_score = (rsi - 50) / 50 + (macd.iloc[-1] - signal.iloc[-1]) / signal.iloc[-1]
        return momentum_score
        
    def _calculate_volume_profile(self, volumes: np.ndarray) -> float:
        """Calculate volume profile metrics."""
        avg_volume = np.mean(volumes[-self.lookback_period:])
        current_volume = volumes[-1]
        volume_ratio = current_volume / avg_volume
        return volume_ratio
        
    def _calculate_mean_reversion_score(self, prices: np.ndarray) -> float:
        """Calculate mean reversion score."""
        # Bollinger Bands
        sma = np.mean(prices[-self.lookback_period:])
        std = np.std(prices[-self.lookback_period:])
        upper_band = sma + 2 * std
        lower_band = sma - 2 * std
        
        current_price = prices[-1]
        z_score = (current_price - sma) / std
        
        # Mean reversion probability
        prob = norm.cdf(-abs(z_score)) * 2
        return prob
        
    def _calculate_breakout_score(self, prices: np.ndarray) -> float:
        """Calculate breakout score."""
        # ATR for volatility
        high = prices + np.abs(np.random.normal(0, 0.1, len(prices)))
        low = prices - np.abs(np.random.normal(0, 0.1, len(prices)))
        close = prices
        
        tr = np.maximum(high - low, np.maximum(np.abs(high - close), np.abs(low - close)))
        atr = np.mean(tr[-self.lookback_period:])
        
        # Recent high/low
        recent_high = np.max(prices[-self.lookback_period:])
        recent_low = np.min(prices[-self.lookback_period:])
        
        current_price = prices[-1]
        breakout_score = 0
        
        if current_price > recent_high + atr:
            breakout_score = (current_price - recent_high) / atr
        elif current_price < recent_low - atr:
            breakout_score = -(recent_low - current_price) / atr
            
        return breakout_score
        
    def _detect_anomalies(self, features: np.ndarray) -> float:
        """Detect market anomalies using isolation forest."""
        scaled_features = self.scaler.fit_transform(features.reshape(-1, 1))
        anomaly_score = self.anomaly_detector.fit_predict(scaled_features)
        return np.mean(anomaly_score)
        
    def _update_adaptive_thresholds(self, 
                                  volatility: float,
                                  trend_strength: float,
                                  volume_ratio: float):
        """Update adaptive thresholds based on historical data."""
        self.volatility_history.append(volatility)
        self.trend_history.append(trend_strength)
        self.volume_history.append(volume_ratio)
        
        if len(self.volatility_history) > self.adaptive_window:
            self.volatility_history.pop(0)
            self.trend_history.pop(0)
            self.volume_history.pop(0)
            
        if len(self.volatility_history) >= 20:  # Minimum samples for adaptation
            self.volatility_threshold = np.percentile(self.volatility_history, 75)
            self.trend_threshold = np.percentile(self.trend_history, 75)
            self.volume_threshold = np.percentile(self.volume_history, 75)
            
    def _update_regime_persistence(self, regime: MarketRegime):
        """Update regime persistence tracking."""
        current_time = datetime.now()
        
        if regime != self.last_regime:
            self.regime_change_count += 1
            if self.last_regime is not None:
                duration = (current_time - self.regime_history[-1]['timestamp']).total_seconds()
                self.regime_duration[self.last_regime] = self.regime_duration.get(self.last_regime, 0) + duration
                
        self.regime_history.append({
            'regime': regime,
            'timestamp': current_time
        })
        
        if len(self.regime_history) > 1000:  # Keep last 1000 regime changes
            self.regime_history.pop(0)
            
        self.last_regime = regime
        
    def _get_regime_persistence_score(self, regime: MarketRegime) -> float:
        """Calculate regime persistence score."""
        if not self.regime_duration:
            return 0.5
            
        total_duration = sum(self.regime_duration.values())
        regime_duration = self.regime_duration.get(regime, 0)
        
        if total_duration == 0:
            return 0.5
            
        persistence_score = regime_duration / total_duration
        return persistence_score
        
    def _analyze_multi_timeframe(self, 
                               prices: Dict[str, np.ndarray],
                               volumes: Optional[Dict[str, np.ndarray]] = None) -> Dict[str, RegimeMetrics]:
        """Analyze market regime across multiple timeframes."""
        timeframe_metrics = {}
        
        for timeframe in self.timeframes:
            if timeframe in prices:
                timeframe_prices = prices[timeframe]
                timeframe_volumes = volumes.get(timeframe) if volumes else None
                
                metrics = self.detect_regime(timeframe_prices, timeframe_volumes)
                timeframe_metrics[timeframe] = metrics
                
        return timeframe_metrics
        
    def _combine_timeframe_signals(self, timeframe_metrics: Dict[str, RegimeMetrics]) -> RegimeMetrics:
        """Combine signals from multiple timeframes."""
        combined_metrics = RegimeMetrics(
            trend_strength=0.0,
            volatility=0.0,
            momentum=0.0,
            volume_profile=0.0,
            mean_reversion_score=0.0,
            breakout_score=0.0,
            regime_confidence=0.0,
            regime=MarketRegime.NEUTRAL
        )
        
        # Weighted average of metrics
        for timeframe, metrics in timeframe_metrics.items():
            weight = self.timeframe_weights[timeframe]
            combined_metrics.trend_strength += metrics.trend_strength * weight
            combined_metrics.volatility += metrics.volatility * weight
            combined_metrics.momentum += metrics.momentum * weight
            combined_metrics.volume_profile += metrics.volume_profile * weight
            combined_metrics.mean_reversion_score += metrics.mean_reversion_score * weight
            combined_metrics.breakout_score += metrics.breakout_score * weight
            combined_metrics.regime_confidence += metrics.regime_confidence * weight
            
        # Determine dominant regime
        regime_scores = {}
        for timeframe, metrics in timeframe_metrics.items():
            weight = self.timeframe_weights[timeframe]
            regime = metrics.regime
            regime_scores[regime] = regime_scores.get(regime, 0) + weight
            
        combined_metrics.regime = max(regime_scores.items(), key=lambda x: x[1])[0]
        
        return combined_metrics
        
    def detect_regime(self, 
                     prices: Union[np.ndarray, Dict[str, np.ndarray]],
                     volumes: Optional[Union[np.ndarray, Dict[str, np.ndarray]]] = None) -> RegimeMetrics:
        """Enhanced regime detection with multi-timeframe analysis."""
        # Handle single timeframe input
        if isinstance(prices, np.ndarray):
            prices = {'1h': prices}
            if volumes is not None:
                volumes = {'1h': volumes}
                
        # Multi-timeframe analysis
        timeframe_metrics = self._analyze_multi_timeframe(prices, volumes)
        combined_metrics = self._combine_timeframe_signals(timeframe_metrics)
        
        # Update adaptive thresholds
        self._update_adaptive_thresholds(
            combined_metrics.volatility,
            combined_metrics.trend_strength,
            combined_metrics.volume_profile
        )
        
        # Update regime persistence
        self._update_regime_persistence(combined_metrics.regime)
        
        # Adjust confidence based on regime persistence
        persistence_score = self._get_regime_persistence_score(combined_metrics.regime)
        combined_metrics.regime_confidence *= (0.7 + 0.3 * persistence_score)
        
        return combined_metrics
        
    def get_regime_transition_probability(self, 
                                        from_regime: MarketRegime,
                                        to_regime: MarketRegime) -> float:
        """Calculate probability of transitioning between regimes."""
        if not self.regime_history:
            return 0.0
            
        transitions = []
        for i in range(1, len(self.regime_history)):
            if (self.regime_history[i-1]['regime'] == from_regime and 
                self.regime_history[i]['regime'] == to_regime):
                transitions.append(self.regime_history[i]['timestamp'] - 
                                self.regime_history[i-1]['timestamp'])
                
        if not transitions:
            return 0.0
            
        avg_transition_time = np.mean([t.total_seconds() for t in transitions])
        total_time = (self.regime_history[-1]['timestamp'] - 
                     self.regime_history[0]['timestamp']).total_seconds()
        
        return len(transitions) / (total_time / avg_transition_time)
        
    def get_regime_statistics(self) -> Dict[str, Any]:
        """Get comprehensive regime statistics."""
        if not self.regime_history:
            return {}
            
        stats = {
            'current_regime': self.last_regime,
            'regime_duration': self.regime_duration,
            'regime_changes': self.regime_change_count,
            'avg_regime_duration': np.mean(list(self.regime_duration.values())) 
                if self.regime_duration else 0,
            'volatility_threshold': self.volatility_threshold,
            'trend_threshold': self.trend_threshold,
            'volume_threshold': self.volume_threshold
        }
        
        # Calculate transition probabilities
        transition_matrix = {}
        for from_regime in MarketRegime:
            transition_matrix[from_regime] = {}
            for to_regime in MarketRegime:
                if from_regime != to_regime:
                    prob = self.get_regime_transition_probability(from_regime, to_regime)
                    transition_matrix[from_regime][to_regime] = prob
                    
        stats['transition_probabilities'] = transition_matrix
        
        return stats

# ✅ Compatibility function used in QLearningAgent, etc.
def detect_market_regime(price_series):
    detector = MarketRegimeDetector()
    return detector.detect(price_series)
