import pytest
import pandas as pd
import numpy as np
from src.agents.trading.momentum_agent import MomentumAgent
from src.core.regime_tracker import MarketRegime

@pytest.fixture
def mock_data():
    # Generate mock price data
    dates = pd.date_range(start='2023-01-01', periods=100, freq='H')
    prices = np.random.normal(100, 5, 100).cumsum()
    volumes = np.random.normal(1000, 200, 100)
    
    # Generate high and low prices around close prices
    high_prices = prices + np.abs(np.random.normal(0, 2, 100))
    low_prices = prices - np.abs(np.random.normal(0, 2, 100))
    
    return pd.DataFrame({
        'close': prices,
        'high': high_prices,
        'low': low_prices,
        'volume': volumes
    }, index=dates)

@pytest.fixture
def momentum_agent():
    config = {
        'timeframes': ['1h', '4h', '1d'],
        'momentum_weights': {'1h': 0.3, '4h': 0.4, '1d': 0.3},
        'regime_weights': {
            'trending': 1.0,
            'high_volatility': 0.5,
            'high_volume': 0.8,
            'neutral': 0.6
        }
    }
    return MomentumAgent(config)

def test_initialization(momentum_agent):
    assert momentum_agent.timeframes == ["1h", "4h", "1d"]
    assert momentum_agent.momentum_weights["1h"] == 0.3
    assert momentum_agent.regime_weights["trending"] == 1.0

def test_calculate_signal(momentum_agent, mock_data):
    signal = momentum_agent.calculate_signal(mock_data, MarketRegime.TRENDING)
    assert isinstance(signal, float)
    assert -1 <= signal <= 1

def test_calculate_timeframe_signal(momentum_agent, mock_data):
    signal = momentum_agent._calculate_timeframe_signal(mock_data, timeframe='1h')
    assert isinstance(signal, float)
    assert -1 <= signal <= 1

def test_calculate_trend_strength(momentum_agent, mock_data):
    strength = momentum_agent._calculate_trend_strength(mock_data)
    assert isinstance(strength, float)
    assert 0 <= strength <= 1

def test_calculate_momentum_score(momentum_agent, mock_data):
    score = momentum_agent._calculate_momentum_score(mock_data)
    assert isinstance(score, float)
    assert -1 <= score <= 1

def test_get_trading_insights(momentum_agent, mock_data):
    insights = momentum_agent.get_trading_insights(mock_data)
    assert isinstance(insights, dict)
    assert 'position_analysis' in insights
    assert 'risk_factors' in insights
    assert 'performance_metrics' in insights

def test_calculate_position_risk(momentum_agent, mock_data):
    risk = momentum_agent._calculate_position_risk(mock_data)
    assert isinstance(risk, float)
    assert 0 <= risk <= 1

def test_signal_adaptation(momentum_agent, mock_data):
    # Test signal adaptation for different market regimes
    regimes = [
        MarketRegime.TRENDING,
        MarketRegime.HIGH_VOLATILITY,
        MarketRegime.HIGH_VOLUME,
        MarketRegime.NEUTRAL
    ]
    
    signals = []
    for regime in regimes:
        signal = momentum_agent.calculate_signal(mock_data, regime)
        signals.append(signal)
    
    # Signals should be different for different regimes
    assert len(set(signals)) > 1

def test_risk_management(momentum_agent, mock_data):
    # Test risk management with high volatility
    high_vol_data = mock_data.copy()
    high_vol_data['close'] = high_vol_data['close'] * (1 + np.random.normal(0, 0.1, len(high_vol_data)))
    
    risk = momentum_agent._calculate_position_risk(high_vol_data)
    assert risk > momentum_agent._calculate_position_risk(mock_data)

def test_performance_metrics(momentum_agent, mock_data):
    insights = momentum_agent.get_trading_insights(mock_data)
    metrics = insights['performance_metrics']
    
    assert 'sharpe_ratio' in metrics
    assert 'sortino_ratio' in metrics
    assert 'max_drawdown' in metrics
    assert isinstance(metrics['sharpe_ratio'], float)
    assert isinstance(metrics['sortino_ratio'], float)
    assert isinstance(metrics['max_drawdown'], float) 