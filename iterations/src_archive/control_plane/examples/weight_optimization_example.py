import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import seaborn as sns
from ta.trend import ADXIndicator
from ta.momentum import RSIIndicator
from ta.trend import MACD
from ta.volume import VolumeWeightedAveragePrice

from ..weight_optimizer import WeightOptimizer, IndicatorWeights

def fetch_market_data(symbol: str = 'SPY', lookback_days: int = 252) -> pd.DataFrame:
    """
    Fetch market data and calculate technical indicators.
    
    Args:
        symbol: Stock symbol to fetch
        lookback_days: Number of days of historical data
        
    Returns:
        DataFrame with price data and indicators
    """
    # Fetch data
    end_date = datetime.now()
    start_date = end_date - timedelta(days=lookback_days + 50)  # Extra days for indicator calculation
    
    data = yf.download(symbol, start=start_date, end=end_date)
    
    # Calculate indicators
    # ADX
    adx = ADXIndicator(data['High'], data['Low'], data['Close'])
    data['adx'] = adx.adx()
    
    # RSI
    rsi = RSIIndicator(data['Close'])
    data['rsi'] = rsi.rsi()
    
    # MACD
    macd = MACD(data['Close'])
    data['macd'] = macd.macd()
    data['macd_signal'] = macd.macd_signal()
    
    # VWAP
    vwap = VolumeWeightedAveragePrice(
        high=data['High'],
        low=data['Low'],
        close=data['Close'],
        volume=data['Volume']
    )
    data['vwap'] = vwap.volume_weighted_average_price()
    
    # Drop any NaN values
    data = data.dropna()
    
    return data

def plot_optimization_results(data: pd.DataFrame,
                            weights: IndicatorWeights,
                            symbol: str):
    """
    Plot the optimization results.
    
    Args:
        data: Market data with indicators
        weights: Optimized indicator weights
        symbol: Stock symbol
    """
    # Create figure with subplots
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[2, 1])
    
    # Plot price and indicators
    ax1.plot(data.index, data['Close'], label='Price', color='black', alpha=0.7)
    ax1.plot(data.index, data['vwap'], label='VWAP', color='blue', alpha=0.5)
    
    # Plot combined signal
    vwap_signal = (data['Close'] > data['vwap']).astype(float)
    adx_signal = (data['adx'] > 25).astype(float)
    rsi_signal = ((data['rsi'] < 30) | (data['rsi'] > 70)).astype(float)
    macd_signal = (data['macd'] > data['macd_signal']).astype(float)
    
    combined_signal = (
        weights.vwap_weight * vwap_signal +
        weights.adx_weight * adx_signal +
        weights.rsi_weight * rsi_signal +
        weights.macd_weight * macd_signal
    )
    
    # Normalize signal to price scale for visualization
    signal_scale = (data['Close'].max() - data['Close'].min()) * 0.1
    scaled_signal = (combined_signal * signal_scale) + data['Close'].min()
    
    ax1.plot(data.index, scaled_signal, label='Trading Signal', color='red', alpha=0.5)
    ax1.set_title(f'{symbol} Price and Signals')
    ax1.legend()
    ax1.grid(True)
    
    # Plot indicator weights
    weights_data = {
        'VWAP': weights.vwap_weight,
        'ADX': weights.adx_weight,
        'RSI': weights.rsi_weight,
        'MACD': weights.macd_weight
    }
    
    colors = sns.color_palette('husl', n_colors=4)
    ax2.bar(weights_data.keys(), weights_data.values(), color=colors)
    ax2.set_title('Optimized Indicator Weights')
    ax2.set_ylim(0, 1)
    ax2.grid(True)
    
    plt.tight_layout()
    plt.show()

def main():
    # Parameters
    symbol = 'SPY'
    lookback_days = 252  # One year of trading data
    
    print(f"\nFetching market data for {symbol}...")
    data = fetch_market_data(symbol, lookback_days)
    
    print("\nInitializing weight optimizer...")
    optimizer = WeightOptimizer(
        population_size=100,
        generations=50,
        mutation_rate=0.1,
        crossover_rate=0.8,
        tournament_size=5,
        elite_size=2
    )
    
    print("\nOptimizing indicator weights...")
    best_weights = optimizer.optimize(data)
    
    print("\nOptimization Results:")
    print("=" * 50)
    print(f"VWAP Weight: {best_weights.vwap_weight:.3f}")
    print(f"ADX Weight: {best_weights.adx_weight:.3f}")
    print(f"RSI Weight: {best_weights.rsi_weight:.3f}")
    print(f"MACD Weight: {best_weights.macd_weight:.3f}")
    print(f"Fitness Score (Sharpe): {best_weights.fitness:.3f}")
    
    print("\nPlotting results...")
    plot_optimization_results(data, best_weights, symbol)

if __name__ == '__main__':
    main() 