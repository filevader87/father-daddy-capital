import pytest
import asyncio
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from unittest.mock import Mock, patch, AsyncMock
from src.core.coordinator import coordinator
from src.main import TradingSystem
from src.risk.risk_manager import risk_manager
from src.monitoring.system_monitor import system_monitor
from src.utils.performance_optimizer import performance_optimizer
from src.monitoring.discord_bot import bot

@pytest.fixture
def mock_config():
    """Mock configuration for testing"""
    return {
        'discord': {'token': 'test_token'},
        'monitoring': {
            'health_check_interval': 1,
            'thresholds': {
                'cpu': 80,
                'memory': 85,
                'disk': 90
            }
        },
        'trading': {
            'risk_limits': {
                'drawdown': 0.1,
                'daily_risk': 10000
            }
        }
    }

@pytest.fixture
def mock_system_metrics():
    """Mock system metrics for testing"""
    return {
        'cpu_percent': 50.0,
        'memory_percent': 60.0,
        'disk_percent': 70.0
    }

@pytest.fixture
def mock_trading_metrics():
    """Mock trading metrics for testing"""
    return {
        'trades_count': 10,
        'total_risk': 5000.0,
        'max_drawdown': 0.05,
        'daily_pnl': 1000.0
    }

@pytest.fixture
def mock_performance_metrics():
    """Mock performance metrics for testing"""
    return {
        'function1': {
            'total_calls': 100,
            'total_time': 1.5,
            'avg_time': 0.015
        }
    }

@pytest.fixture
def mock_backtest_data():
    """Mock backtest data for testing"""
    dates = pd.date_range(start='2023-01-01', end='2023-01-10', freq='D')
    return pd.DataFrame({
        'close': np.random.normal(100, 5, len(dates)),
        'volume': np.random.randint(1000, 10000, len(dates))
    }, index=dates)

@pytest.fixture
def mock_strategy():
    """Mock trading strategy for testing"""
    strategy = Mock()
    strategy.generate_signals.return_value = {
        'AAPL': {'action': 'buy', 'confidence': 0.8}
    }
    return strategy

@pytest.mark.asyncio
async def test_system_initialization(mock_config):
    """Test system initialization"""
    with patch('src.utils.config_loader.config_loader.load_config') as mock_load_config, \
         patch('src.utils.performance_optimizer.performance_optimizer.reset_profiling') as mock_reset_profiling, \
         patch('src.risk.risk_manager.risk_manager.reset_daily_metrics') as mock_reset_metrics, \
         patch('src.monitoring.discord_bot.bot.start') as mock_bot_start:
        
        mock_load_config.return_value = mock_config
        mock_bot_start.return_value = None
        
        trading_system = TradingSystem()
        await trading_system.start()
        
        assert trading_system.running
        assert trading_system.health_check_task is not None
        mock_load_config.assert_called_once()
        mock_reset_profiling.assert_called_once()
        mock_reset_metrics.assert_called_once()
        mock_bot_start.assert_called_once_with('test_token')

@pytest.mark.asyncio
async def test_health_check_loop(mock_system_metrics, mock_trading_metrics, mock_performance_metrics):
    """Test health check loop functionality"""
    with patch('src.monitoring.system_monitor.system_monitor.get_system_metrics') as mock_get_system_metrics, \
         patch('src.risk.risk_manager.risk_manager.get_daily_metrics') as mock_get_trading_metrics, \
         patch('src.utils.performance_optimizer.performance_optimizer.get_performance_report') as mock_get_performance:
        
        mock_get_system_metrics.return_value = mock_system_metrics
        mock_get_trading_metrics.return_value = mock_trading_metrics
        mock_get_performance.return_value = mock_performance_metrics
        
        trading_system = TradingSystem()
        await trading_system.start()
        
        # Wait for health check to run
        await asyncio.sleep(2)
        
        assert coordinator.system_metrics == mock_system_metrics
        assert coordinator.trading_metrics == mock_trading_metrics
        assert coordinator.performance_metrics == mock_performance_metrics
        
        await trading_system.shutdown()

