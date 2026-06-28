"""
Agent Orchestrator - Coordinates multiple trading agents and their lifecycle

Handles:
- Agent registration and lifecycle management
- Signal aggregation from multiple sources
- State synchronization across agents
- Memory persistence and retrieval
"""

from .agent_protocol import AgentProtocol
from .agent_registry import AgentRegistry


class AgentOrchestrator:
    """
    Orchestrates trading agents and their coordinated actions
    
    Architecture:
    1. Register agents (AETS, momentum, sentiment, etc.)
    2. Collect market data for all agents
    3. Generate signals from each agent
    4. Aggregate and fuse signals
    5. Pass through risk gate
    6. Execute trades
    """
    
    def __init__(self, registry_class=AgentRegistry):
        """Initialize orchestrator with agent registry"""
        self.registry = registry_class()
        self._agents = {}
        self._initialized = False
    
    def register_agent(self, agent):
        """Register a trading agent"""
        self._agents[agent.name] = agent
        return True
    
    def register_agents(self, *agents, **kwargs):
        """Register multiple agents"""
        for i, agent in enumerate(agents):
            agent.name = f"{agent.name}_{i}" if agent.name == agent.__class__.__name__ else agent.name
            self.register_agent(agent)
        return True
    
    async def start(self):
        """Initialize and start all registered agents"""
        for agent_name, agent in self._agents.items():
            if hasattr(agent, 'connect'):
                await agent.connect()
            if hasattr(agent, 'start'):
                await agent.start()
        self._initialized = True
        return True
    
    async def stop(self):
        """Stop all agents"""
        for agent_name, agent in self._agents.items():
            if hasattr(agent, 'stop'):
                await agent.stop()
        self._initialized = False
        return True
    
    async def get_signals(self, market_data):
        """
        Collect signals from all agents
        
        Returns:
            List of (agent_name, signal_value, confidence, reason) tuples
        """
        if not self._initialized:
            raise RuntimeError("Orchestrator not initialized")
        
        signals = []
        for agent_name, agent in self._agents.items():
            try:
                signal = await agent.get_signal(market_data)
                signals.append((agent_name, signal))
            except Exception as e:
                # Skip agents that fail
                continue
        
        return signals
    
    async def get_signal(self, market_data, agent_name):
        """Get signal from a specific agent"""
        if agent_name in self._agents:
            try:
                signal = await self._agents[agent_name].get_signal(market_data)
                return signal
            except Exception:
                pass
        return None
    
    async def get_decision(self, market_data):
        """
        Get the final trading decision from all agents
        
        Returns:
            Decision object with:
                - action: 'buy', 'sell', 'hold', 'neutral'
                - confidence: aggregated confidence
                - agents: list of supporting agents
        """
        all_signals = await self.get_signals(market_data)
        
        if not all_signals:
            return {'action': 'hold', 'confidence': 0.0, 'reason': 'No signals'}
        
        # Simple signal fusion: weighted by confidence
        total_confidence = 0
        buy_count = 0
        sell_count = 0
        
        for agent_name, signal in all_signals:
            action = signal.get('action', 'neutral')
            confidence = signal.get('confidence', 0.0)
            
            if action == 'buy':
                buy_count += confidence
            elif action == 'sell':
                sell_count += confidence
            
            total_confidence += confidence
        
        # Weighted decision
        total = buy_count + sell_count
        buy_percent = (buy_count / total * 100) if total > 0 else 0
        sell_percent = (sell_count / total * 100) if total > 0 else 0
        
        if buy_percent > sell_percent:
            return {'action': 'buy', 'confidence': buy_percent, 'agents': [s[0] for s in all_signals if s[1].get('action') == 'buy']}
        elif sell_percent > buy_percent:
            return {'action': 'sell', 'confidence': sell_percent, 'agents': [s[0] for s in all_signals if s[1].get('action') == 'sell']}
        else:
            return {'action': 'hold', 'confidence': (buy_percent + sell_percent) / 2, 'reason': 'Market neutral'}
    

class AgentProtocol:
    """Protocol for trading agent"""
    name: str = "agent"
    
    async def get_signal(self, market_data):
        pass
    
    async def connect(self):
        pass
    
    async def start(self):
        pass
    
    async def stop(self):
        pass


class AgentRegistry:
    """Registry for trading agents"""
    def __init__(self):
        self.agents = {}
    
    def add(self, agent):
        self.agents[agent.name] = agent
        return True
    
    def get(self, name):
        return self.agents.get(name)