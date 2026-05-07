#!/usr/bin/env python3
"""
Father Daddy Capital — Neural Plasticity Layer
================================================
Lightweight neural network with online learning (plasticity).
Zero external dependencies beyond numpy.

Architecture:
  - 2 hidden layers (ReLU) + output layer (tanh → expected return [-1,1])
  - Online SGD: weight update after every closed trade based on P&L outcome
  - Elastic weight consolidation (EWC) — preserves important weights
  - Experience replay buffer — prevents catastrophic forgetting
  - Learning rate decay — stabilizes as data accumulates

Plasticity means: the network never stops learning. Every trade outcome reshapes
the weights. Winning patterns get reinforced. Losing patterns get pruned.
"""

import numpy as np
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── Configuration ───────────────────────────────────────────────────────────

INPUT_DIM = 8          # RSI, MACD, trend, momentum, mean_rev, volatility, asset_class_enc, confidence
HIDDEN1_DIM = 16
HIDDEN2_DIM = 8
OUTPUT_DIM = 1         # Expected return [-1, 1]

INITIAL_LR = 0.01
LR_DECAY = 0.9995       # Per-update decay
MIN_LR = 0.0001

REPLAY_BUFFER_SIZE = 200
REPLAY_BATCH_SIZE = 16
REPLAY_FREQUENCY = 5    # Replay every N updates

EWC_LAMBDA = 0.1        # Importance of old weights (higher = more rigid)

WEIGHTS_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/neural_weights")
WEIGHTS_FILE = WEIGHTS_DIR / "plastic_weights.npz"
MEMORY_FILE = WEIGHTS_DIR / "plastic_memory.json"
PERF_FILE = WEIGHTS_DIR / "plastic_performance.json"


# ─── Layer Helpers ───────────────────────────────────────────────────────────

def relu(x: np.ndarray) -> np.ndarray:
    return np.maximum(0.0, x)

def relu_derivative(x: np.ndarray) -> np.ndarray:
    return (x > 0.0).astype(float)

def tanh(x: np.ndarray) -> np.ndarray:
    return np.tanh(x)

def tanh_derivative(x: np.ndarray) -> np.ndarray:
    return 1.0 - np.tanh(x) ** 2

def glorot_init(in_dim: int, out_dim: int) -> np.ndarray:
    """Xavier/Glorot initialization — optimal for tanh/relu."""
    limit = np.sqrt(6.0 / (in_dim + out_dim))
    return np.random.uniform(-limit, limit, (in_dim, out_dim))


# ─── Plastic Network ─────────────────────────────────────────────────────────

