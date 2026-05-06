"""
Configuration Manager
-------------------
This module implements the core configuration management system.
"""

import os
import json
import yaml
from typing import Dict, Any, Optional, List
from pathlib import Path
import logging
from src.utils.logger import get_logger
from dotenv import load_dotenv
import jsonschema
from dataclasses import dataclass
from src.core.event_bus import EventBus
from datetime import datetime
from src.core.base_service import BaseService
from src.config import TradingConfig

# Load configuration
config = TradingConfig.load_from_file()

logger = get_logger(__name__)

@dataclass
class ConfigVersion:
    """Configuration version information."""
    version: str
    timestamp: str
    environment: str
    description: str
    author: str

class ConfigManager(BaseService):
    """Enhanced configuration manager with environment handling and validation."""
    
    def __init__(self, event_bus: EventBus, config: Optional[Dict[str, Any]] = None):
        super().__init__(event_bus)
        self.configs: Dict[str, Dict[str, Any]] = {}
        self.schemas: Dict[str, Dict[str, Any]] = {}
        self.versions: Dict[str, List[ConfigVersion]] = {}
        self.environment = os.getenv("APP_ENV", "development")
        self.config_dir = "src/config"
        
        # Ensure config directory exists
        os.makedirs(self.config_dir, exist_ok=True)
        
        self.config = self._load_config(config)
        self.logger = logging.getLogger("ConfigManager")
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate configuration manager settings."""
        default_config = {
            'paths': {
                'config_dir': 'config',
                'archive_dir': 'config/archive',
                'backup_dir': 'config/backup'
            },
            'file_types': {
                'json': True,
                'yaml': True,
                'yml': True
            },
            'validation': {
                'required_fields': ['version', 'timestamp'],
                'schema_path': 'config/schema.json'
            },
            'system': {
                'startup_timeout': 300,
                'shutdown_timeout': 60,
                'health_check_interval': 30
            }
        }
        
        if config:
            default_config.update(config)
        return default_config
        
    def get(self, name: str, default: Any = None) -> Any:
        """Get configuration value by name."""
        config_key = f"{name}_{self.environment}"
        return self.configs.get(config_key, default)
        
    async def _initialize(self):
        """Initialize configuration manager."""
        try:
            # Load configuration schemas
            await self._load_schemas()
            
            # Load configurations
            await self._load_configs()
            
            # Subscribe to config change events
            self.event_bus.subscribe("config_change", self._handle_config_change)
            
            # Subscribe to state change events for config persistence
            self.event_bus.subscribe("state_change", self._handle_state_change)
            
            logger.info("Configuration manager initialized")
        except Exception as e:
            logger.error(f"Failed to initialize configuration manager: {e}")
            raise
            
    async def _shutdown(self):
        """Shutdown configuration manager."""
        try:
            # Unsubscribe from events
            self.event_bus.unsubscribe("config_change", self._handle_config_change)
            self.event_bus.unsubscribe("state_change", self._handle_state_change)
            
            # Persist all configurations
            for name in self.configs:
                if name == "default":
                    continue
                env = name.split('_')[-1]
                await self._persist_config(name.split('_')[0], env)
        except Exception as e:
            logger.error(f"Error during config manager shutdown: {e}")
            raise
            
    async def _handle_state_change(self, event):
        """Handle state change events for config persistence."""
        try:
            if event.data.get("component") == "config_manager":
                # Persist config state
                name = event.data.get("name")
                env = event.data.get("environment")
                if name and env:
                    await self._persist_config(name, env)
        except Exception as e:
            logger.error(f"Error handling state change: {e}")
            
    def get_config(self, name: str, environment: Optional[str] = None) -> Any:
        """Get configuration value or named configuration."""
        if "." in name:
            current: Any = self.configs.get("default", {})
            for part in name.split("."):
                if not isinstance(current, dict):
                    return None
                current = current.get(part)
            return current
        if name in self.configs.get("default", {}):
            return self.configs["default"][name]
        env = environment or self.environment
        config_key = f"{name}_{env}"
        
        if config_key not in self.configs:
            return None
            
        return self.configs[config_key]
        
    async def update_config(self, name: Any, config: Optional[Dict[str, Any]] = None,
                          version: Optional[str] = None,
                          description: Optional[str] = None,
                          author: Optional[str] = None,
                          environment: Optional[str] = None):
        """Update configuration with versioning."""
        if isinstance(name, dict) and config is None:
            self.configs["default"] = name
            return

        env = environment or self.environment
        config_key = f"{name}_{env}"
        
        try:
            # Validate configuration
            if not await self._validate_config(name, config):
                raise ValueError(f"Invalid configuration for {name}")
                
            # Update configuration
            self.configs[config_key] = config
            
            # Add version info
            if version:
                if config_key not in self.versions:
                    self.versions[config_key] = []
                self.versions[config_key].append(ConfigVersion(
                    version=version,
                    timestamp=str(datetime.now()),
                    environment=env,
                    description=description or "",
                    author=author or "system"
                ))
                
            # Persist configuration
            await self._persist_config(name, env)
            
            # Update state manager if available
            if "state_manager" in self._dependencies:
                from src.core.state_manager import StateManager
                state_manager: StateManager = self.get_dependency("state_manager")
                await state_manager.save_state(
                    "config_manager",
                    {
                        "name": name,
                        "environment": env,
                        "config": config,
                        "version": version
                    },
                    version=version,
                    description=description,
                    author=author
                )
            
            # Publish config change event
            await self.event_bus.publish(
                "config_change",
                {
                    "name": name,
                    "environment": env,
                    "config": config,
                    "version": version,
                    "timestamp": str(datetime.now())
                },
                "config_manager"
            )
            
            logger.info(f"Configuration updated: {name} for environment {env}")
        except Exception as e:
            logger.error(f"Error updating configuration {name}: {e}")
            raise
            
    async def get_config_versions(self, name: str, environment: Optional[str] = None) -> List[ConfigVersion]:
        """Get version history for a configuration."""
        env = environment or self.environment
        config_key = f"{name}_{env}"
        return self.versions.get(config_key, [])
        
    async def _load_schemas(self):
        """Load configuration schemas."""
        try:
            schema_dir = os.path.join(self.config_dir, "schemas")
            if not os.path.exists(schema_dir):
                return
                
            for file in os.listdir(schema_dir):
                if file.endswith("_schema.json"):
                    name = file[:-12]  # Remove '_schema.json'
                    with open(os.path.join(schema_dir, file), 'r') as f:
                        self.schemas[name] = json.load(f)
        except Exception as e:
            logger.error(f"Error loading schemas: {e}")
            raise
            
    async def _load_configs(self):
        """Load configurations from disk."""
        try:
            for file in os.listdir(self.config_dir):
                if file.endswith("_config.json"):
                    name = file[:-12]  # Remove '_config.json'
                    with open(os.path.join(self.config_dir, file), 'r') as f:
                        data = json.load(f)
                        for env, config in data.items():
                            config_key = f"{name}_{env}"
                            self.configs[config_key] = config
        except Exception as e:
            logger.error(f"Error loading configurations: {e}")
            raise
            
    async def _persist_config(self, name: str, environment: str):
        """Persist configuration to disk."""
        try:
            config_file = os.path.join(self.config_dir, f"{name}_config.json")
            config_key = f"{name}_{environment}"
            
            # Load existing configs
            if os.path.exists(config_file):
                with open(config_file, 'r') as f:
                    data = json.load(f)
            else:
                data = {}
                
            # Update config for environment
            data[environment] = self.configs[config_key]
            
            # Save updated configs
            with open(config_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error persisting configuration {name}: {e}")
            raise
            
    async def _validate_config(self, name: str, config: Dict[str, Any]) -> bool:
        """Validate configuration against schema."""
        if name not in self.schemas:
            return True  # No schema defined
            
        try:
            jsonschema.validate(instance=config, schema=self.schemas[name])
            return True
        except jsonschema.exceptions.ValidationError as e:
            logger.error(f"Configuration validation failed for {name}: {e}")
            return False
            
    async def _handle_config_change(self, event):
        """Handle configuration change events."""
        try:
            name = event.data["name"]
            environment = event.data["environment"]
            config = event.data["config"]
            version = event.data.get("version")
            
            await self.update_config(
                name,
                config,
                version=version,
                description=f"Config change from {event.source}",
                author=event.source,
                environment=environment
            )
        except Exception as e:
            logger.error(f"Error handling config change: {e}")
            
    def get_config_summary(self) -> Dict[str, Any]:
        """Get summary of all configurations."""
        return {
            name: {
                "environment": name.split("_")[-1],
                "version": self.versions[name][-1].version if name in self.versions and self.versions[name] else None,
                "last_updated": self.versions[name][-1].timestamp if name in self.versions and self.versions[name] else None
            }
            for name in self.configs
        }

# Global config manager instance
config_manager = ConfigManager(EventBus()) 
