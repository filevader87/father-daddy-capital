#!/usr/bin/env python3
"""
Father Daddy Capital — Meta-Controller (Auto-Adaptive Strategy Layer)
======================================================================
Sits above all five trading engines. Every scan cycle:

  1. SIGNAL BUS — aggregates every engine's output into one state vector
  2. REGIME DETECTOR — IF/AND/OR boolean classification of market regime
  3. STRATEGY WEIGHTS — dynamically allocates capital per engine per regime
  4. HYBRID RULES — cross-engine trade logic that no single engine sees
  5. RISK COORDINATOR — global limits, correlation hedges, drawdown stops

The meta-controller does NOT replace the engines. It tells them what to do
and how much capital they get this cycle. Engines still own execution.

Regimes (mutually exclusive, re-evaluated every scan):
  - RISK_ON       Strong trending: swing + alt dominate, scalp suppressed
  - SIDEWAYS      Range-bound: scalp dominates, swing/alt reduced
  - RISK_OFF      Panic/crash: cash is king, all positions capped
  - CRYPTO_DIV    Crypto and equities diverge: arb opportunities
  - VOL_COMPRESS  Low vol: mean-reversion strategies favored
  - VOL_EXPAND    High vol: momentum/trend strategies favored
  - NEUTRAL       Default: equal weights, no overrides
"""

import numpy as np
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

# ─── Configuration ───────────────────────────────────────────────────────────

OUTPUT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/output")
STATE_FILE = OUTPUT_DIR / "paper_state.json"

# Capital pools (adjusted dynamically by StrategyWeights)
TOTAL_BANKROLL = 100_000.0
DEFAULT_WEIGHTS = {
    "swing":      0.40,   # Core multi-asset swing trading
    "scalp":      0.10,   # Short-term crypto scalping
    "polymarket": 0.15,   # Binary event contracts
    "altcoin":    0.25,   # Altcoin momentum/volume farming
    "arb":        0.10,   # Polymarket complete-set arbitrage
}

# Regime classification thresholds
TREND_THRESHOLD = 0.30       # Signal strength to consider "trending"
VOL_HIGH_FACTOR = 1.5        # Current vol > median * factor → high vol
VOL_LOW_FACTOR = 0.6         # Current vol < median * factor → low vol
CORRELATION_THRESHOLD = 0.6  # Cross-asset correlation for systemic risk
DIVERGENCE_THRESHOLD = -0.3  # Negative correlation for crypto/equity divergence
DRAWDOWN_CAP = 0.08          # 8% global drawdown → force risk reduction
MAX_CORRELATED_EXPOSURE = 0.30  # Max exposure to correlated assets

# History window for regime detection
HISTORY_WINDOW = 20  # scans


@dataclass
class SignalBus:
    """Aggregated state from all engines at the current scan."""

    # ── Price / market data ──
    btc_price: float = 0.0
    btc_rsi: float = 50.0
    btc_momentum_5m: float = 0.0
    btc_volatility: float = 0.0
    equity_index_price: float = 0.0   # SPY proxy
    equity_trend: float = 0.0          # Aggregate equity direction
    crypto_trend: float = 0.0          # Aggregate crypto direction

    # ── Engine states ──
    swing_signal_strength: float = 0.0    # Average signal score across all assets
    swing_open_positions: int = 0
    swing_pnl_24h: float = 0.0

    scalp_signal_strength: float = 0.0
    scalp_open_positions: int = 0
    scalp_pnl_24h: float = 0.0

    pm_edge_present: bool = False         # Any contract with edge > 0.03
    pm_best_edge: float = 0.0
    pm_open_positions: int = 0
    pm_pnl_24h: float = 0.0

    alt_signal_strength: float = 0.0
    alt_open_positions: int = 0
    alt_pnl_24h: float = 0.0

    arb_opportunity: bool = False
    arb_open_positions: int = 0
    arb_pnl_24h: float = 0.0

    # ── Cross-market ──
    crypto_equity_correlation: float = 0.0
    total_open_positions: int = 0
    total_exposure_pct: float = 0.0       # % of bankroll deployed

    # ── Risk ──
    current_drawdown_pct: float = 0.0
    daily_pnl: float = 0.0
    scan_number: int = 0

    # ── Raw engine outputs ──
    swing_scan_results: list = field(default_factory=list)
    scalp_entries: list = field(default_factory=list)
    pm_entries: list = field(default_factory=list)
    alt_entries: list = field(default_factory=list)
    arb_tick: Optional[dict] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not k.startswith("_") and not isinstance(v, list)}


