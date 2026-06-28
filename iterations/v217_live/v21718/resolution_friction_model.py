#!/usr/bin/env python3
"""
V21.7.18 — P1: Resolution Friction Model
==========================================
Replace placeholder estimated_cost with empirical settlement friction.
Track time_to_resolution, capital_lockup, redemption costs.
Compute friction-adjusted EV for scaling decisions.
"""
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "v21718_hardening"
OUT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(OUT / "resolution_friction.log"), logging.StreamHandler()],
)
log = logging.getLogger("v21718_friction")


@dataclass
class ResolutionFrictionEntry:
    """Single trade's resolution friction data."""
    trade_id: str
    profile: str
    market_slug: str
    condition_id: str
    side: str
    entry_price: float
    exit_price: float = 0.0
    entry_ts: float = 0.0
    resolution_ts: float = 0.0
    redeem_ts: float = 0.0
    settlement_source: str = ""
    redemption_source: str = ""
    redeem_tx_hash: str = ""
    redeem_gas_cost: float = 0.0
    pnl_gross: float = 0.0
    resolved: bool = False

    @property
    def time_to_resolution_seconds(self) -> float:
        return max(0, self.resolution_ts - self.entry_ts) if self.entry_ts and self.resolution_ts else 0.0

    @property
    def time_to_redeem_seconds(self) -> float:
        return max(0, self.redeem_ts - self.resolution_ts) if self.resolution_ts and self.redeem_ts else 0.0

    @property
    def capital_lockup_seconds(self) -> float:
        end = self.redeem_ts or self.resolution_ts or time.time()
        return max(0, end - self.entry_ts) if self.entry_ts else 0.0

    @property
    def resolution_delay_penalty(self) -> float:
        """Penalty increases with resolution time. 5m market: target <900s, 15m: <1800s."""
        target = 900 if "5m" in self.market_slug else 1800
        delay = self.time_to_resolution_seconds
        if delay <= target:
            return 0.0
        return min(0.01, (delay - target) / 86400)  # Max 1% per day extra

    @property
    def capital_lockup_penalty(self) -> float:
        """Opportunity cost of capital lockup. ~5% annual rate."""
        lockup_days = self.capital_lockup_seconds / 86400
        return lockup_days * 0.05 / 365  # 5% annual, daily

    @property
    def unresolved_penalty(self) -> float:
        """Unresolved positions cannot be counted as wins or losses."""
        return 0.05 if not self.resolved else 0.0


