import pytest
import os
import numpy as np
import pandas as pd
import torch
from src.core.event_bus import EventBus
from src.utils.api_manager import APIManager
from src.core.config_manager import ConfigManager
from src.models.ltc_cell import LTCCell
from src.utils.risk_optimizer import RiskOptimizer
from src.utils.feature_engineering import FeatureEngineer
from src.utils.synthetic_dna import SyntheticDNAGenerator
from src.utils.self_repair import SelfRepairSystem
from typing import Dict, Any, Generator
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
import json

from src.trading_interface import (
    place_order,
    get_position,
    get_latest_price,
    get_crypto_price,
    get_market_data
)
from src.utils.api_manager import api_manager
from src.utils.solana_dex_interface import solana_dex_interface
from src.logger import logger
from src.monitoring import monitoring, MonitoringSystem

# Set environment variables
os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'
os.environ['CUDA_VISIBLE_DEVICES'] = '0'
os.environ['OMP_NUM_THREADS'] = '1'

# Test configuration
TEST_CONFIG = {
    'max_retries': 3,
    'retry_delay': 0.1,
    'timeout': 5.0,
    'mock_responses': {
        'AAPL': {'price': 150.0, 'volume': 1000000},
        'BTCUSD': {'price': 50000.0, 'volume': 1000},
        'SOLUSD': {'price': 100.0, 'volume': 5000}
    }
}

@pytest.fixture
def event_bus():
    """Fixture to provide an EventBus instance."""
    return EventBus()

@pytest.fixture
def api_manager(event_bus):
    """Fixture to provide an APIManager instance."""
    return APIManager(event_bus)

@pytest.fixture
def config_manager(event_bus):
    """Fixture to provide a ConfigManager instance."""
    return ConfigManager(event_bus)

@pytest.fixture
def ltc_cell():
    """Fixture to provide an LTCCell instance."""
    return LTCCell(units=16)

@pytest.fixture
def swarm_data():
    """Fixture to provide sample swarm data for testing."""
    return {
        'positions': np.random.randn(100, 3),  # 100 agents, 3D positions
        'velocities': np.random.randn(100, 3),  # 100 agents, 3D velocities
        'fitness': np.random.rand(100),  # 100 fitness values
        'best_position': np.array([0.5, 0.5, 0.5]),
        'best_fitness': 0.95
    }

@pytest.fixture
def risk_optimizer():
    """Fixture to provide a RiskOptimizer instance."""
    return RiskOptimizer(
        max_position_size=0.1,
        max_leverage=2.0,
        risk_free_rate=0.02,
        target_volatility=0.15
    )

@pytest.fixture(scope='session')
def torch_device():
    """Determine the best available device for PyTorch."""
    if torch.cuda.is_available():
        return torch.device('cuda')
    elif torch.backends.mps.is_available():
        return torch.device('mps')
    else:
        return torch.device('cpu')

@pytest.fixture(scope='session')
def seed():
    """Set random seed for reproducibility."""
    seed = 42
    np.random.seed(seed)
    torch.manual_seed(seed)
    return seed

@pytest.fixture(scope='session')
def market_data_config():
    """Configuration for market data generation."""
    return {
        'start_date': '2023-01-01',
        'end_date': '2023-04-10',
        'freq': 'D',
        'num_assets': 3,
        'price_range': (95, 105),
        'volume_range': (1000, 10000)
    }

@pytest.fixture(scope='session')
def feature_engineering_config():
    """Configuration for feature engineering."""
    return {
        'window_sizes': [5, 10, 20],
        'indicators': ['sma', 'ema', 'rsi', 'macd'],
        'normalization': True,
        'fill_method': 'ffill'
    }

@pytest.fixture(scope='session')
def ltc_config():
    """Configuration for LTC cell."""
    return {
        'units': 16,
        'input_size': 20,  # Based on feature engineering output
        'batch_first': True,
        'dropout': 0.1
    }

@pytest.fixture(scope='session')
def risk_optimizer_config():
    """Configuration for risk optimizer."""
    return {
        'max_position_size': 0.1,
        'max_leverage': 2.0,
        'risk_free_rate': 0.02,
        'target_volatility': 0.15,
        'transaction_cost': 0.001
    }

@pytest.fixture(scope='session')
def dna_generator_config():
    """Configuration for synthetic DNA generator."""
    return {
        'sequence_length': 100,
        'mutation_rate': 0.01,
        'crossover_rate': 0.7,
        'population_size': 10,
        'nucleotides': ['A', 'C', 'G', 'T']
    }

