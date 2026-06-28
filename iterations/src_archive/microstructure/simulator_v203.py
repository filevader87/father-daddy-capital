#!/usr/bin/env python3
"""
V20.3 Binary Settlement Simulator — Section 9
=================================================
Completely rewritten simulator that matches actual Polymarket binary mechanics.

V20.1 simulator used close_price=0.50 for all positions, masking real losses.
This simulator uses ONLY binary settlement (0 or 1).

Simulates:
  - Entry at ask price (pay the spread)
  - Hold to expiry (default mode)
  - Binary PnL: shares * 1.0 if win, 0.0 if loss, minus cost
  - Slippage, latency, spread cost
  - Stale price aborts
  - Failed fills

NO synthetic take-profit unless modeled with actual executable bid before expiry.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone
from enum import Enum
import time
import random


# ── Configuration ──
DEFAULT_SIZE_USD = 2.00
DEFAULT_SLIPPAGE_BPS = 10       # 0.1% slippage
DEFAULT_LATENCY_MS = 500        # CLOB fill latency
DEFAULT_MAX_STALE_SECONDS = 5.0  # Abort if price >5s old
DEFAULT_MAX_SPREAD = 0.03        # Reject if spread >3¢


class TradeResult(str, Enum):
    FILLED = "filled"
    FAILED_FILL = "failed_fill"
    STALE_PRICE_ABORT = "stale_price_abort"
    SPREAD_TOO_WIDE = "spread_too_wide"
    LIVE_BLOCKED = "live_blocked"


class SettlementResult(str, Enum):
    BINARY_WIN = "binary_win"
    BINARY_LOSS = "binary_loss"
    UNRESOLVED = "unresolved"


@dataclass
class SimulatedTrade:
    """A single simulated binary trade."""
    # Identity
    trade_id: str = ""
    slug: str = ""
    timestamp: str = ""
    
    # Entry
    selected_side: str = ""            # "UP" or "DOWN"
    entry_price: float = 0.0           # ask price at entry
    size_usd: float = DEFAULT_SIZE_USD
    shares: float = 0.0               # = size_usd / entry_price
    
    # Fill
    fill_result: TradeResult = TradeResult.FILLED
    fill_price: float = 0.0           # actual fill (entry + slippage)
    fill_slippage_bps: float = 0.0
    fill_latency_ms: float = 0.0
    
    # Market state at entry
    bid_at_entry: float = 0.0
    ask_at_entry: float = 0.0
    spread_at_entry: float = 0.0
    imbalance_at_entry: float = 0.0
    bid_depth_at_entry: float = 0.0
    ask_depth_at_entry: float = 0.0
    
    # Settlement
    settlement_result: SettlementResult = SettlementResult.UNRESOLVED
    resolved_winner: str = ""          # "UP" or "DOWN"
    settlement_value: float = 0.0      # 0.0 or 1.0 ONLY
    
    # PnL
    payout: float = 0.0               # shares * settlement_value
    net_pnl: float = 0.0              # payout - size_usd
    realized_settlement_pnl: float = 0.0  # Same as net_pnl (binary)
    unrealized_mark_pnl: float = 0.0  # Only for open positions, NOT for settlement
    
    # Mark-to-market (separate from settlement)
    mark_price: float = 0.0
    mark_timestamp: str = ""
    
    # Diagnostics
    transition_score: float = 0.0
    raw_transition_score: float = 0.0
    regime: str = ""
    regime_entropy_bits: float = 0.0
    

@dataclass
class SimulationConfig:
    """Configuration for the V20.3 simulator."""
    size_usd: float = DEFAULT_SIZE_USD
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    latency_ms: float = DEFAULT_LATENCY_MS
    max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS
    max_spread: float = DEFAULT_MAX_SPREAD
    hold_to_expiry: bool = True           # Default: no early exit
    allow_early_exit: bool = False        # Must use actual executable bid
    live_blocked: bool = True            # V20.3: always True
    
    # Profile filtering
    profile_name: str = ""
    min_transition_score: float = 0.1
    regime_filter: List[str] = field(default_factory=list)


class BinarySimulatorV203:
    """V20.3 Binary Settlement Simulator.
    
    Simulates trades using ACTUAL Polymarket binary mechanics:
      1. Buy shares at ask price (+ slippage)
      2. Hold until expiry (default)
      3. Settle at 1.0 (win) or 0.0 (loss)
      4. PnL = shares * settlement_value - size_usd
    
    This replaces the V20.1 simulator which used close_price=0.50.
    """
    
    def __init__(self, config: Optional[SimulationConfig] = None):
        self.config = config or SimulationConfig()
        self._trades: List[SimulatedTrade] = []
        self._trade_counter: int = 0
    
    def simulate_entry(
        self,
        slug: str,
        selected_side: str,
        ask_price: float,
        bid_price: float,
        down_ask_price: float = 0.0,
        down_bid_price: float = 0.0,
        spread: float = 0.0,
        imbalance: float = 0.0,
        bid_depth: float = 0.0,
        ask_depth: float = 0.0,
        transition_score: float = 0.0,
        raw_transition_score: float = 0.0,
        regime: str = "",
        regime_entropy: float = 0.0,
        price_timestamp: Optional[float] = None,
    ) -> SimulatedTrade:
        """Simulate a trade entry with binary settlement mechanics.
        
        Args:
            slug: Market slug/condition_id
            selected_side: "UP" or "DOWN"
            ask_price: Ask price of selected token (what we pay)
            bid_price: Bid price of selected token
            down_ask_price/down_bid_price: Opposite token prices
            spread: Real bid-ask spread (selected_token_ask - selected_token_bid)
            imbalance: Real orderbook imbalance
            bid_depth/ask_depth: Depth at top 3 levels
            transition_score: V20.3 tanh-normalized transition
            raw_transition_score: Pre-normalization raw score
            regime: V20.3 regime classification
            regime_entropy: Current regime entropy in bits
            price_timestamp: When the price data was captured
        
        Returns:
            SimulatedTrade with fill details. settlement_value will be 0 until resolved.
        """
        self._trade_counter += 1
        trade_id = f"SIM-V203-{self._trade_counter:04d}"
        now = datetime.now(timezone.utc)
        
        trade = SimulatedTrade(
            trade_id=trade_id,
            slug=slug,
            timestamp=now.isoformat(),
            selected_side=selected_side,
            entry_price=ask_price,
            size_usd=self.config.size_usd,
            bid_at_entry=bid_price,
            ask_at_entry=ask_price,
            spread_at_entry=spread,
            imbalance_at_entry=imbalance,
            bid_depth_at_entry=bid_depth,
            ask_depth_at_entry=ask_depth,
            transition_score=transition_score,
            raw_transition_score=raw_transition_score,
            regime=regime,
            regime_entropy_bits=regime_entropy,
        )
        
        # ── Gate: Spread too wide ──
        if spread > self.config.max_spread and spread > 0:
            trade.fill_result = TradeResult.SPREAD_TOO_WIDE
            trade.fill_price = 0.0
            trade.shares = 0.0
            trade.net_pnl = 0.0
            self._trades.append(trade)
            return trade
        
        # ── Gate: Stale price ──
        if price_timestamp is not None:
            staleness = time.time() - price_timestamp
            if staleness > self.config.max_stale_seconds:
                trade.fill_result = TradeResult.STALE_PRICE_ABORT
                trade.fill_price = 0.0
                trade.shares = 0.0
                trade.net_pnl = 0.0
                self._trades.append(trade)
                return trade
        
        # ── Simulate fill ──
        # Fill at ask + slippage
        slippage = ask_price * (self.config.slippage_bps / 10000)
        fill_price = ask_price + slippage
        
        # Random fill failure (1-2% on thin books)
        fill_failure_prob = max(0.01, 0.05 * (1.0 - min(bid_depth / 100, 1.0)))
        if random.random() < fill_failure_prob:
            trade.fill_result = TradeResult.FAILED_FILL
            trade.fill_price = 0.0
            trade.shares = 0.0
            trade.net_pnl = 0.0
            self._trades.append(trade)
            return trade
        
        # Fill successful
        trade.fill_result = TradeResult.FILLED
        trade.fill_price = round(fill_price, 6)
        trade.fill_slippage_bps = self.config.slippage_bps
        trade.fill_latency_ms = self.config.latency_ms
        
        # Compute shares
        trade.shares = round(self.config.size_usd / fill_price, 6)
        
        # Settlement is unresolved until market resolves
        trade.settlement_result = SettlementResult.UNRESOLVED
        trade.settlement_value = 0.0  # Will be set when resolved
        trade.payout = 0.0
        trade.net_pnl = 0.0
        trade.realized_settlement_pnl = 0.0
        
        self._trades.append(trade)
        return trade
    
    def resolve_trade(
        self,
        trade_id: str,
        resolved_winner: str,
    ) -> Optional[SimulatedTrade]:
        """Resolve a simulated trade with binary settlement.
        
        Args:
            trade_id: The SIM-V203-NNNN trade ID
            resolved_winner: "UP" or "DOWN" — actual market outcome
        
        Returns:
            Updated SimulatedTrade with binary PnL, or None if trade not found.
        """
        trade = None
        for t in self._trades:
            if t.trade_id == trade_id:
                trade = t
                break
        
        if trade is None:
            return None
        
        if trade.fill_result != TradeResult.FILLED:
            return trade  # Can't resolve unfilled trades
        
        if resolved_winner not in ("UP", "DOWN"):
            # Cannot resolve — winner unknown
            trade.settlement_result = SettlementResult.UNRESOLVED
            return trade
        
        # Binary settlement
        is_win = (trade.selected_side == resolved_winner)
        settlement_value = 1.0 if is_win else 0.0
        
        trade.resolved_winner = resolved_winner
        trade.settlement_value = settlement_value
        trade.settlement_result = (
            SettlementResult.BINARY_WIN if is_win
            else SettlementResult.BINARY_LOSS
        )
        
        # PnL calculation
        trade.payout = round(trade.shares * settlement_value, 6)
        trade.net_pnl = round(trade.payout - trade.size_usd, 4)
        trade.realized_settlement_pnl = trade.net_pnl
        
        return trade
    
    def compute_mark_to_market(
        self,
        trade_id: str,
        mark_price: float,
    ) -> Optional[SimulatedTrade]:
        """Compute mark-to-market PnL for an open (unresolved) position.
        
        This is SEPARATE from settlement PnL. Mark-to-market uses current
        bid/mid/ask price. Settlement uses binary 0 or 1.
        
        Args:
            trade_id: The SIM-V203-NNNN trade ID
            mark_price: Current market price (bid, mid, or ask are all valid)
        
        Returns:
            Updated trade with unrealized_mark_pnl set.
        """
        trade = None
        for t in self._trades:
            if t.trade_id == trade_id:
                trade = t
                break
        
        if trade is None:
            return None
        
        # Mark-to-market: can use any price estimate
        trade.mark_price = mark_price
        trade.mark_timestamp = datetime.now(timezone.utc).isoformat()
        
        # unrealized_mark_pnl = (mark_price - entry_price) * shares
        # This is SEPARATE from realized_settlement_pnl
        unrealized_value = trade.shares * mark_price
        trade.unrealized_mark_pnl = round(unrealized_value - trade.size_usd, 4)
        
        return trade
    
    def get_results(self) -> Dict:
        """Get simulation results summary."""
        filled = [t for t in self._trades if t.fill_result == TradeResult.FILLED]
        resolved = [t for t in filled if t.settlement_result != SettlementResult.UNRESOLVED]
        wins = [t for t in resolved if t.settlement_result == SettlementResult.BINARY_WIN]
        losses = [t for t in resolved if t.settlement_result == SettlementResult.BINARY_LOSS]
        
        total_pnl = sum(t.net_pnl for t in resolved)
        
        # PnL by side
        up_trades = [t for t in resolved if t.selected_side == "UP"]
        down_trades = [t for t in resolved if t.selected_side == "DOWN"]
        up_pnl = sum(t.net_pnl for t in up_trades)
        down_pnl = sum(t.net_pnl for t in down_trades)
        up_wr = len([t for t in up_trades if t.settlement_result == SettlementResult.BINARY_WIN]) / len(up_trades) if up_trades else 0
        down_wr = len([t for t in down_trades if t.settlement_result == SettlementResult.BINARY_WIN]) / len(down_trades) if down_trades else 0
        
        return {
            "version": "V20.3",
            "settlement_type": "binary_only",
            "total_trades": len(self._trades),
            "filled_trades": len(filled),
            "resolved_trades": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(resolved) if resolved else 0,
            "total_pnl": round(total_pnl, 4),
            "up_trades": len(up_trades),
            "up_pnl": round(up_pnl, 4),
            "up_win_rate": round(up_wr, 4),
            "down_trades": len(down_trades),
            "down_pnl": round(down_pnl, 4),
            "down_win_rate": round(down_wr, 4),
            "spread_abort": len([t for t in self._trades if t.fill_result == TradeResult.SPREAD_TOO_WIDE]),
            "stale_abort": len([t for t in self._trades if t.fill_result == TradeResult.STALE_PRICE_ABORT]),
            "fill_failures": len([t for t in self._trades if t.fill_result == TradeResult.FAILED_FILL]),
            "unresolved": len([t for t in filled if t.settlement_result == SettlementResult.UNRESOLVED]),
        }