#!/usr/bin/env python3
"""
V20.3.1 §2 — Cell Half-Life Analytics
======================================
Measure edge persistence instead of isolated profitability.
Cells that decay rapidly must be disabled even if historically profitable.

Metrics:
  - cell_half_life: time (trades) for EV to decay to 50% of peak
  - cell_decay_rate: exponential decay coefficient
  - rolling_pf_velocity, rolling_ev_velocity, rolling_wr_velocity
  - rolling_drawdown_velocity

Promotion requires STABLE persistence, not temporary spikes.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
import math
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import deque

import sys
sys.path.insert(0, '/home/naq1987s/father-daddy-capital')
from src.cell.cell_framework import CellKey, CellState, CellTracker, CellStatus


# ── Configuration ──
ROLLING_WINDOW = 20          # Number of trades for rolling metrics
DECAY_SIGNIFICANCE = 0.10   # Minimum half-life in trades (below = rapid decay)
MIN_TRADES_FOR_HALFLIFE = 10


@dataclass
class CellHealthMetrics:
    """Health metrics for a single cell — measures persistence not just profitability."""
    cell_key: Optional[CellKey] = None
    
    # Core persistence
    cell_half_life: float = 0.0         # Trades until EV decays to 50% of peak
    cell_decay_rate: float = 0.0         # Exponential decay coefficient
    peak_ev: float = 0.0                 # Highest EV/dollar observed
    current_ev: float = 0.0              # Current EV/dollar
    
    # Rolling velocities (change per trade)
    rolling_pf_velocity: float = 0.0    # Rate of PF change
    rolling_ev_velocity: float = 0.0     # Rate of EV change
    rolling_wr_velocity: float = 0.0     # Rate of WR change
    rolling_drawdown_velocity: float = 0.0  # Rate of drawdown change
    
    # Persistence classification
    is_persistent: bool = False
    is_decay_alert: bool = False
    persistence_grade: str = "UNKNOWN"  # "DURABLE", "MODERATE", "FRAGILE", "DECAYING", "DEAD"
    
    # Recommendation
    recommendation: str = "HOLD"  # "PROMOTE", "HOLD", "DOWNGRADE", "KILL"


class CellHealthAnalyzer:
    """Analyze cell health and persistence.
    
    Measures whether edge is durable or temporary.
    Kills decaying cells even if historically profitable.
    """
    
    def __init__(self, tracker: CellTracker):
        self.tracker = tracker
        # Store rolling PnL histories per cell
        self._pnl_history: Dict[CellKey, deque] = {}
        self._ev_history: Dict[CellKey, deque] = {}
        self._pf_history: Dict[CellKey, deque] = {}
        self._wr_history: Dict[CellKey, deque] = {}
        self._dd_history: Dict[CellKey, deque] = {}
    
    def update_history(self, key: CellKey, win: bool, pnl: float):
        """Record trade outcome for rolling analysis."""
        if key not in self._pnl_history:
            self._pnl_history[key] = deque(maxlen=ROLLING_WINDOW * 3)
            self._ev_history[key] = deque(maxlen=ROLLING_WINDOW * 3)
            self._pf_history[key] = deque(maxlen=ROLLING_WINDOW * 3)
            self._wr_history[key] = deque(maxlen=ROLLING_WINDOW * 3)
            self._dd_history[key] = deque(maxlen=ROLLING_WINDOW * 3)
        
        cell = self.tracker.get_or_create_cell(key)
        
        self._pnl_history[key].append(pnl)
        self._ev_history[key].append(cell.ev_per_dollar)
        self._pf_history[key].append(cell.profit_factor if cell.profit_factor != float('inf') else 10.0)
        self._wr_history[key].append(cell.win_rate)
        self._dd_history[key].append(cell.current_drawdown)
    
    def analyze_cell(self, key: CellKey) -> CellHealthMetrics:
        """Analyze health and persistence of a single cell."""
        cell = self.tracker.get_or_create_cell(key)
        metrics = CellHealthMetrics(cell_key=key)
        
        # ── Derived stats ──
        metrics.peak_ev = max(h for h in self._ev_history.get(key, [0])) if key in self._ev_history else 0
        metrics.current_ev = cell.ev_per_dollar
        
        # ── Cell half-life ──
        metrics.cell_half_life = self._compute_half_life(key)
        metrics.cell_decay_rate = self._compute_decay_rate(key)
        
        # ── Rolling velocities ──
        metrics.rolling_pf_velocity = self._compute_velocity(key, self._pf_history)
        metrics.rolling_ev_velocity = self._compute_velocity(key, self._ev_history)
        metrics.rolling_wr_velocity = self._compute_velocity(key, self._wr_history)
        metrics.rolling_drawdown_velocity = self._compute_velocity(key, self._dd_history)
        
        # ── Persistence classification ──
        metrics.is_persistent = metrics.cell_half_life >= DECAY_SIGNIFICANCE * cell.resolved_trades if cell.resolved_trades > 0 else False
        metrics.is_decay_alert = metrics.cell_half_life > 0 and metrics.cell_half_life < DECAY_SIGNIFICANCE * cell.resolved_trades
        
        # ── Persistence grade ──
        if cell.resolved_trades < MIN_TRADES_FOR_HALFLIFE:
            metrics.persistence_grade = "UNKNOWN"
        elif cell.status == CellStatus.DISABLED:
            metrics.persistence_grade = "DEAD"
        elif metrics.rolling_ev_velocity > 0.01 and metrics.is_persistent:
            metrics.persistence_grade = "DURABLE"
        elif metrics.rolling_ev_velocity > 0 and metrics.is_persistent:
            metrics.persistence_grade = "MODERATE"
        elif metrics.rolling_ev_velocity < -0.01 or metrics.is_decay_alert:
            metrics.persistence_grade = "DECAYING"
        else:
            metrics.persistence_grade = "FRAGILE"
        
        # ── Recommendation ──
        if metrics.persistence_grade == "DURABLE" and cell.profit_factor >= 1.25:
            metrics.recommendation = "PROMOTE"
        elif metrics.persistence_grade in ("DURABLE", "MODERATE") and cell.ev_per_dollar > 0:
            metrics.recommendation = "HOLD"
        elif metrics.persistence_grade == "FRAGILE":
            metrics.recommendation = "DOWNGRADE"
        elif metrics.persistence_grade in ("DECAYING", "DEAD"):
            metrics.recommendation = "KILL"
        else:
            metrics.recommendation = "HOLD"
        
        return metrics
    
    def _compute_half_life(self, key: CellKey) -> float:
        """Compute half-life: number of trades for EV to decay to 50% of peak.
        
        Method: Find peak EV, then find how many trades until EV drops to 50%.
        If EV is still rising, half-life = infinity (no decay yet).
        If EV never peaked, half-life = 0.
        """
        evs = list(self._ev_history.get(key, deque()))
        if len(evs) < MIN_TRADES_FOR_HALFLIFE:
            return 0.0
        
        peak_ev = max(evs)
        if peak_ev <= 0:
            return 0.0
        
        target = peak_ev * 0.5
        peak_idx = evs.index(peak_ev)
        
        # Search for decay after peak
        for i in range(peak_idx + 1, len(evs)):
            if evs[i] <= target:
                return float(i - peak_idx)  # Half-life in trades
        
        # Still above half — no decay detected
        return float('inf')
    
    def _compute_decay_rate(self, key: CellKey) -> float:
        """Compute exponential decay rate of EV.
        
        Method: Fit EV(t) = peak * exp(-decay * t) using last N trades.
        Returns decay coefficient (positive = decaying, negative = growing).
        """
        evs = list(self._ev_history.get(key, deque()))
        if len(evs) < MIN_TRADES_FOR_HALFLIFE:
            return 0.0
        
        # Simple estimate: (last_ev - first_ev) / ((first_ev + last_ev) / 2) / n_trades
        first = evs[0] if evs[0] != 0 else 0.001
        last = evs[-1]
        n = len(evs)
        
        if first <= 0:
            return 0.0
        
        return (last - first) / (abs(first) * n)
    
    def _compute_velocity(self, key: CellKey, history: Dict) -> float:
        """Compute rate of change over rolling window.
        
        Velocity = (recent_mean - older_mean) / window
        Positive = improving, negative = deteriorating.
        """
        values = list(history.get(key, deque()))
        if len(values) < 4:
            return 0.0
        
        recent = values[-ROLLING_WINDOW:] if len(values) >= ROLLING_WINDOW else values[-len(values)//2:]
        older = values[:-ROLLING_WINDOW] if len(values) >= ROLLING_WINDOW * 2 else values[:len(values)//2]
        
        if not recent or not older:
            return 0.0
        
        recent_mean = sum(recent) / len(recent)
        older_mean = sum(older) / len(older)
        
        return recent_mean - older_mean
    
    def generate_half_life_report(self) -> Dict:
        """Generate half-life dashboard for all cells."""
        cells = self.tracker.get_all_cells()
        results = []
        
        for key, cell in cells.items():
            if cell.status == CellStatus.DISABLED:
                continue
            metrics = self.analyze_cell(key)
            results.append({
                "cell": str(key),
                "half_life": metrics.cell_half_life,
                "decay_rate": round(metrics.cell_decay_rate, 6),
                "peak_ev": round(metrics.peak_ev, 4),
                "current_ev": round(metrics.current_ev, 4),
                "pf_velocity": round(metrics.rolling_pf_velocity, 4),
                "ev_velocity": round(metrics.rolling_ev_velocity, 4),
                "wr_velocity": round(metrics.rolling_wr_velocity, 4),
                "persistence_grade": metrics.persistence_grade,
                "recommendation": metrics.recommendation,
                "trades": cell.resolved_trades,
            })
        
        # Sort by half-life descending (most persistent first)
        results.sort(key=lambda x: x["half_life"] if x["half_life"] != float('inf') else 999, reverse=True)
        
        return {
            "timestamp": time.time(),
            "total_cells_analyzed": len(results),
            "durable": len([r for r in results if r["persistence_grade"] == "DURABLE"]),
            "moderate": len([r for r in results if r["persistence_grade"] == "MODERATE"]),
            "fragile": len([r for r in results if r["persistence_grade"] == "FRAGILE"]),
            "decaying": len([r for r in results if r["persistence_grade"] == "DECAYING"]),
            "dead": len([r for r in results if r["persistence_grade"] == "DEAD"]),
            "cells": results,
        }