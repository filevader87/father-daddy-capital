#!/usr/bin/env python3
"""
V20.3 Real Spread Computation — Section 5
============================================
Replaces broken spread = UP_price + DOWN_price with real bid-ask spread.

The V20.2 audit found that 99.8% of observations had spread=0.98, which was
actually UP_token_price + DOWN_token_price (always ~0.98-1.00 for 5m markets).
That's NOT the bid-ask spread.

Real bid-ask spread:
  selected_spread = selected_token_ask - selected_token_bid

MAX_SPREAD = 0.03 (reject candidate if exceeded)

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
from dataclasses import dataclass
from typing import Optional, Tuple


MAX_SPREAD = 0.03  # Reject candidates with wider spread


@dataclass
class SpreadResult:
    """Result of real spread computation."""
    up_bid: Optional[float] = None
    up_ask: Optional[float] = None
    down_bid: Optional[float] = None
    down_ask: Optional[float] = None
    up_spread: Optional[float] = None
    down_spread: Optional[float] = None
    selected_spread: Optional[float] = None
    opposite_spread: Optional[float] = None
    spread_valid: bool = False
    blocked_by_missing_book_depth: bool = False
    blocked_by_max_spread: bool = False


def compute_real_spread(
    book_data: dict,
    selected_side: str = "UP",
) -> SpreadResult:
    """Compute real bid-ask spread from CLOB orderbook data.
    
    Args:
        book_data: Dict with 'bids' and 'asks' lists, or 'up_book' and 'down_book'.
                   Each list contains dicts with 'price' and 'size' keys.
        selected_side: "UP" or "DOWN" — which token we're buying.
    
    Returns:
        SpreadResult with all spread fields.
    """
    result = SpreadResult()
    
    # Try to extract UP and DOWN books
    up_book = book_data.get("up_book") or book_data.get("up_orderbook")
    down_book = book_data.get("down_book") or book_data.get("down_orderbook")
    
    # If we have separate books, use them
    if up_book and down_book:
        up_bids = up_book.get("bids", [])
        up_asks = up_book.get("asks", [])
        down_bids = down_book.get("bids", [])
        down_asks = down_book.get("asks", [])
    else:
        # Single book format — this is the selected token's book
        bids = book_data.get("bids", [])
        asks = book_data.get("asks", [])
        
        # If we only have one book, derive the opposite from token prices
        # UP book → DOWN book has inverse prices (1 - price)
        up_token_price = book_data.get("up_token_price")
        down_token_price = book_data.get("down_token_price")
        
        if bids and asks:
            # We have the selected token's book
            selected_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
            selected_asks = sorted(asks, key=lambda x: float(x.get("price", 1)))
            
            if selected_side == "UP":
                result.up_bid = float(selected_bids[0]["price"]) if selected_bids else None
                result.up_ask = float(selected_asks[0]["price"]) if selected_asks else None
                
                # Derive DOWN from UP: DOWN_bid = 1 - UP_ask, DOWN_ask = 1 - UP_bid
                if result.up_ask is not None:
                    result.down_bid = round(1.0 - result.up_ask, 2)
                if result.up_bid is not None:
                    result.down_ask = round(1.0 - result.up_bid, 2)
            else:
                result.down_bid = float(selected_bids[0]["price"]) if selected_bids else None
                result.down_ask = float(selected_asks[0]["price"]) if selected_asks else None
                
                # Derive UP from DOWN
                if result.down_ask is not None:
                    result.up_bid = round(1.0 - result.down_ask, 2)
                if result.down_bid is not None:
                    result.up_ask = round(1.0 - result.down_bid, 2)
        else:
            # No book data at all
            result.blocked_by_missing_book_depth = True
            return result
    
    # Compute spreads if we have bid/ask data
    if up_book and down_book:
        # Process UP book
        if up_book.get("bids") and up_book.get("asks"):
            up_bids = sorted(up_book["bids"], key=lambda x: float(x.get("price", 0)), reverse=True)
            up_asks = sorted(up_book["asks"], key=lambda x: float(x.get("price", 1)))
            result.up_bid = float(up_bids[0]["price"])
            result.up_ask = float(up_asks[0]["price"])
        
        # Process DOWN book
        if down_book.get("bids") and down_book.get("asks"):
            down_bids = sorted(down_book["bids"], key=lambda x: float(x.get("price", 0)), reverse=True)
            down_asks = sorted(down_book["asks"], key=lambda x: float(x.get("price", 1)))
            result.down_bid = float(down_bids[0]["price"])
            result.down_ask = float(down_asks[0]["price"])
    
    # Calculate spreads
    if result.up_bid is not None and result.up_ask is not None:
        result.up_spread = round(result.up_ask - result.up_bid, 4)
    if result.down_bid is not None and result.down_ask is not None:
        result.down_spread = round(result.down_ask - result.down_bid, 4)
    
    # Selected and opposite spread
    if selected_side == "UP":
        result.selected_spread = result.up_spread
        result.opposite_spread = result.down_spread
    else:
        result.selected_spread = result.down_spread
        result.opposite_spread = result.up_spread
    
    # Validate
    if result.selected_spread is not None:
        result.spread_valid = True
        if result.selected_spread > MAX_SPREAD:
            result.blocked_by_max_spread = True
    else:
        result.blocked_by_missing_book_depth = True
    
    return result


def compute_legacy_spread_warning(up_price: float, down_price: float) -> dict:
    """Generate a warning dict when legacy (broken) spread is detected.
    
    Legacy spread = up_price + down_price, which is NOT the bid-ask spread.
    """
    legacy_spread = up_price + down_price
    return {
        "legacy_spread": round(legacy_spread, 4),
        "legacy_method": "up_price + down_price (BROKEN)",
        "warning": f"Legacy spread {legacy_spread:.4f} is NOT the bid-ask spread. "
                   f"Use compute_real_spread() instead.",
        "is_broken": abs(legacy_spread - 1.0) < 0.05,  # ~1.0 = broken
    }