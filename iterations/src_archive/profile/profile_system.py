"""V21 Profile System — Directional Extraction Hypotheses
=========================================================
Each profile is a directional hypothesis, not an indicator strategy.
Profiles compete continuously. Weak die permanently. Strong absorb allocation.
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from enum import Enum
import time
import math


class DirectionTag(str, Enum):
    """All directional hypotheses — no reversal assumptions."""
    DOWN_CONTINUATION = "DOWN_CONTINUATION"       # Down move continues
    DOWN_ACCELERATION = "DOWN_ACCELERATION"       # Down move accelerating
    DOWN_PANIC_FOLLOWTHROUGH = "DOWN_PANIC_FOLLOWTHROUGH"  # Panic selling continues
    DOWN_LATE_WINDOW_MOMENTUM = "DOWN_LATE_WINDOW_MOMENTUM"  # DOWN near expiry
    UP_CONTINUATION = "UP_CONTINUATION"           # Up move continues
    UP_BREAKOUT_ACCELERATION = "UP_BREAKOUT_ACCELERATION"  # Up move accelerating
    UP_VOL_EXPANSION = "UP_VOL_EXPANSION"         # Volatility expansion upward
    UP_LATE_WINDOW_MOMENTUM = "UP_LATE_WINDOW_MOMENTUM"    # UP near expiry
    UP_REVERSAL = "UP_REVERSAL"                   # Verified reversal upward (data-confirmed only)
    DOWN_REVERSAL = "DOWN_REVERSAL"               # Verified reversal downward (data-confirmed only)
    MARKET_LAG_ATTACK = "MARKET_LAG_ATTACK"       # Oracle lag exploitation
    ORACLE_DELAY_EXPLOIT = "ORACLE_DELAY_EXPLOIT" # Polymarket repricing lag


class ProfileStatus(str, Enum):
    EXPLORING = "exploring"        # New, unproven
    PROBATION = "probation"        # Under evaluation
    ACTIVE = "active"              # Currently trading
    PROMOTED = "promoted"          # Strong, receiving capital
    KILLED = "killed"              # Permanently disabled
    DORMANT = "dormant"            # Temporarily paused


class KillReason(str, Enum):
    PF_BELOW_090 = "PF_BELOW_090"
    EV_BELOW_MINUS_010 = "EV_BELOW_MINUS_010"
    LOSS_STREAK_8 = "LOSS_STREAK_8"
    EXECUTION_ANOMALY = "EXECUTION_ANOMALY"
    SETTLEMENT_INCONSISTENCY = "SETTLEMENT_INCONSISTENCY"
    ADVERSARIAL_SCORE_080 = "ADVERSARIAL_SCORE_080"
    HALF_LIFE_EXPIRED = "HALF_LIFE_EXPIRED"


@dataclass
class ProfileConfig:
    """Static profile configuration — immutable per profile."""
    profile_id: str                    # e.g. "BTC_5M_DOWN_CONTINUATION"
    asset: str                         # BTC, ETH, SOL, XRP
    interval: str                      # 5m, 15m
    direction_tag: DirectionTag
    rsi_context: str                   # "low", "mid", "high", "any"
    time_window: str                   # "early", "mid", "late", "full"
    description: str = ""


@dataclass
class ProfileStats:
    """Live statistics for a profile — updated after every trade."""
    resolved_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_ev: float = 0.0
    realized_ev_per_dollar: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    current_streak: int = 0           # Positive = win streak, negative = loss streak
    max_drawdown: float = 0.0
    peak_pnl: float = 0.0
    last_trade_ts: float = 0.0
    first_trade_ts: float = 0.0
    half_life: float = 0.0            # Trades until EV decays to 50%
    entropy_bits: float = 0.0
    adversarial_score: float = 0.0
    execution_anomalies: int = 0
    settlement_inconsistencies: int = 0


# ── V21 Profile Definitions ──
PROFILE_DEFINITIONS: List[ProfileConfig] = [
    # BTC 5m profiles
    ProfileConfig("BTC_5M_DOWN_CONTINUATION", "BTC", "5m", DirectionTag.DOWN_CONTINUATION, "any", "late",
                  "BTC 5m DOWN continuation — directional persistence in declining momentum"),
    ProfileConfig("BTC_5M_DOWN_ACCELERATION", "BTC", "5m", DirectionTag.DOWN_ACCELERATION, "low", "mid",
                  "BTC 5m DOWN acceleration — accelerating decline from oversold"),
    ProfileConfig("BTC_5M_PANIC_FOLLOWTHROUGH", "BTC", "5m", DirectionTag.DOWN_PANIC_FOLLOWTHROUGH, "low", "early",
                  "BTC 5m panic followthrough — DOWN continuation after RSI<35 crash"),
    ProfileConfig("BTC_5M_LATE_DOWN", "BTC", "5m", DirectionTag.DOWN_LATE_WINDOW_MOMENTUM, "any", "late",
                  "BTC 5m late-window DOWN — MM lag near resolution"),
    ProfileConfig("BTC_5M_UP_CONTINUATION", "BTC", "5m", DirectionTag.UP_CONTINUATION, "any", "late",
                  "BTC 5m UP continuation — directional persistence in rising momentum"),
    ProfileConfig("BTC_5M_UP_BREAKOUT", "BTC", "5m", DirectionTag.UP_BREAKOUT_ACCELERATION, "high", "early",
                  "BTC 5m UP breakout — accelerating upward from overbought"),
    ProfileConfig("BTC_5M_ORACLE_LAG", "BTC", "5m", DirectionTag.ORACLE_DELAY_EXPLOIT, "any", "late",
                  "BTC 5m oracle lag — Polymarket repricing lag exploitation"),
    
    # BTC 15m profiles
    ProfileConfig("BTC_15M_DOWN_CONTINUATION", "BTC", "15m", DirectionTag.DOWN_CONTINUATION, "any", "late",
                  "BTC 15m DOWN continuation — sustained directional persistence"),
    ProfileConfig("BTC_15M_BREAKDOWN_ACCELERATION", "BTC", "15m", DirectionTag.DOWN_ACCELERATION, "low", "mid",
                  "BTC 15m breakdown acceleration — momentum cascade"),
    ProfileConfig("BTC_15M_ORACLE_DELAY", "BTC", "15m", DirectionTag.ORACLE_DELAY_EXPLOIT, "any", "late",
                  "BTC 15m oracle delay — longer window repricing lag"),
    
    # ETH profiles
    ProfileConfig("ETH_5M_PANIC_FOLLOWTHROUGH", "ETH", "5m", DirectionTag.DOWN_PANIC_FOLLOWTHROUGH, "low", "early",
                  "ETH 5m panic followthrough — DOWN continuation in ETH crashes"),
    ProfileConfig("ETH_5M_DOWN_CONTINUATION", "ETH", "5m", DirectionTag.DOWN_CONTINUATION, "any", "late",
                  "ETH 5m DOWN continuation"),
    ProfileConfig("ETH_15M_OVEREXTENSION_COLLAPSE", "ETH", "15m", DirectionTag.DOWN_REVERSAL, "high", "mid",
                  "ETH 15m overextension collapse — verified reversal from overbought"),
    ProfileConfig("ETH_5M_VOL_EXPANSION_UP", "ETH", "5m", DirectionTag.UP_VOL_EXPANSION, "any", "early",
                  "ETH 5m vol expansion UP — volatility breakout upward"),
    
    # SOL profiles
    ProfileConfig("SOL_15M_VOL_EXPANSION_UP", "SOL", "15m", DirectionTag.UP_VOL_EXPANSION, "any", "early",
                  "SOL 15m vol expansion UP"),
    ProfileConfig("SOL_5M_DOWN_CONTINUATION", "SOL", "5m", DirectionTag.DOWN_CONTINUATION, "any", "late",
                  "SOL 5m DOWN continuation"),
    ProfileConfig("SOL_5M_LATE_WINDOW_MOMENTUM", "SOL", "5m", DirectionTag.DOWN_LATE_WINDOW_MOMENTUM, "any", "late",
                  "SOL 5m late-window momentum — repricing lag exploitation"),
    
    # XRP profiles
    ProfileConfig("XRP_5M_LATE_WINDOW_DOWN", "XRP", "5m", DirectionTag.DOWN_LATE_WINDOW_MOMENTUM, "any", "late",
                  "XRP 5m late-window DOWN — isolated to final 60-120s"),
    ProfileConfig("XRP_5M_ORACLE_LAG", "XRP", "5m", DirectionTag.ORACLE_DELAY_EXPLOIT, "any", "late",
                  "XRP 5m oracle lag — repricing delay attack"),
]


class ProfileTracker:
    """Manages all V21 profiles — creation, evolution, kill, promotion."""
    
    def __init__(self):
        self.profiles: Dict[str, ProfileStats] = {}
        self.statuses: Dict[str, ProfileStatus] = {}
        self.kill_reasons: Dict[str, KillReason] = {}
        self.creation_times: Dict[str, float] = {}
        
        # Initialize all defined profiles as exploring
        for pdef in PROFILE_DEFINITIONS:
            self.profiles[pdef.profile_id] = ProfileStats()
            self.statuses[pdef.profile_id] = ProfileStatus.EXPLORING
            self.creation_times[pdef.profile_id] = time.time()
    
    def record_trade(self, profile_id: str, win: bool, pnl: float, ev: float = 0.0):
        """Record a trade result against a profile."""
        if profile_id not in self.profiles:
            return
        
        stats = self.profiles[profile_id]
        stats.resolved_trades += 1
        if win:
            stats.wins += 1
            stats.current_streak = max(1, stats.current_streak + 1) if stats.current_streak > 0 else 1
        else:
            stats.losses += 1
            stats.current_streak = min(-1, stats.current_streak - 1) if stats.current_streak < 0 else -1
        
        stats.total_pnl += pnl
        stats.total_ev += ev
        stats.peak_pnl = max(stats.peak_pnl, stats.total_pnl)
        stats.max_drawdown = max(stats.max_drawdown, stats.peak_pnl - stats.total_pnl)
        stats.last_trade_ts = time.time()
        if stats.first_trade_ts == 0:
            stats.first_trade_ts = time.time()
        
        # Recalculate derived stats
        if stats.resolved_trades > 0:
            stats.win_rate = stats.wins / stats.resolved_trades
        if stats.losses > 0:
            stats.profit_factor = stats.total_pnl / abs(sum(1 for _ in range(stats.losses))) if stats.losses > 0 else float('inf')
        stats.realized_ev_per_dollar = stats.total_ev / max(1, stats.resolved_trades)
    
    def check_kill_conditions(self, profile_id: str) -> Optional[KillReason]:
        """Check if a profile should be permanently killed. Returns kill reason or None."""
        if profile_id not in self.profiles:
            return None
        
        stats = self.profiles[profile_id]
        
        # Kill Rules (§10):
        if stats.resolved_trades >= 20 and stats.profit_factor < 0.90:
            return KillReason.PF_BELOW_090
        if stats.resolved_trades >= 10 and stats.realized_ev_per_dollar < -0.10:
            return KillReason.EV_BELOW_MINUS_010
        if stats.current_streak <= -8:
            return KillReason.LOSS_STREAK_8
        if stats.execution_anomalies > 0:
            return KillReason.EXECUTION_ANOMALY
        if stats.settlement_inconsistencies > 0:
            return KillReason.SETTLEMENT_INCONSISTENCY
        if stats.adversarial_score > 0.80:
            return KillReason.ADVERSARIAL_SCORE_080
        
        return None
    
    def check_promotion(self, profile_id: str) -> bool:
        """Check if a profile qualifies for promotion. §10 promotion rules."""
        if profile_id not in self.profiles:
            return False
        
        stats = self.profiles[profile_id]
        
        # Promotion rules:
        if stats.resolved_trades < 20:
            return False
        if stats.profit_factor < 1.25:
            return False
        if stats.realized_ev_per_dollar < 0.10:
            return False
        if stats.win_rate < 0.55:
            return False
        if stats.entropy_bits < 0.50:
            return False
        
        return True
    
    def get_active_profiles(self) -> List[str]:
        """Get profiles eligible for trading (not killed, not dormant)."""
        return [
            pid for pid, status in self.statuses.items()
            if status in (ProfileStatus.EXPLORING, ProfileStatus.PROBATION,
                         ProfileStatus.ACTIVE, ProfileStatus.PROMOTED)
        ]
    
    def get_allocation_weights(self) -> Dict[str, float]:
        """PBOT-style allocation: 70/20/10 split among promoted/probing/exploring."""
        promoted = [pid for pid, s in self.statuses.items() if s == ProfileStatus.PROMOTED]
        active = [pid for pid, s in self.statuses.items() if s == ProfileStatus.ACTIVE]
        exploring = [pid for pid, s in self.statuses.items() if s == ProfileStatus.EXPLORING]
        
        weights = {}
        
        # 70% to promoted profiles
        if promoted:
            per_profile = 0.70 / len(promoted)
            for pid in promoted:
                weights[pid] = per_profile
        elif active:
            per_profile = 0.70 / len(active)
            for pid in active:
                weights[pid] = per_profile
        
        # 20% to probing (active if promoted exist, else exploring)
        probe_targets = active if promoted else exploring
        if probe_targets:
            per_profile = 0.20 / len(probe_targets)
            for pid in probe_targets:
                weights[pid] = weights.get(pid, 0) + per_profile
        
        # 10% to high-risk exploration
        if exploring:
            per_profile = 0.10 / len(exploring)
            for pid in exploring:
                weights[pid] = weights.get(pid, 0) + per_profile
        
        return weights
    
    def evolve(self):
        """PBOT-style aggressive profile evolution — check all profiles for kill/promotion."""
        killed_this_round = []
        promoted_this_round = []
        
        for profile_id in list(self.profiles.keys()):
            if self.statuses[profile_id] == ProfileStatus.KILLED:
                continue
            
            # Check kill conditions
            kill_reason = self.check_kill_conditions(profile_id)
            if kill_reason:
                self.statuses[profile_id] = ProfileStatus.KILLED
                self.kill_reasons[profile_id] = kill_reason
                killed_this_round.append((profile_id, kill_reason))
                continue
            
            # Check promotion
            current_status = self.statuses[profile_id]
            if current_status == ProfileStatus.PROBATION and self.check_promotion(profile_id):
                self.statuses[profile_id] = ProfileStatus.ACTIVE
                promoted_this_round.append(profile_id)
            elif current_status == ProfileStatus.ACTIVE and self.check_promotion(profile_id):
                self.statuses[profile_id] = ProfileStatus.PROMOTED
                promoted_this_round.append(profile_id)
            elif current_status == ProfileStatus.EXPLORING and self.profiles[profile_id].resolved_trades >= 5:
                self.statuses[profile_id] = ProfileStatus.PROBATION
        
        return killed_this_round, promoted_this_round