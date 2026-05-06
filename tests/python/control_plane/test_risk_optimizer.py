import pytest
from unittest.mock import patch, MagicMock
from src.control_plane.risk_optimizer import RiskOptimizer
from src.trading_interface import get_position, get_latest_price
from src.logger import logger
import numpy as np

@pytest.fixture
def sample_returns():
    """Create sample returns data for testing."""
    np.random.seed(42)
    return np.random.randn(100, 3) * 0.01  # 3 assets, 100 days

def test_risk_optimizer_initialization(risk_optimizer):
    """Test RiskOptimizer initialization."""
    assert risk_optimizer.max_position_size == 0.1
    assert risk_optimizer.max_leverage == 2.0
    assert risk_optimizer.risk_free_rate == 0.02
    assert risk_optimizer.target_volatility == 0.15

def test_portfolio_optimization(risk_optimizer, sample_returns):
    """Test portfolio optimization."""
    weights = risk_optimizer.optimize_portfolio(sample_returns)
    
    # Check weights
    assert len(weights) == sample_returns.shape[1]
    assert np.all(weights >= 0)  # No short positions
    assert np.abs(np.sum(weights) - 1.0) < 1e-6  # Weights sum to 1
    
    # Check position size constraints
    assert np.all(weights <= risk_optimizer.max_position_size)

def test_risk_metrics(risk_optimizer, sample_returns):
    """Test risk metrics calculation."""
    weights = risk_optimizer.optimize_portfolio(sample_returns)
    
    # Calculate portfolio metrics
    portfolio_returns = sample_returns @ weights
    volatility = risk_optimizer.calculate_volatility(portfolio_returns)
    sharpe_ratio = risk_optimizer.calculate_sharpe_ratio(portfolio_returns)
    
    # Check metrics
    assert volatility > 0
    assert isinstance(sharpe_ratio, float)

def test_leverage_constraints(risk_optimizer, sample_returns):
    """Test leverage constraints."""
    # Test with high target volatility
    risk_optimizer.target_volatility = 0.3
    weights = risk_optimizer.optimize_portfolio(sample_returns)
    
    # Check leverage
    leverage = np.sum(np.abs(weights))
    assert leverage <= risk_optimizer.max_leverage

def test_drawdown_management(risk_optimizer, sample_returns):
    """Test drawdown management."""
    # Calculate drawdown
    portfolio_returns = sample_returns @ risk_optimizer.optimize_portfolio(sample_returns)
    drawdown = risk_optimizer.calculate_drawdown(portfolio_returns)
    
    # Check drawdown properties
    assert np.all(drawdown <= 0)  # Drawdown should be non-positive
    assert len(drawdown) == len(portfolio_returns)

def test_risk_parity(risk_optimizer, sample_returns):
    """Test risk parity optimization."""
    weights = risk_optimizer.optimize_risk_parity(sample_returns)
    
    # Check risk parity properties
    assert len(weights) == sample_returns.shape[1]
    assert np.all(weights >= 0)
    assert np.abs(np.sum(weights) - 1.0) < 1e-6
    
    # Check risk contributions
    risk_contributions = risk_optimizer.calculate_risk_contributions(weights, sample_returns)
    assert np.all(np.abs(risk_contributions - np.mean(risk_contributions)) < 1e-6)

def test_correlation_scenarios(risk_optimizer):
    """Test optimization with different correlation scenarios."""
    # Perfectly correlated assets
    returns_correlated = np.ones((100, 3)) * np.random.randn(100, 1)
    weights_correlated = risk_optimizer.optimize_portfolio(returns_correlated)
    assert np.all(np.abs(weights_correlated - 1/3) < 1e-6)
    
    # Negatively correlated assets
    returns_negative = np.random.randn(100, 3)
    returns_negative[:, 1] = -returns_negative[:, 0]
    weights_negative = risk_optimizer.optimize_portfolio(returns_negative)
    assert np.all(weights_negative >= 0)
    
    # Uncorrelated assets
    returns_uncorrelated = np.random.randn(100, 3)
    weights_uncorrelated = risk_optimizer.optimize_portfolio(returns_uncorrelated)
    assert np.all(weights_uncorrelated >= 0)

def test_extreme_market_conditions(risk_optimizer):
    """Test optimization under extreme market conditions."""
    # Market crash scenario
    returns_crash = np.random.randn(100, 3) * 0.01
    returns_crash[50:] *= 5  # Simulate crash
    weights_crash = risk_optimizer.optimize_portfolio(returns_crash)
    assert np.all(weights_crash >= 0)
    
    # High volatility scenario
    returns_volatile = np.random.randn(100, 3) * 0.05
    weights_volatile = risk_optimizer.optimize_portfolio(returns_volatile)
    assert np.all(weights_volatile >= 0)
    
    # Low volatility scenario
    returns_calm = np.random.randn(100, 3) * 0.001
    weights_calm = risk_optimizer.optimize_portfolio(returns_calm)
    assert np.all(weights_calm >= 0)

def test_transaction_costs(risk_optimizer, sample_returns):
    """Test optimization with transaction costs."""
    # Set transaction costs
    risk_optimizer.transaction_cost = 0.001
    
    # Optimize with costs
    weights = risk_optimizer.optimize_portfolio(sample_returns)
    
    # Check that weights are more stable (less frequent changes)
    assert np.all(weights >= 0)
    assert np.abs(np.sum(weights) - 1.0) < 1e-6

def test_constraint_violations(risk_optimizer, sample_returns):
    """Test handling of constraint violations."""
    # Test with impossible constraints
    risk_optimizer.max_position_size = 0.01
    risk_optimizer.max_leverage = 1.0
    
    with pytest.raises(ValueError):
        risk_optimizer.optimize_portfolio(sample_returns)
    
    # Test with tight constraints
    risk_optimizer.max_position_size = 0.2
    risk_optimizer.max_leverage = 1.5
    weights = risk_optimizer.optimize_portfolio(sample_returns)
    assert np.all(weights >= 0)
    assert np.all(weights <= risk_optimizer.max_position_size)

def test_optimization_objectives(risk_optimizer, sample_returns):
    """Test different optimization objectives."""
    # Test minimum variance
    weights_min_var = risk_optimizer.optimize_minimum_variance(sample_returns)
    assert np.all(weights_min_var >= 0)
    assert np.abs(np.sum(weights_min_var) - 1.0) < 1e-6
    
    # Test maximum Sharpe ratio
    weights_max_sharpe = risk_optimizer.optimize_max_sharpe(sample_returns)
    assert np.all(weights_max_sharpe >= 0)
    assert np.abs(np.sum(weights_max_sharpe) - 1.0) < 1e-6
    
    # Test maximum return
    weights_max_return = risk_optimizer.optimize_max_return(sample_returns)
    assert np.all(weights_max_return >= 0)
    assert np.abs(np.sum(weights_max_return) - 1.0) < 1e-6 