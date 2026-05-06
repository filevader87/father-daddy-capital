
# src/orchestration/agent_state_registry.py

class AgentStateRegistry:
    def __init__(self):
        self.registry = {}

    def register_agent(self, name, role, initial_value):
        self.registry[name] = {
            "role": role,
            "value": initial_value,
            "trades": [],
            "signals": [],
        }

    def update_agent(self, name, profit, signal):
        if name in self.registry:
            self.registry[name]["value"] += profit
            if signal:
                self.registry[name]["signals"].append(signal)
            self.registry[name]["trades"].append({
                "profit": profit,
                "signal": signal,
            })

    def get_agent_state(self, name):
        return self.registry.get(name, None)

    def get_all_states(self):
        return self.registry

    def broadcast_signal(self, source, signal):
        for agent in self.registry:
            if agent != source:
                self.registry[agent]["signals"].append(signal)