# ─── Regime Detector ─────────────────────────────────────────────────────────

class RegimeDetector:
    """
    IF/AND/OR boolean classification of the current market regime.

    Evaluates every scan. One regime active at a time.
    Priority order: RISK_OFF > RISK_ON > CRYPTO_DIV > VOL > SIDEWAYS > NEUTRAL
    """

    def __init__(self):
        self.regime_history: list[str] = []
        self._vol_history: list[float] = []
        self._corr_history: list[float] = []

    @property
    def median_vol(self) -> float:
        if len(self._vol_history) < 5:
            return 0.3
        return float(np.median(self._vol_history[-20:]))

    def classify(self, bus: SignalBus) -> str:
        """
        Run the IF/AND/OR classification tree.
        Returns one of: RISK_ON, SIDEWAYS, RISK_OFF, CRYPTO_DIV,
                        VOL_COMPRESS, VOL_EXPAND, NEUTRAL
        """
        # Track histories
        self._vol_history.append(bus.btc_volatility)
        self._corr_history.append(bus.crypto_equity_correlation)
        if len(self._vol_history) > 100:
            self._vol_history = self._vol_history[-100:]
        if len(self._corr_history) > 100:
            self._corr_history = self._corr_history[-100:]

        vol_ratio = bus.btc_volatility / max(self.median_vol, 0.01)

        # ── RULE 1: RISK_OFF ───────────────────────────────────────────
        # IF global drawdown > 8%
        # OR (crypto correlation > 0.7 AND all trends are DOWN AND vol is high)
        if bus.current_drawdown_pct > DRAWDOWN_CAP:
            regime = "RISK_OFF"
        elif (bus.crypto_equity_correlation > CORRELATION_THRESHOLD
              and bus.crypto_trend < -TREND_THRESHOLD
              and bus.equity_trend < -TREND_THRESHOLD
              and vol_ratio > VOL_HIGH_FACTOR):
            regime = "RISK_OFF"

        # ── RULE 2: RISK_ON ────────────────────────────────────────────
        # IF crypto trend IS UP AND equity trend IS UP
        # AND (swing signal IS strong OR alt signal IS strong)
        # AND drawdown < 3%
        elif (bus.crypto_trend > TREND_THRESHOLD
              and bus.equity_trend > TREND_THRESHOLD
              and (bus.swing_signal_strength > 0.2 or bus.alt_signal_strength > 0.3)
              and bus.current_drawdown_pct < 0.03):
            regime = "RISK_ON"

        # ── RULE 3: CRYPTO_DIV ─────────────────────────────────────────
        # IF crypto vs equity correlation IS negative
        # AND (crypto vol > 2x equity vol OR equity vol > 2x crypto vol)
        elif (bus.crypto_equity_correlation < DIVERGENCE_THRESHOLD
              and bus.btc_volatility > 0.3
              and bus.arb_opportunity):
            regime = "CRYPTO_DIV"

        # ── RULE 4: VOL_COMPRESS ───────────────────────────────────────
        # IF vol < median_vol * 0.6
        # AND (swing signal is weak AND scalp signal is weak)
        elif (vol_ratio < VOL_LOW_FACTOR
              and bus.swing_signal_strength < 0.3):
            regime = "VOL_COMPRESS"

        # ── RULE 5: VOL_EXPAND ─────────────────────────────────────────
        # IF vol > median_vol * 1.5
        # AND (swing signal IS strong OR alt signal IS strong)
        elif (vol_ratio > VOL_HIGH_FACTOR
              and (bus.swing_signal_strength > 0.3 or bus.alt_signal_strength > 0.3)):
            regime = "VOL_EXPAND"

        # ── RULE 6: SIDEWAYS ───────────────────────────────────────────
        # IF crypto trend IS neutral (between -0.2 and +0.2)
        # AND equity trend IS neutral
        # AND vol is normal (0.6-1.5x median)
        elif (abs(bus.crypto_trend) < 0.2
              and abs(bus.equity_trend) < 0.2
              and 0.6 < vol_ratio < 1.5):
            regime = "SIDEWAYS"

        # ── DEFAULT: NEUTRAL ───────────────────────────────────────────
        else:
            regime = "NEUTRAL"

        self.regime_history.append(regime)
        if len(self.regime_history) > 50:
            self.regime_history = self.regime_history[-50:]

        return regime

    @property
    def regime_stability(self) -> float:
        """Fraction of recent scans in current regime (0-1)."""
        if len(self.regime_history) < 5:
            return 1.0
        current = self.regime_history[-1]
        recent = self.regime_history[-10:]
        return recent.count(current) / len(recent)


