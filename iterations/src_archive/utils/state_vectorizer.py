def vectorize_state(price, indicators, timestep, regime=None):
    return [round(price, 2), round(indicators.get("RSI", 50), 2), round(indicators.get("MACD", 0), 2), timestep, regime or "Unknown"]
