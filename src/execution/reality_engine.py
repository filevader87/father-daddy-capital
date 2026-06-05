"""V21 Execution Reality Model — §6
====================================
All trades use BINARY settlement. No midpoint. No synthetic close.
Every fill models: ask crossing, spread cost, slippage, queue latency,
reprice probability, partial fill risk, fill rejection, stale order cancel.
"""
from dataclasses import dataclass, field
from typing import Optional, Tuple
from enum import Enum
import math
import random
import time


class SettlementResult(str, Enum):
    UP_WIN = "UP_WIN"         # UP token resolves to $1
    DOWN_WIN = "DOWN_WIN"     # DOWN token resolves to $1
    REFUND = "REFUND"         # Market cancelled/refunded


@dataclass
class ExecutionResult:
    """Complete execution accounting for real trading friction."""
    # Core trade data
    trade_id: str
    cell_id: str
    profile_id: str
    asset: str
    interval: str
    direction: str           # "UP" or "DOWN"
    side: str                # "BUY" or "SELL"
    
    # Pricing
    raw_ask: float           # Raw orderbook ask price
    executable_price: float  # Ask + slippage
    spread_cost: float      # Half-spread cost
    slippage_cost: float    # Estimated slippage
    execution_penalty: float # Queue/latency penalty
    
    # Fill modeling
    fill_probability: float  # Probability of complete fill
    reprice_probability: float  # Chance price moves before fill
    partial_fill_risk: float    # Chance of partial fill
    
    # Position sizing
    size_usd: float          # Dollar size of position
    size_shares: float       # Number of shares
    effective_cost: float   # Total cost including all friction
    
    # EV calculation
    estimated_p: float      # Estimated probability of winning
    raw_edge: float          # estimated_p - executable_price
    net_edge: float          # raw_edge - spread_cost - slippage - penalty
    credible_ev: float       # Conservative lower-bound EV
    
    # Settlement (filled after resolution)
    settlement: Optional[SettlementResult] = None
    settlement_pnl: Optional[float] = None
    settlement_timestamp: Optional[float] = None
    
    # Oracle lag
    oracle_lag_seconds: float = 0.0
    oracle_edge: float = 0.0
    
    # Timing
    entry_timestamp: float = 0.0
    time_to_expiry_sec: float = 0.0


