from src.agents.short_term.crypto_aets import CryptoAETS
from src.agents.short_term.stock_aets import StockAETS
import random

def simulate_market_data(length=100):
    base_price = 100
    return [round(base_price + random.uniform(-2, 2) * i, 2) for i in range(length)]

def static_logic_strategy(market_data):
    holding = False
    pnl = 0
    for price in market_data:
        if price < 90 and not holding:
            holding = True
            entry = price
        elif price > 110 and holding:
            pnl += price - entry
            holding = False
    return pnl

def train_multiple_episodes(agent_class, episodes=10):
    agent = agent_class(training_mode=True)
    all_rewards = []

    for episode in range(episodes):
        data = simulate_market_data()
        agent.set_market_data(data)
        agent.run_training_episode()

        total_reward = sum([r for (_, _, r) in agent.trade_log])
        all_rewards.append(total_reward)

        print(f"Episode {episode + 1}: Total Reward = {total_reward:.2f}")

    return all_rewards

def evaluate_rl_vs_static(agent_class, episodes=10):
    print(f"Training {agent_class.__name__} for {episodes} episodes...")
    rl_rewards = train_multiple_episodes(agent_class, episodes=episodes)

    static_results = []
    for _ in range(episodes):
        data = simulate_market_data()
        pnl = static_logic_strategy(data)
        static_results.append(pnl)

    avg_rl = sum(rl_rewards) / len(rl_rewards)
    avg_static = sum(static_results) / len(static_results)

    print("\nPerformance Summary:")
    print(f"Average RL Agent Reward:    {avg_rl:.2f}")
    print(f"Average Static Logic PnL:   {avg_static:.2f}")