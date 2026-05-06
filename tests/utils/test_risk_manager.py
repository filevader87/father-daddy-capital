import pytest
from src.utils.risk_manager import risk_manager

def test_risk_manager():
    """Test that risk_manager returns True for empty input."""
    # Call risk_manager with empty dict
    result = risk_manager({})
    
    # Assertion
    assert result is True 