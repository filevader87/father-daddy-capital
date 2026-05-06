import pytest
import time
import statistics
from typing import Dict, Any, List
from unittest.mock import patch, MagicMock
from src.trading_interface import place_order, get_latest_price, get_position
from src.logger import logger

@pytest.fixture
def mock_api_manager() -> MagicMock:
    """Mock API manager for performance testing"""
    with patch('src.utils.api_manager.api_manager') as mock:
        mock.get_api_key.return_value = "test_key"
        mock.make_request.return_value = MagicMock(
            json=lambda: {"ask_price": 100.0},
            status_code=200
        )
        yield mock

def measure_performance(func, *args, **kwargs) -> Dict[str, float]:
    """Measure function performance metrics"""
    times: List[float] = []
    for _ in range(100):
        start_time = time.perf_counter()
        func(*args, **kwargs)
        end_time = time.perf_counter()
        times.append(end_time - start_time)
    
    return {
        "min": min(times),
        "max": max(times),
        "mean": statistics.mean(times),
        "median": statistics.median(times),
        "p95": statistics.quantiles(times, n=20)[18],
        "p99": statistics.quantiles(times, n=100)[98]
    }

def test_order_placement_performance(mock_api_manager: MagicMock) -> None:
    """Test order placement performance"""
    metrics = measure_performance(place_order, "AAPL", 1.0, "buy")
    
    # Log performance metrics
    logger.info("Order Placement Performance:")
    logger.info(f"Min: {metrics['min']:.6f}s")
    logger.info(f"Max: {metrics['max']:.6f}s")
    logger.info(f"Mean: {metrics['mean']:.6f}s")
    logger.info(f"Median: {metrics['median']:.6f}s")
    logger.info(f"95th percentile: {metrics['p95']:.6f}s")
    logger.info(f"99th percentile: {metrics['p99']:.6f}s")
    
    # Assert performance requirements
    assert metrics['mean'] < 0.1  # Average should be under 100ms
    assert metrics['p95'] < 0.2   # 95% should be under 200ms
    assert metrics['p99'] < 0.5   # 99% should be under 500ms

def test_price_fetch_performance(mock_api_manager: MagicMock) -> None:
    """Test price fetch performance"""
    metrics = measure_performance(get_latest_price, "AAPL")
    
    # Log performance metrics
    logger.info("Price Fetch Performance:")
    logger.info(f"Min: {metrics['min']:.6f}s")
    logger.info(f"Max: {metrics['max']:.6f}s")
    logger.info(f"Mean: {metrics['mean']:.6f}s")
    logger.info(f"Median: {metrics['median']:.6f}s")
    logger.info(f"95th percentile: {metrics['p95']:.6f}s")
    logger.info(f"99th percentile: {metrics['p99']:.6f}s")
    
    # Assert performance requirements
    assert metrics['mean'] < 0.05  # Average should be under 50ms
    assert metrics['p95'] < 0.1    # 95% should be under 100ms
    assert metrics['p99'] < 0.2    # 99% should be under 200ms

def test_position_fetch_performance(mock_api_manager: MagicMock) -> None:
    """Test position fetch performance"""
    metrics = measure_performance(get_position, "AAPL")
    
    # Log performance metrics
    logger.info("Position Fetch Performance:")
    logger.info(f"Min: {metrics['min']:.6f}s")
    logger.info(f"Max: {metrics['max']:.6f}s")
    logger.info(f"Mean: {metrics['mean']:.6f}s")
    logger.info(f"Median: {metrics['median']:.6f}s")
    logger.info(f"95th percentile: {metrics['p95']:.6f}s")
    logger.info(f"99th percentile: {metrics['p99']:.6f}s")
    
    # Assert performance requirements
    assert metrics['mean'] < 0.05  # Average should be under 50ms
    assert metrics['p95'] < 0.1    # 95% should be under 100ms
    assert metrics['p99'] < 0.2    # 99% should be under 200ms

def test_concurrent_performance(mock_api_manager: MagicMock) -> None:
    """Test concurrent operation performance"""
    import concurrent.futures
    
    def run_concurrent_operations() -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            for _ in range(100):
                futures.append(executor.submit(place_order, "AAPL", 1.0, "buy"))
                futures.append(executor.submit(get_latest_price, "AAPL"))
                futures.append(executor.submit(get_position, "AAPL"))
            
            # Wait for all operations to complete
            concurrent.futures.wait(futures)
    
    start_time = time.perf_counter()
    run_concurrent_operations()
    total_time = time.perf_counter() - start_time
    
    # Log performance metrics
    logger.info("Concurrent Operations Performance:")
    logger.info(f"Total time for 300 operations: {total_time:.3f}s")
    logger.info(f"Average time per operation: {total_time/300:.6f}s")
    
    # Assert performance requirements
    assert total_time < 5.0  # Total time should be under 5 seconds
    assert total_time/300 < 0.1  # Average time per operation should be under 100ms 