# ─── Strategy Weights ────────────────────────────────────────────────────────

class StrategyWeights:
    """
    Dynamically allocates capital across engines based on current regime.

    Each regime has a weight vector [swing, scalp, polymarket, altcoin, arb].
    Weights are blended with DEFAULT_WEIGHTS based on regime_stability
    to prevent abrupt capital shifts.
    """

    # Weight matrices per regime (must sum to 1.0)
    REGIME_WEIGHTS = {
        "RISK_ON": {
            "swing": 0.45, "scalp": 0.05, "polymarket": 0.10,
            "altcoin": 0.35, "arb": 0.05,
        },
        "SIDEWAYS": {
            "swing": 0.20, "scalp": 0.35, "polymarket": 0.15,
            "altcoin": 0.15, "arb": 0.15,
        },
        "RISK_OFF": {
            "swing": 0.15, "scalp": 0.10, "polymarket": 0.05,
            "altcoin": 0.05, "arb": 0.05, "cash": 0.60,
        },
        "CRYPTO_DIV": {
            "swing": 0.15, "scalp": 0.20, "polymarket": 0.15,
            "altcoin": 0.10, "arb": 0.40,
        },
        "VOL_COMPRESS": {
            "swing": 0.25, "scalp": 0.20, "polymarket": 0.10,
            "altcoin": 0.15, "arb": 0.30,
        },
        "VOL_EXPAND": {
            "swing": 0.35, "scalp": 0.10, "polymarket": 0.20,
            "altcoin": 0.30, "arb": 0.05,
        },
        "NEUTRAL": {
            "swing": 0.35, "scalp": 0.15, "polymarket": 0.15,
            "altcoin": 0.25, "arb": 0.10,
        },
    }

    def __init__(self):
        self.current_weights = dict(DEFAULT_WEIGHTS)
        self._weight_history: list[dict] = []

    def compute(self, regime: str, stability: float,
                bus: SignalBus) -> dict:
        """
        Compute capital allocation weights for this cycle.

        Args:
            regime: Current regime classification
            stability: 0-1 how stable the regime is
            bus: Full signal bus for override logic

        Returns:
            {engine_name: weight} dict summing to 1.0
        """
        target = dict(self.REGIME_WEIGHTS.get(regime, DEFAULT_WEIGHTS))

        # ── Smooth blending: target × stability + current × (1-stability) ──
        blended = {}
        for engine in DEFAULT_WEIGHTS:
            target_w = target.get(engine, 0.0)
            current_w = self.current_weights.get(engine, DEFAULT_WEIGHTS[engine])
            blended[engine] = target_w * stability + current_w * (1.0 - stability)

        # ── Override: if an engine has been crushing it, give it more ──
        pnl_by_engine = {
            "swing": bus.swing_pnl_24h,
            "scalp": bus.scalp_pnl_24h,
            "polymarket": bus.pm_pnl_24h,
            "altcoin": bus.alt_pnl_24h,
            "arb": bus.arb_pnl_24h,
        }
        best_engine = max(pnl_by_engine, key=lambda k: pnl_by_engine[k])
        if pnl_by_engine[best_engine] > 50:  # $50+ in 24h
            boost = min(0.10, pnl_by_engine[best_engine] / 1000)
            blended[best_engine] = min(0.60, blended.get(best_engine, 0) + boost)

        # ── Override: if an engine is hemorrhaging, cut it ──
        for engine, pnl in pnl_by_engine.items():
            if pnl < -100:  # Lost $100+ in 24h
                cut = min(0.10, abs(pnl) / 2000)
                blended[engine] = max(0.02, blended.get(engine, 0) - cut)

        # ── Normalize to 1.0 ──
        total = sum(blended.values())
        if total > 0:
            blended = {k: v / total for k, v in blended.items()}

        self.current_weights = blended
        self._weight_history.append(dict(blended))
        if len(self._weight_history) > 50:
            self._weight_history = self._weight_history[-50:]

        return blended


