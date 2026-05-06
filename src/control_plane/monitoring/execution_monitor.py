import logging
import logging.handlers
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from prometheus_client import Counter, Gauge, Histogram, Summary
import structlog
from functools import wraps
import time
import traceback

# Configure structured logging
logging.basicConfig(
    format="%(message)s",
    level=logging.INFO,
)

# Create logs directory if it doesn't exist
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

# Configure file handler with rotation
file_handler = logging.handlers.RotatingFileHandler(
    log_dir / "trading.log",
    maxBytes=10_000_000,  # 10MB
    backupCount=5
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

# Create structured logger
logger = structlog.get_logger()

# Prometheus metrics
EXECUTION_LATENCY = Histogram(
    'trade_execution_latency_seconds',
    'Time taken to execute trades',
    ['strategy', 'side']
)

EXECUTION_ERRORS = Counter(
    'trade_execution_errors_total',
    'Number of trade execution errors',
    ['strategy', 'error_type']
)

ORDER_VOLUME = Counter(
    'trade_order_volume_total',
    'Total trading volume',
    ['strategy', 'side']
)

POSITION_VALUE = Gauge(
    'position_value_current',
    'Current position value',
    ['strategy', 'symbol']
)

DRAWDOWN = Gauge(
    'portfolio_drawdown_current',
    'Current portfolio drawdown',
    ['strategy']
)

API_LATENCY = Summary(
    'api_latency_seconds',
    'API request latency',
    ['endpoint']
)

class ExecutionMonitor:
    """
    Monitors and logs trade execution with Prometheus metrics.
    """
    
    def __init__(self, strategy_name: str):
        """
        Initialize the execution monitor.
        
        Args:
            strategy_name: Name of the trading strategy
        """
        self.strategy_name = strategy_name
        self.logger = logger.bind(strategy=strategy_name)
        
    def log_trade(self,
                  side: str,
                  symbol: str,
                  quantity: float,
                  price: float,
                  order_type: str,
                  metadata: Optional[Dict[str, Any]] = None):
        """
        Log trade execution details.
        
        Args:
            side: Buy or Sell
            symbol: Trading symbol
            quantity: Trade quantity
            price: Execution price
            order_type: Type of order
            metadata: Additional trade metadata
        """
        trade_info = {
            'timestamp': datetime.utcnow().isoformat(),
            'side': side,
            'symbol': symbol,
            'quantity': quantity,
            'price': price,
            'order_type': order_type,
            'strategy': self.strategy_name
        }
        
        if metadata:
            trade_info.update(metadata)
            
        self.logger.info('trade_executed', **trade_info)
        
        # Update Prometheus metrics
        ORDER_VOLUME.labels(
            strategy=self.strategy_name,
            side=side
        ).inc(quantity * price)
        
        POSITION_VALUE.labels(
            strategy=self.strategy_name,
            symbol=symbol
        ).set(quantity * price)
        
    def log_error(self,
                  error_type: str,
                  error_msg: str,
                  metadata: Optional[Dict[str, Any]] = None):
        """
        Log execution errors.
        
        Args:
            error_type: Type of error
            error_msg: Error message
            metadata: Additional error context
        """
        error_info = {
            'timestamp': datetime.utcnow().isoformat(),
            'error_type': error_type,
            'error_message': error_msg,
            'stack_trace': traceback.format_exc()
        }
        
        if metadata:
            error_info.update(metadata)
            
        self.logger.error('execution_error', **error_info)
        
        # Update Prometheus metrics
        EXECUTION_ERRORS.labels(
            strategy=self.strategy_name,
            error_type=error_type
        ).inc()
        
    def log_portfolio_update(self,
                           total_value: float,
                           cash: float,
                           positions: Dict[str, Dict[str, float]]):
        """
        Log portfolio status updates.
        
        Args:
            total_value: Total portfolio value
            cash: Available cash
            positions: Current positions
        """
        portfolio_info = {
            'timestamp': datetime.utcnow().isoformat(),
            'total_value': total_value,
            'cash': cash,
            'positions': positions
        }
        
        self.logger.info('portfolio_update', **portfolio_info)
        
        # Update position values
        for symbol, pos_info in positions.items():
            POSITION_VALUE.labels(
                strategy=self.strategy_name,
                symbol=symbol
            ).set(pos_info['value'])
            
    def monitor_drawdown(self, current_value: float, peak_value: float):
        """
        Monitor and log portfolio drawdown.
        
        Args:
            current_value: Current portfolio value
            peak_value: Peak portfolio value
        """
        drawdown = (peak_value - current_value) / peak_value
        
        if drawdown > 0.1:  # Alert on 10% drawdown
            self.logger.warning('high_drawdown', 
                              drawdown=drawdown,
                              current_value=current_value,
                              peak_value=peak_value)
            
        DRAWDOWN.labels(strategy=self.strategy_name).set(drawdown)
        
    def monitor_api_call(self, endpoint: str):
        """
        Decorator to monitor API call latency.
        
        Args:
            endpoint: API endpoint name
        """
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = func(*args, **kwargs)
                    API_LATENCY.labels(endpoint=endpoint).observe(
                        time.time() - start_time
                    )
                    return result
                except Exception as e:
                    self.log_error(
                        'api_error',
                        str(e),
                        {'endpoint': endpoint}
                    )
                    raise
            return wrapper
        return decorator
        
def execution_timing(strategy: str, side: str):
    """
    Decorator to measure trade execution timing.
    
    Args:
        strategy: Strategy name
        side: Trade side (buy/sell)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with EXECUTION_LATENCY.labels(strategy=strategy, side=side).time():
                return func(*args, **kwargs)
        return wrapper
    return decorator 