#!/usr/bin/env python3
"""
FDC Scaled Entry Engine
Extracted from 0xce25e214d5cfe4f459cf67f08df581885aae7fdc bot analysis.
250 trades, 100% WR, +$338,705 P&L. All BTC 15-min "Up or Down" contracts.

Core strategy patterns extracted:
  1. Force-entry: enter at signal, don't wait for optimal price
  2. Multi-scale: 2-4 entries per contract window at different prices
  3. BTC-dominant: 97% BTC, 3% ETH — ignore everything else
  4. Wide price band: entry at 0.10-0.72 regardless of edge calculation
  5. Fast churn: all positions resolve within 15 min

Author: Hugh (3rd of 5)
Source: Live bot analysis, 250 closed trades
Date: 2026-05-15
"""

from __future__ import annotations
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import math


# ══════════════════════════════════════════════════════════════════════════════
# Configuration (from bot observation + FDC adaptation)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScaledEntryConfig:
    """Parameters extracted from 0xce25 bot's actual behavior."""

    # Entry scaling
    max_entries_per_contract: int = 3    # Bot uses 2-4 entries per window
    base_entry_size: float = 5.0         # Min bet (adapted for our $250 bankroll)
    size_multiplier: float = 1.5         # Each subsequent entry is larger
    max_position_usd: float = 25.0       # Cap total exposure per window

    # Price tolerance (the bot enters at ANY favorable price)
    min_contract_price: float = 0.05    # Bot buys as low as 0.10, as high as 0.72
    max_contract_price: float = 0.80
    force_entry: bool = True            # Enter at signal regardless of edge calc
    min_edge_override: float = 0.005    # Near-zero edge threshold when force_entry

    # Asset filter (97% BTC)
    preferred_assets: List[str] = None   # ['Bitcoin'] — set in __post_init__
    allow_fallback: bool = True          # Allow ETH if no BTC contracts

    # Position management
    max_concurrent_positions: int = 5    # Bot holds 5-10 open
    min_time_between_entries: int = 1    # 1 minute between scale-in entries

    def __post_init__(self):
        if self.preferred_assets is None:
            self.preferred_assets = ["Bitcoin"]


# ══════════════════════════════════════════════════════════════════════════════
# Scaled Entry Logic
# ══════════════════════════════════════════════════════════════════════════════

def filter_contracts_by_asset(
    contracts: List[dict],
    preferred: List[str],
) -> List[dict]:
    """Filter contracts to preferred assets only. Bot is 97% BTC."""
    filtered = []
    for c in contracts:
        question = c.get("question", "")
        for asset in preferred:
            if asset.lower() in question.lower():
                filtered.append(c)
                break
    return filtered or contracts  # Fallback to all if none match


def compute_entry_tiers(
    base_size: float,
    multiplier: float,
    max_tiers: int,
    max_total: float,
) -> List[float]:
    """Compute scaled entry sizes. Each tier is larger than the previous."""
    tiers = []
    total = 0.0
    for i in range(max_tiers):
        size = min(base_size * (multiplier ** i), max_total - total)
        if size <= 0:
            break
        tiers.append(round(size, 2))
        total += size
        if total >= max_total:
            break
    return tiers


def evaluate_scaled_entries(
    signal: dict,
    contracts: List[dict],
    state: dict,
    config: Optional[ScaledEntryConfig] = None,
) -> List[dict]:
    """
    Generate scaled entry orders using the bot's strategy.

    Unlike the traditional Kelly approach that waits for edge and calibrates,
    this uses force-entry: if signal says direction, enter. Scale in with
    increasing size. The bot's 100% WR comes from being right about direction,
    not from optimizing entry price.

    Args:
        signal: btc_signal() output with direction, confidence, prices
        contracts: discovered contracts from pm_engine
        state: FDC state dict with bankroll, positions
        config: ScaledEntryConfig (uses defaults if None)

    Returns:
        List of entry orders (compatible with pm_engine format)
    """
    if config is None:
        config = ScaledEntryConfig()

    direction = signal.get("direction", "neutral")
    confidence = signal.get("confidence", 0)
    btc_price = signal.get("price", 0)

    if direction == "neutral":
        return []
    if confidence < 0.05:  # Near-zero bar — force entry at any signal
        return []

    # Filter to preferred assets
    filtered = filter_contracts_by_asset(contracts, config.preferred_assets)
    if not filtered and config.allow_fallback:
        filtered = contracts

    bankroll = state.get("bankroll", 250)
    positions = state.get("positions", {})
    invested = sum(p.get("bet", 0) for p in positions.values())
    available = max(0, bankroll - invested)

    # Build candidates
    candidates = []
    for c in filtered:
        ep = c.get("up_price") if direction == "up" else c.get("down_price", 0)
        if not (config.min_contract_price < ep < config.max_contract_price):
            continue

        # Force-entry: edge doesn't need to be positive, just not catastrophic
        edge = confidence - ep
        if edge < config.min_edge_override and not config.force_entry:
            continue

        # Prefer expiry within 15 min (short-duration), but accept daily
        mins = c.get("mins_to_expiry", 15)
        score = edge * (1.0 if mins <= 15 else 0.5)  # Penalize daily contracts

        candidates.append({
            "contract": c,
            "side": "Up" if direction == "up" else "Down",
            "price": ep,
            "edge": round(edge, 4),
            "mins": mins,
            "score": round(score, 4),
        })

    if not candidates:
        return []

    # Sort by score (edge × time decay)
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Compute entry tiers
    tiers = compute_entry_tiers(
        config.base_entry_size,
        config.size_multiplier,
        config.max_entries_per_contract,
        config.max_position_usd,
    )

    entries = []
    used_contracts = set()

    for cand in candidates:
        cid = cand["contract"].get("conditionId", "")
        key = cid[:16] + "_" + cand["side"]

        if key in positions or key in used_contracts:
            continue
        if len(positions) + len(entries) >= config.max_concurrent_positions:
            break
        if available < config.base_entry_size:
            break

        # Apply entry tiers to this contract
        for tier_size in tiers:
            if available < tier_size:
                break

            bet = min(tier_size, available)
            entries.append({
                "action": f"BUY_{cand['side']}",
                "question": cand["contract"].get("question", ""),
                "conditionId": cid,
                "contract_price": cand["price"],
                "bet": round(bet, 2),
                "edge": cand["edge"],
                "price_at_entry": round(btc_price, 2),
                "signal_conf": confidence,
                "signal_rsi": signal.get("rsi", 50),
                "mins_to_expiry": cand["mins"],
                "entry_time": "",  # Filled by caller
                "side": cand["side"],
                "strategy": "scaled",
                "tier": tiers.index(tier_size) + 1,
                "tier_total": len(tiers),
            })
            available -= bet

        if entries:
            used_contracts.add(key)

    return entries


