
import json
from datetime import datetime
import os

class FeedbackLoop:
    def __init__(self, log_path='logs/feedback_log.json'):
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        if not os.path.exists(log_path):
            with open(log_path, 'w') as f:
                json.dump([], f)

    def record_feedback(self, agent_name, signal, action_taken, outcome, score):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "agent": agent_name,
            "signal": signal,
            "action": action_taken,
            "outcome": outcome,
            "score": score
        }
        with open(self.log_path, 'r+') as f:
            data = json.load(f)
            data.append(entry)
            f.seek(0)
            json.dump(data, f, indent=2)

    def get_recent_feedback(self, n=10):
        with open(self.log_path, 'r') as f:
            data = json.load(f)
        return data[-n:]
