
import numpy as np
import pandas as pd

class RiskManager:
    def __init__(self, max_leverage=4.0, base_atr_multiplier=2.0, max_spread_ratio=0.001):
        self.max_leverage = max_leverage
        self.base_atr_multiplier = base_atr_multiplier
        self.max_spread_ratio = max_spread_ratio

    def calculate_leverage(self, volatility, conviction_score, account_drawdown):
        """Dynamically adjust leverage based on risk conditions."""
        base_leverage = min(self.max_leverage, 1 + conviction_score * 2)
        volatility_factor = max(0.5, 1 - volatility)
        drawdown_factor = max(0.25, 1 - (account_drawdown / 20))

        adjusted_leverage = base_leverage * volatility_factor * drawdown_factor
        return min(self.max_leverage, max(1, adjusted_leverage))

    def calculate_trailing_stops(self, df):
        """Vectorized trailing stop calculation."""
        df['trailing_long_stop'] = df['long_stop'].cummax()
        df['trailing_short_stop'] = df['short_stop'].cummin()
        return df
    