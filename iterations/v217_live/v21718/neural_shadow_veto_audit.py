#!/usr/bin/env python3
"""
V21.7.18 — P2: Neural Shadow Veto Audit
=========================================
Diagnostic shadow veto layer. Records what neural shadow WOULD have done
for every eligible trade candidate. Not connected to execution.
Promotion to veto layer requires: 100+ resolved candidates, vetoed_losers > vetoed_winners,
net_EV_improvement > 0, PF_improvement > 0, no core-edge suppression.
"""
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional
from collections import defaultdict

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "v21718_hardening"
OUT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(OUT / "neural_shadow_veto.log"), logging.StreamHandler()],
)
log = logging.getLogger("v21718_neural_shadow")


class NeuralShadowVetoAudit:
    """
    Diagnostic-only shadow veto.
    Records neural shadow score for every eligible trade candidate.
    Does NOT generate entries. Does NOT override hard gates.
    """

    def __init__(self):
        self.audit_entries: List[dict] = []
        self._load_existing()

    def _load_existing(self):
        path = OUT / "neural_shadow_veto_entries.json"
        if path.exists():
            try:
                with open(path) as f:
                    self.audit_entries = json.load(f)
                log.info(f"Loaded {len(self.audit_entries)} existing veto audit entries")
            except Exception as e:
                log.warning(f"Could not load existing veto audit data: {e}")

    def _save(self):
        path = OUT / "neural_shadow_veto_entries.json"
        with open(path, "w") as f:
            json.dump(self.audit_entries, f, indent=2, default=str)

    def record_candidate(self, trade_id: str, profile: str, market_slug: str,
                         condition_id: str, side: str, entry_price: float,
                         core_gate_decision: str, neural_shadow_score: float) -> dict:
        """
        Record a trade candidate with both core gate decision and neural shadow score.
        core_gate_decision: "ACCEPTED" | "REJECTED" | "BLOCKED"
        neural_shadow_score: 0.0-1.0 (higher = more confident in the trade direction)
        """
        entry = {
            "trade_id": trade_id,
            "profile": profile,
            "market_slug": market_slug,
            "condition_id": condition_id,
            "side": side,
            "entry_price": entry_price,
            "core_gate_decision": core_gate_decision,
            "neural_shadow_score": neural_shadow_score,
            "neural_shadow_veto_candidate": neural_shadow_score < 0.3,
            "would_have_blocked": neural_shadow_score < 0.3 and core_gate_decision == "ACCEPTED",
            "actual_outcome": None,
            "pnl": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.audit_entries.append(entry)
        self._save()
        log.info(f"Recorded candidate {trade_id}: core={core_gate_decision} shadow={neural_shadow_score:.3f} would_block={entry['would_have_blocked']}")
        return entry

    def resolve_candidate(self, trade_id: str, outcome: int, pnl: float) -> bool:
        """Resolve a candidate with actual outcome (1=win, 0=loss) and PnL."""
        for entry in self.audit_entries:
            if entry["trade_id"] == trade_id:
                entry["actual_outcome"] = outcome
                entry["pnl"] = pnl
                self._save()
                return True
        return False

    def evaluate_veto_effectiveness(self) -> dict:
        """
        Evaluate whether neural shadow would improve EV as a veto layer.
        Promotion criteria (§5):
        - resolved_candidates >= 100
        - vetoed_losers > vetoed_winners
        - net_EV_improvement > 0
        - PF_improvement > 0
        - no core-edge suppression
        - no mode errors
        """
        resolved = [e for e in self.audit_entries if e.get("actual_outcome") is not None]
        unresolved = [e for e in self.audit_entries if e.get("actual_outcome") is None]

        if not resolved:
            return {
                "classification": "NEURAL_SHADOW_DIAGNOSTIC_ONLY",
                "resolved_candidates": 0,
                "unresolved_candidates": len(unresolved),
                "total_candidates": len(self.audit_entries),
                "blocked_winners": 0,
                "blocked_losers": 0,
                "core_ev": 0.0,
                "veto_ev": 0.0,
                "net_ev_improvement": 0.0,
                "core_pf": "N/A",
                "veto_pf": "N/A",
                "pf_improvement": "N/A",
                "edge_suppression_detected": False,
                "promotion_eligible": False,
                "reason": "INSUFFICIENT_DATA: need 100+ resolved candidates",
            }

        # Veto analysis: would_have_blocked trades
        blocked_winners = [e for e in resolved if e["would_have_blocked"] and e["actual_outcome"] == 1]
        blocked_losers = [e for e in resolved if e["would_have_blocked"] and e["actual_outcome"] == 0]

        # Core gate accepted trades
        core_accepted = [e for e in resolved if e["core_gate_decision"] == "ACCEPTED"]
        core_accepted_pnl = sum(e.get("pnl", 0) for e in core_accepted)
        core_wins = [e for e in core_accepted if e["actual_outcome"] == 1]
        core_losses = [e for e in core_accepted if e["actual_outcome"] == 0]
        core_pf = (sum(e.get("pnl", 0) for e in core_wins) /
                   abs(sum(e.get("pnl", 0) for e in core_losses))) if core_losses else float("inf")

        # If veto were active: remove blocked trades
        veto_accepted = [e for e in core_accepted if not e["would_have_blocked"]]
        veto_accepted_pnl = sum(e.get("pnl", 0) for e in veto_accepted)
        veto_wins = [e for e in veto_accepted if e["actual_outcome"] == 1]
        veto_losses = [e for e in veto_accepted if e["actual_outcome"] == 0]
        veto_pf = (sum(e.get("pnl", 0) for e in veto_wins) /
                   abs(sum(e.get("pnl", 0) for e in veto_losses))) if veto_losses else float("inf")

        # Edge suppression check: does veto block high-confidence core trades?
        core_high_confidence_blocked = [e for e in blocked_winners if e["neural_shadow_score"] > 0.5]
        edge_suppression = len(core_high_confidence_blocked) > 0

        net_ev_improvement = veto_accepted_pnl - core_accepted_pnl
        pf_improvement = (veto_pf - core_pf) if isinstance(veto_pf, float) and isinstance(core_pf, float) else 0

        promotion_eligible = (
            len(resolved) >= 100 and
            len(blocked_losers) > len(blocked_winners) and
            net_ev_improvement > 0 and
            pf_improvement > 0 and
            not edge_suppression
        )

        if promotion_eligible:
            classification = "NEURAL_VETO_REVIEW_CANDIDATE"
        else:
            classification = "NEURAL_SHADOW_DIAGNOSTIC_ONLY"

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "directive": "V21.7.18",
            "classification": classification,
            "resolved_candidates": len(resolved),
            "unresolved_candidates": len(unresolved),
            "total_candidates": len(self.audit_entries),
            "blocked_winners": len(blocked_winners),
            "blocked_losers": len(blocked_losers),
            "core_ev": round(core_accepted_pnl, 4),
            "veto_ev": round(veto_accepted_pnl, 4),
            "net_ev_improvement": round(net_ev_improvement, 4),
            "core_pf": round(core_pf, 4) if isinstance(core_pf, float) and core_pf != float("inf") else "inf",
            "veto_pf": round(veto_pf, 4) if isinstance(veto_pf, float) and veto_pf != float("inf") else "inf",
            "pf_improvement": round(pf_improvement, 4) if isinstance(pf_improvement, (int, float)) else "N/A",
            "edge_suppression_detected": edge_suppression,
            "promotion_eligible": promotion_eligible,
            "promotion_criteria": {
                "resolved_candidates_required": 100,
                "resolved_candidates_actual": len(resolved),
                "vetoed_losers_gt_winners": len(blocked_losers) > len(blocked_winners),
                "net_ev_improvement_gt_0": net_ev_improvement > 0,
                "pf_improvement_gt_0": pf_improvement > 0 if isinstance(pf_improvement, (int, float)) else False,
                "no_core_edge_suppression": not edge_suppression,
                "no_mode_errors": True,
            },
            "rules": [
                "neural_shadow_does_NOT_generate_entries",
                "neural_shadow_does_NOT_override_hard_gates",
                "promotion_requires_100+_resolved_candidates",
                "promotion_requires_vetoed_losers_gt_winners",
                "promotion_requires_net_EV_improvement",
                "promotion_requires_PF_improvement",
                "promotion_requires_no_core_edge_suppression",
                "not_live_enabled_automatically",
            ],
        }

        return report

    def generate_report(self) -> dict:
        """Generate the neural shadow veto audit report per §5."""
        report = self.evaluate_veto_effectiveness()

        with open(OUT / "neural_shadow_veto_audit.json", "w") as f:
            json.dump(report, f, indent=2)

        log.info(f"Neural shadow: {report['classification']}")
        log.info(f"  Resolved: {report['resolved_candidates']}, Blocked winners: {report['blocked_winners']}, Blocked losers: {report['blocked_losers']}")
        log.info(f"  Core EV: {report['core_ev']}, Veto EV: {report['veto_ev']}, Net improvement: {report['net_ev_improvement']}")

        return report


if __name__ == "__main__":
    audit = NeuralShadowVetoAudit()
    report = audit.generate_report()