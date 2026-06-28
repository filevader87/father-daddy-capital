import matplotlib.pyplot as plt
import os
from datetime import datetime

def visualize_agent_signals(agent_name, symbol, signal_history, output_dir="logs"):
    """
    Generate a simple time-series plot of trading signals.
    signal_history: list of (timestamp, signal_string)
    """
    if not signal_history:
        print(f"No signals to plot for {symbol}.")
        return

    times, signals = zip(*signal_history)
    signal_values = [1 if s == "buy" else -1 if s == "sell" else 0 for s in signals]

    plt.figure(figsize=(10, 4))
    plt.plot(times, signal_values, marker="o", linestyle="-", label="Signal")
    plt.title(f"{agent_name} - {symbol} Signals")
    plt.yticks([-1, 0, 1], ["SELL", "HOLD", "BUY"])
    plt.grid(True)
    plt.xlabel("Time")
    plt.ylabel("Signal")

    os.makedirs(output_dir, exist_ok=True)
    filename = f"{agent_name}_{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    path = os.path.join(output_dir, filename)
    plt.savefig(path)
    plt.close()
    print(f"[Visualizer] Saved signal plot to {path}")
