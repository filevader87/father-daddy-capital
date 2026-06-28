#!/usr/bin/env python3
"""
V20.3.1 §§10-11 — No Scaling Directive + System Identity + Required Outputs
==============================================================================
§10: NO scaling until validation conditions met (500+ trades, positive EV, etc.)
§11: System is now an adaptive quantitative market-selection organism, not a
     single strategy. Edge comes from evolutionary pressure, not static thesis.

Required output files:
  - top_cells.json
  - dying_cells.json
  - adversarial_market_report.json
  - regime_entropy_report.json
  - directional_efficiency_matrix.csv
  - cross_asset_correlation_report.json
  - cell_half_life_dashboard.json
  - exploration_pressure_log.json

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
import json
import csv
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import sys
sys.path.insert(0, '/home/naq1987s/father-daddy-capital')
from src.cell.cell_framework import CellKey, CellTracker, CellStatus


# ── §10: No Scaling Guard ──
# LIVE BLOCKED. No scaling permitted until ALL conditions met.

MINIMUM_VALIDATION_TRADES = 500
MINIMUM_REALIZED_EV = 0.0      # Positive realized expectancy
MINIMUM_PF = 1.25              # Profit factor after friction
MINIMUM_REGIME_ENTROPY = 0.5   # Non-degenerate regime classifier
MINIMUM_SETTLEMENT_ACCURACY = 1.0  # Zero errors
MINIMUM_CROSS_ASSET_VALIDATION = True
MINIMUM_MULTI_REGIME_VALIDATION = True
MINIMUM_BINARY_SETTLEMENT = True
MINIMUM_FRICTION_SURVIVAL = True


@dataclass
class ScalingGate:
    """Hard gate on any scaling until ALL conditions validated.
    
    The system SHALL NOT:
      - Increase leverage
      - Increase concurrency
      - Expand Kelly
      - Scale bankroll
      - Increase live size
      - Deploy multi-position live concentration
    
    Until ALL conditions are met.
    """
    resolved_trades: int = 0
    realized_ev_per_dollar: float = -1.0
    profit_factor: float = 0.0
    regime_entropy_bits: float = 0.0
    settlement_errors: int = 1
    accounting_errors: int = 1
    cross_asset_validated: bool = False
    multi_regime_validated: bool = False
    binary_settlement_validated: bool = False
    friction_survival_validated: bool = False
    
    @property
    def scaling_permitted(self) -> bool:
        """All conditions must be True for scaling."""
        return (
            self.resolved_trades >= MINIMUM_VALIDATION_TRADES and
            self.realized_ev_per_dollar > MINIMUM_REALIZED_EV and
            self.profit_factor >= MINIMUM_PF and
            self.regime_entropy_bits >= MINIMUM_REGIME_ENTROPY and
            self.settlement_errors == 0 and
            self.accounting_errors == 0 and
            self.cross_asset_validated and
            self.multi_regime_validated and
            self.binary_settlement_validated and
            self.friction_survival_validated
        )
    
    @property
    def scaling_blockers(self) -> List[str]:
        """List all blocking conditions."""
        blockers = []
        if self.resolved_trades < MINIMUM_VALIDATION_TRADES:
            blockers.append(f"trades={self.resolved_trades} < {MINIMUM_VALIDATION_TRADES}")
        if self.realized_ev_per_dollar <= MINIMUM_REALIZED_EV:
            blockers.append(f"EV={self.realized_ev_per_dollar:.4f} <= 0")
        if self.profit_factor < MINIMUM_PF:
            blockers.append(f"PF={self.profit_factor:.2f} < {MINIMUM_PF}")
        if self.regime_entropy_bits < MINIMUM_REGIME_ENTROPY:
            blockers.append(f"entropy={self.regime_entropy_bits:.2f} < {MINIMUM_REGIME_ENTROPY}")
        if self.settlement_errors > 0:
            blockers.append(f"settlement_errors={self.settlement_errors}")
        if self.accounting_errors > 0:
            blockers.append(f"accounting_errors={self.accounting_errors}")
        if not self.cross_asset_validated:
            blockers.append("cross_asset_validation=False")
        if not self.multi_regime_validated:
            blockers.append("multi_regime_validation=False")
        if not self.binary_settlement_validated:
            blockers.append("binary_settlement_validation=False")
        if not self.friction_survival_validated:
            blockers.append("friction_survival=False")
        return blockers
    
    def enforce_no_scaling(self) -> None:
        """Hard enforcement. Raises if any scaling is attempted."""
        if not self.scaling_permitted:
            blockers = "; ".join(self.scaling_blockers)
            raise RuntimeError(
                f"SCALING_BLOCKED: {blockers}. "
                f"No leverage, concurrency, Kelly, bankroll, or live-size increase permitted."
            )


# ── §11: System Identity ──
# The engine is no longer a single trading strategy.
# It is an adaptive quantitative market-selection organism.
# Edge comes from: evolutionary pressure, adversarial filtering,
# empirical survival, dynamic capital concentration, localized persistence.

SYSTEM_IDENTITY = "adaptive_quantitative_market_selection_organism"
SYSTEM_VERSION = "V20.3.1"

LIVE_ENABLED = False
PAPER_ONLY = True

# No live probe authorized under V20.3.1
LIVE_PROMOTION_REQUIREMENTS = {
    "stable_positive_realized_expectancy": True,
    "post_friction_profitability": True,
    "regime_diversity": True,
    "non_degenerate_transition": True,
    "adversarial_robustness": True,
}


# ── Required Output File Generators ──

OUTPUT_DIR = "/home/naq1987s/father-daddy-capital/output/v2031"


def _ensure_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def write_top_cells(tracker: CellTracker, top_n: int = 20) -> str:
    """Write top_cells.json — top N cells by realized EV."""
    _ensure_output_dir()
    cells = tracker.get_all_cells()
    active = [(str(k), c) for k, c in cells.items()
              if c.status != CellStatus.DISABLED]
    active.sort(key=lambda x: x[1].ev_per_dollar, reverse=True)
    top = [
        {
            "cell": k,
            "ev_per_dollar": round(c.ev_per_dollar, 6),
            "profit_factor": round(c.profit_factor if c.profit_factor != float('inf') else 10.0, 4),
            "win_rate": round(c.win_rate, 4),
            "resolved_trades": c.resolved_trades,
            "status": c.status.value,
        }
        for k, c in active[:top_n]
    ]
    path = os.path.join(OUTPUT_DIR, "top_cells.json")
    with open(path, 'w') as f:
        json.dump({"timestamp": time.time(), "top_cells": top}, f, indent=2)
    return path


def write_dying_cells(tracker: CellTracker) -> str:
    """Write dying_cells.json — cells with EV < -0.05 or decaying."""
    _ensure_output_dir()
    cells = tracker.get_all_cells()
    dying = [
        {
            "cell": str(k),
            "ev_per_dollar": round(c.ev_per_dollar, 6),
            "profit_factor": round(c.profit_factor if c.profit_factor != float('inf') else 10.0, 4),
            "resolved_trades": c.resolved_trades,
            "status": c.status.value,
            "reason": "negative_ev" if c.ev_per_dollar < -0.05 else "deteriorating",
        }
        for k, c in cells.items()
        if c.status != CellStatus.DISABLED and c.ev_per_dollar < -0.05
    ]
    path = os.path.join(OUTPUT_DIR, "dying_cells.json")
    with open(path, 'w') as f:
        json.dump({"timestamp": time.time(), "dying_cells": dying}, f, indent=2)
    return path


def write_adversarial_report(report: dict) -> str:
    """Write adversarial_market_report.json."""
    _ensure_output_dir()
    path = os.path.join(OUTPUT_DIR, "adversarial_market_report.json")
    with open(path, 'w') as f:
        json.dump(report, f, indent=2)
    return path


def write_regime_entropy_report(report: dict) -> str:
    """Write regime_entropy_report.json."""
    _ensure_output_dir()
    path = os.path.join(OUTPUT_DIR, "regime_entropy_report.json")
    with open(path, 'w') as f:
        json.dump(report, f, indent=2)
    return path


def write_directional_efficiency_csv(csv_content: str) -> str:
    """Write directional_efficiency_matrix.csv."""
    _ensure_output_dir()
    path = os.path.join(OUTPUT_DIR, "directional_efficiency_matrix.csv")
    with open(path, 'w') as f:
        f.write(csv_content)
    return path


def write_cross_asset_report(report: dict) -> str:
    """Write cross_asset_correlation_report.json."""
    _ensure_output_dir()
    path = os.path.join(OUTPUT_DIR, "cross_asset_correlation_report.json")
    with open(path, 'w') as f:
        json.dump(report, f, indent=2)
    return path


def write_half_life_dashboard(report: dict) -> str:
    """Write cell_half_life_dashboard.json."""
    _ensure_output_dir()
    path = os.path.join(OUTPUT_DIR, "cell_half_life_dashboard.json")
    with open(path, 'w') as f:
        json.dump(report, f, indent=2)
    return path


def write_exploration_pressure_log(pressure: dict) -> str:
    """Write exploration_pressure_log.json."""
    _ensure_output_dir()
    path = os.path.join(OUTPUT_DIR, "exploration_pressure_log.json")
    # Append mode for time-series
    existing = []
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            existing = []
    existing.append({"timestamp": time.time(), **pressure})
    with open(path, 'w') as f:
        json.dump(existing, f, indent=2)
    return path


def write_scaling_gate_status(gate: ScalingGate) -> str:
    """Write scaling_gate_status.json."""
    _ensure_output_dir()
    path = os.path.join(OUTPUT_DIR, "scaling_gate_status.json")
    status = {
        "timestamp": time.time(),
        "scaling_permitted": gate.scaling_permitted,
        "blockers": gate.scaling_blockers,
        "resolved_trades": gate.resolved_trades,
        "realized_ev_per_dollar": gate.realized_ev_per_dollar,
        "profit_factor": gate.profit_factor,
        "regime_entropy_bits": gate.regime_entropy_bits,
        "settlement_errors": gate.settlement_errors,
        "accounting_errors": gate.accounting_errors,
        "cross_asset_validated": gate.cross_asset_validated,
        "multi_regime_validated": gate.multi_regime_validated,
        "binary_settlement_validated": gate.binary_settlement_validated,
        "friction_survival_validated": gate.friction_survival_validated,
        "system_identity": SYSTEM_IDENTITY,
        "system_version": SYSTEM_VERSION,
        "live_enabled": LIVE_ENABLED,
        "paper_only": PAPER_ONLY,
    }
    with open(path, 'w') as f:
        json.dump(status, f, indent=2)
    return path