"""
Advanced Adaptive Trading Agent
------------------------------
Implements adaptive strategy selection, regime detection, learning, and asset specialization
"""

import asyncio
import logging
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
import torch
import torch.nn as nn
from collections import deque
import json
from pathlib import Path

from src.config.unified_config import get_agent_config, get_trading_config
from src.utils.indicators import compute_rsi, compute_macd, compute_obv, compute_volatility
from src.utils.market_regime import detect_market_regime
from src.utils.technical_analyzer import TechnicalAnalyzer
from src.utils.risk_metrics import calculate_var, calculate_sharpe_ratio
from src.risk.risk_manager import RiskManager
from src.utils.performance_logger import PerformanceLogger

logger = logging.getLogger(__name__)

class MarketRegime(Enum):
    """Advanced market regime classification."""
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    BREAKOUT = "breakout"
    REVERSAL = "reversal"
    CRASH = "crash"
    RALLY = "rally"

class StrategyType(Enum):
    """Adaptive strategy types."""
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    SCALPING = "scalping"
    MARKET_MAKING = "market_making"
    ARBITRAGE = "arbitrage"
    TREND_FOLLOWING = "trend_following"
    CONTRARIAN = "contrarian"

class AssetClass(Enum):
    """Asset classes with specialized characteristics."""
    CRYPTO = "crypto"
    STOCKS = "stocks"
    FOREX = "forex"

@dataclass
class MarketContext:
    """Comprehensive market context."""
    regime: MarketRegime
    volatility: float
    volume: float
    liquidity: float
    trend_strength: float
    momentum: float
    time_of_day: str
    day_of_week: str
    asset_class: AssetClass
    market_cap: Optional[float] = None
    sector: Optional[str] = None

@dataclass
class StrategyPerformance:
    """Strategy performance tracking."""
    strategy: StrategyType
    asset_class: AssetClass
    regime: MarketRegime
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    avg_trade_duration: float = 0.0
    last_updated: datetime = field(default_factory=datetime.now)

@dataclass
class AdaptiveParameters:
    """Adaptive parameters that learn over time."""
    confidence_threshold: float = 0.7
    position_size_multiplier: float = 1.0
    stop_loss_multiplier: float = 1.0
    take_profit_multiplier: float = 1.0
    signal_cooldown: int = 300
    regime_weights: Dict[str, float] = field(default_factory=dict)
    strategy_weights: Dict[str, float] = field(default_factory=dict)

