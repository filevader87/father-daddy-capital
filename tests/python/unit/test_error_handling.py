import pytest
import time
from typing import Dict, Any
from unittest.mock import patch, MagicMock
from src.trading_interface import (
    place_order,
    get_position,
    get_latest_price,
    check_circuit_breaker,
    update_circuit_breaker
)
from src.utils.solana_dex_interface import DEXExecutionError, OrderSimulationError
from src.monitoring import monitoring

def test_network_timeout(mock_api_manager: MagicMock, mock_logger: MagicMock) -> None:
    """Test handling of network timeouts"""
    # Simulate network timeout
    mock_api_manager.make_request.side_effect = TimeoutError("Request timed out")
    
    # Attempt to place order
    result = place_order("AAPL", 1.0, "buy")
    
    # Verify error handling
    assert result["status"] == "error"
    assert "timeout" in result["error"].lower()
    mock_logger.error.assert_called()

def test_rate_limit_exceeded(mock_api_manager: MagicMock, mock_logger: MagicMock) -> None:
    """Test handling of rate limit exceeded"""
    # Simulate rate limit response
    mock_api_manager.make_request.return_value = MagicMock(
        status_code=429,
        json=lambda: {"message": "Rate limit exceeded"}
    )
    
    # Attempt to place order
    result = place_order("AAPL", 1.0, "buy")
    
    # Verify error handling
    assert result["status"] == "error"
    assert "rate limit" in result["error"].lower()
    mock_logger.warning.assert_called()

def test_insufficient_funds(mock_api_manager: MagicMock, mock_logger: MagicMock) -> None:
    """Test handling of insufficient funds"""
    # Simulate insufficient funds response
    mock_api_manager.make_request.return_value = MagicMock(
        status_code=400,
        json=lambda: {"message": "Insufficient funds"}
    )
    
    # Attempt to place order
    result = place_order("AAPL", 1000000.0, "buy")
    
    # Verify error handling
    assert result["status"] == "error"
    assert "insufficient funds" in result["error"].lower()
    mock_logger.error.assert_called()

def test_invalid_symbol(mock_api_manager: MagicMock, mock_logger: MagicMock) -> None:
    """Test handling of invalid symbol"""
    # Attempt to place order with invalid symbol
    result = place_order("INVALID", 1.0, "buy")
    
    # Verify error handling
    assert result["status"] == "error"
    assert "invalid symbol" in result["error"].lower()
    mock_logger.error.assert_called()

def test_dex_simulation_failure(
    mock_solana_dex: MagicMock,
    mock_logger: MagicMock,
    clean_circuit_breaker_state
) -> None:
    """Test handling of DEX simulation failure"""
    # Simulate DEX simulation failure
    mock_solana_dex.simulate_order.side_effect = OrderSimulationError(
        "Insufficient liquidity"
    )
    
    # Attempt to place order
    result = place_order("SOLUSD", 1.0, "buy")
    
    # Verify error handling
    assert result["status"] == "dex_solana_simulation_failed"
    assert "insufficient liquidity" in result["error"].lower()
    mock_logger.error.assert_called()

def test_dex_execution_failure(
    mock_solana_dex: MagicMock,
    mock_logger: MagicMock,
    clean_circuit_breaker_state
) -> None:
    """Test handling of DEX execution failure"""
    # Setup mock simulation success
    mock_solana_dex.simulate_order.return_value = {
        "price_impact": 0.005,
        "estimated_price": 100.0,
        "liquidity": 1000000.0
    }
    
    # Simulate DEX execution failure
    mock_solana_dex.place_dex_order.side_effect = DEXExecutionError(
        "Transaction failed"
    )
    
    # Attempt to place order
    result = place_order("SOLUSD", 1.0, "buy")
    
    # Verify error handling
    assert result["status"] == "dex_solana_failed"
    assert "transaction failed" in result["error"].lower()
    mock_logger.error.assert_called()

def test_circuit_breaker_activation(
    mock_solana_dex: MagicMock,
    mock_logger: MagicMock,
    clean_circuit_breaker_state
) -> None:
    """Test circuit breaker activation after multiple failures"""
    # Simulate consecutive failures
    mock_solana_dex.simulate_order.side_effect = OrderSimulationError(
        "Network error"
    )
    
    # Attempt multiple orders
    for _ in range(5):
        result = place_order("SOLUSD", 1.0, "buy")
        assert result["status"] == "dex_solana_simulation_failed"
    
    # Verify circuit breaker activation
    assert check_circuit_breaker("SOLUSD")
    mock_logger.error.assert_called_with(
        "Circuit breaker activated for SOLUSD after 5 failures"
    )

def test_circuit_breaker_reset(
    mock_solana_dex: MagicMock,
    mock_logger: MagicMock,
    clean_circuit_breaker_state
) -> None:
    """Test circuit breaker reset after timeout"""
    # Activate circuit breaker
    for _ in range(5):
        update_circuit_breaker("SOLUSD", False)
    
    # Verify circuit breaker is active
    assert check_circuit_breaker("SOLUSD")
    
    # Simulate timeout
    with patch('time.time', return_value=time.time() + 301):
        # Verify circuit breaker is reset
        assert not check_circuit_breaker("SOLUSD")
        mock_logger.info.assert_called_with(
            "Circuit breaker reset for SOLUSD"
        )

def test_price_impact_threshold(
    mock_solana_dex: MagicMock,
    mock_logger: MagicMock,
    clean_circuit_breaker_state
) -> None:
    """Test handling of high price impact"""
    # Simulate high price impact
    mock_solana_dex.simulate_order.return_value = {
        "price_impact": 0.05,  # 5% price impact
        "estimated_price": 100.0,
        "liquidity": 100000.0
    }
    
    # Attempt to place order
    result = place_order("SOLUSD", 10.0, "buy")
    
    # Verify warning was logged
    mock_logger.warning.assert_called_with(
        "High price impact detected: 5.00%"
    )

def test_concurrent_order_handling(
    mock_api_manager: MagicMock,
    mock_logger: MagicMock
) -> None:
    """Test handling of concurrent order requests"""
    import concurrent.futures
    
    def place_order_concurrent():
        return place_order("AAPL", 1.0, "buy")
    
    # Attempt concurrent orders
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(place_order_concurrent) for _ in range(10)]
        results = [f.result() for f in futures]
    
    # Verify all orders were handled
    assert len(results) == 10
    assert all(r["status"] != "error" for r in results)

def test_error_metrics_recording(
    mock_api_manager: MagicMock,
    mock_monitoring: MagicMock
) -> None:
    """Test recording of error metrics"""
    # Simulate error
    mock_api_manager.make_request.side_effect = Exception("Test error")
    
    # Attempt to place order
    place_order("AAPL", 1.0, "buy")
    
    # Verify error metrics were recorded
    error_rate = mock_monitoring._calculate_error_rate()
    assert error_rate > 0
    assert "order_latency" in mock_monitoring.metrics
    assert "error_count" in mock_monitoring.metrics 