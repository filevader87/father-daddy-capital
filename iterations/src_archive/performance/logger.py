
import os
import json
from datetime import datetime

LOG_PATH = "logs/performance_log.json"

def log_performance(agent_name, metrics):
    if not os.path.exists("logs"):
        os.makedirs("logs")
    entry = {
        "timestamp": datetime.now().isoformat(),
        "agent": agent_name,
        "metrics": metrics
    }
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r") as f:
            data = json.load(f)
    else:
        data = []
    data.append(entry)
    with open(LOG_PATH, "w") as f:
        json.dump(data, f, indent=2)
