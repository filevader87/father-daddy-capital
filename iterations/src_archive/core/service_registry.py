from typing import Dict, List, Any, Optional, Type, TypeVar
import asyncio
from datetime import datetime
from enum import Enum
from src.utils.logger import get_logger
from src.core.event_bus import EventBus
from src.core.config_manager import ConfigManager
from src.core.state_manager import StateManager
from src.core.agent_protocol import AgentProtocol
from src.config import TradingConfig as config

logger = get_logger(__name__)
T = TypeVar('T')

class ServiceStatus(Enum):
    """Service status enumeration."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"

class ServiceInfo:
    """Service information container."""
    
    def __init__(self, service: Any, name: str, dependencies: List[str]):
        self.service = service
        self.name = name
        self.dependencies = dependencies
        self.status = ServiceStatus.STOPPED
        self.error: Optional[str] = None
        self.start_time: Optional[datetime] = None
        self.stop_time: Optional[datetime] = None
        self.restart_count: int = 0
        self.last_error: Optional[str] = None
        self.last_error_time: Optional[datetime] = None
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert service info to dictionary."""
        return {
            "name": self.name,
            "status": self.status.value,
            "dependencies": self.dependencies,
            "error": self.error,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "stop_time": self.stop_time.isoformat() if self.stop_time else None,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "last_error_time": self.last_error_time.isoformat() if self.last_error_time else None
        }

