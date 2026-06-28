#!/usr/bin/env python3
"""
FDC Smart Money Filter
Extracted from Polymarket-bot (MrFadiAi) smart money analysis engine.
Polls leaderboard, applies quality gates (WR >= 60%, PF >= 1.5x,
consistency checks), categorizes markets, and scores wallets.

Use as Track 5 in FDC: follow proven winners with filter overlays.

Author: Hugh (3rd of 5)
Date: 2026-05-15
"""

from __future__ import annotations
import json
import time
import re
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta


# ─── Constants ─────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Quality gate thresholds (from bot-config.ts v3.1)
MIN_WIN_RATE = 0.60        # 60% win rate
MIN_PROFIT_FACTOR = 1.50   # Gross wins / gross losses
MIN_TRADE_COUNT = 20       # Avoid one-hit wonders
MAX_CONSISTENCY_GAP = 0.30 # Max allowed gap between best/worst week

# Risk tiers
MAX_DAILY_LOSS_PCT = 0.05  # 5% daily
MAX_MONTHLY_LOSS_PCT = 0.15
MAX_DRAWDOWN = 0.25
TOTAL_HALT_LOSS = 0.40


# ─── Market Categorization ─────────────────────────────────────────────────

CATEGORY_KEYWORDS: Dict[str, str] = {
    "crypto":      r"\b(btc|bitcoin|eth|ethereum|sol|solana|xrp|crypto|doge|ada|matic)\b",
    "politics":    r"\b(trump|biden|election|president|senate|congress|vote|politic|maga|democrat|republican)\b",
    "sports":      r"\b(nfl|nba|mlb|nhl|super bowl|world cup|championship|game|match|ufc|soccer|football|basketball)\b",
    "economics":   r"\b(fed|interest rate|inflation|gdp|recession|economic|unemployment|cpi)\b",
    "entertainment": r"\b(oscar|grammy|movie|twitter|celebrity|entertainment|netflix|spotify)\b",
    "science":     r"\b(spacex|nasa|ai|openai|google|apple|tesla|tech|technology|science)\b",
}

CATEGORY_PRIORITY = ["crypto", "politics", "sports", "economics", "entertainment", "science"]


def categorize_market(title: str) -> str:
    """Classify a Polymarket title into a category."""
    lower = title.lower()
    for cat in CATEGORY_PRIORITY:
        if re.search(CATEGORY_KEYWORDS[cat], lower):
            return cat
    return "other"


# ─── Data Structures ───────────────────────────────────────────────────────


@dataclass
class WalletProfile:
    """Smart money wallet with quality metrics."""
    address: str
    name: str = ""
    pnl: float = 0.0
    volume: float = 0.0
    trade_count: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_win: float = 0.0
    max_loss: float = 0.0
    winning_markets: int = 0
    losing_markets: int = 0
    categories: Dict[str, float] = field(default_factory=dict)  # cat → PnL
    last_active: Optional[str] = None
    score: float = 0.0
    rank: Optional[int] = None


@dataclass
class QualityGates:
    """Gate results for a wallet."""
    passes_win_rate: bool = False
    passes_profit_factor: bool = False
    passes_trade_count: bool = False
    passes_consistency: bool = False  # whale-detection: no one-hit wonders
    passes_drawdown: bool = False

    @property
    def all_pass(self) -> bool:
        return all([
            self.passes_win_rate,
            self.passes_profit_factor,
            self.passes_trade_count,
            self.passes_consistency,
            self.passes_drawdown,
        ])

    @property
    def gate_count(self) -> int:
        return sum([
            self.passes_win_rate,
            self.passes_profit_factor,
            self.passes_trade_count,
            self.passes_consistency,
            self.passes_drawdown,
        ])


@dataclass
class SmartMoneySignal:
    """A trade signal derived from smart money activity."""
    wallet: WalletProfile
    condition_id: str
    market_title: str
    token_id: str
    side: str  # BUY / SELL
    outcome: str
    price: float
    size: float
    category: str
    timestamp: datetime
    confidence: float = 0.0  # 0-1, derived from wallet score × gate count


