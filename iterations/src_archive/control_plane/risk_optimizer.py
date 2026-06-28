"""
Risk Optimizer Module
------------------
This module implements risk optimization strategies for the trading system.
"""

import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from src.logger import logger
from src.config import TradingConfig
from src.utils.risk_metrics import calculate_var, calculate_sharpe_ratio
from src.monitoring import monitoring

class RiskOptimizer:
    """Risk optimization and portfolio allocation."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize risk optimizer.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        self.config = config or TradingConfig.load_from_file()
        self.position_limits = self.config.get("position_limits", {})
        self.risk_limits = self.config.get("risk_limits", {})
        self.monitoring = monitoring
        
    def optimize_position_sizes(
        self,
        positions: Dict[str, Dict[str, float]],
        market_data: Dict[str, Dict[str, List[float]]],
        risk_metrics: Dict[str, Dict[str, float]]
    ) -> Dict[str, float]:
        """Optimize position sizes based on risk metrics.
        
        Args:
            positions (Dict[str, Dict[str, float]]): Current positions
            market_data (Dict[str, Dict[str, List[float]]]): Historical market data
            risk_metrics (Dict[str, Dict[str, float]]): Risk metrics per symbol
            
        Returns:
            Dict[str, float]: Optimized position sizes per symbol
        """
        try:
            # Calculate portfolio metrics
            total_value = sum(pos["market_value"] for pos in positions.values())
            current_weights = {
                symbol: pos["market_value"] / total_value
                for symbol, pos in positions.items()
            }
            
            # Calculate risk-adjusted returns
            returns = {}
            volatilities = {}
            sharpe_ratios = {}
            
            for symbol, data in market_data.items():
                returns[symbol] = np.mean(data["returns"])
                volatilities[symbol] = np.std(data["returns"])
                sharpe_ratios[symbol] = calculate_sharpe_ratio(
                    data["returns"],
                    risk_free_rate=self.config.get("risk_free_rate", 0.02)
                )
            
            # Calculate optimal weights using mean-variance optimization
            optimal_weights = self._mean_variance_optimization(
                returns=returns,
                volatilities=volatilities,
                correlations=self._calculate_correlations(market_data),
                risk_aversion=self.config.get("risk_aversion", 2.0)
            )
            
            # Apply position limits
            for symbol in optimal_weights:
                limit = self.position_limits.get(symbol, float("inf"))
                optimal_weights[symbol] = min(optimal_weights[symbol], limit)
            
            # Normalize weights
            total_weight = sum(optimal_weights.values())
            if total_weight > 0:
                optimal_weights = {
                    symbol: weight / total_weight
                    for symbol, weight in optimal_weights.items()
                }
            
            # Calculate target position sizes
            target_positions = {
                symbol: weight * total_value
                for symbol, weight in optimal_weights.items()
            }
            
            # Log optimization results
            self._log_optimization_results(
                current_weights=current_weights,
                optimal_weights=optimal_weights,
                risk_metrics=risk_metrics
            )
            
            return target_positions
            
        except Exception as e:
            logger.error(f"Failed to optimize position sizes: {e}")
            return {symbol: pos["market_value"] for symbol, pos in positions.items()}
    
    def _mean_variance_optimization(
        self,
        returns: Dict[str, float],
        volatilities: Dict[str, float],
        correlations: Dict[Tuple[str, str], float],
        risk_aversion: float
    ) -> Dict[str, float]:
        """Perform mean-variance optimization.
        
        Args:
            returns (Dict[str, float]): Expected returns per symbol
            volatilities (Dict[str, float]): Volatilities per symbol
            correlations (Dict[Tuple[str, str], float]): Correlation matrix
            risk_aversion (float): Risk aversion parameter
            
        Returns:
            Dict[str, float]: Optimal weights per symbol
        """
        try:
            symbols = list(returns.keys())
            n = len(symbols)
            
            # Build covariance matrix
            cov_matrix = np.zeros((n, n))
            for i, sym1 in enumerate(symbols):
                for j, sym2 in enumerate(symbols):
                    if i == j:
                        cov_matrix[i, j] = volatilities[sym1] ** 2
                    else:
                        cov_matrix[i, j] = (
                            volatilities[sym1] *
                            volatilities[sym2] *
                            correlations.get((sym1, sym2), 0)
                        )
            
            # Calculate optimal weights
            returns_vector = np.array([returns[sym] for sym in symbols])
            inv_cov = np.linalg.inv(cov_matrix)
            weights = inv_cov @ returns_vector / (risk_aversion * n)
            
            # Ensure non-negative weights
            weights = np.maximum(weights, 0)
            
            # Normalize weights
            if weights.sum() > 0:
                weights = weights / weights.sum()
            
            return dict(zip(symbols, weights))
            
        except Exception as e:
            logger.error(f"Failed to perform mean-variance optimization: {e}")
            return {symbol: 1.0 / len(returns) for symbol in returns}
    
    def _calculate_correlations(
        self,
        market_data: Dict[str, Dict[str, List[float]]]
    ) -> Dict[Tuple[str, str], float]:
        """Calculate correlation matrix from market data.
        
        Args:
            market_data (Dict[str, Dict[str, List[float]]]): Historical market data
            
        Returns:
            Dict[Tuple[str, str], float]: Correlation matrix
        """
        try:
            symbols = list(market_data.keys())
            correlations = {}
            
            for i, sym1 in enumerate(symbols):
                for j, sym2 in enumerate(symbols[i:], i):
                    if sym1 == sym2:
                        correlations[(sym1, sym2)] = 1.0
                    else:
                        corr = np.corrcoef(
                            market_data[sym1]["returns"],
                            market_data[sym2]["returns"]
                        )[0, 1]
                        correlations[(sym1, sym2)] = corr
                        correlations[(sym2, sym1)] = corr
            
            return correlations
            
        except Exception as e:
            logger.error(f"Failed to calculate correlations: {e}")
            return {}
    
    def _log_optimization_results(
        self,
        current_weights: Dict[str, float],
        optimal_weights: Dict[str, float],
        risk_metrics: Dict[str, Dict[str, float]]
    ) -> None:
        """Log optimization results.
        
        Args:
            current_weights (Dict[str, float]): Current portfolio weights
            optimal_weights (Dict[str, float]): Optimal portfolio weights
            risk_metrics (Dict[str, Dict[str, float]]): Risk metrics per symbol
        """
        try:
            # Calculate weight changes
            changes = {
                symbol: optimal_weights.get(symbol, 0) - current_weights.get(symbol, 0)
                for symbol in set(optimal_weights) | set(current_weights)
            }
            
            # Log significant changes
            for symbol, change in changes.items():
                if abs(change) > 0.05:  # 5% threshold
                    logger.info(
                        f"Significant weight change for {symbol}: "
                        f"{current_weights.get(symbol, 0):.2%} -> "
                        f"{optimal_weights.get(symbol, 0):.2%}"
                    )
                    
                    # Record metrics
                    self.monitoring.record_metric(
                        f"weight_change_{symbol}",
                        change
                    )
                    
                    if symbol in risk_metrics:
                        metrics = risk_metrics[symbol]
                        logger.info(
                            f"Risk metrics for {symbol}: "
                            f"VaR={metrics.get('var', 0):.2%}, "
                            f"Sharpe={metrics.get('sharpe', 0):.2f}"
                        )
            
        except Exception as e:
            logger.error(f"Failed to log optimization results: {e}") 