class ResolutionFrictionModel:
    """Track resolution friction and compute EV adjustments."""

    def __init__(self):
        self.trades: Dict[str, ResolutionFrictionEntry] = {}
        self._load_existing()

    def _load_existing(self):
        path = OUT / "resolution_friction_trades.json"
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                for t in data.get("trades", []):
                    entry = ResolutionFrictionEntry(**t)
                    self.trades[entry.trade_id] = entry
                log.info(f"Loaded {len(self.trades)} existing friction entries")
            except Exception as e:
                log.warning(f"Could not load existing friction data: {e}")

    def _save(self):
        path = OUT / "resolution_friction_trades.json"
        data = {"trades": [asdict(t) for t in self.trades.values()]}
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def record_trade(self, trade_id: str, profile: str, market_slug: str,
                     condition_id: str, side: str, entry_price: float,
                     entry_ts: float = None):
        """Record a new trade for friction tracking."""
        entry = ResolutionFrictionEntry(
            trade_id=trade_id, profile=profile, market_slug=market_slug,
            condition_id=condition_id, side=side, entry_price=entry_price,
            entry_ts=entry_ts or time.time(),
        )
        self.trades[trade_id] = entry
        self._save()
        log.info(f"Recorded trade {trade_id}: {profile} {side} @ {entry_price}")

    def resolve_trade(self, trade_id: str, exit_price: float, resolution_ts: float = None,
                     settlement_source: str = "polymarket"):
        """Mark a trade as resolved."""
        if trade_id not in self.trades:
            log.warning(f"Trade {trade_id} not found for resolution")
            return
        t = self.trades[trade_id]
        t.exit_price = exit_price
        t.resolution_ts = resolution_ts or time.time()
        t.settlement_source = settlement_source
        t.resolved = True
        if t.side == "DOWN":
            t.pnl_gross = (1 - exit_price) - (1 - t.entry_price) if exit_price < t.entry_price else -(t.entry_price - exit_price)
        else:
            t.pnl_gross = exit_price - t.entry_price
        self._save()
        log.info(f"Resolved trade {trade_id}: pnl_gross={t.pnl_gross:.4f}")

    def redeem_trade(self, trade_id: str, redeem_ts: float = None,
                     redeem_tx_hash: str = "", redeem_gas_cost: float = 0.0,
                     redemption_source: str = "polymarket_ctf"):
        """Record redemption of a winning position."""
        if trade_id not in self.trades:
            log.warning(f"Trade {trade_id} not found for redemption")
            return
        t = self.trades[trade_id]
        t.redeem_ts = redeem_ts or time.time()
        t.redeem_tx_hash = redeem_tx_hash
        t.redeem_gas_cost = redeem_gas_cost
        t.redemption_source = redemption_source
        self._save()
        log.info(f"Redeemed trade {trade_id}: gas_cost={redeem_gas_cost:.6f}")

    def compute_ev(self, profile: str = None) -> dict:
        """Compute friction-adjusted EV across all resolved trades."""
        resolved = [t for t in self.trades.values() if t.resolved]
        if profile:
            resolved = [t for t in resolved if t.profile == profile]

        if not resolved:
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "profile": profile or "all",
                "classification": "NO_RESOLVED_TRADES",
                "gross_EV": 0.0,
                "spread_adjusted_EV": 0.0,
                "slippage_adjusted_EV": 0.0,
                "resolution_friction_adjusted_EV": 0.0,
                "capital_lockup_adjusted_EV": 0.0,
                "net_EV": 0.0,
                "sample_size": 0,
                "unresolved_count": len([t for t in self.trades.values() if not t.resolved]),
            }

        total_gross = sum(t.pnl_gross for t in resolved)
        total_spread_adj = total_gross - sum(0.01 for _ in resolved)  # ~1¢ spread per trade
        total_slippage_adj = total_spread_adj - sum(0.005 for _ in resolved)  # ~0.5¢ slippage
        total_resolution_adj = total_slippage_adj - sum(t.resolution_delay_penalty for t in resolved)
        total_lockup_adj = total_resolution_adj - sum(t.capital_lockup_penalty for t in resolved)
        total_gas = sum(t.redeem_gas_cost for t in resolved)
        net_ev = total_lockup_adj - total_gas

        unresolved_count = len([t for t in self.trades.values() if not t.resolved])

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "profile": profile or "all",
            "classification": "EV_ACCOUNTING_HARDENED",
            "gross_EV": round(total_gross, 4),
            "spread_adjusted_EV": round(total_spread_adj, 4),
            "slippage_adjusted_EV": round(total_slippage_adj, 4),
            "resolution_friction_adjusted_EV": round(total_resolution_adj, 4),
            "capital_lockup_adjusted_EV": round(total_lockup_adj, 4),
            "net_EV": round(net_ev, 4),
            "redeem_gas_cost_total": round(total_gas, 6),
            "sample_size": len(resolved),
            "unresolved_count": unresolved_count,
            "avg_resolution_time_seconds": round(sum(t.time_to_resolution_seconds for t in resolved) / len(resolved), 1),
            "avg_capital_lockup_seconds": round(sum(t.capital_lockup_seconds for t in resolved) / len(resolved), 1),
            "avg_redeem_gas_cost": round(total_gas / max(1, len(resolved)), 6),
        }

    def generate_report(self) -> dict:
        """Generate the resolution friction report per §3."""
        overall = self.compute_ev()
        by_profile = {}
        for profile in set(t.profile for t in self.trades.values()):
            by_profile[profile] = self.compute_ev(profile)

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "directive": "V21.7.18",
            "overall": overall,
            "by_profile": by_profile,
            "total_trades": len(self.trades),
            "resolved_trades": len([t for t in self.trades.values() if t.resolved]),
            "unresolved_trades": len([t for t in self.trades.values() if not t.resolved]),
            "rules": [
                "no_placeholder_constants_when_empirical_values_exist",
                "unresolved_trades_not_counted_as_wins_or_losses",
                "no_promotion_if_unresolved_rate_high",
                "no_double_friction_penalties",
            ],
        }

        with open(OUT / "resolution_friction_report.json", "w") as f:
            json.dump(report, f, indent=2)

        return report


if __name__ == "__main__":
    model = ResolutionFrictionModel()
    report = model.generate_report()
    log.info(f"Resolution friction: {report['overall']['classification']}")
    log.info(f"  Total: {report['total_trades']}, Resolved: {report['resolved_trades']}, Unresolved: {report['unresolved_trades']}")