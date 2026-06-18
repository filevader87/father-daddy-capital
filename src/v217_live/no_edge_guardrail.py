#!/usr/bin/env python3
"""No-Edge Guardrail — reusable module to block duplicate failed strategy tests.
Import and call check_strategy() before launching any new strategy test.
"""
import json, os
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent.parent
REGISTRY_PATH = BASE / "output/v21761_no_edge_guardrail/failed_strategy_registry.json"

def load_registry():
    if not os.path.exists(str(REGISTRY_PATH)):
        return {"killed_variants": []}
    with open(str(REGISTRY_PATH)) as f:
        return json.load(f)

def check_strategy(asset, interval, side, entry_bucket, profit_target, 
                   entry_rule, exit_rule, hypothesis_class=None):
    """
    Check if a proposed strategy is materially similar to a killed variant.
    Returns (blocked: bool, reason: str).
    """
    registry = load_registry()
    killed = registry.get("killed_variants", [])
    
    # If no hypothesis class or forbidden class, block
    allowed_hypotheses = [
        "ORDER_FLOW_IMBALANCE", "CVD_OBI_ALIGNMENT", "ORACLE_REFERENCE_DISLOCATION",
        "CHAINLINK_RTDS_LAG", "RARE_BREAKAWAY_CONTINUATION", "LIQUIDITY_SHOCK_REPRICING",
        "SETTLEMENT_SOURCE_DIVERGENCE", "VOLATILITY_EXPANSION_DISLOCATION",
        "ORDERBOOK_STRESS_DISLOCATION", "CROSS_ASSET_REFLEXIVE_MOVE"
    ]
    forbidden = [
        "bucket-only scalp", "asset-only entry", "side-only entry", "cheap-token buying",
        "generic 30-60c repricing", "historical Markov memory", "streak continuation",
        "RSI/MACD/EMA/VWAP stacking"
    ]
    
    if hypothesis_class is None or hypothesis_class.lower() in [f.lower() for f in forbidden]:
        return True, "NO_CAUSAL_HYPOTHESIS: Strategy must have a causal hypothesis from allowed list."
    
    if hypothesis_class not in allowed_hypotheses:
        return True, f"FORBIDDEN_HYPOTHESIS: {hypothesis_class} not in allowed list."
    
    # Check for material similarity to killed variants
    for variant in killed:
        similarity_score = 0
        total_checks = 0
        
        if variant.get("asset") in (asset, "ALL") or asset == "ALL":
            if variant.get("asset") != "ALL" and asset != "ALL" and variant.get("asset") != asset:
                pass  # different specific assets, no match
            else:
                similarity_score += 1
        total_checks += 1
        
        if variant.get("interval") == interval:
            similarity_score += 1
        total_checks += 1
        
        if variant.get("entry_bucket") == entry_bucket and entry_bucket != "N/A":
            similarity_score += 2  # bucket match is strong signal
        total_checks += 1
        
        if "scalp" in str(variant.get("exit_rule", "")).lower() and "scalp" in str(exit_rule).lower():
            similarity_score += 2  # scalp exit match is strong
        total_checks += 1
        
        if variant.get("profit_target") == profit_target and profit_target != "N/A":
            similarity_score += 1
        total_checks += 1
        
        similarity_ratio = similarity_score / total_checks if total_checks > 0 else 0
        
        if similarity_ratio >= 0.6:
            return True, f"DUPLICATE_FAILED_STRATEGY: Materially similar to {variant['strategy_id']} (similarity: {similarity_ratio:.0%})"
    
    return False, "PASS: No duplicate detected, causal hypothesis provided."

if __name__ == "__main__":
    # Self-test
    tests = [
        {"asset": "BTC", "interval": "5m", "side": "BOTH", "entry_bucket": "30-60c",
         "profit_target": "+3c", "entry_rule": "bucket filter", "exit_rule": "scalp exit +3c",
         "hypothesis_class": None},
        {"asset": "BTC", "interval": "5m", "side": "BOTH", "entry_bucket": "30-60c",
         "profit_target": "+3c", "entry_rule": "bucket filter", "exit_rule": "scalp exit +3c",
         "hypothesis_class": "ORDER_FLOW_IMBALANCE"},
        {"asset": "ETH", "interval": "5m", "side": "BOTH", "entry_bucket": "30-60c",
         "profit_target": "+2c", "entry_rule": "bucket filter", "exit_rule": "scalp exit +2c",
         "hypothesis_class": "ORDER_FLOW_IMBALANCE"},
        {"asset": "BTC", "interval": "15m", "side": "UP", "entry_bucket": "12-20c",
         "profit_target": "N/A", "entry_rule": "order flow divergence", "exit_rule": "hold to expiry",
         "hypothesis_class": "CVD_OBI_ALIGNMENT"},
    ]
    
    print("No-Edge Guardrail Self-Test")
    print("=" * 60)
    for i, t in enumerate(tests):
        blocked, reason = check_strategy(**t)
        status = "BLOCKED" if blocked else "ALLOWED"
        print(f"\nTest {i+1}: {status}")
        print(f"  {reason}")