"""
Logger
------
This module implements the logging system.
"""

import logging
import logging.handlers
import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from pathlib import Path
from src.config import TradingConfig

# Load configuration
config = TradingConfig.load_from_file()

class StructuredLogger(logging.Logger):
    """Custom logger that supports structured logging."""
    
    def _log(self, level, msg, args, exc_info=None, extra=None, **kwargs):
        if extra is None:
            extra = {}
            
        # Add structured data
        structured_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": logging.getLevelName(level),
            "message": msg,
            **extra
        }
        
        # Add any additional kwargs
        structured_data.update(kwargs)
        
        # Convert to JSON string
        msg = json.dumps(structured_data)
        super()._log(level, msg, args, exc_info, extra)

def setup_logger(name: str, log_level: int = logging.INFO) -> StructuredLogger:
    """Setup a structured logger with file and console handlers."""
    
    # Create logs directory if it doesn't exist
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # Create logger
    logging.setLoggerClass(StructuredLogger)
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    
    # Clear existing handlers
    logger.handlers.clear()
    
    # Create formatters
    console_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # Create file handler with rotation
    log_file = log_dir / f"{name}.log"
    try:
        # Try to create/touch the file first with permissive mode
        log_file.touch(mode=0o666, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(log_file),  # Convert Path to string
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            mode='a',  # Append mode
        )
        file_handler.setFormatter(console_formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        # If file creation fails, log to console only
        logger.warning(f"Could not create log file {log_file}: {e}")
    
    return logger

class LogManager:
    """Manager for system-wide logging configuration."""
    
    def __init__(self):
        self.loggers: Dict[str, StructuredLogger] = {}
        self.default_level = logging.INFO
        
    def get_logger(self, name: str) -> StructuredLogger:
        """Get or create a logger with the given name."""
        if name not in self.loggers:
            self.loggers[name] = setup_logger(name, self.default_level)
        return self.loggers[name]
    
    def set_level(self, level: int):
        """Set the logging level for all managed loggers."""
        self.default_level = level
        for logger in self.loggers.values():
            logger.setLevel(level)
            
    def add_handler(self, handler: logging.Handler):
        """Add a handler to all managed loggers."""
        for logger in self.loggers.values():
            logger.addHandler(handler)
            
    def remove_handler(self, handler: logging.Handler):
        """Remove a handler from all managed loggers."""
        for logger in self.loggers.values():
            logger.removeHandler(handler)
            
    def clear_handlers(self):
        """Clear all handlers from all managed loggers."""
        for logger in self.loggers.values():
            logger.handlers.clear()
            
    def get_logger_names(self) -> list:
        """Get names of all managed loggers."""
        return list(self.loggers.keys())
        
# Global log manager instance
log_manager = LogManager()

def get_logger(name: str) -> StructuredLogger:
    """Get a logger from the global log manager."""
    return log_manager.get_logger(name)

# Global logger instance for direct import
logger = get_logger("default")

def debug(self, message: str, extra: Optional[dict] = None):
    self.logger.debug(message, extra=extra)
    
def info(self, message: str, extra: Optional[dict] = None):
    self.logger.info(message, extra=extra)
    
def warning(self, message: str, extra: Optional[dict] = None):
    self.logger.warning(message, extra=extra)
    
def error(self, message: str, extra: Optional[dict] = None):
    self.logger.error(message, extra=extra)
    
def critical(self, message: str, extra: Optional[dict] = None):
    self.logger.critical(message, extra=extra)
    
def log_trade(self, symbol: str, qty: float, side: str, price: float, 
             simulated: bool = False, extra: Optional[dict] = None):
    """Log trade execution with additional context"""
    trade_type = "SIMULATED" if simulated else "LIVE"
    message = f"{trade_type} {side.upper()} {qty} {symbol} @ ${price}"
    self.info(message, extra=extra)
    
def log_api_call(self, service: str, endpoint: str, status: str, 
                response_time: float, extra: Optional[dict] = None):
    """Log API calls with performance metrics"""
    message = f"API Call - {service}: {endpoint} - Status: {status} - Response Time: {response_time:.2f}s"
    self.debug(message, extra=extra)
    
def log_system_health(self, component: str, status: str, 
                     metrics: Optional[dict] = None, extra: Optional[dict] = None):
    """Log system health metrics"""
    message = f"System Health - {component}: {status}"
    if metrics:
        message += f" - Metrics: {metrics}"
    self.info(message, extra=extra)

class Logger:
    """Logging system for managing application logs."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the logger.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        self.config = self._load_config(config)
        self.logger = logging.getLogger("Logger")
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate logging configuration."""
        default_config = {
            'logging': {
                'level': config.LOG_LEVEL,
                'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                'date_format': '%Y-%m-%d %H:%M:%S'
            },
            'file': {
                'enabled': True,
                'path': 'logs',
                'max_size': 10485760,
                'backup_count': 5
            },
            'console': {
                'enabled': True,
                'level': 'INFO'
            }
        }
        
        if config:
            default_config.update(config)
        return default_config 