@pytest.mark.asyncio
async def test_critical_issues_handling():
    """Test handling of critical system issues"""
    with patch('src.monitoring.system_monitor.system_monitor.get_system_metrics') as mock_get_system_metrics, \
         patch('src.risk.risk_manager.risk_manager.get_daily_metrics') as mock_get_trading_metrics, \
         patch('src.core.coordinator.coordinator.stop_trading') as mock_stop_trading, \
         patch('src.monitoring.discord_bot.bot.alert_channel') as mock_alert_channel:
        
        # Simulate critical CPU usage
        mock_get_system_metrics.return_value = {'cpu_percent': 90.0, 'memory_percent': 60.0, 'disk_percent': 70.0}
        mock_get_trading_metrics.return_value = {'max_drawdown': 0.05, 'total_risk': 5000.0}
        mock_alert_channel.send = AsyncMock()
        
        trading_system = TradingSystem()
        await trading_system.start()
        
        # Wait for health check to run
        await asyncio.sleep(2)
        
        mock_stop_trading.assert_called_once()
        mock_alert_channel.send.assert_called_once()
        
        await trading_system.shutdown()

@pytest.mark.asyncio
async def test_backtest_execution(mock_backtest_data, mock_strategy):
    """Test backtest execution with coordinated components"""
    with patch('src.backtest.backtester.Backtester.run_backtest') as mock_run_backtest, \
         patch('src.utils.performance_optimizer.performance_optimizer.profile') as mock_profile, \
         patch('src.risk.risk_manager.risk_manager.update_backtest_metrics') as mock_update_metrics:
        
        mock_run_backtest.return_value = {'sharpe_ratio': 1.5, 'total_return': 0.1}
        
        results = await coordinator.run_backtest(
            mock_strategy,
            mock_backtest_data,
            start_date=datetime(2023, 1, 1),
            end_date=datetime(2023, 1, 10)
        )
        
        mock_run_backtest.assert_called_once()
        mock_profile.assert_called_once()
        mock_update_metrics.assert_called_once_with({'sharpe_ratio': 1.5, 'total_return': 0.1})
        assert results == {'sharpe_ratio': 1.5, 'total_return': 0.1}

@pytest.mark.asyncio
async def test_trading_control():
    """Test trading start/stop functionality"""
    with patch('src.core.coordinator.coordinator._notify_status_change') as mock_notify:
        
        # Test starting trading
        await coordinator.start_trading()
        assert coordinator.trading_enabled
        mock_notify.assert_called_once_with("Trading operations started")
        
        # Test stopping trading
        await coordinator.stop_trading()
        assert not coordinator.trading_enabled
        assert mock_notify.call_count == 2

@pytest.mark.asyncio
async def test_system_shutdown():
    """Test graceful system shutdown"""
    trading_system = TradingSystem()
    await trading_system.start()
    
    # Simulate shutdown signal
    await trading_system.shutdown(signal.SIGTERM)
    
    assert not trading_system.running
    assert trading_system.health_check_task.cancelled()

@pytest.mark.asyncio
async def test_system_status_reporting():
    """Test system status reporting"""
    with patch('src.monitoring.system_monitor.system_monitor.get_system_metrics') as mock_get_system_metrics, \
         patch('src.risk.risk_manager.risk_manager.get_daily_metrics') as mock_get_trading_metrics, \
         patch('src.utils.performance_optimizer.performance_optimizer.get_performance_report') as mock_get_performance:
        
        mock_get_system_metrics.return_value = {'cpu_percent': 50.0}
        mock_get_trading_metrics.return_value = {'trades_count': 10}
        mock_get_performance.return_value = {'function1': {'total_calls': 100}}
        
        status = coordinator.get_system_status()
        
        assert 'system_metrics' in status
        assert 'trading_metrics' in status
        assert 'performance_metrics' in status
        assert 'trading_enabled' in status
        assert 'last_health_check' in status 