class PlasticNetwork:
    """2-hidden-layer network that learns continuously from trade outcomes."""

    def __init__(self):
        # Weights & biases
        self.W1 = glorot_init(INPUT_DIM, HIDDEN1_DIM)
        self.b1 = np.zeros((1, HIDDEN1_DIM))
        self.W2 = glorot_init(HIDDEN1_DIM, HIDDEN2_DIM)
        self.b2 = np.zeros((1, HIDDEN2_DIM))
        self.W3 = glorot_init(HIDDEN2_DIM, OUTPUT_DIM)
        self.b3 = np.zeros((1, OUTPUT_DIM))

        # Plasticity state
        self.learning_rate = INITIAL_LR
        self.updates = 0
        self.total_loss = 0.0
        self.win_loss_log = []  # Track recent performance

        # EWC — importance estimates for each weight
        self.ewc_fisher = {k: np.zeros_like(v) for k, v in self._weights_dict().items()}
        self.ewc_anchor = {k: v.copy() for k, v in self._weights_dict().items()}
        self.ewc_anchor_age = 0

        # Experience replay
        self.replay_buffer: list[tuple[np.ndarray, float]] = []

    def _weights_dict(self) -> dict[str, np.ndarray]:
        return {"W1": self.W1, "W2": self.W2, "W3": self.W3}

    # ── Forward pass ─────────────────────────────────────────────────────

    def forward(self, x: np.ndarray) -> tuple[np.ndarray, list[np.ndarray]]:
        """
        Forward pass. Returns (output, [activations]) for backprop.
        x shape: (batch, INPUT_DIM)
        """
        cache = [x]  # cache[0] = input

        # Hidden 1
        z1 = x @ self.W1 + self.b1
        a1 = relu(z1)
        cache.extend([z1, a1])

        # Hidden 2
        z2 = a1 @ self.W2 + self.b2
        a2 = relu(z2)
        cache.extend([z2, a2])

        # Output
        z3 = a2 @ self.W3 + self.b3
        a3 = tanh(z3)
        cache.extend([z3, a3])

        return a3, cache

    def predict(self, x: np.ndarray) -> float:
        """Single prediction. Returns scalar in [-1, 1]."""
        if x.ndim == 1:
            x = x.reshape(1, -1)
        out, _ = self.forward(x)
        return float(out[0, 0])

    # ── Backward pass (SGD) ──────────────────────────────────────────────

    def _backward(self, cache: list[np.ndarray], y_true: np.ndarray, lr: float):
        """
        Compute gradients and update weights.
        cache: [input, z1, a1, z2, a2, z3, a3]
        """
        x_in = cache[0]
        a1 = cache[2]
        a2 = cache[4]
        a3 = cache[6]

        batch_size = x_in.shape[0]

        # Output layer gradient
        d_loss = 2 * (a3 - y_true) / batch_size  # MSE derivative
        d_z3 = d_loss * tanh_derivative(cache[5])  # z3 = cache[5]
        d_W3 = a2.T @ d_z3
        d_b3 = np.sum(d_z3, axis=0, keepdims=True)

        # Hidden 2 gradient
        d_a2 = d_z3 @ self.W3.T
        d_z2 = d_a2 * relu_derivative(cache[3])  # z2 = cache[3]
        d_W2 = a1.T @ d_z2
        d_b2 = np.sum(d_z2, axis=0, keepdims=True)

        # Hidden 1 gradient
        d_a1 = d_z2 @ self.W2.T
        d_z1 = d_a1 * relu_derivative(cache[1])  # z1 = cache[1]
        d_W1 = x_in.T @ d_z1
        d_b1 = np.sum(d_z1, axis=0, keepdims=True)

        # EWC penalty — resist changing important weights
        ewc_penalty_W3 = self.ewc_fisher["W3"] * (self.W3 - self.ewc_anchor["W3"])
        ewc_penalty_W2 = self.ewc_fisher["W2"] * (self.W2 - self.ewc_anchor["W2"])
        ewc_penalty_W1 = self.ewc_fisher["W1"] * (self.W1 - self.ewc_anchor["W1"])

        # Update with EWC regularization
        self.W3 -= lr * (d_W3 + EWC_LAMBDA * ewc_penalty_W3)
        self.b3 -= lr * d_b3
        self.W2 -= lr * (d_W2 + EWC_LAMBDA * ewc_penalty_W2)
        self.b2 -= lr * d_b2
        self.W1 -= lr * (d_W1 + EWC_LAMBDA * ewc_penalty_W1)
        self.b1 -= lr * d_b1

    # ── Online learning ──────────────────────────────────────────────────

    def learn_from_trade(
        self,
        signal_vector: np.ndarray,
        predicted_return: float,
        actual_return: float,
    ):
        """
        One-shot online learning from a closed trade.

        Args:
            signal_vector: The input features at time of prediction (INPUT_DIM,)
            predicted_return: What the network predicted [-1, 1]
            actual_return: What actually happened [-1, 1] (scaled P&L)
        """
        x = signal_vector.reshape(1, -1)
        y = np.array([[actual_return]])

        # Forward + backward
        _, cache = self.forward(x)
        self._backward(cache, y, self.learning_rate)

        # Track loss
        loss = (predicted_return - actual_return) ** 2
        self.total_loss += loss
        self.updates += 1

        # Decay learning rate
        self.learning_rate = max(MIN_LR, self.learning_rate * LR_DECAY)

        # Log
        self.win_loss_log.append({
            "pred": round(predicted_return, 4),
            "actual": round(actual_return, 4),
            "loss": round(float(loss), 4),
            "lr": round(self.learning_rate, 6),
            "update": self.updates,
        })
        # Keep last 500
        if len(self.win_loss_log) > 500:
            self.win_loss_log = self.win_loss_log[-500:]

    def add_to_replay(self, signal_vector: np.ndarray, actual_return: float):
        """Store experience for later replay."""
        self.replay_buffer.append((signal_vector.copy(), actual_return))
        if len(self.replay_buffer) > REPLAY_BUFFER_SIZE:
            self.replay_buffer = self.replay_buffer[-REPLAY_BUFFER_SIZE:]

    def replay(self):
        """Replay a batch of past experiences to prevent forgetting."""
        if len(self.replay_buffer) < REPLAY_BATCH_SIZE:
            return

        indices = np.random.choice(
            len(self.replay_buffer),
            size=min(REPLAY_BATCH_SIZE, len(self.replay_buffer)),
            replace=False,
        )
        batch_x = np.vstack([self.replay_buffer[i][0] for i in indices])
        batch_y = np.array([[self.replay_buffer[i][1]] for i in indices])

        _, cache = self.forward(batch_x)
        # Use a lower LR for replay — gentler correction
        self._backward(cache, batch_y, self.learning_rate * 0.5)

    def consolidate(self):
        """
        EWC consolidation — snapshot current weights as anchors and estimate
        their importance (Fisher information proxy: gradient magnitude).
        Called periodically to lock in stable patterns.
        """
        self.ewc_anchor = {k: v.copy() for k, v in self._weights_dict().items()}
        # Fisher proxy: use current weight magnitudes as importance estimate
        for key in self.ewc_fisher:
            self.ewc_fisher[key] = np.abs(self._weights_dict()[key]) * 0.01
        self.ewc_anchor_age = 0

    # ── Persistence ──────────────────────────────────────────────────────

    def save(self):
        WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
        np.savez(
            str(WEIGHTS_FILE),
            W1=self.W1, b1=self.b1,
            W2=self.W2, b2=self.b2,
            W3=self.W3, b3=self.b3,
        )
        # Save plasticity metadata
        with open(MEMORY_FILE, "w") as f:
            json.dump({
                "updates": self.updates,
                "learning_rate": self.learning_rate,
                "total_loss": self.total_loss,
                "ewc_anchor_age": self.ewc_anchor_age,
                "replay_size": len(self.replay_buffer),
                "last_saved": datetime.now().isoformat(),
            }, f, indent=2)

    def load(self):
        if WEIGHTS_FILE.exists():
            data = np.load(str(WEIGHTS_FILE))
            self.W1 = data["W1"]
            self.b1 = data["b1"]
            self.W2 = data["W2"]
            self.b2 = data["b2"]
            self.W3 = data["W3"]
            self.b3 = data["b3"]
            # Initialize EWC anchor at loaded weights
            self.ewc_anchor = {k: v.copy() for k, v in self._weights_dict().items()}

        if MEMORY_FILE.exists():
            with open(MEMORY_FILE) as f:
                meta = json.load(f)
            self.updates = meta.get("updates", 0)
            self.learning_rate = max(MIN_LR, meta.get("learning_rate", INITIAL_LR))
            self.total_loss = meta.get("total_loss", 0.0)
            self.ewc_anchor_age = meta.get("ewc_anchor_age", 0)

    # ── Diagnostics ──────────────────────────────────────────────────────

    def stats(self) -> dict:
        avg_loss = self.total_loss / max(1, self.updates)
        recent = self.win_loss_log[-50:]
        recent_correct = sum(
            1 for e in recent if (e["pred"] > 0) == (e["actual"] > 0)
        ) if recent else 0

        return {
            "updates": self.updates,
            "learning_rate": round(self.learning_rate, 6),
            "avg_loss": round(avg_loss, 4),
            "recent_accuracy": round(recent_correct / max(1, len(recent)), 3),
            "replay_buffer_size": len(self.replay_buffer),
            "ewc_anchor_age": self.ewc_anchor_age,
            "weight_norms": {
                "W1": round(float(np.linalg.norm(self.W1)), 3),
                "W2": round(float(np.linalg.norm(self.W2)), 3),
                "W3": round(float(np.linalg.norm(self.W3)), 3),
            },
        }


