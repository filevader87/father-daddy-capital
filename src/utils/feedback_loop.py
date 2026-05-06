class FeedbackLoop:
    def __init__(self):
        self.history = []

    def log_feedback(self, agent_name, signal, result, reward):
        self.history.append({
            "agent": agent_name,
            "signal": signal,
            "result": result,
            "reward": reward
        })

    def get_feedback(self):
        return self.history
