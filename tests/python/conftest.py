import pytest
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add the project root directory to the Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Common fixtures
@pytest.fixture
def mock_api_manager():
    """Mock API manager for testing"""
    with patch('src.utils.api_manager.api_manager') as mock:
        yield mock

@pytest.fixture
def mock_solana_dex():
    """Mock Solana DEX interface for testing"""
    with patch('src.utils.solana_dex_interface.solana_dex_interface') as mock:
        yield mock

@pytest.fixture
def mock_logger():
    """Mock logger for testing"""
    with patch('src.logger.logger') as mock:
        yield mock

@pytest.fixture
def trading_config():
    """Load trading configuration for testing"""
    from src.config import TradingConfig
    return TradingConfig()

@pytest.fixture
def mock_env_vars():
    """Mock environment variables for testing"""
    with patch.dict(os.environ, {
        'ALPACA_API_KEY': 'test_key',
        'ALPACA_SECRET_KEY': 'test_secret',
        'COINMARKETCAP_API_KEY': 'test_key',
        'CONFIG_PATH': 'config/trading_config.json'
    }):
        yield 