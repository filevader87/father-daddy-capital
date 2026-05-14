#!/usr/bin/env python3
"""
Father Daddy Capital — Bayesian Calibration Layer
===================================================
Sits atop the plastic network. Computes a calibrated probability from
neural predictions, tracks calibration quality via Brier score, and
outputs uncertainty bounds for Kelly sizing.

Design principles:
  - Sparse-data tolerant (Bayesian priors, not point estimates)
  - Updates on every resolved trade (online logistic calibration)
  - Outputs credible intervals for position sizing decisions
  - No Pyro/PyMC dependency — pure numpy implementation

Architecture:
  Neural pred [-1,1] → Logistic calibration → P(yes) with 95% CI
  Updates: Newton-Raphson step on binary cross-entropy per resolved trade
  Priors: β ~ Normal(0, 1) — skeptical by default
"""

import numpy as np
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Configuration ───────────────────────────────────────────────────────────

PRIOR_MEAN = 0.0           # Prior mean for all β coefficients
PRIOR_PRECISION = 1.0      # Prior precision (1/variance) — higher = stronger prior
N_FEATURES = 12             # Input feature vector size (see feature_encoder.py)
INITIAL_LR = 0.02
LR_DECAY = 0.998

BAYESIAN_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/neural_weights")
BAYESIAN_STATE = BAYESIAN_DIR / "bayesian_state.json"


