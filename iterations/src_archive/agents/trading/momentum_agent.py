"""
Momentum Trading Agent
---------------------
This module implements a momentum-based trading agent.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
import numpy as np
import pandas as pd
from src.config import TradingConfig
from src.agents.trading.base_agent import BaseTradingAgent
import talib as ta

# Load configuration
config = TradingConfig.load_from_file()

class MomentumAgent(BaseTradingAgent):
    """Momentum-based trading agent."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the momentum agent.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        super().__init__(config)
        self.logger = logging.getLogger("MomentumAgent")
        self.positions = {}
        self.trades = []
        self.timeframes = config.get('timeframes', ['1h', '4h', '1d'])
        self.momentum_weights = config.get('momentum_weights', {'1h': 0.3, '4h': 0.4, '1d': 0.3})
        self.regime_weights = config.get('regime_weights', {
            'trending': 1.0,
            'high_volatility': 0.5,
            'high_volume': 0.8,
            'neutral': 0.6
        })
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate momentum agent configuration."""
        default_config = {
            'momentum': {
                'lookback_period': 20,
                'signal_threshold': 0.5,
                'exit_threshold': 0.2,
                'smoothing_period': 5
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

    def calculate_signal(self, data: pd.DataFrame, market_regime=None) -> float:
        """Calculate trading signal using multiple timeframe momentum analysis."""
        # Get market regime if not provided
        regime = market_regime.value if market_regime else self.detect_market_regime(data)
        regime_weight = self.regime_weights.get(regime, 0.5)
        
        # Calculate momentum signals for each timeframe
        signals = {}
        for tf in self.timeframes:
            signals[tf] = self._calculate_timeframe_signal(data, tf)
        
        # Calculate weighted signal
        weighted_signal = sum(signals[tf] * self.momentum_weights[tf] for tf in self.timeframes)
        
        # Apply regime-specific adjustments
        if regime == 'high_volatility':
            # Reduce signal strength in high volatility
            weighted_signal *= 0.7
        elif regime == 'trending':
            # Amplify signal in trending markets
            weighted_signal *= 1.2
        elif regime == 'high_volume':
            # Increase confidence in high volume
            weighted_signal *= 1.1
        else:
            # Neutral regime - no adjustment
            pass
            
        # Apply regime weight
        final_signal = weighted_signal * regime_weight
        
        # Calculate risk factors
        risk_factors = self.calculate_risk_factors(data)
        risk_adjustment = 1 - np.mean(list(risk_factors.values()))
        
        # Apply risk adjustment
        final_signal *= risk_adjustment
        
        return float(np.clip(final_signal, -1, 1))
    
    def _calculate_timeframe_signal(self, data: pd.DataFrame, timeframe: str) -> float:
        """Calculate momentum signal for a specific timeframe."""
        # If high/low not available, use close price with small variation
        if 'high' not in data.columns or 'low' not in data.columns:
            data = data.copy()
            data['high'] = data['close'] * 1.002  # 0.2% above close
            data['low'] = data['close'] * 0.998   # 0.2% below close
        
        # Calculate ADX for trend strength
        adx = ta.ADX(data['high'], data['low'], data['close'], timeperiod=14)
        
        # Calculate RSI for momentum
        rsi = ta.RSI(data['close'], timeperiod=14)
        
        # Calculate MACD for trend direction
        macd, macd_signal, _ = ta.MACD(data['close'])
        
        # Normalize indicators
        adx_norm = (adx - 20) / 30  # ADX range typically 20-50
        rsi_norm = (rsi - 50) / 50  # RSI range 0-100
        macd_norm = (macd - macd_signal) / data['close'].std()
        
        # Combine signals
        signal = (adx_norm * 0.3 + rsi_norm * 0.4 + macd_norm * 0.3).iloc[-1]
        
        return np.clip(signal, -1, 1)
    
    def _calculate_trend_strength(self, data: pd.DataFrame) -> float:
        """Calculate trend strength using moving averages and ADX."""
        # Calculate moving averages
        ma_short = data['close'].rolling(window=20).mean()
        ma_long = data['close'].rolling(window=50).mean()
        
        # Calculate ADX
        adx = ta.ADX(data['high'], data['low'], data['close'], timeperiod=14)
        
        # Calculate trend strength based on moving averages
        ma_trend = (ma_short.iloc[-1] / ma_long.iloc[-1]) - 1
        
        # Normalize ADX (typically ranges from 0 to 100)
        adx_norm = adx.iloc[-1] / 100 if not pd.isna(adx.iloc[-1]) else 0.5
        
        # Combine signals and normalize to [0, 1]
        trend_strength = (0.7 * adx_norm + 0.3 * abs(ma_trend))
        return float(np.clip(trend_strength, 0, 1))
    
    def _calculate_momentum_score(self, data: pd.DataFrame) -> float:
        """Calculate momentum score using multiple indicators."""
        # Rate of Change
        roc = data['close'].pct_change(periods=20)
        
        # Stochastic RSI
        stoch_rsi = ta.STOCHRSI(data['close'], timeperiod=14, fastk_period=5, fastd_period=3)
        
        # Commodity Channel Index
        cci = ta.CCI(data['high'], data['low'], data['close'], timeperiod=14)
        
        # Combine indicators
        momentum_score = (
            roc.iloc[-1] * 0.4 +
            (stoch_rsi[0].iloc[-1] - 50) / 50 * 0.3 +
            cci.iloc[-1] / 100 * 0.3
        )
        
        return float(momentum_score)
    
    def get_trading_insights(self, data: pd.DataFrame) -> Dict[str, Dict[str, float]]:
        """Get comprehensive trading insights."""
        insights = {
            'position_analysis': {},
            'risk_factors': {},
            'performance_metrics': {}
        }
        
        # Position analysis
        signal = self.calculate_signal(data)
        position_size = self.calculate_position_size(signal, data)
        insights['position_analysis'] = {
            'signal': signal,
            'position_size': position_size,
            'current_position': self.position
        }
        
        # Risk factors
        risk_factors = self.calculate_risk_factors(data)
        insights['risk_factors'] = risk_factors
        
        # Performance metrics
        metrics = self.get_performance_metrics()
        insights['performance_metrics'] = metrics
        
        return insights
    
    def _calculate_position_risk(self, data: pd.DataFrame) -> float:
        """Calculate position risk based on volatility and market conditions."""
        # Calculate volatility
        returns = data['close'].pct_change()
        volatility = returns.rolling(window=20).std() * np.sqrt(252)
        
        # Get market regime
        regime = self.detect_market_regime(data)
        
        # Calculate position risk based on regime
        if regime == 'high_volatility':
            risk_multiplier = 1.5
        elif regime == 'trending':
            risk_multiplier = 0.8
        elif regime == 'high_volume':
            risk_multiplier = 0.9
        else:
            risk_multiplier = 1.0
            
        # Calculate final risk score
        position_risk = float(volatility.iloc[-1] * risk_multiplier)
        return np.clip(position_risk, 0, 1)
    
    def _calculate_correlation(self, data: pd.DataFrame) -> float:
        """Calculate correlation with market returns."""
        if 'market_returns' not in data.columns:
            return 0.0
            
        returns = data['close'].pct_change()
        market_returns = data['market_returns']
        
        correlation = returns.rolling(60).corr(market_returns)
        return float(correlation.iloc[-1]) if not pd.isna(correlation.iloc[-1]) else 0.0
    
    def _calculate_volatility_risk(self, data: pd.DataFrame) -> float:
        """Calculate volatility risk using exponential weighted standard deviation."""
        returns = data['close'].pct_change()
        volatility = returns.ewm(span=20).std() * np.sqrt(252)
        return float(volatility.iloc[-1])
    
    def _calculate_liquidity_risk(self, data: pd.DataFrame) -> float:
        """Calculate liquidity risk based on volume and price impact."""
        avg_volume = data['volume'].rolling(20).mean()
        volume_ratio = data['volume'] / avg_volume
        return float(1 / volume_ratio.iloc[-1]) if volume_ratio.iloc[-1] != 0 else 1.0
    
    def _calculate_slippage_risk(self, data: pd.DataFrame) -> float:
        """Calculate slippage risk based on price volatility and volume."""
        volatility = data['close'].pct_change().std()
        volume_factor = 1 / (data['volume'].iloc[-1] / data['volume'].rolling(20).mean().iloc[-1])
        return float(volatility * volume_factor)
    
    def _calculate_volume_regime(self, data: pd.DataFrame) -> float:
        """Calculate volume regime indicator."""
        # Calculate volume moving averages
        volume_ma_short = data['volume'].rolling(window=10).mean()
        volume_ma_long = data['volume'].rolling(window=30).mean()
        
        # Calculate volume ratio
        volume_ratio = volume_ma_short.iloc[-1] / volume_ma_long.iloc[-1]
        
        # Calculate volume trend
        volume_trend = data['volume'].pct_change().rolling(window=10).mean().iloc[-1]
        
        # Combine indicators and normalize to [0, 1]
        volume_score = (0.6 * (volume_ratio - 1) + 0.4 * volume_trend)
        return float(np.clip((volume_score + 1) / 2, 0, 1))
    
    def _calculate_acceleration_factor(self, data: pd.DataFrame) -> float:
        """Calculate price acceleration factor."""
        returns = data['close'].pct_change()
        momentum = returns.rolling(window=10).mean()
        acceleration = momentum.diff()
        return float(acceleration.iloc[-1])
    
    def _calculate_volatility_skew(self, data: pd.DataFrame) -> float:
        """Calculate volatility skew."""
        returns = data['close'].pct_change()
        skew = returns.rolling(window=20).skew()
        return float(skew.iloc[-1]) if not pd.isna(skew.iloc[-1]) else 0.0
    
    def _calculate_volume_trend(self, data: pd.DataFrame) -> float:
        """Calculate volume trend indicator."""
        volume_ma = data['volume'].rolling(window=20).mean()
        volume_trend = data['volume'] / volume_ma - 1
        return float(volume_trend.iloc[-1])
    
    def _calculate_volume_volatility(self, data: pd.DataFrame) -> float:
        """Calculate volume volatility."""
        volume_returns = data['volume'].pct_change()
        volatility = volume_returns.rolling(window=20).std()
        return float(volatility.iloc[-1])
    
    def _calculate_price_momentum(self, data: pd.DataFrame) -> float:
        """Calculate price momentum using multiple timeframes."""
        returns = data['close'].pct_change()
        momentum_10 = returns.rolling(window=10).mean()
        momentum_20 = returns.rolling(window=20).mean()
        momentum_50 = returns.rolling(window=50).mean()
        
        # Combine momentum signals
        momentum = (momentum_10.iloc[-1] * 0.5 + 
                   momentum_20.iloc[-1] * 0.3 + 
                   momentum_50.iloc[-1] * 0.2)
        return float(momentum)
    
    def _calculate_price_reversal(self, data: pd.DataFrame) -> float:
        """Calculate price reversal signal."""
        # Calculate RSI
        rsi = ta.RSI(data['close'], timeperiod=14)
        
        # Calculate Bollinger Bands
        upper, middle, lower = ta.BBANDS(data['close'], timeperiod=20)
        
        # Calculate reversal signal
        rsi_signal = 1.0 if rsi.iloc[-1] < 30 else (-1.0 if rsi.iloc[-1] > 70 else 0.0)
        bb_signal = 1.0 if data['close'].iloc[-1] < lower.iloc[-1] else (-1.0 if data['close'].iloc[-1] > upper.iloc[-1] else 0.0)
        
        # Combine signals
        reversal_signal = (rsi_signal + bb_signal) / 2
        return float(reversal_signal) 