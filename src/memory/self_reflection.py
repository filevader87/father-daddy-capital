def generate_reflection(memory):
    episodes = memory.get_all_episodes()
    best_trade = memory.get_best_trade()
    common_actions = memory.get_common_actions()

    lines = []
    lines.append("🧠 Self-Reflection Summary:")
    lines.append(f"- Total Episodes: {len(episodes)}")
    if best_trade:
        state, action, reward = best_trade
        lines.append(f"- Best trade: Action={action}, Reward={reward:.2f}, State={state}")
    if common_actions:
        top_action, count = common_actions[0]
        lines.append(f"- Most frequent action: {top_action} ({count} times)")
    lines.append("- Recommendation: Continue reinforcing top action in similar states.")

    return "\n".join(lines)