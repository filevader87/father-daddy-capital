import pytest
from src.utils.news_analyzer import get_crypto_sentiment

def test_get_crypto_sentiment():
    """Test that get_crypto_sentiment returns a float value."""
    # Test with BTCUSD
    sentiment = get_crypto_sentiment("BTCUSD")
    
    # Assertions
    assert isinstance(sentiment, float)
    assert -1.0 <= sentiment <= 1.0  # Sentiment should be between -1 and 1 