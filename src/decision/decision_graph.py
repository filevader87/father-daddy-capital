
class DecisionGraph:
    def __init__(self):
        self.graph = {}

    def update_graph(self, state, action, reward):
        if state not in self.graph:
            self.graph[state] = {}
        if action not in self.graph[state]:
            self.graph[state][action] = {"count": 0, "reward_sum": 0.0}

        self.graph[state][action]["count"] += 1
        self.graph[state][action]["reward_sum"] += reward

    def get_most_rewarding_action(self, state):
        if state in self.graph:
            action_data = self.graph[state]
            return max(action_data, key=lambda a: action_data[a]["reward_sum"] / action_data[a]["count"])
        return None
