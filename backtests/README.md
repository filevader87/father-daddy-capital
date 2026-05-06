# Enhanced Trading Strategy Optimizations

This repository contains a set of advanced trading strategy optimizations focusing on signal generation, position sizing, risk management, and performance monitoring.

## Features

### 1. Enhanced Signal Generation
- VWAP-based signals with 0.005 threshold sensitivity
- ADX trend strength indicator with 30 threshold
- RSI with trend confirmation
- Machine learning-based trade quality scoring

### 2. Improved Position Sizing
- ATR-based dynamic position sizing
- Kelly Criterion implementation for optimal sizing
- Volatility-adjusted position scaling
- Quality score-based position adjustments

### 3. Advanced Risk Management
- Dynamic stop-loss calculation using ATR and volatility
- Enhanced trailing stop implementation
- Spread-based trade filtering
- Comprehensive drawdown protection

### 4. Performance Monitoring
- Detailed trade quality metrics
- Enhanced position tracking
- Comprehensive performance logging
- Real-time risk monitoring

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### 1. Signal Generation
```python
from strategy.indicators import EnhancedIndicators

indicators = EnhancedIndicators()
df = indicators.calculate_vwap(df)
df = indicators.calculate_adx(df)
df = indicators.calculate_rsi(df)
df = indicators.calculate_ml_score(df)
```

### 2. Position Sizing
```python
from strategy.position_sizing import PositionSizer

sizer = PositionSizer(risk_factor=0.02, kelly_fraction=0.5)
df = sizer.calculate_position_size(df, capital=100000)
df = sizer.adjust_for_drawdown(df)
```

### 3. Risk Management
```python
from strategy.risk_management import RiskManager

risk_manager = RiskManager(base_atr_multiplier=2.0, max_spread_ratio=0.001)
df = risk_manager.apply_risk_management(df)
```

### 4. Performance Monitoring
```python
from strategy.performance_monitor import PerformanceMonitor

monitor = PerformanceMonitor()
metrics = monitor.calculate_trade_metrics(df)
monitor.log_performance(df, metrics, symbol='BTCUSD')
summary = monitor.generate_summary_report(df, metrics, symbol='BTCUSD')
```

## Configuration

Key parameters can be adjusted in each module:

- `indicators.py`: VWAP sensitivity, ADX threshold, RSI parameters
- `position_sizing.py`: Risk factor, Kelly fraction
- `risk_management.py`: ATR multiplier, spread ratio
- `performance_monitor.py`: Logging directory, reporting formats

## Performance Metrics

The system tracks various performance metrics including:
- Total return and Sharpe ratio
- Win rate and profit/loss ratio
- Position utilization and risk exposure
- Trade quality metrics
- Stop-loss effectiveness

## Contributing

Feel free to submit issues and enhancement requests! 