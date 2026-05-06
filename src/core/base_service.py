from typing import Dict, Any, Optional
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
import asyncio
from src.core.event_bus import EventBus
from src.utils.logger import get_logger

logger = get_logger(__name__)

class ServiceHealth:
    """Service health status and metrics."""
    
    def __init__(self):
        self.status: str = "unknown"
        self.last_check: datetime = datetime.now()
        self.error_count: int = 0
        self.warning_count: int = 0
        self.uptime: timedelta = timedelta()
        self.metrics: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        
    async def update_status(self, status: str):
        """Update service status."""
        async with self._lock:
            self.status = status
            self.last_check = datetime.now()
            
    async def increment_error(self):
        """Increment error count."""
        async with self._lock:
            self.error_count += 1
            
    async def increment_warning(self):
        """Increment warning count."""
        async with self._lock:
            self.warning_count += 1
            
    async def update_metric(self, name: str, value: Any):
        """Update a metric value."""
        async with self._lock:
            self.metrics[name] = value
            
    def to_dict(self) -> Dict[str, Any]:
        """Convert health status to dictionary."""
        return {
            "status": self.status,
            "last_check": self.last_check.isoformat(),
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "uptime": str(self.uptime),
            "metrics": self.metrics
        }

class BaseService(ABC):
    """Base class for all core services providing common functionality."""
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._initialized = False
        self._dependencies: Dict[str, Any] = {}
        self.health = ServiceHealth()
        self._start_time = datetime.now()
        self._monitor_task: Optional[asyncio.Task] = None
        
    async def initialize(self):
        """Initialize the service and its dependencies."""
        if self._initialized:
            return
            
        try:
            # Initialize dependencies first
            for dep in self._dependencies.values():
                if hasattr(dep, 'initialize'):
                    await dep.initialize()
                    
            # Initialize the service
            await self._initialize()
            self._initialized = True
            
            # Start health monitoring
            self._monitor_task = asyncio.create_task(self._monitor_health())
            
            await self.health.update_status("running")
            logger.info(f"{self.__class__.__name__} initialized successfully")
        except Exception as e:
            await self.health.update_status("error")
            await self.health.increment_error()
            logger.error(f"Failed to initialize {self.__class__.__name__}: {e}")
            raise
            
    @abstractmethod
    async def _initialize(self):
        """Service-specific initialization logic."""
        pass
        
    def register_dependency(self, name: str, dependency: Any):
        """Register a service dependency."""
        self._dependencies[name] = dependency
        
    def get_dependency(self, name: str) -> Any:
        """Get a registered dependency."""
        if name not in self._dependencies:
            raise ValueError(f"Dependency {name} not found")
        return self._dependencies[name]
        
    async def shutdown(self):
        """Shutdown the service and its dependencies."""
        try:
            # Stop health monitoring
            if self._monitor_task:
                self._monitor_task.cancel()
                try:
                    await self._monitor_task
                except asyncio.CancelledError:
                    pass
                    
            await self._shutdown()
            self._initialized = False
            await self.health.update_status("stopped")
            logger.info(f"{self.__class__.__name__} shut down successfully")
        except Exception as e:
            await self.health.update_status("error")
            await self.health.increment_error()
            logger.error(f"Error shutting down {self.__class__.__name__}: {e}")
            raise
            
    async def _shutdown(self):
        """Service-specific shutdown logic."""
        pass
        
    def is_initialized(self) -> bool:
        """Check if the service is initialized."""
        return self._initialized
        
    async def _monitor_health(self):
        """Monitor service health."""
        try:
            while True:
                # Update uptime
                self.health.uptime = datetime.now() - self._start_time
                
                # Check dependencies
                for name, dep in self._dependencies.items():
                    if hasattr(dep, 'health'):
                        dep_health = dep.health
                        if dep_health.status == "error":
                            await self.health.increment_warning()
                            logger.warning(f"Dependency {name} is in error state")
                            
                # Service-specific health check
                await self._check_health()
                
                # Publish health status
                await self.event_bus.publish(
                    "service_health",
                    {
                        "service": self.__class__.__name__,
                        "health": self.health.to_dict()
                    },
                    "health_monitor"
                )
                
                await asyncio.sleep(60)  # Check every minute
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await self.health.increment_error()
            logger.error(f"Health monitoring error: {e}")
            
    async def _check_health(self):
        """Service-specific health check."""
        pass
        
    def get_health_status(self) -> Dict[str, Any]:
        """Get current health status."""
        return self.health.to_dict() 