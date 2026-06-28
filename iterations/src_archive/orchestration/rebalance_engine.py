# src/portfolio/rebalance_engine.py

class RebalanceEngine:
    def __init__(self, threshold=0.05):
        self.threshold = threshold  # 5% deviation triggers rebalancing

    def should_rebalance(self, current_allocations, target_allocations):
        rebalance_actions = {}
        for asset, target_weight in target_allocations.items():
            current_weight = current_allocations.get(asset, 0.0)
            deviation = abs(current_weight - target_weight)
            if deviation > self.threshold:
                rebalance_actions[asset] = {
                    "current": current_weight,
                    "target": target_weight,
                    "adjust": round(target_weight - current_weight, 4)
                }
        return rebalance_actions