class RegimeDetector:
    """Advanced market regime detection system."""
    
    def __init__(self):
        self.logger = logging.getLogger("RegimeDetector")
        self.regime_history = deque(maxlen=100)
        self.regime_confidence = {}
        
    def detect_regime(self, data: pd.DataFrame, asset_class: AssetClass) -> Tuple[MarketRegime, float]:
        """Detect current market regime with confidence score."""
        if len(data) < 50:
            return MarketRegime.RANGING, 0.5
        
        # Calculate regime indicators
        returns = data['close'].pct_change().dropna()
        volatility = returns.std() * np.sqrt(252)  # Annualized
        
        # Trend analysis
        sma_20 = data['close'].rolling(20).mean()
        sma_50 = data['close'].rolling(50).mean()
        trend_strength = abs(sma_20.iloc[-1] - sma_50.iloc[-1]) / sma_50.iloc[-1]
        
        # Momentum analysis
        rsi = compute_rsi(data['close'].values, 14)
        current_rsi = rsi[-1] if not np.isnan(rsi[-1]) else 50
        
        # Volume analysis
        volume_ma = data['volume'].rolling(20).mean()
        volume_ratio = data['volume'].iloc[-1] / volume_ma.iloc[-1]
        
        # Price action analysis
        high_20 = data['high'].rolling(20).max()
        low_20 = data['low'].rolling(20).min()
        price_position = (data['close'].iloc[-1] - low_20.iloc[-1]) / (high_20.iloc[-1] - low_20.iloc[-1])
        
        # Regime classification logic
        regime, confidence = self._classify_regime(
            volatility, trend_strength, current_rsi, volume_ratio, 
            price_position, asset_class
        )
        
        # Update regime history
        self.regime_history.append((regime, confidence, datetime.now()))
        self.regime_confidence[regime] = confidence
        
        return regime, confidence
    
    def _classify_regime(self, volatility: float, trend_strength: float, 
                        rsi: float, volume_ratio: float, price_position: float,
                        asset_class: AssetClass) -> Tuple[MarketRegime, float]:
        """Classify market regime based on multiple indicators."""
        
        # Asset-specific thresholds
        if asset_class == AssetClass.CRYPTO:
            vol_threshold = 0.05
            trend_threshold = 0.1
            volume_threshold = 1.5
        else:  # Stocks/Forex
            vol_threshold = 0.03
            trend_threshold = 0.05
            volume_threshold = 1.2
        
        # High volatility regimes
        if volatility > vol_threshold * 2:
            if volume_ratio > volume_threshold * 2:
                return MarketRegime.CRASH, 0.9
            else:
                return MarketRegime.HIGH_VOLATILITY, 0.8
        
        # Trending regimes
        if trend_strength > trend_threshold:
            if rsi > 60 and price_position > 0.7:
                return MarketRegime.TRENDING_UP, 0.8
            elif rsi < 40 and price_position < 0.3:
                return MarketRegime.TRENDING_DOWN, 0.8
            elif rsi > 70:
                return MarketRegime.RALLY, 0.7
            else:
                return MarketRegime.TRENDING_UP if price_position > 0.5 else MarketRegime.TRENDING_DOWN, 0.6
        
        # Breakout detection
        if volume_ratio > volume_threshold and (price_position > 0.9 or price_position < 0.1):
            return MarketRegime.BREAKOUT, 0.8
        
        # Reversal detection
        if (rsi > 70 and price_position < 0.3) or (rsi < 30 and price_position > 0.7):
            return MarketRegime.REVERSAL, 0.7
        
        # Low volatility
        if volatility < vol_threshold * 0.5:
            return MarketRegime.LOW_VOLATILITY, 0.6
        
        # Default to ranging
        return MarketRegime.RANGING, 0.5
    
    def get_regime_stability(self) -> float:
        """Get regime stability score based on recent history."""
        if len(self.regime_history) < 10:
            return 0.5
        
        recent_regimes = [r[0] for r in list(self.regime_history)[-10:]]
        most_common = max(set(recent_regimes), key=recent_regimes.count)
        stability = recent_regimes.count(most_common) / len(recent_regimes)
        
        return stability

