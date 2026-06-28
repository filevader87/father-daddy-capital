#!/usr/bin/env python3
"""
Performance Optimizations Test Script
-------------------------------------
This script demonstrates all the performance optimizations working together:
1. Async Data Ingestion
2. Vectorized Calculations
3. Parallel Backtests
4. Profiling & Benchmarking
"""

import asyncio
import sys
import os
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

from src.utils.async_data_ingestion import get_async_data_ingestion, DataSource
from src.utils.vectorized_feature_engineering import get_vectorized_feature_engineer
from src.utils.parallel_backtesting import get_parallel_backtester, BacktestConfig
from src.utils.performance_profiler import get_performance_profiler, profile_operation, benchmark_operation
from src.utils.signal_validator import signal_validator

# Set up logging
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def create_sample_market_data(n_rows: int = 1000) -> pd.DataFrame:
    """Create sample market data for testing."""
    np.random.seed(42)
    
    # Generate price data with realistic movements
    returns = np.random.normal(0.0001, 0.02, n_rows)
    prices = 100 * np.exp(np.cumsum(returns))
    
    # Generate OHLCV data
    data = pd.DataFrame({
        'open': prices * (1 + np.random.normal(0, 0.001, n_rows)),
        'high': prices * (1 + np.abs(np.random.normal(0, 0.01, n_rows))),
        'low': prices * (1 - np.abs(np.random.normal(0, 0.01, n_rows))),
        'close': prices,
        'volume': np.random.lognormal(10, 1, n_rows)
    }, index=pd.date_range(start='2023-01-01', periods=n_rows, freq='1H'))
    
    return data

def sample_backtest_function(config: BacktestConfig) -> dict:
    """Sample backtest function for testing parallel backtesting."""
    # Simulate backtest execution
    time.sleep(0.1)  # Simulate computation time
    
    # Generate random results
    total_return = np.random.normal(0.05, 0.1)
    sharpe_ratio = np.random.normal(1.0, 0.5)
    max_drawdown = abs(np.random.normal(0.1, 0.05))
    win_rate = np.random.uniform(0.4, 0.7)
    num_trades = np.random.randint(10, 100)
    
    return {
        'total_return': total_return,
        'sharpe_ratio': sharpe_ratio,
        'max_drawdown': max_drawdown,
        'win_rate': win_rate,
        'num_trades': num_trades,
        'final_value': 100000 * (1 + total_return)
    }

@profile_operation("data_ingestion_test")
async def test_async_data_ingestion():
    """Test async data ingestion performance."""
    print("\n🚀 Testing Async Data Ingestion...")
    
    # Create async data ingestion instance
    config = {
        'alpaca': DataSource(
            name='alpaca',
            base_url='https://paper-api.alpaca.markets',
            api_key='test_key',
            rate_limit=200
        ),
        'defillama': DataSource(
            name='defillama',
            base_url='https://coins.llama.fi',
            rate_limit=100
        ),
        'coingecko': DataSource(
            name='coingecko',
            base_url='https://api.coingecko.com',
            rate_limit=50
        )
    }
    
    async_data_ingestion = await get_async_data_ingestion()
    
    # Test crypto price fetching
    crypto_symbols = ['BTC', 'ETH', 'SOL', 'AVAX']
    start_time = time.time()
    
    try:
        prices = await async_data_ingestion.fetch_crypto_prices_parallel(crypto_symbols)
        execution_time = time.time() - start_time
        
        print(f"  ✅ Fetched {len(prices)} crypto prices in {execution_time:.3f}s")
        print(f"  📊 Prices: {prices}")
        
    except Exception as e:
        print(f"  ❌ Crypto price fetching failed: {e}")
    
    # Test stock data fetching
    stock_symbols = ['AAPL', 'MSFT', 'NVDA']
    start_time = time.time()
    
    try:
        stock_data = await async_data_ingestion.fetch_stock_data_parallel(stock_symbols)
        execution_time = time.time() - start_time
        
        print(f"  ✅ Fetched {len(stock_data)} stock data points in {execution_time:.3f}s")
        
    except Exception as e:
        print(f"  ❌ Stock data fetching failed: {e}")
    
    await async_data_ingestion.close()

@profile_operation("feature_engineering_test")
def test_vectorized_feature_engineering():
    """Test vectorized feature engineering performance."""
    print("\n📊 Testing Vectorized Feature Engineering...")
    
    # Create sample data
    data = create_sample_market_data(1000)
    print(f"  📈 Created sample data with {len(data)} rows")
    
    # Get vectorized feature engineer
    feature_engineer = get_vectorized_feature_engineer()
    
    # Test feature engineering
    start_time = time.time()
    features = feature_engineer.process(data)
    execution_time = time.time() - start_time
    
    print(f"  ✅ Generated {features.shape[1]} features in {execution_time:.3f}s")
    print(f"  📊 Feature shape: {features.shape}")
    print(f"  🔧 Features: {list(features.columns[:10])}...")  # Show first 10 features
    
    return features

