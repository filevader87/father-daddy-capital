import pytest
import sys
from io import StringIO
from src.utils.performance_logger import log_trade

def test_log_trade():
    """Test that log_trade emits the expected log message."""
    # Create a StringIO object to capture stdout
    captured_output = StringIO()
    
    # Save the original stdout
    original_stdout = sys.stdout
    
    try:
        # Redirect stdout to our StringIO object
        sys.stdout = captured_output
        
        # Call log_trade with test parameters
        log_trade("SYM", "BUY", 1, 100.0, 100.0, 0.5)
        
        # Get the captured output
        output = captured_output.getvalue()
        
        # Assertions
        assert "[BUY] 1 SYM" in output
    finally:
        # Restore the original stdout
        sys.stdout = original_stdout 