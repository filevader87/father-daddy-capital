import asyncio
import logging
from typing import Dict, List, Callable, Any, Optional
from dataclasses import dataclass
from datetime import datetime
import json
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

@dataclass
class Event:
    """Event data structure for the event bus."""
    event_type: str
    data: Any
    source: str
    timestamp: datetime = None
    metadata: Optional[Dict] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

class EventBus:
    """Enhanced event bus for system-wide communication."""
    
    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}
        self._subscriptions: Dict[str, List[Callable]] = {}
        self.event_history: List[Event] = []
        self.routing_rules: Dict[str, List[str]] = {}
        self.processing_queue = asyncio.Queue()
        self.processing_task = None
        
    async def initialize(self):
        """Initialize the event bus."""
        await self.start()
        logger.info("Event bus initialized")
        
    async def start(self):
        """Start the event bus."""
        self.processing_task = asyncio.create_task(self._process_events())
        logger.info("Event bus started")
        
    async def stop(self):
        """Stop the event bus."""
        if self.processing_task:
            self.processing_task.cancel()
            try:
                await self.processing_task
            except asyncio.CancelledError:
                pass
        logger.info("Event bus stopped")
        
    def subscribe(self, event_type: str, callback: Callable):
        """Subscribe to an event type."""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(callback)
        logger.debug(f"Subscribed to {event_type}")
        
    def unsubscribe(self, event_type: str, callback: Callable):
        """Unsubscribe from an event type."""
        if event_type in self._handlers and callback in self._handlers[event_type]:
            self._handlers[event_type].remove(callback)
            logger.debug(f"Unsubscribed from {event_type}")
            
    def add_routing_rule(self, event_type: str, target_types: List[str]):
        """Add a routing rule for event types."""
        self.routing_rules[event_type] = target_types
        logger.debug(f"Added routing rule for {event_type}")
        
    async def publish(self, event_type: str, data: Any, source: str):
        """Publish an event."""
        event = Event(event_type, data, source)
        self.event_history.append(event)
        await self.processing_queue.put(event)
        
        # Process direct subscribers
        if event_type in self._handlers:
            for callback in self._handlers[event_type]:
                try:
                    await callback(event.data)
                except Exception as e:
                    logger.error(f"Error in event callback: {e}")
        
    async def _process_events(self):
        """Process events from the queue."""
        while True:
            try:
                event = await self.processing_queue.get()
                
                # Process routing rules
                if event.event_type in self.routing_rules:
                    for target_type in self.routing_rules[event.event_type]:
                        await self.publish(target_type, event.data, f"router_{event.source}")
                        
                self.processing_queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing event: {e}")
                
    def get_event_history(self, event_type: Optional[str] = None, 
                         start_time: Optional[datetime] = None,
                         end_time: Optional[datetime] = None) -> List[Event]:
        """Get event history with optional filtering."""
        filtered = self.event_history
        
        if event_type:
            filtered = [e for e in filtered if e.event_type == event_type]
            
        if start_time:
            filtered = [e for e in filtered if e.timestamp >= start_time]
            
        if end_time:
            filtered = [e for e in filtered if e.timestamp <= end_time]
            
        return filtered
        
    def get_subscriber_count(self, event_type: str) -> int:
        """Get number of subscribers for an event type."""
        return len(self._handlers.get(event_type, []))
        
    def get_routing_rules(self) -> Dict[str, List[str]]:
        """Get all routing rules."""
        return self.routing_rules.copy()
        
    def clear_event_history(self):
        """Clear event history."""
        self.event_history.clear()
        
    def get_status(self) -> Dict[str, Any]:
        """Get current event bus status."""
        return {
            "subscriber_counts": {
                event_type: len(callbacks)
                for event_type, callbacks in self._handlers.items()
            },
            "event_count": len(self.event_history),
            "queue_size": self.processing_queue.qsize(),
            "routing_rules": self.get_routing_rules()
        } 
