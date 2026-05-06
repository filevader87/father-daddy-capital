"""
Hybrid Trading Agent Architecture
--------------------------------
Optimal balance between specialization and efficiency for high-level trading
"""

import asyncio
import logging
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import torch
import torch.nn as nn

from src.config.unified_config import get_agent_config, get_trading_config
from src.utils.indicators import compute_rsi, compute_macd, compute_obv, compute_volatility
from src.utils.market_regime import detect_market_regime
from src.utils.technical_analyzer import TechnicalAnalyzer
from src.utils.risk_metrics import calculate_var, calculate_sharpe_ratio
from src.risk.risk_manager import RiskManager
from src.utils.performance_logger import PerformanceLogger

logger = logging.getLogger(__name__)

class MarketRegime(Enum):
    """Market regime types."""
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"
    LOW_VOLATILITY = "low_volatility"

class AssetClass(Enum):
    """Asset class types with specific characteristics."""
    CRYPTO = "crypto"
    STOCKS = "stocks"
    FOREX = "forex"
    COMMODITIES = "commodities"

class StrategyType(Enum):
    """Strategy types optimized for different conditions."""
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    ARBITRAGE = "arbitrage"
    MARKET_MAKING = "market_making"
    SCALPING = "scalping"

@dataclass
class MarketContext:
    """Market context for strategy selection."""
    regime: MarketRegime
    volatility: float
    volume: float
    liquidity: float
    time_of_day: str
    asset_class: AssetClass

@dataclass
class Signal:
    """Enhanced trading signal with context."""
    symbol: str
    signal: str  # BUY, SELL, HOLD
    confidence: float
    price: float
    timestamp: datetime
    strategy: StrategyType
    market_context: MarketContext
    metadata: Dict[str, Any]

