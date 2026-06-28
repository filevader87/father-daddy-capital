class MarketEnv:
    def __init__(self, market_data):
        self.market_data = market_data
        self.current_step = 0
        self.holding = False
        self.entry_price = 0

    def reset(self):
        self.current_step = 0
        self.holding = False
        self.entry_price = 0
        return self._get_state()

    def step(self, action):
        price = self.market_data[self.current_step]
        next_state = self._get_state()

        reward = 0
        profit = 0

        if action == "buy" and not self.holding:
            self.holding = True
            self.entry_price = price
        elif action == "sell" and self.holding:
            profit = price - self.entry_price
            reward = profit
            self.holding = False
        elif action == "hold":
            reward = -0.1  # small penalty to discourage inactivity

        self.current_step += 1
        done = self.current_step >= len(self.market_data) - 1
        return next_state, reward, done

    def _get_state(self):
        price = self.market_data[self.current_step]
        return (round(price, 2), self.holding)