# ─── Quality Gate Evaluator ────────────────────────────────────────────────


def evaluate_wallet_quality(
    profile: WalletProfile,
    trade_history: List[dict] = None,
) -> QualityGates:
    """
    Apply 5 quality gates to a wallet profile.

    Gates:
    1. Win rate >= 60%
    2. Profit factor >= 1.5
    3. Trade count >= 20 (avoid one-hit wonders)
    4. Consistency: no single trade > 40% of total PnL (whale detection)
    5. Max drawdown from peak PnL < 25%
    """
    gates = QualityGates()

    gates.passes_win_rate = profile.win_rate >= MIN_WIN_RATE
    gates.passes_profit_factor = profile.profit_factor >= MIN_PROFIT_FACTOR
    gates.passes_trade_count = profile.trade_count >= MIN_TRADE_COUNT

    # Consistency: check for whale-dependency (one trade dominates)
    if trade_history:
        total_pnl = sum(t.get("pnl", 0) for t in trade_history)
        if total_pnl > 0:
            max_single = max(t.get("pnl", 0) for t in trade_history)
            single_concentration = max_single / total_pnl
            gates.passes_consistency = single_concentration < 0.40
        else:
            gates.passes_consistency = False
    else:
        gates.passes_consistency = True  # no data = no evidence of cheating

    # Drawdown: check if max loss exceeds threshold
    if profile.max_loss > 0:
        dd_ratio = abs(profile.max_loss) / max(profile.pnl, 1.0)
        gates.passes_drawdown = dd_ratio < MAX_DRAWDOWN
    else:
        gates.passes_drawdown = True

    return gates


# ─── Wallet Scoring ────────────────────────────────────────────────────────


def score_wallet(profile: WalletProfile, gates: QualityGates) -> float:
    """
    Composite score 0-100 combining PnL, win rate, profit factor,
    gate pass count, and volume.

    Formula:
        base = 25 * (pnl normalizer) + 25 * win_rate + 20 * pf_normalizer + 15 * gate_count/5 + 15 * volume_normalizer
    """
    # PnL score: $10K+ = max points
    pnl_score = min(25.0, profile.pnl / 400.0)  # $10K = max

    # Win rate: linear 0-25
    wr_score = min(25.0, profile.win_rate / 0.80 * 25.0)

    # Profit factor: 1.0 = 5pts, 2.0+ = 20pts
    pf_score = min(20.0, (profile.profit_factor - 0.5) / 1.5 * 20.0)

    # Gate count: each gate = 3 points (max 15)
    gate_score = gates.gate_count * 3.0

    # Volume: $50K+ = max
    vol_score = min(15.0, profile.volume / 3333.0)

    return round(pnl_score + wr_score + pf_score + gate_score + vol_score, 1)


# ─── Signal Confidence ─────────────────────────────────────────────────────


def compute_signal_confidence(
    wallet: WalletProfile,
    gates: QualityGates,
) -> float:
    """
    Confidence = wallet_score / 100 × gates_passing / 5

    A wallet with score=80 and 5/5 gates → 0.80 confidence
    A wallet with score=80 and 3/5 gates → 0.48 confidence
    """
    gate_ratio = gates.gate_count / 5.0
    score_ratio = wallet.score / 100.0
    return round(gate_ratio * score_ratio, 3)


# ─── Trade Copy Logic ──────────────────────────────────────────────────────