@profile_operation("parallel_backtesting_test")
def test_parallel_backtesting():
    """Test parallel backtesting performance."""
    print("\n🔄 Testing Parallel Backtesting...")
    
    # Get parallel backtester
    backtester = get_parallel_backtester()
    
    # Test parameter sweep
    base_config = BacktestConfig(
        symbol='BTCUSD',
        start_date='2023-01-01',
        end_date='2023-12-31',
        initial_capital=100000.0
    )
    
    param_grid = {
        'rsi_period': [14, 21],
        'sma_period': [20, 50],
        'volatility_threshold': [0.1, 0.2]
    }
    
    start_time = time.time()
    results = backtester.run_parameter_sweep(base_config, param_grid, sample_backtest_function)
    execution_time = time.time() - start_time
    
    print(f"  ✅ Completed {len(results)} backtests in {execution_time:.3f}s")
    print(f"  📊 Success rate: {len([r for r in results if r.error is None]) / len(results):.1%}")
    
    # Test multi-symbol backtests
    symbols = ['BTCUSD', 'ETHUSD', 'SOLUSD', 'AVAXUSD']
    strategy_params = {'rsi_period': 14, 'sma_period': 20}
    
    start_time = time.time()
    multi_results = backtester.run_multi_symbol_backtests(
        symbols, '2023-01-01', '2023-12-31', strategy_params, sample_backtest_function
    )
    execution_time = time.time() - start_time
    
    print(f"  ✅ Completed {len(multi_results)} multi-symbol backtests in {execution_time:.3f}s")
    
    # Analyze results
    analysis = backtester.analyze_results(results)
    print(f"  📈 Average Sharpe Ratio: {analysis.get('sharpe_ratio', {}).get('mean', 0):.3f}")
    print(f"  📈 Average Total Return: {analysis.get('total_return', {}).get('mean', 0):.1%}")
    
    return results

@profile_operation("signal_validation_test")
def test_signal_validation():
    """Test signal validation performance."""
    print("\n✅ Testing Signal Validation...")
    
    # Create test signals
    test_signals = [
        {
            'symbol': 'BTCUSD',
            'side': 'buy',
            'quantity': 1.0,
            'price': 50000.0,
            'order_type': 'market',
            'strategy': 'crypto_aets'
        },
        {
            'symbol': 'ETHUSD',
            'side': 'sell',
            'quantity': 10.0,
            'price': 3000.0,
            'order_type': 'limit',
            'strategy': 'crypto_aets'
        },
        {
            'symbol': 'INVALID',
            'side': 'buy',
            'quantity': -100,  # Invalid
            'price': 100.0,
            'order_type': 'market',
            'strategy': 'test_strategy'
        }
    ]
    
    start_time = time.time()
    validation_results = []
    
    for i, signal in enumerate(test_signals):
        result = signal_validator.validate_signal(signal)
        validation_results.append(result)
        print(f"  Signal {i+1}: {signal_validator.get_validation_summary(result)}")
    
    execution_time = time.time() - start_time
    print(f"  ✅ Validated {len(test_signals)} signals in {execution_time:.3f}s")
    
    return validation_results

@benchmark_operation(iterations=50, operation="feature_engineering_benchmark")
def benchmark_feature_engineering():
    """Benchmark feature engineering performance."""
    data = create_sample_market_data(500)
    feature_engineer = get_vectorized_feature_engineer()
    return feature_engineer.process(data)

@benchmark_operation(iterations=20, operation="signal_validation_benchmark")
def benchmark_signal_validation():
    """Benchmark signal validation performance."""
    signal = {
        'symbol': 'BTCUSD',
        'side': 'buy',
        'quantity': 1.0,
        'price': 50000.0,
        'order_type': 'market',
        'strategy': 'crypto_aets'
    }
    return signal_validator.validate_signal(signal)

async def main():
    """Run all performance optimization tests."""
    print("🚀 Starting Performance Optimization Tests")
    print("=" * 50)
    
    # Get performance profiler
    profiler = get_performance_profiler()
    
    try:
        # Test 1: Async Data Ingestion
        await test_async_data_ingestion()
        
        # Test 2: Vectorized Feature Engineering
        features = test_vectorized_feature_engineering()
        
        # Test 3: Parallel Backtesting
        backtest_results = test_parallel_backtesting()
        
        # Test 4: Signal Validation
        validation_results = test_signal_validation()
        
        # Test 5: Benchmarking
        print("\n📊 Running Benchmarks...")
        feature_benchmark = benchmark_feature_engineering()
        validation_benchmark = benchmark_signal_validation()
        
        print(f"  📈 Feature Engineering: {feature_benchmark['mean_time']:.6f}s ± {feature_benchmark['std_time']:.6f}s")
        print(f"  📈 Signal Validation: {validation_benchmark['mean_time']:.6f}s ± {validation_benchmark['std_time']:.6f}s")
        
        # Generate performance summary
        print("\n📋 Performance Summary")
        print("=" * 30)
        summary = profiler.get_performance_summary()
        
        for operation, stats in summary.items():
            if isinstance(stats, dict) and 'mean_time' in stats:
                print(f"  {operation}: {stats['mean_time']:.3f}s (success rate: {stats['success_rate']:.1%})")
        
        # Save results
        results_file = profiler.save_results()
        print(f"\n💾 Performance results saved to: {results_file}")
        
        print("\n✅ All performance optimization tests completed successfully!")
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        raise

if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main()) 