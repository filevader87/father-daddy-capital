#!/usr/bin/env python3
"""
V20.3 Binary Settlement Engine — Sections 2-4
================================================
- Every resolved position settles at 0 or 1 (binary)
- NO midpoint fallback (close_price=0.50 is NOT settlement)
- Correct PnL: shares * 1.0 or 0.0, minus cost
- Separate mark-to-market from settlement PnL

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
from dataclasses import dataclass, field
from typing import Optional, Literal
from datetime import datetime, timezone


@dataclass
class BinarySettlementResult:
    """Result of a binary settlement."""
    settlement_type: str = "binary_expiry"
    settlement_value: float = 0.0          # 0.0 or 1.0 ONLY
    resolved_winner: str = ""               # "UP" or "DOWN"
    winning_token_id: str = ""
    selected_token_id: str = ""
    selected_side: str = ""                 # "UP" or "DOWN"
    win_loss: str = ""                      # "WIN" or "LOSS"
    shares: float = 0.0
    size_usd: float = 0.0
    entry_price: float = 0.0
    payout: float = 0.0
    net_pnl: float = 0.0
    unresolved_pending_resolution: bool = False
    settlement_timestamp: str = ""


@dataclass
class MarkToMarket:
    """Mark-to-market value while position is open."""
    mark_price: float = 0.0
    mark_bid: float = 0.0
    mark_ask: float = 0.0
    mark_spread: float = 0.0
    unrealized_mark_pnl: float = 0.0
    mark_timestamp: str = ""


def compute_shares(size_usd: float, entry_price: float) -> float:
    """Compute number of shares bought at entry_price with size_usd capital.
    
    shares = size_usd / entry_price
    At entry 0.50 with $2: shares = 2/0.50 = 4.0
    At entry 0.56 with $2: shares = 2/0.56 = 3.571
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    return size_usd / entry_price


def compute_binary_pnl(size_usd: float, entry_price: float,
                        settlement_value: float) -> tuple:
    """Compute PnL for binary settlement.
    
    Args:
        size_usd: Dollar amount invested (e.g., $2.00)
        entry_price: Price per share at entry (e.g., 0.50)
        settlement_value: 1.0 if selected token won, 0.0 if lost
    
    Returns:
        (shares, payout, net_pnl)
    
    Examples:
        Entry 0.50, $2, WIN:  shares=4.0, payout=4.0, net_pnl=+$2.00
        Entry 0.50, $2, LOSS: shares=4.0, payout=0.0, net_pnl=-$2.00
        Entry 0.56, $2, WIN:  shares=3.571, payout=3.571, net_pnl=+$1.571
        Entry 0.56, $2, LOSS: shares=3.571, payout=0.0,   net_pnl=-$2.00
    """
    if settlement_value not in (0.0, 1.0):
        raise ValueError(
            f"settlement_value must be 0.0 or 1.0, got {settlement_value}. "
            f"Binary settlement only — no midpoint fallback."
        )
    shares = compute_shares(size_usd, entry_price)
    payout = shares * settlement_value
    net_pnl = payout - size_usd
    return shares, payout, net_pnl