def should_copy_trade(
    signal: SmartMoneySignal,
    min_confidence: float = 0.50,
    min_trade_size: float = 5.0,
    max_slippage: float = 0.03,
    allowed_categories: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """
    Decide whether to copy a smart money trade.

    Returns:
        (should_copy, reason)
    """
    # Confidence gate
    if signal.confidence < min_confidence:
        return False, f"confidence {signal.confidence:.3f} < min {min_confidence}"

    # Size gate
    usdc_value = signal.size * signal.price
    if usdc_value < min_trade_size:
        return False, f"trade size ${usdc_value:.2f} < min ${min_trade_size}"

    # Category filter (optional)
    if allowed_categories and signal.category not in allowed_categories:
        return False, f"category '{signal.category}' not in allowed list"

    # Skip sell signals (only follow buys — entry signals)
    if signal.side == "SELL":
        return False, "exit signals not followed (only entries)"

    return True, "pass"


# ─── Leaderboard Parser ────────────────────────────────────────────────────


def parse_leaderboard_entry(entry: dict) -> WalletProfile:
    """Parse raw Polymarket leaderboard JSON into WalletProfile."""
    return WalletProfile(
        address=entry.get("address", ""),
        name=entry.get("user_name", "")
              or entry.get("x_username", "")
              or entry.get("address", "")[:8],
        pnl=float(entry.get("pnl", 0)),
        volume=float(entry.get("volume", 0)),
        trade_count=int(entry.get("trade_count", 0)),
        win_rate=float(entry.get("win_rate", 0)),
        profit_factor=float(entry.get("profit_factor", 0)),
        avg_win=float(entry.get("avg_win", 0)),
        avg_loss=float(entry.get("avg_loss", 0)),
        max_win=float(entry.get("max_win", 0)),
        max_loss=float(entry.get("max_loss", 0)),
        winning_markets=int(entry.get("winning_markets", 0)),
        losing_markets=int(entry.get("losing_markets", 0)),
        categories=entry.get("category_breakdown", {}),
        last_active=entry.get("last_active"),
        rank=entry.get("rank"),
    )


# ─── Reporting ─────────────────────────────────────────────────────────────


def wallet_report_line(profile: WalletProfile, gates: QualityGates) -> str:
    """Single-line wallet summary."""
    status = "✅" if gates.all_pass else f"⚠️ {gates.gate_count}/5"
    return (
        f"| #{profile.rank or '-'} | `{profile.name}` | "
        f"${profile.pnl:,.0f} | "
        f"{profile.win_rate:.0%} | "
        f"{profile.profit_factor:.2f} | "
        f"{profile.score:.0f} | "
        f"{status} |"
    )


# ─── Quick Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Mock wallet
    wallet = WalletProfile(
        address="0xohanism...",
        name="ohanism",
        pnl=31253,
        volume=500000,
        trade_count=200,
        win_rate=0.80,
        profit_factor=2.10,
        avg_win=195,
        avg_loss=-85,
        max_win=1129,
        max_loss=-340,
        winning_markets=45,
        losing_markets=12,
        categories={"crypto": 28000, "politics": 2000, "sports": 1253},
        rank=1,
    )

    gates = evaluate_wallet_quality(wallet)
    wallet.score = score_wallet(wallet, gates)

    print(f"Wallet: {wallet.name}")
    print(f"  Score: {wallet.score}/100")
    print(f"  Gates: {gates.gate_count}/5 ({'PASS' if gates.all_pass else 'FAIL'})")
    print(f"    WR: {'✓' if gates.passes_win_rate else '✗'} | "
          f"PF: {'✓' if gates.passes_profit_factor else '✗'} | "
          f"Count: {'✓' if gates.passes_trade_count else '✗'} | "
          f"Consistency: {'✓' if gates.passes_consistency else '✗'} | "
          f"DD: {'✓' if gates.passes_drawdown else '✗'}")

    signal = SmartMoneySignal(
        wallet=wallet,
        condition_id="0xcondition...",
        market_title="BTC above $80K on May 15?",
        token_id="0xabc123...",
        side="BUY",
        outcome="Yes",
        price=0.48,
        size=50,
        category="crypto",
        timestamp=datetime.now(),
        confidence=compute_signal_confidence(wallet, gates),
    )

    copy, reason = should_copy_trade(signal)
    print(f"\nSignal: {signal.market_title}")
    print(f"  Copy: {copy} ({reason})")
    print(f"  Confidence: {signal.confidence}")

    print(f"\nCategory test: 'BTC above $80K?' → {categorize_market('BTC above $80K?')}")
    print(f"Category test: 'Trump wins 2028?' → {categorize_market('Trump wins 2028?')}")
    print(f"Category test: 'Lakers win NBA?' → {categorize_market('Lakers win NBA?')}")
