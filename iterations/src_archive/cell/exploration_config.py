#!/usr/bin/env python3
"""
V20.3 Multi-Asset Exploration Config — §2
============================================
Paper exploration for BTC/ETH/SOL/XRP × 5m/15m × UP/DOWN × 8 price buckets.
No overfiltering in discovery mode. Let bad cells reveal themselves quickly.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
from enum import Enum


# ── Asset Configuration ──

class Asset(str, Enum):
    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    XRP = "XRP"


class Interval(str, Enum):
    FIVE_MIN = "5m"
    FIFTEEN_MIN = "15m"


class Side(str, Enum):
    UP = "UP"
    DOWN = "DOWN"


# Price buckets for entry
ENTRY_BUCKETS = [
    (0.05, 0.10),
    (0.10, 0.20),
    (0.20, 0.30),
    (0.30, 0.40),
    (0.40, 0.50),
    (0.50, 0.60),
    (0.60, 0.70),
    (0.70, 0.80),
    (0.80, 0.95),   # 0.80+ bucket
]

BUCKET_LABELS = [f"{lo:.2f}-{hi:.2f}" for lo, hi in ENTRY_BUCKETS[:8]] + ["0.80+"]

# Regimes from regime_v203
REGIMES = [
    "trend_continuation",
    "trend_exhaustion",
    "panic_sell",
    "balanced_rotation",
    "liquidity_vacuum",
    "fake_reversal",
    "volatility_expansion",
    "volatility_compression",
]

# Transition deciles from transition_v203
TRANSITION_DECILES = [
    "very_negative",   # < -0.6
    "negative",         # -0.6 to -0.3
    "slight_negative",  # -0.3 to -0.1
    "neutral_low",      # -0.1 to 0.1
    "neutral_high",     # 0.1 to 0.3
    "positive",         # 0.3 to 0.6
    "very_positive",    # > 0.6
]

# Time to expiry buckets
TTE_BUCKETS = ["<3m", "3-5m", "5-10m", "10-15m", "15m+"]


# ── Directional Asymmetry Tags (§7) ──

DIRECTIONAL_CELLS = [
    "DOWN_CONTINUATION",   # RSI < 35 + DOWN side
    "UP_REVERSAL",         # RSI < 35 + UP side (contrarian)
    "DOWN_REVERSAL",       # RSI > 65 + DOWN side
    "UP_CONTINUATION",     # RSI > 65 + UP side
]


# ── Paper Trade Safety Gates ──

@dataclass
class PaperSafetyGates:
    """Minimum safety gates for paper exploration.
    
    Do not overfilter in discovery mode.
    Let bad cells reveal themselves quickly.
    """
    # Must have valid short-window market
    must_have_valid_market: bool = True
    
    # Must have real bid/ask
    must_have_real_book: bool = True
    min_bid_depth: float = 10.0   # Minimum depth at top 3 bid levels
    min_ask_depth: float = 10.0   # Minimum depth at top 3 ask levels
    
    # Must have real spread computed (not fake 0.98)
    must_have_real_spread: bool = True
    max_spread: float = 0.10     # Wider in discovery mode
    
    # Must have binary settlement available
    must_have_binary_settlement: bool = True
    
    # No duplicate position (same cell, same slug)
    no_duplicate_position: bool = True
    
    # No midpoint settlement (must be 0 or 1)
    no_midpoint_settlement: bool = True
    
    # Discovery mode: be permissive
    discovery_mode: bool = True
    
    # Minimum time between trades in same cell (seconds)
    min_trade_interval: float = 60.0
    
    # Maximum concurrent positions
    max_concurrent_positions: int = 3


# ── RSI Condition Matrix (§7) ──

RSI_CONDITIONS = [
    {"rsi_range": (0, 35), "label": "RSI_oversold", "tags": ["DOWN_CONTINUATION", "UP_REVERSAL"]},
    {"rsi_range": (35, 50), "label": "RSI_neutral_low", "tags": ["NEUTRAL"]},
    {"rsi_range": (50, 65), "label": "RSI_neutral_high", "tags": ["NEUTRAL"]},
    {"rsi_range": (65, 100), "label": "RSI_overbought", "tags": ["UP_CONTINUATION", "DOWN_REVERSAL"]},
]


# ── Total cell space ──

def compute_total_cells() -> int:
    """Compute the total number of possible cells."""
    n_assets = len(Asset)
    n_intervals = len(Interval)
    n_sides = len(Side)
    n_buckets = len(ENTRY_BUCKETS)
    n_regimes = len(REGIMES)
    n_deciles = len(TRANSITION_DECILES)
    n_tte = len(TTE_BUCKETS)
    
    return n_assets * n_intervals * n_sides * n_buckets * n_regimes * n_deciles * n_tte


def generate_all_cell_keys():
    """Generate all possible cell key components."""
    from cell.cell_framework import CellKey
    
    keys = []
    for asset in Asset:
        for interval in Interval:
            for side in Side:
                for bucket in BUCKET_LABELS:
                    for regime in REGIMES:
                        for decile in TRANSITION_DECILES:
                            for tte in TTE_BUCKETS:
                                keys.append(CellKey(
                                    asset=asset.value,
                                    interval=interval.value,
                                    side=side.value,
                                    entry_bucket=bucket,
                                    regime=regime,
                                    transition_decile=decile,
                                    time_to_expiry=tte,
                                ))
    return keys


# ── Exploration Configuration ──

@dataclass
class ExplorationConfig:
    """Full configuration for multi-asset aggressive paper exploration."""
    assets: List[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL", "XRP"])
    intervals: List[str] = field(default_factory=lambda: ["5m", "15m"])
    sides: List[str] = field(default_factory=lambda: ["UP", "DOWN"])
    buckets: List[str] = field(default_factory=lambda: BUCKET_LABELS)
    
    # Polymarket condition IDs to monitor
    # Populated at runtime from CLOB market discovery
    market_conditions: List[str] = field(default_factory=list)
    
    # Paper trading parameters
    size_usd: float = 2.0
    safety: PaperSafetyGates = field(default_factory=PaperSafetyGates)
    
    # Allocation percentages (§6)
    exploration_pct: float = 0.10      # 10% to new exploration
    promising_pct: float = 0.20        # 20% to under-sampled promising
    exploitation_pct: float = 0.70     # 70% to top cells
    
    # Rerank interval (seconds)
    rerank_interval: float = 1800.0    # 30 minutes
    
    # Live remains BLOCKED
    live_enabled: bool = False
    micro_live_enabled: bool = False
    
    @property
    def total_possible_cells(self) -> int:
        return (len(self.assets) * len(self.intervals) * len(self.sides) *
                len(self.buckets) * len(REGIMES) * len(TRANSITION_DECILES) * len(TTE_BUCKETS))