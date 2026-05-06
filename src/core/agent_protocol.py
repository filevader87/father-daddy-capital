from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from enum import Enum
import asyncio
from datetime import datetime
import json
from src.core.event_bus import EventBus
from src.utils.logger import get_logger

logger = get_logger(__name__)

class MessageType(Enum):
    """Types of messages in the agent communication protocol."""
    REQUEST = "request"
    RESPONSE = "response"
    BROADCAST = "broadcast"
    ERROR = "error"
    HEARTBEAT = "heartbeat"

@dataclass
class AgentMessage:
    """Message structure for agent communication."""
    sender: str
    recipient: str
    message_type: MessageType
    content: Dict[str, Any]
    timestamp: datetime
    message_id: str
    correlation_id: Optional[str] = None
    priority: int = 0
    ttl: int = 300  # Time to live in seconds

class AgentProtocol:
    """Protocol for agent communication and coordination."""
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.message_queue = asyncio.Queue()
        self.message_history: Dict[str, AgentMessage] = {}
        self.agent_registry: Dict[str, Any] = {}
        self.processing_task = None
        
    async def start(self):
        """Start the agent protocol system."""
        self.processing_task = asyncio.create_task(self._process_messages())
        logger.info("Agent protocol started")
        
    async def stop(self):
        """Stop the agent protocol system."""
        if self.processing_task:
            self.processing_task.cancel()
            try:
                await self.processing_task
            except asyncio.CancelledError:
                pass
        logger.info("Agent protocol stopped")
        
    def register_agent(self, agent_id: str, agent: Any):
        """Register an agent with the protocol."""
        self.agent_registry[agent_id] = agent
        logger.info(f"Agent registered: {agent_id}")
        
    def unregister_agent(self, agent_id: str):
        """Unregister an agent from the protocol."""
        self.agent_registry.pop(agent_id, None)
        logger.info(f"Agent unregistered: {agent_id}")
        
    async def send_message(self, message: AgentMessage):
        """Send a message through the protocol."""
        if message.recipient not in self.agent_registry and message.message_type != MessageType.BROADCAST:
            raise ValueError(f"Recipient {message.recipient} not found")
            
        await self.message_queue.put(message)
        self.message_history[message.message_id] = message
        
    async def broadcast(self, sender: str, content: Dict[str, Any], priority: int = 0):
        """Broadcast a message to all agents."""
        message = AgentMessage(
            sender=sender,
            recipient="*",
            message_type=MessageType.BROADCAST,
            content=content,
            timestamp=datetime.now(),
            message_id=self._generate_message_id(),
            priority=priority
        )
        await self.send_message(message)
        
    async def request(self, sender: str, recipient: str, content: Dict[str, Any], priority: int = 0) -> AgentMessage:
        """Send a request and wait for response."""
        message = AgentMessage(
            sender=sender,
            recipient=recipient,
            message_type=MessageType.REQUEST,
            content=content,
            timestamp=datetime.now(),
            message_id=self._generate_message_id(),
            priority=priority
        )
        
        response_queue = asyncio.Queue()
        self.message_history[message.message_id] = message
        
        # Send request
        await self.send_message(message)
        
        # Wait for response
        try:
            response = await asyncio.wait_for(response_queue.get(), timeout=30)
            return response
        except asyncio.TimeoutError:
            raise TimeoutError("Request timed out")
            
    async def _process_messages(self):
        """Process messages from the queue."""
        while True:
            try:
                message = await self.message_queue.get()
                
                if message.message_type == MessageType.BROADCAST:
                    await self._handle_broadcast(message)
                elif message.message_type == MessageType.REQUEST:
                    await self._handle_request(message)
                elif message.message_type == MessageType.RESPONSE:
                    await self._handle_response(message)
                elif message.message_type == MessageType.ERROR:
                    await self._handle_error(message)
                elif message.message_type == MessageType.HEARTBEAT:
                    await self._handle_heartbeat(message)
                    
                self.message_queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                
    async def _handle_broadcast(self, message: AgentMessage):
        """Handle broadcast messages."""
        for agent_id, agent in self.agent_registry.items():
            if agent_id != message.sender:
                try:
                    await agent.handle_message(message)
                except Exception as e:
                    logger.error(f"Error handling broadcast for {agent_id}: {e}")
                    
    async def _handle_request(self, message: AgentMessage):
        """Handle request messages."""
        try:
            agent = self.agent_registry[message.recipient]
            response = await agent.handle_message(message)
            
            response_message = AgentMessage(
                sender=message.recipient,
                recipient=message.sender,
                message_type=MessageType.RESPONSE,
                content=response,
                timestamp=datetime.now(),
                message_id=self._generate_message_id(),
                correlation_id=message.message_id
            )
            
            await self.send_message(response_message)
            
        except Exception as e:
            error_message = AgentMessage(
                sender=message.recipient,
                recipient=message.sender,
                message_type=MessageType.ERROR,
                content={"error": str(e)},
                timestamp=datetime.now(),
                message_id=self._generate_message_id(),
                correlation_id=message.message_id
            )
            
            await self.send_message(error_message)
            
    async def _handle_response(self, message: AgentMessage):
        """Handle response messages."""
        if message.correlation_id:
            original_message = self.message_history.get(message.correlation_id)
            if original_message:
                # Notify the original sender
                sender = self.agent_registry.get(original_message.sender)
                if sender:
                    await sender.handle_message(message)
                    
    async def _handle_error(self, message: AgentMessage):
        """Handle error messages."""
        logger.error(f"Error from {message.sender}: {message.content.get('error')}")
        
    async def _handle_heartbeat(self, message: AgentMessage):
        """Handle heartbeat messages."""
        # Update agent status
        if message.sender in self.agent_registry:
            logger.debug(f"Heartbeat from {message.sender}")
            
    def _generate_message_id(self) -> str:
        """Generate a unique message ID."""
        return f"msg_{datetime.now().timestamp()}_{len(self.message_history)}"
        
    def get_agent_status(self) -> Dict[str, Any]:
        """Get status of all registered agents."""
        return {
            agent_id: {
                "registered": True,
                "last_heartbeat": None,  # Add heartbeat tracking
                "message_count": len([
                    msg for msg in self.message_history.values()
                    if msg.sender == agent_id or msg.recipient == agent_id
                ])
            }
            for agent_id in self.agent_registry
        } 