#!/usr/bin/env python3
"""
FDC Backtesting Harness
Extracted from Microsoft qlib backtesting architecture.
Provides exchange simulation, account tracking, and generator-based event loops.

qlib pattern: Exchange → Account → Executor → backtest_loop(Strategy)
FDC version: simpler, zero-dependency, designed for crypto/Polymarket paper testing.

Key concepts stolen:
  - Exchange: decouples deal_price logic from strategy (qlib supports $close, $vwap, $open)
  - Account: tracks cash, positions, costs separately from decision logic
  - Generator loop: yields trade decisions per step (collect_data_loop pattern)
  - Config-driven execution: all params in one dict, no class hierarchy

Usage:
    from fdc_backtest import BacktestEngine
    engine = BacktestEngine(initial_capital=100000)
    engine.run(strategy_fn, price_data, start_date, end_date)

Author: Hugh (3rd of 5)
Source: microsoft/qlib backtest architecture
Date: 2026-05-15
"""

from __future__ import annotations
from typing import Callable, Generator, Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import math


# ══════════════════════════════════════════════════════════════════════════════
# Enums & Types
# ══════════════════════════════════════════════════════════════════════════════

class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(Enum):
    MARKET = "MARKET"     # Fill at current price
    LIMIT = "LIMIT"       # Fill only at/above bid or at/below ask
    STOP = "STOP"         # Triggered when price crosses threshold

class DealPrice(Enum):
    """How to determine the fill price for an order. From qlib exchange.py."""
    CLOSE = "$close"      # Use period close price
    OPEN = "$open"        # Use period open price
    VWAP = "$vwap"        # Volume-weighted average price
    NEXT_OPEN = "$next_open"  # Fill at next period's open (realistic for daily)

class FillModel(Enum):
    """How orders interact with available liquidity."""
    FULL = "full"         # Always fill completely (paper default)
    PROPORTIONAL = "proportional"  # Fill proportional to volume
    LIMIT_BOOK = "limit_book"      # Walk orderbook depth (uses fdc_orderbook)


# ══════════════════════════════════════════════════════════════════════════════
# Data Structures
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Order:
    """Order request from strategy to exchange."""
    symbol: str
    side: OrderSide
    size: float           # Number of shares/contracts
    price: Optional[float] = None  # None = market order
    order_type: OrderType = OrderType.MARKET
    timestamp: Optional[datetime] = None
    meta: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Fill:
    """Confirmed fill from exchange."""
    order: Order
    fill_price: float
    fill_size: float       # May be less than ordered
    cost: float             # Commission + slippage
    timestamp: datetime
    order_id: int = 0

@dataclass
class Position:
    """Holding in a single instrument."""
    symbol: str
    shares: float
    avg_price: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Exchange (from qlib/exchange.py)
# ══════════════════════════════════════════════════════════════════════════════

