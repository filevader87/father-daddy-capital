# src/utils/performance_metrics.py

def log_performance_metrics(agent_name, symbol, action, price, notional, cycle, timestamp):
    log_line = (
        f"[Cycle {cycle}] {agent_name} executed {action.upper()} on {symbol} "
        f"at ${price:.2f} for notional ${notional:.2f} — {timestamp}"
    )
    print(log_line)
    return log_line 