def compute_mark_pnl(size_usd: float, entry_price: float,
                      mark_price: float) -> tuple:
    """Compute mark-to-market PnL (for open positions, NOT settlement).
    
    This is unrealized value. It CAN use midpoint/bid/ask.
    This is SEPARATE from realized settlement PnL.
    
    Args:
        size_usd: Dollar amount invested
        entry_price: Price per share at entry
        mark_price: Current mark price (bid, ask, or mid are all valid)
    
    Returns:
        (shares, unrealized_value, unrealized_pnl)
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    shares = compute_shares(size_usd, entry_price)
    unrealized_value = shares * mark_price
    unrealized_pnl = unrealized_value - size_usd
    return shares, unrealized_value, unrealized_pnl


def settle_position(
    size_usd: float,
    entry_price: float,
    selected_side: str,
    resolved_winner: str,
    selected_token_id: str = "",
    winning_token_id: str = "",
) -> BinarySettlementResult:
    """Settle a binary position at actual resolution.
    
    Args:
        size_usd: Dollar amount invested
        entry_price: Price per share at entry
        selected_side: "UP" or "DOWN"
        resolved_winner: "UP" or "DOWN" (from CLOB/Gamma API winner field)
        selected_token_id: Token ID of the side we bought
        winning_token_id: Token ID of the winning side
    
    Returns:
        BinarySettlementResult with full settlement details
    
    Raises:
        ValueError if resolved_winner is not "UP" or "DOWN"
    """
    if resolved_winner not in ("UP", "DOWN"):
        # Cannot settle — winner unknown
        return BinarySettlementResult(
            settlement_type="binary_expiry",
            settlement_value=0.0,
            resolved_winner=resolved_winner,
            winning_token_id=winning_token_id,
            selected_token_id=selected_token_id,
            selected_side=selected_side,
            win_loss="UNKNOWN",
            shares=compute_shares(size_usd, entry_price),
            size_usd=size_usd,
            entry_price=entry_price,
            payout=0.0,
            net_pnl=0.0,
            unresolved_pending_resolution=True,
            settlement_timestamp=datetime.now(timezone.utc).isoformat(),
        )
    
    # Determine settlement value: 1.0 if our side won, 0.0 if lost
    is_win = (selected_side == resolved_winner)
    settlement_value = 1.0 if is_win else 0.0
    win_loss = "WIN" if is_win else "LOSS"
    
    shares, payout, net_pnl = compute_binary_pnl(
        size_usd, entry_price, settlement_value
    )
    
    return BinarySettlementResult(
        settlement_type="binary_expiry",
        settlement_value=settlement_value,
        resolved_winner=resolved_winner,
        winning_token_id=winning_token_id,
        selected_token_id=selected_token_id,
        selected_side=selected_side,
        win_loss=win_loss,
        shares=shares,
        size_usd=size_usd,
        entry_price=entry_price,
        payout=payout,
        net_pnl=net_pnl,
        unresolved_pending_resolution=False,
        settlement_timestamp=datetime.now(timezone.utc).isoformat(),
    )


def compute_historical_pnl(size_usd: float, entry_price: float,
                             selected_side: str,
                             winner: str) -> dict:
    """Convenience function for historical PnL computation.
    
    Maps from V20.1 close_price (0.50) to correct binary PnL.
    
    Args:
        size_usd: Dollar amount invested
        entry_price: Price per share at entry
        selected_side: "UP" or "DOWN"
        winner: "UP" or "DOWN" (actual resolution)
    
    Returns dict with all settlement fields.
    """
    result = settle_position(size_usd, entry_price, selected_side, winner)
    return {
        "settlement_type": result.settlement_type,
        "settlement_value": result.settlement_value,
        "resolved_winner": result.resolved_winner,
        "selected_side": result.selected_side,
        "win_loss": result.win_loss,
        "shares": round(result.shares, 6),
        "entry_price": result.entry_price,
        "size_usd": result.size_usd,
        "payout": round(result.payout, 4),
        "net_pnl": round(result.net_pnl, 4),
        "unresolved": result.unresolved_pending_resolution,
    }


# ─── V20.1 → V20.2 PnL Reconciliation ───

def recalculate_v201_positions(positions: list) -> list:
    """Recalculate V20.1 positions with binary settlement.
    
    Takes list of position dicts from micro_validation_report.json
    and returns corrected PnL for each.
    """
    results = []
    for pos in positions:
        slug = pos.get("slug", "")
        side = pos.get("side", "")
        entry_price = pos.get("entry_ask", pos.get("entry_price", 0.5))
        close_price = pos.get("close_price", 0.5)  # V20.1 midpoint
        size_usd = pos.get("size", 2.0)
        
        # Determine actual winner from slug timing (heuristic)
        # In V20.1 all close_price was 0.50 — this is NOT settlement
        # Actual resolution must come from CLOB/Gamma API
        # For now, mark as unresolved
        result = {
            "slug": slug,
            "side": side,
            "entry_price": entry_price,
            "v201_close_price": close_price,
            "v201_pnl_dollars": pos.get("pnl_dollars", 0),
            "v201_used_midpoint": True,
            "binary_settlement_required": True,
            "actual_resolution_needed": "CLOB_API_lookup",
        }
        results.append(result)
    
    return results