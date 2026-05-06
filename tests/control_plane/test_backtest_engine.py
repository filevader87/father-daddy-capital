import unittest
import numpy as np
import pandas as pd
from src.control_plane.backtest_engine import VectorizedBacktester, BacktestResults
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

class TestVectorizedBacktester(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures."""
        self.backtester = VectorizedBacktester(
            initial_capital=100000.0,
            commission=0.001,
            risk_free_rate=0.02,
            max_position_size=1.0
        )
        
        # Create sample market data with realistic price movements
        dates = pd.date_range(start='2023-01-01', periods=252, freq='D')
        
        # Generate random walk for prices
        np.random.seed(42)  # For reproducibility
        returns = np.random.normal(0.0001, 0.02, 252)  # Daily returns with slight upward bias
        prices = 100 * np.exp(np.cumsum(returns))  # Log-normal price process
        
        self.market_data = pd.DataFrame({
            'open': prices * (1 + np.random.normal(0, 0.001, 252)),
            'high': prices * (1 + np.abs(np.random.normal(0, 0.01, 252))),
            'low': prices * (1 - np.abs(np.random.normal(0, 0.01, 252))),
            'close': prices,
            'volume': np.random.lognormal(10, 1, 252),
            'vwap': prices * (1 + np.random.normal(0, 0.001, 252)),
            'adx': np.random.uniform(10, 50, 252),
            'rsi': np.random.uniform(30, 70, 252),
            'macd': np.random.normal(0, 1, 252),
            'macd_signal': np.random.normal(0, 1, 252)
        }, index=dates)
        
        # Sample indicator weights
        self.weights = {
            'vwap': 0.25,
            'adx': 0.25,
            'rsi': 0.25,
            'macd': 0.25
        }
        
    def test_signal_calculation(self):
        """Test signal calculation performance and correctness."""
        start_time = time.time()
        signals = self.backtester._calculate_signals(self.market_data, self.weights)
        calculation_time = time.time() - start_time
        
        self.assertLess(calculation_time, 0.1)  # Should be fast
        self.assertEqual(len(signals), len(self.market_data))
        self.assertTrue(np.all(signals >= -1))
        self.assertTrue(np.all(signals <= 1))
        
    def test_position_calculation(self):
        """Test position size calculation."""
        signals = np.random.random(252) * 2 - 1
        volatility = np.ones(252) * 0.2
        
        positions = self.backtester._calculate_position_sizes(signals, volatility)
        
        self.assertEqual(len(positions), len(signals))
        self.assertTrue(np.all(positions >= -self.backtester.max_position_size))
        self.assertTrue(np.all(positions <= self.backtester.max_position_size))
        
    def test_trade_calculation(self):
        """Test trade calculation and cost computation."""
        positions = np.random.random(252) * 2 - 1
        prices = self.market_data['close'].values
        
        trades, costs = self.backtester._calculate_trades(positions, prices)
        
        self.assertIsInstance(trades, pd.DataFrame)
        self.assertTrue(len(trades) > 0)
        self.assertEqual(len(costs), len(positions))
        self.assertTrue(np.all(costs >= 0))
        
    def test_return_calculation(self):
        """Test return calculation including costs."""
        positions = np.random.random(252) * 2 - 1
        returns = np.random.random(252) * 0.02 - 0.01
        costs = np.random.random(252) * 0.001
        
        strategy_returns = self.backtester._calculate_returns(
            positions,
            returns,
            costs
        )
        
        self.assertEqual(len(strategy_returns), len(returns) - 2)  # Two elements lost due to alignment
        
    def test_metric_calculation(self):
        """Test portfolio metric calculations."""
        returns = np.random.random(251) * 0.02 - 0.01
        
        sharpe, max_drawdown, total_return = self.backtester._calculate_portfolio_metrics(
            returns
        )
        
        self.assertIsInstance(sharpe, float)
        self.assertIsInstance(max_drawdown, float)
        self.assertIsInstance(total_return, float)
        self.assertTrue(max_drawdown <= 0)
        
    def test_full_backtest(self):
        """Test full backtest execution and performance."""
        start_time = time.time()
        results = self.backtester.run_backtest(self.market_data, self.weights)
        execution_time = time.time() - start_time
        
        print(f"Max drawdown: {results.max_drawdown}")
        print(f"Portfolio values: {results.portfolio_values}")
        print(f"Returns: {results.returns}")
        
        self.assertLess(execution_time, 1.0)  # Should complete in under 1 second
        self.assertIsInstance(results, BacktestResults)
        self.assertEqual(len(results.portfolio_values), len(self.market_data))
        self.assertTrue(results.sharpe_ratio != 0)
        self.assertTrue(results.max_drawdown <= 0)
        self.assertTrue(isinstance(results.win_rate, float))
        
    def test_parallel_execution(self):
        """Test parallel backtesting with multiple parameter sets."""
        weight_sets = [
            {'vwap': w, 'adx': 1-w, 'rsi': 0, 'macd': 0}
            for w in np.linspace(0, 1, 10)
        ]
        
        start_time = time.time()
        with ThreadPoolExecutor() as executor:
            backtest_fn = partial(self.backtester.run_backtest, self.market_data)
            results = list(executor.map(backtest_fn, weight_sets))
        execution_time = time.time() - start_time
        
        self.assertLess(execution_time, 2.0)  # Should be fast even with multiple runs
        self.assertEqual(len(results), len(weight_sets))
        self.assertTrue(all(isinstance(r, BacktestResults) for r in results))

if __name__ == '__main__':
    unittest.main() 