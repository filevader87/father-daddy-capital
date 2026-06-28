from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import asyncio
import psutil
import platform
from dataclasses import dataclass
from src.utils.logger import get_logger
from src.core.circuit_breaker import CircuitBreaker
from src.core.event_bus import EventBus
from src.core.service_registry import service_registry

logger = get_logger(__name__)

@dataclass
class SystemMetrics:
    """System metrics container."""
    cpu_percent: float
    memory_percent: float
    disk_usage: float
    network_io: Dict[str, float]
    process_count: int
    thread_count: int
    timestamp: datetime

@dataclass
class ComponentMetrics:
    """Component metrics container."""
    response_time: float
    error_rate: float
    request_count: int
    active_connections: int
    memory_usage: float
    timestamp: datetime

class SystemMonitor:
    """Enhanced system monitoring implementation."""
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.metrics_history: List[SystemMetrics] = []
        self.component_metrics: Dict[str, List[ComponentMetrics]] = {}
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}
        self.monitoring_task: Optional[asyncio.Task] = None
        self.alert_thresholds = {
            'cpu_percent': 80.0,
            'memory_percent': 85.0,
            'disk_usage': 90.0,
            'error_rate': 0.1
        }
        
    async def start(self):
        """Start the monitoring system."""
        self.monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info("System monitoring started")
        
    async def stop(self):
        """Stop the monitoring system."""
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
        logger.info("System monitoring stopped")
        
    async def _monitoring_loop(self):
        """Main monitoring loop."""
        while True:
            try:
                # Collect system metrics
                metrics = await self._collect_system_metrics()
                self.metrics_history.append(metrics)
                
                # Check for alerts
                await self._check_alerts(metrics)
                
                # Cleanup old metrics
                self._cleanup_old_metrics()
                
                # Publish metrics
                await self._publish_metrics(metrics)
                
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                
    async def _collect_system_metrics(self) -> SystemMetrics:
        """Collect system-wide metrics."""
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        network = psutil.net_io_counters()
        
        return SystemMetrics(
            cpu_percent=cpu_percent,
            memory_percent=memory.percent,
            disk_usage=disk.percent,
            network_io={
                'bytes_sent': network.bytes_sent,
                'bytes_recv': network.bytes_recv
            },
            process_count=len(psutil.pids()),
            thread_count=psutil.Process().num_threads(),
            timestamp=datetime.now()
        )
        
    async def _check_alerts(self, metrics: SystemMetrics):
        """Check for system alerts."""
        alerts = []
        
        if metrics.cpu_percent > self.alert_thresholds['cpu_percent']:
            alerts.append(f"High CPU usage: {metrics.cpu_percent}%")
            
        if metrics.memory_percent > self.alert_thresholds['memory_percent']:
            alerts.append(f"High memory usage: {metrics.memory_percent}%")
            
        if metrics.disk_usage > self.alert_thresholds['disk_usage']:
            alerts.append(f"High disk usage: {metrics.disk_usage}%")
            
        if alerts:
            await self.event_bus.publish(
                "system_alert",
                {"alerts": alerts, "timestamp": datetime.now()},
                "system_monitor"
            )
            
    def _cleanup_old_metrics(self):
        """Cleanup metrics older than 1 hour."""
        cutoff = datetime.now() - timedelta(hours=1)
        self.metrics_history = [
            m for m in self.metrics_history
            if m.timestamp > cutoff
        ]
        
        for component in self.component_metrics:
            self.component_metrics[component] = [
                m for m in self.component_metrics[component]
                if m.timestamp > cutoff
            ]
            
    async def _publish_metrics(self, metrics: SystemMetrics):
        """Publish metrics to event bus."""
        await self.event_bus.publish(
            "system_metrics",
            {
                "metrics": metrics.__dict__,
                "timestamp": datetime.now()
            },
            "system_monitor"
        )
        
    def register_component(self, name: str):
        """Register a component for monitoring."""
        if name not in self.component_metrics:
            self.component_metrics[name] = []
            self.circuit_breakers[name] = CircuitBreaker(
                name=f"component_{name}",
                failure_threshold=5,
                recovery_timeout=timedelta(seconds=30)
            )
            
    def update_component_metrics(self, name: str, metrics: ComponentMetrics):
        """Update component metrics."""
        if name in self.component_metrics:
            self.component_metrics[name].append(metrics)
            
    def get_system_metrics(self) -> Dict[str, Any]:
        """Get current system metrics."""
        if not self.metrics_history:
            return {}
            
        latest = self.metrics_history[-1]
        return {
            "cpu_percent": latest.cpu_percent,
            "memory_percent": latest.memory_percent,
            "disk_usage": latest.disk_usage,
            "network_io": latest.network_io,
            "process_count": latest.process_count,
            "thread_count": latest.thread_count,
            "timestamp": latest.timestamp
        }
        
    def get_component_metrics(self, name: str) -> Dict[str, Any]:
        """Get component metrics."""
        if name not in self.component_metrics:
            return {}
            
        metrics = self.component_metrics[name]
        if not metrics:
            return {}
            
        latest = metrics[-1]
        return {
            "response_time": latest.response_time,
            "error_rate": latest.error_rate,
            "request_count": latest.request_count,
            "active_connections": latest.active_connections,
            "memory_usage": latest.memory_usage,
            "timestamp": latest.timestamp
        }
        
    def get_circuit_breaker_status(self, name: str) -> Dict[str, Any]:
        """Get circuit breaker status for a component."""
        if name in self.circuit_breakers:
            return self.circuit_breakers[name].get_metrics()
        return {}
        
    def get_system_health(self) -> Dict[str, Any]:
        """Get overall system health status."""
        if not self.metrics_history:
            return {"status": "unknown"}
            
        latest = self.metrics_history[-1]
        health_status = "healthy"
        
        if (latest.cpu_percent > self.alert_thresholds['cpu_percent'] or
            latest.memory_percent > self.alert_thresholds['memory_percent'] or
            latest.disk_usage > self.alert_thresholds['disk_usage']):
            health_status = "degraded"
            
        return {
            "status": health_status,
            "metrics": self.get_system_metrics(),
            "components": {
                name: self.get_component_metrics(name)
                for name in self.component_metrics
            },
            "circuit_breakers": {
                name: self.get_circuit_breaker_status(name)
                for name in self.circuit_breakers
            }
        }

# Singleton instance
system_monitor = SystemMonitor(service_registry.get_event_bus()) 