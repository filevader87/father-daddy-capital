class EpisodicMemory:
    def __init__(self):
        self.memory = []

    def store_episode(self, episode_log):
        self.memory.append(episode_log)

    def get_all_episodes(self):
        return self.memory

    def get_best_trade(self):
        best = None
        max_reward = float('-inf')
        for episode in self.memory:
            for state, action, reward in episode:
                if reward > max_reward:
                    max_reward = reward
                    best = (state, action, reward)
        return best

    def get_common_actions(self):
        from collections import Counter
        actions = []
        for episode in self.memory:
            actions += [a for _, a, _ in episode]
        return Counter(actions).most_common()