# ══════════════════════════════════════════════════════════════════════════════
# Neural Training Data Feeder
# ══════════════════════════════════════════════════════════════════════════════

def feed_bot_trades_to_neural(
    trade_file: str = "/mnt/c/Users/12035/father_daddy_capital/data/ce25_bot_trades.json",
    neural_engine=None,
) -> dict:
    """
    Feed the bot's closed trades into the neural plasticity layer.
    Each closed trade becomes a training example: signal → outcome.

    The bot's trades don't have RSI/MACD signal data, so we use proxy features:
    - price_at_entry (relative to strike)
    - entry_price (0-1 prediction market price)
    - outcome (1=win, 0=loss — all 1.0 for this bot!)
    - asset_class (BTC=0)

    This gives the neural layer 250 examples of "what winning looks like."
    """
    import json
    import numpy as np

    try:
        with open(trade_file) as f:
            trades = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"error": "trade file not found", "fed": 0}

    if neural_engine is None:
        return {"error": "no neural engine provided", "fed": 0}

    fed = 0
    for trade in trades:
        pnl = trade.get("pnl", 0)
        price = trade.get("price", 0)
        title = trade.get("title", "")

        if pnl <= 0 or price <= 0:
            continue  # Skip losses (none for this bot) and bad data

        # Build proxy signal vector (8 dims)
        # Since we don't have RSI/MACD, we use the trade data itself
        direction = 1.0 if "Up" in title else -1.0
        asset_enc = 0.0 if "Bitcoin" in title else (1.0 if "Ethereum" in title else 2.0)

        signal_vector = np.array([
            direction * 0.5,     # RSI signal proxy: direction
            direction * 0.3,     # MACD proxy
            direction * 0.4,     # Trend proxy
            direction * 0.2,     # Momentum proxy
            0.0,                 # Mean reversion (unknown)
            abs(price - 0.5),    # Volatility proxy: distance from 0.5
            asset_enc / 3.0,     # Asset class normalized
            0.8,                 # Confidence proxy (bot is confident)
        ], dtype=float)

        # P&L scaling: wins are +1, super-wins capped
        pnl_scaled = np.clip(pnl / 500.0, 0, 1.0)  # $500 = max signal

        # Feed to neural
        try:
            neural_engine.network.learn_from_trade(signal_vector, 0.5, pnl_scaled)
            neural_engine.network.add_to_replay(signal_vector, pnl_scaled)
            fed += 1
        except Exception:
            continue

    # Batch train from replay buffer
    if fed > 0 and hasattr(neural_engine.network, 'replay'):
        try:
            neural_engine.network.replay()
        except Exception:
            pass

    neural_engine.network.save()
    if hasattr(neural_engine, 'performance'):
        neural_engine.performance.save()

    return {"fed": fed, "total_trades": len(trades), "source": "0xce25_bot"}


# ══════════════════════════════════════════════════════════════════════════════
# Quick Test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== Scaled Entry Engine Test ===\n")

    # Mock signal
    sig = {"direction": "up", "confidence": 0.85, "rsi": 35, "price": 79100}

    # Mock contracts (simulating BTC daily above/below)
    contracts = [
        {"question": "Will Bitcoin be above $78,000?", "conditionId": "0xaaa", "up_price": 0.97, "down_price": 0.03, "mins_to_expiry": 15},
        {"question": "Will Bitcoin be above $80,000?", "conditionId": "0xbbb", "up_price": 0.16, "down_price": 0.84, "mins_to_expiry": 15},
        {"question": "Will Bitcoin be above $82,000?", "conditionId": "0xccc", "up_price": 0.01, "down_price": 0.99, "mins_to_expiry": 15},
    ]

    state = {"bankroll": 250, "positions": {}}
    entries = evaluate_scaled_entries(sig, contracts, state)

    print(f"Signal: {sig['direction']} @ {sig['confidence']}")
    print(f"Entries: {len(entries)}")
    for e in entries:
        print(f"  Tier {e['tier']}/{e['tier_total']}: {e['action']} ${e['bet']} @ {e['contract_price']:.3f} edge={e['edge']:.3f}")

    # Neural feed test
    import sys
    sys.path.insert(0, "/mnt/c/Users/12035/father_daddy_capital/src/neural")
    try:
        import plastic_network as pn
        neural = pn.NeuralPlasticityEngine()
        before = neural.network.updates
        result = feed_bot_trades_to_neural(neural_engine=neural)
        after = neural.network.updates
        print(f"\nNeural: {before} → {after} updates (+{after-before})")
        print(f"Fed: {result.get('fed', 0)} trades from bot data")
    except ImportError:
        print("\nNeural module not available in test env")