@pytest.fixture(scope='session')
def self_repair_config():
    """Configuration for self-repair system."""
    return {
        'health_threshold': 0.7,
        'repair_attempts': 3,
        'recovery_time': 5,
        'monitoring_interval': 1,
        'degradation_rate': 0.1
    }

@pytest.fixture(scope='session')
def performance_thresholds():
    """Performance thresholds for monitoring."""
    return {
        'feature_engineering_time': 1.0,  # seconds
        'ltc_inference_time': 0.5,  # seconds
        'optimization_time': 0.5,  # seconds
        'memory_usage': 1024 * 1024 * 1024,  # 1GB
        'cpu_usage': 80,  # percentage
    }

@pytest.fixture(scope='session')
def interconnected_system(
    torch_device,
    ltc_config,
    feature_engineering_config,
    risk_optimizer_config,
    dna_generator_config,
    self_repair_config
):
    """Create an interconnected system with all components."""
    # Initialize components with configurations
    ltc_cell = LTCCell(
        units=ltc_config['units'],
        input_size=ltc_config['input_size'],
        batch_first=ltc_config['batch_first'],
        dropout=ltc_config['dropout']
    ).to(torch_device)
    
    feature_engineer = FeatureEngineer(
        window_sizes=feature_engineering_config['window_sizes'],
        indicators=feature_engineering_config['indicators'],
        normalization=feature_engineering_config['normalization'],
        fill_method=feature_engineering_config['fill_method']
    )
    
    risk_optimizer = RiskOptimizer(
        max_position_size=risk_optimizer_config['max_position_size'],
        max_leverage=risk_optimizer_config['max_leverage'],
        risk_free_rate=risk_optimizer_config['risk_free_rate'],
        target_volatility=risk_optimizer_config['target_volatility'],
        transaction_cost=risk_optimizer_config['transaction_cost']
    )
    
    dna_generator = SyntheticDNAGenerator(
        sequence_length=dna_generator_config['sequence_length'],
        mutation_rate=dna_generator_config['mutation_rate'],
        crossover_rate=dna_generator_config['crossover_rate'],
        population_size=dna_generator_config['population_size'],
        nucleotides=dna_generator_config['nucleotides']
    )
    
    self_repair = SelfRepairSystem(
        health_threshold=self_repair_config['health_threshold'],
        repair_attempts=self_repair_config['repair_attempts'],
        recovery_time=self_repair_config['recovery_time'],
        monitoring_interval=self_repair_config['monitoring_interval'],
        degradation_rate=self_repair_config['degradation_rate']
    )
    
    return {
        'ltc_cell': ltc_cell,
        'feature_engineer': feature_engineer,
        'risk_optimizer': risk_optimizer,
        'dna_generator': dna_generator,
        'self_repair': self_repair,
        'device': torch_device
    }

@pytest.fixture(scope='session')
def sample_market_data(market_data_config, seed):
    """Create sample market data for testing."""
    np.random.seed(seed)
    dates = pd.date_range(
        start=market_data_config['start_date'],
        end=market_data_config['end_date'],
        freq=market_data_config['freq']
    )
    
    data = {}
    for i in range(market_data_config['num_assets']):
        asset_name = f'Asset_{i+1}'
        data[asset_name] = pd.DataFrame({
            'open': np.random.randn(len(dates)).cumsum() + market_data_config['price_range'][0],
            'high': np.random.randn(len(dates)).cumsum() + market_data_config['price_range'][1],
            'low': np.random.randn(len(dates)).cumsum() + market_data_config['price_range'][0],
            'close': np.random.randn(len(dates)).cumsum() + market_data_config['price_range'][0],
            'volume': np.random.randint(
                market_data_config['volume_range'][0],
                market_data_config['volume_range'][1],
                len(dates)
            )
        }, index=dates)
    
    return data

@pytest.fixture(scope='session')
def test_config() -> Dict[str, Any]:
    """Global test configuration"""
    return TEST_CONFIG

@pytest.fixture(scope='session')
def mock_api_manager() -> Generator[MagicMock, None, None]:
    """Mock API manager for testing"""
    with patch('src.utils.api_manager.api_manager') as mock:
        mock.get_api_key.return_value = "test_key"
        mock.make_request.return_value = MagicMock(
            json=lambda: {"ask_price": 100.0},
            status_code=200
        )
        yield mock

