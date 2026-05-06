"""
Short-Term Agents Testing Script
-------------------------------
This script is designed for testing and validating the short-term trading agents. It:
1. Runs a limited number of trading cycles (default: 10)
2. Tests both CryptoAETS and StockAETS agents
3. Includes detailed logging and notifications
4. Provides performance metrics and visualization

This script is NOT for production use. It is specifically designed for:
- Agent validation
- Performance testing
- System debugging
- Development and testing environments

For production deployment, use deploy_loop.py instead.
"""
import os
import sys
import time
import random
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

from src.agents.short_term.crypto_aets import CryptoAETS
from src.agents.short_term.stock_aets import StockAETS
from src.utils.performance_metrics import log_performance_metrics
from src.visualization.visualizer import visualize_agent_signals
from src.utils.notifier import send_telegram_message

LOG_DIR = "logs"

def log_action(agent_name, message):
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"{agent_name}_{today}.log")
    with open(log_file, "a") as f:
        f.write(f"[{datetime.now()}] {message}\n")

def short_test_loop(cycles=2):
    print(f"Running SHORT TEST simulation ({cycles} cycles)...")
    crypto_assets = ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "RNDRUSD", "XRPUSD", "ADAUSD"]
    stock_assets = ["AAPL", "MSFT", "NVDA", "TSLA"]
    total_allocated = 0

    for cycle in range(cycles):
        for symbol in crypto_assets:
            agent = CryptoAETS(symbol=symbol)
            result = agent.run_cycle()
            if result:
                total_allocated += result["notional"]
                log_action("CryptoAETS", result["log"])
                print(result["log"])
        
        for symbol in stock_assets:
            agent = StockAETS(symbol=symbol)
            result = agent.run_cycle()
            if result:
                total_allocated += result["notional"]
                log_action("StockAETS", result["log"])
                print(result["log"])

        send_telegram_message(f"📊 Cycle {cycle+1} complete. Total allocated: ${total_allocated:.2f}")

if __name__ == "__main__":
    short_test_loop(cycles=10)