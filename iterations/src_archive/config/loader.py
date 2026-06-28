"""
Configuration Loader
------------------
Handles loading and migration of configuration from various sources.
"""

import os
import json
import yaml
from typing import Dict, Any, Optional
from pathlib import Path
from . import TradingConfig

class ConfigLoader:
    """Handles configuration loading and migration."""
    
    def __init__(self, base_path: str = "config"):
        self.base_path = Path(base_path)
        self.config = TradingConfig()
        
    def load_all(self) -> TradingConfig:
        """Load and merge all configuration sources."""
        # Load main config
        main_config = self._load_file("main_config.json")
        if main_config:
            self.config = TradingConfig(**main_config)
            
        # Load system config
        system_config = self._load_file("system_config.yaml")
        if system_config:
            self._merge_config(system_config)
            
        # Load execution config
        exec_config = self._load_file("execution/execution_config.yml")
        if exec_config:
            self._merge_config(exec_config)
            
        # Load agent config
        agent_config = self._load_file("agent_config.yaml")
        if agent_config:
            self._merge_config(agent_config)
            
        # Update from environment variables
        self.config.update_from_env()
        
        return self.config
        
    def _load_file(self, relative_path: str) -> Optional[Dict[str, Any]]:
        """Load configuration from a file."""
        file_path = self.base_path / relative_path
        if not file_path.exists():
            return None
            
        with open(file_path, 'r') as f:
            if file_path.suffix == '.json':
                return json.load(f)
            elif file_path.suffix in ['.yaml', '.yml']:
                return yaml.safe_load(f)
            else:
                raise ValueError(f"Unsupported config file format: {file_path.suffix}")
                
    def _merge_config(self, new_config: Dict[str, Any]) -> None:
        """Merge new configuration into existing config."""
        for key, value in new_config.items():
            if hasattr(self.config, key.upper()):
                setattr(self.config, key.upper(), value)
            elif isinstance(value, dict):
                # Handle nested configurations
                for subkey, subvalue in value.items():
                    full_key = f"{key}_{subkey}".upper()
                    if hasattr(self.config, full_key):
                        setattr(self.config, full_key, subvalue)
                        
    def save_unified_config(self, output_path: str = "config/unified_config.json") -> None:
        """Save unified configuration to file."""
        self.config.save_to_file(output_path)
        
    def migrate_old_configs(self) -> None:
        """Migrate old configuration files to the new unified format."""
        # Load all configurations
        self.load_all()
        
        # Save unified config
        self.save_unified_config()
        
        # Archive old config files
        archive_dir = self.base_path / "archive"
        archive_dir.mkdir(exist_ok=True)
        
        for config_file in self.base_path.glob("**/*.{json,yaml,yml}"):
            if config_file.name != "unified_config.json":
                new_path = archive_dir / config_file.relative_to(self.base_path)
                new_path.parent.mkdir(parents=True, exist_ok=True)
                config_file.rename(new_path) 