class Exchange:
    """
    Simulates exchange behavior: deal price logic, commission, limits.
    
    qlib pattern: Exchange is separate from Account. It determines
    what price orders fill at, applies costs, enforces trading limits.
    The strategy never talks to Exchange directly — the Executor mediates.
    """

    def __init__(
        self,
        deal_price: DealPrice = DealPrice.CLOSE,
        commission_pct: float = 0.001,    # 0.1% per trade
        min_commission: float = 1.0,       # Minimum commission
        slippage_pct: float = 0.0005,      # 0.05% slippage
        fill_model: FillModel = FillModel.FULL,
        max_position_pct: float = 0.25,    # Max 25% of capital in one position
        price_impact_factor: float = 0.0,  # How much our trade moves the price
    ):
        self.deal_price = deal_price
        self.commission_pct = commission_pct
        self.min_commission = min_commission
        self.slippage_pct = slippage_pct
        self.fill_model = fill_model
        self.max_position_pct = max_position_pct
        self.price_impact_factor = price_impact_factor
        self._order_counter = 0

    def get_deal_price(
        self, bar: Dict[str, float], side: OrderSide
    ) -> float:
        """Get the fill price for an order at this bar. From qlib deal_price logic."""
        if self.deal_price == DealPrice.CLOSE:
            price = bar.get("close", bar.get("Close", 0))
        elif self.deal_price == DealPrice.OPEN:
            price = bar.get("open", bar.get("Open", 0))
        elif self.deal_price == DealPrice.VWAP:
            price = bar.get("vwap", bar.get("VWAP", bar.get("close", 0)))
        elif self.deal_price == DealPrice.NEXT_OPEN:
            price = bar.get("next_open", bar.get("open", bar.get("Open", 0)))
        else:
            price = bar.get("close", bar.get("Close", 0))

        # Apply slippage: buy at slightly higher, sell at slightly lower
        if side == OrderSide.BUY:
            price *= (1 + self.slippage_pct)
        else:
            price *= (1 - self.slippage_pct)

        return price

    def compute_commission(self, value: float) -> float:
        """Compute trade commission. From qlib exchange open_cost/close_cost."""
        return max(self.min_commission, value * self.commission_pct)

    def submit_order(
        self,
        order: Order,
        bar: Dict[str, float],
        volume: float = 0,
    ) -> Fill:
        """Process an order through the exchange. Returns a fill."""
        self._order_counter += 1

        # Decide fill price
        if order.price is not None and order.order_type == OrderType.LIMIT:
            # Limit order: only fill if price is favorable
            deal_price = self.get_deal_price(bar, order.side)
            if (order.side == OrderSide.BUY and deal_price > order.price) or \
               (order.side == OrderSide.SELL and deal_price < order.price):
                # Limit order not executable at this price
                return Fill(
                    order=order, fill_price=order.price,
                    fill_size=0, cost=0,
                    timestamp=order.timestamp or datetime.now(),
                    order_id=self._order_counter,
                )
            fill_price = order.price
        else:
            fill_price = self.get_deal_price(bar, order.side)

        # Decide fill size
        if self.fill_model == FillModel.FULL:
            fill_size = order.size
        elif self.fill_model == FillModel.PROPORTIONAL and volume > 0:
            # Fill proportional to available volume
            max_fill = volume * 0.10  # Can't exceed 10% of period volume
            fill_size = min(order.size, max_fill)
        else:
            fill_size = order.size

        # Apply price impact (large orders move the market)
        if self.price_impact_factor > 0:
            impact = fill_size * self.price_impact_factor
            if order.side == OrderSide.BUY:
                fill_price *= (1 + impact)
            else:
                fill_price *= (1 - impact)

        gross_value = fill_size * fill_price
        commission = self.compute_commission(gross_value)
        net_cost = gross_value + commission if order.side == OrderSide.BUY else gross_value - commission

        return Fill(
            order=order, fill_price=fill_price, fill_size=fill_size,
            cost=commission, timestamp=order.timestamp or datetime.now(),
            order_id=self._order_counter,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Account (from qlib/account.py)
# ══════════════════════════════════════════════════════════════════════════════

class Account:
    """
    Tracks cash, positions, and P&L. Separate from strategy logic.
    qlib pattern: Account holds position value, cash, frozen cash.
    Updates happen through apply_fill() — the Executor calls this.
    """

    def __init__(self, initial_capital: float = 100_000.0):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Position] = {}
        self.total_commission: float = 0.0
        self.total_realized_pnl: float = 0.0
        self.fill_history: List[Fill] = []
        self.equity_curve: List[Dict[str, Any]] = []

    def apply_fill(self, fill: Fill):
        """Update account state after a fill. From qlib account position updates."""
        symbol = fill.order.symbol
        self.fill_history.append(fill)

        if fill.fill_size == 0:
            return  # No fill

        gross = fill.fill_size * fill.fill_price
        self.total_commission += fill.cost

        if fill.order.side == OrderSide.BUY:
            # Add to position
            if symbol in self.positions:
                pos = self.positions[symbol]
                # Weighted average price
                total_cost = pos.shares * pos.avg_price + gross
                pos.shares += fill.fill_size
                pos.avg_price = total_cost / pos.shares if pos.shares > 0 else 0
            else:
                self.positions[symbol] = Position(
                    symbol=symbol, shares=fill.fill_size, avg_price=fill.fill_price
                )
            self.cash -= (gross + fill.cost)

        else:  # SELL
            if symbol not in self.positions:
                return  # Can't sell what we don't have

            pos = self.positions[symbol]
            realized_pnl = fill.fill_size * (fill.fill_price - pos.avg_price)
            pos.realized_pnl += realized_pnl
            self.total_realized_pnl += realized_pnl
            pos.shares -= fill.fill_size
            self.cash += (gross - fill.cost)

            if pos.shares <= 0:
                del self.positions[symbol]

    def mark_to_market(self, bar: Dict[str, float]):
        """Update unrealized P&L from current prices. From qlib end-of-period valuation."""
        unrealized = 0.0
        for symbol, pos in self.positions.items():
            current_price = bar.get("close", bar.get("Close", 0))
            pos.unrealized_pnl = pos.shares * (current_price - pos.avg_price)
            unrealized += pos.unrealized_pnl

        total_equity = self.cash + sum(
            p.shares * (bar.get("close", p.avg_price)) for p in self.positions.values()
        )
        self.equity_curve.append({
            "timestamp": datetime.now().isoformat(),
            "equity": round(total_equity, 2),
            "cash": round(self.cash, 2),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl": round(self.total_realized_pnl, 2),
            "positions": len(self.positions),
        })

        return total_equity

    def total_equity(self, bar: Dict[str, float]) -> float:
        """Current total equity = cash + position values."""
        position_value = sum(
            p.shares * bar.get("close", p.avg_price)
            for p in self.positions.values()
        )
        return self.cash + position_value

    def metrics(self) -> dict:
        """Performance metrics. From qlib indicator/report calculations."""
        if len(self.equity_curve) < 2:
            return {"return": 0, "sharpe": 0, "max_drawdown": 0}

        returns = []
        peak = self.initial_capital
        max_dd = 0.0

        for i in range(1, len(self.equity_curve)):
            prev = self.equity_curve[i-1]["equity"]
            curr = self.equity_curve[i]["equity"]
            if prev > 0:
                returns.append(curr / prev - 1)
            peak = max(peak, curr)
            dd = (peak - curr) / peak
            max_dd = max(max_dd, dd)

        total_return = (self.equity_curve[-1]["equity"] / self.initial_capital - 1) * 100

        if returns:
            import statistics
            mean_ret = statistics.mean(returns)
            std_ret = statistics.stdev(returns) if len(returns) > 1 else 1e-9
            sharpe = (mean_ret / std_ret) * math.sqrt(252) if std_ret > 0 else 0  # annualized
        else:
            sharpe = 0

        return {
            "total_return_pct": round(total_return, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "total_trades": len(self.fill_history),
            "total_commission": round(self.total_commission, 2),
            "final_equity": round(self.equity_curve[-1]["equity"], 2) if self.equity_curve else self.initial_capital,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Backtest Engine (from qlib backtest_loop + collect_data_loop)
# ══════════════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    Main backtest runner. Generator-based like qlib's collect_data_loop.
    
    qlib pattern:
        for decision in collect_data_loop(start, end, strategy, executor):
            # decision is yielded at each time step
            # executor handles order submission internally
            pass
    
    Our version: simpler feed-forward loop, no nested decision execution.
    """

    def __init__(
        self,
        initial_capital: float = 100_000.0,
        exchange: Optional[Exchange] = None,
        verbose: bool = True,
    ):
        self.account = Account(initial_capital=initial_capital)
        self.exchange = exchange or Exchange()
        self.verbose = verbose

    def run(
        self,
        strategy_fn: Callable[[Account, Dict[str, float], int], List[Order]],
        price_data: Dict[str, List[Dict[str, float]]],
        start_idx: int = 0,
        end_idx: Optional[int] = None,
    ) -> dict:
        """
        Run a strategy against price data.

        Args:
            strategy_fn: (account, bar, step) → List[Order]
                Called at each time step. Returns orders to execute.
            price_data: {symbol: [{open, high, low, close, volume}, ...]}
            start_idx, end_idx: slice of data to run

        Returns:
            Performance metrics dict
        """
        # Determine time range
        first_symbol = next(iter(price_data.keys()))
        bars = price_data[first_symbol]
        if end_idx is None:
            end_idx = len(bars)

        for step in range(start_idx, end_idx):
            # Build current bar for all symbols
            bar = {}
            for symbol, series in price_data.items():
                if step < len(series):
                    bar[symbol] = series[step]
                else:
                    bar[symbol] = {}

            # Call strategy
            try:
                orders = strategy_fn(self.account, bar, step)
            except Exception as e:
                if self.verbose:
                    print(f"Strategy error at step {step}: {e}")
                orders = []

            # Submit orders through exchange
            for order in orders:
                symbol_bar = bar.get(order.symbol, {})
                volume = symbol_bar.get("volume", symbol_bar.get("Volume", 0))
                fill = self.exchange.submit_order(order, symbol_bar, volume)
                self.account.apply_fill(fill)

            # Mark to market
            # Use first symbol's bar for total equity snapshot
            snapshot_bar = bar.get(next(iter(price_data.keys())), {})
            self.account.mark_to_market(snapshot_bar)

            if self.verbose and step % 50 == 0:
                eq = self.account.total_equity(snapshot_bar)
                print(f"  Step {step}/{end_idx}: Equity ${eq:,.2f} | "
                      f"Positions: {len(self.account.positions)}")

        return self.account.metrics()

    def run_generator(
        self,
        strategy_fn: Callable[[Account, Dict[str, float], int], List[Order]],
        price_data: Dict[str, List[Dict[str, float]]],
        start_idx: int = 0,
    ) -> Generator:
        """
        Generator version — yields (step, bar, orders, account) at each step.
        From qlib's collect_data_loop pattern: allows external code to inspect
        or modify state mid-backtest.
        """
        first_symbol = next(iter(price_data.keys()))
        bars = price_data[first_symbol]

        for step in range(start_idx, len(bars)):
            bar = {}
            for symbol, series in price_data.items():
                bar[symbol] = series[step] if step < len(series) else {}

            orders = strategy_fn(self.account, bar, step)
            for order in orders:
                symbol_bar = bar.get(order.symbol, {})
                fill = self.exchange.submit_order(order, symbol_bar)
                self.account.apply_fill(fill)

            self.account.mark_to_market(bar.get(first_symbol, {}))
            yield step, bar, orders, self.account


# ══════════════════════════════════════════════════════════════════════════════
# Example / Smoke Test
# ══════════════════════════════════════════════════════════════════════════════

def _example_strategy(account: Account, bar, step: int) -> List[Order]:
    """Simple 20-SMA crossover strategy. BUY/SELL on crossover signals."""
    orders = []
    price = bar.get("default", {}).get("close", 0)
    if price == 0:
        return orders

    # Track last price for crossover detection
    if not hasattr(_example_strategy, "_last_price"):
        _example_strategy._last_price = price
        return orders

    prev = _example_strategy._last_price
    _example_strategy._last_price = price

    # Buy on crossover up, sell on crossover down
    if price > prev and account.cash > 1000:
        size = int(account.cash * 0.20 / price)
        if size > 0:
            orders.append(Order(symbol="default", side=OrderSide.BUY, size=size))
    elif price < prev:
        pos = account.positions.get("default")
        if pos and pos.shares > 0:
            orders.append(Order(symbol="default", side=OrderSide.SELL, size=pos.shares))

    return orders


if __name__ == "__main__":
    import random

    print("=== FDC Backtesting Harness (qlib pattern) ===\n")

    # Generate mock price data (100 bars of geometric random walk)
    random.seed(42)
    prices = [100.0]
    for _ in range(100):
        prices.append(prices[-1] * (1 + random.gauss(0.001, 0.02)))
    
    price_data = {"default": [
        {"open": p, "high": p*1.005, "low": p*0.995, "close": p, "volume": 10000}
        for p in prices[1:]
    ]}

    # Full mode
    engine = BacktestEngine(initial_capital=100_000)
    metrics = engine.run(_example_strategy, price_data)
    print("\nFull backtest:")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    # Generator mode
    print("\nGenerator mode (first 5 steps):")
    engine2 = BacktestEngine(initial_capital=100_000, verbose=False)
    gen = engine2.run_generator(_example_strategy, price_data)
    for i, (step, bar, orders, acct) in enumerate(gen):
        print(f"  Step {step}: equity=${acct.total_equity(bar):,.2f}, "
              f"orders={len(orders)}, positions={len(acct.positions)}")
        if i >= 4:
            break