# ─── Hybrid Rules ────────────────────────────────────────────────────────────

class HybridRules:
    """
    Cross-engine trade logic. Detects patterns that no single engine sees.

    Rules are IF/AND boolean conditions. When triggered, they produce
    trade instructions that override or supplement individual engine decisions.

    Output format: list of {"action": str, "reason": str, ...} directives
    that the main loop interprets before running engines.
    """

    def __init__(self):
        self.ref_vol = 0.3

    def evaluate(self, bus: SignalBus, regime: str) -> list[dict]:
        directives = []

        # ── RULE H1: Scalp + Swing convergence ─────────────────────────
        # IF scalp sees SOL oversold AND swing sees BTC trending up
        # AND regime is RISK_ON or VOL_EXPAND
        # → amplify SOL scalp entries (increase position size)
        if (regime in ("RISK_ON", "VOL_EXPAND")
            and bus.scalp_signal_strength > 0.3
            and bus.crypto_trend > TREND_THRESHOLD):
            directives.append({
                "type": "amplify",
                "target": "scalp",
                "factor": 1.5,  # 50% larger scalp positions
                "reason": "Scalp+swing convergence: crypto trending, scalp signals strong",
                "conditions": "scalp_signal>0.3 AND crypto_trend>0.3 AND regime=risk",
            })

        # ── RULE H2: PM edge confirmation ──────────────────────────────
        # IF PM sees BTC contract underpriced
        # AND alt engine sees risk-on altcoin behavior
        # AND scalp aggregate is bullish
        # → increase PM bet size, lower edge threshold for entry
        if (bus.pm_edge_present
            and bus.pm_best_edge > 0.05
            and bus.alt_signal_strength > 0.3
            and bus.scalp_signal_strength > 0.2):
            directives.append({
                "type": "amplify",
                "target": "polymarket",
                "factor": 2.0,  # Double PM bet size
                "reason": "PM edge confirmed by alt+scalp cross-signal",
                "conditions": "pm_edge>0.05 AND alt_signal>0.3 AND scalp_signal>0.2",
            })

        # ── RULE H3: Contagion exit ────────────────────────────────────
        # IF swing stop-loss fires on a major equity (NVDA/AAPL)
        # AND macro correlation is high (>0.6)
        # AND regime just shifted to RISK_OFF
        # → also exit correlated positions (SPY, QQQ, MSFT)
        if (regime == "RISK_OFF"
            and bus.crypto_equity_correlation > CORRELATION_THRESHOLD
            and bus.swing_open_positions > 3):
            directives.append({
                "type": "reduce_exposure",
                "target": "swing",
                "factor": 0.5,  # Cut swing exposure in half
                "reason": "Contagion: RISK_OFF + high correlation + many open positions",
                "conditions": "regime=RISK_OFF AND correlation>0.6 AND swing_positions>3",
            })

        # ── RULE H4: Arb engine hedge ──────────────────────────────────
        # IF arb has open positions AND crypto vol is spiking
        # → reduce altcoin exposure (arb positions correlate with crypto)
        if (bus.arb_open_positions > 0
            and bus.btc_volatility > self.ref_vol * 1.5
            and bus.alt_open_positions > 2):
            directives.append({
                "type": "reduce_exposure",
                "target": "altcoin",
                "factor": 0.7,  # Reduce alt exposure by 30%
                "reason": "Arb+crypto vol spike: correlated risk across arb and alt engines",
                "conditions": "arb_open>0 AND vol_spike AND alt_open>2",
            })

        # ── RULE H5: Mean-reversion fade ───────────────────────────────
        # IF vol is compressing AND swing has been profitable
        # AND PM sees no edge
        # → shift from directional to range-bound strategies
        if (regime == "VOL_COMPRESS"
            and bus.swing_pnl_24h > 20
            and not bus.pm_edge_present):
            directives.append({
                "type": "strategy_shift",
                "target": "swing",
                "mode": "mean_reversion",
                "reason": "Vol compression + profitable swing → shift to mean-reversion",
                "conditions": "vol_compress AND swing_profitable AND no_pm_edge",
            })

        # ── RULE H6: Polymarket cash-out cascade ───────────────────────
        # IF PM has 3+ open positions AND BTC reverses hard (momentum < -0.5)
        # → liquidate all PM positions early (cut losses)
        if (bus.pm_open_positions >= 3
            and bus.btc_momentum_5m < -0.5
            and bus.pm_pnl_24h < -20):
            directives.append({
                "type": "liquidate",
                "target": "polymarket",
                "reason": "PM cascade: 3+ open + BTC reversal + PM losing",
                "conditions": "pm_open>=3 AND btc_momentum<-0.5 AND pm_losing",
            })

        return directives


