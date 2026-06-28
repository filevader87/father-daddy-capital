import numpy as np
import random

class QLearningAgent:
    def __init__(self, actions, alpha=0.1, gamma=0.95, epsilon=0.1):
        self.q_table = {}  # state-action-value mapping
        self.actions = actions  # ['buy', 'sell', 'hold']
        self.alpha = alpha  # learning rate
        self.gamma = gamma  # discount factor
        self.epsilon = epsilon  # exploration rate

    def get_state_key(self, state):
        return str(state)

    def choose_action(self, state):
        key = self.get_state_key(state)
        if random.uniform(0, 1) < self.epsilon or key not in self.q_table:
            return random.choice(self.actions)
        return max(self.q_table[key], key=self.q_table[key].get)

    def learn(self, state, action, reward, next_state):
        state_key = self.get_state_key(state)
        next_state_key = self.get_state_key(next_state)

        if state_key not in self.q_table:
            self.q_table[state_key] = {a: 0 for a in self.actions}
        if next_state_key not in self.q_table:
            self.q_table[next_state_key] = {a: 0 for a in self.actions}

        predict = self.q_table[state_key][action]
        target = reward + self.gamma * max(self.q_table[next_state_key].values())
        self.q_table[state_key][action] += self.alpha * (target - predict)