class BayesianCalibrator:
    """
    Logistic calibration layer that maps neural predictions to calibrated
    probabilities with uncertainty estimates.

    Model: P(yes | x) = σ(β₀ + β₁·x₁ + ... + βₙ·xₙ)
    Update: Laplace approximation on each resolved trade
    """

    def __init__(self):
        # β coefficients: intercept (β₀) + one per feature
        self.beta = np.full(N_FEATURES + 1, 0.0, dtype=float)  # β₀ first
        # Precision matrix (inverse covariance) — diagonal for simplicity
        self.precision = np.full(N_FEATURES + 1, PRIOR_PRECISION, dtype=float)
        self.mu = np.zeros(N_FEATURES + 1, dtype=float)  # posterior mode

        # Calibration tracking
        self.updates = 0
        self.learning_rate = INITIAL_LR
        self.brier_sum = 0.0
        self.brier_count = 0

        # Trade log for diagnostics
        self.calibration_log: list[dict] = []

        self._load()

    # ── Prediction ────────────────────────────────────────────────────────

    def _sigmoid(self, z: float) -> float:
        """Safe sigmoid."""
        if z > 20:
            return 1.0
        if z < -20:
            return 0.0
        return 1.0 / (1.0 + np.exp(-z))

    def _linear_score(self, features: np.ndarray) -> float:
        """β₀ + Σ βᵢ·featureᵢ"""
        return float(self.beta[0] + np.dot(self.beta[1:], features))

    def predict(self, features: np.ndarray) -> dict:
        """
        Return calibrated probability with uncertainty bounds.

        Args:
            features: [N_FEATURES,] vector from FeatureEncoder.encode()

        Returns:
            {
                "probability": float,         # P(yes)
                "probability_ci_low": float,  # 2.5% percentile
                "probability_ci_high": float, # 97.5% percentile
                "certainty": float,           # 1 - CI width
                "log_odds": float,            # raw log odds
            }
        """
        assert len(features) == N_FEATURES, \
            f"Expected {N_FEATURES} features, got {len(features)}"

        log_odds = self._linear_score(features)
        prob = self._sigmoid(log_odds)

        # Uncertainty: use diagonal precision for CI
        # Var(linear_score) = Σ prec⁻¹ᵢ · (featureᵢ)²  (approximation)
        # features[0] for intercept is implicitly 1
        full_features = np.concatenate([[1.0], features])
        var_log_odds = np.sum(full_features ** 2 / np.maximum(self.precision, 1e-8))
        std_log_odds = np.sqrt(var_log_odds)

        # 95% CI on log-odds, then transform
        lo_log_odds = log_odds - 1.96 * std_log_odds
        hi_log_odds = log_odds + 1.96 * std_log_odds

        prob_lo = self._sigmoid(lo_log_odds)
        prob_hi = self._sigmoid(hi_log_odds)
        ci_width = prob_hi - prob_lo

        return {
            "probability": round(float(prob), 4),
            "probability_ci_low": round(float(prob_lo), 4),
            "probability_ci_high": round(float(prob_hi), 4),
            "certainty": round(float(1.0 - min(ci_width, 1.0)), 4),
            "log_odds": round(float(log_odds), 4),
        }

    # ── Online learning ───────────────────────────────────────────────────

    def update(self, features: np.ndarray, outcome: int):
        """
        Update calibration from a resolved trade.

        Args:
            features: [N_FEATURES,] feature vector at time of prediction
            outcome: 1 if YES contract paid, 0 if it did not
        """
        assert len(features) == N_FEATURES
        assert outcome in (0, 1)

        full_features = np.concatenate([[1.0], features])

        # ── 1. Forward: compute current prediction ──
        log_odds = self._linear_score(features)
        prob = self._sigmoid(log_odds)

        # ── 2. Binary cross-entropy gradient ──
        # dL/dβ = (prob - outcome) · feature
        error = prob - float(outcome)
        gradient = error * full_features

        # ── 3. Hessian (outer product for logistic regression) ──
        # d²L/dβ² = prob · (1 - prob) · feature · feature^T
        hessian_diag = prob * (1.0 - prob) * full_features ** 2

        # ── 4. Laplace update (Bayesian online learning) ──
        # precision += hessian_diag  (Fisher information accumulation)
        # beta -= lr · precision⁻¹ · gradient  (Newton-like step)
        self.precision += hessian_diag
        effective_lr = self.learning_rate / np.maximum(self.precision, 1e-8)
        self.beta -= effective_lr * gradient

        # ── 5. Track Brier score ──
        brier = (prob - outcome) ** 2
        self.brier_sum += brier
        self.brier_count += 1

        # ── 6. Decay learning rate ──
        self.learning_rate = max(0.001, self.learning_rate * LR_DECAY)

        self.updates += 1

        # ── 7. Log ──
        self.calibration_log.append({
            "update": self.updates,
            "prob": round(float(prob), 4),
            "outcome": outcome,
            "brier": round(float(brier), 4),
            "lr": round(float(self.learning_rate), 6),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if len(self.calibration_log) > 500:
            self.calibration_log = self.calibration_log[-500:]

        self._save()

    # ── Metrics ───────────────────────────────────────────────────────────

    @property
    def brier_score(self) -> float:
        """Current Brier score (lower is better, 0.25 = random)."""
        if self.brier_count == 0:
            return 0.25
        return float(self.brier_sum / self.brier_count)

    @property
    def calibration_factor(self) -> float:
        """
        Multiplier for Kelly sizing based on calibration quality.
        0.0 = completely uncalibrated, 1.0 = perfectly calibrated.
        """
        return float(max(0.0, min(1.0, 1.0 - self.brier_score * 4.0)))

    def stats(self) -> dict:
        return {
            "updates": self.updates,
            "brier_score": round(self.brier_score, 4),
            "calibration_factor": round(self.calibration_factor, 4),
            "learning_rate": round(float(self.learning_rate), 6),
            "beta_coefficients": [round(float(b), 4) for b in self.beta.tolist()],
            "precision_range": [
                round(float(np.min(self.precision)), 4),
                round(float(np.max(self.precision)), 4),
            ],
        }

    # ── Persistence ───────────────────────────────────────────────────────

    def _save(self):
        BAYESIAN_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "beta": self.beta.tolist(),
            "precision": self.precision.tolist(),
            "updates": self.updates,
            "learning_rate": float(self.learning_rate),
            "brier_sum": float(self.brier_sum),
            "brier_count": self.brier_count,
            "last_saved": datetime.now(timezone.utc).isoformat(),
        }
        BAYESIAN_STATE.write_text(json.dumps(state, indent=2))

    def _load(self):
        if not BAYESIAN_STATE.exists():
            return
        state = json.loads(BAYESIAN_STATE.read_text())
        self.beta = np.array(state["beta"], dtype=float)
        self.precision = np.array(state["precision"], dtype=float)
        self.updates = state.get("updates", 0)
        self.learning_rate = max(0.001, state.get("learning_rate", INITIAL_LR))
        self.brier_sum = state.get("brier_sum", 0.0)
        self.brier_count = state.get("brier_count", 0)


# ─── Test ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cal = BayesianCalibrator()
    print(f"Loaded: {cal.updates} updates, Brier={cal.brier_score:.4f}")

    # Simulate calibration learning
    np.random.seed(42)
    for i in range(50):
        # Features: 10-dim, some with signal
        features = np.random.randn(N_FEATURES) * 0.5
        # True probability depends on feature[0] and feature[3]
        true_prob = cal._sigmoid(0.5 * features[0] + 0.3 * features[3])
        outcome = 1 if np.random.random() < true_prob else 0

        result = cal.predict(features)
        cal.update(features, outcome)

        if i % 10 == 9:
            s = cal.stats()
            print(f"  Update {i+1}: Brier={s['brier_score']:.4f}, "
                  f"Factor={s['calibration_factor']:.4f}")

    print(f"\nFinal: {cal.stats()['updates']} updates, "
          f"Brier={cal.stats()['brier_score']:.4f}, "
          f"Calibration={cal.stats()['calibration_factor']:.2%}")
