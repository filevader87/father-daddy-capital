import alpaca_trade_api as tradeapi
import requests
import numpy as np
from dataclasses import dataclass
from typing import Optional, Dict
from datetime import datetime
import json
from pathlib import Path
import logging

@dataclass
class TradeSignal:
    """Container for trade signals."""
    symbol: str
    side: str  # 'buy' or 'sell'
    quantity: float
    price: float
    order_type: str  # 'market' or 'limit'
    strategy: str
    timestamp: Optional[datetime] = None
    metadata: Optional[Dict] = None

class ExecutionAgent:
    def __init__(self, strategy_name: str, max_slippage: float = 0.001, max_retries: int = 3):
        self.strategy_name = strategy_name
        self.max_slippage = max_slippage
        self.max_retries = max_retries
        
        # Initialize portfolio state
        self.cash = 0.0
        self.portfolio_value = 0.0
        self.peak_value = 0.0
        self.positions = {}
        
        # Initialize trade history
        self.trades = []
        
        # Initialize error tracking
        self.errors = []
        
        # Setup logging
        self.logger = logging.getLogger('execution_agent')
        self.logger.setLevel(logging.INFO)
        
        # Create logs directory if it doesn't exist
        log_dir = Path('logs')
        log_dir.mkdir(exist_ok=True)
        
        # Add file handler
        fh = logging.FileHandler(log_dir / 'trading.log')
        fh.setLevel(logging.INFO)
        
        # Create formatter
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        
        # Add handler to logger
        self.logger.addHandler(fh)
        
    def execute_signal(self, signal: TradeSignal) -> bool:
        """Execute a trade signal with comprehensive validation.
        
        Args:
            signal: TradeSignal object containing trade details
            
        Returns:
            bool: True if trade was executed successfully, False otherwise
        """
        try:
            # Comprehensive signal validation
            if not self._validate_signal(signal):
                return False
                
            # Execute based on side
            if signal.side.lower() == 'buy':
                return self.execute_buy(signal)
            elif signal.side.lower() == 'sell':
                return self.execute_sell(signal)
            else:
                self.logger.error(f"Invalid trade side: {signal.side}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error executing trade signal: {str(e)}")
            return False
            
    def _validate_signal(self, signal: TradeSignal) -> bool:
        """Validate trade signal comprehensively."""
        try:
            # Check if signal is None
            if signal is None:
                self.logger.error("Trade signal is None")
                return False
            
            # Validate symbol
            if not signal.symbol or not isinstance(signal.symbol, str):
                self.logger.error(f"Invalid symbol: {signal.symbol}")
                return False
                
            # Validate symbol format (basic check)
            if len(signal.symbol.strip()) == 0:
                self.logger.error("Empty symbol")
                return False
                
            # Validate side
            if not signal.side or not isinstance(signal.side, str):
                self.logger.error(f"Invalid side: {signal.side}")
                return False
                
            valid_sides = ['buy', 'sell']
            if signal.side.lower() not in valid_sides:
                self.logger.error(f"Invalid trade side: {signal.side}")
                return False
                
            # Validate quantity
            if not isinstance(signal.quantity, (int, float)):
                self.logger.error(f"Invalid quantity type: {type(signal.quantity)}")
                return False
                
            if signal.quantity <= 0:
                self.logger.error(f"Invalid quantity: {signal.quantity} (must be positive)")
                return False
                
            # Validate price
            if not isinstance(signal.price, (int, float)):
                self.logger.error(f"Invalid price type: {type(signal.price)}")
                return False
                
            if signal.price <= 0:
                self.logger.error(f"Invalid price: {signal.price} (must be positive)")
                return False
                
            # Validate order type
            if not signal.order_type or not isinstance(signal.order_type, str):
                self.logger.error(f"Invalid order type: {signal.order_type}")
                return False
                
            valid_order_types = ['market', 'limit']
            if signal.order_type.lower() not in valid_order_types:
                self.logger.error(f"Invalid order type: {signal.order_type}")
                return False
                
            # Validate strategy
            if not signal.strategy or not isinstance(signal.strategy, str):
                self.logger.error(f"Invalid strategy: {signal.strategy}")
                return False
                
            # Check for reasonable bounds
            if signal.quantity > 1000000:  # 1M units max
                self.logger.error(f"Quantity too large: {signal.quantity}")
                return False
                
            if signal.price > 1000000:  # $1M max price
                self.logger.error(f"Price too large: {signal.price}")
                return False
                
            # Calculate notional value
            notional = signal.quantity * signal.price
            if notional > 10000000:  # $10M max notional
                self.logger.error(f"Notional value too large: {notional}")
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating signal: {str(e)}")
            return False
            
    def execute_buy(self, signal: TradeSignal) -> bool:
        """Execute a buy order."""
        try:
            # Calculate total cost with slippage
            slippage = np.random.uniform(0, self.max_slippage)
            execution_price = signal.price * (1 + slippage)
            total_cost = signal.quantity * execution_price
            
            # Check if we have enough cash
            if total_cost > self.cash:
                self.logger.error(f"Insufficient cash for buy order: {total_cost} > {self.cash}")
                return False
                
            # Update positions
            if signal.symbol not in self.positions:
                self.positions[signal.symbol] = {'quantity': 0, 'value': 0.0}
                
            # Update position quantity and value
            self.positions[signal.symbol]['quantity'] += signal.quantity
            self.positions[signal.symbol]['value'] = (
                self.positions[signal.symbol]['quantity'] * execution_price
            )
            
            # Update cash and portfolio value
            self.cash -= total_cost
            self.portfolio_value = self.cash + sum(pos['value'] for pos in self.positions.values())
            self.peak_value = max(self.peak_value, self.portfolio_value)
            
            # Log the trade
            self.trades.append({
                'symbol': signal.symbol,
                'side': 'buy',
                'quantity': signal.quantity,
                'price': signal.price,
                'execution_price': execution_price,
                'slippage': slippage,
                'timestamp': datetime.now().isoformat()
            })
            
            return True
        except Exception as e:
            self.logger.error(f"Error executing buy order: {str(e)}")
            self.errors.append(str(e))
            return False
            
    def execute_sell(self, signal: TradeSignal) -> bool:
        """Execute a sell order."""
        try:
            # Check if we have enough shares
            if signal.symbol not in self.positions or self.positions[signal.symbol]['quantity'] < signal.quantity:
                self.logger.error(f"Insufficient shares for sell order: {signal.quantity} > {self.positions[signal.symbol]['quantity']}")
                return False
                
            # Calculate total proceeds with slippage
            slippage = np.random.uniform(0, self.max_slippage)
            execution_price = signal.price * (1 - slippage)
            total_proceeds = signal.quantity * execution_price
            
            # Update positions
            self.positions[signal.symbol]['quantity'] -= signal.quantity
            self.positions[signal.symbol]['value'] = (
                self.positions[signal.symbol]['quantity'] * execution_price
            )
            
            # Update cash and portfolio value
            self.cash += total_proceeds
            self.portfolio_value = self.cash + sum(pos['value'] for pos in self.positions.values())
            
            # Log the trade
            self.trades.append({
                'symbol': signal.symbol,
                'side': 'sell',
                'quantity': signal.quantity,
                'price': signal.price,
                'execution_price': execution_price,
                'slippage': slippage,
                'timestamp': datetime.now().isoformat()
            })
            
            return True
        except Exception as e:
            self.logger.error(f"Error executing sell order: {str(e)}")
            return False
            
    def _update_position(self, symbol: str, quantity: float, price: float):
        """Update position value and portfolio metrics."""
        if symbol in self.positions:
            self.positions[symbol]['value'] = quantity * price
            self.portfolio_value = self.cash + sum(pos['value'] for pos in self.positions.values())
            self.peak_value = max(self.peak_value, self.portfolio_value)

    def get_market_price_alpaca(self, symbol):
        """Fetch the latest market price from Alpaca."""
        try:
            barset = self.alpaca_api.get_latest_bar(symbol)
            return barset.c
        except Exception as e:
            print(f"Error fetching Alpaca market price: {e}")
            return None

    def get_market_price_coinbase(self, symbol):
        """Fetch the latest market price from Coinbase."""
        try:
            response = requests.get(f"{self.coinbase_base_url}/products/{symbol}-USD/ticker")
            if response.status_code == 200:
                return float(response.json()['price'])
            else:
                return None
        except Exception as e:
            print(f"Error fetching Coinbase market price: {e}")
            return None

    def smart_order_routing(self, symbol, quantity, side):
        """Selects the best exchange for execution based on price."""
        alpaca_price = self.get_market_price_alpaca(symbol)
        coinbase_price = self.get_market_price_coinbase(symbol)

        if alpaca_price is None or coinbase_price is None:
            print("Unable to fetch market prices, defaulting to Alpaca.")
            best_exchange = "Alpaca"
        else:
            if side.lower() == "buy":
                best_exchange = "Alpaca" if alpaca_price < coinbase_price else "Coinbase"
            else:
                best_exchange = "Alpaca" if alpaca_price > coinbase_price else "Coinbase"

        return best_exchange

    def execute_trade(self, symbol, quantity, side, limit_price=None):
        """Executes trade using Smart Order Routing (SOR)."""
        best_exchange = self.smart_order_routing(symbol, quantity, side)
        execution_price = limit_price or (self.get_market_price_alpaca(symbol) if best_exchange == "Alpaca" else self.get_market_price_coinbase(symbol))

        slippage = np.random.lognormal(mean=0, sigma=0.001)  
        final_price = execution_price * (1 + slippage) if side.lower() == "buy" else execution_price * (1 - slippage)

        try:
            if best_exchange == "Alpaca":
                order = self.alpaca_api.submit_order(
                    symbol=symbol,
                    qty=quantity,
                    side=side,
                    type="limit" if limit_price else "market",
                    time_in_force="gtc",
                    limit_price=final_price if limit_price else None
                )
            else:
                print(f"Executing {side.upper()} {quantity} {symbol} at {final_price} via Coinbase (simulation).")
                order = {"exchange": "Coinbase", "symbol": symbol, "side": side, "quantity": quantity, "price": final_price}

            print(f"Trade Executed: {side.upper()} {quantity} {symbol} at {final_price} ({best_exchange})")
            return order
        except Exception as e:
            print(f"Error executing trade: {e}")
            return None
