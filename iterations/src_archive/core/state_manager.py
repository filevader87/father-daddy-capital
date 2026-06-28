from typing import Dict, Any, Optional, List
from datetime import datetime
import json
import os
import asyncio
from dataclasses import dataclass
from src.utils.logger import get_logger
from src.core.event_bus import EventBus
from src.core.base_service import BaseService
from src.config import TradingConfig as config

logger = get_logger(__name__)

@dataclass
class StateVersion:
    """State version information."""
    version: str
    timestamp: datetime
    description: str
    author: str

class StateManager(BaseService):
    """Enhanced state manager with versioning and persistence."""
    
    def __init__(self, event_bus: EventBus):
        super().__init__(event_bus)
        self.states: Dict[str, Dict[str, Any]] = {}
        self.history: Dict[str, List[Dict[str, Any]]] = {}
        self.versions: Dict[str, List[StateVersion]] = {}
        self._lock = asyncio.Lock()
        self.state_dir = "src/data/state"
        
        # Ensure state directory exists
        os.makedirs(self.state_dir, exist_ok=True)
        
    async def _initialize(self):
        """Initialize state manager."""
        try:
            # Load persisted states
            await self._load_persisted_states()
            
            # Subscribe to state change events
            self.event_bus.subscribe("state_change", self._handle_state_change)
            
            # Subscribe to config change events
            self.event_bus.subscribe("config_change", self._handle_config_change)
            
            logger.info("State manager initialized")
        except Exception as e:
            logger.error(f"Failed to initialize state manager: {e}")
            raise
            
    async def _shutdown(self):
        """Shutdown state manager."""
        try:
            # Unsubscribe from events
            self.event_bus.unsubscribe("state_change", self._handle_state_change)
            self.event_bus.unsubscribe("config_change", self._handle_config_change)
            
            # Persist all states
            for component in self.states:
                await self._persist_state(component)
        except Exception as e:
            logger.error(f"Error during state manager shutdown: {e}")
            raise
            
    async def _handle_config_change(self, event):
        """Handle configuration change events."""
        try:
            if event.data.get("name") == "state_manager":
                # Update state manager configuration
                config = event.data.get("config", {})
                if "state_dir" in config:
                    self.state_dir = config["state_dir"]
                    os.makedirs(self.state_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"Error handling config change: {e}")
            
    async def save_state(self, component: str, state: Dict[str, Any], 
                        version: Optional[str] = None,
                        description: Optional[str] = None,
                        author: Optional[str] = None):
        """Save component state with versioning."""
        async with self._lock:
            try:
                # Update current state
                self.states[component] = state
                
                # Add to history
                if component not in self.history:
                    self.history[component] = []
                self.history[component].append({
                    "state": state,
                    "timestamp": datetime.now(),
                    "version": version
                })
                
                # Add version info if provided
                if version:
                    if component not in self.versions:
                        self.versions[component] = []
                    self.versions[component].append(StateVersion(
                        version=version,
                        timestamp=datetime.now(),
                        description=description or "",
                        author=author or "system"
                    ))
                
                # Persist state
                await self._persist_state(component)
                
                # Update config manager if available
                if "config_manager" in self._dependencies:
                    from src.core.config_manager import ConfigManager
                    config_manager: ConfigManager = self.get_dependency("config_manager")
                    await config_manager.update_config(
                        f"state_{component}",
                        state,
                        version=version,
                        description=description,
                        author=author
                    )
                
                # Publish state change event
                await self.event_bus.publish(
                    "state_change",
                    {
                        "component": component,
                        "state": state,
                        "version": version,
                        "timestamp": datetime.now()
                    },
                    "state_manager"
                )
                
                logger.info(f"State saved for component: {component}")
            except Exception as e:
                logger.error(f"Error saving state for {component}: {e}")
                raise
                
    async def load_state(self, component: str, version: Optional[str] = None) -> Dict[str, Any]:
        """Load component state, optionally for a specific version."""
        async with self._lock:
            try:
                if version:
                    # Find state for specific version
                    for state_info in reversed(self.history.get(component, [])):
                        if state_info["version"] == version:
                            return state_info["state"]
                    raise ValueError(f"Version {version} not found for {component}")
                else:
                    # Return current state
                    return self.states.get(component, {})
            except Exception as e:
                logger.error(f"Error loading state for {component}: {e}")
                raise
                
    async def get_state_history(self, component: str) -> List[Dict[str, Any]]:
        """Get state history for a component."""
        return self.history.get(component, [])
        
    async def get_state_versions(self, component: str) -> List[StateVersion]:
        """Get version history for a component."""
        return self.versions.get(component, [])
        
    async def _persist_state(self, component: str):
        """Persist state to disk."""
        try:
            state_file = os.path.join(self.state_dir, f"{component}_state.json")
            with open(state_file, 'w') as f:
                json.dump({
                    "current_state": self.states[component],
                    "history": self.history[component],
                    "versions": [vars(v) for v in self.versions.get(component, [])]
                }, f, default=str)
        except Exception as e:
            logger.error(f"Error persisting state for {component}: {e}")
            raise
            
    async def _load_persisted_states(self):
        """Load persisted states from disk."""
        try:
            for file in os.listdir(self.state_dir):
                if file.endswith("_state.json"):
                    component = file[:-11]  # Remove '_state.json'
                    with open(os.path.join(self.state_dir, file), 'r') as f:
                        data = json.load(f)
                        self.states[component] = data["current_state"]
                        self.history[component] = data["history"]
                        self.versions[component] = [
                            StateVersion(**v) for v in data["versions"]
                        ]
        except Exception as e:
            logger.error(f"Error loading persisted states: {e}")
            raise
            
    async def _handle_state_change(self, event):
        """Handle state change events."""
        try:
            component = event.data["component"]
            state = event.data["state"]
            version = event.data.get("version")
            
            # Validate state change
            if not await self._validate_state(component, state):
                logger.warning(f"Invalid state change for {component}")
                return
                
            # Update state
            await self.save_state(
                component,
                state,
                version=version,
                description=f"State change from {event.source}",
                author=event.source
            )
        except Exception as e:
            logger.error(f"Error handling state change: {e}")
            
    async def _validate_state(self, component: str, state: Dict[str, Any]) -> bool:
        """Validate component state."""
        # Add component-specific validation logic here
        return True
        
    def get_state_summary(self) -> Dict[str, Any]:
        """Get summary of all states."""
        return {
            "components": list(self.states.keys()),
            "total_states": len(self.states),
            "total_history_entries": sum(len(h) for h in self.history.values()),
            "total_versions": sum(len(v) for v in self.versions.values())
        } 