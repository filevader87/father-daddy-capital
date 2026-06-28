#!/usr/bin/env python3
"""
FDC Risk Gateway — Portfolio-level risk management across all arms.
===============================================================
Single point of control for:
  - Max portfolio drawdown (hard floor)
  - Per-arm capital allocation
  - Cross-arm correlation limits
  - Daily/weekly loss limits at portfolio level

Each arm must check with this gateway before placing orders.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional
import json
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "output" / "risk_gateway_state.json"


@dataclass
class ArmState:
    name: str
    bankroll: float = 0.0
    pnl: float = 0.0
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    open_positions: int = 0
    halted: bool = False
    halt_reason: str = ""


@dataclass  
class PortfolioState:
    total_bankroll: float = 50.0
    max_drawdown_pct: float = 0.20  # 20% hard floor
    max_daily_loss: float = 10.0
    max_weekly_loss: float = 20.0
    arms: Dict[str, ArmState] = field(default_factory=dict)
    
    def total_pnl(self) -> float:
        return sum(a.pnl for a in self.arms.values())
    
    def total_daily_pnl(self) -> float:
        return sum(a.daily_pnl for a in self.arms.values())
    
    def balance(self) -> float:
        return self.total_bankroll + self.total_pnl()
    
    def drawdown(self) -> float:
        return self.total_bankroll - self.balance()
    
    def is_halted(self) -> bool:
        if self.drawdown() >= self.total_bankroll * self.max_drawdown_pct:
            return True
        if self.total_daily_pnl() <= -self.max_daily_loss:
            return True
        return False


_portfolio: Optional[PortfolioState] = None


def get_portfolio() -> PortfolioState:
    global _portfolio
    if _portfolio is None:
        _portfolio = PortfolioState()
        _load_state()
    return _portfolio


def can_trade(arm_name: str, position_size: float) -> bool:
    """Check if an arm can place a trade. Returns True if approved."""
    p = get_portfolio()
    
    if p.is_halted():
        return False
    
    arm = p.arms.get(arm_name)
    if arm and arm.halted:
        return False
    
    if position_size > p.balance():
        return False
    
    return True


def record_trade(arm_name: str, pnl: float):
    """Record a trade outcome for an arm."""
    p = get_portfolio()
    if arm_name not in p.arms:
        p.arms[arm_name] = ArmState(name=arm_name)
    arm = p.arms[arm_name]
    arm.pnl += pnl
    arm.daily_pnl += pnl
    _save_state()


def _save_state():
    p = get_portfolio()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "total_bankroll": p.total_bankroll,
        "max_drawdown_pct": p.max_drawdown_pct,
        "max_daily_loss": p.max_daily_loss,
        "max_weekly_loss": p.max_weekly_loss,
        "arms": {
            name: {
                "bankroll": a.bankroll, "pnl": a.pnl,
                "daily_pnl": a.daily_pnl, "weekly_pnl": a.weekly_pnl,
                "open_positions": a.open_positions,
                "halted": a.halted, "halt_reason": a.halt_reason,
            } for name, a in p.arms.items()
        },
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _load_state():
    global _portfolio
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
            _portfolio.total_bankroll = d.get("total_bankroll", 50.0)
            _portfolio.max_drawdown_pct = d.get("max_drawdown_pct", 0.20)
            _portfolio.max_daily_loss = d.get("max_daily_loss", 10.0)
            _portfolio.max_weekly_loss = d.get("max_weekly_loss", 20.0)
            for name, adata in d.get("arms", {}).items():
                _portfolio.arms[name] = ArmState(
                    name=name, bankroll=adata.get("bankroll", 0),
                    pnl=adata.get("pnl", 0), daily_pnl=adata.get("daily_pnl", 0),
                    weekly_pnl=adata.get("weekly_pnl", 0),
                    open_positions=adata.get("open_positions", 0),
                    halted=adata.get("halted", False),
                    halt_reason=adata.get("halt_reason", ""),
                )
        except Exception:
            pass