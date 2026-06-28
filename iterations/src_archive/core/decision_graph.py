from datetime import datetime
from typing import Dict, List, Optional
from src.utils.logger import get_logger

logger = get_logger(__name__)

class DecisionGraph:
    def __init__(self):
        self.graph: List[Dict] = []
        self.decision_count = 0
        
    def log_decision(self, state: Dict, action: Dict, regime: str, 
                    reward: Optional[float] = None, timestamp: Optional[datetime] = None):
        """Log a decision with state, action, regime, and optional reward"""
        if timestamp is None:
            timestamp = datetime.now()
            
        decision = {
            "id": self.decision_count,
            "timestamp": timestamp.isoformat(),
            "state": state,
            "action": action,
            "regime": regime,
            "reward": reward
        }
        
        self.graph.append(decision)
        self.decision_count += 1
        logger.info(f"Decision logged: {decision['id']} in {regime} regime")
        
    def get_graph(self) -> List[Dict]:
        """Get the complete decision graph"""
        return self.graph
        
    def get_decisions_by_regime(self, regime: str) -> List[Dict]:
        """Get all decisions for a specific regime"""
        return [d for d in self.graph if d['regime'] == regime]
        
    def get_decisions_by_time_range(self, start_time: datetime, end_time: datetime) -> List[Dict]:
        """Get decisions within a time range"""
        return [d for d in self.graph 
                if start_time <= datetime.fromisoformat(d['timestamp']) <= end_time]
        
    def get_decision_by_id(self, decision_id: int) -> Optional[Dict]:
        """Get a specific decision by ID"""
        for decision in self.graph:
            if decision['id'] == decision_id:
                return decision
        return None
        
    def get_latest_decision(self) -> Optional[Dict]:
        """Get the most recent decision"""
        if not self.graph:
            return None
        return self.graph[-1]
        
    def get_decision_stats(self) -> Dict:
        """Get statistics about decisions"""
        if not self.graph:
            return {
                "total_decisions": 0,
                "regime_counts": {},
                "average_reward": 0.0
            }
            
        regime_counts = {}
        total_reward = 0.0
        reward_count = 0
        
        for decision in self.graph:
            regime = decision['regime']
            regime_counts[regime] = regime_counts.get(regime, 0) + 1
            
            if decision['reward'] is not None:
                total_reward += decision['reward']
                reward_count += 1
                
        return {
            "total_decisions": len(self.graph),
            "regime_counts": regime_counts,
            "average_reward": total_reward / reward_count if reward_count > 0 else 0.0
        }
        
    def clear(self):
        """Clear the decision graph"""
        self.graph = []
        self.decision_count = 0
        logger.info("Decision graph cleared")
