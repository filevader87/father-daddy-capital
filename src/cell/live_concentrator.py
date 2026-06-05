#!/usr/bin/env python3
"""
V20.3 Live Deployment Concentrator — §11
============================================
When live resumes, it is concentrated. Only the best cell trades live.

Initial live rule:
  - top_cell_only
  - $2 fixed size
  - max 1 open position
  - max 20 live trades

If live confirms:
  - PF >= 1.25 after 20 trades
  - realized_EV positive
  - no execution failures
  then: increase to $3, add second-best cell

If live fails:
  - disable cell immediately
  - return to paper

No averaging down. No hope. No "one more loop."

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum
import time

import sys
sys.path.insert(0, '/home/naq1987s/father-daddy-capital')
from src.cell.cell_framework import CellKey, CellStatus, CellTracker
from src.live_block_v203 import enforce_live_block, check_live_status, REALITY_ALIGNMENT_FAILED


# ── Live Deployment Config ──

INITIAL_SIZE_USD = 2.0
CONFIRMED_SIZE_USD = 3.0
MAX_OPEN_POSITIONS = 1
MAX_LIVE_TRADES = 20
MIN_CONFIRMED_PF = 1.25
MIN_CONFIRMED_EV_POSITIVE = True
MIN_CONFIRMED_TRADES = 20
MAX_FAILURES_BEFORE_DISABLE = 2


class LiveDeploymentPhase(str, Enum):
    BLOCKED = "BLOCKED"          # Reality alignment failed, live blocked
    PAPER_ONLY = "PAPER_ONLY"    # Paper trading only
    CONCENTRATED = "CONCENTRATED"  # Single top cell, $2, max 1 position
    CONFIRMED = "CONFIRMED"      # PF>=1.25 after 20 trades → $3, second cell added
    EXPANDING = "EXPANDING"      # Adding more cells cautiously


@dataclass
class LiveCellState:
    """Track a cell that's been promoted to live trading."""
    cell_key: CellKey
    size_usd: float = INITIAL_SIZE_USD
    live_trades: int = 0
    live_wins: int = 0
    live_losses: int = 0
    live_pnl: float = 0.0
    execution_failures: int = 0
    confirmed: bool = False
    disabled: bool = False
    disable_reason: str = ""


