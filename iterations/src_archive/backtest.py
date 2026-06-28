"""
Father Daddy Capital - Backtesting Module
---------------------------------------
Unified backtesting system for strategy validation
"""

import asyncio
import argparse
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, List
from pathlib import Path

from src.config.unified_config import UnifiedConfigManager
from src.agents.unified_agent import UnifiedTradingAgent
from src.utils.logger import get_logger

logger = get_logger(__name__)

class BacktestEngine:
    """Unified backtesting engine."""
    
    def __init__(self, config_path: str):
        """Initialize backtest engine.
        
        Args:
            config_path: Path to configuration file
        """
        self.config_manager = UnifiedConfigManager(config_path)
        self.config = self.config_manager.get_trading_config()
        self.agent_config = self.config_manager.get_agent_config()
        
        # Initialize agent
        self.agent = UnifiedTradingAgent(
            strategy_type=self.agent_config.strategy_type,
            asset_class=self.agent_config.asset_class
        )
        
        # Backtest results
        self.results = {}
        self.trades = []
        
        logger.info(f"Backtest engine initialized for {self.agent_config.strategy_type} strategy")
    
    def load_historical_data(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Load historical data for backtesting.
        
        Args:
            symbol: Trading symbol
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            
        Returns:
            DataFrame with OHLCV data
        """
        # This would integrate with actual data providers
        # For now, generate synthetic data for demonstration
        
        dates = pd.date_range(start=start_date, end=end_date, freq='1H')
        n = len(dates)
        
        # Generate realistic price data
        np.random.seed(42)  # For reproducible results
        returns = np.random.normal(0.0001, 0.02, n)  # 0.01% mean return, 2% volatility
        prices = 100 * np.exp(np.cumsum(returns))
        
        # Generate OHLCV data
        data = pd.DataFrame({
            'timestamp': dates,
            'open': prices * (1 + np.random.normal(0, 0.001, n)),
            'high': prices * (1 + np.abs(np.random.normal(0, 0.005, n))),
            'low': prices * (1 - np.abs(np.random.normal(0, 0.005, n))),
            'close': prices,
            'volume': np.random.randint(1000, 10000, n)
        })
        
        data.set_index('timestamp', inplace=True)
        return data
    
    async def run_backtest(self, symbols: List[str], start_date: str, end_date: str) -> Dict[str, Any]:
        """Run backtest for given symbols and date range.
        
        Args:
            symbols: List of trading symbols
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            
        Returns:
            Backtest results
        """
        logger.info(f"Starting backtest for {symbols} from {start_date} to {end_date}")
        
        # Load historical data
        market_data = {}
        for symbol in symbols:
            market_data[symbol] = self.load_historical_data(symbol, start_date, end_date)
        
        # Run backtest simulation
        total_days = (datetime.strptime(end_date, '%Y-%m-%d') - 
                     datetime.strptime(start_date, '%Y-%m-%d')).days
        
        for day in range(total_days):
            current_date = datetime.strptime(start_date, '%Y-%m-%d') + timedelta(days=day)
            
            # Get data up to current date
            current_data = {}
            for symbol, data in market_data.items():
                current_data[symbol] = data[data.index <= current_date]
            
            # Process market data and generate signals
            signals = await self.agent.process_market_data(current_data)
            
            # Execute signals
            if signals:
                trades = await self.agent.execute_signals(signals)
                self.trades.extend(trades)
        
        # Calculate results
        results = self._calculate_results()
        
        logger.info(f"Backtest completed. Total trades: {len(self.trades)}")
        return results
    
    def _calculate_results(self) -> Dict[str, Any]:
        """Calculate backtest performance metrics."""
        if not self.trades:
            return {'error': 'No trades executed'}
        
        # Calculate basic metrics
        total_trades = len(self.trades)
        winning_trades = len([t for t in self.trades if t.pnl and t.pnl > 0])
        losing_trades = len([t for t in self.trades if t.pnl and t.pnl < 0])
        
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        
        # Calculate P&L
        total_pnl = sum(t.pnl for t in self.trades if t.pnl is not None)
        avg_win = np.mean([t.pnl for t in self.trades if t.pnl and t.pnl > 0]) if winning_trades > 0 else 0
        avg_loss = np.mean([t.pnl for t in self.trades if t.pnl and t.pnl < 0]) if losing_trades > 0 else 0
        
        # Calculate Sharpe ratio (simplified)
        returns = [t.pnl for t in self.trades if t.pnl is not None]
        sharpe_ratio = np.mean(returns) / np.std(returns) if len(returns) > 1 and np.std(returns) > 0 else 0
        
        # Portfolio metrics
        portfolio = self.agent.get_portfolio_summary()
        
        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'sharpe_ratio': sharpe_ratio,
            'final_portfolio_value': portfolio['portfolio_value'],
            'max_drawdown': portfolio['drawdown'],
            'strategy': self.agent_config.strategy_type,
            'asset_class': self.agent_config.asset_class
        }
    
    def print_results(self, results: Dict[str, Any]):
        """Print backtest results in a formatted way."""
        print("\n" + "="*60)
        print("BACKTEST RESULTS")
        print("="*60)
        print(f"Strategy: {results.get('strategy', 'N/A')}")
        print(f"Asset Class: {results.get('asset_class', 'N/A')}")
        print(f"Total Trades: {results.get('total_trades', 0)}")
        print(f"Win Rate: {results.get('win_rate', 0):.2%}")
        print(f"Total P&L: ${results.get('total_pnl', 0):.2f}")
        print(f"Average Win: ${results.get('avg_win', 0):.2f}")
        print(f"Average Loss: ${results.get('avg_loss', 0):.2f}")
        print(f"Sharpe Ratio: {results.get('sharpe_ratio', 0):.2f}")
        print(f"Final Portfolio Value: ${results.get('final_portfolio_value', 0):.2f}")
        print(f"Max Drawdown: {results.get('max_drawdown', 0):.2%}")
        print("="*60)

async def main():
    """Main backtest entry point."""
    parser = argparse.ArgumentParser(description='Father Daddy Capital Backtest')
    parser.add_argument('--config', default='config/trading.yaml',
                       help='Configuration file path')
    parser.add_argument('--symbols', nargs='+', default=['BTCUSD'],
                       help='Trading symbols to backtest')
    parser.add_argument('--start-date', default='2023-01-01',
                       help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', default='2023-12-31',
                       help='End date (YYYY-MM-DD)')
    
    args = parser.parse_args()
    
    try:
        # Initialize backtest engine
        engine = BacktestEngine(args.config)
        
        # Run backtest
        results = await engine.run_backtest(
            symbols=args.symbols,
            start_date=args.start_date,
            end_date=args.end_date
        )
        
        # Print results
        engine.print_results(results)
        
    except Exception as e:
        logger.error(f"Backtest failed: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())