# ─── Signal Encoder ──────────────────────────────────────────────────────────

def encode_signal_vector(scan_result: dict) -> np.ndarray:
    """
    Convert a scan result into the neural input vector.
    Expects the signal dict from paper_engine.compute_signals().
    """
    signals = scan_result.get("signals", {})
    return np.array([
        signals.get("rsi", 0.0),
        signals.get("macd", 0.0),
        signals.get("trend", 0.0),
        signals.get("momentum", 0.0),
        signals.get("mean_reversion", 0.0),
        scan_result.get("volatility", 0.0),
        1.0 if scan_result.get("asset_class") == "crypto" else -1.0,
        scan_result.get("confidence", 0.5),
    ], dtype=float)

def scale_pnl_to_target(actual_pnl_pct: float) -> float:
    """
    Scale raw P&L percentage to [-1, 1] target for the network.
    +10% → +0.8, -5% → -0.6, etc. Clipped.
    """
    return float(np.clip(actual_pnl_pct / 0.125, -1.0, 1.0))  # 12.5% = full signal


# ─── Performance Tracking ────────────────────────────────────────────────────

class PlasticPerformance:
    """Track NN performance over time."""

    def __init__(self):
        self.history: list[dict] = []
        if PERF_FILE.exists():
            with open(PERF_FILE) as f:
                self.history = json.load(f)

    def record(self, prediction: float, outcome: float, update: int):
        correct = (prediction > 0) == (outcome > 0)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "update": update,
            "prediction": round(prediction, 4),
            "outcome": round(outcome, 4),
            "correct": correct,
        }
        self.history.append(entry)
        # Keep last 5000 entries
        if len(self.history) > 5000:
            self.history = self.history[-5000:]

    def save(self):
        WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
        with open(PERF_FILE, "w") as f:
            json.dump(self.history[-5000:], f, indent=2)

    def recent_accuracy(self, n: int = 100) -> float:
        recent = self.history[-n:]
        if not recent:
            return 0.5
        return sum(1 for e in recent if e["correct"]) / len(recent)


