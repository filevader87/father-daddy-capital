#!/usr/bin/env python3
"""
Simple Signal Validation Test Script
-----------------------------------
This script tests the signal validation fixes without requiring all dependencies.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

from src.utils.signal_validator import signal_validator, ValidationLevel
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
    
    print("\n✅ Testing Valid Signals...")
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
        },
        {
            'symbol': 'BTCUSD',
            'side': 'buy',
            'quantity': 1e10,  # Very large quantity
            'price': 50000.0,
            'order_type': 'market',
            'strategy': 'crypto_aets'
        },
        {
            'symbol': 'BTCUSD',
            'side': 'buy',
            'quantity': 1.0,
            'price': 1e10,  # Very large price
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

def test_validation_levels():
    """Test different validation levels."""
    print("\n📊 Testing Validation Levels...")
    
    # Create validators with different levels
    basic_validator = signal_validator.__class__(ValidationLevel.BASIC)
    strict_validator = signal_validator.__class__(ValidationLevel.STRICT)
    production_validator = signal_validator.__class__(ValidationLevel.PRODUCTION)
    
    test_signal = {
        'symbol': 'BTCUSD',
        'side': 'buy',
        'quantity': 1.0,
        'price': 50000.0,
        'order_type': 'market',
        'strategy': 'crypto_aets'
    }
    
    print(f"  Basic validation: {basic_validator.validate_signal(test_signal).is_valid}")
    print(f"  Strict validation: {strict_validator.validate_signal(test_signal).is_valid}")
    print(f"  Production validation: {production_validator.validate_signal(test_signal).is_valid}")

def main():
    """Run all validation tests."""
    print("🚀 Starting Signal Validation Tests\n")
    
    test_signal_validator()
    test_validation_levels()
    
    print("\n✅ Signal validation tests completed!")
    print("\n📋 Summary of fixes implemented:")
    print("  ✅ Enhanced position size calculation with bounds checking")
    print("  ✅ Added comprehensive signal validation")
    print("  ✅ Improved error handling in RL agents")
    print("  ✅ Added validation in execution pipeline")
    print("  ✅ Created centralized signal validator utility")
    print("  ✅ Added proper logging for debugging")

if __name__ == "__main__":
    main() 