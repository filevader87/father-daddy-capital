# src/engine/strategy_orchestrator.py

from src.orchestration.agent_state_registry import AgentStateRegistry

class StrategyOrchestrator:
    def __init__(self, registry=None):
        # Allow injecting an external registry or create a new one by default
        self.registry = registry if registry else AgentStateRegistry()

    def register_agent(self, name, role="general", initial_value=10000):
        self.registry.register_agent(name, role, initial_value)

    def update_agent_profit(self, name, profit, signal=None):
        self.registry.update_agent(name, profit, signal)

    def get_agent_info(self, name):
        return self.registry.get_agent_state(name)

    def get_all_info(self):
        return self.registry.get_all_states()

    def register_signal(self, agent_name, symbol, signal):
        self.registry.update_agent(
            name=agent_name,
            profit=0.0,
            signal={"symbol": symbol, "signal": signal}
        )

    def broadcast_signal(self, source, signal):
        self.registry.broadcast_signal(source, signal)

    def summarize_allocations(self):
        summary = []
        for agent_name, state in self.registry.get_all_states().items():
            summary.append(f"{agent_name}: ${state['value']:.2f}")
        return "\n".join(summary)
