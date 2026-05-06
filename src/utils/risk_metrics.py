"""
Risk metric helpers.

The functions in this module are intentionally dependency-light and deterministic
because they are used during import by multiple agents.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np


def _as_returns_array(returns: Iterable[float]) -> np.ndarray:
    values = np.asarray(list(returns), dtype=float)
    if values.size == 0:
        return np.array([], dtype=float)
    return values[~np.isnan(values)]


def calculate_var(returns: Iterable[float], confidence_level: float = 0.95) -> float:
    """Calculate historical Value at Risk as a return threshold."""
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be between 0 and 1")

    values = _as_returns_array(returns)
    if values.size == 0:
        return 0.0

    percentile = (1.0 - confidence_level) * 100.0
    return round(float(np.percentile(values, percentile)), 12)


def calculate_cvar(returns: Iterable[float], confidence_level: float = 0.95) -> float:
    """Calculate historical Conditional Value at Risk."""
    values = _as_returns_array(returns)
    if values.size == 0:
        return 0.0

    var = calculate_var(values, confidence_level)
    tail = values[values <= var]
    if tail.size == 0:
        return var
    return float(np.mean(tail))


def calculate_sharpe_ratio(
    returns: Iterable[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Calculate annualized Sharpe ratio from periodic returns."""
    values = _as_returns_array(returns)
    if values.size < 2:
        return 0.0

    excess = values - (risk_free_rate / periods_per_year)
    volatility = np.std(excess, ddof=1)
    if volatility == 0:
        return 0.0
    return float(np.mean(excess) / volatility * np.sqrt(periods_per_year))


class RiskMetrics:
    """Compatibility wrapper for older call sites."""

    @staticmethod
    def calculate_var(returns: Sequence[float], confidence_level: float = 0.95) -> float:
        return calculate_var(returns, confidence_level)

    @staticmethod
    def calculate_cvar(returns: Sequence[float], confidence_level: float = 0.95) -> float:
        return calculate_cvar(returns, confidence_level)

    @staticmethod
    def calculate_sharpe_ratio(
        returns: Sequence[float],
        risk_free_rate: float = 0.0,
        periods_per_year: int = 252,
    ) -> float:
        return calculate_sharpe_ratio(returns, risk_free_rate, periods_per_year)
