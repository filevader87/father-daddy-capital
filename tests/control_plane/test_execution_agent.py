import unittest
import numpy as np
from src.control_plane.execution_agent import ExecutionAgent, TradeSignal
import tempfile
import json
import os
from pathlib import Path
import logging

class TestExecutionAgent(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures."""
        # Create temporary log directory
        self.temp_dir = tempfile.mkdtemp()
        self.log_dir = Path(self.temp_dir) / 'logs'
        self.log_dir.mkdir(exist_ok=True)
        
        self.agent = ExecutionAgent(
            strategy_name='test_strategy',
            max_slippage=0.001,
            max_retries=3
        )
        
        # Set initial portfolio state
        self.agent.cash = 100000.0
        self.agent.portfolio_value = 100000.0
        self.agent.peak_value = 100000.0
        
        # Update logger to use test directory
        for handler in self.agent.logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                self.agent.logger.removeHandler(handler)
        
        fh = logging.FileHandler(self.log_dir / 'trading.log')
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter(
            '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}'
        )
        fh.setFormatter(formatter)
        self.agent.logger.addHandler(fh)
        
    def tearDown(self):
        """Clean up test fixtures."""
        # Close file handlers
        for handler in self.agent.logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                self.agent.logger.removeHandler(handler)
                
        # Clean up temporary files
        import shutil
        shutil.rmtree(self.temp_dir)
        
    def test_buy_execution(self):
        """Test buy order execution and monitoring."""
        signal = TradeSignal(
            symbol='AAPL',
            side='buy',
            quantity=100,
            price=150.0,
            order_type='limit',
            strategy='test_strategy'
        )
        
        success = self.agent.execute_signal(signal)
        self.assertTrue(success)
        
        # Check position update
        self.assertIn('AAPL', self.agent.positions)
        self.assertEqual(self.agent.positions['AAPL']['quantity'], 100)
        
        # Check cash update
        self.assertLess(self.agent.cash, 100000.0)
        
    def test_sell_execution(self):
        """Test sell order execution and monitoring."""
        # First buy some shares
        buy_signal = TradeSignal(
            symbol='AAPL',
            side='buy',
            quantity=100,
            price=150.0,
            order_type='limit',
            strategy='test_strategy'
        )
        self.agent.execute_signal(buy_signal)
        
        signal = TradeSignal(
            symbol='AAPL',
            side='sell',
            quantity=50,
            price=160.0,
            order_type='limit',
            strategy='test_strategy'
        )
        
        success = self.agent.execute_signal(signal)
        self.assertTrue(success)
        
        # Check position update
        self.assertEqual(self.agent.positions['AAPL']['quantity'], 50)
        
    def test_execution_retry(self):
        """Test execution retry mechanism."""
        # Create a signal that will fail initially
        signal = TradeSignal(
            symbol='INVALID',
            side='buy',
            quantity=100,
            price=0,  # This should cause an error
            order_type='market',
            strategy='test_strategy'
        )
        
        success = self.agent.execute_signal(signal)
        self.assertFalse(success)
        
    def test_drawdown_monitoring(self):
        """Test drawdown monitoring and alerts."""
        # Simulate a large loss
        buy_signal = TradeSignal(
            symbol='AAPL',
            side='buy',
            quantity=100,
            price=150.0,
            order_type='limit',
            strategy='test_strategy'
        )
        self.agent.execute_signal(buy_signal)
        self.agent.positions['AAPL']['value'] *= 0.85  # 15% loss
        
        # Update portfolio value
        old_value = self.agent.portfolio_value
        self.agent._update_position('AAPL', 0, 127.5)  # 15% lower price
        
        # Check drawdown monitoring
        self.assertLess(self.agent.portfolio_value, old_value)
        self.assertGreater(
            (self.agent.peak_value - self.agent.portfolio_value) / self.agent.peak_value,
            0.1
        )
        
    def test_position_tracking(self):
        """Test position and portfolio value tracking."""
        initial_value = self.agent.portfolio_value
        
        # Execute multiple trades
        buy_aapl = TradeSignal(
            symbol='AAPL',
            side='buy',
            quantity=100,
            price=150.0,
            order_type='limit',
            strategy='test_strategy'
        )
        buy_googl = TradeSignal(
            symbol='GOOGL',
            side='buy',
            quantity=50,
            price=200.0,
            order_type='limit',
            strategy='test_strategy'
        )
        sell_aapl = TradeSignal(
            symbol='AAPL',
            side='sell',
            quantity=50,
            price=160.0,
            order_type='limit',
            strategy='test_strategy'
        )
        
        self.agent.execute_signal(buy_aapl)
        self.agent.execute_signal(buy_googl)
        self.agent.execute_signal(sell_aapl)
        
        # Check position tracking
        self.assertEqual(self.agent.positions['AAPL']['quantity'], 50)
        self.assertEqual(self.agent.positions['GOOGL']['quantity'], 50)
        
        # Check portfolio value calculation
        self.assertNotEqual(self.agent.portfolio_value, initial_value)
        
    def test_slippage_bounds(self):
        """Test that slippage stays within bounds."""
        prices = []
        for _ in range(100):
            signal = TradeSignal(
                symbol='AAPL',
                side='buy',
                quantity=1,
                price=100.0,
                order_type='limit',
                strategy='test_strategy'
            )
            
            self.agent.execute_signal(signal)
            prices.append(self.agent.positions['AAPL']['value'] / 
                        self.agent.positions['AAPL']['quantity'])
            
        # Check slippage bounds
        slippages = [(p - 100.0) / 100.0 for p in prices]
        self.assertTrue(all(s <= self.agent.max_slippage for s in slippages))
        
    def test_error_logging(self):
        """Test error logging functionality."""
        # Attempt an invalid trade
        signal = TradeSignal(
            symbol='INVALID',
            side='buy',
            quantity=-100,  # Invalid quantity
            price=100.0,
            order_type='limit',
            strategy='test_strategy'
        )
        
        success = self.agent.execute_signal(signal)
        self.assertFalse(success)
        
        # Check log file
        log_file = self.log_dir / 'trading.log'
        self.assertTrue(log_file.exists())
        
        # Check log content
        with open(log_file, 'r') as f:
            log_lines = [line.strip() for line in f if line.strip()]
            
        # Check that we have at least one error log
        error_logs = [line for line in log_lines if 'ERROR' in line]
        self.assertTrue(len(error_logs) > 0)

if __name__ == '__main__':
    unittest.main() 