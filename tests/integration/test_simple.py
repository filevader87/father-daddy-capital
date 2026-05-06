import pytest

def test_simple():
    """Simple test to verify pytest environment."""
    assert True

def test_imports():
    """Test that required packages can be imported."""
    import ccxt
    import pandas
    import numpy
    assert True 