"""
Parallel Backtesting Module
---------------------------
This module provides parallel backtesting capabilities using joblib and concurrent.futures
to run parameter sweeps or multiple symbols concurrently for maximum performance.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Any, Tuple, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from joblib import Parallel, delayed
import multiprocessing as mp
from functools import partial
import time
import os

logger = logging.getLogger(__name__)

@dataclass
class BacktestConfig:
    """Configuration for a single backtest."""
    symbol: str
    start_date: str
    end_date: str
    initial_capital: float = 100000.0
    commission: float = 0.001
    strategy_params: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.strategy_params is None:
            self.strategy_params = {}

@dataclass
class BacktestResult:
    """Result of a single backtest."""
    symbol: str
    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    num_trades: int
    final_value: float
    execution_time: float
    strategy_params: Dict[str, Any]
    error: Optional[str] = None

class ParallelBacktester:
    """High-performance parallel backtesting engine."""
    
    def __init__(self, 
                 max_workers: Optional[int] = None,
                 use_processes: bool = True,
                 chunk_size: int = 10):
        """Initialize parallel backtester.
        
        Args:
            max_workers: Maximum number of workers (defaults to CPU count)
            use_processes: Whether to use processes (True) or threads (False)
            chunk_size: Size of chunks for joblib parallel processing
        """
        self.max_workers = max_workers or mp.cpu_count()
        self.use_processes = use_processes
        self.chunk_size = chunk_size
        self.executor_class = ProcessPoolExecutor if use_processes else ThreadPoolExecutor
        
        logger.info(f"Initialized ParallelBacktester with {self.max_workers} workers")
    
    def run_parameter_sweep(self,
                           base_config: BacktestConfig,
                           param_grid: Dict[str, List[Any]],
                           backtest_function: Callable) -> List[BacktestResult]:
        """Run parameter sweep using parallel processing.
        
        Args:
            base_config: Base configuration for backtests
            param_grid: Dictionary of parameter names to lists of values
            backtest_function: Function to run individual backtests
            
        Returns:
            List of BacktestResult objects
        """
        # Generate all parameter combinations
        param_combinations = self._generate_param_combinations(param_grid)
        
        logger.info(f"Running parameter sweep with {len(param_combinations)} combinations")
        
        # Create configurations for each parameter combination
        configs = []
        for params in param_combinations:
            config = BacktestConfig(
                symbol=base_config.symbol,
                start_date=base_config.start_date,
                end_date=base_config.end_date,
                initial_capital=base_config.initial_capital,
                commission=base_config.commission,
                strategy_params=params
            )
            configs.append(config)
        
        # Run backtests in parallel
        return self._run_backtests_parallel(configs, backtest_function)
    
    def run_multi_symbol_backtests(self,
                                  symbols: List[str],
                                  start_date: str,
                                  end_date: str,
                                  strategy_params: Dict[str, Any],
                                  backtest_function: Callable) -> List[BacktestResult]:
        """Run backtests for multiple symbols in parallel.
        
        Args:
            symbols: List of symbols to backtest
            start_date: Start date for backtests
            end_date: End date for backtests
            strategy_params: Strategy parameters
            backtest_function: Function to run individual backtests
            
        Returns:
            List of BacktestResult objects
        """
        # Create configurations for each symbol
        configs = []
        for symbol in symbols:
            config = BacktestConfig(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                strategy_params=strategy_params
            )
            configs.append(config)
        
        logger.info(f"Running multi-symbol backtests for {len(symbols)} symbols")
        
        # Run backtests in parallel
        return self._run_backtests_parallel(configs, backtest_function)
    
    def run_optimization_grid(self,
                             base_config: BacktestConfig,
                             param_ranges: Dict[str, Tuple[float, float, int]],
                             backtest_function: Callable,
                             optimization_metric: str = 'sharpe_ratio') -> Tuple[Dict[str, Any], BacktestResult]:
        """Run grid search optimization using parallel processing.
        
        Args:
            base_config: Base configuration for backtests
            param_ranges: Dictionary of parameter names to (min, max, steps)
            backtest_function: Function to run individual backtests
            optimization_metric: Metric to optimize ('sharpe_ratio', 'total_return', etc.)
            
        Returns:
            Tuple of (best_params, best_result)
        """
        # Generate parameter grid
        param_grid = {}
        for param_name, (min_val, max_val, steps) in param_ranges.items():
            param_grid[param_name] = np.linspace(min_val, max_val, steps).tolist()
        
        # Run parameter sweep
        results = self.run_parameter_sweep(base_config, param_grid, backtest_function)
        
        # Find best result
        if not results:
            raise ValueError("No valid backtest results found")
        
        # Filter out failed results
        valid_results = [r for r in results if r.error is None]
        if not valid_results:
            raise ValueError("No successful backtest results found")
        
        # Find best result based on optimization metric
        best_result = max(valid_results, key=lambda x: getattr(x, optimization_metric))
        best_params = best_result.strategy_params
        
        logger.info(f"Best {optimization_metric}: {getattr(best_result, optimization_metric):.4f}")
        logger.info(f"Best parameters: {best_params}")
        
        return best_params, best_result
    
    def run_monte_carlo_optimization(self,
                                   base_config: BacktestConfig,
                                   param_ranges: Dict[str, Tuple[float, float]],
                                   n_trials: int,
                                   backtest_function: Callable,
                                   optimization_metric: str = 'sharpe_ratio') -> Tuple[Dict[str, Any], BacktestResult]:
        """Run Monte Carlo optimization using parallel processing.
        
        Args:
            base_config: Base configuration for backtests
            param_ranges: Dictionary of parameter names to (min, max)
            n_trials: Number of random trials
            backtest_function: Function to run individual backtests
            optimization_metric: Metric to optimize
            
        Returns:
            Tuple of (best_params, best_result)
        """
        # Generate random parameter combinations
        param_combinations = []
        for _ in range(n_trials):
            params = {}
            for param_name, (min_val, max_val) in param_ranges.items():
                params[param_name] = np.random.uniform(min_val, max_val)
            param_combinations.append(params)
        
        # Create configurations
        configs = []
        for params in param_combinations:
            config = BacktestConfig(
                symbol=base_config.symbol,
                start_date=base_config.start_date,
                end_date=base_config.end_date,
                initial_capital=base_config.initial_capital,
                commission=base_config.commission,
                strategy_params=params
            )
            configs.append(config)
        
        logger.info(f"Running Monte Carlo optimization with {n_trials} trials")
        
        # Run backtests in parallel
        results = self._run_backtests_parallel(configs, backtest_function)
        
        # Find best result
        valid_results = [r for r in results if r.error is None]
        if not valid_results:
            raise ValueError("No successful backtest results found")
        
        best_result = max(valid_results, key=lambda x: getattr(x, optimization_metric))
        best_params = best_result.strategy_params
        
        logger.info(f"Best {optimization_metric}: {getattr(best_result, optimization_metric):.4f}")
        logger.info(f"Best parameters: {best_params}")
        
        return best_params, best_result
    
    def _run_backtests_parallel(self, 
                               configs: List[BacktestConfig], 
                               backtest_function: Callable) -> List[BacktestResult]:
        """Run backtests in parallel using the appropriate executor."""
        start_time = time.time()
        
        if self.use_processes:
            # Use joblib for process-based parallelization (better for CPU-intensive tasks)
            results = Parallel(n_jobs=self.max_workers, batch_size=self.chunk_size)(
                delayed(self._run_single_backtest)(config, backtest_function) 
                for config in configs
            )
        else:
            # Use ThreadPoolExecutor for thread-based parallelization (better for I/O-bound tasks)
            with self.executor_class(max_workers=self.max_workers) as executor:
                future_to_config = {
                    executor.submit(self._run_single_backtest, config, backtest_function): config 
                    for config in configs
                }
                
                results = []
                for future in as_completed(future_to_config):
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        config = future_to_config[future]
                        logger.error(f"Backtest failed for {config.symbol}: {e}")
                        results.append(BacktestResult(
                            symbol=config.symbol,
                            total_return=0.0,
                            sharpe_ratio=0.0,
                            max_drawdown=0.0,
                            win_rate=0.0,
                            num_trades=0,
                            final_value=config.initial_capital,
                            execution_time=0.0,
                            strategy_params=config.strategy_params,
                            error=str(e)
                        ))
        
        total_time = time.time() - start_time
        logger.info(f"Completed {len(configs)} backtests in {total_time:.2f} seconds")
        
        return results
    
    def _run_single_backtest(self, config: BacktestConfig, backtest_function: Callable) -> BacktestResult:
        """Run a single backtest with error handling."""
        start_time = time.time()
        
        try:
            # Run the backtest
            result = backtest_function(config)
            
            # Calculate execution time
            execution_time = time.time() - start_time
            
            # Create BacktestResult
            return BacktestResult(
                symbol=config.symbol,
                total_return=result.get('total_return', 0.0),
                sharpe_ratio=result.get('sharpe_ratio', 0.0),
                max_drawdown=result.get('max_drawdown', 0.0),
                win_rate=result.get('win_rate', 0.0),
                num_trades=result.get('num_trades', 0),
                final_value=result.get('final_value', config.initial_capital),
                execution_time=execution_time,
                strategy_params=config.strategy_params
            )
            
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"Backtest failed for {config.symbol}: {e}")
            
            return BacktestResult(
                symbol=config.symbol,
                total_return=0.0,
                sharpe_ratio=0.0,
                max_drawdown=0.0,
                win_rate=0.0,
                num_trades=0,
                final_value=config.initial_capital,
                execution_time=execution_time,
                strategy_params=config.strategy_params,
                error=str(e)
            )
    
    def _generate_param_combinations(self, param_grid: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
        """Generate all combinations of parameters."""
        import itertools
        
        # Get all parameter names and their values
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        
        # Generate all combinations
        combinations = list(itertools.product(*param_values))
        
        # Convert to list of dictionaries
        result = []
        for combo in combinations:
            param_dict = dict(zip(param_names, combo))
            result.append(param_dict)
        
        return result
    
    def analyze_results(self, results: List[BacktestResult]) -> Dict[str, Any]:
        """Analyze backtest results and generate statistics."""
        if not results:
            return {}
        
        # Filter out failed results
        valid_results = [r for r in results if r.error is None]
        failed_results = [r for r in results if r.error is not None]
        
        if not valid_results:
            return {
                'total_tests': len(results),
                'successful_tests': 0,
                'failed_tests': len(failed_results),
                'error': 'No successful backtests'
            }
        
        # Calculate statistics
        total_returns = [r.total_return for r in valid_results]
        sharpe_ratios = [r.sharpe_ratio for r in valid_results]
        max_drawdowns = [r.max_drawdown for r in valid_results]
        win_rates = [r.win_rate for r in valid_results]
        execution_times = [r.execution_time for r in valid_results]
        
        analysis = {
            'total_tests': len(results),
            'successful_tests': len(valid_results),
            'failed_tests': len(failed_results),
            'success_rate': len(valid_results) / len(results),
            
            # Performance statistics
            'total_return': {
                'mean': np.mean(total_returns),
                'std': np.std(total_returns),
                'min': np.min(total_returns),
                'max': np.max(total_returns),
                'median': np.median(total_returns)
            },
            'sharpe_ratio': {
                'mean': np.mean(sharpe_ratios),
                'std': np.std(sharpe_ratios),
                'min': np.min(sharpe_ratios),
                'max': np.max(sharpe_ratios),
                'median': np.median(sharpe_ratios)
            },
            'max_drawdown': {
                'mean': np.mean(max_drawdowns),
                'std': np.std(max_drawdowns),
                'min': np.min(max_drawdowns),
                'max': np.max(max_drawdowns),
                'median': np.median(max_drawdowns)
            },
            'win_rate': {
                'mean': np.mean(win_rates),
                'std': np.std(win_rates),
                'min': np.min(win_rates),
                'max': np.max(win_rates),
                'median': np.median(win_rates)
            },
            'execution_time': {
                'mean': np.mean(execution_times),
                'std': np.std(execution_times),
                'min': np.min(execution_times),
                'max': np.max(execution_times),
                'median': np.median(execution_times)
            }
        }
        
        return analysis

# Global instance
_parallel_backtester = None

def get_parallel_backtester() -> ParallelBacktester:
    """Get or create global parallel backtester instance."""
    global _parallel_backtester
    
    if _parallel_backtester is None:
        _parallel_backtester = ParallelBacktester()
    
    return _parallel_backtester 