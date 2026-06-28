"""
Agent Protocol - Common interface for trading agents

Provides baseline for:
- Signal generation
- Market data processing  
- Lifecycle management
"""

# Base Agent Protocol
class AgentProtocol:
    """Protocol for trading agent
    
    Agents implement:
    - get_signal(market_data) -> Signal
    - name: str
    
    Optional methods:
    - connect()
    - start()
    - stop()
    """
    def __init__(self):
        self.name = None


class AgentRegistry:
    """Registry for trading agents"""
    def __init__(self):
        self.agents = {}
    
    def add(self, agent):
        """Add an agent to the registry"""
        self.agents[agent.name or id(agent)] = agent
        return True
    
    def get(self, name):
        """Get an agent by name"""
        return self.agents.get(name)
    
    def get_all(self):
        """Get all registered agents"""
        return self.agents.values()