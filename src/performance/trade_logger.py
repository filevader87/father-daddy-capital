
import os
import json
from datetime import datetime

LOG_DIR = "logs"

def log_trade(agent_name, symbol, action, price, qty, profit=0.0, reasoning=None):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "symbol": symbol,
        "action": action,
        "price": price,
        "qty": qty,
        "profit": profit,
        "reasoning": reasoning
    }
    filename = os.path.join(LOG_DIR, f"{agent_name}_trades.json")
    if os.path.exists(filename):
        with open(filename, "r") as f:
            data = json.load(f)
    else:
        data = []
    data.append(log_entry)
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)