class AdaptiveLearningSystem:
    """Adaptive learning system that improves over time."""
    
    def __init__(self):
        self.logger = logging.getLogger("AdaptiveLearningSystem")
        self.performance_history = {}
        self.parameter_history = {}
        self.learning_rate = 0.01
        self.memory_size = 1000
        
    def update_performance(self, strategy: StrategyType, asset_class: AssetClass, 
                          regime: MarketRegime, trade_result: Dict[str, Any]):
        """Update performance metrics for learning."""
        key = f"{strategy.value}_{asset_class.value}_{regime.value}"
        
        if key not in self.performance_history:
            self.performance_history[key] = StrategyPerformance(
                strategy=strategy, asset_class=asset_class, regime=regime
            )
        
        perf = self.performance_history[key]
        perf.total_trades += 1
        perf.total_pnl += trade_result.get('pnl', 0)
        
        if trade_result.get('pnl', 0) > 0:
            perf.winning_trades += 1
        
        perf.win_rate = perf.winning_trades / perf.total_trades
        perf.last_updated = datetime.now()
        
        # Update Sharpe ratio and other metrics
        self._update_advanced_metrics(perf, trade_result)
    
    def _update_advanced_metrics(self, perf: StrategyPerformance, trade_result: Dict[str, Any]):
        """Update advanced performance metrics."""
        # This would implement more sophisticated metrics
        # For now, simplified implementation
        if perf.total_trades > 10:
            # Calculate rolling Sharpe ratio
            recent_trades = list(self.performance_history.values())[-10:]
            returns = [t.total_pnl for t in recent_trades]
            if len(returns) > 1 and np.std(returns) > 0:
                perf.sharpe_ratio = np.mean(returns) / np.std(returns)
    
    def get_optimal_strategy(self, asset_class: AssetClass, regime: MarketRegime) -> StrategyType:
        """Get optimal strategy based on historical performance."""
        best_strategy = StrategyType.MOMENTUM
        best_score = -float('inf')
        
        for strategy in StrategyType:
            key = f"{strategy.value}_{asset_class.value}_{regime.value}"
            if key in self.performance_history:
                perf = self.performance_history[key]
                if perf.total_trades >= 5:  # Minimum sample size
                    # Composite score combining multiple metrics
                    score = (
                        perf.win_rate * 0.3 +
                        perf.sharpe_ratio * 0.4 +
                        (perf.total_pnl / max(perf.total_trades, 1)) * 0.3
                    )
                    if score > best_score:
                        best_score = score
                        best_strategy = strategy
        
        return best_strategy
    
    def adapt_parameters(self, current_params: AdaptiveParameters, 
                        recent_performance: List[Dict[str, Any]]) -> AdaptiveParameters:
        """Adapt parameters based on recent performance."""
        new_params = AdaptiveParameters(
            confidence_threshold=current_params.confidence_threshold,
            position_size_multiplier=current_params.position_size_multiplier,
            stop_loss_multiplier=current_params.stop_loss_multiplier,
            take_profit_multiplier=current_params.take_profit_multiplier,
            signal_cooldown=current_params.signal_cooldown
        )
        
        # Adaptive parameter adjustment based on performance
        if recent_performance:
            avg_pnl = np.mean([p.get('pnl', 0) for p in recent_performance])
            win_rate = len([p for p in recent_performance if p.get('pnl', 0) > 0]) / len(recent_performance)
            
            # Adjust confidence threshold based on win rate
            if win_rate > 0.6:
                new_params.confidence_threshold = max(0.5, current_params.confidence_threshold - 0.05)
            elif win_rate < 0.4:
                new_params.confidence_threshold = min(0.9, current_params.confidence_threshold + 0.05)
            
            # Adjust position size based on performance
            if avg_pnl > 0:
                new_params.position_size_multiplier = min(2.0, current_params.position_size_multiplier * 1.05)
            else:
                new_params.position_size_multiplier = max(0.5, current_params.position_size_multiplier * 0.95)
        
        return new_params

class TimeHistoryLearning:
    """Time-based pattern learning system."""
    
    def __init__(self):
        self.logger = logging.getLogger("TimeHistoryLearning")
        self.time_patterns = {}
        self.seasonal_adjustments = {}
        
    def learn_time_patterns(self, symbol: str, data: pd.DataFrame, trades: List[Dict[str, Any]]):
        """Learn time-based trading patterns."""
        if len(trades) < 10:
            return
        
        # Analyze performance by time of day
        hourly_performance = {}
        for trade in trades:
            hour = trade['timestamp'].hour
            if hour not in hourly_performance:
                hourly_performance[hour] = []
            hourly_performance[hour].append(trade.get('pnl', 0))
        
        # Calculate average performance by hour
        for hour, pnls in hourly_performance.items():
            if len(pnls) >= 3:  # Minimum sample size
                avg_pnl = np.mean(pnls)
                self.time_patterns[f"{symbol}_hour_{hour}"] = avg_pnl
        
        # Analyze performance by day of week
        daily_performance = {}
        for trade in trades:
            day = trade['timestamp'].weekday()
            if day not in daily_performance:
                daily_performance[day] = []
            daily_performance[day].append(trade.get('pnl', 0))
        
        for day, pnls in daily_performance.items():
            if len(pnls) >= 3:
                avg_pnl = np.mean(pnls)
                self.time_patterns[f"{symbol}_day_{day}"] = avg_pnl
    
    def get_time_adjustment(self, symbol: str, timestamp: datetime) -> float:
        """Get time-based performance adjustment factor."""
        hour_key = f"{symbol}_hour_{timestamp.hour}"
        day_key = f"{symbol}_day_{timestamp.weekday()}"
        
        hour_adj = self.time_patterns.get(hour_key, 0.0)
        day_adj = self.time_patterns.get(day_key, 0.0)
        
        # Combine adjustments (normalize to 0.8-1.2 range)
        total_adj = 1.0 + (hour_adj + day_adj) * 0.1
        return max(0.8, min(1.2, total_adj))

