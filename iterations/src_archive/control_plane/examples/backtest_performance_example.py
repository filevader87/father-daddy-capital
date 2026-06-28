import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import seaborn as sns
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import time

from ..backtest_engine import VectorizedBacktester
from ..weight_optimizer import WeightOptimizer

def fetch_market_data(symbol: str = 'SPY', lookback_days: int = 252) -> pd.DataFrame:
    """Fetch market data with indicators."""
    # Fetch data
    end_date = datetime.now()
    start_date = end_date - timedelta(days=lookback_days + 50)
    
    data = yf.download(symbol, start=start_date, end=end_date)
    
    # Calculate indicators (vectorized)
    # VWAP
    data['vwap'] = (data['High'] + data['Low'] + data['Close']) / 3
    
    # ADX (simplified for example)
    high_low = data['High'] - data['Low']
    high_close = np.abs(data['High'] - data['Close'].shift())
    low_close = np.abs(data['Low'] - data['Close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    data['adx'] = atr / data['Close'] * 100
    
    # RSI
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    data['rsi'] = 100 - (100 / (1 + rs))
    
    # MACD
    exp1 = data['Close'].ewm(span=12, adjust=False).mean()
    exp2 = data['Close'].ewm(span=26, adjust=False).mean()
    data['macd'] = exp1 - exp2
    data['macd_signal'] = data['macd'].ewm(span=9, adjust=False).mean()
    
    return data.dropna()

def run_performance_comparison(data: pd.DataFrame,
                             n_iterations: int = 100):
    """Compare performance between different backtesting approaches."""
    backtester = VectorizedBacktester()
    
    # Generate random weight sets
    weight_sets = []
    for _ in range(n_iterations):
        weights = np.random.random(4)
        weights = weights / np.sum(weights)
        weight_sets.append({
            'vwap': weights[0],
            'adx': weights[1],
            'rsi': weights[2],
            'macd': weights[3]
        })
    
    # Sequential execution
    start_time = time.time()
    sequential_results = []
    for weights in weight_sets:
        result = backtester.run_backtest(data, weights)
        sequential_results.append(result)
    sequential_time = time.time() - start_time
    
    # Parallel execution
    start_time = time.time()
    with ThreadPoolExecutor() as executor:
        backtest_fn = partial(backtester.run_backtest, data)
        parallel_results = list(executor.map(backtest_fn, weight_sets))
    parallel_time = time.time() - start_time
    
    return sequential_time, parallel_time, sequential_results, parallel_results

def plot_performance_comparison(sequential_time: float,
                              parallel_time: float,
                              sequential_results: list,
                              parallel_results: list):
    """Plot performance comparison results."""
    # Create figure with subplots
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10))
    
    # Plot execution times
    times = [sequential_time, parallel_time]
    labels = ['Sequential', 'Parallel']
    ax1.bar(labels, times, color=['blue', 'green'])
    ax1.set_title('Execution Time Comparison')
    ax1.set_ylabel('Time (seconds)')
    for i, v in enumerate(times):
        ax1.text(i, v, f'{v:.2f}s', ha='center', va='bottom')
    
    # Plot performance metrics
    seq_sharpe = [r.sharpe_ratio for r in sequential_results]
    par_sharpe = [r.sharpe_ratio for r in parallel_results]
    
    sns.boxplot(data=[seq_sharpe, par_sharpe], ax=ax2)
    ax2.set_xticklabels(['Sequential', 'Parallel'])
    ax2.set_title('Sharpe Ratio Distribution')
    ax2.set_ylabel('Sharpe Ratio')
    
    plt.tight_layout()
    plt.show()

def main():
    # Parameters
    symbol = 'SPY'
    lookback_days = 252
    n_iterations = 100
    
    print(f"\nFetching market data for {symbol}...")
    data = fetch_market_data(symbol, lookback_days)
    
    print(f"\nRunning performance comparison with {n_iterations} iterations...")
    sequential_time, parallel_time, sequential_results, parallel_results = (
        run_performance_comparison(data, n_iterations)
    )
    
    print("\nPerformance Results:")
    print("=" * 50)
    print(f"Sequential Execution Time: {sequential_time:.2f} seconds")
    print(f"Parallel Execution Time: {parallel_time:.2f} seconds")
    print(f"Speedup Factor: {sequential_time/parallel_time:.2f}x")
    
    print("\nPlotting results...")
    plot_performance_comparison(
        sequential_time,
        parallel_time,
        sequential_results,
        parallel_results
    )

if __name__ == '__main__':
    main() 