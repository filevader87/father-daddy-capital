import os
import sys
from datetime import datetime

# Add src directory to system path
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

# Imports from internal modules
from src.agents.short_term.crypto_aets import CryptoAETS
from src.agents.short_term.stock_aets import StockAETS
from src.utils.performance_metrics import log_performance_metrics
from src.performance.visualizer import visualize_agent_signals
from src.utils.notifier import send_telegram_message

LOG_DIR = "logs"

def log_action(agent_name, message):
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"{agent_name}_{today}.log")
    with open(log_file, "a") as f:
        f.write(f"[{datetime.now()}] {message}\n")

def short_test_loop(cycles=10):
    print(f"🚀 Running SHORT TEST simulation ({cycles} cycles)...")
    crypto_assets = ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "RNDRUSD", "XRPUSD", "ADAUSD"]
    stock_assets = ["AAPL", "MSFT", "NVDA", "TSLA"]
    total_allocated = 0

    for cycle in range(cycles):
        print(f"\n🔁 Cycle {cycle + 1}/{cycles}")

        for symbol in crypto_assets:
            agent = CryptoAETS(symbol=symbol)
            result = agent.run_cycle()
            if result:
                total_allocated += result["notional"]
                log_action("CryptoAETS", result["log"])
                log_performance_metrics("CryptoAETS", symbol, result["log"].split()[0],
                                        float(result["log"].split("$")[-1]),
                                        result["notional"], cycle + 1,
                                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                print(result["log"])

        for symbol in stock_assets:
            agent = StockAETS(symbol=symbol)
            result = agent.run_cycle()
            if result:
                total_allocated += result["notional"]
                log_action("StockAETS", result["log"])
                log_performance_metrics("StockAETS", symbol, result["log"].split()[0],
                                        float(result["log"].split("$")[-1]),
                                        result["notional"], cycle + 1,
                                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                print(result["log"])

    summary = f"📊 {cycles} cycles complete. Total allocated: ${total_allocated:.2f}"
    print(summary)
    send_telegram_message(summary)

if __name__ == "__main__":
    short_test_loop()
