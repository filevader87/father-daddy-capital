#!/usr/bin/env python3
"""
V20.3 Profit Dashboard — §10
================================
Hourly output of cell-level evidence. No global aggregate optimism.
Only cell-level reality.

Top 20 cells by realized_EV_per_dollar
Bottom 20 cells by realized_EV_per_dollar
Top 20 by PF
Kill list
Promotion watchlist
Capital allocation recommendation
PnL by asset / side / bucket / interval / regime / transition_decile

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from collections import defaultdict

import sys
sys.path.insert(0, '/home/naq1987s/father-daddy-capital')
from src.cell.cell_framework import CellKey, CellState, CellStatus, CellTracker
from src.allocation.cell_bandit import BanditAllocator


@dataclass
class DashboardReport:
    """Hourly profit dashboard report."""
    timestamp: float = 0.0
    
    # Top/bottom cells
    top_20_ev: List[Dict] = field(default_factory=list)
    bottom_20_ev: List[Dict] = field(default_factory=list)
    top_20_pf: List[Dict] = field(default_factory=list)
    
    # Kill and promotion
    kill_list: List[Dict] = field(default_factory=list)
    promotion_watchlist: List[Dict] = field(default_factory=list)
    
    # Allocation recommendation
    allocation: Dict = field(default_factory=dict)
    
    # PnL breakdowns
    pnl_by_asset: Dict[str, Dict] = field(default_factory=dict)
    pnl_by_side: Dict[str, Dict] = field(default_factory=dict)
    pnl_by_bucket: Dict[str, Dict] = field(default_factory=dict)
    pnl_by_interval: Dict[str, Dict] = field(default_factory=dict)
    pnl_by_regime: Dict[str, Dict] = field(default_factory=dict)
    pnl_by_decile: Dict[str, Dict] = field(default_factory=dict)
    
    # Summary
    total_cells: int = 0
    active_cells: int = 0
    killed_cells: int = 0
    total_pnl: float = 0.0
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    
    # Which exact cell has highest realized EV?
    best_cell: str = ""
    best_cell_ev: float = 0.0
    best_cell_evidence: int = 0


class ProfitDashboard:
    """Generate hourly cell-level profit dashboards."""
    
    def __init__(self, tracker: CellTracker, allocator: BanditAllocator):
        self.tracker = tracker
        self.allocator = allocator
    
    def generate_report(self) -> DashboardReport:
        """Generate a full dashboard report."""
        report = DashboardReport(timestamp=time.time())
        
        cells = self.tracker.get_all_cells()
        active = [c for c in cells.values() if c.status != CellStatus.DISABLED]
        killed = [c for c in cells.values() if c.status == CellStatus.DISABLED]
        
        # ── Core counts ──
        report.total_cells = len(cells)
        report.active_cells = len(active)
        report.killed_cells = len(killed)
        report.total_pnl = round(sum(c.total_pnl for c in cells.values()), 4)
        report.total_trades = sum(c.resolved_trades for c in cells.values())
        report.total_wins = sum(c.wins for c in cells.values())
        report.total_losses = sum(c.losses for c in cells.values())
        
        # ── Top 20 by EV/dollar ──
        sorted_ev = sorted(active, key=lambda c: c.ev_per_dollar, reverse=True)
        report.top_20_ev = [
            {
                "cell": str(c.key),
                "ev_per_dollar": round(c.ev_per_dollar, 4),
                "pf": round(c.profit_factor, 2) if c.profit_factor != float('inf') else "inf",
                "wr": round(c.win_rate, 4),
                "trades": c.resolved_trades,
                "pnl": round(c.total_pnl, 4),
            }
            for c in sorted_ev[:20]
        ]
        
        # ── Bottom 20 by EV/dollar ──
        report.bottom_20_ev = [
            {
                "cell": str(c.key),
                "ev_per_dollar": round(c.ev_per_dollar, 4),
                "pf": round(c.profit_factor, 2) if c.profit_factor != float('inf') else "inf",
                "wr": round(c.win_rate, 4),
                "trades": c.resolved_trades,
                "pnl": round(c.total_pnl, 4),
            }
            for c in sorted_ev[-20:]
        ]
        
        # ── Top 20 by PF ──
        sorted_pf = sorted(active, key=lambda c: c.profit_factor if c.profit_factor != float('inf') else 999, reverse=True)
        report.top_20_pf = [
            {
                "cell": str(c.key),
                "pf": round(c.profit_factor, 2) if c.profit_factor != float('inf') else "inf",
                "ev_per_dollar": round(c.ev_per_dollar, 4),
                "wr": round(c.win_rate, 4),
                "trades": c.resolved_trades,
                "pnl": round(c.total_pnl, 4),
            }
            for c in sorted_pf[:20]
        ]
        
        # ── Kill list ──
        report.kill_list = [
            {
                "cell": str(c.key),
                "reason": c.kill_reason,
                "trades": c.resolved_trades,
                "pnl": round(c.total_pnl, 4),
            }
            for c in killed
        ]
        
        # ── Promotion watchlist ──
        promotion = self.allocator.get_promotion_watchlist()
        report.promotion_watchlist = [
            {
                "cell": str(c.key),
                "status": c.status.value,
                "ev_per_dollar": round(c.ev_per_dollar, 4),
                "pf": round(c.profit_factor, 2) if c.profit_factor != float('inf') else "inf",
                "wr": round(c.win_rate, 4),
                "trades": c.resolved_trades,
            }
            for c in promotion
        ]
        
        # ── Allocation recommendation ──
        report.allocation = self.allocator.get_allocation_report()
        
        # ── PnL breakdowns ──
        report.pnl_by_asset = self._pnl_by_dimension(active, lambda c: c.key.asset)
        report.pnl_by_side = self._pnl_by_dimension(active, lambda c: c.key.side)
        report.pnl_by_bucket = self._pnl_by_dimension(active, lambda c: c.key.entry_bucket)
        report.pnl_by_interval = self._pnl_by_dimension(active, lambda c: c.key.interval)
        report.pnl_by_regime = self._pnl_by_dimension(active, lambda c: c.key.regime)
        report.pnl_by_decile = self._pnl_by_dimension(active, lambda c: c.key.transition_decile)
        
        # ── Best cell ──
        if sorted_ev:
            best = sorted_ev[0]
            report.best_cell = str(best.key)
            report.best_cell_ev = round(best.ev_per_dollar, 4)
            report.best_cell_evidence = best.resolved_trades
        
        return report
    
    def _pnl_by_dimension(self, cells: List[CellState], key_fn) -> Dict[str, Dict]:
        """Break down PnL by a cell dimension."""
        by_dim = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "pf": 0.0})
        
        for cell in cells:
            dim = key_fn(cell)
            by_dim[dim]["trades"] += cell.resolved_trades
            by_dim[dim]["wins"] += cell.wins
            by_dim[dim]["losses"] += cell.losses
            by_dim[dim]["pnl"] += cell.total_pnl
            by_dim[dim]["pf"] = round(cell.gross_profit / abs(cell.gross_loss), 2) if cell.gross_loss != 0 else float('inf')
        
        # Aggregate PF for each dimension
        result = {}
        for dim, data in by_dim.items():
            total_gp = sum(c.gross_profit for c in cells if key_fn(c) == dim)
            total_gl = abs(sum(c.gross_loss for c in cells if key_fn(c) == dim))
            agg_pf = round(total_gp / total_gl, 2) if total_gl > 0 else float('inf')
            result[dim] = {
                "trades": data["trades"],
                "wins": data["wins"],
                "losses": data["losses"],
                "pnl": round(data["pnl"], 4),
                "win_rate": round(data["wins"] / data["trades"], 4) if data["trades"] > 0 else 0,
                "pf": agg_pf,
            }
        
        return result
    
    def format_report(self, report: DashboardReport) -> str:
        """Format dashboard report as readable text."""
        lines = []
        lines.append("═" * 70)
        lines.append("V20.3 PROFIT DASHBOARD — CELL-LEVEL EVIDENCE ONLY")
        lines.append("═" * 70)
        lines.append("")
        
        # ── Core question ──
        lines.append("┌── THE QUESTION ──")
        lines.append(f"│ Which exact cell has highest realized EV?")
        lines.append(f"│ → {report.best_cell}")
        lines.append(f"│   EV/dollar: {report.best_cell_ev}")
        lines.append(f"│   Evidence: {report.best_cell_evidence} resolved trades")
        lines.append("└──")
        lines.append("")
        
        # ── Summary ──
        lines.append(f"Total cells: {report.total_cells} | Active: {report.active_cells} | Killed: {report.killed_cells}")
        lines.append(f"Total PnL: ${report.total_pnl:.2f} | Trades: {report.total_trades} | W/L: {report.total_wins}/{report.total_losses}")
        lines.append("")
        
        # ── Top 5 by EV ──
        lines.append("── TOP 5 CELLS BY EV/DOLLAR ──")
        for i, c in enumerate(report.top_20_ev[:5]):
            lines.append(f"  {i+1}. {c['cell']}")
            lines.append(f"     EV=${c['ev_per_dollar']:.4f} PF={c['pf']} WR={c['wr']:.1%} N={c['trades']} PnL=${c['pnl']:.2f}")
        lines.append("")
        
        # ── Bottom 5 by EV ──
        lines.append("── BOTTOM 5 CELLS BY EV/DOLLAR ──")
        for i, c in enumerate(report.bottom_20_ev[:5]):
            lines.append(f"  {i+1}. {c['cell']}")
            lines.append(f"     EV=${c['ev_per_dollar']:.4f} PF={c['pf']} WR={c['wr']:.1%} N={c['trades']} PnL=${c['pnl']:.2f}")
        lines.append("")
        
        # ── Kill list ──
        if report.kill_list:
            lines.append("── KILL LIST ──")
            for k in report.kill_list[:10]:
                lines.append(f"  ✗ {k['cell']}: {k['reason']}")
            lines.append("")
        
        # ── Promotion watchlist ──
        if report.promotion_watchlist:
            lines.append("── PROMOTION WATCHLIST ──")
            for p in report.promotion_watchlist[:10]:
                lines.append(f"  ↑ {p['cell']}: EV=${p['ev_per_dollar']:.4f} PF={p['pf']} N={p['trades']}")
            lines.append("")
        
        # ── PnL by side ──
        lines.append("── PnL BY SIDE ──")
        for side, data in sorted(report.pnl_by_side.items()):
            lines.append(f"  {side}: PnL=${data['pnl']:.2f} WR={data['win_rate']:.1%} PF={data['pf']} N={data['trades']}")
        lines.append("")
        
        # ── PnL by asset ──
        lines.append("── PnL BY ASSET ──")
        for asset, data in sorted(report.pnl_by_asset.items()):
            lines.append(f"  {asset}: PnL=${data['pnl']:.2f} WR={data['win_rate']:.1%} PF={data['pf']} N={data['trades']}")
        lines.append("")
        
        # ── Allocation ──
        alloc = report.allocation
        lines.append("── ALLOCATION ──")
        lines.append(f"  Exploitation: {alloc.get('exploitation_cells', 0)} cells")
        lines.append(f"  Promising:    {alloc.get('promising_cells', 0)} cells")
        lines.append(f"  Exploration:  {alloc.get('exploration_cells', 0)} cells")
        
        lines.append("")
        lines.append("═" * 70)
        return "\n".join(lines)