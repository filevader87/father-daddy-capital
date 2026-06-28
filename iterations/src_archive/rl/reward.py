def calculate_reward(profit, drawdown, volatility):
    """
    Reward function based on profit, drawdown, and risk.
    """
    reward = profit - drawdown * 0.5 - volatility * 0.2
    return reward