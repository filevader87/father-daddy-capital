#!/usr/bin/env python3
"""
FDC Orderbook Layer
Extracted from poly-maker (warproxxx) orderbook analysis engine.
Provides depth-aware fills, smart pricing, and position-aware sizing
for Polymarket CLOB orderbooks.

Replace naive "use mid-price" pattern with real fill simulation.

Author: Hugh (3rd of 5)
Date: 2026-05-15
"""

from __future__ import annotations
import math
from typing import Optional, Tuple, Dict, List


# ─── Depth Analysis ────────────────────────────────────────────────────────


def find_best_price_with_depth(
    price_dict: Dict[float, float],
    min_depth: float,
    reverse: bool = False,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    Walk the orderbook to find price levels with sufficient depth.

    Args:
        price_dict: {price: size} mapping. Assumed sorted ascending (bids)
                    or descending (asks) — caller reverses before passing.
        min_depth: minimum cumulative size required at a price level
        reverse: True for bids (best = highest), False for asks (best = lowest)

    Returns:
        (best_price, best_size, second_best_price, second_best_size, top_price)
    """
    items = list(price_dict.items())
    if reverse:
        items.reverse()

    best_price: Optional[float] = None
    best_size: Optional[float] = None
    second_best_price: Optional[float] = None
    second_best_size: Optional[float] = None
    top_price: Optional[float] = None
    found_best = False

    for price, size in items:
        if top_price is None:
            top_price = price

        if found_best:
            second_best_price = price
            second_best_size = size
            break

        if size > min_depth:
            if best_price is None:
                best_price = price
                best_size = size
                found_best = True

    return best_price, best_size, second_best_price, second_best_size, top_price


def depth_sum_near_mid(
    book: Dict[float, float],
    mid_price: float,
    deviation: float = 0.05,
    side: str = "bids",
) -> float:
    """
    Sum depth within deviation% of mid-price.

    For bids: sum prices ≥ best_bid, ≤ mid_price * (1 + deviation)
    For asks: sum prices ≥ mid_price * (1 - deviation), ≤ best_ask
    """
    total = 0.0
    if side == "bids":
        for price, size in book.items():
            if price <= mid_price * (1 + deviation):
                total += size
    else:
        for price, size in book.items():
            if price >= mid_price * (1 - deviation):
                total += size
    return total


# ─── Book Analysis ─────────────────────────────────────────────────────────


def analyze_orderbook(
    market: str,
    bids: Dict[float, float],
    asks: Dict[float, float],
    target_size: float,
    is_no_token: bool = False,
    deviation_threshold: float = 0.05,
) -> dict:
    """
    Full orderbook analysis: best prices, depth, mid-price, and NO-token reversal.

    For NO tokens, bids become asks and vice versa (price = 1 - YES price),
    and the depth sums swap sides.

    Args:
        market: market/condition ID (for logging)
        bids: {price: size} — best bid = highest price
        asks: {price: size} — best ask = lowest price
        target_size: desired trade size for depth filtering
        is_no_token: True if this is the NO (complement) token
        deviation_threshold: % deviation from mid for depth sums

    Returns:
        {
            best_bid, best_bid_size, second_best_bid, second_best_bid_size,
            top_bid, best_ask, best_ask_size, second_best_ask,
            second_best_ask_size, top_ask, mid_price, bid_depth_near_mid,
            ask_depth_near_mid
        }
    """
    (best_bid, best_bid_size, sec_bid, sec_bid_size, top_bid) = (
        find_best_price_with_depth(bids, target_size, reverse=True)
    )
    (best_ask, best_ask_size, sec_ask, sec_ask_size, top_ask) = (
        find_best_price_with_depth(asks, target_size, reverse=False)
    )

    # Compute mid-price
    if best_bid is not None and best_ask is not None:
        mid_price = (best_bid + best_ask) / 2.0
        bid_depth_near = depth_sum_near_mid(bids, mid_price, deviation_threshold, "bids")
        ask_depth_near = depth_sum_near_mid(asks, mid_price, deviation_threshold, "asks")
    else:
        mid_price = None
        bid_depth_near = 0.0
        ask_depth_near = 0.0

    # NO-token reversal: price = 1 - YES price
    if is_no_token:
        # Check that all required prices exist before reversing
        if all(x is not None for x in [best_bid, best_ask, sec_bid, sec_ask, top_bid, top_ask]):
            best_bid, sec_bid, top_bid, best_ask, sec_ask, top_ask = (
                1.0 - best_ask, 1.0 - sec_ask, 1.0 - top_ask,
                1.0 - best_bid, 1.0 - sec_bid, 1.0 - top_bid,
            )
            (
                best_bid_size, sec_bid_size,
                best_ask_size, sec_ask_size,
            ) = (
                best_ask_size, sec_ask_size,
                best_bid_size, sec_bid_size,
            )
            bid_depth_near, ask_depth_near = ask_depth_near, bid_depth_near
        elif best_bid is not None and best_ask is not None:
            best_bid, best_ask = 1.0 - best_ask, 1.0 - best_bid
            best_bid_size, best_ask_size = best_ask_size, best_bid_size
            bid_depth_near, ask_depth_near = ask_depth_near, bid_depth_near
            if sec_bid is not None:
                sec_bid = 1.0 - sec_bid
            if sec_ask is not None:
                sec_ask = 1.0 - sec_ask
            if top_bid is not None:
                top_bid = 1.0 - top_bid
            if top_ask is not None:
                top_ask = 1.0 - top_ask

    return {
        "best_bid": best_bid,
        "best_bid_size": best_bid_size,
        "second_best_bid": sec_bid,
        "second_best_bid_size": sec_bid_size,
        "top_bid": top_bid,
        "best_ask": best_ask,
        "best_ask_size": best_ask_size,
        "second_best_ask": sec_ask,
        "second_best_ask_size": sec_ask_size,
        "top_ask": top_ask,
        "mid_price": mid_price,
        "bid_depth_near_mid": bid_depth_near,
        "ask_depth_near_mid": ask_depth_near,
    }


# ─── Smart Pricing ─────────────────────────────────────────────────────────


def get_smart_order_prices(
    book: dict,
    avg_price: float,
    tick_size: float,
    min_size: float,
) -> Tuple[float, float]:
    """
    Compute intelligent bid/ask prices that step the book by one tick
    rather than crossing the spread wastefully.

    Rules:
    - Bid = best_bid + tick (step ahead of current best)
    - Ask = best_ask - tick
    - If best_bid depth < min_size * 1.5, sit ON the best bid (aggressive)
    - If best_ask depth < 250, sit ON the best ask
    - Avoid crossing: if bid ≥ top_ask, fall back to top_bid
    - If bid == ask after adjustment, use top_bid / top_ask

    Args:
        book: output from analyze_orderbook()
        avg_price: our average entry price for this position
        tick_size: market tick size
        min_size: market min order size

    Returns:
        (bid_price, ask_price)
    """
    best_bid = book["best_bid"]
    best_ask = book["best_ask"]
    top_bid = book["top_bid"]
    top_ask = book["top_ask"]
    best_bid_size = book["best_bid_size"] or 0
    best_ask_size = book["best_ask_size"] or 0

    # Start one tick from best
    bid_price = (best_bid or 0) + tick_size
    ask_price = (best_ask or 0) - tick_size

    # Aggressive: sit on the book when depth is thin
    if best_bid_size < min_size * 1.5:
        bid_price = best_bid or 0

    if best_ask_size < min_size * 1.5:
        ask_price = best_ask or 0

    # Prevent crossing the spread
    if bid_price >= (best_ask or 0):
        bid_price = top_bid or bid_price

    if ask_price <= (best_bid or 0):
        ask_price = top_ask or ask_price

    # If still equal, fall back to tops
    if abs(bid_price - ask_price) < tick_size / 2:
        bid_price = top_bid or bid_price
        ask_price = top_ask or ask_price

    # Don't sell below average entry
    if avg_price > 0 and ask_price <= avg_price:
        ask_price = avg_price

    return round(bid_price, 4), round(ask_price, 4)


# ─── Position-Aware Sizing ────────────────────────────────────────────────


def compute_trade_sizes(
    position: float,
    bid_price: float,
    trade_size: float,
    max_size: Optional[float] = None,
    min_size: float = 5.0,
    other_side_position: float = 0.0,
    low_price_multiplier: Optional[int] = None,
) -> Tuple[float, float]:
    """
    Position-aware buy/sell amounts.

    Strategy:
    - If below max_size: quote buy = trade_size, only sell if we have trade_size+
    - If at max_size: offer sell = trade_size (progressive exit), keep quoting buy
      if total exposure allows
    - Low-price assets (<$0.10): apply multiplier for gas efficiency
    - Enforce minimum order size

    Args:
        position: current position on this token
        bid_price: current bid price (for low-price multiplier check)
        trade_size: standard trade amount
        max_size: maximum position allowed (defaults to trade_size)
        min_size: minimum order size
        other_side_position: position on the other outcome token
        low_price_multiplier: multiply buys for sub-$0.10 tokens

    Returns:
        (buy_amount, sell_amount)
    """
    if max_size is None:
        max_size = trade_size

    total_exposure = position + other_side_position

    if position < max_size:
        # Building phase: quote buy, only sell if substantial
        remaining = max_size - position
        buy_amount = min(trade_size, remaining)
        sell_amount = trade_size if position >= trade_size else 0
    else:
        # At capacity: progressive exit + opportunistic rebuy
        sell_amount = min(position, trade_size)
        buy_amount = trade_size if total_exposure < max_size * 2 else 0

    # Enforce min order size
    if 0 < buy_amount < min_size:
        # If we're close to min_size, bump up
        if buy_amount > min_size * 0.7:
            buy_amount = max(min_size, buy_amount)
        else:
            buy_amount = 0

    # Low-price multiplier (gas efficiency)
    if bid_price < 0.10 and buy_amount > 0 and low_price_multiplier:
        buy_amount *= low_price_multiplier

    return buy_amount, sell_amount


# ─── Fill Simulation ───────────────────────────────────────────────────────


def simulate_fill(
    price_book: Dict[float, float],
    target_size: float,
    side: str = "BUY",
) -> Tuple[float, float, float]:
    """
    Walk the orderbook and compute the average fill price for target_size.

    Returns:
        (avg_fill_price, filled_size, slippage_pct)
    """
    items = sorted(price_book.items(), reverse=(side == "SELL"))
    filled = 0.0
    cost = 0.0
    best_price = items[0][0] if items else 0.0

    for price, size in items:
        remaining = target_size - filled
        if remaining <= 0:
            break
        take = min(size, remaining)
        filled += take
        cost += take * price

    if filled == 0:
        return 0.0, 0.0, 0.0

    avg_price = cost / filled
    slippage = abs(avg_price - best_price) / max(best_price, 0.001) * 100
    return round(avg_price, 4), round(filled, 1), round(slippage, 3)


# ─── Convenience Wrapper ───────────────────────────────────────────────────


def get_trade_params(
    bids: Dict[float, float],
    asks: Dict[float, float],
    position: float,
    avg_price: float,
    tick_size: float,
    min_size: float,
    trade_size: float,
    max_size: Optional[float] = None,
    is_no_token: bool = False,
    other_side_position: float = 0.0,
) -> dict:
    """
    One-call trade parameter computation for a single token.

    Returns: {
        book: full book analysis,
        bid_price, ask_price: smart order prices,
        buy_amount, sell_amount: position-aware sizes,
        mid_price, spread, bid_depth, ask_depth
    }
    """
    book = analyze_orderbook("", bids, asks, trade_size, is_no_token=is_no_token)
    bid_price, ask_price = get_smart_order_prices(book, avg_price, tick_size, min_size)
    buy, sell = compute_trade_sizes(
        position, bid_price, trade_size, max_size=max_size,
        min_size=min_size, other_side_position=other_side_position,
    )

    return {
        "book": book,
        "bid_price": bid_price,
        "ask_price": ask_price,
        "buy_amount": buy,
        "sell_amount": sell,
        "mid_price": book["mid_price"],
        "spread": (
            round(book["best_ask"] - book["best_bid"], 4)
            if book["best_ask"] and book["best_bid"]
            else None
        ),
        "bid_depth_near_mid": book["bid_depth_near_mid"],
        "ask_depth_near_mid": book["ask_depth_near_mid"],
    }


# ─── Quick Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Mock orderbook: BTC above $80K YES token
    mock_bids = {0.48: 100, 0.47: 200, 0.46: 50, 0.45: 300}
    mock_asks = {0.52: 150, 0.53: 100, 0.54: 80, 0.55: 200}

    params = get_trade_params(
        bids=mock_bids,
        asks=mock_asks,
        position=50,
        avg_price=0.50,
        tick_size=0.01,
        min_size=5,
        trade_size=20,
        max_size=100,
    )

    print("=== Orderbook Analysis ===")
    for k, v in params.items():
        print(f"  {k}: {v}")

    print("\n=== Fill Simulation ===")
    avg, filled, slip = simulate_fill(mock_asks, 50, "BUY")
    print(f"  BUY 50: avg={avg}, filled={filled}, slippage={slip}%")
    avg, filled, slip = simulate_fill(mock_bids, 50, "SELL")
    print(f"  SELL 50: avg={avg}, filled={filled}, slippage={slip}%")
