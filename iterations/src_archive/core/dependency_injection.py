from typing import Dict, Type, Any, Optional, Callable
from dataclasses import dataclass
import inspect
from functools import wraps

@dataclass
class ServiceDefinition:
    """Definition of a service in the DI container."""
    service_type: Type
    implementation: Any
    singleton: bool = True
    dependencies: Dict[str, Type] = None
    initialization_order: int = 0

class DependencyContainer:
    """Dependency injection container."""
    
    def __init__(self):
        self._services: Dict[str, ServiceDefinition] = {}
        self._instances: Dict[str, Any] = {}
        self._initialized = False
        
    def register(self, 
                service_type: Type,
                implementation: Any = None,
                singleton: bool = True,
                dependencies: Dict[str, Type] = None,
                initialization_order: int = 0):
        """Register a service in the container."""
        if implementation is None:
            implementation = service_type
            
        self._services[service_type.__name__] = ServiceDefinition(
            service_type=service_type,
            implementation=implementation,
            singleton=singleton,
            dependencies=dependencies or {},
            initialization_order=initialization_order
        )
        
    def resolve(self, service_type: Type) -> Any:
        """Resolve a service from the container."""
        service_name = service_type.__name__
        
        if service_name not in self._services:
            raise ValueError(f"Service {service_name} not registered")
            
        service_def = self._services[service_name]
        
        if service_def.singleton and service_name in self._instances:
            return self._instances[service_name]
            
        # Resolve dependencies
        dependencies = {}
        for param_name, dep_type in service_def.dependencies.items():
            dependencies[param_name] = self.resolve(dep_type)
            
        # Create instance
        instance = service_def.implementation(**dependencies)
        
        if service_def.singleton:
            self._instances[service_name] = instance
            
        return instance
        
    def initialize(self):
        """Initialize all services in the correct order."""
        if self._initialized:
            return
            
        # Sort services by initialization order
        services = sorted(
            self._services.values(),
            key=lambda x: x.initialization_order
        )
        
        # Initialize services
        for service in services:
            self.resolve(service.service_type)
            
        self._initialized = True
        
    def clear(self):
        """Clear all services and instances."""
        self._services.clear()
        self._instances.clear()
        self._initialized = False
        
    def get_service_info(self) -> Dict[str, Any]:
        """Get information about registered services."""
        return {
            name: {
                'type': service.service_type.__name__,
                'singleton': service.singleton,
                'dependencies': {
                    name: dep.__name__ 
                    for name, dep in service.dependencies.items()
                },
                'initialization_order': service.initialization_order
            }
            for name, service in self._services.items()
        }

def inject(container: DependencyContainer):
    """Decorator to inject dependencies into a function."""
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Get function parameters
            sig = inspect.signature(func)
            params = sig.parameters
            
            # Inject dependencies
            for name, param in params.items():
                if param.annotation != inspect.Parameter.empty:
                    if name not in kwargs:
                        try:
                            kwargs[name] = container.resolve(param.annotation)
                        except ValueError:
                            pass
                            
            return func(*args, **kwargs)
        return wrapper
    return decorator

# Global container instance
container = DependencyContainer()

def register_service(service_type: Type,
                    implementation: Any = None,
                    singleton: bool = True,
                    dependencies: Dict[str, Type] = None,
                    initialization_order: int = 0):
    """Register a service in the global container."""
    container.register(
        service_type,
        implementation,
        singleton,
        dependencies,
        initialization_order
    )

def resolve_service(service_type: Type) -> Any:
    """Resolve a service from the global container."""
    return container.resolve(service_type)

def initialize_services():
    """Initialize all services in the global container."""
    container.initialize()

def get_service_info() -> Dict[str, Any]:
    """Get information about registered services in the global container."""
    return container.get_service_info() 