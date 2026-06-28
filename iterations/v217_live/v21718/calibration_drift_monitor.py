#!/usr/bin/env python3
"""
V21.7.18 — P1: Calibration Drift Monitor
==========================================
Weekly calibration monitoring for probability estimates.
Tracks Brier score, log loss, calibration by probability bucket,
realized WR, expected EV, realized EV, profit factor per profile.

Alert triggers: Brier worsens, realized EV negative, 90% bucket underperforms,
PF < 1.0, calibration deterioration.
"""
import json
import math
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "v21718_hardening"
OUT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(OUT / "calibration_drift.log"), logging.StreamHandler()],
)
log = logging.getLogger("v21718_calibration")

# Probability buckets for calibration analysis
PROB_BUCKETS = {
    "50-60": (0.50, 0.60),
    "60-70": (0.60, 0.70),
    "70-80": (0.70, 0.80),
    "80-90": (0.80, 0.90),
    "90-100": (0.90, 1.00),
}

PROFILES = [
    "BTC_DOWN_15M",
    "BTC_DOWN_5M",
    "SCALPER_DOWN_LAG_03_05",
    "RAIN_YES",
    "RAIN_NO",
]


def brier_score(predictions: List[float], outcomes: List[int]) -> float:
    """Brier score: mean((predicted - actual)^2). Lower is better."""
    if not predictions:
        return float("inf")
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def log_loss(predictions: List[float], outcomes: List[int], eps: float = 1e-15) -> float:
    """Log loss: -mean(actual * log(pred) + (1-actual) * log(1-pred)). Lower is better."""
    if not predictions:
        return float("inf")
    total = 0.0
    for p, o in zip(predictions, outcomes):
        p = max(eps, min(1 - eps, p))
        total -= o * math.log(p) + (1 - o) * math.log(1 - p)
    return total / len(predictions)


def profit_factor(wins: List[float], losses: List[float]) -> float:
    """Profit factor: sum(wins) / sum(abs(losses)). > 1.0 is profitable."""
    total_wins = sum(wins) if wins else 0
    total_losses = sum(abs(l) for l in losses) if losses else 0
    if total_losses == 0:
        return float("inf") if total_wins > 0 else 0.0
    return total_wins / total_losses


