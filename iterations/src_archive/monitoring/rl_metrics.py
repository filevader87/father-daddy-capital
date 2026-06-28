from prometheus_client import Counter, Gauge, Histogram, start_http_server
from typing import Dict, Any
import logging
from datetime import datetime

class RLMetrics:
    """Exports RL training metrics to Prometheus."""
    
    def __init__(self, port: int = 8000):
        self.logger = logging.getLogger(__name__)
        
        # Training metrics
        self.episode_counter = Counter(
            'rl_episodes_total',
            'Total number of episodes completed'
        )
        self.steps_counter = Counter(
            'rl_steps_total',
            'Total number of steps taken'
        )
        self.reward_gauge = Gauge(
            'rl_reward',
            'Current episode reward'
        )
        self.avg_reward_gauge = Gauge(
            'rl_avg_reward',
            'Average reward over last 100 episodes'
        )
        self.q_value_gauge = Gauge(
            'rl_q_value',
            'Current Q-value for state-action pair',
            ['state', 'action']
        )
        
        # Performance metrics
        self.episode_duration = Histogram(
            'rl_episode_duration_seconds',
            'Duration of episodes in seconds',
            buckets=[1, 5, 10, 30, 60, 120, 300, 600]
        )
        self.step_duration = Histogram(
            'rl_step_duration_seconds',
            'Duration of steps in seconds',
            buckets=[0.1, 0.5, 1, 2, 5, 10]
        )
        
        # Learning metrics
        self.learning_rate_gauge = Gauge(
            'rl_learning_rate',
            'Current learning rate'
        )
        self.exploration_rate_gauge = Gauge(
            'rl_exploration_rate',
            'Current exploration rate'
        )
        self.discount_factor_gauge = Gauge(
            'rl_discount_factor',
            'Current discount factor'
        )
        
        # Start Prometheus HTTP server
        start_http_server(port)
        self.logger.info(f"Started Prometheus metrics server on port {port}")
        
    def record_episode(self, 
                      episode: int,
                      reward: float,
                      avg_reward: float,
                      duration: float):
        """Record episode metrics."""
        self.episode_counter.inc()
        self.reward_gauge.set(reward)
        self.avg_reward_gauge.set(avg_reward)
        self.episode_duration.observe(duration)
        
    def record_step(self,
                   state: str,
                   action: str,
                   q_value: float,
                   duration: float):
        """Record step metrics."""
        self.steps_counter.inc()
        self.q_value_gauge.labels(state=state, action=action).set(q_value)
        self.step_duration.observe(duration)
        
    def update_hyperparameters(self,
                             learning_rate: float,
                             exploration_rate: float,
                             discount_factor: float):
        """Update hyperparameter metrics."""
        self.learning_rate_gauge.set(learning_rate)
        self.exploration_rate_gauge.set(exploration_rate)
        self.discount_factor_gauge.set(discount_factor)
        
    def record_batch_update(self,
                          batch_size: int,
                          update_duration: float):
        """Record batch update metrics."""
        self.step_duration.observe(update_duration)
        
    def record_error(self, error_type: str):
        """Record error metrics."""
        Counter(
            'rl_errors_total',
            'Total number of errors by type',
            ['type']
        ).labels(type=error_type).inc()
        
    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics values."""
        return {
            'episodes': self.episode_counter._value.get(),
            'steps': self.steps_counter._value.get(),
            'current_reward': self.reward_gauge._value.get(),
            'avg_reward': self.avg_reward_gauge._value.get(),
            'learning_rate': self.learning_rate_gauge._value.get(),
            'exploration_rate': self.exploration_rate_gauge._value.get(),
            'discount_factor': self.discount_factor_gauge._value.get()
        } 