
def calculate_win_rate(trades):
    wins = [t for t in trades if t['profit'] > 0]
    return len(wins) / len(trades) if trades else 0

def calculate_average_return(trades):
    return sum(t['profit'] for t in trades) / len(trades) if trades else 0

def calculate_max_drawdown(trade_values):
    peak = trade_values[0]
    max_dd = 0
    for value in trade_values:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)
    return max_dd