# ─── Risk Coordinator ────────────────────────────────────────────────────────

class RiskCoordinator:
    """
    Global risk limits that override ALL individual engine decisions.

    Enforces:
      - Max total exposure (% of bankroll)
      - Max correlated exposure
      - Max drawdown → force reduce
      - Minimum cash reserve
      - Correlation-based position limits
    """

    def __init__(self):
        self.drawdown_triggered = False
        self.exposure_capped = False

    def evaluate(self, bus: SignalBus, weights: dict, bankroll: float) -> dict:
        """
        Returns:
            {
                "max_total_exposure": float (fraction of bankroll),
                "max_per_engine": {engine: max_dollars},
                "force_exits": list[symbol],
                "alerts": list[str],
            }
        """
        limits = {
            "max_total_exposure": 0.60,  # Never > 60% deployed
            "max_per_engine": {},
            "force_exits": [],
            "alerts": [],
        }

        # ── Drawdown protection ────────────────────────────────────────
        if bus.current_drawdown_pct > 0.05:
            # At 5% drawdown: cap total exposure at 40%
            limits["max_total_exposure"] = 0.40
            limits["alerts"].append(
                f"DD={bus.current_drawdown_pct:.1%}: exposure capped at 40%")
            self.drawdown_triggered = True

        if bus.current_drawdown_pct > 0.07:
            # At 7% drawdown: cap at 25%, exit non-core positions
            limits["max_total_exposure"] = 0.25
            limits["alerts"].append(
                f"DD={bus.current_drawdown_pct:.1%}: exposure capped at 25%, reduce positions")

        if bus.current_drawdown_pct > DRAWDOWN_CAP:
            # At 8%+: force exit everything except maybe PM settled positions
            limits["max_total_exposure"] = 0.10
            limits["alerts"].append(
                f"DD={bus.current_drawdown_pct:.1%}: CRITICAL — reduce to 10% max")

        # ── Per-engine caps from weights ───────────────────────────────
        for engine, weight in weights.items():
            engine_cap = weight * bankroll * limits["max_total_exposure"]
            limits["max_per_engine"][engine] = round(engine_cap, 2)

        # ── Correlation-based limit ────────────────────────────────────
        if bus.crypto_equity_correlation > CORRELATION_THRESHOLD:
            # High correlation = assets move together = higher portfolio risk
            # Reduce the max exposure cap
            limits["max_total_exposure"] *= 0.75
            limits["alerts"].append(
                f"High correlation ({bus.crypto_equity_correlation:.2f}): "
                f"exposure reduced 25%")

        # ── Minimum cash reserve ───────────────────────────────────────
        cash_reserve = 0.10 * bankroll  # Always keep 10% cash
        deployed_limit = bankroll * limits["max_total_exposure"]
        if bankroll - deployed_limit < cash_reserve:
            limits["max_total_exposure"] = (bankroll - cash_reserve) / bankroll
            limits["alerts"].append("Cash reserve triggered: exposure adjusted")

        return limits