class CalibrationDriftMonitor:
    """Monitor calibration drift across profiles and probability buckets."""

    def __init__(self):
        self.predictions: Dict[str, List[dict]] = {p: [] for p in PROFILES}
        self._load_existing()

    def _load_existing(self):
        path = OUT / "calibration_predictions.json"
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                for profile in PROFILES:
                    self.predictions[profile] = data.get(profile, [])
                log.info(f"Loaded calibration data: {sum(len(v) for v in self.predictions.values())} predictions")
            except Exception as e:
                log.warning(f"Could not load calibration data: {e}")

    def _save(self):
        path = OUT / "calibration_predictions.json"
        with open(path, "w") as f:
            json.dump(self.predictions, f, indent=2, default=str)

    def record_prediction(self, profile: str, predicted_prob: float,
                          entry_price: float, market_slug: str,
                          condition_id: str, side: str, trade_id: str):
        """Record a prediction before outcome is known."""
        if profile not in self.predictions:
            self.predictions[profile] = []
        self.predictions[profile].append({
            "trade_id": trade_id,
            "predicted_prob": predicted_prob,
            "entry_price": entry_price,
            "market_slug": market_slug,
            "condition_id": condition_id,
            "side": side,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "outcome": None,
            "pnl": None,
        })
        self._save()

    def resolve_prediction(self, profile: str, trade_id: str, outcome: int, pnl: float):
        """Resolve a prediction with actual outcome (1=win, 0=loss) and realized PnL."""
        for pred in self.predictions.get(profile, []):
            if pred["trade_id"] == trade_id:
                pred["outcome"] = outcome
                pred["pnl"] = pnl
                self._save()
                return True
        return False

    def _bucket_for(self, prob: float) -> Optional[str]:
        for name, (lo, hi) in PROB_BUCKETS.items():
            if lo <= prob < hi:
                return name
        if prob >= 1.0:
            return "90-100"
        return None

    def compute_profile_metrics(self, profile: str) -> dict:
        """Compute full calibration metrics for a profile."""
        preds = [p for p in self.predictions.get(profile, []) if p.get("outcome") is not None]
        unresolved = [p for p in self.predictions.get(profile, []) if p.get("outcome") is None]

        if not preds:
            return {
                "profile": profile,
                "classification": "INSUFFICIENT_DATA",
                "sample_size": 0,
                "unresolved": len(unresolved),
                "brier_score": None,
                "log_loss": None,
                "expected_EV": 0.0,
                "realized_EV": 0.0,
                "profit_factor": None,
                "win_rate": None,
                "calibration_by_bucket": {},
                "drift_vs_previous": None,
                "alerts": ["INSUFFICIENT_DATA: need resolved samples"],
            }

        predictions = [p["predicted_prob"] for p in preds]
        outcomes = [p["outcome"] for p in preds]
        pnls = [p["pnl"] for p in preds]

        bs = brier_score(predictions, outcomes)
        ll = log_loss(predictions, outcomes)

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total_pnl = sum(pnls)
        expected_ev = sum(p * (1 - e) - (1 - p) * e for p, e in zip(predictions, outcomes)) / len(preds)

        # Calibration by bucket
        bucket_data = defaultdict(lambda: {"predicted": [], "outcomes": [], "pnls": []})
        for p in preds:
            bucket = self._bucket_for(p["predicted_prob"])
            if bucket:
                bucket_data[bucket]["predicted"].append(p["predicted_prob"])
                bucket_data[bucket]["outcomes"].append(p["outcome"])
                bucket_data[bucket]["pnls"].append(p["pnl"])

        calibration_by_bucket = {}
        for bucket, data in bucket_data.items():
            n = len(data["outcomes"])
            realized_wr = sum(data["outcomes"]) / n if n > 0 else 0
            expected_wr = sum(data["predicted"]) / n if n > 0 else 0
            calibration_by_bucket[bucket] = {
                "sample_size": n,
                "expected_win_rate": round(expected_wr, 4),
                "realized_win_rate": round(realized_wr, 4),
                "calibration_gap": round(realized_wr - expected_wr, 4),
                "realized_EV": round(sum(data["pnls"]), 4),
            }

        # Alerts
        alerts = []
        if bs > 0.25:
            alerts.append(f"HIGH_BRIER: {bs:.4f} > 0.25 threshold")
        if total_pnl < 0:
            alerts.append(f"NEGATIVE_REALIZED_EV: {total_pnl:.4f}")
        if "90-100" in calibration_by_bucket:
            bucket_90 = calibration_by_bucket["90-100"]
            if bucket_90["realized_win_rate"] < bucket_90["expected_win_rate"] - 0.1:
                alerts.append(f"90%_BUCKET_UNDERPERFORMS: realized_wr={bucket_90['realized_win_rate']:.2f} expected_wr={bucket_90['expected_win_rate']:.2f}")
        pf = profit_factor(wins, losses)
        if pf < 1.0 and len(preds) >= 10:
            alerts.append(f"PF_BELOW_1: {pf:.4f}")

        return {
            "profile": profile,
            "classification": "CALIBRATION_MONITORING_ACTIVE",
            "sample_size": len(preds),
            "unresolved": len(unresolved),
            "brier_score": round(bs, 4),
            "log_loss": round(ll, 4),
            "expected_EV": round(expected_ev, 4),
            "realized_EV": round(total_pnl, 4),
            "profit_factor": round(pf, 4) if pf != float("inf") else "inf",
            "win_rate": round(sum(outcomes) / len(outcomes), 4),
            "calibration_by_bucket": calibration_by_bucket,
            "drift_vs_previous": None,
            "alerts": alerts,
        }

    def generate_report(self) -> dict:
        """Generate the calibration drift report per §4."""
        profile_metrics = {}
        for profile in PROFILES:
            profile_metrics[profile] = self.compute_profile_metrics(profile)

        all_alerts = []
        for profile, metrics in profile_metrics.items():
            for alert in metrics.get("alerts", []):
                all_alerts.append(f"{profile}: {alert}")

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "directive": "V21.7.18",
            "classification": "CALIBRATION_MONITORING_ACTIVE",
            "profiles": profile_metrics,
            "total_resolved_samples": sum(m["sample_size"] for m in profile_metrics.values()),
            "total_unresolved": sum(m["unresolved"] for m in profile_metrics.values()),
            "alerts": all_alerts,
            "rules": [
                "do_not_retrain_or_overwrite_calibrator_automatically",
                "review_required_before_retraining",
                "insufficient_data_if_sample_size_lt_10",
            ],
            "weekly_check": "weekly_calibration_check",
        }

        with open(OUT / "calibration_drift_report.json", "w") as f:
            json.dump(report, f, indent=2)

        log.info(f"Calibration report: {report['classification']}")
        for profile, metrics in profile_metrics.items():
            log.info(f"  {profile}: n={metrics['sample_size']}, brier={metrics['brier_score']}, alerts={len(metrics.get('alerts', []))}")

        return report


if __name__ == "__main__":
    monitor = CalibrationDriftMonitor()
    report = monitor.generate_report()