#!/usr/bin/env python3
"""
V20.3 Cell Framework — §1 Cell Thinking
=========================================
Every trade setup is a cell:
  asset × interval × side × entry_bucket × regime × transition_decile × time_to_expiry

Cells are the ONLY unit of evaluation. No global strategy promotion.
Cells that fail die. Cells that win get capital.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from enum import Enum
from collections import defaultdict
import time
import math


# ── Enums ──

class CellStatus(str, Enum):
    EXPLORING = "EXPLORING"        # Under-sampled, gathering data
    ACTIVE = "ACTIVE"              # Enough data, still evaluating
    PROMOTED = "PROMOTED"          # High-priority paper exploitation
    LIVE_CANDIDATE = "LIVE_CANDIDATE"  # Meets live promotion criteria
    DISABLED = "DISABLED"          # Killed — dead, not shadow, not diagnostic
    KILLED = "KILLED"              # Alias for clarity


class Bucket(str, Enum):
    B005_010 = "0.05-0.10"
    B010_020 = "0.10-0.20"
    B020_030 = "0.20-0.30"
    B030_040 = "0.30-0.40"
    B040_050 = "0.40-0.50"
    B050_060 = "0.50-0.60"
    B060_070 = "0.60-0.70"
    B070_080 = "0.70-0.80"
    B080_PLUS = "0.80+"


class TransitionDecile(str, Enum):
    D1_VERY_NEG = "very_negative"   # tanh < -0.6
    D2_NEG = "negative"             # -0.6 to -0.3
    D3_SLIGHT_NEG = "slight_negative"  # -0.3 to -0.1
    D4_NEUTRAL_LOW = "neutral_low"  # -0.1 to 0.1
    D5_NEUTRAL_HIGH = "neutral_high"  # 0.1 to 0.3 (same as low for symmetry)
    D6_POSITIVE = "positive"        # 0.3 to 0.6
    D7_VERY_POS = "very_positive"   # > 0.6
    D_UNKNOWN = "unknown"


class TimeToExpiry(str, Enum):
    T_UNDER_3M = "<3m"
    T_3_5M = "3-5m"
    T_5_10M = "5-10m"
    T_10_15M = "10-15m"
    T_15_PLUS = "15m+"


class DirectionTag(str, Enum):
    DOWN_CONTINUATION = "DOWN_CONTINUATION"
    UP_CONTINUATION = "UP_CONTINUATION"
    DOWN_REVERSAL = "DOWN_REVERSAL"
    UP_REVERSAL = "UP_REVERSAL"
    NEUTRAL = "NEUTRAL"


def bucket_from_price(price: float) -> Bucket:
    """Map entry price to bucket enum."""
    if price < 0.10:
        return Bucket.B005_010
    elif price < 0.20:
        return Bucket.B010_020
    elif price < 0.30:
        return Bucket.B020_030
    elif price < 0.40:
        return Bucket.B030_040
    elif price < 0.50:
        return Bucket.B040_050
    elif price < 0.60:
        return Bucket.B050_060
    elif price < 0.70:
        return Bucket.B060_070
    elif price < 0.80:
        return Bucket.B070_080
    else:
        return Bucket.B080_PLUS


def decile_from_transition(transition_score: float) -> TransitionDecile:
    """Map tanh-normalized transition score to decile."""
    if transition_score < -0.6:
        return TransitionDecile.D1_VERY_NEG
    elif transition_score < -0.3:
        return TransitionDecile.D2_NEG
    elif transition_score < -0.1:
        return TransitionDecile.D3_SLIGHT_NEG
    elif transition_score < 0.1:
        return TransitionDecile.D4_NEUTRAL_LOW
    elif transition_score < 0.3:
        return TransitionDecile.D5_NEUTRAL_HIGH
    elif transition_score < 0.6:
        return TransitionDecile.D6_POSITIVE
    elif transition_score >= 0.6:
        return TransitionDecile.D7_VERY_POS
    else:
        return TransitionDecile.D_UNKNOWN


def time_to_expiry_bucket(seconds: float) -> TimeToExpiry:
    """Map seconds to expiry to bucket."""
    if seconds < 180:
        return TimeToExpiry.T_UNDER_3M
    elif seconds < 300:
        return TimeToExpiry.T_3_5M
    elif seconds < 600:
        return TimeToExpiry.T_5_10M
    elif seconds < 900:
        return TimeToExpiry.T_10_15M
    else:
        return TimeToExpiry.T_15_PLUS


def direction_tag(side: str, rsi: float) -> DirectionTag:
    """Derive directional asymmetry tag from side + RSI."""
    if side == "DOWN" and rsi < 35:
        return DirectionTag.DOWN_CONTINUATION
    elif side == "DOWN" and rsi > 65:
        return DirectionTag.DOWN_REVERSAL
    elif side == "UP" and rsi > 65:
        return DirectionTag.UP_CONTINUATION
    elif side == "UP" and rsi < 35:
        return DirectionTag.UP_REVERSAL
    else:
        return DirectionTag.NEUTRAL


# ── Cell Key ──

@dataclass(frozen=True)
class CellKey:
    """Immutable cell identifier: asset × interval × side × entry_bucket × regime × transition_decile × time_to_expiry"""
    asset: str           # "BTC", "ETH", "SOL", "XRP"
    interval: str        # "5m", "15m"
    side: str            # "UP", "DOWN"
    entry_bucket: str    # "0.40-0.50", "0.50-0.60", etc.
    regime: str          # "trend_continuation", "panic_sell", etc.
    transition_decile: str  # "very_negative", "positive", etc.
    time_to_expiry: str  # "<3m", "5-10m", etc.

    def __str__(self):
        return f"{self.asset}×{self.interval}×{self.side}×{self.entry_bucket}×{self.regime}×{self.transition_decile}×{self.time_to_expiry}"


# ── Cell State ──

@dataclass
class CellState:
    """Mutable state for a single cell — tracks all trade outcomes and statistics."""
    key: CellKey
    status: CellStatus = CellStatus.EXPLORING
    direction_tag: str = "NEUTRAL"
    
    # ── Trade counts ──
    resolved_trades: int = 0
    wins: int = 0
    losses: int = 0
    unresolved: int = 0
    
    # ── PnL ──
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    
    # ── Bayesian posterior (Beta distribution) ──
    alpha: float = 1.0  # Beta prior alpha (pseudocount for wins)
    beta: float = 1.0   # Beta prior beta (pseudocount for losses)
    
    # ── Streaks ──
    current_streak: int = 0      # positive = win streak, negative = loss streak
    max_win_streak: int = 0
    max_loss_streak: int = 0
    
    # ── Drawdown ──
    peak_pnl: float = 0.0
    max_drawdown: float = 0.0
    current_drawdown: float = 0.0
    
    # ── Kill reasons ──
    kill_reason: str = ""
    kill_timestamp: float = 0.0
    
    # ── Promotion ──
    promotion_timestamp: float = 0.0
    promotion_reason: str = ""
    
    # ── Timing ──
    first_trade_ts: float = 0.0
    last_trade_ts: float = 0.0
    
    # ── Error tracking ──
    settlement_errors: int = 0
    accounting_errors: int = 0
    
    # ── Derived stats ──
    @property
    def win_rate(self) -> float:
        return self.wins / self.resolved_trades if self.resolved_trades > 0 else 0.0
    
    @property
    def profit_factor(self) -> float:
        return self.gross_profit / abs(self.gross_loss) if self.gross_loss != 0 else float('inf')
    
    @property
    def ev_per_dollar(self) -> float:
        """Expected value per dollar invested."""
        if self.resolved_trades == 0:
            return 0.0
        return self.total_pnl / (self.resolved_trades * 2.0)  # $2 per trade
    
    @property
    def posterior_mean(self) -> float:
        """Posterior mean win probability (Beta distribution)."""
        return self.alpha / (self.alpha + self.beta)
    
    @property
    def posterior_ev(self) -> float:
        """Posterior expected value per dollar at entry 0.50.
        EV = posterior_p * (1/0.50 - 1) - (1 - posterior_p)
           = posterior_p * 1.0 - (1 - posterior_p)
           = 2 * posterior_p - 1
        """
        p = self.posterior_mean
        return 2.0 * p - 1.0
    
    @property
    def credible_lower_ev(self) -> float:
        """Lower bound of 95% credible interval for EV.
        Uses Beta(alpha, beta) -> lower = Beta.ppf(0.025, alpha, beta).
        Approximation: posterior_mean - 1.96 * sqrt(variance)
        """
        a, b = self.alpha, self.beta
        variance = (a * b) / ((a + b) ** 2 * (a + b + 1))
        std = math.sqrt(variance) if variance > 0 else 0
        lower_p = max(0, self.posterior_mean - 1.96 * std)
        return 2.0 * lower_p - 1.0
    
    @property
    def break_even_wr(self) -> float:
        """Break-even win rate at average entry price."""
        if self.resolved_trades == 0:
            return 0.5  # Assume 50/50 entry price
        # Approximation: at entry 0.50, break-even WR = 50%
        # At entry 0.56, break-even WR = 56%
        return 0.50  # Will be refined with actual entry prices


class CellTracker:
    """Tracks all cells and manages their lifecycle.
    
    Cells are created on-demand when a trade is logged.
    Kill and promotion rules are applied after each trade resolution.
    """
    
    def __init__(self):
        self._cells: Dict[CellKey, CellState] = {}
        self._trade_log: List[Dict] = []
    
    def get_or_create_cell(self, key: CellKey) -> CellState:
        """Get existing cell or create a new one."""
        if key not in self._cells:
            self._cells[key] = CellState(key=key)
        return self._cells[key]
    
    def log_trade_result(
        self,
        key: CellKey,
        win: bool,
        pnl: float,
        settlement_error: bool = False,
        accounting_error: bool = False,
        entry_price: float = 0.5,
        timestamp: float = 0.0,
    ) -> CellState:
        """Log a resolved trade result to a cell and update its state.
        
        Args:
            key: Cell identifier
            win: True if binary WIN
            pnl: Realized settlement PnL (positive or negative)
            settlement_error: True if settlement was invalid
            accounting_error: True if PnL didn't match expected
            entry_price: Entry price for the trade
            timestamp: Unix timestamp
        
        Returns:
            Updated CellState after applying kill/promotion rules.
        """
        cell = self.get_or_create_cell(key)
        
        if timestamp == 0:
            timestamp = time.time()
        
        if cell.first_trade_ts == 0:
            cell.first_trade_ts = timestamp
        cell.last_trade_ts = timestamp
        
        # Track errors
        if settlement_error:
            cell.settlement_errors += 1
        if accounting_error:
            cell.accounting_errors += 1
        
        # Update counts
        cell.resolved_trades += 1
        if win:
            cell.wins += 1
            cell.alpha += 1  # Bayesian update
            cell.gross_profit += pnl
            cell.current_streak = max(1, cell.current_streak + 1) if cell.current_streak > 0 else 1
            cell.max_win_streak = max(cell.max_win_streak, cell.current_streak)
        else:
            cell.losses += 1
            cell.beta += 1   # Bayesian update
            cell.gross_loss += abs(pnl)
            cell.current_streak = min(-1, cell.current_streak - 1) if cell.current_streak < 0 else -1
            cell.max_loss_streak = max(cell.max_loss_streak, abs(cell.current_streak))
        
        # Update PnL
        cell.total_pnl += pnl
        
        # Update drawdown
        if cell.total_pnl > cell.peak_pnl:
            cell.peak_pnl = cell.total_pnl
        cell.current_drawdown = cell.peak_pnl - cell.total_pnl
        cell.max_drawdown = max(cell.max_drawdown, cell.current_drawdown)
        
        # ── Apply Kill Rules (§4) ──
        self._apply_kill_rules(cell)
        
        # ── Apply Promotion Rules (§5) ──
        if cell.status != CellStatus.DISABLED:
            self._apply_promotion_rules(cell)
        
        # Log the trade
        self._trade_log.append({
            "timestamp": timestamp,
            "cell": str(key),
            "win": win,
            "pnl": pnl,
            "entry_price": entry_price,
            "cell_status": cell.status.value,
            "settlement_error": settlement_error,
            "accounting_error": accounting_error,
        })
        
        return cell
    
    def _apply_kill_rules(self, cell: CellState):
        """§4 Aggressive Kill Rules.
        
        Kill immediately if:
          - resolved_trades >= 10 AND ev_per_dollar < -0.10
          - resolved_trades >= 20 AND PF < 0.90
          - max_loss_streak >= 8
          - settlement_error > 0
          - accounting_error > 0
        """
        if cell.status == CellStatus.DISABLED:
            return
        
        # Kill: negative EV after 10 trades
        if cell.resolved_trades >= 10 and cell.ev_per_dollar < -0.10:
            cell.status = CellStatus.DISABLED
            cell.kill_reason = f"EV_PER_DOLLAR={cell.ev_per_dollar:.4f} < -0.10 after {cell.resolved_trades} trades"
            cell.kill_timestamp = time.time()
            return
        
        # Kill: low profit factor after 20 trades
        if cell.resolved_trades >= 20 and cell.profit_factor < 0.90:
            cell.status = CellStatus.DISABLED
            cell.kill_reason = f"PF={cell.profit_factor:.4f} < 0.90 after {cell.resolved_trades} trades"
            cell.kill_timestamp = time.time()
            return
        
        # Kill: loss streak >= 8
        if cell.max_loss_streak >= 8:
            cell.status = CellStatus.DISABLED
            cell.kill_reason = f"MAX_LOSS_STREAK={cell.max_loss_streak} >= 8"
            cell.kill_timestamp = time.time()
            return
        
        # Kill: settlement or accounting errors
        if cell.settlement_errors > 0:
            cell.status = CellStatus.DISABLED
            cell.kill_reason = f"SETTLEMENT_ERRORS={cell.settlement_errors} > 0"
            cell.kill_timestamp = time.time()
            return
        
        if cell.accounting_errors > 0:
            cell.status = CellStatus.DISABLED
            cell.kill_reason = f"ACCOUNTING_ERRORS={cell.accounting_errors} > 0"
            cell.kill_timestamp = time.time()
            return
    
    def _apply_promotion_rules(self, cell: CellState):
        """§5 Aggressive Promotion Rules.
        
        Promote to HIGH-PRIORITY paper if:
          - resolved_trades >= 20
          - ev_per_dollar > 0.10
          - PF >= 1.25
          - WR > break_even_WR + 5pp
        
        Promote to LIVE_CANDIDATE if:
          - resolved_trades >= 50
          - ev_per_dollar > 0.05
          - PF >= 1.25
          - max_drawdown acceptable
          - settlement_errors = 0
          - accounting_errors = 0
        """
        # Promote to HIGH-PRIORITY paper exploitation
        if cell.resolved_trades >= 20:
            if cell.ev_per_dollar > 0.10 and cell.profit_factor >= 1.25 and cell.win_rate > (cell.break_even_wr + 0.05):
                if cell.status in (CellStatus.EXPLORING, CellStatus.ACTIVE):
                    cell.status = CellStatus.PROMOTED
                    cell.promotion_timestamp = time.time()
                    cell.promotion_reason = (
                        f"EV/dollar={cell.ev_per_dollar:.4f}>0.10, "
                        f"PF={cell.profit_factor:.2f}>=1.25, "
                        f"WR={cell.win_rate:.1%}>{cell.break_even_wr+0.05:.1%}"
                    )
        
        # Promote to LIVE_CANDIDATE
        if cell.resolved_trades >= 50:
            if (cell.ev_per_dollar > 0.05 and cell.profit_factor >= 1.25 and
                cell.settlement_errors == 0 and cell.accounting_errors == 0 and
                cell.max_drawdown < 10.0):  # $10 max drawdown for $2 positions
                cell.status = CellStatus.LIVE_CANDIDATE
                cell.promotion_timestamp = time.time()
                cell.promotion_reason = (
                    f"EV/dollar={cell.ev_per_dollar:.4f}>0.05, "
                    f"PF={cell.profit_factor:.2f}>=1.25, "
                    f"settled={cell.resolved_trades}, "
                    f"errors=0"
                )
    
    def get_cells_by_status(self, status: CellStatus) -> List[CellState]:
        """Get all cells with a given status."""
        return [c for c in self._cells.values() if c.status == status]
    
    def get_all_cells(self) -> Dict[CellKey, CellState]:
        """Get all cells."""
        return dict(self._cells)
    
    def get_top_cells(self, n: int = 20, metric: str = "ev_per_dollar") -> List[CellState]:
        """Get top N cells by a metric."""
        active_cells = [c for c in self._cells.values() if c.status != CellStatus.DISABLED]
        
        if metric == "ev_per_dollar":
            active_cells.sort(key=lambda c: c.ev_per_dollar, reverse=True)
        elif metric == "profit_factor":
            active_cells.sort(key=lambda c: c.profit_factor if c.profit_factor != float('inf') else 999, reverse=True)
        elif metric == "win_rate":
            active_cells.sort(key=lambda c: c.win_rate, reverse=True)
        elif metric == "posterior_ev":
            active_cells.sort(key=lambda c: c.posterior_ev, reverse=True)
        
        return active_cells[:n]
    
    def get_bottom_cells(self, n: int = 20) -> List[CellState]:
        """Get bottom N cells by EV per dollar."""
        active_cells = [c for c in self._cells.values() if c.status != CellStatus.DISABLED]
        active_cells.sort(key=lambda c: c.ev_per_dollar)
        return active_cells[:n]
    
    def get_summary(self) -> Dict:
        """Get summary statistics across all cells."""
        total_cells = len(self._cells)
        by_status = defaultdict(int)
        for c in self._cells.values():
            by_status[c.status.value] += 1
        
        active = [c for c in self._cells.values() if c.status != CellStatus.DISABLED]
        killed = [c for c in self._cells.values() if c.status == CellStatus.DISABLED]
        
        return {
            "total_cells": total_cells,
            "by_status": dict(by_status),
            "total_pnl": round(sum(c.total_pnl for c in self._cells.values()), 4),
            "total_trades": sum(c.resolved_trades for c in self._cells.values()),
            "total_wins": sum(c.wins for c in self._cells.values()),
            "total_losses": sum(c.losses for c in self._cells.values()),
            "active_cells": len(active),
            "killed_cells": len(killed),
            "promoted_cells": by_status.get("PROMOTED", 0),
            "live_candidates": by_status.get("LIVE_CANDIDATE", 0),
        }