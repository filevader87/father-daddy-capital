from prometheus_client import start_http_server, Counter, Gauge, Histogram
import time
import psutil

# System metrics
cpu_usage = Gauge('system_cpu_usage_percent', 'Current CPU usage percentage')
memory_usage = Gauge('system_memory_usage_bytes', 'Current memory usage in bytes')
disk_usage = Gauge('system_disk_usage_percent', 'Current disk usage percentage')

# Trading metrics
trades_executed = Counter('trading_trades_executed_total', 'Total number of trades executed', ['strategy', 'symbol'])
trade_volume = Counter('trading_volume_total', 'Total trading volume', ['strategy', 'symbol'])
trade_latency = Histogram('trading_latency_seconds', 'Trade execution latency', ['strategy'])

# Backtest metrics
backtest_runs = Counter('backtest_runs_total', 'Total number of backtests run', ['strategy'])
backtest_duration = Histogram('backtest_duration_seconds', 'Backtest execution duration')
optimization_iterations = Counter('optimization_iterations_total', 'Total number of optimization iterations')

# Agent metrics
agent_calls = Counter('agent_calls_total', 'Total number of agent calls', ['agent_type'])
agent_latency = Histogram('agent_latency_seconds', 'Agent execution latency', ['agent_type'])
agent_errors = Counter('agent_errors_total', 'Total number of agent errors', ['agent_type', 'error_type'])

def collect_system_metrics():
    """Collect and update system metrics."""
    cpu_usage.set(psutil.cpu_percent())
    memory = psutil.virtual_memory()
    memory_usage.set(memory.used)
    disk = psutil.disk_usage('/')
    disk_usage.set(disk.percent)

def main():
    # Start up the server to expose the metrics
    start_http_server(9090)
    
    # Update system metrics every 15 seconds
    while True:
        collect_system_metrics()
        time.sleep(15)

if __name__ == '__main__':
    main() 