import numpy as np
import random
import json
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)

class MemoryQLearningAgent:
    def __init__(self, actions=None, alpha=0.1, gamma=0.95, epsilon=0.1):
        self.actions = actions if actions else ['BUY', 'SELL', 'HOLD']
        self.q_table = defaultdict(lambda: np.zeros(len(self.actions)))
        self.alpha = alpha  # Learning rate
        self.gamma = gamma  # Discount factor
        self.epsilon = epsilon  # Exploration rate
        self.last_state = None
        self.last_action = None

    def get_state_key(self, state):
        numeric_part = [float(x) for x in state if isinstance(x, (int, float, np.number))]
        string_part = [str(x) for x in state if isinstance(x, str)]
        rounded_numeric = tuple(np.round(numeric_part, 2))
        return rounded_numeric + tuple(string_part)

    def choose_action(self, state):
        """Choose action with enhanced validation and error handling."""
        try:
            # Validate state
            if state is None or len(state) == 0:
                logger.warning("Invalid state provided to choose_action")
                return 'HOLD'
            
            # Convert state to key with validation
            try:
                state_key = self.get_state_key(state)
            except Exception as e:
                logger.error(f"Error converting state to key: {e}")
                return 'HOLD'
            
            # Initialize Q-table entry if needed
            if state_key not in self.q_table:
                self.q_table[state_key] = np.zeros(len(self.actions))
            
            # Validate Q-table entry
            if len(self.q_table[state_key]) != len(self.actions):
                logger.warning("Q-table entry length mismatch, reinitializing")
                self.q_table[state_key] = np.zeros(len(self.actions))
            
            # Choose action with exploration vs exploitation
            if random.random() < self.epsilon:
                action_index = random.randint(0, len(self.actions) - 1)
            else:
                # Find best action, with tie-breaking
                max_q = np.max(self.q_table[state_key])
                best_actions = np.where(self.q_table[state_key] == max_q)[0]
                action_index = np.random.choice(best_actions)
            
            # Validate action index
            if action_index < 0 or action_index >= len(self.actions):
                logger.warning(f"Invalid action index: {action_index}, defaulting to HOLD")
                action_index = self.actions.index('HOLD') if 'HOLD' in self.actions else 0
            
            # Get action and validate
            action = self.actions[action_index]
            if action not in self.actions:
                logger.warning(f"Invalid action returned: {action}, defaulting to HOLD")
                action = 'HOLD'
            
            # Store for update
            self.last_state = state_key
            self.last_action = action_index
            
            return action
            
        except Exception as e:
            logger.error(f"Error in choose_action: {e}")
            return 'HOLD'

    def update(self, new_state, reward):
        """Online mode: update from previous state/action to new state"""
        if self.last_state is None or self.last_action is None:
            return

        new_state_key = self.get_state_key(new_state)
        if new_state_key not in self.q_table:
            self.q_table[new_state_key] = np.zeros(len(self.actions))

        best_future_q = np.max(self.q_table[new_state_key])
        td_target = reward + self.gamma * best_future_q
        td_error = td_target - self.q_table[self.last_state][self.last_action]
        self.q_table[self.last_state][self.last_action] += self.alpha * td_error

        self.last_state = None
        self.last_action = None

    def update_explicit(self, state, action, reward, next_state):
        """Batch mode: update from a full transition"""
        state_key = self.get_state_key(state)
        next_state_key = self.get_state_key(next_state)

        if state_key not in self.q_table:
            self.q_table[state_key] = np.zeros(len(self.actions))
        if next_state_key not in self.q_table:
            self.q_table[next_state_key] = np.zeros(len(self.actions))

        action_index = self.actions.index(action)
        best_future_q = np.max(self.q_table[next_state_key])
        td_target = reward + self.gamma * best_future_q
        td_error = td_target - self.q_table[state_key][action_index]
        self.q_table[state_key][action_index] += self.alpha * td_error

    def save(self, filepath):
        """Save Q-table to disk as JSON"""
        serialized_q = {str(key): list(values) for key, values in self.q_table.items()}
        with open(filepath, 'w') as f:
            json.dump(serialized_q, f, indent=2)

    def load(self, filepath):
        """Load Q-table from disk"""
        with open(filepath, 'r') as f:
            loaded_q = json.load(f)
            for key, values in loaded_q.items():
                parsed_key = eval(key)
                self.q_table[parsed_key] = np.array(values)

    def print_summary(self, limit=10):
        """Print a summary of top Q-table entries"""
        print("\n--- Q-Learning Summary ---")
        for i, (state, values) in enumerate(self.q_table.items()):
            print(f"State: {state} → Q: {np.round(values, 3)}")
            if i + 1 >= limit:
                break
        print("--------------------------\n")

