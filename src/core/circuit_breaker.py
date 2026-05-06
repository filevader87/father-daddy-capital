from typing import Optional, Callable, Dict, Any
from enum import Enum
import asyncio
from datetime import datetime, timedelta
from dataclasses import dataclass
from src.utils.logger import get_logger

logger = get_logger(__name__)

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Circuit is open, requests fail fast
    HALF_OPEN = "half_open"  # Testing if service has recovered

@dataclass
class CircuitMetrics:
    """Circuit breaker metrics."""
    failure_count: int = 0
    success_count: int = 0
    total_requests: int = 0
    last_failure_time: Optional[datetime] = None
    last_success_time: Optional[datetime] = None
    total_failure_duration: timedelta = timedelta(0)
    consecutive_failures: int = 0

class CircuitBreaker:
    """Advanced circuit breaker implementation."""
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: timedelta = timedelta(seconds=30),
        half_open_requests: int = 3,
        error_threshold: float = 0.5,
        metrics_window: timedelta = timedelta(minutes=5)
    ):
        self.name = name
        self.state = CircuitState.CLOSED
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_requests = half_open_requests
        self.error_threshold = error_threshold
        self.metrics_window = metrics_window
        
        self.metrics = CircuitMetrics()
        self.last_state_change = datetime.now()
        self.half_open_attempts = 0
        self._lock = asyncio.Lock()
        self._recovery_task: Optional[asyncio.Task] = None
        
    async def execute(self, func: Callable, *args, **kwargs) -> Any:
        """Execute a function with circuit breaker protection."""
        async with self._lock:
            if self.state == CircuitState.OPEN:
                if datetime.now() - self.last_state_change > self.recovery_timeout:
                    await self._attempt_recovery()
                else:
                    raise CircuitBreakerError(f"Circuit {self.name} is open")
                    
            try:
                result = await func(*args, **kwargs)
                await self._record_success()
                return result
            except Exception as e:
                await self._record_failure()
                raise CircuitBreakerError(f"Circuit {self.name} failure: {str(e)}") from e
                
    async def _record_success(self):
        """Record a successful operation."""
        self.metrics.success_count += 1
        self.metrics.total_requests += 1
        self.metrics.last_success_time = datetime.now()
        self.metrics.consecutive_failures = 0
        
        if self.state == CircuitState.HALF_OPEN:
            self.half_open_attempts += 1
            if self.half_open_attempts >= self.half_open_requests:
                await self._close_circuit()
                
    async def _record_failure(self):
        """Record a failed operation."""
        self.metrics.failure_count += 1
        self.metrics.total_requests += 1
        self.metrics.last_failure_time = datetime.now()
        self.metrics.consecutive_failures += 1
        self.metrics.total_failure_duration += datetime.now() - self.last_state_change
        
        if (self.metrics.consecutive_failures >= self.failure_threshold or
            self._calculate_error_rate() > self.error_threshold):
            await self._open_circuit()
            
    async def _open_circuit(self):
        """Open the circuit breaker."""
        if self.state != CircuitState.OPEN:
            self.state = CircuitState.OPEN
            self.last_state_change = datetime.now()
            self.half_open_attempts = 0
            logger.warning(f"Circuit {self.name} opened")
            
            # Schedule recovery attempt
            if self._recovery_task is None or self._recovery_task.done():
                self._recovery_task = asyncio.create_task(self._schedule_recovery())
                
    async def _close_circuit(self):
        """Close the circuit breaker."""
        if self.state != CircuitState.CLOSED:
            self.state = CircuitState.CLOSED
            self.last_state_change = datetime.now()
            self.half_open_attempts = 0
            logger.info(f"Circuit {self.name} closed")
            
    async def _attempt_recovery(self):
        """Attempt to recover the circuit."""
        self.state = CircuitState.HALF_OPEN
        self.last_state_change = datetime.now()
        self.half_open_attempts = 0
        logger.info(f"Circuit {self.name} in half-open state")
        
    async def _schedule_recovery(self):
        """Schedule a recovery attempt."""
        await asyncio.sleep(self.recovery_timeout.total_seconds())
        await self._attempt_recovery()
        
    def _calculate_error_rate(self) -> float:
        """Calculate the current error rate."""
        if self.metrics.total_requests == 0:
            return 0.0
        return self.metrics.failure_count / self.metrics.total_requests
        
    def get_metrics(self) -> Dict[str, Any]:
        """Get current circuit breaker metrics."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.metrics.failure_count,
            "success_count": self.metrics.success_count,
            "total_requests": self.metrics.total_requests,
            "error_rate": self._calculate_error_rate(),
            "last_failure_time": self.metrics.last_failure_time,
            "last_success_time": self.metrics.last_success_time,
            "consecutive_failures": self.metrics.consecutive_failures,
            "total_failure_duration": self.metrics.total_failure_duration.total_seconds(),
            "half_open_attempts": self.half_open_attempts
        }
        
    def reset_metrics(self):
        """Reset circuit breaker metrics."""
        self.metrics = CircuitMetrics()
        self.half_open_attempts = 0

class CircuitBreakerError(Exception):
    """Circuit breaker specific error."""
    pass 