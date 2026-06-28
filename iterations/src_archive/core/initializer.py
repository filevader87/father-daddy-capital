from typing import Dict, List, Type, Any
from dataclasses import dataclass
import logging
from enum import Enum, auto

class ServiceStatus(Enum):
    """Service initialization status."""
    PENDING = auto()
    INITIALIZING = auto()
    RUNNING = auto()
    ERROR = auto()
    STOPPED = auto()

@dataclass
class ServiceInfo:
    """Information about a service."""
    service_type: Type
    dependencies: List[str]
    priority: int
    status: ServiceStatus = ServiceStatus.PENDING
    instance: Any = None
    error: Exception = None

class SystemInitializer:
    """Manages system initialization and service lifecycle."""
    
    def __init__(self):
        self.services: Dict[str, ServiceInfo] = {}
        self.logger = logging.getLogger(__name__)
        self.initialized = False
        
    def register_service(self,
                        name: str,
                        service_type: Type,
                        dependencies: List[str] = None,
                        priority: int = 0):
        """Register a service with its dependencies."""
        if name in self.services:
            raise ValueError(f"Service {name} already registered")
            
        self.services[name] = ServiceInfo(
            service_type=service_type,
            dependencies=dependencies or [],
            priority=priority
        )
        
    async def initialize(self):
        """Initialize all services in the correct order."""
        if self.initialized:
            return
            
        # Sort services by priority and dependencies
        ordered_services = self._get_ordered_services()
        
        # Initialize services
        for service_name in ordered_services:
            service_info = self.services[service_name]
            try:
                await self._initialize_service(service_name, service_info)
            except Exception as e:
                self.logger.error(f"Error initializing {service_name}: {e}")
                service_info.status = ServiceStatus.ERROR
                service_info.error = e
                raise
                
        self.initialized = True
        self.logger.info("System initialization completed")
        
    def _get_ordered_services(self) -> List[str]:
        """Get services in initialization order."""
        # Create dependency graph
        graph = {
            name: set(info.dependencies)
            for name, info in self.services.items()
        }
        
        # Topological sort with priority
        ordered = []
        visited = set()
        
        def visit(name):
            if name in visited:
                return
            visited.add(name)
            
            for dep in graph[name]:
                visit(dep)
                
            ordered.append(name)
            
        # Visit services in priority order
        priority_order = sorted(
            self.services.keys(),
            key=lambda x: self.services[x].priority,
            reverse=True
        )
        
        for service in priority_order:
            visit(service)
            
        return ordered
        
    async def _initialize_service(self, name: str, info: ServiceInfo):
        """Initialize a single service."""
        self.logger.info(f"Initializing service: {name}")
        info.status = ServiceStatus.INITIALIZING
        
        # Resolve dependencies
        dependencies = {}
        for dep_name in info.dependencies:
            dep_info = self.services[dep_name]
            if dep_info.status != ServiceStatus.RUNNING:
                raise RuntimeError(
                    f"Dependency {dep_name} not running for service {name}"
                )
            dependencies[dep_name] = dep_info.instance
            
        # Create and initialize service
        try:
            instance = info.service_type(**dependencies)
            if hasattr(instance, 'initialize'):
                await instance.initialize()
            info.instance = instance
            info.status = ServiceStatus.RUNNING
            self.logger.info(f"Service {name} initialized successfully")
        except Exception as e:
            self.logger.error(f"Error initializing {name}: {e}")
            info.status = ServiceStatus.ERROR
            info.error = e
            raise
            
    async def shutdown(self):
        """Shutdown all services in reverse order."""
        if not self.initialized:
            return
            
        # Get services in reverse initialization order
        ordered_services = reversed(self._get_ordered_services())
        
        for service_name in ordered_services:
            service_info = self.services[service_name]
            if service_info.status == ServiceStatus.RUNNING:
                try:
                    await self._shutdown_service(service_name, service_info)
                except Exception as e:
                    self.logger.error(f"Error shutting down {service_name}: {e}")
                    
        self.initialized = False
        self.logger.info("System shutdown completed")
        
    async def _shutdown_service(self, name: str, info: ServiceInfo):
        """Shutdown a single service."""
        self.logger.info(f"Shutting down service: {name}")
        
        try:
            if hasattr(info.instance, 'shutdown'):
                await info.instance.shutdown()
            info.status = ServiceStatus.STOPPED
            self.logger.info(f"Service {name} shut down successfully")
        except Exception as e:
            self.logger.error(f"Error shutting down {name}: {e}")
            raise
            
    def get_service_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all services."""
        return {
            name: {
                'status': info.status.name,
                'dependencies': info.dependencies,
                'priority': info.priority,
                'error': str(info.error) if info.error else None
            }
            for name, info in self.services.items()
        }
        
    def get_service(self, name: str) -> Any:
        """Get a service instance by name."""
        if name not in self.services:
            raise KeyError(f"Service {name} not found")
            
        info = self.services[name]
        if info.status != ServiceStatus.RUNNING:
            raise RuntimeError(f"Service {name} is not running")
            
        return info.instance 