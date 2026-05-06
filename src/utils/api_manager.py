"""
API Manager
----------
This module implements the API management system.
"""

import os
import time
import logging
from typing import Dict, Optional, Any, List
from dotenv import load_dotenv
import requests
from functools import wraps
import aiohttp
import asyncio
from datetime import datetime, timedelta
from src.utils.logger import get_logger
from src.core.event_bus import EventBus
from src.core.base_service import BaseService
from src.config import TradingConfig

# Load configuration
config = TradingConfig.load_from_file()

logger = get_logger(__name__)

class CircuitBreaker:
    """Circuit breaker for API requests."""
    
    def __init__(self, failure_threshold: int = 5, reset_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failures = 0
        self.last_failure_time: Optional[datetime] = None
        self.state = "closed"  # closed, open, half-open

    @property
    def failure_count(self) -> int:
        return self.failures

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def reset(self):
        self.failures = 0
        self.last_failure_time = None
        self.state = "closed"
        
    def record_failure(self):
        """Record a failure and update circuit breaker state."""
        self.failures += 1
        self.last_failure_time = datetime.now()
        
        if self.failures >= self.failure_threshold:
            self.state = "open"
            logger.warning("Circuit breaker opened due to excessive failures")
            
    def record_success(self):
        """Record a success and reset circuit breaker."""
        self.failures = 0
        self.state = "closed"
        
    def can_make_request(self) -> bool:
        """Check if a request can be made."""
        if self.state == "closed":
            return True
            
        if self.state == "open":
            if self.last_failure_time and \
               (datetime.now() - self.last_failure_time).total_seconds() > self.reset_timeout:
                self.state = "half-open"
                return True
            return False
            
        return True  # half-open state

class APIManager(BaseService):
    """Enhanced API manager with retry logic and circuit breaker."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the API manager.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        event_bus = config if isinstance(config, EventBus) else EventBus()
        super().__init__(event_bus)
        self.config = self._load_config(None if isinstance(config, EventBus) else config)
        self.logger = logging.getLogger("APIManager")
        self.connections = {}
        self.session: Optional[aiohttp.ClientSession] = None
        self.circuit_breaker = CircuitBreaker()
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}
        self.default_retry_config = {
            "max_retries": 3,
            "initial_delay": 1,
            "max_delay": 10,
            "exponential_base": 2
        }
        load_dotenv()
        self._rate_limits: Dict[str, Dict[str, float]] = {}
        self._api_keys: Dict[str, str] = {}
        self._load_api_keys()

    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate API configuration."""
        default_config = {
            'api': {
                'timeout': 30,
                'max_retries': 3,
                'retry_delay': 5,
                'rate_limit': 100
            },
            'connections': {
                'max_connections': 10,
                'connection_timeout': 60,
                'keep_alive': True
            },
            'security': {
                'api_key': os.getenv('API_KEY', ''),
                'api_secret': os.getenv('API_SECRET', ''),
                'encryption': True
            }
        }
        
        if isinstance(config, dict):
            default_config.update(config)
        return default_config

    def _load_api_keys(self):
        """Load and map all API keys from environment variables"""
        env_key_mapping = {
            'ALPACA_API_KEY': 'alpaca_api',
            'ALPACA_SECRET_KEY': 'alpaca_secret',
            'COINMARKETCAP_API_KEY': 'coinmarketcap',
            'CRYPTOPANIC_API_KEY': 'cryptopanic',
            'NEWSAPI_API_KEY': 'newsapi',
            'FINNHUB_API_KEY': 'finnhub'
        }

        for env_var, internal_key in env_key_mapping.items():
            key = os.getenv(env_var)
            if not key:
                logger.warning(f"Missing API key: {env_var}")
                continue
            self._api_keys[internal_key] = key

    def get_api_key(self, service: str) -> str:
        """
        Retrieve API key for a service.
        Valid service values: 'alpaca_api', 'alpaca_secret', 'coinmarketcap', etc.
        """
        normalized_service = service.lower().strip()
        if normalized_service not in self._api_keys:
            raise ValueError(f"Unknown service '{service}'. Available: {list(self._api_keys.keys())}")
        return self._api_keys[normalized_service]

    def rate_limit(self, service: str, calls_per_minute: int):
        """Decorator to rate-limit API calls per service"""
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                current_time = time.time()
                if service not in self._rate_limits:
                    self._rate_limits[service] = {'last_call': 0, 'calls': 0}

                rate_info = self._rate_limits[service]
                time_since_last_call = current_time - rate_info['last_call']

                if time_since_last_call < 60:
                    if rate_info['calls'] >= calls_per_minute:
                        sleep_time = 60 - time_since_last_call
                        print(f"[Rate Limit] Sleeping {sleep_time:.2f}s for service: {service}")
                        time.sleep(sleep_time)
                        rate_info['calls'] = 0
                else:
                    rate_info['calls'] = 0

                rate_info['last_call'] = time.time()
                rate_info['calls'] += 1

                return func(*args, **kwargs)
            return wrapper
        return decorator

    async def _initialize(self):
        """Initialize API manager."""
        self.session = aiohttp.ClientSession()
        logger.info("API manager initialized")
        
    async def _shutdown(self):
        """Shutdown API manager."""
        if self.session:
            await self.session.close()
        self.session = None
        logger.info("API manager shut down")
        
    async def make_request(
        self,
        method: str,
        url: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retry_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Make an API request with retry logic and circuit breaker."""
        if not self.session:
            raise RuntimeError("API manager not initialized")
            
        # Get circuit breaker for endpoint
        if endpoint not in self.circuit_breakers:
            self.circuit_breakers[endpoint] = CircuitBreaker()
        circuit_breaker = self.circuit_breakers[endpoint]
        
        # Check circuit breaker
        if not circuit_breaker.can_make_request():
            raise RuntimeError(f"Circuit breaker open for endpoint: {endpoint}")
            
        # Merge retry config with defaults
        config = {**self.default_retry_config, **(retry_config or {})}
        
        for attempt in range(config["max_retries"] + 1):
            try:
                async with self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=data,
                    headers=headers
                ) as response:
                    if response.status >= 500:
                        raise aiohttp.ClientError(f"Server error: {response.status}")
                        
                    result = await response.json()
                    
                    # Record success
                    circuit_breaker.record_success()
                    
                    # Update health metrics
                    await self.health.update_metric(
                        f"api_{endpoint}_success",
                        self.health.metrics.get(f"api_{endpoint}_success", 0) + 1
                    )
                    
                    return result
                    
            except Exception as e:
                # Record failure
                circuit_breaker.record_failure()
                
                # Update health metrics
                await self.health.update_metric(
                    f"api_{endpoint}_failures",
                    self.health.metrics.get(f"api_{endpoint}_failures", 0) + 1
                )
                
                if attempt == config["max_retries"]:
                    logger.error(f"API request failed after {config['max_retries']} retries: {e}")
                    raise
                    
                # Calculate delay with exponential backoff
                delay = min(
                    config["initial_delay"] * (config["exponential_base"] ** attempt),
                    config["max_delay"]
                )
                
                logger.warning(f"API request failed, retrying in {delay}s: {e}")
                await asyncio.sleep(delay)
                
        raise RuntimeError("Should not reach here")

    async def _check_health(self):
        """Check API health status."""
        try:
            # Check circuit breaker status
            for endpoint, cb in self.circuit_breakers.items():
                if cb.state == "open":
                    await self.health.increment_warning()
                    logger.warning(f"Circuit breaker open for endpoint: {endpoint}")
                    
            # Check session health
            if not self.session or self.session.closed:
                await self.health.update_status("error")
                await self.health.increment_error()
                logger.error("API session is closed")
                
        except Exception as e:
            await self.health.increment_error()
            logger.error(f"Health check failed: {e}")
            
    def get_circuit_breaker_status(self) -> Dict[str, Any]:
        """Get circuit breaker status for all endpoints."""
        return {
            endpoint: {
                "state": cb.state,
                "failures": cb.failures,
                "last_failure": cb.last_failure_time.isoformat() if cb.last_failure_time else None
            }
            for endpoint, cb in self.circuit_breakers.items()
        }

# Create singleton instance
api_manager = APIManager()
