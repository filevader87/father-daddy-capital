"""
Unified Trading Agent
--------------------
Single agent that handles all trading strategies and asset classes
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

class StrategyType(Enum):
    """Available trading strategies."""
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    ARBITRAGE = "arbitrage"

class AssetClass(Enum):
    """Supported asset classes."""
    CRYPTO = "crypto"
    STOCKS = "stocks"
    FOREX = "forex"

class TradeSignal(Enum):
    """Trade signal types."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

@dataclass
class Signal:
    """Trading signal with confidence and metadata."""
    symbol: str
    signal: TradeSignal
    confidence: float
    price: float
    timestamp: datetime
    strategy: StrategyType
    metadata: Dict[str, Any]

@dataclass
class Trade:
    """Trade execution record."""
    symbol: str
    side: str
    quantity: float
    price: float
    timestamp: datetime
    strategy: StrategyType
    pnl: Optional[float] = None

class StrategyEngine:
    """Strategy execution engine."""
    
    def __init__(self, strategy_type: StrategyType):
        self.strategy_type = strategy_type
        self.logger = logging.getLogger(f"StrategyEngine-{strategy_type.value}")
    
    def analyze(self, data: pd.DataFrame, symbol: str) -> Signal:
        """Analyze market data and generate trading signal."""
        if self.strategy_type == StrategyType.MOMENTUM:
            return self._momentum_strategy(data, symbol)
        elif self.strategy_type == StrategyType.MEAN_REVERSION:
            return self._mean_reversion_strategy(data, symbol)
        elif self.strategy_type == StrategyType.BREAKOUT:
            return self._breakout_strategy(data, symbol)
        elif self.strategy_type == StrategyType.ARBITRAGE:
            return self._arbitrage_strategy(data, symbol)
        else:
            return Signal(symbol, TradeSignal.HOLD, 0.0, 0.0, datetime.now(), self.strategy_type, {})
    
    def _momentum_strategy(self, data: pd.DataFrame, symbol: str) -> Signal:
        """Momentum-based trading strategy."""
        if len(data) < 20:
            return Signal(symbol, TradeSignal.HOLD, 0.0, 0.0, datetime.now(), self.strategy_type, {})
        
        # Calculate momentum indicators
        rsi = compute_rsi(data['close'].values, 14)
        macd, macd_signal = compute_macd(data['close'].values)
        sma_20 = data['close'].rolling(20).mean()
        sma_50 = data['close'].rolling(50).mean()
        
        current_price = data['close'].iloc[-1]
        current_rsi = rsi[-1] if not np.isnan(rsi[-1]) else 50
        current_macd = macd[-1] if not np.isnan(macd[-1]) else 0
        current_macd_signal = macd_signal[-1] if not np.isnan(macd_signal[-1]) else 0
        
        # Momentum signals
        bullish_momentum = (
            current_rsi > 50 and current_rsi < 70 and  # RSI in bullish range
            current_macd > current_macd_signal and      # MACD bullish crossover
            sma_20.iloc[-1] > sma_50.iloc[-1]          # Short-term trend above long-term
        )
        
        bearish_momentum = (
            current_rsi < 50 and current_rsi > 30 and  # RSI in bearish range
            current_macd < current_macd_signal and      # MACD bearish crossover
            sma_20.iloc[-1] < sma_50.iloc[-1]          # Short-term trend below long-term
        )
        
        if bullish_momentum:
            confidence = min(0.9, (current_rsi - 50) / 20 + 0.5)
            return Signal(
                symbol=symbol,
                signal=TradeSignal.BUY,
                confidence=confidence,
                price=current_price,
                timestamp=datetime.now(),
                strategy=self.strategy_type,
                metadata={'rsi': current_rsi, 'macd': current_macd}
            )
        elif bearish_momentum:
            confidence = min(0.9, (50 - current_rsi) / 20 + 0.5)
            return Signal(
                symbol=symbol,
                signal=TradeSignal.SELL,
                confidence=confidence,
                price=current_price,
                timestamp=datetime.now(),
                strategy=self.strategy_type,
                metadata={'rsi': current_rsi, 'macd': current_macd}
            )
        else:
            return Signal(symbol, TradeSignal.HOLD, 0.0, current_price, datetime.now(), self.strategy_type, {})
    
    def _mean_reversion_strategy(self, data: pd.DataFrame, symbol: str) -> Signal:
        """Mean reversion trading strategy."""
        if len(data) < 20:
            return Signal(symbol, TradeSignal.HOLD, 0.0, 0.0, datetime.now(), self.strategy_type, {})
        
        # Calculate mean reversion indicators
        sma_20 = data['close'].rolling(20).mean()
        std_20 = data['close'].rolling(20).std()
        current_price = data['close'].iloc[-1]
        current_sma = sma_20.iloc[-1]
        current_std = std_20.iloc[-1]
        
        # Bollinger Bands
        upper_band = current_sma + (2 * current_std)
        lower_band = current_sma - (2 * current_std)
        
        # Mean reversion signals
        oversold = current_price < lower_band
        overbought = current_price > upper_band
        
        if oversold:
            confidence = min(0.9, (lower_band - current_price) / current_std)
            return Signal(
                symbol=symbol,
                signal=TradeSignal.BUY,
                confidence=confidence,
                price=current_price,
                timestamp=datetime.now(),
                strategy=self.strategy_type,
                metadata={'bollinger_position': (current_price - lower_band) / (upper_band - lower_band)}
            )
        elif overbought:
            confidence = min(0.9, (current_price - upper_band) / current_std)
            return Signal(
                symbol=symbol,
                signal=TradeSignal.SELL,
                confidence=confidence,
                price=current_price,
                timestamp=datetime.now(),
                strategy=self.strategy_type,
                metadata={'bollinger_position': (current_price - lower_band) / (upper_band - lower_band)}
            )
        else:
            return Signal(symbol, TradeSignal.HOLD, 0.0, current_price, datetime.now(), self.strategy_type, {})
    
    def _breakout_strategy(self, data: pd.DataFrame, symbol: str) -> Signal:
        """Breakout trading strategy."""
        if len(data) < 20:
            return Signal(symbol, TradeSignal.HOLD, 0.0, 0.0, datetime.now(), self.strategy_type, {})
        
        # Calculate breakout indicators
        high_20 = data['high'].rolling(20).max()
        low_20 = data['low'].rolling(20).min()
        current_price = data['close'].iloc[-1]
        current_high = high_20.iloc[-1]
        current_low = low_20.iloc[-1]
        
        # Volume confirmation
        avg_volume = data['volume'].rolling(20).mean().iloc[-1]
        current_volume = data['volume'].iloc[-1]
        volume_spike = current_volume > (avg_volume * 1.5)
        
        # Breakout signals
        bullish_breakout = current_price > current_high and volume_spike
        bearish_breakout = current_price < current_low and volume_spike
        
        if bullish_breakout:
            confidence = min(0.9, (current_volume / avg_volume) / 3)
            return Signal(
                symbol=symbol,
                signal=TradeSignal.BUY,
                confidence=confidence,
                price=current_price,
                timestamp=datetime.now(),
                strategy=self.strategy_type,
                metadata={'volume_ratio': current_volume / avg_volume}
            )
        elif bearish_breakout:
            confidence = min(0.9, (current_volume / avg_volume) / 3)
            return Signal(
                symbol=symbol,
                signal=TradeSignal.SELL,
                confidence=confidence,
                price=current_price,
                timestamp=datetime.now(),
                strategy=self.strategy_type,
                metadata={'volume_ratio': current_volume / avg_volume}
            )
        else:
            return Signal(symbol, TradeSignal.HOLD, 0.0, current_price, datetime.now(), self.strategy_type, {})
    
    def _arbitrage_strategy(self, data: pd.DataFrame, symbol: str) -> Signal:
        """Arbitrage trading strategy (placeholder for cross-exchange arbitrage)."""
        # This would typically involve comparing prices across exchanges
        # For now, return HOLD signal
        return Signal(symbol, TradeSignal.HOLD, 0.0, data['close'].iloc[-1], datetime.now(), self.strategy_type, {})

