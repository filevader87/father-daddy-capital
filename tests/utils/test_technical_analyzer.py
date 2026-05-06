import pytest
from src.utils.technical_analyzer import TechnicalAnalyzer

def test_technical_analyzer():
    """Test that TechnicalAnalyzer returns a dictionary from analyze()."""
    # Initialize the analyzer
    analyzer = TechnicalAnalyzer()
    
    # Call analyze with empty dict
    result = analyzer.analyze({})
    
    # Assertions
    assert isinstance(result, dict)
    assert len(result) > 0  # Should return some analysis even with empty input 