class AssetSpecializedEngine:
    """Asset-specialized trading engines."""
    
    def __init__(self, asset_class: AssetClass):
        self.asset_class = asset_class
        self.logger = logging.getLogger(f"AssetEngine-{asset_class.value}")
        
        # Asset-specific parameters
        self._setup_asset_parameters()
    
    def _setup_asset_parameters(self):
        """Setup asset-specific trading parameters."""
        if self.asset_class == AssetClass.CRYPTO:
            self.parameters = {
                'min_volume': 1000000,
                'max_spread': 0.002,
                'volatility_threshold': 0.05,
                'liquidity_threshold': 50000,
                'trading_hours': '24/7',
                'leverage_available': True,
                'slippage_tolerance': 0.001,
                'min_trade_size': 0.001
            }
        elif self.asset_class == AssetClass.STOCKS:
            self.parameters = {
                'min_volume': 100000,
                'max_spread': 0.001,
                'volatility_threshold': 0.03,
                'liquidity_threshold': 10000,
                'trading_hours': '9:30-16:00',
                'leverage_available': False,
                'slippage_tolerance': 0.0005,
                'min_trade_size': 1
            }
        elif self.asset_class == AssetClass.FOREX:
            self.parameters = {
                'min_volume': 1000000,
                'max_spread': 0.0005,
                'volatility_threshold': 0.02,
                'liquidity_threshold': 100000,
                'trading_hours': '24/5',
                'leverage_available': True,
                'slippage_tolerance': 0.0002,
                'min_trade_size': 0.01
            }
    
    def analyze_with_specialization(self, data: pd.DataFrame, symbol: str, 
                                   context: MarketContext, strategy: StrategyType) -> Dict[str, Any]:
        """Analyze with asset-specific optimizations."""
        
        # Apply asset-specific filters
        if not self._validate_asset_conditions(data, symbol):
            return {'signal': 'HOLD', 'confidence': 0.0, 'reason': 'Asset conditions not met'}
        
        # Asset-specific strategy execution
        if strategy == StrategyType.MOMENTUM:
            return self._crypto_momentum(data, symbol, context) if self.asset_class == AssetClass.CRYPTO else self._stock_momentum(data, symbol, context)
        elif strategy == StrategyType.MEAN_REVERSION:
            return self._mean_reversion(data, symbol, context)
        elif strategy == StrategyType.SCALPING:
            return self._scalping(data, symbol, context)
        else:
            return {'signal': 'HOLD', 'confidence': 0.0, 'reason': 'Strategy not implemented'}
    
    def _validate_asset_conditions(self, data: pd.DataFrame, symbol: str) -> bool:
        """Validate asset-specific trading conditions."""
        if len(data) < 20:
            return False
        
        # Volume check
        avg_volume = data['volume'].rolling(20).mean().iloc[-1]
        if avg_volume < self.parameters['min_volume']:
            return False
        
        # Volatility check
        volatility = data['close'].pct_change().std() * np.sqrt(252)
        if volatility > self.parameters['volatility_threshold'] * 3:  # Too volatile
            return False
        
        return True
    
    def _crypto_momentum(self, data: pd.DataFrame, symbol: str, context: MarketContext) -> Dict[str, Any]:
        """Crypto-specific momentum strategy."""
        # More aggressive parameters for crypto
        rsi = compute_rsi(data['close'].values, 14)
        macd, macd_signal = compute_macd(data['close'].values)
        
        current_rsi = rsi[-1] if not np.isnan(rsi[-1]) else 50
        current_macd = macd[-1] if not np.isnan(macd[-1]) else 0
        current_macd_signal = macd_signal[-1] if not np.isnan(macd_signal[-1]) else 0
        
        # Crypto-specific momentum logic (more aggressive)
        bullish = current_rsi > 45 and current_rsi < 75 and current_macd > current_macd_signal
        bearish = current_rsi < 55 and current_rsi > 25 and current_macd < current_macd_signal
        
        if bullish:
            confidence = min(0.9, (current_rsi - 45) / 30 + 0.6)
            return {'signal': 'BUY', 'confidence': confidence, 'metadata': {'rsi': current_rsi, 'strategy': 'crypto_momentum'}}
        elif bearish:
            confidence = min(0.9, (55 - current_rsi) / 30 + 0.6)
            return {'signal': 'SELL', 'confidence': confidence, 'metadata': {'rsi': current_rsi, 'strategy': 'crypto_momentum'}}
        else:
            return {'signal': 'HOLD', 'confidence': 0.0, 'metadata': {'strategy': 'crypto_momentum'}}
    
    def _stock_momentum(self, data: pd.DataFrame, symbol: str, context: MarketContext) -> Dict[str, Any]:
        """Stock-specific momentum strategy."""
        # More conservative parameters for stocks
        rsi = compute_rsi(data['close'].values, 21)  # Longer period for stocks
        macd, macd_signal = compute_macd(data['close'].values)
        
        current_rsi = rsi[-1] if not np.isnan(rsi[-1]) else 50
        current_macd = macd[-1] if not np.isnan(macd[-1]) else 0
        current_macd_signal = macd_signal[-1] if not np.isnan(macd_signal[-1]) else 0
        
        # Stock-specific momentum logic (more conservative)
        bullish = current_rsi > 50 and current_rsi < 70 and current_macd > current_macd_signal
        bearish = current_rsi < 50 and current_rsi > 30 and current_macd < current_macd_signal
        
        if bullish:
            confidence = min(0.9, (current_rsi - 50) / 20 + 0.5)
            return {'signal': 'BUY', 'confidence': confidence, 'metadata': {'rsi': current_rsi, 'strategy': 'stock_momentum'}}
        elif bearish:
            confidence = min(0.9, (50 - current_rsi) / 20 + 0.5)
            return {'signal': 'SELL', 'confidence': confidence, 'metadata': {'rsi': current_rsi, 'strategy': 'stock_momentum'}}
        else:
            return {'signal': 'HOLD', 'confidence': 0.0, 'metadata': {'strategy': 'stock_momentum'}}
    
    def _mean_reversion(self, data: pd.DataFrame, symbol: str, context: MarketContext) -> Dict[str, Any]:
        """Asset-agnostic mean reversion strategy."""
        sma_20 = data['close'].rolling(20).mean()
        std_20 = data['close'].rolling(20).std()
        current_price = data['close'].iloc[-1]
        
        upper_band = sma_20.iloc[-1] + (2 * std_20.iloc[-1])
        lower_band = sma_20.iloc[-1] - (2 * std_20.iloc[-1])
        
        oversold = current_price < lower_band
        overbought = current_price > upper_band
        
        if oversold:
            confidence = min(0.9, (lower_band - current_price) / std_20.iloc[-1])
            return {'signal': 'BUY', 'confidence': confidence, 'metadata': {'strategy': 'mean_reversion'}}
        elif overbought:
            confidence = min(0.9, (current_price - upper_band) / std_20.iloc[-1])
            return {'signal': 'SELL', 'confidence': confidence, 'metadata': {'strategy': 'mean_reversion'}}
        else:
            return {'signal': 'HOLD', 'confidence': 0.0, 'metadata': {'strategy': 'mean_reversion'}}
    
    def _scalping(self, data: pd.DataFrame, symbol: str, context: MarketContext) -> Dict[str, Any]:
        """High-frequency scalping strategy."""
        if len(data) < 5:
            return {'signal': 'HOLD', 'confidence': 0.0, 'metadata': {'strategy': 'scalping'}}
        
        ema_5 = data['close'].ewm(span=5).mean()
        ema_10 = data['close'].ewm(span=10).mean()
        current_price = data['close'].iloc[-1]
        
        bullish = ema_5.iloc[-1] > ema_10.iloc[-1] and current_price > ema_5.iloc[-1]
        bearish = ema_5.iloc[-1] < ema_10.iloc[-1] and current_price < ema_5.iloc[-1]
        
        if bullish:
            return {'signal': 'BUY', 'confidence': 0.7, 'metadata': {'strategy': 'scalping'}}
        elif bearish:
            return {'signal': 'SELL', 'confidence': 0.7, 'metadata': {'strategy': 'scalping'}}
        else:
            return {'signal': 'HOLD', 'confidence': 0.0, 'metadata': {'strategy': 'scalping'}}