class UnifiedTradingAgent:
    """Unified trading agent that handles all strategies and asset classes."""
    
    def __init__(self, strategy_type: str = None, asset_class: str = None):
        """Initialize unified trading agent.
        
        Args:
            strategy_type: Trading strategy type (momentum, mean_reversion, breakout, arbitrage)
            asset_class: Asset class (crypto, stocks, forex)
        """
        self.config = get_agent_config()
        self.trading_config = get_trading_config()
        
        # Set strategy and asset class from config if not provided
        self.strategy_type = StrategyType(strategy_type or self.config.strategy_type)
        self.asset_class = AssetClass(asset_class or self.config.asset_class)
        
        # Initialize components
        self.strategy_engine = StrategyEngine(self.strategy_type)
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
        
        self.logger = logging.getLogger(f"UnifiedAgent-{self.strategy_type.value}-{self.asset_class.value}")
        self.logger.info(f"Initialized {self.strategy_type.value} agent for {self.asset_class.value}")
    
    async def process_market_data(self, market_data: Dict[str, pd.DataFrame]) -> List[Signal]:
        """Process market data and generate trading signals.
        
        Args:
            market_data: Dictionary of symbol -> DataFrame with OHLCV data
            
        Returns:
            List of trading signals
        """
        signals = []
        
        for symbol, data in market_data.items():
            try:
                # Check cooldown period
                if self._is_signal_cooldown(symbol):
                    continue
                
                # Generate signal from strategy engine
                signal = self.strategy_engine.analyze(data, symbol)
                
                # Filter by confidence threshold
                if signal.confidence >= self.config.signal_threshold:
                    # Apply risk management filters
                    if self.risk_manager.validate_signal(signal, self.positions, self.portfolio_value):
                        signals.append(signal)
                        self.last_signals[symbol] = datetime.now()
                        self.logger.info(f"Generated {signal.signal.value} signal for {symbol} with confidence {signal.confidence:.2f}")
                
            except Exception as e:
                self.logger.error(f"Error processing {symbol}: {e}")
        
        return signals
    
    def _is_signal_cooldown(self, symbol: str) -> bool:
        """Check if symbol is in signal cooldown period."""
        if symbol not in self.last_signals:
            return False
        
        time_since_last = datetime.now() - self.last_signals[symbol]
        return time_since_last.total_seconds() < self.signal_cooldown
    
    async def execute_signals(self, signals: List[Signal]) -> List[Trade]:
        """Execute trading signals.
        
        Args:
            signals: List of trading signals to execute
            
        Returns:
            List of executed trades
        """
        executed_trades = []
        
        for signal in signals:
            try:
                # Calculate position size based on risk management
                position_size = self.risk_manager.calculate_position_size(
                    signal, self.portfolio_value, self.positions
                )
                
                if position_size > 0:
                    # Execute trade (placeholder - would integrate with actual trading interface)
                    trade = await self._execute_trade(signal, position_size)
                    if trade:
                        executed_trades.append(trade)
                        self.trades.append(trade)
                        self._update_portfolio(trade)
                        
                        self.logger.info(f"Executed {trade.side} trade for {trade.symbol}: {trade.quantity} @ {trade.price}")
                
            except Exception as e:
                self.logger.error(f"Error executing signal for {signal.symbol}: {e}")
        
        return executed_trades
    
    async def _execute_trade(self, signal: Signal, position_size: float) -> Optional[Trade]:
        """Execute individual trade (placeholder implementation)."""
        # This would integrate with actual trading interface
        # For now, simulate trade execution
        
        if signal.signal == TradeSignal.BUY:
            cost = position_size * signal.price
            if cost <= self.cash:
                return Trade(
                    symbol=signal.symbol,
                    side="BUY",
                    quantity=position_size,
                    price=signal.price,
                    timestamp=datetime.now(),
                    strategy=signal.strategy
                )
        elif signal.signal == TradeSignal.SELL:
            if signal.symbol in self.positions and self.positions[signal.symbol] >= position_size:
                return Trade(
                    symbol=signal.symbol,
                    side="SELL",
                    quantity=position_size,
                    price=signal.price,
                    timestamp=datetime.now(),
                    strategy=signal.strategy
                )
        
        return None
    
    def _update_portfolio(self, trade: Trade) -> None:
        """Update portfolio state after trade execution."""
        if trade.side == "BUY":
            self.cash -= trade.quantity * trade.price
            if trade.symbol in self.positions:
                self.positions[trade.symbol] += trade.quantity
            else:
                self.positions[trade.symbol] = trade.quantity
        elif trade.side == "SELL":
            self.cash += trade.quantity * trade.price
            if trade.symbol in self.positions:
                self.positions[trade.symbol] -= trade.quantity
                if self.positions[trade.symbol] <= 0:
                    del self.positions[trade.symbol]
        
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
    
    def get_portfolio_summary(self) -> Dict[str, Any]:
        """Get portfolio summary."""
        return {
            'cash': self.cash,
            'portfolio_value': self.portfolio_value,
            'positions': self.positions,
            'total_trades': len(self.trades),
            'strategy': self.strategy_type.value,
            'asset_class': self.asset_class.value,
            'drawdown': (self.peak_value - self.portfolio_value) / self.peak_value if self.peak_value > 0 else 0
        }
    
    def get_performance_metrics(self) -> Dict[str, float]:
        """Get performance metrics."""
        if not self.trades:
            return {}
        
        # Calculate basic performance metrics
        total_pnl = sum(trade.pnl for trade in self.trades if trade.pnl is not None)
        win_rate = len([t for t in self.trades if t.pnl and t.pnl > 0]) / len(self.trades)
        
        return {
            'total_pnl': total_pnl,
            'win_rate': win_rate,
            'total_trades': len(self.trades),
            'portfolio_value': self.portfolio_value,
            'drawdown': (self.peak_value - self.portfolio_value) / self.peak_value if self.peak_value > 0 else 0
        }