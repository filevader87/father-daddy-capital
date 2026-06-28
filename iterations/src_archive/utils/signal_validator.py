"""
Trade Signal Validation Utility
------------------------------
This module provides comprehensive validation for trade signals across the system.
It ensures all trade signals meet safety and business requirements before execution.
"""

import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

class ValidationLevel(Enum):
    """Validation levels for trade signals."""
    BASIC = "basic"
    STRICT = "strict"
    PRODUCTION = "production"

@dataclass
class ValidationResult:
    """Result of signal validation."""
    is_valid: bool
    errors: List[str]
    warnings: List[str]
    validation_level: ValidationLevel

class SignalValidator:
    """Comprehensive trade signal validator."""
    
    def __init__(self, validation_level: ValidationLevel = ValidationLevel.PRODUCTION):
        self.validation_level = validation_level
        self.max_quantity = 1000000  # 1M units max
        self.max_price = 1000000     # $1M max price
        self.max_notional = 10000000 # $10M max notional
        self.min_quantity = 0.001    # Minimum quantity
        self.min_price = 0.01        # Minimum price
        
        # Valid symbols (can be extended)
        self.valid_crypto_symbols = [
            'BTCUSD', 'ETHUSD', 'SOLUSD', 'AVAXUSD', 'RNDRUSD', 'XRPUSD', 'ADAUSD'
        ]
        self.valid_stock_symbols = [
            'AAPL', 'MSFT', 'NVDA', 'TSLA', 'GOOGL', 'AMZN', 'META', 'NFLX'
        ]
        
    def validate_signal(self, signal_data: Dict[str, Any]) -> ValidationResult:
        """Validate a trade signal comprehensively."""
        errors = []
        warnings = []
        
        try:
            # Basic structure validation
            if not self._validate_structure(signal_data, errors):
                return ValidationResult(False, errors, warnings, self.validation_level)
            
            # Extract fields
            symbol = signal_data.get('symbol', '')
            side = signal_data.get('side', '')
            quantity = signal_data.get('quantity', 0)
            price = signal_data.get('price', 0)
            order_type = signal_data.get('order_type', '')
            strategy = signal_data.get('strategy', '')
            
            # Field-specific validation
            self._validate_symbol(symbol, errors, warnings)
            self._validate_side(side, errors)
            self._validate_quantity(quantity, errors, warnings)
            self._validate_price(price, errors, warnings)
            self._validate_order_type(order_type, errors)
            self._validate_strategy(strategy, errors)
            
            # Cross-field validation
            if not errors:
                self._validate_notional(quantity, price, errors, warnings)
                self._validate_symbol_consistency(symbol, side, quantity, price, errors, warnings)
            
            # Production-level validations
            if self.validation_level == ValidationLevel.PRODUCTION:
                self._validate_production_constraints(signal_data, errors, warnings)
            
            is_valid = len(errors) == 0
            
        except Exception as e:
            logger.error(f"Error during signal validation: {e}")
            errors.append(f"Validation error: {str(e)}")
            is_valid = False
            
        return ValidationResult(is_valid, errors, warnings, self.validation_level)
    
    def _validate_structure(self, signal_data: Dict[str, Any], errors: List[str]) -> bool:
        """Validate basic structure of signal data."""
        if not isinstance(signal_data, dict):
            errors.append("Signal data must be a dictionary")
            return False
            
        required_fields = ['symbol', 'side', 'quantity', 'price', 'order_type', 'strategy']
        missing_fields = [field for field in required_fields if field not in signal_data]
        
        if missing_fields:
            errors.append(f"Missing required fields: {missing_fields}")
            return False
            
        return True
    
    def _validate_symbol(self, symbol: str, errors: List[str], warnings: List[str]):
        """Validate trading symbol."""
        if not symbol or not isinstance(symbol, str):
            errors.append("Symbol must be a non-empty string")
            return
            
        symbol = symbol.strip().upper()
        if len(symbol) == 0:
            errors.append("Symbol cannot be empty")
            return
            
        # Check for valid symbols
        valid_symbols = self.valid_crypto_symbols + self.valid_stock_symbols
        if symbol not in valid_symbols:
            warnings.append(f"Symbol '{symbol}' not in known valid symbols list")
    
    def _validate_side(self, side: str, errors: List[str]):
        """Validate trade side."""
        if not side or not isinstance(side, str):
            errors.append("Side must be a non-empty string")
            return
            
        valid_sides = ['buy', 'sell']
        if side.lower() not in valid_sides:
            errors.append(f"Invalid trade side: {side}. Must be one of {valid_sides}")
    
    def _validate_quantity(self, quantity: Any, errors: List[str], warnings: List[str]):
        """Validate quantity."""
        if not isinstance(quantity, (int, float)):
            errors.append(f"Quantity must be a number, got {type(quantity)}")
            return
            
        if quantity <= 0:
            errors.append(f"Quantity must be positive, got {quantity}")
            return
            
        if quantity < self.min_quantity:
            errors.append(f"Quantity too small: {quantity} < {self.min_quantity}")
            
        if quantity > self.max_quantity:
            errors.append(f"Quantity too large: {quantity} > {self.max_quantity}")
    
    def _validate_price(self, price: Any, errors: List[str], warnings: List[str]):
        """Validate price."""
        if not isinstance(price, (int, float)):
            errors.append(f"Price must be a number, got {type(price)}")
            return
            
        if price <= 0:
            errors.append(f"Price must be positive, got {price}")
            return
            
        if price < self.min_price:
            errors.append(f"Price too small: {price} < {self.min_price}")
            
        if price > self.max_price:
            errors.append(f"Price too large: {price} > {self.max_price}")
    
    def _validate_order_type(self, order_type: str, errors: List[str]):
        """Validate order type."""
        if not order_type or not isinstance(order_type, str):
            errors.append("Order type must be a non-empty string")
            return
            
        valid_types = ['market', 'limit']
        if order_type.lower() not in valid_types:
            errors.append(f"Invalid order type: {order_type}. Must be one of {valid_types}")
    
    def _validate_strategy(self, strategy: str, errors: List[str]):
        """Validate strategy name."""
        if not strategy or not isinstance(strategy, str):
            errors.append("Strategy must be a non-empty string")
            return
            
        if len(strategy.strip()) == 0:
            errors.append("Strategy cannot be empty")
    
    def _validate_notional(self, quantity: float, price: float, errors: List[str], warnings: List[str]):
        """Validate notional value."""
        notional = quantity * price
        if notional > self.max_notional:
            errors.append(f"Notional value too large: {notional} > {self.max_notional}")
        elif notional > self.max_notional * 0.8:
            warnings.append(f"Large notional value: {notional}")
    
    def _validate_symbol_consistency(self, symbol: str, side: str, quantity: float, price: float, 
                                   errors: List[str], warnings: List[str]):
        """Validate consistency between symbol and other fields."""
        # Add symbol-specific validations here
        pass
    
    def _validate_production_constraints(self, signal_data: Dict[str, Any], errors: List[str], warnings: List[str]):
        """Apply production-level constraints."""
        # Add production-specific validations here
        pass
    
    def get_validation_summary(self, result: ValidationResult) -> str:
        """Get a human-readable validation summary."""
        if result.is_valid:
            summary = "✅ Signal validation PASSED"
            if result.warnings:
                summary += f" (with {len(result.warnings)} warnings)"
        else:
            summary = f"❌ Signal validation FAILED with {len(result.errors)} errors"
            
        return summary

# Global validator instance
signal_validator = SignalValidator(ValidationLevel.PRODUCTION) 