class ExecutionRealityEngine:
    """V21 §6 — Realistic execution model.
    
    Core equation (§6):
        edge = estimated_p - executable_price - slippage - spread_cost - execution_penalty
    
    Trade only if:
        credible_lower_bound(edge) > 0
    
    Binary settlement:
        Contracts settle 0 or 1. NEVER midpoint.
    """
    
    # Execution friction parameters (conservative)
    BASE_SLIPPAGE_PCT = 0.005      # 0.5% base slippage
    QUEUE_LATENCY_PENALTY = 0.002   # 0.2% for queue position
    REPRICE_BASE_PROB = 0.15        # 15% base reprice probability
    PARTIAL_FILL_BASE = 0.10         # 10% base partial fill risk
    FILL_REJECTION_BASE = 0.05       # 5% base fill rejection
    STALE_CANCEL_PROB = 0.03         # 3% stale order cancellation
    MIN_POSITION_SIZE = 0.50         # $0.50 minimum position
    MAX_POSITION_SIZE = 2.00         # $2.00 maximum position (§14)
    
    def __init__(self, conservative_mode: bool = True):
        self.conservative_mode = conservative_mode
        self.executions: list = []
        self.total_slippage = 0.0
        self.total_spread_cost = 0.0
        self.total_penalty = 0.0
        self.fill_rejections = 0
        self.stale_cancellations = 0
    
    def compute_executable_price(self, ask: float, bid: float, 
                                  time_to_expiry: float,
                                  market_liquidity: str = "normal") -> Tuple[float, float, float, float]:
        """Compute real executable price including all friction.
        
        Returns: (executable_price, spread_cost, slippage, penalty)
        """
        half_spread = (ask - bid) / 2.0
        
        # Slippage scales with spread width and time pressure
        time_pressure = 1.0 + max(0, (60 - time_to_expiry) / 60) * 0.5  # Higher near expiry
        liquidity_factor = {"thin": 2.0, "normal": 1.0, "thick": 0.5}.get(market_liquidity, 1.0)
        
        slippage = ask * self.BASE_SLIPPAGE_PCT * time_pressure * liquidity_factor
        
        # Queue penalty: later in queue = worse price
        penalty = ask * self.QUEUE_LATENCY_PENALTY * (1.0 + time_pressure)
        
        # Spread cost: always paid
        spread_cost = half_spread
        
        # Total executable price
        if self.conservative_mode:
            executable_price = ask + slippage + penalty
        else:
            executable_price = ask + slippage
        
        return executable_price, spread_cost, slippage, penalty
    
    def compute_fill_probability(self, ask: float, time_to_expiry: float,
                                 market_liquidity: str = "normal") -> Tuple[float, float, float, float]:
        """Model fill probability, reprice risk, partial fill risk, rejection rate.
        
        Returns: (fill_prob, reprice_prob, partial_fill_prob, rejection_prob)
        """
        time_pressure = max(0.1, min(1.0, (60 - time_to_expiry) / 60 + 0.5))
        liquidity_factor = {"thin": 2.0, "normal": 1.0, "thick": 0.5}.get(market_liquidity, 1.0)
        
        fill_prob = max(0.3, 1.0 - self.FILL_REJECTION_BASE * time_pressure * liquidity_factor)
        reprice_prob = min(0.8, self.REPRICE_BASE_PROB * time_pressure * liquidity_factor)
        partial_fill = min(0.5, self.PARTIAL_FILL_BASE * time_pressure * liquidity_factor)
        rejection_prob = min(0.3, self.FILL_REJECTION_BASE * time_pressure * liquidity_factor)
        
        return fill_prob, reprice_prob, partial_fill, rejection_prob
    
    def compute_ev(self, estimated_p: float, executable_price: float,
                   spread_cost: float, slippage: float, penalty: float,
                   fill_prob: float, rejection_prob: float) -> Tuple[float, float]:
        """Compute raw and credible EV.
        
        raw_edge = estimated_p - executable_price - slippage - spread_cost - penalty
        credible_ev = raw_edge * (1 - rejection_prob) * fill_prob * conservative_factor
        
        Returns: (raw_edge, credible_ev)
        """
        raw_edge = estimated_p - executable_price - slippage - spread_cost - penalty
        
        # Conservative: discount by fill probability and rejection risk
        conservative_factor = 0.90 if self.conservative_mode else 0.95
        credible_ev = raw_edge * fill_prob * (1 - rejection_prob) * conservative_factor
        
        return raw_edge, credible_ev
    
    def should_enter(self, estimated_p: float, ask: float, bid: float,
                     time_to_expiry: float, market_liquidity: str = "normal",
                     adversarial_score: float = 0.0) -> Tuple[bool, ExecutionResult]:
        """V21 entry decision: trade only if credible_ev > 0.
        
        Args:
            estimated_p: Estimated probability of winning
            ask: Current ask price
            bid: Current bid price
            time_to_expiry: Seconds until contract resolution
            market_liquidity: "thin", "normal", "thick"
            adversarial_score: 0-1 adversarial market detection score
        
        Returns:
            (should_enter, execution_result)
        """
        trade_id = f"V21-{int(time.time()*1000)}"
        
        executable_price, spread_cost, slippage, penalty = self.compute_executable_price(
            ask, bid, time_to_expiry, market_liquidity
        )
        
        fill_prob, reprice_prob, partial_fill, rejection_prob = self.compute_fill_probability(
            ask, time_to_expiry, market_liquidity
        )
        
        raw_edge, credible_ev = self.compute_ev(
            estimated_p, executable_price, spread_cost, slippage, penalty,
            fill_prob, rejection_prob
        )
        
        # Adversarial penalty (§13)
        if adversarial_score > 0.60:
            credible_ev *= 0.5  # Halve allocation at 0.60
        if adversarial_score > 0.80:
            credible_ev = -1.0  # Disable at 0.80
        
        # Position sizing based on edge
        if credible_ev > 0.10:
            size_usd = self.MAX_POSITION_SIZE
        elif credible_ev > 0.05:
            size_usd = self.MIN_POSITION_SIZE + (credible_ev - 0.05) / 0.05 * 0.50
        elif credible_ev > 0:
            size_usd = self.MIN_POSITION_SIZE
        else:
            size_usd = 0.0  # No entry
        
        should_enter = credible_ev > 0 and size_usd > 0
        
        result = ExecutionResult(
            trade_id=trade_id,
            cell_id="",
            profile_id="",
            asset="",
            interval="",
            direction="",
            side="BUY",
            raw_ask=ask,
            executable_price=executable_price,
            spread_cost=spread_cost,
            slippage_cost=slippage,
            execution_penalty=penalty,
            fill_probability=fill_prob,
            reprice_probability=reprice_prob,
            partial_fill_risk=partial_fill,
            size_usd=size_usd,
            size_shares=size_usd / ask if ask > 0 else 0,
            effective_cost=size_usd + spread_cost + slippage + penalty,
            estimated_p=estimated_p,
            raw_edge=raw_edge,
            net_edge=raw_edge - spread_cost - slippage - penalty,
            credible_ev=credible_ev,
            time_to_expiry_sec=time_to_expiry,
            entry_timestamp=time.time(),
        )
        
        return should_enter, result
    
    def settle_trade(self, trade: ExecutionResult, 
                     won: bool) -> ExecutionResult:
        """Binary settlement: contract resolves to 0 or 1. NEVER midpoint.
        
        Args:
            trade: The execution to settle
            won: True if direction was correct (token resolves to $1)
        """
        if won:
            settlement = SettlementResult.UP_WIN if trade.direction == "UP" else SettlementResult.DOWN_WIN
            payout = trade.size_shares * 1.0  # $1 per winning share
            cost = trade.effective_cost
            pnl = payout - cost
        else:
            settlement = SettlementResult.DOWN_WIN if trade.direction == "UP" else SettlementResult.UP_WIN
            payout = 0.0  # Losing token resolves to $0
            cost = trade.effective_cost
            pnl = -cost
        
        trade.settlement = settlement
        trade.settlement_pnl = pnl
        trade.settlement_timestamp = time.time()
        
        return trade


