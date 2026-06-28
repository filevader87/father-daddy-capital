# src/portfolio/rebalance_scheduler.py

import os
import json
from datetime import datetime, timedelta

class RebalanceScheduler:
    def __init__(self, filepath="rebalance_schedule.json", interval_hours=24):
        self.filepath = filepath
        self.interval = timedelta(hours=interval_hours)
        self.last_rebalance_time = self.load_last_rebalance_time()

    def load_last_rebalance_time(self):
        if os.path.exists(self.filepath):
            with open(self.filepath, "r") as f:
                data = json.load(f)
                return datetime.fromisoformat(data.get("last_rebalance", "1970-01-01T00:00:00"))
        return datetime.min

    def update_rebalance_time(self):
        with open(self.filepath, "w") as f:
            json.dump({"last_rebalance": datetime.now().isoformat()}, f)

    def is_rebalance_due(self):
        return datetime.now() - self.last_rebalance_time > self.interval
