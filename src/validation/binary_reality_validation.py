#!/usr/bin/env python3
"""
V20.3 Binary Reality Validation — Section 11
================================================
Framework to validate the rebuilt paper engine matches reality.

Required outputs:
  - resolved_trades
  - binary_win_loss
  - realized_settlement_pnl
  - spread_distribution
  - imbalance_distribution
  - transition_score_distribution
  - regime_distribution
  - PnL by side
  - PnL by bucket
  - PnL by regime
  - PnL by transition decile
  - DOWN diagnostic results

Pass criteria:
  - all settlements binary (0 or 1)
  - no midpoint settlement
  - spread field valid
  - imbalance non-fake (not always 0.0)
  - transition not clamped (>50% at ±1.0 = fail)
  - regime entropy > 0
  - settlement_errors = 0
  - accounting_errors = 0

Minimum: 50 resolved binary paper trades with positive realized EV
and profit factor >= 1.25.

Live remains BLOCKED until this passes.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from collections import Counter, defaultdict
import statistics


# ── Pass Criteria ──
MINIMUM_RESOLVED_TRADES = 50
MINIMUM_EV_POSITIVE = True
MINIMUM_PROFIT_FACTOR = 1.25
MAX_SETTLEMENT_ERRORS = 0
MAX_ACCOUNTING_ERRORS = 0
MAX_CLAMPED_RATIO = 0.50  # >50% at ±1.0 = fail
MINIMUM_REGIME_ENTROPY = 0.5  # bits


@dataclass
class ValidationTradeLog:
    """A single trade log for validation."""
    trade_id: str = ""
    slug: str = ""
    side: str = ""                    # "UP" or "DOWN"
    entry_price: float = 0.0
    size_usd: float = 2.0
    shares: float = 0.0
    settlement_value: float = -1.0    # Must be 0.0 or 1.0
    resolved_winner: str = ""          # "UP" or "DOWN"
    win_loss: str = ""                 # "WIN" or "LOSS"
    realized_settlement_pnl: float = 0.0
    unrealized_mark_pnl: float = 0.0
    
    # Microstructure at entry
    spread: float = 0.0
    imbalance: float = 0.0
    transition_score: float = 0.0
    raw_transition_score: float = 0.0
    regime: str = ""
    
    # Diagnostics
    settlement_error: bool = False
    accounting_error: bool = False


class V203BinaryRealityValidation:
    """V20.3 Binary Reality Validation framework.
    
    Collects trade logs and computes all required distributions
    and pass/fail criteria.
    """
    
    def __init__(self):
        self._trades: List[ValidationTradeLog] = []
        self._settlement_errors: int = 0
        self._accounting_errors: int = 0
    
    def log_trade(self, trade: ValidationTradeLog):
        """Log a trade for validation."""
        # Check settlement validity
        if trade.settlement_value not in (0.0, 1.0):
            trade.settlement_error = True
            self._settlement_errors += 1
        
        # Check accounting: PnL must be shares * settlement_value - size_usd
        if trade.shares > 0 and trade.settlement_value in (0.0, 1.0):
            expected_pnl = (trade.shares * trade.settlement_value) - trade.size_usd
            if abs(trade.realized_settlement_pnl - expected_pnl) > 0.01:
                trade.accounting_error = True
                self._accounting_errors += 1
        
        # Check for midpoint settlement (0.50)
        if trade.settlement_value == 0.50:
            trade.settlement_error = True
            self._settlement_errors += 1
        
        self._trades.append(trade)
    
    def validate(self) -> dict:
        """Run full validation and return pass/fail report."""
        if not self._trades:
            return {"status": "NO_DATA", "error": "No trades to validate"}
        
        resolved = [t for t in self._trades if t.settlement_value in (0.0, 1.0)]
        wins = [t for t in resolved if t.win_loss == "WIN"]
        losses = [t for t in resolved if t.win_loss == "LOSS"]
        
        # ── Settlement checks ──
        all_binary = all(t.settlement_value in (0.0, 1.0) for t in resolved)
        no_midpoint = not any(t.settlement_value == 0.50 for t in self._trades)
        
        # ── Spread checks ──
        spreads = [t.spread for t in resolved if t.spread > 0]
        spread_valid = len(spreads) > 0 and not all(s == 0.98 for s in spreads)
        spread_distribution = {
            "count": len(spreads),
            "mean": round(statistics.mean(spreads), 4) if spreads else 0,
            "median": round(statistics.median(spreads), 4) if spreads else 0,
            "min": round(min(spreads), 4) if spreads else 0,
            "max": round(max(spreads), 4) if spreads else 0,
            "stdev": round(statistics.stdev(spreads), 4) if len(spreads) > 1 else 0,
            "percentile_05": round(sorted(spreads)[int(len(spreads)*0.05)], 4) if len(spreads) > 20 else 0,
            "percentile_95": round(sorted(spreads)[int(len(spreads)*0.95)], 4) if len(spreads) > 20 else 0,
        } if spreads else {"count": 0, "mean": 0, "median": 0, "min": 0, "max": 0, "stdev": 0}
        
        # ── Imbalance checks ──
        imbalances = [t.imbalance for t in resolved if t.imbalance is not None]
        all_zero_imbalance = all(i == 0.0 for i in imbalances) if imbalances else True
        imbalance_distribution = {
            "count": len(imbalances),
            "mean": round(statistics.mean(imbalances), 4) if imbalances else 0,
            "median": round(statistics.median(imbalances), 4) if imbalances else 0,
            "min": round(min(imbalances), 4) if imbalances else 0,
            "max": round(max(imbalances), 4) if imbalances else 0,
            "stdev": round(statistics.stdev(imbalances), 4) if len(imbalances) > 1 else 0,
            "all_zero": all_zero_imbalance,
        }
        
        # ── Transition score checks ──
        transition_scores = [t.transition_score for t in resolved]
        extreme_ratio = sum(1 for s in transition_scores if abs(s) > 0.99) / len(transition_scores) if transition_scores else 0
        transition_distribution = {
            "count": len(transition_scores),
            "mean": round(statistics.mean(transition_scores), 4) if transition_scores else 0,
            "median": round(statistics.median(transition_scores), 4) if transition_scores else 0,
            "min": round(min(transition_scores), 4) if transition_scores else 0,
            "max": round(max(transition_scores), 4) if transition_scores else 0,
            "stdev": round(statistics.stdev(transition_scores), 4) if len(transition_scores) > 1 else 0,
            "extreme_ratio": round(extreme_ratio, 4),
            "clamped": extreme_ratio > MAX_CLAMPED_RATIO,
        }
        
        # ── Regime distribution ──
        regimes = [t.regime for t in resolved if t.regime]
        regime_counts = Counter(regimes)
        regime_distribution = {
            "counts": dict(regime_counts),
            "entropy_bits": self._entropy(regime_counts) if regime_counts else 0,
            "dominant_regime": regime_counts.most_common(1)[0][0] if regime_counts else None,
            "dominant_ratio": regime_counts.most_common(1)[0][1] / len(regimes) if regimes else 0,
        }
        
        # ── PnL by side ──
        up_trades = [t for t in resolved if t.side == "UP"]
        down_trades = [t for t in resolved if t.side == "DOWN"]
        pnl_by_side = {
            "UP": {
                "count": len(up_trades),
                "wins": len([t for t in up_trades if t.win_loss == "WIN"]),
                "win_rate": len([t for t in up_trades if t.win_loss == "WIN"]) / len(up_trades) if up_trades else 0,
                "total_pnl": round(sum(t.realized_settlement_pnl for t in up_trades), 4),
                "avg_pnl": round(statistics.mean([t.realized_settlement_pnl for t in up_trades]), 4) if up_trades else 0,
            },
            "DOWN": {
                "count": len(down_trades),
                "wins": len([t for t in down_trades if t.win_loss == "WIN"]),
                "win_rate": len([t for t in down_trades if t.win_loss == "WIN"]) / len(down_trades) if down_trades else 0,
                "total_pnl": round(sum(t.realized_settlement_pnl for t in down_trades), 4),
                "avg_pnl": round(statistics.mean([t.realized_settlement_pnl for t in down_trades]), 4) if down_trades else 0,
            },
        }
        
        # ── PnL by entry bucket ──
        pnl_by_bucket = {}
        buckets = [(0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70)]
        for lo, hi in buckets:
            bucket_trades = [t for t in resolved if lo <= t.entry_price < hi]
            if bucket_trades:
                pnl_by_bucket[f"{lo:.2f}-{hi:.2f}"] = {
                    "count": len(bucket_trades),
                    "win_rate": len([t for t in bucket_trades if t.win_loss == "WIN"]) / len(bucket_trades),
                    "total_pnl": round(sum(t.realized_settlement_pnl for t in bucket_trades), 4),
                    "avg_pnl": round(statistics.mean([t.realized_settlement_pnl for t in bucket_trades]), 4),
                }
        
        # ── PnL by regime ──
        pnl_by_regime = {}
        for regime, count in regime_counts.items():
            regime_trades = [t for t in resolved if t.regime == regime]
            pnl_by_regime[regime] = {
                "count": count,
                "win_rate": len([t for t in regime_trades if t.win_loss == "WIN"]) / len(regime_trades) if regime_trades else 0,
                "total_pnl": round(sum(t.realized_settlement_pnl for t in regime_trades), 4),
                "avg_pnl": round(statistics.mean([t.realized_settlement_pnl for t in regime_trades]), 4) if regime_trades else 0,
            }
        
        # ── PnL by transition decile ──
        pnl_by_transition_decile = {}
        if transition_scores:
            sorted_trades = sorted(resolved, key=lambda t: t.transition_score)
            decile_size = max(1, len(sorted_trades) // 10)
            for i in range(10):
                start = i * decile_size
                end = start + decile_size if i < 9 else len(sorted_trades)
                decile_trades = sorted_trades[start:end]
                if decile_trades:
                    pnl_by_transition_decile[f"D{i+1}"] = {
                        "range": f"{decile_trades[0].transition_score:.3f} to {decile_trades[-1].transition_score:.3f}",
                        "count": len(decile_trades),
                        "win_rate": len([t for t in decile_trades if t.win_loss == "WIN"]) / len(decile_trades),
                        "total_pnl": round(sum(t.realized_settlement_pnl for t in decile_trades), 4),
                    }
        
        # ── Overall metrics ──
        total_pnl = sum(t.realized_settlement_pnl for t in resolved)
        total_wins = len([t for t in resolved if t.realized_settlement_pnl > 0])
        total_losses = len([t for t in resolved if t.realized_settlement_pnl < 0])
        gross_profit = sum(t.realized_settlement_pnl for t in resolved if t.realized_settlement_pnl > 0)
        gross_loss = abs(sum(t.realized_settlement_pnl for t in resolved if t.realized_settlement_pnl < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # ── Pass/Fail ──
        criteria = {
            "all_settlements_binary": all_binary,
            "no_midpoint_settlement": no_midpoint,
            "spread_valid": spread_valid,
            "imbalance_non_fake": not all_zero_imbalance,
            "transition_not_clamped": extreme_ratio <= MAX_CLAMPED_RATIO,
            "regime_entropy_positive": regime_distribution["entropy_bits"] > 0,
            "settlement_errors_zero": self._settlement_errors == 0,
            "accounting_errors_zero": self._accounting_errors == 0,
            "minimum_trades_met": len(resolved) >= MINIMUM_RESOLVED_TRADES,
            "ev_positive": total_pnl > 0,
            "profit_factor_met": profit_factor >= MINIMUM_PROFIT_FACTOR,
        }
        
        all_pass = all(criteria.values())
        
        return {
            "version": "V20.3_BINARY_REALITY_VALIDATION",
            "status": "PASS" if all_pass else "FAIL",
            "criteria": criteria,
            "summary": {
                "resolved_trades": len(resolved),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": len(wins) / len(resolved) if resolved else 0,
                "total_pnl": round(total_pnl, 4),
                "profit_factor": round(profit_factor, 4) if profit_factor != float('inf') else "inf",
                "gross_profit": round(gross_profit, 4),
                "gross_loss": round(gross_loss, 4),
                "avg_pnl_per_trade": round(total_pnl / len(resolved), 4) if resolved else 0,
            },
            "distributions": {
                "binary_win_loss": {
                    "wins": len(wins),
                    "losses": len(losses),
                },
                "spread": spread_distribution,
                "imbalance": imbalance_distribution,
                "transition_score": transition_distribution,
                "regime": regime_distribution,
                "pnl_by_side": pnl_by_side,
                "pnl_by_bucket": pnl_by_bucket,
                "pnl_by_regime": pnl_by_regime,
                "pnl_by_transition_decile": pnl_by_transition_decile,
            },
            "errors": {
                "settlement_errors": self._settlement_errors,
                "accounting_errors": self._accounting_errors,
            },
            "live_remains_blocked": True,
            "required_next_step": "Pass V20.3_BINARY_REALITY_VALIDATION with 50+ resolved trades before enabling live",
        }
    
    def _entropy(self, counter: Counter) -> float:
        """Compute Shannon entropy in bits."""
        import math
        total = sum(counter.values())
        if total == 0:
            return 0.0
        return -sum((c / total) * math.log2(c / total) for c in counter.values())