#!/usr/bin/env python3
"""
V20.3.1 §§6-9 — RSI Demotion, Directional Matrix, Cell Tournament, Adaptive Exploration
========================================================================================
§6: RSI becomes context feature only, NOT direction engine
§7: Directional efficiency matrix (asset×interval×direction×regime×vol_state)
§8: Continuous cell tournament (every 15min)
§9: Volatility-adaptive exploration (compress=explore, expand=exploit)

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
import math
import time
import random
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
from enum import Enum

import sys
sys.path.insert(0, '/home/naq1987s/father-daddy-capital')
from src.cell.cell_framework import CellKey, CellState, CellTracker, CellStatus, DirectionTag


# ══════════════════════════════════════════════════════════════════
# §6 — RSI Demotion
# ══════════════════════════════════════════════════════════════════

RSI_WEIGHT_REDUCTION = 0.20  # RSI weight reduced from 1.0 to 0.20
MICROSTRUCTURE_WEIGHTS = {
    "orderbook_imbalance": 0.25,
    "transition_dynamics": 0.20,
    "momentum_persistence": 0.15,
    "volatility_state": 0.10,
    "regime_structure": 0.10,
    "oracle_lag": 0.05,
    "market_drift": 0.05,
    "directional_asymmetry": 0.10,
    "rsi_context": 0.05,     # RSI demoted to 5% as context only
}


def compute_directional_signal(
    rsi: float,
    orderbook_imbalance: float,
    transition_score: float,
    momentum_persistence: float,
    volatility_state: str,
    regime: str,
    directional_asymmetry: float,
    market_drift: float = 0.0,
    oracle_lag_ms: float = 0.0,
) -> Dict[str, object]:
    """Compute combined directional signal with RSI demoted to context.
    
    RSI is NO LONGER the primary direction authority.
    Microstructure, momentum, and imbalance dominate.
    
    Returns dict of weighted signals and composite score.
    """
    signals: Dict[str, float] = {}
    rsi_signal = (rsi - 50) / 50
    signals["orderbook_imbalance"] = float(orderbook_imbalance)
    signals["transition_dynamics"] = float(transition_score)
    signals["momentum_persistence"] = float(momentum_persistence)
    signals["volatility_state"] = float({"compress": -0.5, "expand": 0.5, "neutral": 0.0}.get(volatility_state, 0.0))
    signals["regime_structure"] = float({"trend_continuation": 0.3, "panic_sell": -0.5, "balanced_rotation": 0.0,
                            "volatility_expansion": 0.2, "volatility_compression": -0.2,
                            "fake_reversal": 0.1, "liquidity_vacuum": -0.3}.get(regime, 0.0))
    signals["oracle_lag"] = -min(1.0, oracle_lag_ms / 1000.0)
    signals["market_drift"] = float(market_drift)
    signals["directional_asymmetry"] = float(directional_asymmetry)
    signals["rsi_context"] = rsi_signal * RSI_WEIGHT_REDUCTION

    composite = sum(
        MICROSTRUCTURE_WEIGHTS.get(k, 0.1) * v
        for k, v in signals.items()
    )

    signals["composite"] = composite
    signals["rsi_weight_pct"] = RSI_WEIGHT_REDUCTION * 100.0

    result: Dict[str, object] = dict(signals)
    result["primary_authority"] = "orderbook_imbalance" if abs(orderbook_imbalance) > 0.1 else "momentum"

    return result


# ══════════════════════════════════════════════════════════════════
# §7 — Directional Market Efficiency Matrix
# ══════════════════════════════════════════════════════════════════

class VolState(str, Enum):
    COMPRESS = "compress"
    EXPAND = "expand"
    NEUTRAL = "neutral"


@dataclass
class DirectionalEfficiency:
    """WR, EV, PF, Decay, Allocation for each direction tag combination."""
    asset: str = ""
    interval: str = ""
    direction_tag: str = ""      # DOWN_CONTINUATION, UP_REVERSAL, etc.
    regime: str = ""
    vol_state: str = ""
    
    # Metrics
    wr: float = 0.0
    ev_per_dollar: float = 0.0
    profit_factor: float = 0.0
    decay_rate: float = 0.0
    
    # Allocation
    allocation_weight: float = 0.0
    sample_size: int = 0


class DirectionalEfficiencyMatrix:
    """Empirically evaluate directional asymmetry across markets.
    
    Matrix: Asset × Interval × DirectionTag × Regime × VolState
    Populated from realized binary outcomes, not assumptions.
    """
    
    def __init__(self, tracker: CellTracker):
        self.tracker = tracker
        self._matrix: Dict[str, DirectionalEfficiency] = {}
    
    def _matrix_key(self, asset: str, interval: str, direction_tag: str,
                    regime: str, vol_state: str) -> str:
        return f"{asset}|{interval}|{direction_tag}|{regime}|{vol_state}"
    
    def update_from_cells(self, vol_state: VolState = VolState.NEUTRAL):
        """Update matrix from cell tracker data."""
        cells = self.tracker.get_all_cells()
        
        for key, cell in cells.items():
            if cell.status == CellStatus.DISABLED:
                continue
            
            # Derive direction tag from cell side + RSI zone
            # This is approximate — real RSI comes from market data
            direction = direction_tag_heuristic(cell.key.side, cell.key.regime)
            
            matrix_key = self._matrix_key(
                cell.key.asset, cell.key.interval, direction,
                cell.key.regime, vol_state.value
            )
            
            self._matrix[matrix_key] = DirectionalEfficiency(
                asset=cell.key.asset,
                interval=cell.key.interval,
                direction_tag=direction,
                regime=cell.key.regime,
                vol_state=vol_state.value,
                wr=cell.win_rate,
                ev_per_dollar=cell.ev_per_dollar,
                profit_factor=cell.profit_factor if cell.profit_factor != float('inf') else 10.0,
                decay_rate=0.0,  # Updated by CellHealthAnalyzer
                allocation_weight=0.0,
                sample_size=cell.resolved_trades,
            )
    
    def get_directional_allocation(self) -> Dict[str, float]:
        """Compute allocation weights across directions.
        
        Favors persistent profitable asymmetries with low decay.
        Uses softmax over EV/dollar * (1 - decay_rate).
        """
        if not self._matrix:
            return {}
        
        entries = list(self._matrix.values())
        scores = {}
        for entry in entries:
            if entry.sample_size < 5:
                score = 0.01  # Minimal exploration weight
            else:
                # Favor: high EV, high PF, low decay
                score = entry.ev_per_dollar * min(entry.profit_factor, 5.0) * max(0.1, 1.0 - entry.decay_rate)
                score = max(0.01, score)
            scores[entry.direction_tag] = scores.get(entry.direction_tag, 0) + score
        
        # Softmax normalization
        total = sum(math.exp(s) for s in scores.values())
        if total == 0:
            return {k: 1.0 / len(scores) for k in scores}
        
        return {k: math.exp(v) / total for k, v in scores.items()}
    
    def get_matrix_csv(self) -> str:
        """Export directional efficiency matrix as CSV."""
        lines = ["asset,interval,direction_tag,regime,vol_state,wr,ev_per_dollar,pf,decay_rate,sample_size"]
        for entry in self._matrix.values():
            lines.append(
                f"{entry.asset},{entry.interval},{entry.direction_tag},{entry.regime},"
                f"{entry.vol_state},{entry.wr:.4f},{entry.ev_per_dollar:.4f},"
                f"{entry.profit_factor:.2f},{entry.decay_rate:.4f},{entry.sample_size}"
            )
        return "\n".join(lines)


def direction_tag_heuristic(side: str, regime: str) -> str:
    """Approximate direction tag from side and regime.
    
    Full RSI-based tagging requires live market data.
    """
    if side == "DOWN" and regime in ("panic_sell", "volatility_expansion"):
        return "DOWN_CONTINUATION"
    elif side == "DOWN" and regime in ("fake_reversal", "trend_exhaustion"):
        return "DOWN_REVERSAL"
    elif side == "UP" and regime in ("volatility_compression", "trend_continuation"):
        return "UP_CONTINUATION"
    elif side == "UP" and regime in ("panic_sell", "liquidity_vacuum"):
        return "UP_REVERSAL"
    else:
        return "NEUTRAL"


# ══════════════════════════════════════════════════════════════════
# §8 — Continuous Cell Tournament
# ══════════════════════════════════════════════════════════════════

TOURNAMENT_INTERVAL = 900  # 15 minutes


@dataclass
class TournamentResult:
    """Result of a cell tournament round."""
    timestamp: float = 0.0
    top_cells: List[str] = field(default_factory=list)
    challenger_cells: List[str] = field(default_factory=list)
    dying_cells: List[str] = field(default_factory=list)
    newborn_cells: List[str] = field(default_factory=list)
    killed_cells: List[str] = field(default_factory=list)
    promoted_cells: List[str] = field(default_factory=list)
    allocation_changes: Dict[str, float] = field(default_factory=dict)


class CellTournament:
    """Continuous evolutionary tournament for cells.
    
    Every 15 minutes:
      - Rerank all cells
      - Identify top (champions), challengers, dying, newborn
      - Reallocate capital: strong absorb, weak die
    """
    
    def __init__(self, tracker: CellTracker):
        self.tracker = tracker
        self._last_tournament: float = 0.0
        self._previous_top: List[CellKey] = []
        self._previous_challengers: List[CellKey] = []
    
    def run_tournament(self) -> TournamentResult:
        """Run a tournament round.
        
        Classifies cells into:
          - top_cells: Top 20% by EV (champions)
          - challenger_cells: Rising cells with improving metrics
          - dying_cells: Decaying cells with deteriorating metrics
          - newborn_cells: Recently created cells (< 5 trades)
          - killed_cells: DISABLED cells from this round
        """
        result = TournamentResult(timestamp=time.time())
        cells = self.tracker.get_all_cells()
        
        # Classify all active cells
        active = [(k, c) for k, c in cells.items() if c.status != CellStatus.DISABLED]
        if not active:
            return result
        
        # Sort by EV/dollar
        active.sort(key=lambda x: x[1].ev_per_dollar, reverse=True)
        
        # ── Top cells (top 20%) ──
        n_top = max(1, len(active) // 5)
        top = active[:n_top]
        result.top_cells = [str(k) for k, c in top]
        
        # ── Challengers (improving EV, not yet top) ──
        challengers = []
        for k, c in active[n_top:]:
            if c.resolved_trades >= 5 and self.rolling_ev_velocity(c) > 0:
                challengers.append((k, c))
        result.challenger_cells = [str(k) for k, c in challengers]
        
        # ── Dying cells (deteriorating) ──
        dying = []
        for k, c in active:
            if c.resolved_trades >= 10 and c.ev_per_dollar < -0.05:
                dying.append((k, c))
        result.dying_cells = [str(k) for k, c in dying]
        
        # ── Newborn cells (< 5 trades) ──
        newborn = []
        for k, c in active:
            if c.resolved_trades < 5:
                newborn.append((k, c))
        result.newborn_cells = [str(k) for k, c in newborn]
        
        # ── Record and return ──
        self._previous_top = [k for k, c in top]
        self._previous_challengers = [k for k, c in challengers]
        self._last_tournament = time.time()
        
        return result
    
    def rolling_ev_velocity(self, cell: CellState) -> float:
        """Approximate rolling EV velocity from cell state.
        Positive = improving, negative = deteriorating.
        """
        if cell.resolved_trades < 5:
            return 0.0
        # Use recent PnL trend as proxy
        if cell.total_pnl > 0 and cell.ev_per_dollar > 0:
            return cell.ev_per_dollar * 0.1  # Improving
        elif cell.ev_per_dollar < 0:
            return -0.1  # Deteriorating
        return 0.0


# ══════════════════════════════════════════════════════════════════
# §9 — Volatility-Adaptive Exploration
# ══════════════════════════════════════════════════════════════════

@dataclass
class ExplorationPressure:
    """Adaptive exploration pressure based on volatility regime."""
    volatility_regime: str = "neutral"  # "compress" or "expand"
    exploration_pressure: float = 0.5   # 0=min, 1=max
    adaptive_explore_ratio: float = 0.10  # Base 10%
    current_explore_ratio: float = 0.10
    current_exploit_ratio: float = 0.70
    current_promising_ratio: float = 0.20
    reason: str = ""


class VolatilityAdaptiveExploration:
    """Reverse traditional exploration behavior.
    
    During volatility compression:
      → Increase exploration (inefficiencies measurable, noise low)
    During volatility expansion:
      → Reduce exploration (signal unreliable, exploit proven cells)
    """
    
    BASE_EXPLORE = 0.10
    BASE_PROMISING = 0.20
    BASE_EXPLOIT = 0.70
    
    def compute_pressure(self, realized_volatility: float,
                          vol_regime: str,
                          recent_vol_history: Optional[List[float]] = None) -> ExplorationPressure:
        """Compute adaptive exploration pressure.
        
        Args:
            realized_volatility: Current realized volatility
            vol_regime: "compress", "expand", or "neutral"
            recent_vol_history: Recent volatility readings for trend
        
        Returns:
            ExplorationPressure with adjusted ratios
        """
        pressure = ExplorationPressure()
        pressure.volatility_regime = vol_regime
        
        if vol_regime == "compress":
            # During compression: increase exploration
            # Noise is low, alpha is measurable
            pressure.exploration_pressure = 0.8
            pressure.current_explore_ratio = min(0.35, self.BASE_EXPLORE * 2.0)
            pressure.adaptive_explore_ratio = pressure.current_explore_ratio
            pressure.current_exploit_ratio = 0.60 - (pressure.current_explore_ratio - self.BASE_EXPLORE)
            pressure.current_promising_ratio = 0.40 - pressure.current_exploit_ratio
            pressure.reason = "Volatility compression: increase exploration, alpha measurable"
        elif vol_regime == "expand":
            # During expansion: reduce exploration, increase exploitation
            # Signal unreliable, stick to proven cells
            pressure.exploration_pressure = 0.2
            pressure.current_explore_ratio = max(0.03, self.BASE_EXPLORE * 0.3)
            pressure.adaptive_explore_ratio = pressure.current_explore_ratio
            pressure.current_exploit_ratio = min(0.85, self.BASE_EXPLOIT * 1.15)
            pressure.current_promising_ratio = 1.0 - pressure.current_explore_ratio - pressure.current_exploit_ratio
            pressure.reason = "Volatility expansion: reduce exploration, exploit proven cells"
        else:
            # Neutral: base ratios
            pressure.exploration_pressure = 0.5
            pressure.current_explore_ratio = self.BASE_EXPLORE
            pressure.current_exploit_ratio = self.BASE_EXPLOIT
            pressure.current_promising_ratio = self.BASE_PROMISING
            pressure.adaptive_explore_ratio = self.BASE_EXPLORE
            pressure.reason = "Neutral volatility: base allocation ratios"
        
        # Normalize to ensure they sum to 1.0
        total = pressure.current_explore_ratio + pressure.current_exploit_ratio + pressure.current_promising_ratio
        if abs(total - 1.0) > 0.01:
            pressure.current_explore_ratio /= total
            pressure.current_exploit_ratio /= total
            pressure.current_promising_ratio /= total
        
        return pressure