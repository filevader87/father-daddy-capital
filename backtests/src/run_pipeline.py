import os
import json
import pandas as pd
import numpy as np
from datetime import datetime
from data_pipeline.preprocess_data import DataPreprocessor
from data_pipeline.backtest_integration import BacktestDataIntegrator

def load_config(config_path):
    """Load configuration using centralized config loader."""
    try:
        from src.config.loader import ConfigLoader
        loader = ConfigLoader()
        return loader.load_all()
    except Exception as e:
        print(f"Failed to load config: {str(e)}")
        raise

def run_backtest(config):
    """Run backtest for all configured assets."""
    preprocessor = DataPreprocessor()
    integrator = BacktestDataIntegrator(preprocessor)
    
    results = {}
    
    # Process crypto assets
    for symbol in config['assets']['crypto']:
        print(f"\nRunning backtest for {symbol}...")
        try:
            result = integrator.run_backtest(
                asset_type="crypto",
                symbol=symbol.replace('/', '_'),
                start_date=config['date_range']['start'],
                end_date=config['date_range']['end'],
                initial_capital=config['initial_capital']
            )
            if result is not None:
                results[symbol] = result
                print(f"Backtest completed for {symbol}")
                print(f"Total Return: {result['total_return']:.2%}")
                print(f"Win Rate: {result['win_rate']:.2%}")
                print(f"Max Drawdown: {result['max_drawdown']:.2%}")
                print(f"Sharpe Ratio: {result['sharpe_ratio']:.2f}")
                print(f"Number of Trades: {result['num_trades']}")
        except Exception as e:
            print(f"Error running backtest for {symbol}: {str(e)}")
    
    # Process stock assets
    for symbol in config['assets']['stocks']:
        print(f"\nRunning backtest for {symbol}...")
        try:
            result = integrator.run_backtest(
                asset_type="stocks",
                symbol=symbol,
                start_date=config['date_range']['start'],
                end_date=config['date_range']['end'],
                initial_capital=config['initial_capital']
            )
            if result is not None:
                results[symbol] = result
                print(f"Backtest completed for {symbol}")
                print(f"Total Return: {result['total_return']:.2%}")
                print(f"Win Rate: {result['win_rate']:.2%}")
                print(f"Max Drawdown: {result['max_drawdown']:.2%}")
                print(f"Sharpe Ratio: {result['sharpe_ratio']:.2f}")
                print(f"Number of Trades: {result['num_trades']}")
        except Exception as e:
            print(f"Error running backtest for {symbol}: {str(e)}")
    
    return results

def generate_summary_report(results, output_path):
    """Generate a summary report of all backtest results."""
    if not results:
        print("\nNo valid backtest results to generate report.")
        return
        
    summary_data = []
    
    for symbol, result in results.items():
        summary_data.append({
            'Symbol': symbol,
            'Total Return': result['total_return'],
            'Win Rate': result['win_rate'],
            'Max Drawdown': result['max_drawdown'],
            'Sharpe Ratio': result['sharpe_ratio'],
            'Number of Trades': result['num_trades']
        })
    
    df = pd.DataFrame(summary_data)
    df.to_csv(output_path, index=False)
    print(f"\nSummary report saved to {output_path}")

def main():
    """
    Main entry point for backtesting pipeline.
    
    ENVIRONMENT: BACKTESTING
    USAGE: python backtests/src/run_pipeline.py
    PURPOSE: Run historical backtests to validate strategies
    FEATURES: Historical data analysis, strategy validation, performance metrics
    """
    # Load configuration
    config_path = "backtest_pipeline/config.json"
    config = load_config(config_path)
    
    # Create results directory if it doesn't exist
    os.makedirs("results", exist_ok=True)
    
    # Run backtest
    print("Starting backtest simulation...")
    results = run_backtest(config)
    
    # Generate summary report
    output_path = "results/summary_report.csv"
    generate_summary_report(results, output_path)
    
    print("\nBacktest simulation completed!")

if __name__ == "__main__":
    main() 