# ─── Meta-Controller ─────────────────────────────────────────────────────────

class MetaController:
    """
    Main orchestrator. Called once per scan cycle before engine execution.

    Usage in paper_engine.py:
        controller = MetaController()

        # In run_once():
        decision = controller.decide(state, swing_scan_results,
                                     scalp_entries, pm_entries, alt_entries)

        # decision['weights'] → capital allocation per engine
        # decision['directives'] → cross-engine trade instructions
        # decision['regime'] → current market regime
        # decision['risk'] → global risk limits
    """

    def __init__(self):
        self.bus = SignalBus()
        self.regime_detector = RegimeDetector()
        self.strategy_weights = StrategyWeights()
        self.hybrid_rules = HybridRules()
        self.risk_coordinator = RiskCoordinator()
        self.last_decision: Optional[dict] = None
        self.cycle_count = 0

    def decide(self, state: dict,
               swing_scan_results: list[dict],
               scalp_entries: list[dict],
               pm_entries: list[dict],
               alt_entries: list[dict],
               arb_tick: Optional[dict] = None,
               crypto_equity_correlation: float = 0.0,
               ccxt_provider=None,
               ) -> dict:
        """
        Run the full meta-controller decision pipeline.

        Args:
            state: Current paper_state.json contents
            swing_scan_results: Output from scan_market()
            scalp_entries: New scalp entries this cycle
            pm_entries: New PM entries this cycle
            alt_entries: New altcoin entries this cycle
            arb_tick: Arb engine output this cycle
            crypto_equity_correlation: BTC/SPY correlation

        Returns decision dict with regime, weights, directives, and risk limits.
        """
        self.cycle_count += 1
        bankroll = state.get("capital", 100000.0)

        # ── 1. Populate SignalBus ──────────────────────────────────────
        self._populate_bus(state, swing_scan_results, scalp_entries,
                           pm_entries, alt_entries, arb_tick,
                           crypto_equity_correlation, ccxt_provider)

        # ── 2. Regime Detection ────────────────────────────────────────
        regime = self.regime_detector.classify(self.bus)
        stability = self.regime_detector.regime_stability

        # ── 3. Strategy Weights ────────────────────────────────────────
        weights = self.strategy_weights.compute(regime, stability, self.bus)
        # Override hybrid rules' vol reference
        self.hybrid_rules.ref_vol = self.regime_detector.median_vol

        # ── 4. Hybrid Rules ────────────────────────────────────────────
        directives = self.hybrid_rules.evaluate(self.bus, regime)

        # ── 5. Risk Coordinator ────────────────────────────────────────
        risk = self.risk_coordinator.evaluate(self.bus, weights, bankroll)

        decision = {
            "cycle": self.cycle_count,
            "regime": regime,
            "regime_stability": round(stability, 3),
            "weights": weights,
            "capital_per_engine": risk["max_per_engine"],
            "directives": directives,
            "risk": {
                "max_total_exposure": risk["max_total_exposure"],
                "alerts": risk["alerts"],
            },
            "signal_summary": {
                "crypto_trend": round(self.bus.crypto_trend, 3),
                "equity_trend": round(self.bus.equity_trend, 3),
                "drawdown": round(self.bus.current_drawdown_pct, 3),
                "correlation": round(self.bus.crypto_equity_correlation, 3),
                "total_exposure": round(self.bus.total_exposure_pct, 3),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        self.last_decision = decision
        return decision

    def _populate_bus(self, state, swing_results, scalp_entries,
                      pm_entries, alt_entries, arb_tick, correlation,
                      ccxt_provider=None):
        """Fill SignalBus from current scan state, including CCXT data."""
        bus = self.bus
        initial = 100000.0

        # ── CCXT cross-exchange data (if available) ─────────────────
        if ccxt_provider is not None:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if not loop.is_running():
                    # Cross-exchange dispersion for regime detection
                    dispersion = loop.run_until_complete(
                        ccxt_provider.cross_exchange_dispersion("BTC-USD"))
                    bus.btc_volatility = max(bus.btc_volatility,
                                            dispersion.get('dispersion_pct', 0) / 100)
                    if dispersion.get('regime_signal') == 'arb_opportunity':
                        bus.arb_opportunity = True
                    # Volume-weighted trend
                    vw_trend = loop.run_until_complete(
                        ccxt_provider.volume_weighted_trend("BTC-USD"))
                    if abs(vw_trend) > 0.1:
                        bus.crypto_trend = float(np.clip(
                            bus.crypto_trend * 0.7 + vw_trend * 0.3, -1, 1))
            except Exception:
                pass

        # ── Price data from swing scan ──
        if swing_results:
            prices = {r["symbol"]: r["price"] for r in swing_results}
            bus.btc_price = prices.get("BTC-USD", prices.get("BTC-USD", 0))
            bus.equity_index_price = prices.get("SPY", 0)

            signals = [r.get("signals", {}).get("score", 0) for r in swing_results]
            bus.swing_signal_strength = float(np.mean([abs(s) for s in signals])) if signals else 0.0

            # Crypto trend: aggregate of SOL, BTC, ETH, AVAX
            crypto_signals = []
            equity_signals = []
            for r in swing_results:
                sym = r.get("symbol", "")
                sig = r.get("signals", {})
                score = sig.get("score", 0)
                if sym in ("SOL-USD", "BTC-USD", "ETH-USD", "AVAX-USD"):
                    crypto_signals.append(score)
                elif sym in ("SPY", "QQQ", "AAPL", "NVDA", "MSFT", "TSLA"):
                    equity_signals.append(score)

            bus.crypto_trend = float(np.mean(crypto_signals)) if crypto_signals else 0.0
            bus.equity_trend = float(np.mean(equity_signals)) if equity_signals else 0.0

            # Volatility from scan results
            vols = [r.get("volatility", 0) for r in swing_results if "volatility" in r]
            bus.btc_volatility = float(np.mean(vols)) if vols else 0.3

        # ── Swing positions ──
        positions = state.get("positions", {})
        bus.swing_open_positions = len(positions)

        # ── Scalp ──
        bus.scalp_open_positions = len(state.get("scalp_positions", {}))
        bus.scalp_signal_strength = float(np.mean([
            e.get("signal_score", 0) for e in scalp_entries
        ])) if scalp_entries else 0.0

        # ── Polymarket ──
        pm_positions = state.get("polymarket_positions", {})
        bus.pm_open_positions = len(pm_positions)
        bus.pm_edge_present = bool(pm_entries)
        bus.pm_best_edge = max(
            [e.get("edge", 0) for e in pm_entries], default=0.0)

        # ── Altcoin ──
        bus.alt_open_positions = len(state.get("alt_positions", {}))
        bus.alt_signal_strength = float(np.mean([
            abs(e.get("confidence", 0)) for e in alt_entries
        ])) if alt_entries else 0.0

        # ── Arb ──
        arb_state = state.get("arb_state", {})
        bus.arb_open_positions = len(arb_state.get("positions", {}))
        bus.arb_opportunity = arb_tick is not None and bool(
            arb_tick.get("entries") if isinstance(arb_tick, dict) else arb_tick)

        # ── P&L 24h ──
        today = datetime.now().strftime("%Y-%m-%d")
        bus.daily_pnl = state.get("daily_pnl", {}).get(today, 0)
        bus.swing_pnl_24h = bus.daily_pnl
        bus.pm_pnl_24h = state.get("polymarket_pnl", 0)
        bus.alt_pnl_24h = state.get("alt_pnl", 0)

        # ── Cross-market ──
        bus.crypto_equity_correlation = correlation
        bus.total_open_positions = (
            bus.swing_open_positions + bus.scalp_open_positions +
            bus.pm_open_positions + bus.alt_open_positions +
            bus.arb_open_positions)

        # ── Exposure ──
        invested = sum(p.get("shares", 0) * p.get("entry_price", 0)
                       for p in positions.values())
        bus.total_exposure_pct = invested / max(state.get("capital", initial), 1.0)

        # ── Drawdown ──
        peak = state.get("peak_capital", initial)
        current = state.get("capital", initial)
        bus.current_drawdown_pct = max(0.0, (peak - current) / max(peak, 1.0))

        bus.scan_number = state.get("scans", 0)
        bus.swing_scan_results = swing_results

    def report(self) -> str:
        """Generate human-readable report of the last decision."""
        if self.last_decision is None:
            return "Meta-Controller: No decision yet."

        d = self.last_decision
        regime_emoji = {
            "RISK_ON": "🟢", "SIDEWAYS": "🟡", "RISK_OFF": "🔴",
            "CRYPTO_DIV": "🟣", "VOL_COMPRESS": "🔵", "VOL_EXPAND": "🟠",
            "NEUTRAL": "⚪",
        }

        lines = [
            "",
            "🧠 META-CONTROLLER",
            f"   Regime: {regime_emoji.get(d['regime'], '?')} {d['regime']} "
            f"(stability: {d['regime_stability']:.0%})",
        ]

        # Weights
        lines.append("   Capital Allocation:")
        for engine, w in d["weights"].items():
            cap = d["capital_per_engine"].get(engine, 0)
            lines.append(f"     {engine:12s}: {w:.0%} → ${cap:,.0f}")

        # Directives
        if d["directives"]:
            lines.append(f"   Cross-Engine Rules ({len(d['directives'])}):")
            for directive in d["directives"]:
                lines.append(
                    f"     ⚡ {directive['type']} → {directive['target']}: "
                    f"{directive['reason'][:80]}")

        # Risk
        if d["risk"]["alerts"]:
            lines.append("   ⚠ Risk Alerts:")
            for alert in d["risk"]["alerts"]:
                lines.append(f"     {alert}")

        return "\n".join(lines)


# ─── Test ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    controller = MetaController()

    # Simulate a scan cycle
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())

        # Minimal scan results simulation
        positions = state.get("positions", {})
        fake_scan = []
        for sym, pos in list(positions.items())[:5]:
            fake_scan.append({
                "symbol": sym,
                "price": pos.get("entry_price", 100),
                "signals": {
                    "score": np.random.uniform(-0.3, 0.5),
                    "rsi": np.random.uniform(30, 70),
                },
                "volatility": np.random.uniform(0.1, 0.4),
            })

        # Also simulate SPY and BTC if not in positions
        fake_scan.append({"symbol": "SPY", "price": 740,
                         "signals": {"score": 0.1, "rsi": 55}, "volatility": 0.2})
        fake_scan.append({"symbol": "BTC-USD", "price": 81000,
                         "signals": {"score": 0.2, "rsi": 52}, "volatility": 0.3})

        decision = controller.decide(
            state, fake_scan,
            scalp_entries=[{"signal_score": 0.4}],
            pm_entries=[],
            alt_entries=[{"confidence": 0.35}],
            arb_tick=None,
            crypto_equity_correlation=0.3,
        )

        print(controller.report())
        print(f"\nDecision JSON: {json.dumps(decision, indent=2, default=str)[:500]}...")
    else:
        print(f"No state file at {STATE_FILE}")
