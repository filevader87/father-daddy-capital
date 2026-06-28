
def generate_state_vector(price, rsi, macd, sentiment, timestep, regime):
    return (round(price, 2), round(rsi, 2), round(macd, 2), sentiment, timestep, regime)
