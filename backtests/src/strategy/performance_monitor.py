import numpy as np
import pandas as pd
from datetime import datetime
import json
import os

class PerformanceMonitor:
    def __init__(self, log_dir="results/performance_logs"):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        
    def calculate_trade_metrics(self, df):
        """Calculate detailed trade quality metrics."""
        metrics = {}
        
        # Basic performance metrics
        metrics['total_return'] = ((1 + df['returns']).cumprod() - 1).iloc[-1]
        metrics['sharpe_ratio'] = np.sqrt(252) * (df['returns'].mean() / df['returns'].std())
        metrics['max_drawdown'] = (df['equity_curve'] / df['equity_curve'].cummax() - 1).min()
        
        # Trade specific metrics
        long_trades = df[df['position'] > 0]
        short_trades = df[df['position'] < 0]
        
        metrics['num_trades'] = len(long_trades) + len(short_trades)
        metrics['win_rate'] = len(df[df['returns'] > 0]) / metrics['num_trades'] if metrics['num_trades'] > 0 else 0
        
        # Risk metrics
        metrics['avg_trade_duration'] = df['trade_duration'].mean()
        metrics['avg_profit_loss_ratio'] = abs(df[df['returns'] > 0]['returns'].mean() / 
                                             df[df['returns'] < 0]['returns'].mean()) if len(df[df['returns'] < 0]) > 0 else np.inf
        
        # Quality metrics
        if 'ml_score' in df.columns:
            metrics['avg_trade_quality'] = df['ml_score'].mean()
            metrics['quality_win_correlation'] = df['ml_score'].corr(df['returns'] > 0)
        
        return metrics
    
    def calculate_position_metrics(self, df):
        """Calculate position-related metrics."""
        position_metrics = {}
        
        # Position utilization
        position_metrics['avg_position_size'] = df['position_size'].mean()
        position_metrics['max_position_size'] = df['position_size'].max()
        
        # Risk utilization
        position_metrics['avg_risk_per_trade'] = (df['position_size'] * df['atr_pct']).mean()
        position_metrics['max_risk_per_trade'] = (df['position_size'] * df['atr_pct']).max()
        
        # Stop-loss effectiveness
        if 'long_stop_hit' in df.columns and 'short_stop_hit' in df.columns:
            total_stops = len(df[df['long_stop_hit'] | df['short_stop_hit']])
            position_metrics['stop_hit_rate'] = total_stops / len(df) if len(df) > 0 else 0
        
        return position_metrics
    
    def log_performance(self, df, metrics, symbol, timestamp=None):
        """Log detailed performance metrics to file."""
        if timestamp is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Combine all metrics
        log_data = {
            'symbol': symbol,
            'timestamp': timestamp,
            'trade_metrics': metrics,
            'position_metrics': self.calculate_position_metrics(df),
            'daily_returns': df['returns'].resample('D').sum().to_dict(),
            'equity_curve': df['equity_curve'].resample('D').last().to_dict()
        }
        
        # Save to file
        log_file = os.path.join(self.log_dir, f"{symbol}_{timestamp}_performance.json")
        with open(log_file, 'w') as f:
            json.dump(log_data, f, indent=4)
        
        return log_file
    
    def generate_summary_report(self, df, metrics, symbol):
        """Generate a comprehensive performance summary."""
        summary = pd.DataFrame()
        
        # Performance summary
        summary.loc['Total Return', 'Value'] = f"{metrics['total_return']:.2%}"
        summary.loc['Sharpe Ratio', 'Value'] = f"{metrics['sharpe_ratio']:.2f}"
        summary.loc['Max Drawdown', 'Value'] = f"{metrics['max_drawdown']:.2%}"
        summary.loc['Win Rate', 'Value'] = f"{metrics['win_rate']:.2%}"
        summary.loc['Number of Trades', 'Value'] = metrics['num_trades']
        summary.loc['Avg Profit/Loss Ratio', 'Value'] = f"{metrics['avg_profit_loss_ratio']:.2f}"
        
        if 'avg_trade_quality' in metrics:
            summary.loc['Avg Trade Quality', 'Value'] = f"{metrics['avg_trade_quality']:.2f}"
        
        # Risk metrics
        position_metrics = self.calculate_position_metrics(df)
        summary.loc['Avg Position Size', 'Value'] = f"{position_metrics['avg_position_size']:.2f}"
        summary.loc['Max Position Size', 'Value'] = f"{position_metrics['max_position_size']:.2f}"
        summary.loc['Avg Risk per Trade', 'Value'] = f"{position_metrics['avg_risk_per_trade']:.2%}"
        
        if 'stop_hit_rate' in position_metrics:
            summary.loc['Stop Hit Rate', 'Value'] = f"{position_metrics['stop_hit_rate']:.2%}"
        
        return summary 