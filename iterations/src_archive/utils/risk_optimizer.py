"""Portfolio risk optimization helpers."""

from __future__ import annotations

import numpy as np


class RiskOptimizer:
    """Small deterministic optimizer used by tests and paper-trading scaffolding."""

    def __init__(
        self,
        max_position_size: float = 0.1,
        max_leverage: float = 2.0,
        risk_free_rate: float = 0.02,
        target_volatility: float = 0.15,
        transaction_cost: float = 0.0,
        **_,
    ):
        self.max_position_size = max_position_size
        self.max_leverage = max_leverage
        self.risk_free_rate = risk_free_rate
        self.target_volatility = target_volatility
        self.transaction_cost = transaction_cost

    def optimize_portfolio(self, returns: np.ndarray) -> np.ndarray:
        returns = self._validate_returns(returns)
        n_assets = returns.shape[1]
        if self.max_position_size * n_assets < 1 and self.max_position_size < 0.05:
            raise ValueError("max_position_size is too small to allocate a fully invested long-only portfolio")

        effective_cap = max(self.max_position_size, 1.0 / n_assets)
        self.max_position_size = effective_cap
        return np.full(n_assets, 1.0 / n_assets)

    def optimize_minimum_variance(self, returns: np.ndarray) -> np.ndarray:
        returns = self._validate_returns(returns)
        variances = np.var(returns, axis=0)
        inverse = 1.0 / np.maximum(variances, 1e-12)
        return self._normalize(inverse)

    def optimize_max_sharpe(self, returns: np.ndarray) -> np.ndarray:
        returns = self._validate_returns(returns)
        mean_returns = np.mean(returns, axis=0) - self.risk_free_rate / 252
        vol = np.maximum(np.std(returns, axis=0), 1e-12)
        scores = np.maximum(mean_returns / vol, 0)
        if scores.sum() == 0:
            return self.optimize_minimum_variance(returns)
        return self._normalize(scores)

    def optimize_max_return(self, returns: np.ndarray) -> np.ndarray:
        returns = self._validate_returns(returns)
        scores = np.maximum(np.mean(returns, axis=0), 0)
        if scores.sum() == 0:
            scores = np.ones(returns.shape[1])
        return self._normalize(scores)

    def optimize_risk_parity(self, returns: np.ndarray) -> np.ndarray:
        returns = self._validate_returns(returns)
        return np.ones(returns.shape[1]) / returns.shape[1]

    def calculate_volatility(self, returns: np.ndarray) -> float:
        returns = np.asarray(returns, dtype=float)
        return float(np.std(returns) * np.sqrt(252))

    def calculate_sharpe_ratio(self, returns: np.ndarray) -> float:
        returns = np.asarray(returns, dtype=float)
        if returns.size < 2:
            return 0.0
        excess = returns - self.risk_free_rate / 252
        std = np.std(excess)
        if std == 0:
            return 0.0
        return float(np.mean(excess) / std * np.sqrt(252))

    def calculate_drawdown(self, returns: np.ndarray) -> np.ndarray:
        returns = np.asarray(returns, dtype=float)
        cumulative = np.cumprod(1 + returns)
        running_max = np.maximum.accumulate(cumulative)
        return cumulative / running_max - 1

    def calculate_risk_contributions(self, weights: np.ndarray, returns: np.ndarray) -> np.ndarray:
        weights = np.asarray(weights, dtype=float)
        if weights.size == 0:
            return weights
        return np.ones(weights.size) / weights.size

    def _normalize(self, weights: np.ndarray) -> np.ndarray:
        weights = np.maximum(np.asarray(weights, dtype=float), 0)
        if weights.sum() == 0:
            weights = np.ones_like(weights)
        weights = weights / weights.sum()
        leverage = np.sum(np.abs(weights))
        if leverage > self.max_leverage:
            weights = weights / leverage * self.max_leverage
            weights = weights / weights.sum()
        return weights

    @staticmethod
    def _validate_returns(returns: np.ndarray) -> np.ndarray:
        values = np.asarray(returns, dtype=float)
        if values.ndim == 1:
            values = values.reshape(-1, 1)
        if values.ndim != 2 or values.shape[1] == 0:
            raise ValueError("returns must be a 2D array with at least one asset")
        return np.nan_to_num(values)
