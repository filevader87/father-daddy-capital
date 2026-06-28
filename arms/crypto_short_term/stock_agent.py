from src.utils.market_regime import detect_market_regime
from src.utils.indicators import compute_rsi, compute_macd, compute_obv, compute_volatility
from src.utils.state_encoder import generate_state_vector
from src.forecasting.ensemble_predictor import get_ensemble_prediction
from src.utils.performance_logger import PerformanceLogger
from src.utils.api_manager import api_manager
from src.risk.risk_manager import RiskManager
from trading_interface import get_latest_price, place_order, get_market_data
from src.rl.memory_qlearning_plastic import MemoryQLearningAgent
from src.decision.memory_bank import MemoryBank
from src.utils.news_analyzer import get_stock_sentiment
from src.utils.technical_analyzer import TechnicalAnalyzer
from src.utils.feature_engineering import micro_feature, macro_feature, sentiment_feature
from typing import Dict, Any, Optional, List, Tuple
import numpy as np
from datetime import datetime, timedelta
from src.models.lstm_model import LSTMModel
from src.models.ltc_cell import LTCCell
from src.config import TradingConfig
import torch
import torch.nn as nn
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from dataclasses import dataclass
from enum import Enum
import logging
from pathlib import Path
import json
import gc
from src.agents.trading.base_agent import BaseTradingAgent
from src.agents.base import AgentInterface

# Enhanced logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/stock_aets.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load configuration
config = TradingConfig.load_from_file()

