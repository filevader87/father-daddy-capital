from typing import Dict, Any, Optional, Callable
import asyncio
import os
import json
import time
from datetime import datetime
from src.utils.logger import get_logger
from src.core.event_bus import EventBus
from src.core.base_service import BaseService

logger = get_logger(__name__)

class ConfigHotReload(BaseService):
    """Configuration hot-reload system for RL parameters."""
    
    def __init__(self, event_bus: EventBus):
        super().__init__(event_bus)
        self.config_file = "src/config/rl_params.json"
        self.watchers: Dict[str, List[Callable]] = {}
        self.last_modified = 0
        self._watch_task: Optional[asyncio.Task] = None
        
    async def _initialize(self):
        """Initialize config hot-reload."""
        # Ensure config file exists
        if not os.path.exists(self.config_file):
            self._create_default_config()
            
        # Start watching for changes
        self._watch_task = asyncio.create_task(self._watch_config())
        logger.info("Config hot-reload initialized")
        
    async def _shutdown(self):
        """Shutdown config hot-reload."""
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        logger.info("Config hot-reload shut down")
        
    def _create_default_config(self):
        """Create default RL parameters configuration."""
        default_config = {
            "learning_rate": 0.001,
            "discount_factor": 0.99,
            "exploration_rate": 0.1,
            "batch_size": 32,
            "memory_size": 10000,
            "target_update": 1000,
            "min_exploration_rate": 0.01,
            "exploration_decay": 0.995
        }
        
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        with open(self.config_file, 'w') as f:
            json.dump(default_config, f, indent=2)
            
    async def _watch_config(self):
        """Watch for configuration changes."""
        try:
            while True:
                try:
                    current_modified = os.path.getmtime(self.config_file)
                    
                    if current_modified > self.last_modified:
                        # Config file changed
                        await self._reload_config()
                        self.last_modified = current_modified
                        
                except Exception as e:
                    logger.error(f"Error watching config file: {e}")
                    await self.health.increment_error()
                    
                await asyncio.sleep(1)  # Check every second
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Config watch error: {e}")
            await self.health.increment_error()
            
    async def _reload_config(self):
        """Reload configuration and notify watchers."""
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)
                
            # Validate config
            if not self._validate_config(config):
                raise ValueError("Invalid configuration")
                
            # Notify watchers
            for param, callbacks in self.watchers.items():
                if param in config:
                    for callback in callbacks:
                        try:
                            await callback(config[param])
                        except Exception as e:
                            logger.error(f"Error in config watcher callback: {e}")
                            await self.health.increment_error()
                            
            # Publish config change event
            await self.event_bus.publish(
                "config_reload",
                {
                    "config": config,
                    "timestamp": datetime.now().isoformat()
                },
                "config_hot_reload"
            )
            
            logger.info("Configuration reloaded successfully")
            await self.health.update_metric("reload_count", self.health.metrics.get("reload_count", 0) + 1)
            
        except Exception as e:
            logger.error(f"Error reloading configuration: {e}")
            await self.health.increment_error()
            
    def _validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate configuration values."""
        required_params = {
            "learning_rate": (float, 0.0, 1.0),
            "discount_factor": (float, 0.0, 1.0),
            "exploration_rate": (float, 0.0, 1.0),
            "batch_size": (int, 1, 1024),
            "memory_size": (int, 1000, 1000000),
            "target_update": (int, 1, 10000),
            "min_exploration_rate": (float, 0.0, 1.0),
            "exploration_decay": (float, 0.0, 1.0)
        }
        
        for param, (param_type, min_val, max_val) in required_params.items():
            if param not in config:
                logger.error(f"Missing required parameter: {param}")
                return False
                
            if not isinstance(config[param], param_type):
                logger.error(f"Invalid type for parameter {param}")
                return False
                
            if not min_val <= config[param] <= max_val:
                logger.error(f"Parameter {param} out of range")
                return False
                
        return True
        
    def register_watcher(self, param: str, callback: Callable):
        """Register a callback for parameter changes."""
        if param not in self.watchers:
            self.watchers[param] = []
        self.watchers[param].append(callback)
        logger.info(f"Registered watcher for parameter: {param}")
        
    def unregister_watcher(self, param: str, callback: Callable):
        """Unregister a parameter watcher."""
        if param in self.watchers:
            self.watchers[param].remove(callback)
            if not self.watchers[param]:
                del self.watchers[param]
            logger.info(f"Unregistered watcher for parameter: {param}")
            
    async def _check_health(self):
        """Check hot-reload health status."""
        try:
            if not os.path.exists(self.config_file):
                await self.health.update_status("error")
                await self.health.increment_error()
                logger.error("Config file not found")
                
            if not self._watch_task or self._watch_task.done():
                await self.health.update_status("error")
                await self.health.increment_error()
                logger.error("Config watch task not running")
                
        except Exception as e:
            await self.health.increment_error()
            logger.error(f"Health check failed: {e}")
            
    def get_watcher_status(self) -> Dict[str, Any]:
        """Get status of parameter watchers."""
        return {
            "watched_parameters": list(self.watchers.keys()),
            "total_watchers": sum(len(callbacks) for callbacks in self.watchers.values()),
            "last_reload": self.last_modified
        } 