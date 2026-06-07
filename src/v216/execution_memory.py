#!/usr/bin/env python3
"""
V21.6 Execution Memory (§10)
==============================
Remembers which entries slipped, which buckets failed,
which regimes widened spreads, etc.

This memory becomes: execution_instinct
The system evolves away from hostile liquidity zones
toward repeatable executable extraction.
"""

import json
import numpy as np
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict


@dataclass
class ExecutionTrace:
    """Single execution trace stored in memory."""
    trade_id: int
    asset: str
    interval: str
    side: str
    bucket_zone: str
    timing: str
    regime: str
    execution_mode: str  # maker/taker/hybrid/abort
    entry_price: float
    actual_entry: float
    slippage_bps: float
    fill_pct: float
    fill_latency_ms: float
    settlement: float  # 0.0 or 1.0
    won: bool
    pnl: float
    realized_ev: float
    net_convexity: float
    friction_score: float
    liquidity_score: float
    hostility_score: float
    spread_bps: float
    spread_trend: str
    rejected: bool = False
    stale: bool = False


class ExecutionMemory:
    """
    §10: Persists execution traces and evolves execution instinct.
    Tracks which paths work, which decay, and which are hostile.
    """

    def __init__(self, memory_path: str = None):
        self.memory_path = Path(memory_path) if memory_path else None
        self.traces: List[ExecutionTrace] = []
        self.cell_memory: Dict[tuple, dict] = defaultdict(lambda: {
            'attempts': 0,
            'fills': 0,
            'wins': 0,
            'pnl': 0.0,
            'total_slippage_bps': 0.0,
            'total_fill_latency_ms': 0.0,
            'rejections': 0,
            'stales': 0,
            'avg_slippage': 0.0,
            'avg_latency': 0.0,
            'win_rate': 0.0,
            'realized_ev': 0.0,
            'hostility_incidents': 0,
        })

        # Instinct scores (evolved from experience)
        self.bucket_instinct: Dict[str, float] = {}
        self.timing_instinct: Dict[str, float] = {}
        self.regime_instinct: Dict[str, float] = {}
        self.mode_instinct: Dict[str, float] = {}
        self.asset_instinct: Dict[str, float] = {}

        # Failed paths (avoid these)
        self.failed_paths: Dict[tuple, int] = defaultdict(int)  # path → consecutive failures
        self.hostile_zones: Dict[tuple, dict] = {}  # zone → hostility data

        self._load()

    def record(self, trace: ExecutionTrace):
        """Record an execution trace."""
        self.traces.append(trace)

        # Update cell memory
        cell_key = (trace.asset, trace.interval, trace.side,
                    trace.bucket_zone, trace.timing, trace.regime)
        cell = self.cell_memory[cell_key]
        cell['attempts'] += 1

        if trace.rejected or trace.stale:
            cell['rejections'] += (1 if trace.rejected else 0)
            cell['stales'] += (1 if trace.stale else 0)
            self.failed_paths[cell_key] = self.failed_paths.get(cell_key, 0) + 1
            return

        # Successful fill
        self.failed_paths[cell_key] = 0  # Reset consecutive failures
        cell['fills'] += 1
        cell['total_slippage_bps'] += trace.slippage_bps
        cell['total_fill_latency_ms'] += trace.fill_latency_ms
        cell['pnl'] += trace.pnl

        if trace.won:
            cell['wins'] += 1

        # Update averages
        n = cell['fills']
        cell['avg_slippage'] = cell['total_slippage_bps'] / n
        cell['avg_latency'] = cell['total_fill_latency_ms'] / n
        cell['win_rate'] = cell['wins'] / n
        cell['realized_ev'] = cell['pnl'] / n

        # Track hostility
        if trace.hostility_score > 0.6:
            cell['hostility_incidents'] += 1

        # Evolve instincts
        self._evolve_instincts()

    def _evolve_instincts(self):
        """
        §10: Evolve execution instinct from accumulated memory.
        Moves AWAY from hostile zones, TOWARD repeatable extraction.
        """
        # Bucket instinct: higher EV → higher instinct score
        bucket_pnl = defaultdict(list)
        timing_pnl = defaultdict(list)
        regime_pnl = defaultdict(list)
        mode_pnl = defaultdict(list)
        asset_pnl = defaultdict(list)

        for key, cell in self.cell_memory.items():
            asset, interval, side, bucket, timing, regime = key
            ev = cell['realized_ev']
            if cell['fills'] < 3:
                continue

            bucket_pnl[bucket].append(ev)
            timing_pnl[timing].append(ev)
            regime_pnl[regime].append(ev)

            if cell.get('execution_mode_count'):
                mode_pnl[cell.get('best_mode', 'hybrid')].append(ev)
            asset_pnl[(asset, interval)].append(ev)

        # Instinct = tanh(normalized positive EV)
        for bucket, evs in bucket_pnl.items():
            avg_ev = np.mean(evs)
            self.bucket_instinct[bucket] = float(np.tanh(max(0, avg_ev * 5)))

        for timing, evs in timing_pnl.items():
            avg_ev = np.mean(evs)
            self.timing_instinct[timing] = float(np.tanh(max(0, avg_ev * 5)))

        for regime, evs in regime_pnl.items():
            avg_ev = np.mean(evs)
            self.regime_instinct[regime] = float(np.tanh(max(0, avg_ev * 5)))

        for mode, evs in mode_pnl.items():
            avg_ev = np.mean(evs)
            self.mode_instinct[mode] = float(np.tanh(max(0, avg_ev * 5)))

        for (asset, interval), evs in asset_pnl.items():
            avg_ev = np.mean(evs)
            self.asset_instinct[f"{asset}_{interval}"] = float(np.tanh(max(0, avg_ev * 5)))

        # Mark hostile zones
        for key, cell in self.cell_memory.items():
            if cell['fills'] >= 5 and cell['realized_ev'] < -0.10:
                hostility_rate = cell['hostility_incidents'] / max(cell['attempts'], 1)
                self.hostile_zones[key] = {
                    'ev': cell['realized_ev'],
                    'win_rate': cell['win_rate'],
                    'hostility_rate': hostility_rate,
                    'slippage': cell['avg_slippage'],
                    'attempts': cell['attempts'],
                }

    def get_instinct_score(self, bucket: str = None, timing: str = None,
                           regime: str = None, asset: str = None) -> float:
        """
        Combined instinct score for a potential execution path.
        Higher = more likely to be executable.
        """
        scores = []

        if bucket and bucket in self.bucket_instinct:
            scores.append(self.bucket_instinct[bucket])
        if timing and timing in self.timing_instinct:
            scores.append(self.timing_instinct[timing])
        if regime and regime in self.regime_instinct:
            scores.append(self.regime_instinct[regime])

        if not scores:
            return 0.5  # Unknown territory

        return float(np.mean(scores))

    def should_avoid(self, cell_key: tuple) -> Tuple[bool, str]:
        """
        Check if a cell is in a hostile zone that should be avoided.
        Returns (should_avoid, reason).
        """
        if cell_key in self.hostile_zones:
            zone = self.hostile_zones[cell_key]
            if zone['ev'] < -0.20:
                return True, f"hostile_ev={zone['ev']:.3f}"
            if zone['hostility_rate'] > 0.5:
                return True, f"hostility_rate={zone['hostility_rate']:.1%}"
            if zone['slippage'] > 3000:
                return True, f"slippage={zone['slippage']:.0f}bps"

        # Check consecutive failures
        failures = self.failed_paths.get(cell_key, 0)
        if failures >= 5:
            return True, f"consecutive_failures={failures}"

        return False, "pass"

    def get_failed_paths(self, min_failures: int = 3) -> List[tuple]:
        """Return cells with consecutive failures above threshold."""
        return [k for k, v in self.failed_paths.items() if v >= min_failures]

    def get_best_cells(self, n: int = 10) -> List[tuple]:
        """Return top-N cells by realized EV."""
        cells_with_data = [
            (key, cell) for key, cell in self.cell_memory.items()
            if cell['fills'] >= 5
        ]
        ranked = sorted(cells_with_data, key=lambda x: x[1]['realized_ev'], reverse=True)
        return [(key, cell['realized_ev'], cell['win_rate'], cell['fills'])
                for key, _, cell in ranked[:n]]

    def summary(self) -> dict:
        """Return summary of execution memory state."""
        total_attempts = sum(c['attempts'] for c in self.cell_memory.values())
        total_fills = sum(c['fills'] for c in self.cell_memory.values())
        total_wins = sum(c['wins'] for c in self.cell_memory.values())
        total_pnl = sum(c['pnl'] for c in self.cell_memory.values())

        return {
            'total_traces': len(self.traces),
            'total_cell_attempts': total_attempts,
            'total_fills': total_fills,
            'total_wins': total_wins,
            'total_pnl': total_pnl,
            'hostile_zones': len(self.hostile_zones),
            'failed_paths': len(self.get_failed_paths()),
            'fill_rate': total_fills / max(total_attempts, 1),
            'overall_wr': total_wins / max(total_fills, 1),
            'overall_ev': total_pnl / max(total_fills, 1),
            'bucket_instinct': dict(sorted(
                self.bucket_instinct.items(),
                key=lambda x: x[1], reverse=True
            )),
            'timing_instinct': dict(sorted(
                self.timing_instinct.items(),
                key=lambda x: x[1], reverse=True
            )),
        }

    def _load(self):
        """Load memory from disk if available."""
        if self.memory_path and self.memory_path.exists():
            try:
                data = json.loads(self.memory_path.read_text())
                # Reconstruct from saved data
                for key_str, cell_data in data.get('cell_memory', {}).items():
                    key = tuple(key_str.split('|'))
                    self.cell_memory[key] = cell_data
                self.bucket_instinct = data.get('bucket_instinct', {})
                self.timing_instinct = data.get('timing_instinct', {})
            except (json.JSONDecodeError, KeyError):
                pass

    def save(self):
        """Persist memory to disk."""
        if not self.memory_path:
            return

        # Serialize cell_memory
        serialized_cells = {}
        for key, cell in self.cell_memory.items():
            key_str = '|'.join(str(k) for k in key)
            serialized_cells[key_str] = cell

        data = {
            'cell_memory': serialized_cells,
            'bucket_instinct': self.bucket_instinct,
            'timing_instinct': self.timing_instinct,
            'regime_instinct': self.regime_instinct,
            'mode_instinct': self.mode_instinct,
            'asset_instinct': self.asset_instinct,
        }

        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory_path.write_text(json.dumps(data, indent=2, default=str))