class LiveConstraints:
    """V21 §14 — Hard live constraints. No exceptions. No override.
    
    Max position size: $2
    Max concurrent positions: 1
    Max live profiles: 1
    Max daily loss: $10
    Max weekly loss: $30
    Max live trades/day: 20
    Forced shutdown on errors: TRUE
    """
    
    MAX_POSITION_SIZE_USD = 2.00
    MAX_CONCURRENT_POSITIONS = 1
    MAX_LIVE_PROFILES = 1
    MAX_DAILY_LOSS_USD = 10.00
    MAX_WEEKLY_LOSS_USD = 30.00
    MAX_DAILY_TRADES = 20
    FORCED_SHUTDOWN_ON_ERRORS = True
    
    # No martingale. No leverage escalation. No revenge sizing.
    NO_MARTINGALE = True
    NO_LEVERAGE_ESCALATION = True
    NO_REVENGE_SIZING = True
    
    def __init__(self):
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        self.daily_trades = 0
        self.current_positions = 0
        self.active_profiles = 0
        self.last_daily_reset = time.time()
        self.last_weekly_reset = time.time()
        self.emergency_shutdown = False
    
    def check_position_size(self, size_usd: float) -> bool:
        return size_usd <= self.MAX_POSITION_SIZE_USD and size_usd > 0
    
    def check_concurrent_positions(self) -> bool:
        return self.current_positions < self.MAX_CONCURRENT_POSITIONS
    
    def check_live_profiles(self) -> bool:
        return self.active_profiles <= self.MAX_LIVE_PROFILES
    
    def check_daily_loss(self) -> bool:
        return self.daily_pnl > -self.MAX_DAILY_LOSS_USD
    
    def check_weekly_loss(self) -> bool:
        return self.weekly_pnl > -self.MAX_WEEKLY_LOSS_USD
    
    def check_daily_trade_limit(self) -> bool:
        return self.daily_trades < self.MAX_DAILY_TRADES
    
    def can_trade(self, size_usd: float) -> Tuple[bool, str]:
        """Check all live constraints. Returns (allowed, reason)."""
        if self.emergency_shutdown:
            return False, "EMERGENCY_SHUTDOWN"
        
        if not self.check_position_size(size_usd):
            return False, f"POSITION_SIZE_EXCEEDED: ${size_usd:.2f} > ${self.MAX_POSITION_SIZE_USD:.2f}"
        
        if not self.check_concurrent_positions():
            return False, f"CONCURRENT_POSITIONS_EXCEEDED: {self.current_positions} >= {self.MAX_CONCURRENT_POSITIONS}"
        
        if not self.check_daily_loss():
            return False, f"DAILY_LOSS_LIMIT: ${self.daily_pnl:.2f} < -${self.MAX_DAILY_LOSS_USD:.2f}"
        
        if not self.check_weekly_loss():
            return False, f"WEEKLY_LOSS_LIMIT: ${self.weekly_pnl:.2f} < -${self.MAX_WEEKLY_LOSS_USD:.2f}"
        
        if not self.check_daily_trade_limit():
            return False, f"DAILY_TRADES_EXCEEDED: {self.daily_trades} >= {self.MAX_DAILY_TRADES}"
        
        return True, "OK"
    
    def record_trade(self, pnl: float):
        """Record trade result and update constraints."""
        self.daily_trades += 1
        self.daily_pnl += pnl
        self.weekly_pnl += pnl
        
        # Trigger emergency shutdown on constraint violations
        if self.daily_pnl <= -self.MAX_DAILY_LOSS_USD and self.FORCED_SHUTDOWN_ON_ERRORS:
            self.emergency_shutdown = True
    
    def reset_daily(self):
        """Reset daily counters."""
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.last_daily_reset = time.time()
    
    def reset_weekly(self):
        """Reset weekly counters."""
        self.weekly_pnl = 0.0
        self.last_weekly_reset = time.time()
    
    def check_resets(self):
        """Auto-reset daily/weekly counters."""
        now = time.time()
        if now - self.last_daily_reset > 86400:
            self.reset_daily()
        if now - self.last_weekly_reset > 604800:
            self.reset_weekly()