class AdaptiveTradingAgent:
    """Advanced adaptive trading agent with all requested features."""
    
    def __init__(self, asset_classes: List[str] = None):
        """Initialize adaptive trading agent."""
        self.config = get_agent_config()
        self.trading_config = get_trading_config()
        
        # Initialize components
        self.asset_classes = [AssetClass(ac) for ac in (asset_classes or ['crypto', 'stocks'])]
        self.regime_detector = RegimeDetector()
        self.learning_system = AdaptiveLearningSystem()
        self.time_learning = TimeHistoryLearning()
        
        # Asset-specialized engines
        self.asset_engines = {
            asset_class: AssetSpecializedEngine(asset_class) 
            for asset_class in self.asset_classes
        }
        
        # Shared components
        self.risk_manager = RiskManager()
        self.performance_logger = PerformanceLogger()
        
        # Agent state
        self.positions = {}
        self.trades = []
        self.cash = self.trading_config.portfolio.get('initial_cash', 100000)
        self.portfolio_value = self.cash
        self.peak_value = self.cash
        
        # Adaptive parameters
        self.adaptive_params = AdaptiveParameters()
        
        # Signal tracking
        self.last_signals = {}
        
        self.logger = logging.getLogger("AdaptiveTradingAgent")
        self.logger.info(f"Initialized adaptive agent for: {[ac.value for ac in self.asset_classes]}")
    
    def _determine_asset_class(self, symbol: str) -> AssetClass:
        """Determine asset class from symbol."""
        if any(crypto in symbol.upper() for crypto in ['BTC', 'ETH', 'SOL', 'ADA', 'DOT', 'MATIC']):
            return AssetClass.CRYPTO
        elif any(stock in symbol.upper() for stock in ['AAPL', 'MSFT', 'GOOGL', 'TSLA', 'AMZN', 'NVDA']):
            return AssetClass.STOCKS
        elif any(forex in symbol.upper() for forex in ['USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD']):
            return AssetClass.FOREX
        else:
            return AssetClass.CRYPTO  # Default to crypto
    
    def _create_market_context(self, data: pd.DataFrame, symbol: str) -> MarketContext:
        """Create comprehensive market context."""
        asset_class = self._determine_asset_class(symbol)
        regime, regime_confidence = self.regime_detector.detect_regime(data, asset_class)
        
        # Calculate additional context metrics
        returns = data['close'].pct_change().dropna()
        volatility = returns.std() * np.sqrt(252)
        volume = data['volume'].mean()
        liquidity = data['volume'].std() / data['volume'].mean() if data['volume'].mean() > 0 else 0
        
        # Trend strength
        sma_20 = data['close'].rolling(20).mean()
        sma_50 = data['close'].rolling(50).mean()
        trend_strength = abs(sma_20.iloc[-1] - sma_50.iloc[-1]) / sma_50.iloc[-1] if sma_50.iloc[-1] > 0 else 0
        
        # Momentum
        rsi = compute_rsi(data['close'].values, 14)
        momentum = rsi[-1] if not np.isnan(rsi[-1]) else 50
        
        return MarketContext(
            regime=regime,
            volatility=volatility,
            volume=volume,
            liquidity=liquidity,
            trend_strength=trend_strength,
            momentum=momentum,
            time_of_day=datetime.now().strftime('%H:%M'),
            day_of_week=datetime.now().strftime('%A'),
            asset_class=asset_class
        )
    
    async def process_market_data(self, market_data: Dict[str, pd.DataFrame]) -> List[Dict[str, Any]]:
        """Process market data with adaptive strategy selection."""
        signals = []
        
        for symbol, data in market_data.items():
            try:
                # Check cooldown period
                if self._is_signal_cooldown(symbol):
                    continue
                
                # Create market context
                context = self._create_market_context(data, symbol)
                
                # Get optimal strategy based on learning
                optimal_strategy = self.learning_system.get_optimal_strategy(
                    context.asset_class, context.regime
                )
                
                # Get asset-specialized engine
                asset_engine = self.asset_engines[context.asset_class]
                
                # Analyze with specialization
                analysis_result = asset_engine.analyze_with_specialization(
                    data, symbol, context, optimal_strategy
                )
                
                # Apply time-based adjustments
                time_adjustment = self.time_learning.get_time_adjustment(symbol, datetime.now())
                adjusted_confidence = analysis_result['confidence'] * time_adjustment
                
                # Apply adaptive confidence threshold
                if adjusted_confidence >= self.adaptive_params.confidence_threshold:
                    # Apply risk management filters
                    signal_data = {
                        'symbol': symbol,
                        'signal': analysis_result['signal'],
                        'confidence': adjusted_confidence,
                        'price': data['close'].iloc[-1],
                        'timestamp': datetime.now(),
                        'strategy': optimal_strategy,
                        'regime': context.regime,
                        'asset_class': context.asset_class,
                        'metadata': analysis_result.get('metadata', {})
                    }
                    
                    if self.risk_manager.validate_signal(signal_data, self.positions, self.portfolio_value):
                        signals.append(signal_data)
                        self.last_signals[symbol] = datetime.now()
                        
                        self.logger.info(f"Generated {signal_data['signal']} signal for {symbol} using {optimal_strategy.value} strategy in {context.regime.value} regime with confidence {adjusted_confidence:.2f}")
                
            except Exception as e:
                self.logger.error(f"Error processing {symbol}: {e}")
        
        return signals
    
    def _is_signal_cooldown(self, symbol: str) -> bool:
        """Check if symbol is in signal cooldown period."""
        if symbol not in self.last_signals:
            return False
        
        time_since_last = datetime.now() - self.last_signals[symbol]
        return time_since_last.total_seconds() < self.adaptive_params.signal_cooldown
    
    async def execute_signals(self, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Execute trading signals with adaptive position sizing."""
        executed_trades = []
        
        for signal in signals:
            try:
                # Calculate adaptive position size
                base_position_size = self.risk_manager.calculate_position_size(
                    signal, self.portfolio_value, self.positions
                )
                
                # Apply adaptive multiplier
                adaptive_position_size = base_position_size * self.adaptive_params.position_size_multiplier
                
                if adaptive_position_size > 0:
                    # Execute trade
                    trade = await self._execute_trade(signal, adaptive_position_size)
                    if trade:
                        executed_trades.append(trade)
                        self.trades.append(trade)
                        self._update_portfolio(trade)
                        
                        # Update learning system
                        self.learning_system.update_performance(
                            signal['strategy'], signal['asset_class'], 
                            signal['regime'], trade
                        )
                        
                        # Update time learning
                        self.time_learning.learn_time_patterns(
                            signal['symbol'], 
                            pd.DataFrame(),  # Would pass actual data
                            [trade]
                        )
                        
                        self.logger.info(f"Executed {trade['side']} trade for {trade['symbol']}: {trade['quantity']} @ {trade['price']}")
                
            except Exception as e:
                self.logger.error(f"Error executing signal for {signal['symbol']}: {e}")
        
        # Adapt parameters based on recent performance
        if executed_trades:
            self.adaptive_params = self.learning_system.adapt_parameters(
                self.adaptive_params, executed_trades
            )
        
        return executed_trades
    
    async def _execute_trade(self, signal: Dict[str, Any], position_size: float) -> Optional[Dict[str, Any]]:
        """Execute individual trade (placeholder implementation)."""
        # This would integrate with actual trading interface
        # For now, simulate trade execution
        
        if signal['signal'] == 'BUY':
            cost = position_size * signal['price']
            if cost <= self.cash:
                return {
                    'symbol': signal['symbol'],
                    'side': 'BUY',
                    'quantity': position_size,
                    'price': signal['price'],
                    'timestamp': datetime.now(),
                    'strategy': signal['strategy'].value,
                    'regime': signal['regime'].value,
                    'asset_class': signal['asset_class'].value,
                    'pnl': 0.0  # Would be calculated later
                }
        elif signal['signal'] == 'SELL':
            if signal['symbol'] in self.positions and self.positions[signal['symbol']] >= position_size:
                return {
                    'symbol': signal['symbol'],
                    'side': 'SELL',
                    'quantity': position_size,
                    'price': signal['price'],
                    'timestamp': datetime.now(),
                    'strategy': signal['strategy'].value,
                    'regime': signal['regime'].value,
                    'asset_class': signal['asset_class'].value,
                    'pnl': 0.0  # Would be calculated later
                }
        
        return None
    
    def _update_portfolio(self, trade: Dict[str, Any]) -> None:
        """Update portfolio state after trade execution."""
        if trade['side'] == 'BUY':
            self.cash -= trade['quantity'] * trade['price']
            if trade['symbol'] in self.positions:
                self.positions[trade['symbol']] += trade['quantity']
            else:
                self.positions[trade['symbol']] = trade['quantity']
        elif trade['side'] == 'SELL':
            self.cash += trade['quantity'] * trade['price']
            if trade['symbol'] in self.positions:
                self.positions[trade['symbol']] -= trade['quantity']
                if self.positions[trade['symbol']] <= 0:
                    del self.positions[trade['symbol']]
        
        # Update portfolio value
        self._calculate_portfolio_value()
    
    def _calculate_portfolio_value(self) -> None:
        """Calculate current portfolio value."""
        # This would use current market prices
        # For now, use last trade prices as approximation
        total_value = self.cash
        
        for symbol, quantity in self.positions.items():
            # Placeholder - would get current market price
            last_price = 100.0  # This should be current market price
            total_value += quantity * last_price
        
        self.portfolio_value = total_value
        
        # Update peak value for drawdown calculation
        if self.portfolio_value > self.peak_value:
            self.peak_value = self.portfolio_value
    
    def get_adaptive_metrics(self) -> Dict[str, Any]:
        """Get comprehensive adaptive performance metrics."""
        return {
            'portfolio_value': self.portfolio_value,
            'total_trades': len(self.trades),
            'adaptive_parameters': {
                'confidence_threshold': self.adaptive_params.confidence_threshold,
                'position_size_multiplier': self.adaptive_params.position_size_multiplier,
                'signal_cooldown': self.adaptive_params.signal_cooldown
            },
            'regime_stability': self.regime_detector.get_regime_stability(),
            'learning_performance': {
                key: {
                    'total_trades': perf.total_trades,
                    'win_rate': perf.win_rate,
                    'total_pnl': perf.total_pnl,
                    'sharpe_ratio': perf.sharpe_ratio
                }
                for key, perf in self.learning_system.performance_history.items()
            },
            'time_patterns': dict(self.time_learning.time_patterns)
        }