@pytest.fixture(scope='session')
def mock_solana_dex() -> Generator[MagicMock, None, None]:
    """Mock Solana DEX interface for testing"""
    with patch('src.utils.solana_dex_interface.solana_dex_interface') as mock:
        mock.simulate_order.return_value = {
            "price_impact": 0.005,
            "estimated_price": 100.0,
            "liquidity": 1000000.0
        }
        mock.place_dex_order.return_value = {
            "status": "executed",
            "price": 100.0,
            "tx_hash": "0x123"
        }
        yield mock

@pytest.fixture(scope='session')
def mock_logger() -> Generator[MagicMock, None, None]:
    """Mock logger for testing"""
    with patch('src.logger.logger') as mock:
        yield mock

@pytest.fixture(scope='session')
def mock_monitoring() -> Generator[MonitoringSystem, None, None]:
    """Mock monitoring system for testing"""
    monitoring_system = MonitoringSystem()
    monitoring_system.alert_webhook = None  # Disable webhook in tests
    with patch('src.monitoring.monitoring', monitoring_system):
        yield monitoring_system

@pytest.fixture(scope='function')
def clean_circuit_breaker_state() -> Generator[None, None, None]:
    """Reset circuit breaker state before each test"""
    from src.trading_interface import circuit_breaker_state
    original_state = circuit_breaker_state.copy()
    circuit_breaker_state.clear()
    yield
    circuit_breaker_state.update(original_state)

@pytest.fixture(scope='function')
def mock_market_data() -> Generator[Dict[str, Any], None, None]:
    """Mock market data for testing"""
    return {
        'AAPL': {
            'price': 150.0,
            'volume': 1000000,
            'bid': 149.9,
            'ask': 150.1,
            'timestamp': datetime.now().isoformat()
        },
        'BTCUSD': {
            'price': 50000.0,
            'volume': 1000,
            'bid': 49900.0,
            'ask': 50100.0,
            'timestamp': datetime.now().isoformat()
        }
    }

@pytest.fixture(scope='function')
def mock_order_response() -> Generator[Dict[str, Any], None, None]:
    """Mock order response for testing"""
    return {
        'id': 'test_order_id',
        'symbol': 'AAPL',
        'qty': 1.0,
        'side': 'buy',
        'type': 'market',
        'status': 'filled',
        'filled_at': datetime.now().isoformat(),
        'filled_price': 150.0
    }

@pytest.fixture(scope='function')
def mock_position() -> Generator[Dict[str, Any], None, None]:
    """Mock position data for testing"""
    return {
        'symbol': 'AAPL',
        'qty': 10.0,
        'avg_entry_price': 145.0,
        'market_value': 1500.0,
        'unrealized_pl': 50.0,
        'unrealized_plpc': 0.0345
    }

@pytest.fixture(scope='function')
def test_data_dir(tmp_path) -> Generator[str, None, None]:
    """Create temporary test data directory"""
    data_dir = tmp_path / "test_data"
    data_dir.mkdir()
    yield str(data_dir)

@pytest.fixture(scope='function')
def mock_state_file(tmp_path) -> Generator[str, None, None]:
    """Create temporary state file for testing"""
    state_file = tmp_path / "test_state.json"
    state_data = {
        'circuit_breakers': {},
        'daily_volumes': {},
        'last_reset': datetime.now().isoformat()
    }
    with open(state_file, 'w') as f:
        json.dump(state_data, f)
    yield str(state_file)

@pytest.fixture(scope='function')
def mock_metrics_file(tmp_path) -> Generator[str, None, None]:
    """Create temporary metrics file for testing"""
    metrics_file = tmp_path / "test_metrics.json"
    metrics_data = {
        'order_latency': [],
        'error_count': [],
        'request_count': [],
        'price_impact': []
    }
    with open(metrics_file, 'w') as f:
        json.dump(metrics_data, f)
    yield str(metrics_file)

@pytest.fixture(scope='function')
def mock_alert_file(tmp_path) -> Generator[str, None, None]:
    """Create temporary alert file for testing"""
    alert_file = tmp_path / "test_alerts.json"
    alert_data = {
        'alerts': [],
        'last_alert_time': datetime.now().isoformat()
    }
    with open(alert_file, 'w') as f:
        json.dump(alert_data, f)
    yield str(alert_file)

def pytest_configure(config):
    """Configure pytest with custom markers"""
    config.addinivalue_line(
        "markers",
        "integration: mark test as integration test"
    )
    config.addinivalue_line(
        "markers",
        "slow: mark test as slow running"
    )
    config.addinivalue_line(
        "markers",
        "performance: mark test as performance test"
    )
    config.addinivalue_line(
        "markers",
        "circuit_breaker: mark test as circuit breaker test"
    ) 