# ─── Integration Entry Point ─────────────────────────────────────────────────

class NeuralPlasticityEngine:
    """
    Drop-in neural overlay for the FDC paper trading engine.
    Call before every trade to get a neural score, call after every
    closed trade to update the network.
    """

    def __init__(self):
        self.network = PlasticNetwork()
        self.network.load()
        self.performance = PlasticPerformance()
        print(f"[neural] Loaded. Updates: {self.network.updates}, "
              f"LR: {self.network.learning_rate:.6f}")

    def predict_return(self, scan_result: dict) -> float:
        """Return neural expected return for this asset [-1, 1]."""
        x = encode_signal_vector(scan_result)
        return self.network.predict(x)

    def learn(self, scan_result: dict, predicted: float, actual_pnl_pct: float):
        """Update network from a closed trade."""
        x = encode_signal_vector(scan_result)
        target = scale_pnl_to_target(actual_pnl_pct)

        # Add to replay buffer
        self.network.add_to_replay(x, target)

        # Online update
        self.network.learn_from_trade(x, predicted, target)

        # Periodic replay
        if self.network.updates % REPLAY_FREQUENCY == 0:
            self.network.replay()

        # Periodic EWC consolidation — every 50 updates
        if self.network.updates % 50 == 0 and self.network.updates > 0:
            self.network.consolidate()

        # Track performance
        self.performance.record(predicted, target, self.network.updates)

        # Auto-save every 25 updates
        if self.network.updates % 25 == 0:
            self.network.save()
            self.performance.save()

    def stats(self) -> dict:
        return {
            **self.network.stats(),
            "rolling_accuracy": self.performance.recent_accuracy(100),
        }

    def save(self):
        self.network.save()
        self.performance.save()


# ─── Test Harness ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("═" * 56)
    print("  Father Daddy Capital — Neural Plasticity Test")
    print("═" * 56)

    engine = NeuralPlasticityEngine()

    # Simulate a few trades
    np.random.seed(42)
    print(f"\n  Initial state: {engine.stats()['updates']} updates, "
          f"LR: {engine.stats()['learning_rate']:.6f}")
    print(f"  Rolling accuracy: {engine.stats()['rolling_accuracy']:.2%}")

    for i in range(20):
        # Fake scan result
        scan = {
            "signals": {
                "rsi": np.random.uniform(-1, 1),
                "macd": np.random.uniform(-1, 1),
                "trend": np.random.uniform(-1, 1),
                "momentum": np.random.uniform(-1, 1),
                "mean_reversion": np.random.uniform(-1, 1),
            },
            "volatility": np.random.uniform(0.1, 0.5),
            "asset_class": np.random.choice(["crypto", "equity"]),
            "confidence": np.random.uniform(0.4, 0.9),
        }

        pred = engine.predict_return(scan)
        # Simulate outcome with some signal
        actual_pnl = np.random.uniform(-0.08, 0.12)
        engine.learn(scan, pred, actual_pnl)

    stats = engine.stats()
    print(f"\n  After 20 trades:")
    print(f"    Updates:       {stats['updates']}")
    print(f"    Learning rate: {stats['learning_rate']:.6f}")
    print(f"    Avg loss:      {stats['avg_loss']:.4f}")
    print(f"    Accuracy:      {stats['rolling_accuracy']:.2%}")
    print(f"    Weight norms:  {stats['weight_norms']}")
    print(f"    Replay buffer: {stats['replay_buffer_size']}")

    engine.save()
    print(f"\n  Weights saved → {WEIGHTS_FILE}")
    print("═" * 56)