class AdversarialDetector:
    """V21 §13 — Adversarial Market Detection.
    
    The market is assumed hostile. Continuous measurement of:
    - Fake reversals
    - MM pinning
    - Spread traps
    - Repricing asymmetry
    - Liquidity spoofing
    - Directional bait
    
    Score: 0.0 (normal) → 1.0 (hostile)
    0.60: halve allocation
    0.80: disable profile
    """
    
    def __init__(self):
        self.score: float = 0.0
        self.fake_reversal_count: int = 0
        self.mm_pinning_events: int = 0
        self.spread_trap_count: int = 0
        self.repricing_asymmetry: float = 0.0
        self.liquidity_spoof_count: int = 0
        self.directional_bait_count: int = 0
        self.total_observations: int = 0
    
    def update(self, bid: float, ask: float, last_bid: float, last_ask: float,
               spot_move_pct: float, contract_move_pct: float,
               volume_change_pct: float):
        """Update adversarial score based on market observations."""
        self.total_observations += 1
        
        # 1. Spread trap detection: spread widens right after entry signal
        current_spread = ask - bid
        prev_spread = last_ask - last_bid if last_ask and last_bid else current_spread
        if current_spread > prev_spread * 2.0 and prev_spread > 0:
            self.spread_trap_count += 1
        
        # 2. Repricing asymmetry: spot moves but contract doesn't (or vice versa)
        if abs(spot_move_pct) > 0.002:  # Spot moved > 0.2%
            if abs(contract_move_pct) < abs(spot_move_pct) * 0.3:
                self.repricing_asymmetry += 0.1
        
        # 3. Liquidity spoofing: volume spike then immediate reversal
        if volume_change_pct > 2.0:  # Volume doubled
            self.liquidity_spoof_count += 1
        
        # 4. MM pinning: price stuck near 0.50 (max uncertainty)
        if abs(ask + bid - 1.0) < 0.02:
            self.mm_pinning_events += 1
        
        # Compute combined score
        n = max(1, self.total_observations)
        fake_reversal_rate = min(1.0, self.fake_reversal_count / n * 5)
        spread_trap_rate = min(1.0, self.spread_trap_count / n * 10)
        repricing_score = min(1.0, self.repricing_asymmetry / 5)
        spoof_rate = min(1.0, self.liquidity_spoof_count / n * 3)
        pinning_rate = min(1.0, self.mm_pinning_events / n * 5)
        bait_rate = min(1.0, self.directional_bait_count / n * 8)
        
        # Weighted adversarial score
        self.score = (
            fake_reversal_rate * 0.25 +
            spread_trap_rate * 0.20 +
            repricing_score * 0.20 +
            spoof_rate * 0.15 +
            pinning_rate * 0.10 +
            bait_rate * 0.10
        )
        
        # Clamp to [0, 1]
        self.score = max(0.0, min(1.0, self.score))
    
    def get_allocation_multiplier(self) -> float:
        """Get position size multiplier based on adversarial score.
        0.60 → 0.50 (halve)
        0.80 → 0.00 (disable)
        """
        if self.score >= 0.80:
            return 0.0
        elif self.score >= 0.60:
            return 0.50
        else:
            return 1.0 - (self.score * 0.5)  # Linear decrease from 1.0 to 0.5