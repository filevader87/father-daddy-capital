import pandas as pd
import json
from datetime import datetime
from strategy.genetic_optimizer import StrategyOptimizer
from strategy.backtest_engine import OptimizedBacktestEngine

def load_data(file_path: str) -> pd.DataFrame:
    """Load and prepare historical market data."""
    data = pd.read_csv(file_path)
    data['timestamp'] = pd.to_datetime(data['timestamp'])
    return data

def save_optimized_parameters(params, fitness_score, output_file: str):
    """Save optimized parameters to a JSON file."""
    result = {
        'optimization_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'fitness_score': float(fitness_score),
        'parameters': {
            'vwap_sensitivity': float(params.vwap_sensitivity),
            'adx_threshold': float(params.adx_threshold),
            'rsi_period': int(params.rsi_period),
            'rsi_overbought': float(params.rsi_overbought),
            'rsi_oversold': float(params.rsi_oversold),
            'atr_multiplier': float(params.atr_multiplier),
            'kelly_fraction': float(params.kelly_fraction),
            'risk_factor': float(params.risk_factor),
            'max_spread_ratio': float(params.max_spread_ratio)
        }
    }
    
    with open(output_file, 'w') as f:
        json.dump(result, f, indent=4)

def main():
    # Configuration
    data_file = "data/historical_data.csv"
    symbol = "BTCUSD"
    output_file = "results/optimized_parameters.json"
    
    # Load historical data
    print("Loading historical data...")
    data = load_data(data_file)
    
    # Initialize optimizer with custom parameters
    optimizer = StrategyOptimizer(
        population_size=50,
        generations=30,
        mutation_rate=0.1,
        crossover_rate=0.7,
        elite_size=5,
        n_jobs=-1  # Use all available CPU cores
    )
    
    # Run optimization
    print("\nStarting genetic optimization...")
    print("This may take a while depending on the data size and number of generations.")
    best_params, best_fitness = optimizer.evolve_strategies(data, symbol)
    
    # Save results
    print("\nOptimization completed!")
    print(f"Best Fitness Score: {best_fitness:.4f}")
    print("\nBest Parameters:")
    for field, value in best_params.__dict__.items():
        print(f"{field}: {value:.6f}" if isinstance(value, float) else f"{field}: {value}")
    
    save_optimized_parameters(best_params, best_fitness, output_file)
    print(f"\nOptimized parameters saved to: {output_file}")
    
    # Run a backtest with optimized parameters
    print("\nRunning backtest with optimized parameters...")
    engine = OptimizedBacktestEngine()
    
    # Configure engine with optimized parameters
    engine.indicators.vwap_sensitivity = best_params.vwap_sensitivity
    engine.indicators.adx_threshold = best_params.adx_threshold
    engine.indicators.rsi_period = best_params.rsi_period
    engine.indicators.rsi_overbought = best_params.rsi_overbought
    engine.indicators.rsi_oversold = best_params.rsi_oversold
    engine.risk_manager.base_atr_multiplier = best_params.atr_multiplier
    engine.position_sizer.kelly_fraction = best_params.kelly_fraction
    engine.position_sizer.risk_factor = best_params.risk_factor
    engine.risk_manager.max_spread_ratio = best_params.max_spread_ratio
    
    # Run and display results
    engine.preload_market_data(data, symbol)
    results = engine.run_backtest(symbol)
    summary = engine.get_performance_summary(results, symbol)
    
    print("\nBacktest Results with Optimized Parameters:")
    print(summary)
    
    # Plot results
    engine.plot_results(results)

if __name__ == "__main__":
    main() 