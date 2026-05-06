import pytest
import pytest_asyncio
import asyncio
from datetime import datetime
from typing import Dict, Any
import json
from pathlib import Path
import numpy as np

from src.core.service_registry import ServiceRegistry
from src.core.config_manager import ConfigManager
from src.core.event_bus import EventBus
from src.core.memory_bank import MemoryBank
from src.rl.q_table_manager import QTableManager

@pytest_asyncio.fixture
async def event_bus():
    """Fixture for EventBus."""
    bus = EventBus()
    await bus.initialize()
    yield bus
    await bus.stop()

@pytest_asyncio.fixture
async def config_manager(event_bus):
    """Fixture for ConfigManager."""
    manager = ConfigManager(event_bus)
    await manager.initialize()
    yield manager
    await manager.shutdown() if hasattr(manager, 'shutdown') else None

@pytest_asyncio.fixture
async def service_registry():
    """Fixture for ServiceRegistry."""
    registry = ServiceRegistry()
    await registry.initialize()
    yield registry
    await registry.shutdown()

@pytest_asyncio.fixture
async def memory_bank():
    """Fixture for MemoryBank."""
    bank = MemoryBank()
    await bank.initialize()
    yield bank
    await bank.clear() if hasattr(bank, 'clear') else None

@pytest_asyncio.fixture
async def q_table_manager(tmp_path):
    """Fixture for QTableManager."""
    manager = QTableManager(tmp_path)
    await manager.initialize()
    yield manager
    await manager.clear() if hasattr(manager, 'clear') else None

@pytest.mark.asyncio
async def test_service_registry(service_registry):
    """Test ServiceRegistry functionality."""
    # Test service registration
    service_registry.register_service("test_service", lambda: "test_value", [])
    assert service_registry.get_service("test_service")() == "test_value"
    
    # Test service unregistration
    service_registry.unregister("test_service")
    with pytest.raises(KeyError):
        service_registry.get_service("test_service")

@pytest.mark.asyncio
async def test_config_manager(config_manager):
    """Test ConfigManager functionality."""
    # Test configuration loading
    config = {
        "test_key": "test_value",
        "nested": {"key": "value"}
    }
    await config_manager.update_config(config)
    
    # Test configuration retrieval
    assert config_manager.get_config("test_key") == "test_value"
    assert config_manager.get_config("nested.key") == "value"

@pytest.mark.asyncio
async def test_event_bus(event_bus):
    """Test EventBus functionality."""
    events = []

    async def handler(event_data):
        events.append(event_data)

    # Test event subscription
    event_bus.subscribe("test_event", handler)

    # Test event publishing
    await event_bus.publish("test_event", {"data": "test"}, "test_source")
    await asyncio.sleep(0.1)  # Allow time for event processing
    
    assert len(events) == 1
    assert events[0]["data"] == "test"

@pytest.mark.asyncio
async def test_memory_bank(memory_bank):
    """Test MemoryBank functionality."""
    # Test memory storage
    memory = {
        "state": "test_state",
        "action": "test_action",
        "reward": 1.0,
        "next_state": "next_state",
        "done": False
    }
    await memory_bank.add_memory("test_episode", memory)
    
    # Test memory retrieval
    memories = await memory_bank.get_memories("test_episode")
    assert len(memories) == 1
    assert memories[0]["state"] == "test_state"

@pytest.mark.asyncio
async def test_q_table_manager(q_table_manager):
    """Test QTableManager functionality."""
    # Test Q-table storage
    q_table = {
        "state1": np.array([1.0, 2.0]),
        "state2": np.array([3.0, 4.0])
    }
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "episode": 1
    }
    
    await q_table_manager.save("test_model", q_table, metadata)
    
    # Test Q-table loading
    loaded_q_table, loaded_metadata = await q_table_manager.load("test_model")
    assert np.array_equal(loaded_q_table["state1"], q_table["state1"])
    assert loaded_metadata["episode"] == 1
    
    # Test model listing
    models = await q_table_manager.list_models()
    assert "test_model" in models

@pytest.mark.asyncio
async def test_service_integration(service_registry, config_manager, event_bus):
    """Test integration between core services."""
    # Register services
    service_registry.register_service("config_manager", config_manager, ["event_bus"])
    service_registry.register_service("event_bus", event_bus, [])
    
    # Test service interaction
    config = {"test_key": "test_value"}
    await config_manager.update_config(config)
    
    assert service_registry.get_service("config_manager").get_config("test_key") == "test_value"

@pytest.mark.asyncio
async def test_error_handling(service_registry):
    """Test error handling in core components."""
    # Test invalid service retrieval
    with pytest.raises(KeyError):
        service_registry.get_service("nonexistent_service")
        
    # Test duplicate service registration
    service_registry.register_service("test_service", lambda: "test", [])
    with pytest.raises(ValueError):
        service_registry.register_service("test_service", lambda: "test", []) 