class SpecializedStrategyEngine:
    """Specialized strategy engines for different market conditions."""
    
    def __init__(self, asset_class: AssetClass):
        self.asset_class = asset_class
        self.logger = logging.getLogger(f"SpecializedEngine-{asset_class.value}")
        
        # Asset-specific parameters
        self._setup_asset_parameters()
    
    def _setup_asset_parameters(self):
        """Setup asset-specific trading parameters."""
        if self.asset_class == AssetClass.CRYPTO:
            self.min_volume = 1000000  # Higher volume requirement
            self.max_spread = 0.002    # 0.2% max spread
            self.volatility_threshold = 0.05  # 5% volatility threshold
            self.liquidity_threshold = 50000
        elif self.asset_class == AssetClass.STOCKS:
            self.min_volume = 100000   # Lower volume requirement
            self.max_spread = 0.001    # 0.1% max spread
            self.volatility_threshold = 0.03  # 3% volatility threshold
            self.liquidity_threshold = 10000
        elif self.asset_class == AssetClass.FOREX:
            self.min_volume = 1000000
            self.max_spread = 0.0005   # 0.05% max spread
            self.volatility_threshold = 0.02  # 2% volatility threshold
            self.liquidity_threshold = 100000
    
    def analyze_with_context(self, data: pd.DataFrame, symbol: str, context: MarketContext) -> Signal:
        """Analyze with market context for optimal strategy selection."""
        
        # Select optimal strategy based on market context
        optimal_strategy = self._select_optimal_strategy(context)
        
        # Execute strategy with asset-specific parameters
        if optimal_strategy == StrategyType.MOMENTUM:
            return self._momentum_strategy(data, symbol, context)
        elif optimal_strategy == StrategyType.MEAN_REVERSION:
            return self._mean_reversion_strategy(data, symbol, context)
        elif optimal_strategy == StrategyType.BREAKOUT:
            return self._breakout_strategy(data, symbol, context)
        elif optimal_strategy == StrategyType.SCALPING:
            return self._scalping_strategy(data, symbol, context)
        elif optimal_strategy == StrategyType.MARKET_MAKING:
            return self._market_making_strategy(data, symbol, context)
        else:
            return Signal(symbol, "HOLD", 0.0, data['close'].iloc[-1], datetime.now(), 
                         optimal_strategy, context, {})
    
    def _select_optimal_strategy(self, context: MarketContext) -> StrategyType:
        """Select optimal strategy based on market context."""
        
        # Strategy selection logic based on market regime and asset class
        if context.regime == MarketRegime.TRENDING:
            if context.volatility < self.volatility_threshold:
                return StrategyType.MOMENTUM
            else:
                return StrategyType.BREAKOUT
        elif context.regime == MarketRegime.RANGING:
            if context.asset_class == AssetClass.CRYPTO:
                return StrategyType.MEAN_REVERSION
            else:
                return StrategyType.MARKET_MAKING
        elif context.regime == MarketRegime.VOLATILE:
            if context.asset_class == AssetClass.CRYPTO:
                return StrategyType.SCALPING
            else:
                return StrategyType.BREAKOUT
        else:  # LOW_VOLATILITY
            return StrategyType.MARKET_MAKING
    
    def _momentum_strategy(self, data: pd.DataFrame, symbol: str, context: MarketContext) -> Signal:
        """Momentum strategy with asset-specific optimizations."""
        if len(data) < 20:
            return Signal(symbol, "HOLD", 0.0, 0.0, datetime.now(), StrategyType.MOMENTUM, context, {})
        
        # Asset-specific momentum indicators
        if context.asset_class == AssetClass.CRYPTO:
            # Crypto-specific momentum (more aggressive)
            rsi_period = 14
            macd_fast, macd_slow, macd_signal = 12, 26, 9
        else:
            # Traditional assets (more conservative)
            rsi_period = 21
            macd_fast, macd_slow, macd_signal = 8, 17, 9
        
        # Calculate indicators
        rsi = compute_rsi(data['close'].values, rsi_period)
        macd, macd_signal_line = compute_macd(data['close'].values)
        
        current_price = data['close'].iloc[-1]
        current_rsi = rsi[-1] if not np.isnan(rsi[-1]) else 50
        current_macd = macd[-1] if not np.isnan(macd[-1]) else 0
        current_macd_signal = macd_signal_line[-1] if not np.isnan(macd_signal_line[-1]) else 0
        
        # Asset-specific signal generation
        if context.asset_class == AssetClass.CRYPTO:
            # More aggressive crypto signals
            bullish = current_rsi > 45 and current_rsi < 75 and current_macd > current_macd_signal
            bearish = current_rsi < 55 and current_rsi > 25 and current_macd < current_macd_signal
        else:
            # Conservative traditional asset signals
            bullish = current_rsi > 50 and current_rsi < 70 and current_macd > current_macd_signal
            bearish = current_rsi < 50 and current_rsi > 30 and current_macd < current_macd_signal
        
        if bullish:
            confidence = min(0.9, (current_rsi - 50) / 20 + 0.6)
            return Signal(symbol, "BUY", confidence, current_price, datetime.now(), 
                         StrategyType.MOMENTUM, context, {'rsi': current_rsi, 'macd': current_macd})
        elif bearish:
            confidence = min(0.9, (50 - current_rsi) / 20 + 0.6)
            return Signal(symbol, "SELL", confidence, current_price, datetime.now(), 
                         StrategyType.MOMENTUM, context, {'rsi': current_rsi, 'macd': current_macd})
        else:
            return Signal(symbol, "HOLD", 0.0, current_price, datetime.now(), 
                         StrategyType.MOMENTUM, context, {})
    
    def _mean_reversion_strategy(self, data: pd.DataFrame, symbol: str, context: MarketContext) -> Signal:
        """Mean reversion strategy with asset-specific parameters."""
        if len(data) < 20:
            return Signal(symbol, "HOLD", 0.0, 0.0, datetime.now(), StrategyType.MEAN_REVERSION, context, {})
        
        # Asset-specific mean reversion parameters
        if context.asset_class == AssetClass.CRYPTO:
            lookback = 14
            std_multiplier = 2.5  # More aggressive for crypto
        else:
            lookback = 20
            std_multiplier = 2.0  # Conservative for traditional assets
        
        sma = data['close'].rolling(lookback).mean()
        std = data['close'].rolling(lookback).std()
        current_price = data['close'].iloc[-1]
        current_sma = sma.iloc[-1]
        current_std = std.iloc[-1]
        
        upper_band = current_sma + (std_multiplier * current_std)
        lower_band = current_sma - (std_multiplier * current_std)
        
        oversold = current_price < lower_band
        overbought = current_price > upper_band
        
        if oversold:
            confidence = min(0.9, (lower_band - current_price) / current_std)
            return Signal(symbol, "BUY", confidence, current_price, datetime.now(), 
                         StrategyType.MEAN_REVERSION, context, {'bollinger_position': (current_price - lower_band) / (upper_band - lower_band)})
        elif overbought:
            confidence = min(0.9, (current_price - upper_band) / current_std)
            return Signal(symbol, "SELL", confidence, current_price, datetime.now(), 
                         StrategyType.MEAN_REVERSION, context, {'bollinger_position': (current_price - lower_band) / (upper_band - lower_band)})
        else:
            return Signal(symbol, "HOLD", 0.0, current_price, datetime.now(), 
                         StrategyType.MEAN_REVERSION, context, {})
    
    def _breakout_strategy(self, data: pd.DataFrame, symbol: str, context: MarketContext) -> Signal:
        """Breakout strategy with volume confirmation."""
        if len(data) < 20:
            return Signal(symbol, "HOLD", 0.0, 0.0, datetime.now(), StrategyType.BREAKOUT, context, {})
        
        # Asset-specific breakout parameters
        if context.asset_class == AssetClass.CRYPTO:
            lookback = 14
            volume_threshold = 2.0  # 2x average volume
        else:
            lookback = 20
            volume_threshold = 1.5  # 1.5x average volume
        
        high_20 = data['high'].rolling(lookback).max()
        low_20 = data['low'].rolling(lookback).min()
        avg_volume = data['volume'].rolling(lookback).mean()
        
        current_price = data['close'].iloc[-1]
        current_high = high_20.iloc[-1]
        current_low = low_20.iloc[-1]
        current_volume = data['volume'].iloc[-1]
        current_avg_volume = avg_volume.iloc[-1]
        
        volume_spike = current_volume > (current_avg_volume * volume_threshold)
        
        bullish_breakout = current_price > current_high and volume_spike
        bearish_breakout = current_price < current_low and volume_spike
        
        if bullish_breakout:
            confidence = min(0.9, (current_volume / current_avg_volume) / 3)
            return Signal(symbol, "BUY", confidence, current_price, datetime.now(), 
                         StrategyType.BREAKOUT, context, {'volume_ratio': current_volume / current_avg_volume})
        elif bearish_breakout:
            confidence = min(0.9, (current_volume / current_avg_volume) / 3)
            return Signal(symbol, "SELL", confidence, current_price, datetime.now(), 
                         StrategyType.BREAKOUT, context, {'volume_ratio': current_volume / current_avg_volume})
        else:
            return Signal(symbol, "HOLD", 0.0, current_price, datetime.now(), 
                         StrategyType.BREAKOUT, context, {})
    
    def _scalping_strategy(self, data: pd.DataFrame, symbol: str, context: MarketContext) -> Signal:
        """High-frequency scalping strategy for volatile markets."""
        if len(data) < 5:
            return Signal(symbol, "HOLD", 0.0, 0.0, datetime.now(), StrategyType.SCALPING, context, {})
        
        # Very short-term indicators for scalping
        ema_5 = data['close'].ewm(span=5).mean()
        ema_10 = data['close'].ewm(span=10).mean()
        
        current_price = data['close'].iloc[-1]
        current_ema_5 = ema_5.iloc[-1]
        current_ema_10 = ema_10.iloc[-1]
        
        # Quick scalping signals
        bullish = current_ema_5 > current_ema_10 and current_price > current_ema_5
        bearish = current_ema_5 < current_ema_10 and current_price < current_ema_5
        
        if bullish:
            confidence = 0.7  # Moderate confidence for scalping
            return Signal(symbol, "BUY", confidence, current_price, datetime.now(), 
                         StrategyType.SCALPING, context, {'ema_5': current_ema_5, 'ema_10': current_ema_10})
        elif bearish:
            confidence = 0.7
            return Signal(symbol, "SELL", confidence, current_price, datetime.now(), 
                         StrategyType.SCALPING, context, {'ema_5': current_ema_5, 'ema_10': current_ema_10})
        else:
            return Signal(symbol, "HOLD", 0.0, current_price, datetime.now(), 
                         StrategyType.SCALPING, context, {})
    
    def _market_making_strategy(self, data: pd.DataFrame, symbol: str, context: MarketContext) -> Signal:
        """Market making strategy for low volatility periods."""
        if len(data) < 10:
            return Signal(symbol, "HOLD", 0.0, 0.0, datetime.now(), StrategyType.MARKET_MAKING, context, {})
        
        # Market making logic (simplified)
        current_price = data['close'].iloc[-1]
        price_std = data['close'].rolling(10).std().iloc[-1]
        
        # Market making signals based on price deviation
        if price_std < current_price * 0.01:  # Low volatility
            # Place buy and sell orders around current price
            return Signal(symbol, "HOLD", 0.5, current_price, datetime.now(), 
                         StrategyType.MARKET_MAKING, context, {'volatility': price_std})
        else:
            return Signal(symbol, "HOLD", 0.0, current_price, datetime.now(), 
                         StrategyType.MARKET_MAKING, context, {})

