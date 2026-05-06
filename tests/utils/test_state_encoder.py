import pytest
import numpy as np
from src.utils.state_encoder import generate_state_vector

def test_generate_state_vector():
    """Test state vector generation from a dummy state dictionary."""
    # Create a dummy state dictionary with various data types
    dummy_state = {
        'price': 100.50,
        'volume': 1000,
        'rsi': 65.3,
        'macd': -0.5,
        'signal': 0.2,
        'histogram': -0.7,
        'obv': 15000,
        'volatility': 0.15,
        'position': 1,  # 1 for long, -1 for short, 0 for neutral
        'balance': 10000.0,
        'timestamp': 1714320000  # Unix timestamp
    }
    
    # Generate the state vector
    state_vector = generate_state_vector(dummy_state)
    
    # Assertions
    assert isinstance(state_vector, np.ndarray)
    assert state_vector.dtype == np.float32  # Assuming we want float32 for ML compatibility
    assert len(state_vector) == 10  # Based on the number of features in dummy_state
    assert not np.isnan(state_vector).any()  # No NaN values
    assert not np.isinf(state_vector).any()  # No infinite values 