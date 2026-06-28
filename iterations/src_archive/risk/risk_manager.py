"""
Deterministic risk manager for paper-trading and agent gating.

This replaces the previous import-time coupled implementation with a small
runtime contract that agents can call safely.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from src.config import TradingConfig

logger = logging.getLogger(__name__)


class RiskManager:
    """Core risk checks used before signal execution."""

    def __init__(self, config: Optional[Dict[str, Any] | TradingConfig] = None):
        base_config = TradingConfig.load_from_file()
        overrides = config if isinstance(config, dict) else {}
        config_obj = config if isinstance(config, TradingConfig) else base_config

        self.max_position_size = float(overrides.get("max_position_size", config_obj.MAX_POSITION_SIZE))
        self.max_leverage = float(overrides.get("max_leverage", config_obj.MAX_LEVERAGE))
        self.max_drawdown = float(overrides.get("max_drawdown", config_obj.MAX_DRAWDOWN))
        self.max_daily_trades = int(overrides.get("max_daily_trades", config_obj.MAX_DAILY_TRADES))
        self.max_daily_risk = float(overrides.get("max_daily_risk", config_obj.MAX_DAILY_LOSS))
        self.min_confidence = float(overrides.get("min_confidence", 0.0))
        self.initial_cash = float(config_obj.trading.get("portfolio", {}).get("initial_cash", 100000.0))

        self.positions: Dict[str, Dict[str, float]] = {}
        self.trades = []
        self.reset_daily_metrics()

    async def start(self) -> None:
        logger.info("Risk manager started")

    async def stop(self) -> None:
        logger.info("Risk manager stopped")

    def reset_daily_metrics(self) -> None:
        self.risk_metrics = {
            "trades_count": 0,
            "total_risk": 0.0,
            "daily_pnl": 0.0,
            "max_drawdown": 0.0,
            "var_95": 0.0,
            "position_concentration": 0.0,
        }

    def validate_signal(
        self,
        signal: Any,
        positions: Dict[str, Any],
        portfolio_value: float,
    ) -> bool:
        """Return whether a signal is eligible for sizing/execution."""
        payload = self._signal_payload(signal)
        symbol = payload.get("symbol")
        side = str(payload.get("signal", "")).upper()
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        price = float(payload.get("price", 0.0) or 0.0)

        if not symbol or side not in {"BUY", "SELL"}:
            return False
        if confidence < self.min_confidence:
            return False
        if price <= 0 or portfolio_value <= 0:
            return False
        if self.risk_metrics["trades_count"] >= self.max_daily_trades:
            return False
        if self._current_drawdown(portfolio_value) > self.max_drawdown:
            return False
        if side == "SELL" and symbol not in positions:
            return False

        return True

    def calculate_position_size(
        self,
        signal: Any,
        portfolio_value: float,
        positions: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Calculate base-asset quantity from portfolio risk budget."""
        payload = self._signal_payload(signal)
        price = float(payload.get("price", 0.0) or 0.0)
        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.0) or 0.0)))
        if price <= 0 or portfolio_value <= 0:
            return 0.0

        risk_budget = portfolio_value * self.max_position_size * confidence
        quantity = risk_budget / price

        if positions and payload.get("symbol") in positions and str(payload.get("signal", "")).upper() == "SELL":
            current_position = positions[payload["symbol"]]
            current_qty = current_position.get("qty", current_position) if isinstance(current_position, dict) else current_position
            quantity = min(quantity, float(current_qty))

        return round(max(0.0, quantity), 8)

    def can_place_order(self, symbol: str, qty: float, price: float, side: str) -> bool:
        if not symbol or qty <= 0 or price <= 0:
            return False
        if side.lower() not in {"buy", "sell"}:
            return False

        order_value = qty * price
        max_order_value = self.initial_cash * self.max_position_size * self.max_leverage
        if order_value > max_order_value:
            return False

        projected_risk = self.risk_metrics["total_risk"] + order_value
        if projected_risk > self.initial_cash * self.max_daily_risk:
            return False

        return self.risk_metrics["trades_count"] < self.max_daily_trades

    def update_position(self, symbol: str, quantity: float, price: float, side: str = "buy") -> None:
        signed_quantity = quantity if side.lower() == "buy" else -quantity
        current = self.positions.get(symbol, {"qty": 0.0, "avg_price": price})
        new_qty = current["qty"] + signed_quantity
        if new_qty <= 0:
            self.positions.pop(symbol, None)
        else:
            current_value = current["qty"] * current["avg_price"]
            added_value = max(0.0, signed_quantity) * price
            avg_price = (current_value + added_value) / new_qty if signed_quantity > 0 else current["avg_price"]
            self.positions[symbol] = {"qty": new_qty, "avg_price": avg_price}

    def record_trade(self, symbol: str, quantity: float, price: float, pnl: float = 0.0, side: str = "buy") -> None:
        trade = {
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": symbol,
            "qty": quantity,
            "price": price,
            "side": side,
            "pnl": pnl,
        }
        self.trades.append(trade)
        self.risk_metrics["trades_count"] += 1
        self.risk_metrics["total_risk"] += abs(quantity * price)
        self.risk_metrics["daily_pnl"] += pnl
        self.risk_metrics["max_drawdown"] = min(self.risk_metrics["max_drawdown"], self.risk_metrics["daily_pnl"])

    def get_position_metrics(self, symbol: str) -> Dict[str, Any]:
        return self.positions.get(symbol, {})

    def get_daily_metrics(self) -> Dict[str, Any]:
        return {**self.risk_metrics, "positions_count": len(self.positions)}

    def get_risk_report(self) -> Dict[str, Any]:
        return {
            "daily_metrics": self.get_daily_metrics(),
            "positions": self.positions,
            "risk_limits": {
                "max_position_size": self.max_position_size,
                "max_leverage": self.max_leverage,
                "max_drawdown": self.max_drawdown,
                "max_daily_trades": self.max_daily_trades,
                "max_daily_risk": self.max_daily_risk,
            },
        }

    @staticmethod
    def _signal_payload(signal: Any) -> Dict[str, Any]:
        if isinstance(signal, dict):
            return signal
        payload = {
            "symbol": getattr(signal, "symbol", None),
            "confidence": getattr(signal, "confidence", None),
            "price": getattr(signal, "price", None),
        }
        signal_value = getattr(signal, "signal", None)
        payload["signal"] = getattr(signal_value, "value", signal_value)
        return payload

    def _current_drawdown(self, portfolio_value: float) -> float:
        if self.initial_cash <= 0:
            return 0.0
        return max(0.0, (self.initial_cash - portfolio_value) / self.initial_cash)


risk_manager = RiskManager()
