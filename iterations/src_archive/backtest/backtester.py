import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from src.utils.logger import get_logger
from src.config import TradingConfig as config
from src.utils.risk_manager import risk_manager

logger = get_logger(__name__)

class Backtester:
    def __init__(self, initial_balance: float = 100000.0):
        self.initial_balance = initial_balance
        self.current_balance = initial_balance
        self.positions: Dict[str, Dict] = {}
        self.trades: List[Dict] = []
        self.daily_metrics: Dict[str, float] = {
            'balance': initial_balance,
            'equity': initial_balance,
            'drawdown': 0.0,
            'max_drawdown': 0.0,
            'sharpe_ratio': 0.0,
            'sortino_ratio': 0.0
        }
        
    def run_backtest(self, data: pd.DataFrame, strategy, 
                    start_date: Optional[datetime] = None,
                    end_date: Optional[datetime] = None) -> Dict:
        """Run backtest on historical data"""
        if start_date:
            data = data[data.index >= start_date]
        if end_date:
            data = data[data.index <= end_date]
            
        logger.info(f"Starting backtest from {data.index[0]} to {data.index[-1]}")
        
        for idx, row in data.iterrows():
            # Get strategy signals
            signals = strategy.generate_signals(row)
            
            # Execute trades based on signals
            for symbol, signal in signals.items():
                if signal['action'] != 'hold':
                    self._execute_trade(symbol, signal, row)
                    
            # Update metrics
            self._update_metrics(row)
            
        return self._calculate_performance_metrics()
        
    def _execute_trade(self, symbol: str, signal: Dict, data: pd.Series):
        """Execute a trade in the backtest"""
        price = data[symbol]
        qty = self._calculate_position_size(price, signal['action'])
        
        if qty <= 0:
            return
            
        # Check if we can place the order
        if not risk_manager.can_place_order(symbol, qty, price, signal['action']):
            return
            
        # Calculate P&L for existing position
        pnl = 0.0
        if symbol in self.positions:
            current_position = self.positions[symbol]
            pnl = (price - current_position['avg_price']) * current_position['qty']
            
        # Update position
        if signal['action'] == 'buy':
            self._update_position(symbol, qty, price, 'buy')
        else:
            self._update_position(symbol, qty, price, 'sell')
            
        # Record trade
        self.trades.append({
            'timestamp': data.name,
            'symbol': symbol,
            'action': signal['action'],
            'price': price,
            'qty': qty,
            'pnl': pnl
        })
        
    def _update_position(self, symbol: str, qty: float, price: float, action: str):
        """Update position after trade execution"""
        current_position = self.positions.get(symbol, {'qty': 0, 'avg_price': 0})
        
        if action == 'buy':
            new_qty = current_position['qty'] + qty
            new_avg_price = ((current_position['qty'] * current_position['avg_price']) + 
                           (qty * price)) / new_qty
        else:
            new_qty = current_position['qty'] - qty
            new_avg_price = current_position['avg_price']
            
        if new_qty == 0:
            del self.positions[symbol]
        else:
            self.positions[symbol] = {
                'qty': new_qty,
                'avg_price': new_avg_price
            }
            
    def _calculate_position_size(self, price: float, action: str) -> float:
        """Calculate position size based on risk parameters"""
        risk_amount = self.current_balance * config.get_trading_risk_limit('position_risk')
        position_size = risk_amount / price
        
        # Round to appropriate decimal places
        if action == 'buy':
            return round(position_size, 6)
        return round(position_size, 6)
        
    def _update_metrics(self, data: pd.Series):
        """Update performance metrics"""
        # Calculate current equity
        position_values = sum(
            pos['qty'] * data[symbol] 
            for symbol, pos in self.positions.items()
        )
        self.current_balance = self.initial_balance + sum(trade['pnl'] for trade in self.trades)
        equity = self.current_balance + position_values
        
        # Update drawdown
        drawdown = (self.initial_balance - equity) / self.initial_balance
        self.daily_metrics['drawdown'] = drawdown
        self.daily_metrics['max_drawdown'] = max(
            self.daily_metrics['max_drawdown'],
            drawdown
        )
        
        # Update daily metrics
        self.daily_metrics['balance'] = self.current_balance
        self.daily_metrics['equity'] = equity
        
    def _calculate_performance_metrics(self) -> Dict:
        """Calculate final performance metrics"""
        returns = pd.Series([trade['pnl'] for trade in self.trades])
        
        # Calculate Sharpe Ratio
        risk_free_rate = 0.02  # 2% annual risk-free rate
        daily_rf = (1 + risk_free_rate) ** (1/252) - 1
        excess_returns = returns - daily_rf
        sharpe_ratio = np.sqrt(252) * excess_returns.mean() / excess_returns.std()
        
        # Calculate Sortino Ratio
        downside_returns = returns[returns < 0]
        sortino_ratio = np.sqrt(252) * returns.mean() / downside_returns.std()
        
        return {
            'initial_balance': self.initial_balance,
            'final_balance': self.current_balance,
            'total_return': (self.current_balance - self.initial_balance) / self.initial_balance,
            'max_drawdown': self.daily_metrics['max_drawdown'],
            'sharpe_ratio': sharpe_ratio,
            'sortino_ratio': sortino_ratio,
            'total_trades': len(self.trades),
            'win_rate': len([t for t in self.trades if t['pnl'] > 0]) / len(self.trades),
            'avg_trade_pnl': sum(t['pnl'] for t in self.trades) / len(self.trades)
        } 