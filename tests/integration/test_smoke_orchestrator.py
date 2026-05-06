import pytest
import json
import tempfile
import os
import time
from unittest.mock import patch, MagicMock
from src.control_plane.orchestrator import Orchestrator

@pytest.fixture
def mock_config():
    """Create a temporary config file with complete configuration."""
    config = {
        "exchange": {
            "name": "binance",
            "api_key": "test_key",
            "api_secret": "test_secret"
        },
        "symbols": ["BTC/USDT"],
        "mode": "paper",
        "loop_cycles": 2,
        "interval_seconds": 1,
        "risk_management": {
            "max_position_size": 0.1,
            "max_drawdown": 0.1,
            "stop_loss": 0.02
        },
        "agent": {
            "confidence_threshold": 0.7,
            "signal_types": ["momentum", "mean_reversion"]
        }
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as temp_file:
        json.dump(config, temp_file)
        temp_file_path = temp_file.name
    
    yield temp_file_path
    
    # Cleanup
    if os.path.exists(temp_file_path):
        os.unlink(temp_file_path)

@pytest.fixture
def mock_exchange():
    """Mock exchange for testing."""
    exchange = MagicMock()
    exchange.fetch_ohlcv.return_value = [
        [int(time.time() * 1000), 50000, 51000, 49000, 50500, 100],
        [int(time.time() * 1000) - 3600000, 49000, 50000, 48000, 49500, 90]
    ]
    return exchange

@pytest.mark.timeout(30)
def test_orchestrator_smoke(mock_config, mock_exchange):
    """Test that the orchestrator can run without exceptions and performs basic operations."""
    # Set environment variable
    os.environ['CONFIG_PATH'] = mock_config
    
    try:
        # Create orchestrator with mocked exchange
        with patch('ccxt.binance', return_value=mock_exchange):
            orchestrator = Orchestrator()
            
            # Verify initialization
            assert orchestrator.config is not None
            assert orchestrator.exchange is not None
            assert orchestrator.agent is not None
            
            # Run trading loop
            orchestrator.run_trading_loop()
            
            # Verify exchange was called
            mock_exchange.fetch_ohlcv.assert_called()
            
            # Verify agent was used
            assert hasattr(orchestrator.agent, 'calculate_signal')
            
    finally:
        # Clean up
        if 'CONFIG_PATH' in os.environ:
            del os.environ['CONFIG_PATH']
        if hasattr(orchestrator, 'stop_trading'):
            orchestrator.stop_trading() 