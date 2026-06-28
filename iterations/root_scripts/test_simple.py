#!/usr/bin/env python3
"""
Simple Test Script
------------------
This script tests basic functionality without complex agent initialization.
"""

import os
import sys
import json
from datetime import datetime

# Add project root to Python path
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

def test_config_loading():
    """Test configuration loading."""
    print("🔧 Testing configuration loading...")
    try:
        from src.config import TradingConfig
        config = TradingConfig.load_from_file()
        print("✅ Configuration loaded successfully")
        return True
    except Exception as e:
        print(f"❌ Configuration loading failed: {e}")
        return False

def test_trading_interface():
    """Test trading interface."""
    print("\n📊 Testing trading interface...")
    try:
        from trading_interface import get_latest_price, get_market_data
        
        # Test price fetching
        price = get_latest_price("BTCUSD")
        print(f"✅ BTCUSD price: {price}")
        
        # Test market data
        market_data = get_market_data("BTCUSD")
        print(f"✅ Market data keys: {list(market_data.keys())}")
        
        return True
    except Exception as e:
        print(f"❌ Trading interface test failed: {e}")
        return False

def test_performance_logger():
    """Test performance logger."""
    print("\n📈 Testing performance logger...")
    try:
        from src.utils.performance_logger import PerformanceLogger
        
        logger = PerformanceLogger()
        logger.log_trade("BTCUSD", "buy", 1.0, 50000.0, 50000.0, 100.0)
        print("✅ Performance logger working")
        
        return True
    except Exception as e:
        print(f"❌ Performance logger test failed: {e}")
        return False

def test_risk_manager():
    """Test risk manager."""
    print("\n🛡️ Testing risk manager...")
    try:
        from src.risk.risk_manager import RiskManager
        
        risk_manager = RiskManager()
        can_trade = risk_manager.can_place_order("BTCUSD", 1.0, 50000.0, "buy")
        print(f"✅ Risk manager can_place_order: {can_trade}")
        
        return True
    except Exception as e:
        print(f"❌ Risk manager test failed: {e}")
        return False

def test_directory_structure():
    """Test directory structure."""
    print("\n📁 Testing directory structure...")
    required_dirs = ['logs', 'data', 'state', 'models']
    missing_dirs = []
    
    for directory in required_dirs:
        if not os.path.exists(directory):
            missing_dirs.append(directory)
            os.makedirs(directory, exist_ok=True)
            print(f"✅ Created directory: {directory}")
        else:
            print(f"✅ Directory exists: {directory}")
    
    return len(missing_dirs) == 0

def test_basic_trading_simulation():
    """Test basic trading simulation."""
    print("\n🎯 Testing basic trading simulation...")
    try:
        from trading_interface import get_market_data
        
        # Simulate a simple trading cycle
        symbols = ["BTCUSD", "ETHUSD"]
        total_allocated = 0
        
        for symbol in symbols:
            market_data = get_market_data(symbol)
            if market_data and market_data.get('price', 0) > 0:
                price = market_data['price']
                qty = 1.0  # Simple 1 unit position
                notional = price * qty
                total_allocated += notional
                
                print(f"  📈 {symbol}: ${price:,.2f} x {qty} = ${notional:,.2f}")
        
        print(f"  💰 Total allocated: ${total_allocated:,.2f}")
        return True
        
    except Exception as e:
        print(f"❌ Trading simulation failed: {e}")
        return False

def main():
    """Main test function."""
    print("🚀 Starting Father Daddy Capital Simple Tests")
    print("=" * 60)
    
    tests = [
        ("Configuration Loading", test_config_loading),
        ("Directory Structure", test_directory_structure),
        ("Trading Interface", test_trading_interface),
        ("Performance Logger", test_performance_logger),
        ("Risk Manager", test_risk_manager),
        ("Basic Trading Simulation", test_basic_trading_simulation)
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        try:
            if test_func():
                passed += 1
        except Exception as e:
            print(f"❌ {test_name} failed with exception: {e}")
    
    print("\n" + "=" * 60)
    print("📊 TEST RESULTS")
    print("=" * 60)
    print(f"✅ Passed: {passed}/{total}")
    print(f"❌ Failed: {total - passed}/{total}")
    
    if passed == total:
        print("\n🎉 All tests passed! System is ready for paper trading.")
        return True
    else:
        print(f"\n⚠️ {total - passed} test(s) failed. Please check the issues above.")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1) 