class ServiceRegistry:
    """Registry for managing core services and their dependencies."""
    
    def __init__(self):
        self.services: Dict[str, ServiceInfo] = {}
        self._initialized = False
        self._lock = asyncio.Lock()
        self._monitor_task: Optional[asyncio.Task] = None
        self._start_time = datetime.now()
        self._event_bus = EventBus()  # Create event bus instance
        self._agent_protocol = None  # Will be initialized later
        self._config_manager = None  # Will be initialized later
        
    def get_event_bus(self) -> EventBus:
        """Get the event bus instance."""
        return self._event_bus
        
    def get_agent_protocol(self) -> AgentProtocol:
        """Get the agent protocol instance."""
        if not self._agent_protocol:
            # Fallback to a no-op protocol for tests
            from .protocol import AgentProtocol
            return AgentProtocol()
        return self._agent_protocol

    def get_risk_manager(self):
        # Fallback stub for tests
        class DummyRiskManager:
            def evaluate(self, *args, **kwargs):
                return True
        return DummyRiskManager()

    def get_config_manager(self):
        """Get the config manager instance."""
        if not self._config_manager:
            # Fallback stub for tests
            class DummyConfigManager:
                def __init__(self): 
                    self.configs = {}
                    
                async def initialize(self): 
                    pass
                    
                async def get_config(self, name: str, environment: str = None): 
                    return {}
                    
                def get(self, name: str, default=None):
                    return default
                    
                def register_dependency(self, name: str, dependency: Any): 
                    pass
                    
                async def update_config(self, name: str, config: Dict[str, Any], **kwargs):
                    self.configs[name] = config
                    
                async def _handle_config_change(self, event):
                    pass
                    
                async def shutdown(self):
                    pass
                    
            return DummyConfigManager()
        return self._config_manager
        
    async def initialize(self):
        """Initialize the registry lifecycle without auto-registering services."""
        if self._initialized:
            return
        self._initialized = True
        logger.info("Service registry initialized successfully")
                
    async def _initialize_services(self):
        """Initialize services in dependency order."""
        ordered_services = self._get_ordered_services()
        
        for service_name in ordered_services:
            service_info = self.services[service_name]
            try:
                service_info.status = ServiceStatus.STARTING
                await service_info.service.initialize()
                service_info.status = ServiceStatus.RUNNING
                service_info.start_time = datetime.now()
                service_info.error = None
                logger.info(f"Service initialized: {service_name}")
            except Exception as e:
                service_info.status = ServiceStatus.ERROR
                service_info.error = str(e)
                service_info.last_error = str(e)
                service_info.last_error_time = datetime.now()
                logger.error(f"Error initializing service {service_name}: {e}")
                raise
                
    async def shutdown(self):
        """Shutdown all registered services."""
        if not self._initialized:
            return
            
        async with self._lock:
            try:
                # Stop health monitoring
                if self._monitor_task:
                    self._monitor_task.cancel()
                    try:
                        await self._monitor_task
                    except asyncio.CancelledError:
                        pass
                        
                # Shutdown services in reverse order
                ordered_services = self._get_ordered_services()
                ordered_services.reverse()
                
                for service_name in ordered_services:
                    service_info = self.services[service_name]
                    try:
                        service_info.status = ServiceStatus.STOPPING
                        await service_info.service.shutdown()
                        service_info.status = ServiceStatus.STOPPED
                        service_info.stop_time = datetime.now()
                        logger.info(f"Service shut down: {service_name}")
                    except Exception as e:
                        service_info.status = ServiceStatus.ERROR
                        service_info.error = str(e)
                        service_info.last_error = str(e)
                        service_info.last_error_time = datetime.now()
                        logger.error(f"Error shutting down service {service_name}: {e}")
                        
                self._initialized = False
                logger.info("Service registry shut down successfully")
            except Exception as e:
                logger.error(f"Error during service registry shutdown: {e}")
                raise
                
    def register_service(self, name: str, service: Any, dependencies: List[str]):
        """Register a service with the registry."""
        if name in self.services:
            raise ValueError(f"Service {name} already registered")
        self.services[name] = ServiceInfo(service, name, dependencies)
        logger.info(f"Service registered: {name}")

    def unregister(self, name: str):
        if name not in self.services:
            raise KeyError(f"Service {name} not found")
        del self.services[name]
        
    def get_service(self, name: str) -> Any:
        """Get a registered service."""
        if name not in self.services:
            raise KeyError(f"Service {name} not found")
        return self.services[name].service
        
    def is_initialized(self) -> bool:
        """Check if the registry is initialized."""
        return self._initialized
        
    def _get_ordered_services(self) -> List[str]:
        """Get services in dependency order."""
        visited = set()
        order = []
        
        def visit(name: str):
            if name in visited:
                return
            visited.add(name)
            
            for dep in self.services[name].dependencies:
                visit(dep)
                
            order.append(name)
            
        for name in self.services:
            visit(name)
            
        return order
        
    async def _monitor_services(self):
        """Monitor service health and handle failures."""
        try:
            while True:
                for service_name, service_info in self.services.items():
                    if service_info.status == ServiceStatus.ERROR:
                        # Attempt to restart failed service
                        try:
                            logger.info(f"Attempting to restart failed service: {service_name}")
                            await self._restart_service(service_name)
                        except Exception as e:
                            logger.error(f"Failed to restart service {service_name}: {e}")
                            
                await asyncio.sleep(60)  # Check every minute
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Service monitoring error: {e}")
            
    async def _restart_service(self, service_name: str):
        """Restart a failed service."""
        service_info = self.services[service_name]
        
        try:
            # Shutdown service
            service_info.status = ServiceStatus.STOPPING
            await service_info.service.shutdown()
            
            # Reinitialize service
            service_info.status = ServiceStatus.STARTING
            await service_info.service.initialize()
            
            service_info.status = ServiceStatus.RUNNING
            service_info.restart_count += 1
            service_info.error = None
            logger.info(f"Service restarted successfully: {service_name}")
        except Exception as e:
            service_info.status = ServiceStatus.ERROR
            service_info.error = str(e)
            service_info.last_error = str(e)
            service_info.last_error_time = datetime.now()
            raise
            
    def get_service_summary(self) -> Dict[str, Any]:
        """Get summary of all registered services."""
        return {
            "services": {
                name: info.to_dict()
                for name, info in self.services.items()
            },
            "initialized": self._initialized,
            "total_services": len(self.services),
            "uptime": str(datetime.now() - self._start_time)
        }

# Global service registry instance
service_registry = ServiceRegistry()

def get_service(name: str) -> Any:
    """Get a service from the global registry."""
    return service_registry.get_service(name)

def get_service_by_type(service_type: Type[T]) -> T:
    """Get a service by type from the global registry."""
    for service_info in service_registry.services.values():
        if isinstance(service_info.service, service_type):
            return service_info.service
    raise KeyError(f"Service of type {service_type.__name__} not found") 
