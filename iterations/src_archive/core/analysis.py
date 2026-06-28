from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import numpy as np
from src.utils.logger import get_logger
from src.core.memory_bank import MemoryBank
from src.core.decision_graph import DecisionGraph
from src.core.regime_tracker import RegimeTracker

logger = get_logger(__name__)

class SystemAnalyzer:
    def __init__(self, memory_bank: MemoryBank, decision_graph: DecisionGraph, regime_tracker: RegimeTracker):
        self.memory_bank = memory_bank
        self.decision_graph = decision_graph
        self.regime_tracker = regime_tracker
        
    def analyze_decision_quality(self, time_window: timedelta) -> Dict:
        """Analyze the quality of decisions made"""
        decisions = self.decision_graph.get_decisions_by_time_range(
            datetime.now() - time_window,
            datetime.now()
        )
        
        if not decisions:
            return {
                'total_decisions': 0,
                'quality_metrics': {}
            }
            
        rewards = [d.get('reward', 0) for d in decisions]
        regimes = [d['regime'] for d in decisions]
        
        return {
            'total_decisions': len(decisions),
            'quality_metrics': {
                'average_reward': np.mean(rewards),
                'reward_std': np.std(rewards),
                'reward_skew': self._calculate_skewness(rewards),
                'regime_distribution': self._calculate_distribution(regimes)
            }
        }
        
    def analyze_regime_quality(self) -> Dict:
        """Analyze the quality of regime detection"""
        regime_stats = self.regime_tracker.get_regime_stats()
        transitions = self.regime_tracker.get_regime_changes('all')
        
        if not transitions:
            return {
                'total_transitions': 0,
                'quality_metrics': {}
            }
            
        confidences = [t.get('confidence', 0) for t in transitions]
        
        return {
            'total_transitions': len(transitions),
            'quality_metrics': {
                'average_confidence': np.mean(confidences),
                'confidence_std': np.std(confidences),
                'regime_distribution': regime_stats['regime_counts'],
                'transition_frequency': self._calculate_transition_frequency(transitions)
            }
        }
        
    def analyze_memory_quality(self) -> Dict:
        """Analyze the quality of memory usage"""
        agents = self.memory_bank.get_agents()
        memory_stats = {}
        
        for agent in agents:
            keys = self.memory_bank.get_keys(agent)
            timestamps = [
                self.memory_bank.get_latest_timestamp(agent, key)
                for key in keys
            ]
            
            if timestamps:
                memory_stats[agent] = {
                    'total_entries': len(keys),
                    'update_frequency': self._calculate_update_frequency(timestamps),
                    'memory_age': self._calculate_memory_age(timestamps)
                }
                
        return memory_stats
        
    def analyze_system_efficiency(self) -> Dict:
        """Analyze overall system efficiency"""
        decision_quality = self.analyze_decision_quality(timedelta(days=1))
        regime_quality = self.analyze_regime_quality()
        memory_quality = self.analyze_memory_quality()
        
        return {
            'decision_efficiency': {
                'success_rate': decision_quality['quality_metrics'].get('average_reward', 0),
                'consistency': 1 - decision_quality['quality_metrics'].get('reward_std', 0)
            },
            'regime_efficiency': {
                'detection_confidence': regime_quality['quality_metrics'].get('average_confidence', 0),
                'transition_quality': regime_quality['quality_metrics'].get('transition_frequency', {})
            },
            'memory_efficiency': {
                'update_frequency': {
                    agent: stats['update_frequency']
                    for agent, stats in memory_quality.items()
                },
                'memory_utilization': {
                    agent: stats['total_entries']
                    for agent, stats in memory_quality.items()
                }
            }
        }
        
    def analyze_risk_efficiency(self) -> Dict:
        """Analyze risk management efficiency"""
        decisions = self.decision_graph.get_graph()
        if not decisions:
            return {
                'total_decisions': 0,
                'risk_metrics': {}
            }
            
        rewards = [d.get('reward', 0) for d in decisions]
        drawdowns = self._calculate_drawdowns(rewards)
        
        return {
            'total_decisions': len(decisions),
            'risk_metrics': {
                'sharpe_ratio': self._calculate_sharpe_ratio(rewards),
                'sortino_ratio': self._calculate_sortino_ratio(rewards),
                'max_drawdown': max(drawdowns) if drawdowns else 0,
                'average_drawdown': np.mean(drawdowns) if drawdowns else 0,
                'risk_adjusted_return': self._calculate_risk_adjusted_return(rewards, drawdowns)
            }
        }
        
    def _calculate_skewness(self, data: List[float]) -> float:
        """Calculate skewness of data"""
        if not data:
            return 0.0
        return float(np.mean((np.array(data) - np.mean(data)) ** 3) / np.std(data) ** 3)
        
    def _calculate_distribution(self, data: List[Any]) -> Dict[Any, float]:
        """Calculate distribution of data"""
        unique_values = set(data)
        return {
            value: data.count(value) / len(data)
            for value in unique_values
        }
        
    def _calculate_transition_frequency(self, transitions: List[Dict]) -> Dict[str, float]:
        """Calculate frequency of regime transitions"""
        patterns = {}
        for i in range(1, len(transitions)):
            pattern = f"{transitions[i-1]['regime']}->{transitions[i]['regime']}"
            patterns[pattern] = patterns.get(pattern, 0) + 1
            
        total = sum(patterns.values())
        return {
            pattern: count / total
            for pattern, count in patterns.items()
        }
        
    def _calculate_update_frequency(self, timestamps: List[datetime]) -> float:
        """Calculate average time between updates"""
        if len(timestamps) < 2:
            return 0.0
            
        intervals = [
            (timestamps[i] - timestamps[i-1]).total_seconds()
            for i in range(1, len(timestamps))
        ]
        return np.mean(intervals) if intervals else 0.0
        
    def _calculate_memory_age(self, timestamps: List[datetime]) -> float:
        """Calculate average age of memory entries"""
        if not timestamps:
            return 0.0
            
        now = datetime.now()
        ages = [(now - ts).total_seconds() for ts in timestamps]
        return np.mean(ages)
        
    def _calculate_drawdowns(self, returns: List[float]) -> List[float]:
        """Calculate drawdowns from returns"""
        cumulative = np.cumsum(returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = (running_max - cumulative) / running_max
        return drawdowns.tolist()
        
    def _calculate_sharpe_ratio(self, returns: List[float]) -> float:
        """Calculate Sharpe ratio"""
        if not returns:
            return 0.0
        return np.mean(returns) / np.std(returns) if np.std(returns) != 0 else 0.0
        
    def _calculate_sortino_ratio(self, returns: List[float]) -> float:
        """Calculate Sortino ratio"""
        if not returns:
            return 0.0
        negative_returns = [r for r in returns if r < 0]
        downside_std = np.std(negative_returns) if negative_returns else 0
        return np.mean(returns) / downside_std if downside_std != 0 else 0.0
        
    def _calculate_risk_adjusted_return(self, returns: List[float], drawdowns: List[float]) -> float:
        """Calculate risk-adjusted return"""
        if not returns or not drawdowns:
            return 0.0
        return np.mean(returns) / (np.mean(drawdowns) + 1e-6) 