import numpy as np
import torch
import logging
from typing import Any, Dict, Optional, Union
from datetime import datetime, timedelta

class SelfRepairSystem:
    """System for monitoring and repairing components."""
    
    def __init__(
        self,
        health_threshold: float = 0.7,
        repair_attempts: int = 3,
        recovery_time: int = 5,
        monitoring_interval: int = 1,
        degradation_rate: float = 0.1
    ):
        """
        Initialize the self-repair system.
        
        Args:
            health_threshold: Threshold below which repair is triggered
            repair_attempts: Maximum number of repair attempts
            recovery_time: Time to wait between repair attempts (seconds)
            monitoring_interval: Interval between health checks (seconds)
            degradation_rate: Rate at which component health degrades
        """
        self.health_threshold = health_threshold
        self.repair_attempts = repair_attempts
        self.recovery_time = recovery_time
        self.monitoring_interval = monitoring_interval
        self.degradation_rate = degradation_rate
        
        # Initialize state
        self.health_status = 1.0
        self.is_repairing = False
        self.current_attempt = 0
        self.repair_count = 0
        self._repair_progress = 0
        self.last_repair_time = None
        self.component_health = {}
        
        # Set up logging
        self.logger = logging.getLogger(__name__)
        
        # Validate inputs
        self._validate_inputs()
    
    def _validate_inputs(self):
        """Validate initialization parameters."""
        if not 0 < self.health_threshold < 1:
            raise ValueError("health_threshold must be between 0 and 1")
        if self.repair_attempts <= 0:
            raise ValueError("repair_attempts must be > 0")
        if self.recovery_time <= 0:
            raise ValueError("recovery_time must be > 0")
        if self.monitoring_interval <= 0:
            raise ValueError("monitoring_interval must be > 0")
        if not 0 <= self.degradation_rate <= 1:
            raise ValueError("degradation_rate must be between 0 and 1")
    
    def monitor_health(self, component: Any) -> float:
        """
        Monitor the health of a component.
        
        Args:
            component: The component to monitor
        
        Returns:
            float: Health score between 0 and 1
        """
        try:
            # Get component ID
            component_id = id(component)
            
            # Initialize health if not seen before
            if component_id not in self.component_health:
                self.component_health[component_id] = 1.0
            
            # Check component type
            if isinstance(component, torch.nn.Module):
                health = self._check_neural_network_health(component)
            else:
                health = self._check_general_component_health(component)
            
            # Apply degradation
            self.component_health[component_id] *= (1 - self.degradation_rate)
            self.component_health[component_id] = max(0, min(1, self.component_health[component_id]))
            
            # Log health status
            self.logger.info(f"Component health: {self.component_health[component_id]:.2f}")
            
            return self.component_health[component_id]
            
        except Exception as e:
            self.logger.error(f"Error monitoring health: {str(e)}")
            return 0.0
    
    def _check_neural_network_health(self, model: torch.nn.Module) -> float:
        """Check health of neural network components."""
        try:
            # Check for NaN weights
            has_nan = any(
                torch.isnan(param).any()
                for param in model.parameters()
            )
            if has_nan:
                return 0.0
            
            # Check for exploding gradients
            has_inf = any(
                torch.isinf(param).any()
                for param in model.parameters()
            )
            if has_inf:
                return 0.0
            
            # Check weight magnitudes
            weight_magnitudes = [
                param.abs().mean().item()
                for param in model.parameters()
            ]
            if any(mag > 100 for mag in weight_magnitudes):
                return 0.5
            
            return 1.0
            
        except Exception as e:
            self.logger.error(f"Error checking neural network health: {str(e)}")
            return 0.0
    
    def _check_general_component_health(self, component: Any) -> float:
        """Check health of general components."""
        try:
            # Check if component has expected attributes
            if not hasattr(component, '__dict__'):
                return 0.5
            
            # Check if component has error state
            if hasattr(component, 'error_count'):
                if component.error_count > 0:
                    return 0.0
            
            # Check if component has performance metrics
            if hasattr(component, 'performance_metrics'):
                metrics = component.performance_metrics
                if any(metric < threshold for metric, threshold in metrics.items()):
                    return 0.5
            
            return 1.0
            
        except Exception as e:
            self.logger.error(f"Error checking general component health: {str(e)}")
            return 0.0
    
    def trigger_repair(self) -> bool:
        """
        Trigger the repair process.
        
        Returns:
            bool: True if repair was triggered, False otherwise
        """
        try:
            # Check if already repairing
            if self.is_repairing:
                return False
            
            # Check if enough time has passed since last repair
            if self.last_repair_time is not None:
                time_since_repair = (datetime.now() - self.last_repair_time).total_seconds()
                if time_since_repair < self.recovery_time:
                    return False
            
            # Start repair process
            self.is_repairing = True
            self.current_attempt = 0
            self.repair_count += 1
            self._repair_progress = 0
            self.last_repair_time = datetime.now()
            
            self.logger.info("Repair process triggered")
            return True
            
        except Exception as e:
            self.logger.error(f"Error triggering repair: {str(e)}")
            return False

    def update_health(self, health: float) -> None:
        """Update system-level health status."""
        self.health_status = max(0.0, min(1.0, float(health)))

    def needs_repair(self) -> bool:
        return self.health_status < self.health_threshold

    def step_repair(self) -> None:
        if not self.is_repairing:
            return
        self._repair_progress += 1
        increment = (1.0 - self.health_status) / max(1, self.recovery_time - self._repair_progress + 1)
        self.health_status = max(self.health_status, min(1.0, self.health_status + increment))
        if self._repair_progress >= self.recovery_time:
            self.is_repairing = False
            self.last_repair_time = None

    def is_critical(self) -> bool:
        return self.repair_count > self.repair_attempts

    def handle_repair_error(self) -> None:
        self.repair_count += 1
        self.health_status = max(0.0, self.health_status - self.degradation_rate)
        self.is_repairing = False

    def reset(self) -> None:
        self.health_status = 1.0
        self.is_repairing = False
        self.current_attempt = 0
        self.repair_count = 0
        self._repair_progress = 0
        self.last_repair_time = None
    
    def repair_component(self, component: Any) -> bool:
        """
        Attempt to repair a component.
        
        Args:
            component: The component to repair
        
        Returns:
            bool: True if repair was successful, False otherwise
        """
        try:
            if not self.is_repairing:
                return False
            
            if self.current_attempt >= self.repair_attempts:
                self.is_repairing = False
                return False
            
            # Increment attempt counter
            self.current_attempt += 1
            
            # Attempt repair based on component type
            if isinstance(component, torch.nn.Module):
                success = self._repair_neural_network(component)
            else:
                success = self._repair_general_component(component)
            
            if success:
                self.is_repairing = False
                self.component_health[id(component)] = 1.0
            
            return success
            
        except Exception as e:
            self.logger.error(f"Error repairing component: {str(e)}")
            return False
    
    def _repair_neural_network(self, model: torch.nn.Module) -> bool:
        """Repair neural network components."""
        try:
            # Reset weights if they are NaN or Inf
            for param in model.parameters():
                if torch.isnan(param).any() or torch.isinf(param).any():
                    torch.nn.init.xavier_uniform_(param)
            
            # Apply gradient clipping
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error repairing neural network: {str(e)}")
            return False
    
    def _repair_general_component(self, component: Any) -> bool:
        """Repair general components."""
        try:
            # Reset error counters
            if hasattr(component, 'error_count'):
                component.error_count = 0
            
            # Reset performance metrics
            if hasattr(component, 'performance_metrics'):
                component.performance_metrics = {}
            
            # Reset to default state if possible
            if hasattr(component, 'reset'):
                component.reset()
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error repairing general component: {str(e)}")
            return False
    
    def get_repair_status(self) -> Dict[str, Any]:
        """Get current repair status."""
        return {
            'is_repairing': self.is_repairing,
            'current_attempt': self.current_attempt,
            'last_repair_time': self.last_repair_time,
            'component_health': self.component_health.copy()
        } 
