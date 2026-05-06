import pytest
from src.utils.api_manager import APIManager

def test_api_manager_initialization(api_manager):
    """Test that APIManager initializes correctly."""
    assert api_manager is not None
    assert hasattr(api_manager, 'event_bus')
    assert hasattr(api_manager, 'circuit_breaker')

def test_circuit_breaker_initialization(api_manager):
    """Test that CircuitBreaker initializes correctly."""
    circuit_breaker = api_manager.circuit_breaker
    assert circuit_breaker is not None
    assert circuit_breaker.failure_threshold == 5
    assert circuit_breaker.reset_timeout == 60
    assert circuit_breaker.failure_count == 0
    assert not circuit_breaker.is_open

def test_circuit_breaker_trip(api_manager):
    """Test that CircuitBreaker trips after too many failures."""
    circuit_breaker = api_manager.circuit_breaker
    for _ in range(circuit_breaker.failure_threshold):
        circuit_breaker.record_failure()
    assert circuit_breaker.is_open

def test_circuit_breaker_reset(api_manager):
    """Test that CircuitBreaker resets after timeout."""
    circuit_breaker = api_manager.circuit_breaker
    for _ in range(circuit_breaker.failure_threshold):
        circuit_breaker.record_failure()
    assert circuit_breaker.is_open
    circuit_breaker.reset()
    assert not circuit_breaker.is_open
    assert circuit_breaker.failure_count == 0 