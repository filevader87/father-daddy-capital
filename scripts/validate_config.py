#!/usr/bin/env python3
"""
Configuration Validation Script
-------------------------------
This script validates all configuration files and ensures they are properly set up
for paper trading without requiring real exchange API keys.
"""

import os
import sys
import json
import yaml
from pathlib import Path
from typing import Dict, Any, List

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

def validate_trading_config() -> List[str]:
    """Validate trading configuration."""
    errors = []
    
    try:
        config_path = project_root / "config" / "trading_config.json"
        if not config_path.exists():
            errors.append("trading_config.json not found")
            return errors
            
        with open(config_path, 'r') as f:
            config = json.load(f)
            
        # Check required sections
        required_sections = ['paper_trading', 'exchange', 'trading', 'monitoring', 'notifier']
        for section in required_sections:
            if section not in config:
                errors.append(f"Missing required section: {section}")
                
        # Check paper trading settings
        if 'paper_trading' in config:
            paper_config = config['paper_trading']
            if 'initial_balance' not in paper_config:
                errors.append("Missing initial_balance in paper_trading config")
            elif paper_config['initial_balance'] <= 0:
                errors.append("Initial balance must be positive")
                
        # Check exchange settings
        if 'exchange' in config:
            exchange_config = config['exchange']
            if 'trading_pairs' not in exchange_config:
                errors.append("Missing trading_pairs in exchange config")
            elif not isinstance(exchange_config['trading_pairs'], list):
                errors.append("trading_pairs must be a list")
                
        # Check trading settings
        if 'trading' in config:
            trading_config = config['trading']
            if 'interval_seconds' not in trading_config:
                errors.append("Missing interval_seconds in trading config")
            elif trading_config['interval_seconds'] <= 0:
                errors.append("Trading interval must be positive")
                
    except Exception as e:
        errors.append(f"Error validating trading config: {str(e)}")
        
    return errors

def validate_system_config() -> List[str]:
    """Validate system configuration."""
    errors = []
    
    try:
        config_path = project_root / "config" / "system_config.yaml"
        if not config_path.exists():
            errors.append("system_config.yaml not found")
            return errors
            
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
            
        # Check required sections
        required_sections = ['resources', 'network', 'database', 'security', 'maintenance']
        for section in required_sections:
            if section not in config:
                errors.append(f"Missing required section: {section}")
                
        # Check resource settings
        if 'resources' in config:
            resources = config['resources']
            if 'memory' in resources:
                memory = resources['memory']
                if 'max_heap_size' not in memory:
                    errors.append("Missing max_heap_size in memory config")
                    
    except Exception as e:
        errors.append(f"Error validating system config: {str(e)}")
        
    return errors

def validate_agent_config() -> List[str]:
    """Validate agent configuration."""
    errors = []
    
    try:
        config_path = project_root / "config" / "agent_config.yaml"
        if not config_path.exists():
            errors.append("agent_config.yaml not found")
            return errors
            
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
            
        # Check short-term agent settings
        if 'short_term' in config:
            short_term = config['short_term']
            if 'max_trades_per_day' not in short_term:
                errors.append("Missing max_trades_per_day in short_term config")
            elif short_term['max_trades_per_day'] <= 0:
                errors.append("max_trades_per_day must be positive")
                
        # Check long-term agent settings
        if 'long_term' in config:
            long_term = config['long_term']
            if 'rebalance_interval_days' not in long_term:
                errors.append("Missing rebalance_interval_days in long_term config")
            elif long_term['rebalance_interval_days'] <= 0:
                errors.append("rebalance_interval_days must be positive")
                
    except Exception as e:
        errors.append(f"Error validating agent config: {str(e)}")
        
    return errors

def validate_environment_variables() -> List[str]:
    """Validate environment variables for paper trading."""
    errors = []
    warnings = []
    
    # Required for notifications (but not for basic paper trading)
    notification_vars = [
        'EMAIL_USER', 'EMAIL_RECIPIENT', 
        'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 
        'SLACK_WEBHOOK_URL'
    ]
    
    for var in notification_vars:
        if not os.getenv(var):
            warnings.append(f"Notification variable {var} not set (optional for paper trading)")
            
    # Check for any Binance API keys (should not be required)
    binance_vars = ['BINANCE_API_KEY', 'BINANCE_API_SECRET']
    for var in binance_vars:
        if os.getenv(var):
            warnings.append(f"Binance API key {var} is set but not required for paper trading")
            
    return errors, warnings

def validate_directory_structure() -> List[str]:
    """Validate required directory structure."""
    errors = []
    
    required_dirs = [
        'logs', 'data', 'data/market_data', 'data/positions', 
        'data/trades', 'data/backtests', 'state', 'models'
    ]
    
    for dir_path in required_dirs:
        full_path = project_root / dir_path
        if not full_path.exists():
            errors.append(f"Required directory not found: {dir_path}")
            
    return errors

def main():
    """Main validation function."""
    print("🔍 Validating Father Daddy Capital Configuration...")
    print("=" * 60)
    
    all_errors = []
    all_warnings = []
    
    # Validate configurations
    print("\n📋 Validating Trading Configuration...")
    errors = validate_trading_config()
    all_errors.extend(errors)
    for error in errors:
        print(f"  ❌ {error}")
    if not errors:
        print("  ✅ Trading configuration is valid")
        
    print("\n⚙️  Validating System Configuration...")
    errors = validate_system_config()
    all_errors.extend(errors)
    for error in errors:
        print(f"  ❌ {error}")
    if not errors:
        print("  ✅ System configuration is valid")
        
    print("\n🤖 Validating Agent Configuration...")
    errors = validate_agent_config()
    all_errors.extend(errors)
    for error in errors:
        print(f"  ❌ {error}")
    if not errors:
        print("  ✅ Agent configuration is valid")
        
    print("\n📁 Validating Directory Structure...")
    errors = validate_directory_structure()
    all_errors.extend(errors)
    for error in errors:
        print(f"  ❌ {error}")
    if not errors:
        print("  ✅ Directory structure is valid")
        
    print("\n🌍 Validating Environment Variables...")
    errors, warnings = validate_environment_variables()
    all_errors.extend(errors)
    all_warnings.extend(warnings)
    for error in errors:
        print(f"  ❌ {error}")
    for warning in warnings:
        print(f"  ⚠️  {warning}")
    if not errors:
        print("  ✅ Environment variables are valid for paper trading")
        
    # Summary
    print("\n" + "=" * 60)
    print("📊 VALIDATION SUMMARY")
    print("=" * 60)
    
    if all_errors:
        print(f"❌ Found {len(all_errors)} error(s):")
        for error in all_errors:
            print(f"  • {error}")
        print("\n🔧 Please fix these errors before proceeding.")
        return False
    else:
        print("✅ All configurations are valid!")
        
    if all_warnings:
        print(f"\n⚠️  Found {len(all_warnings)} warning(s):")
        for warning in all_warnings:
            print(f"  • {warning}")
        print("\n💡 These warnings don't prevent paper trading but may affect notifications.")
        
    print("\n🎯 System is ready for paper trading!")
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)