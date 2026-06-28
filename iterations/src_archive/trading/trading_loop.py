"""
Core Trading Loop Implementation
------------------------------
This module contains the core trading logic implementation. It:
1. Implements portfolio management and position sizing
2. Handles RL-based trading decisions
3. Manages risk and position adjustments
4. Implements the core trading mechanics

This is the main trading engine that:
- Calculates portfolio weights
- Manages position sizing
- Handles trade execution
- Implements risk management
- Monitors and repairs model performance

This module is imported and used by the main deploy_loop.py script.
Do not run this file directly - use deploy_loop.py for production deployment.
"""
from src.utils.portfolio import compute_weights
from src.config import TradingConfig
from src.agents.short_term.crypto_aets import CryptoAETS
from src.utils.api_manager import api_manager
import numpy as np
from typing import Dict, List, Optional, Any
import time
import logging
from datetime import datetime
from src.logger import logger

# Load configuration
config = TradingConfig.load_from_file()

class TradingLoop:
    """Core trading loop for executing trades."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the trading loop.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        self.config = self._load_config(config)
        self.logger = logging.getLogger("TradingLoop")
        self.positions = {}
        self.trades = []
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate trading loop configuration."""
        default_config = {
            'loop': {
                'check_interval': 60,
                'max_iterations': 1000,
                'timeout': 3600
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

def get_current_returns() -> np.ndarray:
    """Get current returns for all assets in the portfolio."""
    assets = api_manager.get_active_assets()
    current_prices = api_manager.get_current_prices(assets)
    previous_prices = api_manager.get_previous_prices(assets)
    
    # Calculate returns
    returns = []
    for asset in assets:
        if previous_prices[asset] > 0:  # Avoid division by zero
            ret = (current_prices[asset] - previous_prices[asset]) / previous_prices[asset]
            returns.append(ret)
        else:
            returns.append(0.0)
            
    return np.array(returns)

def get_current_asset() -> str:
    """Get the current asset being traded."""
    return api_manager.get_current_symbol()

def get_rl_position_size(agent: CryptoAETS, state: Dict) -> float:
    """Get the position size from the RL agent."""
    action, _ = agent.act(state)
    return agent._calculate_position_size(
        state['price'],
        state['volatility_24h']
    )

def deploy_loop():
    """Main deployment loop for the trading system."""
    tick_counter = 0
    past_returns_window = []  # Store historical returns
    pnl_window = []  # Store recent PnL values
    weights = {}  # Store current portfolio weights
    
    # Initialize trading agents
    agents: Dict[str, CryptoAETS] = {}
    active_assets = api_manager.get_active_assets()
    for asset in active_assets:
        agents[asset] = CryptoAETS(asset)
    
    while True:
        try:
            # Get current market state
            current_asset = get_current_asset()
            market_data = api_manager.get_market_data(current_asset)
            
            if not market_data:
                time.sleep(1)  # Wait if no data available
                continue
                
            # Update historical returns
            current_returns = get_current_returns()
            past_returns_window.append(current_returns)
            if len(past_returns_window) > 100:  # Keep last 100 returns
                past_returns_window.pop(0)
                
            # Rebalance portfolio weights periodically
            if tick_counter % config.RISK_REBALANCE_EVERY == 0:
                if len(past_returns_window) >= 10:  # Ensure enough data
                    weights = compute_weights(np.array(past_returns_window))
                    
            # Get base position size from RL agent
            agent = agents[current_asset]
            position_size = get_rl_position_size(agent, market_data)
            
            # Adjust position size with portfolio weights
            if weights and current_asset in weights:
                position_size *= weights[current_asset]
                
            # Execute trade with adjusted position size
            if position_size > 0:
                trade_result = api_manager.place_order(
                    symbol=current_asset,
                    quantity=position_size,
                    side='BUY' if market_data['prediction'] > 0 else 'SELL'
                )
                
                # Update PnL window
                if trade_result and 'pnl' in trade_result:
                    pnl_window.append(trade_result['pnl'])
                    if len(pnl_window) > 50:
                        pnl_window.pop(0)
                        
                    # Check for model repair
                    if len(pnl_window) >= 50 and tick_counter % config.REPAIR_CHECK_EVERY == 0:
                        std_pnl = np.std(pnl_window)
                        if std_pnl > config.REPAIR_THRESHOLD:
                            # Clamp model weights
                            for p in agent.lstm_model.parameters():
                                p.data.clamp_(-config.WEIGHT_MAX, config.WEIGHT_MAX)
                            for p in agent.ltc.parameters():
                                p.data.clamp_(-config.WEIGHT_MAX, config.WEIGHT_MAX)
                
            # Log trade and update agent
            agent.run_cycle()
            
            # Increment tick counter
            tick_counter += 1
            
            # Sleep to control loop frequency
            time.sleep(1)
            
        except Exception as e:
            logger.error("Error in deploy loop", extra={"error": str(e)})
            time.sleep(5)  # Wait longer on error 