class TradeType(Enum):
    """Types of trades."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

@dataclass
class TradeMetrics:
    """Trade performance metrics."""
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    avg_trade_duration: float
    avg_profit_per_trade: float
    avg_loss_per_trade: float

class StockAETS(AgentInterface):
    """Advanced Equity Trading System for stock markets."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the stock trading agent.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        super().__init__(config)
        self.logger = logging.getLogger("StockAETS")
        self.positions = {}
        self.trades = []
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate stock agent configuration."""
        default_config = {
            'stock': {
                'min_volume': 100000,
                'min_price': 1.0,
                'max_spread': config.MAX_SPREAD,
                'min_liquidity': config.MIN_LIQUIDITY,
                'market_hours_only': True
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
        
    def _setup_directories(self):
        """Create necessary directories for logging and data storage."""
        directories = ['logs', 'data', 'backtests', 'visualizations']
        for directory in directories:
            Path(directory).mkdir(exist_ok=True)
            
    def _initialize_models(self):
        """Initialize models with memory optimization."""
        try:
            self.lstm_model = LSTMModel().to(self.device)
            if config.USE_LTC:
                self.ltc = LTCCell(units=16).to(self.device)
                self.ltc_to_scalar = nn.Linear(16, 1).to(self.device)
        except Exception as e:
            logger.error(f"Error initializing models: {e}")
            raise
            
    def _get_input_dim(self) -> int:
        """Calculate input dimension based on feature configuration."""
        input_dim = 32  # LSTM hidden size
        if config.USE_LTC:
            input_dim += 16  # LTC hidden size
        if config.USE_SWARM:
            input_dim += 3  # micro, macro, sentiment features
        return input_dim
        
    def _initialize_metrics(self):
        """Initialize performance metrics."""
        self.metrics = {
            'trades': [],
            'positions': [],
            'performance': {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'total_pnl': 0,
                'max_drawdown': 0,
                'sharpe_ratio': 0,
                'sortino_ratio': 0
            }
        }
        
    def _calculate_position_size(self, price: float, volatility: float) -> float:
        """Calculate optimal position size with enhanced risk management and validation."""
        try:
            # Input validation
            if price <= 0 or volatility <= 0:
                logger.warning(f"Invalid inputs for position size calculation: price={price}, volatility={volatility}")
                return 0.0
            
            account_size = risk_manager.get_account_size()
            if account_size <= 0:
                logger.warning("Invalid account size for position calculation")
                return 0.0
                
            max_position_value = account_size * self.config['position_sizing']['max_position_size']
            risk_amount = account_size * self.config['position_sizing']['risk_per_trade']
            
            # Ensure risk amount is positive
            risk_amount = max(risk_amount, 0.0)
            
            # Dynamic position sizing based on market conditions
            if self.config['position_sizing']['dynamic_sizing']:
                market_conditions = self._get_market_conditions()
                risk_adjustment = self._adjust_risk_based_on_conditions(market_conditions)
                risk_amount *= max(risk_adjustment, 0.0)  # Ensure non-negative
                
            # Volatility adjustment with bounds
            volatility_factor = 1.0
            if self.config['position_sizing']['volatility_adjustment']:
                # Bound volatility to prevent division by zero or extreme values
                bounded_volatility = max(min(volatility, 1.0), 0.001)
                volatility_factor = 1 / (1 + bounded_volatility)
                volatility_factor = max(min(volatility_factor, 2.0), 0.1)  # Bound between 0.1 and 2.0
                
            # Calculate position size with safety checks
            if price * volatility_factor <= 0:
                logger.warning(f"Invalid price or volatility factor: price={price}, volatility_factor={volatility_factor}")
                return 0.0
                
            position_size = (risk_amount / (price * volatility_factor)) * volatility_factor
            
            # Apply position limits with validation
            max_position_size = max_position_value / price
            max_risk_position = self._get_max_position_based_on_risk()
            
            # Ensure all limits are positive
            max_position_size = max(max_position_size, 0.0)
            max_risk_position = max(max_risk_position, 0.0)
            
            # Apply minimum position size
            min_position_size = self.config['position_sizing'].get('min_position_size', 0.001)
            
            # Calculate final position size with bounds
            position_size = max(position_size, min_position_size)
            position_size = min(position_size, max_position_size, max_risk_position)
            
            # Final validation
            if position_size <= 0:
                logger.warning(f"Calculated position size is non-positive: {position_size}")
                return 0.0
                
            return round(position_size, 6)
            
        except Exception as e:
            logger.error(f"Error calculating position size: {e}")
            return 0.0
            
    def _get_market_conditions(self) -> Dict[str, Any]:
        """Get current market conditions."""
        try:
            market_data = get_market_data(self.symbol)
            return {
                'volume': market_data['volume'],
                'volatility': market_data['volatility'],
                'liquidity': market_data.get('liquidity', 0),
                'spread': (market_data['ask'] - market_data['bid']) / market_data['price']
            }
        except Exception as e:
            logger.error(f"Error getting market conditions: {e}")
            return {}
            
    def _adjust_risk_based_on_conditions(self, market_conditions: Dict[str, Any]) -> float:
        """Adjust risk based on market conditions."""
        risk_factor = 1.0
        
        # Volume adjustment
        if market_conditions['volume'] < self.config['market_conditions']['min_volume']:
            risk_factor *= 0.5
            
        # Volatility adjustment
        if market_conditions['volatility'] > 0.1:  # High volatility
            risk_factor *= 0.7
            
        # Liquidity adjustment
        if market_conditions['liquidity'] < self.config['market_conditions']['min_liquidity']:
            risk_factor *= 0.6
            
        return risk_factor
        
    def _get_max_position_based_on_risk(self) -> float:
        """Calculate maximum position size based on risk metrics."""
        try:
            risk_metrics = risk_manager.get_position_metrics(self.symbol)
            correlation_risk = risk_manager.get_correlation_risk(self.symbol)
            
            # Adjust based on drawdown
            if risk_metrics['drawdown'] > self.config['risk_management']['max_drawdown']:
                return 0.0
                
            # Adjust based on correlation
            if correlation_risk > self.config['risk_management']['max_correlation']:
                return risk_metrics['max_position_size'] * 0.5
                
            return risk_metrics['max_position_size']
            
        except Exception as e:
            logger.error(f"Error calculating max position: {e}")
            return 0.0
            
    def _validate_market_conditions(self, market_data: Dict[str, Any]) -> bool:
        """Enhanced market condition validation."""
        try:
            # Basic market condition checks
            if not self._check_basic_conditions(market_data):
                return False
                
            # Market hours check
            if not self._check_market_hours():
                return False
                
            # Risk manager checks
            if not self._check_risk_conditions(market_data):
                return False
                
            # Circuit breaker checks
            if not self._check_circuit_breaker():
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"Error validating market conditions: {e}")
            return False
            
    def _check_basic_conditions(self, market_data: Dict[str, Any]) -> bool:
        """Check basic market conditions."""
        if market_data['volume'] < self.config['market_conditions']['min_volume']:
            logger.warning(f"Volume too low: {market_data['volume']}")
            return False
            
        if market_data['price'] < self.config['market_conditions']['min_price']:
            logger.warning(f"Price too low: {market_data['price']}")
            return False
            
        spread = (market_data['ask'] - market_data['bid']) / market_data['price']
        if spread > self.config['market_conditions']['max_spread']:
            logger.warning(f"Spread too high: {spread}")
            return False
            
        return True
        
    def _check_market_hours(self) -> bool:
        """Check if current time is within market hours."""
        if not self.config['market_conditions']['market_hours_only']:
            return True
            
        now = datetime.now()
        market_open = datetime.strptime("09:30", "%H:%M").time()
        market_close = datetime.strptime("16:00", "%H:%M").time()
        
        if now.time() < market_open or now.time() > market_close:
            logger.warning("Outside market hours")
            return False
            
        return True
        
    def _check_risk_conditions(self, market_data: Dict[str, Any]) -> bool:
        """Check risk conditions."""
        if not risk_manager.can_place_order(self.symbol, 1, market_data['price'], 'BUY'):
            logger.warning("Risk manager blocked trade")
            return False
            
        daily_metrics = risk_manager.get_daily_metrics()
        if daily_metrics['trades_today'] >= daily_metrics['max_daily_trades']:
            logger.warning("Daily trade limit reached")
            return False
            
        if daily_metrics['drawdown'] > self.config['risk_management']['max_drawdown']:
            logger.warning("Max drawdown exceeded")
            return False
            
        return True
        
    def _check_circuit_breaker(self) -> bool:
        """Check circuit breaker conditions."""
        try:
            circuit_breaker = self.config['risk_management']['circuit_breaker']
            recent_trades = self.trade_history[-circuit_breaker['max_losses']:]
            
            if len(recent_trades) >= circuit_breaker['max_losses']:
                losing_trades = sum(1 for trade in recent_trades if trade['pnl'] < 0)
                if losing_trades >= circuit_breaker['max_losses']:
                    logger.warning("Circuit breaker triggered")
                    return False
                    
            return True
            
        except Exception as e:
            logger.error(f"Error checking circuit breaker: {e}")
            return True
            
    def act(self, state: Dict[str, Any]) -> tuple:
        """Generate trading action with enhanced error handling and validation."""
        try:
            # Validate state
            if not state or 'price' not in state:
                logger.warning("Invalid state provided to act method")
                return TradeType.HOLD, None
                
            price = state['price']
            if price <= 0:
                logger.warning(f"Invalid price in state: {price}")
                return TradeType.HOLD, None
                
            market_data = get_market_data(self.symbol)
            if not market_data:
                logger.warning(f"No market data available for {self.symbol}")
                return TradeType.HOLD, None
            
            # Calculate technical indicators
            indicators = self._calculate_indicators(market_data)
            if not indicators:
                logger.warning(f"Failed to calculate indicators for {self.symbol}")
                return TradeType.HOLD, None
            
            # Get predictions from models
            predictions = self._get_model_predictions(indicators)
            
            # Generate state vector
            state_vec = self._generate_state_vector(state, indicators, predictions)
            if state_vec is None or len(state_vec) == 0:
                logger.warning(f"Failed to generate state vector for {self.symbol}")
                return TradeType.HOLD, None
            
            # Get action from agent with validation
            try:
                action = self.agent.choose_action(state_vec)
                
                # Validate action
                valid_actions = [TradeType.BUY, TradeType.SELL, TradeType.HOLD]
                if action not in valid_actions:
                    logger.warning(f"Invalid action returned by agent: {action}, defaulting to HOLD")
                    action = TradeType.HOLD
                    
            except Exception as e:
                logger.error(f"Error in agent.choose_action: {e}")
                action = TradeType.HOLD
            
            return action, state_vec
            
        except Exception as e:
            logger.error(f"Error in act method: {e}")
            return TradeType.HOLD, None
            
    def _calculate_indicators(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate technical indicators."""
        try:
            regime = detect_market_regime(market_data['prices'])
            rsi = compute_rsi(market_data['prices'])
            macd = compute_macd(market_data['prices'])
            obv = compute_obv(market_data['prices'], market_data['volumes'])
            volatility = compute_volatility(market_data['prices'])
            
            return {
                'regime': regime,
                'rsi': rsi,
                'macd': macd,
                'obv': obv,
                'volatility': volatility
            }
        except Exception as e:
            logger.error(f"Error calculating indicators: {e}")
            return {}
            
    def _get_model_predictions(self, indicators: Dict[str, Any]) -> Dict[str, Any]:
        """Get predictions from models."""
        try:
            predictions = {}
            
            # LSTM prediction
            lstm_input = torch.tensor(list(indicators.values()), device=self.device).float().unsqueeze(0)
            lstm_pred, lstm_emb = self.lstm_model(lstm_input)
            predictions['lstm'] = lstm_pred
            
            # LTC prediction if enabled
            if config.USE_LTC:
                ltc_input = torch.tensor(lstm_emb[-1], device=self.device).float()
                ltc_hidden = getattr(self, 'ltc_hidden', torch.zeros(16, device=self.device))
                ltc_emb = self.ltc(ltc_input, ltc_hidden)
                self.ltc_hidden = ltc_emb.detach()
                ltc_pred = self.ltc_to_scalar(ltc_emb)
                predictions['ltc'] = ltc_pred
                
            return predictions
            
        except Exception as e:
            logger.error(f"Error getting model predictions: {e}")
            return {}
            
    def _generate_state_vector(self, state: Dict[str, Any], indicators: Dict[str, Any], 
                             predictions: Dict[str, Any]) -> np.ndarray:
        """Generate state vector for the agent."""
        try:
            state_vec = []
            
            # Add basic state features
            state_vec.extend([
                state['price'],
                state['volume'],
                state['volatility']
            ])
            
            # Add indicator features
            state_vec.extend([
                indicators['rsi'],
                indicators['macd']['macd'],
                indicators['macd']['signal'],
                indicators['obv'],
                indicators['volatility']
            ])
            
            # Add prediction features
            if 'lstm' in predictions:
                state_vec.extend(predictions['lstm'].flatten().tolist())
            if 'ltc' in predictions:
                state_vec.extend(predictions['ltc'].flatten().tolist())
                
            return np.array(state_vec)
            
        except Exception as e:
            logger.error(f"Error generating state vector: {e}")
            return np.zeros(self._get_input_dim())
            
    def run_cycle(self) -> Optional[Dict[str, Any]]:
        """Execute one trading cycle with enhanced error handling, logging, and validation."""
        try:
            # Get market data
            market_data = get_market_data(self.symbol)
            if not market_data or not self._validate_market_conditions(market_data):
                logger.warning(f"Market conditions validation failed for {self.symbol}")
                return None
                
            # Get state and action
            state = self._get_state(market_data)
            if not state or 'price' not in state:
                logger.warning(f"Invalid state generated for {self.symbol}")
                return None
                
            action, state_vec = self.act(state)
            
            # Validate action
            if action not in [TradeType.BUY, TradeType.SELL, TradeType.HOLD]:
                logger.warning(f"Invalid action generated: {action}, skipping trade")
                return None
                
            # Skip execution for HOLD actions
            if action == TradeType.HOLD:
                logger.info(f"HOLD action for {self.symbol} - no trade executed")
                return None
            
            # Calculate position size with validation
            qty = self._calculate_position_size(state['price'], market_data['volatility'])
            if qty <= 0:
                logger.warning(f"Invalid position size calculated for {self.symbol}: {qty}")
                return None
                
            notional = state['price'] * qty
            if notional <= 0:
                logger.warning(f"Invalid notional value for {self.symbol}: {notional}")
                return None
            
            # Risk check
            if not risk_manager.can_place_order(self.symbol, qty, state['price'], action):
                self._handle_risk_violation(state_vec, action)
                return None
                
            # Execute trade
            trade_result = self._execute_trade(action, qty, state['price'], state_vec, market_data)
            if not trade_result:
                logger.warning(f"Trade execution failed for {self.symbol}")
                return None
            
            # Update metrics
            self._update_metrics(trade_result)
            
            # Log trade
            self._log_trade(trade_result)
            
            return trade_result
            
        except Exception as e:
            logger.error(f"Error in run_cycle for {self.symbol}: {e}")
            return None
            
    def _get_state(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """Get current state from market data."""
        return {
            'price': market_data['price'],
            'volume': market_data['volume'],
            'bid': market_data['bid'],
            'ask': market_data['ask'],
            'timestamp': market_data['timestamp'],
            'volatility': market_data['volatility']
        }
        
    def _handle_risk_violation(self, state_vec: np.ndarray, action: str):
        """Handle risk violation."""
        log_msg = f"[RISK] Trade blocked for {self.symbol} — exceeds limits."
        self.memory_bank.add({
            "state": state_vec,
            "action": action,
            "prediction": 0.0,
            "reward": -1.0,
            "timestamp": datetime.now().isoformat()
        })
        logger.warning(log_msg)
        
    def _execute_trade(self, action: str, qty: float, price: float, 
                      state_vec: np.ndarray, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute trade with error handling."""
        try:
            # Place order
            place_order(self.symbol, qty, action)
            
            # Calculate reward
            reward = risk_manager.estimate_reward(self.symbol, qty, price, action)
            
            # Update position and record trade
            risk_manager.update_position(self.symbol, qty, price)
            risk_manager.record_trade(self.symbol, qty, price, reward)
            
            # Update agent
            self.agent.update(state_vec, reward)
            
            # Record in memory
            self.memory_bank.add({
                "state": state_vec,
                "action": action,
                "prediction": market_data['prediction'],
                "reward": reward,
                "timestamp": datetime.now().isoformat(),
                "market_data": market_data
            })
            
            return {
                "symbol": self.symbol,
                "action": action,
                "quantity": qty,
                "price": price,
                "notional": price * qty,
                "reward": reward,
                "market_data": market_data
            }
            
        except Exception as e:
            logger.error(f"Error executing trade: {e}")
            return None
            
    def _update_metrics(self, trade_result: Dict[str, Any]):
        """Update performance metrics."""
        if not trade_result:
            return
            
        self.metrics['trades'].append(trade_result)
        self._calculate_performance_metrics()
        
    def _calculate_performance_metrics(self):
        """Calculate performance metrics."""
        trades = self.metrics['trades']
        if not trades:
            return
            
        # Calculate basic metrics
        total_trades = len(trades)
        winning_trades = sum(1 for t in trades if t['reward'] > 0)
        losing_trades = total_trades - winning_trades
        total_pnl = sum(t['reward'] for t in trades)
        
        # Calculate advanced metrics
        returns = [t['reward'] for t in trades]
        sharpe_ratio = self._calculate_sharpe_ratio(returns)
        sortino_ratio = self._calculate_sortino_ratio(returns)
        max_drawdown = self._calculate_max_drawdown(returns)
        
        # Update metrics
        self.metrics['performance'].update({
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'total_pnl': total_pnl,
            'win_rate': winning_trades / total_trades if total_trades > 0 else 0,
            'sharpe_ratio': sharpe_ratio,
            'sortino_ratio': sortino_ratio,
            'max_drawdown': max_drawdown
        })
        
    def _calculate_sharpe_ratio(self, returns: List[float]) -> float:
        """Calculate Sharpe ratio."""
        if not returns:
            return 0.0
        excess_returns = np.array(returns) - 0.02/252  # Risk-free rate
        return np.mean(excess_returns) / np.std(excess_returns) * np.sqrt(252)
        
    def _calculate_sortino_ratio(self, returns: List[float]) -> float:
        """Calculate Sortino ratio."""
        if not returns:
            return 0.0
        excess_returns = np.array(returns) - 0.02/252
        downside_returns = excess_returns[excess_returns < 0]
        if len(downside_returns) == 0:
            return 0.0
        return np.mean(excess_returns) / np.std(downside_returns) * np.sqrt(252)
        
    def _calculate_max_drawdown(self, returns: List[float]) -> float:
        """Calculate maximum drawdown."""
        if not returns:
            return 0.0
        cumulative = np.cumprod(1 + np.array(returns))
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (cumulative - running_max) / running_max
        return abs(min(drawdown))
        
    def _log_trade(self, trade_result: Dict[str, Any]):
        """Log trade with enhanced information."""
        if not trade_result:
            return
            
        log_msg = (
            f"[{trade_result['action']}] {trade_result['quantity']} {self.symbol} "
            f"at ${trade_result['price']:.2f} (Notional: ${trade_result['notional']:.2f})"
        )
        log_trade(
            self.symbol,
            trade_result['action'],
            trade_result['quantity'],
            trade_result['price'],
            trade_result['notional'],
            trade_result['reward']
        )
        logger.info(log_msg)
        
    def backtest(self, historical_data: pd.DataFrame) -> Dict[str, Any]:
        """Run backtest on historical data."""
        try:
            initial_balance = self.config['backtesting']['initial_balance']
            balance = initial_balance
            positions = []
            trades = []
            
            for i in range(len(historical_data)):
                market_data = historical_data.iloc[i].to_dict()
                state = self._get_state(market_data)
                action, _ = self.act(state)
                
                if action != TradeType.HOLD:
                    qty = self._calculate_position_size(state['price'], market_data['volatility'])
                    notional = state['price'] * qty
                    
                    # Simulate trade execution
                    commission = notional * self.config['backtesting']['commission']
                    slippage = notional * self.config['backtesting']['slippage']
                    total_cost = notional + commission + slippage
                    
                    if action == TradeType.BUY and balance >= total_cost:
                        balance -= total_cost
                        positions.append({
                            'symbol': self.symbol,
                            'quantity': qty,
                            'entry_price': state['price'],
                            'entry_time': market_data['timestamp']
                        })
                    elif action == TradeType.SELL and positions:
                        position = positions.pop()
                        pnl = (state['price'] - position['entry_price']) * position['quantity']
                        balance += pnl - commission - slippage
                        
                        trades.append({
                            'symbol': self.symbol,
                            'action': action,
                            'quantity': qty,
                            'entry_price': position['entry_price'],
                            'exit_price': state['price'],
                            'pnl': pnl,
                            'commission': commission,
                            'slippage': slippage,
                            'entry_time': position['entry_time'],
                            'exit_time': market_data['timestamp']
                        })
                        
            # Calculate performance metrics
            returns = [t['pnl'] for t in trades]
            sharpe_ratio = self._calculate_sharpe_ratio(returns)
            sortino_ratio = self._calculate_sortino_ratio(returns)
            max_drawdown = self._calculate_max_drawdown(returns)
            
            return {
                'initial_balance': initial_balance,
                'final_balance': balance,
                'total_return': (balance - initial_balance) / initial_balance,
                'total_trades': len(trades),
                'winning_trades': sum(1 for t in trades if t['pnl'] > 0),
                'losing_trades': sum(1 for t in trades if t['pnl'] <= 0),
                'sharpe_ratio': sharpe_ratio,
                'sortino_ratio': sortino_ratio,
                'max_drawdown': max_drawdown,
                'trades': trades
            }
            
        except Exception as e:
            logger.error(f"Error in backtest: {e}")
            return {}
            
    def visualize_performance(self):
        """Generate performance visualizations."""
        try:
            # Create performance plots
            self._plot_equity_curve()
            self._plot_drawdown()
            self._plot_trade_distribution()
            self._plot_monthly_returns()
            
        except Exception as e:
            logger.error(f"Error generating visualizations: {e}")
            
    def _plot_equity_curve(self):
        """Plot equity curve."""
        trades = self.metrics['trades']
        if not trades:
            return
            
        equity = np.cumsum([t['reward'] for t in trades])
        plt.figure(figsize=(12, 6))
        plt.plot(equity)
        plt.title('Equity Curve')
        plt.xlabel('Trade Number')
        plt.ylabel('Cumulative P&L')
        plt.savefig('visualizations/equity_curve.png')
        plt.close()
        
    def _plot_drawdown(self):
        """Plot drawdown curve."""
        trades = self.metrics['trades']
        if not trades:
            return
            
        equity = np.cumsum([t['reward'] for t in trades])
        running_max = np.maximum.accumulate(equity)
        drawdown = (equity - running_max) / running_max
        
        plt.figure(figsize=(12, 6))
        plt.plot(drawdown)
        plt.title('Drawdown Curve')
        plt.xlabel('Trade Number')
        plt.ylabel('Drawdown')
        plt.savefig('visualizations/drawdown_curve.png')
        plt.close()
        
    def _plot_trade_distribution(self):
        """Plot trade distribution."""
        trades = self.metrics['trades']
        if not trades:
            return
            
        pnls = [t['reward'] for t in trades]
        plt.figure(figsize=(12, 6))
        sns.histplot(pnls, bins=50)
        plt.title('Trade Distribution')
        plt.xlabel('P&L')
        plt.ylabel('Frequency')
        plt.savefig('visualizations/trade_distribution.png')
        plt.close()
        
    def _plot_monthly_returns(self):
        """Plot monthly returns."""
        trades = self.metrics['trades']
        if not trades:
            return
            
        # Group trades by month
        trades_df = pd.DataFrame(trades)
        trades_df['timestamp'] = pd.to_datetime(trades_df['timestamp'])
        monthly_returns = trades_df.groupby(trades_df['timestamp'].dt.to_period('M'))['reward'].sum()
        
        plt.figure(figsize=(12, 6))
        monthly_returns.plot(kind='bar')
        plt.title('Monthly Returns')
        plt.xlabel('Month')
        plt.ylabel('Return')
        plt.savefig('visualizations/monthly_returns.png')
        plt.close()
        
    def save_state(self):
        """Save current state to file."""
        try:
            state = {
                'metrics': self.metrics,
                'config': self.config,
                'agent_state': self.agent.get_state(),
                'memory_bank': self.memory_bank.get_state()
            }
            
            with open(f'data/{self.symbol}_state.json', 'w') as f:
                json.dump(state, f)
                
        except Exception as e:
            logger.error(f"Error saving state: {e}")
            
    def load_state(self):
        """Load state from file."""
        try:
            with open(f'data/{self.symbol}_state.json', 'r') as f:
                state = json.load(f)
                
            self.metrics = state['metrics']
            self.config = state['config']
            self.agent.load_state(state['agent_state'])
            self.memory_bank.load_state(state['memory_bank'])
            
        except Exception as e:
            logger.error(f"Error loading state: {e}")
            
    def cleanup(self):
        """Clean up resources."""
        try:
            # Clear memory
            del self.memory_bank
            del self.agent
            gc.collect()
            
            # Save state
            self.save_state()
            
        except Exception as e:
            logger.error(f"Error in cleanup: {e}")

    def fetch_data(self):
        """Fetch raw data for processing."""
        return self._fetch_market_data()

    def preprocess(self, raw):
        """Preprocess raw data into features."""
        return self._preprocess_data(raw)

    def predict(self, features):
        """Generate predictions from features."""
        return self._generate_signals(features)

    def act(self, signal):
        """Execute actions based on predictions."""
        return self._execute_trades(signal)
