import pytest
import numpy as np
from src.utils.self_repair import SelfRepairSystem

@pytest.fixture
def self_repair_system():
    """Create a SelfRepairSystem instance."""
    return SelfRepairSystem(
        health_threshold=0.7,
        repair_attempts=3,
        recovery_time=5
    )

def test_self_repair_initialization(self_repair_system):
    """Test SelfRepairSystem initialization."""
    assert self_repair_system.health_threshold == 0.7
    assert self_repair_system.repair_attempts == 3
    assert self_repair_system.recovery_time == 5
    assert self_repair_system.health_status == 1.0
    assert not self_repair_system.is_repairing

def test_health_monitoring(self_repair_system):
    """Test health monitoring."""
    # Simulate health degradation
    self_repair_system.update_health(0.6)
    
    # Check health status
    assert self_repair_system.health_status == 0.6
    assert self_repair_system.needs_repair()

def test_repair_trigger(self_repair_system):
    """Test repair trigger mechanism."""
    # Degrade health below threshold
    self_repair_system.update_health(0.5)
    
    # Trigger repair
    self_repair_system.trigger_repair()
    
    # Check repair state
    assert self_repair_system.is_repairing
    assert self_repair_system.repair_count == 1

def test_repair_process(self_repair_system):
    """Test repair process."""
    # Start repair process
    self_repair_system.update_health(0.5)
    self_repair_system.trigger_repair()
    
    # Simulate repair steps
    for _ in range(self_repair_system.recovery_time):
        self_repair_system.step_repair()
    
    # Check repair completion
    assert not self_repair_system.is_repairing
    assert self_repair_system.health_status > self_repair_system.health_threshold

def test_repair_attempts_limit(self_repair_system):
    """Test repair attempts limit."""
    # Simulate multiple repair attempts
    for _ in range(self_repair_system.repair_attempts + 1):
        self_repair_system.update_health(0.5)
        self_repair_system.trigger_repair()
        for _ in range(self_repair_system.recovery_time):
            self_repair_system.step_repair()
    
    # Check if system is in critical state
    assert self_repair_system.is_critical()

def test_health_recovery(self_repair_system):
    """Test health recovery process."""
    # Start with degraded health
    self_repair_system.update_health(0.5)
    
    # Perform repair
    self_repair_system.trigger_repair()
    for _ in range(self_repair_system.recovery_time):
        self_repair_system.step_repair()
    
    # Check health improvement
    assert self_repair_system.health_status > 0.5
    assert not self_repair_system.needs_repair()

def test_error_handling(self_repair_system):
    """Test error handling during repair."""
    # Simulate repair error
    self_repair_system.update_health(0.5)
    self_repair_system.trigger_repair()
    
    # Force error during repair
    self_repair_system.handle_repair_error()
    
    # Check error handling
    assert self_repair_system.repair_count > 0
    assert self_repair_system.health_status < 1.0

def test_system_reset(self_repair_system):
    """Test system reset functionality."""
    # Degrade system and trigger repair
    self_repair_system.update_health(0.5)
    self_repair_system.trigger_repair()
    
    # Reset system
    self_repair_system.reset()
    
    # Check reset state
    assert self_repair_system.health_status == 1.0
    assert not self_repair_system.is_repairing
    assert self_repair_system.repair_count == 0 