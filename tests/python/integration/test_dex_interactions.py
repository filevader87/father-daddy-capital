import pytest
import time
from typing import Dict, Any, Optional
from unittest.mock import patch, MagicMock
from src.trading_interface import place_order, get_latest_price
from src.utils.solana_dex_interface import solana_dex_interface, DEXExecutionError, OrderSimulationError
from src.logger import logger

@pytest.fixture
def mock_solana_dex() -> MagicMock:
    """Mock Solana DEX interface for testing"""
    with patch('src.utils.solana_dex_interface.solana_dex_interface') as mock:
        yield mock

@pytest.fixture
def mock_logger() -> MagicMock:
    """Mock logger for testing"""
    with patch('src.logger.logger') as mock:
        yield mock

def test_dex_order_success(mock_solana_dex: MagicMock, mock_logger: MagicMock) -> None:
    """Test successful DEX order execution"""
    # Setup mock simulation
    mock_solana_dex.simulate_order.return_value = {
        "price_impact": 0.005,
        "estimated_price": 100.0,
        "liquidity": 1000000.0
    }
    
    # Setup mock execution
    mock_solana_dex.place_dex_order.return_value = {
        "status": "executed",
        "price": 100.0,
        "tx_hash": "0x123"
    }
    
    # Execute order
    result: Dict[str, Any] = place_order("SOLUSD", 1.0, "buy")
    
    # Verify results
    assert result["status"] == "dex_solana_executed"
    assert result["price"] == 100.0
    mock_logger.log_trade.assert_called_once()

def test_dex_high_price_impact(mock_solana_dex: MagicMock, mock_logger: MagicMock) -> None:
    """Test DEX order with high price impact"""
    # Setup mock simulation with high price impact
    mock_solana_dex.simulate_order.return_value = {
        "price_impact": 0.05,  # 5% price impact
        "estimated_price": 100.0,
        "liquidity": 100000.0
    }
    
    # Execute order
    result: Dict[str, Any] = place_order("SOLUSD", 10.0, "buy")
    
    # Verify warning was logged
    mock_logger.warning.assert_called_with(
        "High price impact detected: 5.00%"
    )

def test_dex_simulation_failure(mock_solana_dex: MagicMock, mock_logger: MagicMock) -> None:
    """Test DEX order simulation failure"""
    # Setup mock simulation failure
    mock_solana_dex.simulate_order.side_effect = OrderSimulationError(
        "Insufficient liquidity"
    )
    
    # Execute order
    result: Dict[str, Any] = place_order("SOLUSD", 1.0, "buy")
    
    # Verify fallback behavior
    assert result["status"] == "dex_solana_simulation_failed"
    mock_logger.error.assert_called_with(
        "Order simulation failed: Insufficient liquidity"
    )

def test_dex_execution_failure(mock_solana_dex: MagicMock, mock_logger: MagicMock) -> None:
    """Test DEX order execution failure"""
    # Setup mock simulation success
    mock_solana_dex.simulate_order.return_value = {
        "price_impact": 0.005,
        "estimated_price": 100.0,
        "liquidity": 1000000.0
    }
    
    # Setup mock execution failure
    mock_solana_dex.place_dex_order.side_effect = DEXExecutionError(
        "Transaction failed"
    )
    
    # Execute order
    result: Dict[str, Any] = place_order("SOLUSD", 1.0, "buy")
    
    # Verify fallback behavior
    assert result["status"] == "dex_solana_failed"
    mock_logger.error.assert_called_with(
        "DEX execution failed: Transaction failed"
    )

def test_dex_performance_benchmark(mock_solana_dex: MagicMock) -> None:
    """Test DEX performance benchmarks"""
    # Setup mock responses
    mock_solana_dex.simulate_order.return_value = {
        "price_impact": 0.005,
        "estimated_price": 100.0,
        "liquidity": 1000000.0
    }
    mock_solana_dex.place_dex_order.return_value = {
        "status": "executed",
        "price": 100.0,
        "tx_hash": "0x123"
    }
    
    # Measure simulation time
    start_time = time.time()
    for _ in range(100):
        place_order("SOLUSD", 1.0, "buy")
    simulation_time = time.time() - start_time
    
    # Verify performance
    assert simulation_time < 1.0  # Should complete within 1 second
    avg_time = simulation_time / 100
    logger.info(f"Average DEX order time: {avg_time:.3f}s")

def test_dex_circuit_breaker(mock_solana_dex: MagicMock, mock_logger: MagicMock) -> None:
    """Test DEX circuit breaker functionality"""
    # Setup consecutive failures
    mock_solana_dex.simulate_order.side_effect = OrderSimulationError(
        "Network error"
    )
    
    # Execute multiple orders
    for _ in range(5):
        result: Dict[str, Any] = place_order("SOLUSD", 1.0, "buy")
        assert result["status"] == "dex_solana_simulation_failed"
    
    # Verify circuit breaker activation
    mock_logger.error.assert_called_with(
        "DEX circuit breaker activated after 5 consecutive failures"
    )
    
    # Verify fallback to centralized exchange
    result = place_order("SOLUSD", 1.0, "buy")
    assert result["status"] != "dex_solana_simulation_failed" 