#!/usr/bin/env python3
"""
V20.3 Bandit Allocation Engine — §6
======================================
Thompson sampling + UCB over cells.
Score each cell by posterior_EV, PF, sample_size, drawdown_penalty, recency_weight.

Allocate paper trade attempts:
  70% to current top cells (exploitation)
  20% to promising under-sampled cells (promising)
  10% to exploration cells (exploration)

Rerank every 30 minutes: kill losers, promote winners.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
import math
import random
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import defaultdict

# Import cell framework
import sys
sys.path.insert(0, '/home/naq1987s/father-daddy-capital')
from src.cell.cell_framework import CellKey, CellState, CellStatus, CellTracker


# ── Configuration ──

EXPLOITATION_PCT = 0.70    # 70% to top cells
PROMISING_PCT = 0.20       # 20% to under-sampled promising
EXPLORATION_PCT = 0.10     # 10% to new exploration

RERANK_INTERVAL = 1800     # 30 minutes in seconds
MIN_SAMPLES_FOR_UCB = 5    # Minimum samples before UCB kicks in
RECENCY_HALFLIFE = 3600    # 1 hour halflife for recency weighting


@dataclass
class CellScore:
    """Score for a single cell in the bandit allocation."""
    cell_key: CellKey
    bandit_score: float = 0.0
    posterior_ev: float = 0.0
    profit_factor: float = 0.0
    sample_size: int = 0
    drawdown_penalty: float = 0.0
    recency_weight: float = 1.0
    allocation_bucket: str = "exploration"  # "exploitation", "promising", "exploration"


class BanditAllocator:
    """Thompson Sampling + UCB bandit allocation over cells.
    
    Allocates paper trade attempts across cells using:
      - Thompson sampling: draw from Beta(alpha, beta) posterior
      - UCB bonus: uncertainty bonus for under-sampled cells
      - Drawdown penalty: reduce score for high drawdown
      - Recency weight: discount stale cells
    
    Allocation:
      70% exploitation (top cells)
      20% promising (under-sampled but positive EV)
      10% exploration (new/unknown cells)
    """
    
    def __init__(self, tracker: CellTracker):
        self.tracker = tracker
        self._last_rerank: float = 0.0
        self._scores: Dict[CellKey, CellScore] = {}
    
    def compute_cell_score(self, cell: CellState) -> CellScore:
        """Compute bandit score for a cell.
        
        Score = posterior_ev * recency_weight * (1 - drawdown_penalty) + UCB_bonus
        
        Thompson sampling: draw p ~ Beta(alpha, beta)
        UCB bonus: sqrt(2 * ln(N) / n) for exploration
        """
        score = CellScore(cell_key=cell.key)
        
        # ── Thompson Sample ──
        # Draw from Beta(alpha, beta) posterior
        # Higher alpha → more wins → higher sample
        thompson_sample = random.betavariate(cell.alpha, cell.beta)
        score.posterior_ev = cell.posterior_ev
        
        # ── UCB Bonus ──
        # Encourage under-sampled cells
        total_trades = max(1, sum(c.resolved_trades for c in self.tracker.get_all_cells().values()))
        if cell.resolved_trades >= MIN_SAMPLES_FOR_UCB:
            ucb_bonus = math.sqrt(2 * math.log(total_trades) / cell.resolved_trades)
        else:
            ucb_bonus = 0.5  # Bonus for very under-sampled
        
        # ── Profit Factor ──
        pf = cell.profit_factor
        if pf == float('inf'):
            pf = 10.0  # Cap infinite PF
        score.profit_factor = pf
        
        # ── Sample Size Weight ──
        # More samples → more reliable → slightly higher weight
        sample_weight = min(1.0, cell.resolved_trades / 50.0)  # Caps at 1.0 at 50 trades
        
        # ── Drawdown Penalty ──
        # Heavy drawdown → reduce score
        if cell.max_drawdown > 0:
            drawdown_penalty = min(0.8, cell.max_drawdown / 10.0)  # $10 drawdown = 0.8 penalty
        else:
            drawdown_penalty = 0.0
        score.drawdown_penalty = drawdown_penalty
        
        # ── Recency Weight ──
        # Cells with recent activity get higher weight
        if cell.last_trade_ts > 0:
            age = time.time() - cell.last_trade_ts
            recency_weight = math.exp(-age / RECENCY_HALFLIFE)
        else:
            recency_weight = 0.5  # Unknown cells get moderate weight
        score.recency_weight = recency_weight
        
        # ── Final Score ──
        # thompson_sample is our belief about win probability
        # EV = thompson_sample * (1/0.5) - 1 at entry 0.50
        # Simplify: EV = 2 * thompson_sample - 1
        ev = 2.0 * thompson_sample - 1.0
        
        score.bandit_score = (
            ev *                                  # Expected value from Thompson
            recency_weight *                      # Favor recent
            (1.0 - drawdown_penalty) *            # Penalize drawdown
            sample_weight +                        # Weight by sample size
            ucb_bonus * 0.1                        # Small UCB exploration bonus
        )
        
        score.sample_size = cell.resolved_trades
        
        return score
    
    def rank_cells(self) -> List[CellScore]:
        """Rank all active cells by bandit score.
        
        Returns sorted list of CellScore objects.
        Also assigns allocation buckets: exploitation, promising, exploration.
        """
        cells = self.tracker.get_all_cells()
        scores = []
        
        for key, cell in cells.items():
            if cell.status == CellStatus.DISABLED:
                continue  # Dead cells don't get allocation
            
            score = self.compute_cell_score(cell)
            scores.append(score)
        
        # Sort by bandit score descending
        scores.sort(key=lambda s: s.bandit_score, reverse=True)
        
        # Assign allocation buckets
        n_cells = len(scores)
        if n_cells == 0:
            return []
        
        n_exploitation = max(1, int(n_cells * EXPLOITATION_PCT))
        n_promising = max(1, int(n_cells * PROMISING_PCT))
        # Rest is exploration
        
        for i, score in enumerate(scores):
            if i < n_exploitation:
                score.allocation_bucket = "exploitation"
            elif i < n_exploitation + n_promising:
                score.allocation_bucket = "promising"
            else:
                score.allocation_bucket = "exploration"
        
        self._scores = {s.cell_key: s for s in scores}
        self._last_rerank = time.time()
        
        return scores
    
    def allocate_trade(self) -> Optional[CellKey]:
        """Select which cell should get the next paper trade allocation.
        
        Uses Thompson sampling within each bucket, then selects bucket
        according to allocation percentages.
        
        70% chance → pick from exploitation bucket
        20% chance → pick from promising bucket
        10% chance → pick from exploration bucket
        """
        if not self._scores or (time.time() - self._last_rerank) > RERANK_INTERVAL:
            self.rank_cells()
        
        if not self._scores:
            return None
        
        # Select bucket
        roll = random.random()
        if roll < EXPLOITATION_PCT:
            bucket = "exploitation"
        elif roll < EXPLOITATION_PCT + PROMISING_PCT:
            bucket = "promising"
        else:
            bucket = "exploration"
        
        # Filter cells in bucket
        bucket_cells = [s for s in self._scores.values() if s.allocation_bucket == bucket]
        if not bucket_cells:
            # Fall back to any available cell
            bucket_cells = list(self._scores.values())
        
        if not bucket_cells:
            return None
        
        # Thompson sampling within bucket
        # Higher bandit_score → higher probability of selection
        # Use softmax over bandit_scores
        scores = [s.bandit_score for s in bucket_cells]
        max_score = max(scores) if scores else 0
        exp_scores = [math.exp(s - max_score) for s in scores]  # Subtract max for numerical stability
        total = sum(exp_scores)
        
        if total == 0:
            # Uniform random
            selected = random.choice(bucket_cells)
        else:
            probs = [e / total for e in exp_scores]
            selected_idx = random.choices(range(len(bucket_cells)), weights=probs, k=1)[0]
            selected = bucket_cells[selected_idx]
        
        return selected.cell_key
    
    def get_kill_list(self) -> List[Tuple[CellKey, str]]:
        """Get cells that should be killed (§4 kill rules already applied, 
        but this returns them for reporting)."""
        killed = self.tracker.get_cells_by_status(CellStatus.DISABLED)
        return [(c.key, c.kill_reason) for c in killed]
    
    def get_promotion_watchlist(self) -> List[CellState]:
        """Get cells on the promotion watchlist (active cells approaching
        promotion thresholds)."""
        candidates = []
        for cell in self.tracker.get_all_cells().values():
            if cell.status in (CellStatus.EXPLORING, CellStatus.ACTIVE):
                if (cell.resolved_trades >= 15 and  # Close to 20 threshold
                    cell.ev_per_dollar > 0.05 and
                    cell.profit_factor >= 1.10):
                    candidates.append(cell)
        candidates.sort(key=lambda c: c.ev_per_dollar, reverse=True)
        return candidates
    
    def get_allocation_report(self) -> Dict:
        """Get allocation report for dashboard."""
        scores = self._scores if self._scores else {}
        
        by_bucket = defaultdict(list)
        for score in scores.values():
            by_bucket[score.allocation_bucket].append(score)
        
        return {
            "timestamp": time.time(),
            "total_cells": len(scores),
            "exploitation_cells": len(by_bucket.get("exploitation", [])),
            "promising_cells": len(by_bucket.get("promising", [])),
            "exploration_cells": len(by_bucket.get("exploration", [])),
            "top_5": [
                {
                    "cell": str(s.cell_key),
                    "score": round(s.bandit_score, 4),
                    "posterior_ev": round(s.posterior_ev, 4),
                    "pf": round(s.profit_factor, 2),
                    "samples": s.sample_size,
                    "bucket": s.allocation_bucket,
                }
                for s in sorted(scores.values(), key=lambda s: s.bandit_score, reverse=True)[:5]
            ],
            "kill_list_length": len(self.get_kill_list()),
            "promotion_watchlist_length": len(self.get_promotion_watchlist()),
        }


# Need to import Dict at module level for type hints
from typing import Dict