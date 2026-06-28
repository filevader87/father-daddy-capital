#!/usr/bin/env python3
"""
V20.3 Real Imbalance Computation — Section 6
================================================
Replaces broken imbalance=0.0 with real orderbook imbalance.

The V20.2 audit found that 100% of observations had imbalance=0.0 because
bid_depth was always equal to ask_depth. This was from the broken
spread computation (UP+DOWN sum), not from real orderbook depth.

Real imbalance:
  imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
  where bid_depth/ask_depth = sum of sizes at top N=3 levels

If book data is missing: return None, set blocked_by_missing_book_depth=True
Never default to 0.0.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
from dataclasses import dataclass, field
from typing import Optional, List


IMBALANCE_TOP_N = 3  # Use top 3 bid/ask levels for depth


@dataclass
class ImbalanceResult:
    """Result of real imbalance computation."""
    bid_depth: Optional[float] = None
    ask_depth: Optional[float] = None
    imbalance: Optional[float] = None
    bid_levels_used: int = 0
    ask_levels_used: int = 0
    imbalance_valid: bool = False
    blocked_by_missing_book_depth: bool = False
    
    # Per-level detail
    bid_prices: List[float] = field(default_factory=list)
    bid_sizes: List[float] = field(default_factory=list)
    ask_prices: List[float] = field(default_factory=list)
    ask_sizes: List[float] = field(default_factory=list)
    
    def __post_init__(self):
        if self.bid_prices is None:
            self.bid_prices = []
        if self.bid_sizes is None:
            self.bid_sizes = []
        if self.ask_prices is None:
            self.ask_prices = []
        if self.ask_sizes is None:
            self.ask_sizes = []


def compute_real_imbalance(
    book_data: dict,
    top_n: int = IMBALANCE_TOP_N,
) -> ImbalanceResult:
    """Compute real orderbook imbalance from CLOB data.
    
    Args:
        book_data: Dict with 'bids' and 'asks' lists.
                  Each list contains dicts with 'price' and 'size' keys.
        top_n: Number of top levels to include in depth.
    
    Returns:
        ImbalanceResult with all imbalance fields.
        If book data is missing, imbalance will be None and
        blocked_by_missing_book_depth will be True.
    """
    result = ImbalanceResult()
    
    bids = book_data.get("bids", [])
    asks = book_data.get("asks", [])
    
    if not bids or not asks:
        # No book data — cannot compute imbalance
        result.blocked_by_missing_book_depth = True
        result.imbalance = None
        return result
    
    # Sort bids descending by price, asks ascending by price
    try:
        sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
        sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 999)), reverse=False)
    except (ValueError, TypeError):
        result.blocked_by_missing_book_depth = True
        result.imbalance = None
        return result
    
    # Take top N levels
    top_bids = sorted_bids[:top_n]
    top_asks = sorted_asks[:top_n]
    
    if not top_bids or not top_asks:
        result.blocked_by_missing_book_depth = True
        result.imbalance = None
        return result
    
    # Sum sizes at top N levels
    bid_depth = 0.0
    ask_depth = 0.0
    
    result.bid_prices = []
    result.bid_sizes = []
    result.ask_prices = []
    result.ask_sizes = []
    
    for b in top_bids:
        try:
            price = float(b.get("price", 0))
            size = float(b.get("size", 0))
            bid_depth += size
            result.bid_prices.append(price)
            result.bid_sizes.append(size)
        except (ValueError, TypeError):
            continue
    
    for a in top_asks:
        try:
            price = float(a.get("price", 0))
            size = float(a.get("size", 0))
            ask_depth += size
            result.ask_prices.append(price)
            result.ask_sizes.append(size)
        except (ValueError, TypeError):
            continue
    
    result.bid_depth = bid_depth
    result.ask_depth = ask_depth
    result.bid_levels_used = len(result.bid_prices)
    result.ask_levels_used = len(result.ask_sizes)
    
    # Compute imbalance
    total_depth = bid_depth + ask_depth
    
    if total_depth == 0:
        # Both sides empty — no information
        result.blocked_by_missing_book_depth = True
        result.imbalance = None
        return result
    
    result.imbalance = round((bid_depth - ask_depth) / total_depth, 4)
    result.imbalance_valid = True
    
    return result


def compute_legacy_imbalance_warning(bid_depth_equal: float, ask_depth_equal: float) -> dict:
    """Generate warning when legacy (broken) imbalance is detected."""
    return {
        "legacy_bid_depth": bid_depth_equal,
        "legacy_ask_depth": ask_depth_equal,
        "legacy_imbalance": 0.0,
        "warning": f"Legacy imbalance=0.0 with bid_depth={bid_depth_equal} == ask_depth={ask_depth_equal}. "
                    f"This is BROKEN — real imbalance requires separate bid/ask depth.",
        "is_broken": abs(bid_depth_equal - ask_depth_equal) < 0.01,
    }