class HybridTradingAgent:
    """Hybrid trading agent that combines specialization with efficiency."""
    
    def __init__(self, asset_classes: List[str] = None):
        """Initialize hybrid trading agent.
        
        Args:
            asset_classes: List of asset classes to trade
        """
        self.config = get_agent_config()
        self.trading_config = get_trading_config()
        
        # Initialize specialized engines for each asset class
        self.asset_classes = [AssetClass(ac) for ac in (asset_classes or ['crypto'])]
        self.strategy_engines = {
            asset_class: SpecializedStrategyEngine(asset_class) 
            for asset_class in self.asset_classes
        }
        
        # Shared components
        self.risk_manager = RiskManager()
        self.performance_logger = PerformanceLogger()
        self.technical_analyzer = TechnicalAnalyzer()
        
        # Agent state
        self.positions = {}
        self.trades = []
        self.cash = self.trading_config.portfolio.get('initial_cash', 100000)
        self.portfolio_value = self.cash
        self.peak_value = self.cash
        
        # Signal tracking
        self.last_signals = {}
        self.signal_cooldown = self.config.signal_cooldown
        
        self.logger = logging.getLogger("HybridTradingAgent")
        self.logger.info(f"Initialized hybrid agent for asset classes: {[ac.value for ac in self.asset_classes]}")
    
    def _detect_market_context(self, data: pd.DataFrame, symbol: str) -> MarketContext:
        """Detect market context for strategy selection."""
        # Determine asset class from symbol
        asset_class = self._determine_asset_class(symbol)
        
        # Calculate market metrics
        volatility = data['close'].pct_change().std() * np.sqrt(252)  # Annualized volatility
        volume = data['volume'].mean()
        liquidity = data['volume'].std() / data['volume'].mean() if data['volume'].mean() > 0 else 0
        
        # Detect market regime
        regime = self._detect_market_regime(data)
        
        # Determine time of day
        time_of_day = datetime.now().strftime('%H:%M')
        
        return MarketContext(
            regime=regime,
            volatility=volatility,
            volume=volume,
            liquidity=liquidity,
            time_of_day=time_of_day,
            asset_class=asset_class
        )
    
    def _determine_asset_class(self, symbol: str) -> AssetClass:
        """Determine asset class from symbol."""
        if any(crypto in symbol.upper() for crypto in ['BTC', 'ETH', 'SOL', 'ADA', 'DOT']):
            return AssetClass.CRYPTO
        elif any(stock in symbol.upper() for stock in ['AAPL', 'MSFT', 'GOOGL', 'TSLA']):
            return AssetClass.STOCKS
        elif any(forex in symbol.upper() for forex in ['USD', 'EUR', 'GBP', 'JPY']):
            return AssetClass.FOREX
        else:
            return AssetClass.CRYPTO  # Default to crypto
    
    def _detect_market_regime(self, data: pd.DataFrame) -> MarketRegime:
        """Detect current market regime."""
        if len(data) < 20:
            return MarketRegime.RANGING
        
        # Calculate regime indicators
        returns = data['close'].pct_change()
        volatility = returns.std()
        trend_strength = abs(data['close'].iloc[-1] - data['close'].iloc[-20]) / data['close'].iloc[-20]
        
        if volatility > 0.05:  # High volatility
            return MarketRegime.VOLATILE
        elif trend_strength > 0.1:  # Strong trend
            return MarketRegime.TRENDING
        elif volatility < 0.02:  # Low volatility
            return MarketRegime.LOW_VOLATILITY
        else:
            return MarketRegime.RANGING
    
    async def process_market_data(self, market_data: Dict[str, pd.DataFrame]) -> List[Signal]:
        """Process market data with specialized strategy engines."""
        signals = []
        
        for symbol, data in market_data.items():
            try:
                # Check cooldown period
                if self._is_signal_cooldown(symbol):
                    continue
                
                # Detect market context
                context = self._detect_market_context(data, symbol)
                
                # Get appropriate strategy engine
                strategy_engine = self.strategy_engines[context.asset_class]
                
                # Generate signal with context
                signal = strategy_engine.analyze_with_context(data, symbol, context)
                
                # Filter by confidence threshold
                if signal.confidence >= self.config.signal_threshold:
                    # Apply risk management filters
                    if self.risk_manager.validate_signal(signal, self.positions, self.portfolio_value):
                        signals.append(signal)
                        self.last_signals[symbol] = datetime.now()
                        self.logger.info(f"Generated {signal.signal} signal for {symbol} using {signal.strategy.value} strategy with confidence {signal.confidence:.2f}")
                
            except Exception as e:
                self.logger.error(f"Error processing {symbol}: {e}")
        
        return signals
    
    def _is_signal_cooldown(self, symbol: str) -> bool:
        """Check if symbol is in signal cooldown period."""
        if symbol not in self.last_signals:
            return False
        
        time_since_last = datetime.now() - self.last_signals[symbol]
        return time_since_last.total_seconds() < self.signal_cooldown
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """Get performance metrics by strategy and asset class."""
        if not self.trades:
            return {}
        
        # Group trades by strategy and asset class
        strategy_metrics = {}
        for trade in self.trades:
            strategy = trade.strategy.value
            if strategy not in strategy_metrics:
                strategy_metrics[strategy] = {'trades': 0, 'pnl': 0, 'wins': 0}
            
            strategy_metrics[strategy]['trades'] += 1
            if trade.pnl:
                strategy_metrics[strategy]['pnl'] += trade.pnl
                if trade.pnl > 0:
                    strategy_metrics[strategy]['wins'] += 1
        
        # Calculate win rates
        for strategy in strategy_metrics:
            if strategy_metrics[strategy]['trades'] > 0:
                strategy_metrics[strategy]['win_rate'] = strategy_metrics[strategy]['wins'] / strategy_metrics[strategy]['trades']
        
        return strategy_metrics