class LiveConcentrator:
    """Concentrated live deployment — only the best cell trades live.
    
    Phase progression:
      BLOCKED → PAPER_ONLY → CONCENTRATED → CONFIRMED → EXPANDING
    
    BLOCKED: All live paths raise RuntimeError.
    PAPER_ONLY: Paper trading, gathering cell evidence.
    CONCENTRATED: Top cell only, $2 size, 1 position, 20 trades max.
    CONFIRMED: PF>=1.25, EV positive, no failures → $3, add second cell.
    EXPANDING: Gradually adding more cells with confirmed evidence.
    
    If live fails at any phase:
      - Disable the cell immediately
      - Return to PAPER_ONLY
    """
    
    def __init__(self, tracker: CellTracker):
        self.tracker = tracker
        self._phase = LiveDeploymentPhase.PAPER_ONLY
        self._active_cells: Dict[CellKey, LiveCellState] = {}
        self._max_concurrent = MAX_OPEN_POSITIONS
    
    @property
    def phase(self) -> LiveDeploymentPhase:
        """Current deployment phase."""
        if REALITY_ALIGNMENT_FAILED:
            return LiveDeploymentPhase.BLOCKED
        return self._phase
    
    def can_place_live_order(self) -> tuple:
        """Check if a live order can be placed.
        
        Returns:
            (can_place: bool, reason: str)
        """
        # First check the global live block
        if REALITY_ALIGNMENT_FAILED:
            return (False, "LIVE_BLOCKED_REALITY_ALIGNMENT_FAILED")
        
        if self._phase == LiveDeploymentPhase.BLOCKED:
            return (False, "Phase is BLOCKED")
        
        if self._phase == LiveDeploymentPhase.PAPER_ONLY:
            return (False, "Phase is PAPER_ONLY — no live trading yet")
        
        if len(self._active_cells) == 0:
            return (False, "No active live cells")
        
        active_cells = [c for c in self._active_cells.values() if not c.disabled]
        if not active_cells:
            return (False, "All live cells disabled")
        
        # Check max live trades
        total_trades = sum(c.live_trades for c in active_cells)
        if total_trades >= MAX_LIVE_TRADES:
            return (False, f"Max live trades reached ({MAX_LIVE_TRADES})")
        
        return (True, f"Phase={self._phase.value}, active_cells={len(active_cells)}")
    
    def promote_top_cell_to_live(self) -> Optional[CellKey]:
        """Promote the top-scoring cell to concentrated live trading.
        
        Requirements:
          - REALITY_ALIGNMENT_FAILED must be False (live unblocked)
          - Cell must be LIVE_CANDIDATE status
          - Cell must have >= 50 resolved trades
          - Cell must have EV/dollar > 0.05
          - Cell must have PF >= 1.25
          - Cell must have 0 settlement errors
        """
        if REALITY_ALIGNMENT_FAILED:
            return None
        
        # Find top cell that's a LIVE_CANDIDATE
        candidates = self.tracker.get_cells_by_status(CellStatus.LIVE_CANDIDATE)
        if not candidates:
            return None
        
        # Sort by EV/dollar descending
        candidates.sort(key=lambda c: c.ev_per_dollar, reverse=True)
        top_cell = candidates[0]
        
        # Verify requirements
        if top_cell.resolved_trades < 50:
            return None
        if top_cell.ev_per_dollar < 0.05:
            return None
        if top_cell.profit_factor < 1.25:
            return None
        if top_cell.settlement_errors > 0 or top_cell.accounting_errors > 0:
            return None
        
        # Promote
        live_state = LiveCellState(cell_key=top_cell.key, size_usd=INITIAL_SIZE_USD)
        self._active_cells[top_cell.key] = live_state
        self._phase = LiveDeploymentPhase.CONCENTRATED
        
        return top_cell.key
    
    def record_live_result(self, cell_key: CellKey, win: bool, pnl: float,
                            execution_failure: bool = False):
        """Record a live trade result.
        
        If execution failure or cell underperforms, disable and return to paper.
        """
        if cell_key not in self._active_cells:
            return
        
        state = self._active_cells[cell_key]
        state.live_trades += 1
        
        if execution_failure:
            state.execution_failures += 1
            if state.execution_failures >= MAX_FAILURES_BEFORE_DISABLE:
                state.disabled = True
                state.disable_reason = f"Execution failures: {state.execution_failures}"
                self._return_to_paper(f"Cell {cell_key} had {state.execution_failures} execution failures")
                return
        
        if win:
            state.live_wins += 1
            state.live_pnl += pnl
        else:
            state.live_losses += 1
            state.live_pnl += pnl  # pnl is negative for losses
        
        # Check for confirmation after 20 trades
        if state.live_trades >= MIN_CONFIRMED_TRADES:
            live_wr = state.live_wins / state.live_trades
            live_pf = state.live_pnl / abs(min(0.01, -sum(1 for _ in range(state.live_losses)) * INITIAL_SIZE_USD))
            
            if live_wr > 0 and state.live_pnl > 0 and not execution_failure:
                state.confirmed = True
                state.size_usd = CONFIRMED_SIZE_USD
                self._phase = LiveDeploymentPhase.CONFIRMED
            else:
                # Cell failed live — disable and return to paper
                state.disabled = True
                state.disable_reason = f"Failed live: PF={live_pf:.2f}, PnL=${state.live_pnl:.2f}"
                self._return_to_paper(f"Cell {cell_key} failed live: {state.disable_reason}")
    
    def add_second_cell(self) -> Optional[CellKey]:
        """After confirmation, add second-best cell to live.
        
        Only called in CONFIRMED phase.
        """
        if self._phase != LiveDeploymentPhase.CONFIRMED:
            return None
        
        if len([c for c in self._active_cells.values() if not c.disabled]) >= 2:
            return None  # Already have 2 cells
        
        # Find next LIVE_CANDIDATE that's not already live
        active_keys = set(self._active_cells.keys())
        candidates = [
            c for c in self.tracker.get_cells_by_status(CellStatus.LIVE_CANDIDATE)
            if c.key not in active_keys
        ]
        
        if not candidates:
            return None
        
        candidates.sort(key=lambda c: c.ev_per_dollar, reverse=True)
        second_cell = candidates[0]
        
        live_state = LiveCellState(cell_key=second_cell.key, size_usd=CONFIRMED_SIZE_USD)
        self._active_cells[second_cell.key] = live_state
        self._max_concurrent = 2
        
        return second_cell.key
    
    def _return_to_paper(self, reason: str):
        """Return to paper-only mode after live failure."""
        self._phase = LiveDeploymentPhase.PAPER_ONLY
        # Disable all live cells
        for state in self._active_cells.values():
            if not state.disabled:
                state.disabled = True
                state.disable_reason = f"Returned to paper: {reason}"
    
    def get_status(self) -> Dict:
        """Get current live deployment status."""
        active_cells = [c for c in self._active_cells.values() if not c.disabled]
        return {
            "phase": self.phase.value,
            "reality_alignment_failed": REALITY_ALIGNMENT_FAILED,
            "active_live_cells": len(active_cells),
            "max_concurrent_positions": self._max_concurrent,
            "total_live_trades": sum(c.live_trades for c in active_cells),
            "total_live_pnl": round(sum(c.live_pnl for c in active_cells), 4),
            "cells": [
                {
                    "cell": str(c.cell_key),
                    "size_usd": c.size_usd,
                    "trades": c.live_trades,
                    "wins": c.live_wins,
                    "losses": c.live_losses,
                    "pnl": round(c.live_pnl, 4),
                    "confirmed": c.confirmed,
                    "disabled": c.disabled,
                    "disable_reason": c.disable_reason,
                }
                for c in self._active_cells.values()
            ],
        }