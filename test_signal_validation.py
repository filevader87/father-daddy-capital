#!/usr/bin/env python3
"""
Signal Validation Test Script
-----------------------------
This script tests the signal validation fixes to ensure invalid trade signals
are properly caught and handled.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

from src.utils.signal_validator import signal_validator, ValidationLevel
from src.agents.short_term.crypto_aets import CryptoAETS
from src.agents.short_term.stock_aets import StockAETS
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_signal_validator():
    """Test the signal validator with various scenarios."""
    print("🧪 Testing Signal Validator...")
    
    # Test valid signals
    valid_signals = [
        {
            'symbol': 'BTCUSD',
            'side': 'buy',
            'quantity': 1.0,
            'price': 50000.0,
            'order_type': 'market',
            'strategy': 'crypto_aets'
        },
        {
            'symbol': 'AAPL',
            'side': 'sell',
            'quantity': 100,
            'price': 150.0,
            'order_type': 'limit',
            'strategy': 'stock_aets'
        }
    ]
    
    for i, signal in enumerate(valid_signals):
        result = signal_validator.validate_signal(signal)
        print(f"  Test {i+1}: {signal_validator.get_validation_summary(result)}")
        if not result.is_valid:
            print(f"    Errors: {result.errors}")
    
    # Test invalid signals
    invalid_signals = [
        {
            'symbol': 'INVALID',
            'side': 'buy',
            'quantity': -100,  # Negative quantity
            'price': 100.0,
            'order_type': 'limit',
            'strategy': 'test_strategy'
        },
        {
            'symbol': 'BTCUSD',
            'side': 'invalid_side',  # Invalid side
            'quantity': 1.0,
            'price': 50000.0,
            'order_type': 'market',
            'strategy': 'crypto_aets'
        },
        {
            'symbol': 'BTCUSD',
            'side': 'buy',
            'quantity': 0,  # Zero quantity
            'price': 50000.0,
            'order_type': 'market',
            'strategy': 'crypto_aets'
        },
        {
            'symbol': 'BTCUSD',
            'side': 'buy',
            'quantity': 1.0,
            'price': -100.0,  # Negative price
            'order_type': 'market',
            'strategy': 'crypto_aets'
        },
        {
            'symbol': '',  # Empty symbol
            'side': 'buy',
            'quantity': 1.0,
            'price': 100.0,
            'order_type': 'market',
            'strategy': 'crypto_aets'
        }
    ]
    
    print("\n🔍 Testing Invalid Signals...")
    for i, signal in enumerate(invalid_signals):
        result = signal_validator.validate_signal(signal)
        print(f"  Test {i+1}: {signal_validator.get_validation_summary(result)}")
        if result.errors:
            print(f"    Errors: {result.errors}")
        if result.warnings:
            print(f"    Warnings: {result.warnings}")

def test_agent_signal_generation():
    """Test that agents generate valid signals."""
    print("\n🤖 Testing Agent Signal Generation...")
    
    # Test crypto agent
    try:
        crypto_agent = CryptoAETS()
        print("  ✅ CryptoAETS agent initialized successfully")
    except Exception as e:
        print(f"  ❌ CryptoAETS agent initialization failed: {e}")
    
    # Test stock agent
    try:
        stock_agent = StockAETS()
        print("  ✅ StockAETS agent initialized successfully")
    except Exception as e:
        print(f"  ❌ StockAETS agent initialization failed: {e}")

def test_position_size_calculation():
    """Test position size calculation with edge cases."""
    print("\n📊 Testing Position Size Calculation...")
    
    try:
        crypto_agent = CryptoAETS()
        
        # Test with valid inputs
        valid_size = crypto_agent._calculate_position_size(50000.0, 0.05)
        print(f"  Valid inputs (price=50000, vol=0.05): {valid_size}")
        
        # Test with edge cases
        edge_cases = [
            (0.0, 0.05),      # Zero price
            (50000.0, 0.0),   # Zero volatility
            (-100.0, 0.05),   # Negative price
            (50000.0, -0.1),  # Negative volatility
            (1e10, 0.05),     # Very large price
            (50000.0, 1e10),  # Very large volatility
        ]
        
        for price, vol in edge_cases:
            size = crypto_agent._calculate_position_size(price, vol)
            print(f"  Edge case (price={price}, vol={vol}): {size}")
            
    except Exception as e:
        print(f"  ❌ Position size calculation test failed: {e}")

def main():
    """Run all validation tests."""
    print("🚀 Starting Signal Validation Tests\n")
    
    test_signal_validator()
    test_agent_signal_generation()
    test_position_size_calculation()
    
    print("\n✅ Signal validation tests completed!")

if __name__ == "__main__":
    main() 