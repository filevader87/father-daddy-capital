#!/usr/bin/env python3
"""
Father Daddy Capital — Paper Trading Engine
Partial leash-off mode. All trades are simulated. No real money.
Target: $100/day → $500/day

Strategy: Multi-signal aggregation across momentum, mean-reversion, and trend-following
with position sizing via Kelly-inspired risk allocation.

Data: yfinance (all symbols). CCXT available for Phase 1B paper engine upgrade.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import os
import sys
import time
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────────────────────

ASSETS = {
    "crypto": ["SOL-USD", "BTC-USD", "ETH-USD", "AVAX-USD"],
    "equities": ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "TSLA"],
}

ALL_SYMBOLS = ASSETS["crypto"] + ASSETS["equities"]
INITIAL_CAPITAL = 100_000.0
MAX_POSITION_PCT = 0.10
MAX_PORTFOLIO_RISK = 0.15
STOP_LOSS_PCT = 0.05
TAKE_PROFIT_PCT = 0.10
LOOKBACK_DAYS = 50
SCAN_INTERVAL_MINUTES = 15

OUTPUT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/output")
LOG_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/logs")
STATE_FILE = Path("/mnt/c/Users/12035/father_daddy_capital/output/paper_state.json")

import yfinance as yf

# ─── Neural Plasticity ──────────────────────────────────────────────────────
from src.neural.plastic_network import (
    NeuralPlasticityEngine, encode_signal_vector, scale_pnl_to_target,
)

# ─── Meta-Controller ────────────────────────────────────────────────────────
from src.trading.meta_controller import MetaController

# ─── CCXT Data Layer ────────────────────────────────────────────────────────
from src.trading.ccxt_layer import CCXTDataProvider, get_provider, shutdown_provider

# ─── Orderbook Layer (from poly-maker) ─────────────────────────────────────
from fdc_orderbook import analyze_orderbook, simulate_fill, get_trade_params

# ─── Smart Money API (Track 5) ─────────────────────────────────────────────
from fdc_smart_money_api import run_smart_money_cycle, smart_money_summary

_ccxt_provider = None
_neural = None
_meta_controller = None

def _get_ccxt() -> CCXTDataProvider | None:
    """Returns the active CCXT provider, or None if unavailable."""
    global _ccxt_provider
    if _ccxt_provider is not None:
        return _ccxt_provider
    if not _check_ccxt():
        return None
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if not loop.is_running():
            _ccxt_provider = loop.run_until_complete(get_provider())
            return _ccxt_provider
    except Exception:
        pass
    return None

def _get_neural():
    global _neural
    if _neural is None:
        _neural = NeuralPlasticityEngine()
    return _neural

def _get_meta():
    global _meta_controller
    if _meta_controller is None:
        _meta_controller = MetaController()
    return _meta_controller


# ─── Import Engines ──────────────────────────────────────────────────────────
import importlib.util

_scalp_spec = importlib.util.spec_from_file_location(
    "scalp_engine", Path(__file__).parent / "src" / "trading" / "scalp_engine.py"
)
_scalp_module = importlib.util.module_from_spec(_scalp_spec)
_scalp_spec.loader.exec_module(_scalp_module)
run_scalp_cycle = _scalp_module.run_scalp_cycle
scalp_summary = _scalp_module.scalp_summary

_pm_spec = importlib.util.spec_from_file_location(
    "polymarket_engine", Path(__file__).parent / "fdc_polymarket.py"
)
_pm_module = importlib.util.module_from_spec(_pm_spec)
_pm_spec.loader.exec_module(_pm_module)
run_polymarket_cycle = _pm_module.run_polymarket_cycle
polymarket_summary = _pm_module.polymarket_summary

_alt_spec = importlib.util.spec_from_file_location(
    "alt_scanner", Path(__file__).parent / "fdc_alt_scanner.py"
)
_alt_module = importlib.util.module_from_spec(_alt_spec)
_alt_spec.loader.exec_module(_alt_module)
run_alt_cycle = _alt_module.run_alt_cycle
alt_summary = _alt_module.alt_summary
ALTCOIN_UNIVERSE = _alt_module.ALTCOIN_UNIVERSE

# ─── CCXT Status Check (non-blocking, logged only) ──────────────────────────

_CCXT_STATUS = "unknown"

def _check_ccxt():
    """Quick CCXT availability check — logged, not required."""
    global _CCXT_STATUS
    try:
        sys.path.insert(0, str(Path(__file__).parent / "src" / "trading"))
        from market_data_provider import MarketDataProvider
        import asyncio
        provider = MarketDataProvider(use_ccxt=True)
        asyncio.run(provider.connect())
        if provider.using_ccxt:
            _CCXT_STATUS = "connected (coinbase/kraken/okx/gate)"
        else:
            _CCXT_STATUS = "disabled"
        asyncio.run(provider.close())
    except Exception as e:
        _CCXT_STATUS = f"unavailable"


# ─── Signal Generation ───────────────────────────────────────────────────────

def compute_signals(prices: pd.Series) -> dict:
    """Generate multi-factor signals from price series."""
    if len(prices) < 20:
        return {"score": 0, "signals": {}, "confidence": 0}

    delta = prices.diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    current_rsi = float(rsi.iloc[-1])

    ema12 = prices.ewm(span=12).mean()
    ema26 = prices.ewm(span=26).mean()
    macd = ema12 - ema26
    signal_line = macd.ewm(span=9).mean()
    macd_signal = 1 if macd.iloc[-1] > signal_line.iloc[-1] else -1

    sma20 = prices.rolling(20).mean().iloc[-1]
    sma50 = prices.rolling(50).mean().iloc[-1] if len(prices) >= 50 else sma20
    current_price = float(prices.iloc[-1])
    trend_20 = 1 if current_price > sma20 else -1
    trend_50 = 1 if current_price > sma50 else -1

    rets = prices.pct_change().dropna()
    volatility = float(rets.rolling(20).std().iloc[-1] * np.sqrt(365))

    momentum_5d = float((prices.iloc[-1] / prices.iloc[-6] - 1)) if len(prices) >= 6 else 0

    z_score = float((current_price - sma20) / (prices.rolling(20).std().iloc[-1] + 1e-9))
    mean_reversion = -z_score

    rsi_signal = 1 if current_rsi < 30 else (-1 if current_rsi > 70 else 0)
    trend_signal = (trend_20 + trend_50) / 2.0

    score = (
        rsi_signal * 0.20 + macd_signal * 0.25 + trend_signal * 0.25 +
        np.clip(momentum_5d * 10, -1, 1) * 0.15 +
        np.clip(mean_reversion * 0.5, -1, 1) * 0.15
    )

    confidence = min(0.95, max(0.3, 0.6 + abs(score) * 0.3 - volatility * 0.5))

    # Blend neural plasticity prediction if available
    neural_score = 0.0
    try:
        neural = _get_neural()
        if neural.network.updates > 100:
            signals_dict = {
                "rsi": rsi_signal, "macd": macd_signal, "trend": trend_signal,
                "momentum": np.clip(momentum_5d * 10, -1, 1),
                "mean_reversion": np.clip(mean_reversion * 0.5, -1, 1),
            }
            x = encode_signal_vector({
                "signals": signals_dict,
                "volatility": volatility,
                "asset_class": "unknown",
                "confidence": confidence,
            })
            neural_score = neural.network.predict(x)
            # Blend: 70% traditional signal, 30% neural (grows with updates)
            blend_w = min(0.30, (neural.network.updates - 100) / 200)
            score = score * (1 - blend_w) + neural_score * blend_w
    except Exception:
        pass  # Neural is non-critical — degrade gracefully

    return {
        "score": round(score, 3), "rsi": round(current_rsi, 1),
        "volatility": round(volatility, 3), "momentum_5d": round(momentum_5d, 4),
        "trend": "up" if trend_20 > 0 else "down",
        "confidence": round(confidence, 3),
        "signals": {
            "rsi": rsi_signal, "macd": macd_signal, "trend": trend_signal,
            "momentum": round(np.clip(momentum_5d * 10, -1, 1), 3),
            "mean_reversion": round(np.clip(mean_reversion * 0.5, -1, 1), 3),
        }
    }


def scan_market(symbols: list[str], lookback: int = LOOKBACK_DAYS) -> list[dict]:
    """Scan all symbols via yfinance."""
    results = []
    end = datetime.now()
    start = end - timedelta(days=lookback)

    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=start, end=end)
            if len(hist) < 20:
                continue
            prices = hist["Close"]
            current_price = float(prices.iloc[-1])
            signal = compute_signals(prices)
            results.append({
                "symbol": symbol, "price": round(current_price, 2),
                "asset_class": "crypto" if "-USD" in symbol else "equity",
                **signal,
            })
        except Exception as e:
            print(f"  ⚠ {symbol}: {e}", file=sys.stderr)

    results.sort(key=lambda x: abs(x["score"]), reverse=True)
    return results


def fetch_btc_5min() -> list[float]:
    """Fetch BTC 5-minute candles for Polymarket signals."""
    try:
        btc = yf.Ticker("BTC-USD")
        hist = btc.history(period="5d", interval="5m")
        if len(hist) < 14:
            return []
        return hist["Close"].tolist()[-60:]
    except Exception as e:
        print(f"  ⚠ BTC-USD 5m: {e}", file=sys.stderr)
        return []


# ─── Position Sizing ─────────────────────────────────────────────────────────

def calculate_position_size(
    signal_score: float, confidence: float, volatility: float,
    current_price: float, available_capital: float,
) -> float:
    """Kelly-inspired position sizing with orderbook-aware risk constraints."""
    base_allocation = available_capital * MAX_POSITION_PCT
    signal_strength = abs(signal_score)
    scaled = base_allocation * signal_strength * confidence
    vol_penalty = max(0.3, 1.0 - volatility * 2.0)
    scaled *= vol_penalty

    # Volatility multiplier: high-vol assets deserve MORE capital if signal is strong
    # (from poly-maker philosophy: vol IS the product)
    if volatility > 0.03 and signal_strength > 0.6:
        scaled *= min(2.0, 1.0 + volatility * 15)

    scaled = min(scaled, available_capital * MAX_POSITION_PCT)

    shares = scaled / current_price
    if np.isnan(shares) or np.isinf(shares) or shares <= 0:
        return 0
    if current_price > 1000:
        shares = round(shares, 6)
    else:
        shares = int(max(1, shares))
    return max(0, shares)


# ─── State Management ────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "capital": INITIAL_CAPITAL, "positions": {},
        "trade_history": [], "total_pnl": 0.0,
        "daily_pnl": {}, "scans": 0,
        "started": datetime.now().isoformat(),
    }


def save_state(state: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ─── Trading Logic ───────────────────────────────────────────────────────────

def execute_scan(state: dict, scan_results: list[dict]) -> list[dict]:
    """Generate orders from scan results against current state."""
    orders = []
    current_prices = {r["symbol"]: r["price"] for r in scan_results}

    for symbol, pos in list(state["positions"].items()):
        cp = current_prices.get(symbol)
        if cp is None or np.isnan(cp):
            continue
        pnl_pct = (cp - pos["entry_price"]) / pos["entry_price"]
        if pnl_pct <= -STOP_LOSS_PCT:
            orders.append({"action": "SELL", "symbol": symbol, "shares": pos["shares"],
                           "price": cp, "reason": f"STOP_LOSS ({pnl_pct*100:.1f}%)",
                           "_pos_data": {  # snapshot for neural/PnL after deletion
                               "entry_price": pos["entry_price"],
                               "entry_signals": pos.get("entry_signals", {}),
                               "entry_volatility": pos.get("entry_volatility", 0.2),
                               "entry_confidence": pos.get("entry_confidence", 0.5),
                               "asset_class": pos.get("asset_class", "equity"),
                           }})
            del state["positions"][symbol]
        elif pnl_pct >= TAKE_PROFIT_PCT:
            orders.append({"action": "SELL", "symbol": symbol, "shares": pos["shares"],
                           "price": cp, "reason": f"TAKE_PROFIT ({pnl_pct*100:.1f}%)",
                           "_pos_data": {
                               "entry_price": pos["entry_price"],
                               "entry_signals": pos.get("entry_signals", {}),
                               "entry_volatility": pos.get("entry_volatility", 0.2),
                               "entry_confidence": pos.get("entry_confidence", 0.5),
                               "asset_class": pos.get("asset_class", "equity"),
                           }})
            del state["positions"][symbol]

    invested = sum(
        state["positions"][s]["shares"] * current_prices.get(s, 0)
        for s in state["positions"] if not np.isnan(current_prices.get(s, 0))
    )
    total_value = state["capital"] + invested
    available = max(0, min(state["capital"], total_value * (1 - MAX_PORTFOLIO_RISK)) - invested)

    for result in scan_results:
        symbol = result["symbol"]
        if symbol in state["positions"] or len(state["positions"]) >= 8:
            if len(state["positions"]) >= 8:
                break
            continue
        if abs(result["score"]) < 0.3:
            continue

        shares = calculate_position_size(
            signal_score=result["score"], confidence=result["confidence"],
            volatility=result["volatility"], current_price=result["price"],
            available_capital=available)
        if shares <= 0:
            continue

        cost = shares * result["price"]
        if cost > available * 0.5:
            continue

        action = "BUY" if result["score"] > 0 else "SHORT"
        orders.append({"action": action, "symbol": symbol, "shares": shares,
                       "price": result["price"],
                       "reason": f"SIGNAL ({result['score']:.2f}, conf={result['confidence']:.2f})"})

        if action == "BUY":
            state["positions"][symbol] = {
                "shares": shares, "entry_price": result["price"],
                "entry_date": datetime.now().isoformat(),
                "asset_class": result["asset_class"],
                "entry_signals": result.get("signals", {}),
                "entry_volatility": result.get("volatility", 0.2),
                "entry_confidence": result.get("confidence", 0.5),
            }
            state["capital"] -= cost
            available -= cost

    for order in orders:
        if order["action"] == "SELL":
            # Position was already deleted from state — use snapshot attached to order
            pos_data = order.pop("_pos_data", {})
            entry_price = pos_data.get("entry_price", order.get("entry_price", order["price"]))
            pnl = (order["price"] - entry_price) * order["shares"]
            state["capital"] += order["price"] * order["shares"]
            state["total_pnl"] += pnl
            today = datetime.now().strftime("%Y-%m-%d")
            state["daily_pnl"][today] = state["daily_pnl"].get(today, 0) + pnl
            state["trade_history"].append({
                **order, "entry_price": entry_price,
                "pnl": round(pnl, 2),
                "pnl_pct": round((order["price"] - entry_price) / entry_price * 100, 2),
                "timestamp": datetime.now().isoformat(),
            })

            # Feed neural plasticity layer
            try:
                neural = _get_neural()
                pnl_pct = (order["price"] - entry_price) / entry_price
                result_for_neural = {
                    "signals": pos_data.get("entry_signals", {
                        "rsi": 0, "macd": 0, "trend": 0, "momentum": 0, "mean_reversion": 0,
                    }),
                    "volatility": pos_data.get("entry_volatility", 0.2),
                    "asset_class": pos_data.get("asset_class", "equity"),
                    "confidence": pos_data.get("entry_confidence", 0.5),
                }
                pred = neural.predict_return(result_for_neural)
                neural.learn(result_for_neural, pred, pnl_pct)
            except Exception:
                pass  # Neural is non-critical

    state["scans"] += 1
    return orders


# ─── Reporting ───────────────────────────────────────────────────────────────

def generate_report(state: dict, scan_results: list[dict], orders: list[dict]) -> str:
    """Generate Markdown report."""
    current_prices = {r["symbol"]: r["price"] for r in scan_results}
    invested = 0.0
    unrealized = 0.0

    for sym, pos in state["positions"].items():
        ep = pos["entry_price"]
        sh = pos["shares"]
        invested += sh * ep
        cp = current_prices.get(sym, ep)
        if not np.isnan(cp):
            unrealized += (sh * cp) - (sh * ep)

    total_equity = state["capital"] + invested + unrealized
    today = datetime.now().strftime("%Y-%m-%d")
    daily_pnl = state["daily_pnl"].get(today, 0)

    report = (
        f"📊 Father Daddy Capital — Paper Trading Report\n"
        f"{datetime.now().strftime('%Y-%m-%d %H:%M EST')} | Scan #{state['scans']}\n\n"
        f"💰 Capital: ${state['capital']:,.2f}\n"
        f"📈 Invested: ${invested:,.2f}\n"
        f"📊 Unrealized: ${unrealized:,.2f}\n"
        f"🏦 Total Equity: ${total_equity:,.2f} ({(total_equity/INITIAL_CAPITAL-1)*100:+.1f}%)\n"
        f"📅 Today P&L: ${daily_pnl:+,.2f}\n\n"
    )

    if orders:
        report += "📋 Orders Executed:\n"
        for o in orders:
            emoji = "🟢" if o["action"] == "BUY" else "🔴"
            pnl_str = f" → P&L ${o.get('pnl',0):+,.2f}" if "pnl" in o else ""
            report += f"  {emoji} {o['action']} {o['symbol']} ×{o['shares']} @ ${o['price']:.2f}{pnl_str}\n"
    else:
        report += "📋 No orders this scan.\n"

    if state["positions"]:
        report += "\n📌 Open Positions:\n"
        for sym, pos in state["positions"].items():
            cp = current_prices.get(sym, pos["entry_price"])
            pnl = (cp - pos["entry_price"]) * pos["shares"] if not np.isnan(cp) else 0
            pnl_pct = (cp / pos["entry_price"] - 1) * 100 if not np.isnan(cp) else 0
            report += f"  {sym}: {pos['shares']} @ ${pos['entry_price']:.2f} → ${cp:.2f} | ${pnl:+,.2f} ({pnl_pct:+.1f}%)\n"

    report += f"\n⏳ Next scan: {SCAN_INTERVAL_MINUTES} min\n"
    if _CCXT_STATUS != "unknown":
        report += f"🔌 CCXT: {_CCXT_STATUS}\n"
    return report


# ─── Main Loop ───────────────────────────────────────────────────────────────

def run_once():
    """Single scan cycle. Returns report string."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    state = load_state()

    state.setdefault("scalp_positions", {})
    state.setdefault("scalp_exits", [])
    state.setdefault("scalp_scans", 0)

    print(f"🔍 Scanning {len(ALL_SYMBOLS)} swing + {len(state.get('scalp_positions', {}))} scalp...")
    scan_results = scan_market(ALL_SYMBOLS)
    orders = execute_scan(state, scan_results)

    scalp_entries, scalp_exits, _ = run_scalp_cycle(state, neural=_get_neural())
    all_orders = orders + [
        {**e, "_type": "scalp", "_action": "entry"} for e in scalp_entries
    ] + [
        {**x, "_type": "scalp", "_action": "exit"} for x in scalp_exits
    ]

    btc_5m = fetch_btc_5min()
    pm_entries, pm_settlements = run_polymarket_cycle(state, btc_5m)
    for e in (pm_entries or []):
        all_orders.append({**e, "_type": "polymarket", "_action": "entry"})
    for s in (pm_settlements or []):
        all_orders.append({**s, "_type": "polymarket", "_action": "settle"})

    try:
        alt_entries, alt_exits, _ = run_alt_cycle(state)
        for e in (alt_entries or []):
            all_orders.append({**e, "_type": "altcoin", "_action": "entry"})
        for x in (alt_exits or []):
            all_orders.append({**x, "_type": "altcoin", "_action": "exit"})
    except Exception:
        alt_entries, alt_exits = [], []

    # ── Polymarket Complete-Set Arb ──────────────────────────────────
    try:
        _arb_spec = importlib.util.spec_from_file_location(
            "fdc_arb", Path(__file__).parent / "fdc_arb.py"
        )
        _arb_module = importlib.util.module_from_spec(_arb_spec)
        _arb_spec.loader.exec_module(_arb_module)
        arb_tick = _arb_module.run_arb_cycle(state)
        for e in (arb_tick.get("entries") if isinstance(arb_tick, dict) else arb_tick or []):
            all_orders.append({**e, "_type": "arb", "_action": "entry"})
        for s in (arb_tick.get("settlements") if isinstance(arb_tick, dict) else []):
            all_orders.append({**s, "_type": "arb", "_action": "settle"})
    except Exception:
        arb_tick = None

    # ── Track 5: Smart Money Signals ──────────────────────────────────
    try:
        smart_entries, smart_signals = run_smart_money_cycle(state)
        for e in (smart_entries or []):
            all_orders.append({**e, "_type": "smart_money", "_action": "entry"})
    except Exception:
        smart_entries, smart_signals = [], []

    # ── META-CONTROLLER — auto-adaptive strategy layer ────────────────
    try:
        # Compute crypto/equity correlation from scan results
        crypto_prices = [r["price"] for r in scan_results if r.get("asset_class") == "crypto"]
        equity_prices = [r["price"] for r in scan_results if r.get("asset_class") == "equity"]
        correlation = 0.0
        if len(crypto_prices) >= 2 and len(equity_prices) >= 2:
            correlation = float(np.corrcoef(crypto_prices[:min(len(crypto_prices), len(equity_prices))],
                                            equity_prices[:min(len(crypto_prices), len(equity_prices))])[0, 1])
            if np.isnan(correlation):
                correlation = 0.0

        meta = _get_meta()
        ccxt = _get_ccxt()
        decision = meta.decide(
            state=state,
            swing_scan_results=scan_results,
            scalp_entries=scalp_entries,
            pm_entries=pm_entries or [],
            alt_entries=alt_entries or [],
            arb_tick=arb_tick,
            crypto_equity_correlation=correlation,
            ccxt_provider=ccxt,
        )

        # Apply meta decision directives
        for directive in decision["directives"]:
            if directive["type"] == "amplify" and directive["target"] == "scalp":
                for se in scalp_entries:
                    se["position_usd"] = se.get("position_usd", 500) * directive["factor"]
            elif directive["type"] == "amplify" and directive["target"] == "polymarket":
                for pe in (pm_entries or []):
                    pe["bet_size"] = pe.get("bet_size", 50) * directive["factor"]
            elif directive["type"] == "reduce_exposure" and directive["target"] == "altcoin":
                for ae in (alt_entries or []):
                    ae["value"] = ae.get("value", 500) * directive["factor"]
            elif directive["type"] == "liquidate" and directive["target"] == "polymarket":
                # Force liquidate open PM positions
                for key in list(state.get("polymarket_positions", {}).keys()):
                    pos = state["polymarket_positions"].pop(key)
                    state["trade_journal"].append({
                        "type": "polymarket", "key": key,
                        "action": pos.get("action", "LIQUIDATE"),
                        "pnl": -pos.get("bet", pos.get("bet_size", 50)),
                        "settled": False,
                        "exit_reason": "HYBRID_CASCADE",
                        "timestamp": datetime.now().isoformat(),
                    })
                    state["polymarket_pnl"] = state.get("polymarket_pnl", 0) - pos.get("bet", pos.get("bet_size", 50))

    except Exception:
        pass  # Meta-controller is non-critical — degrade gracefully

    # ── Calibration (every 50 scans) ─────────────────────────────────
    try:
        if state["scans"] % 50 == 0 and state["scans"] > 0:
            _cal_spec = importlib.util.spec_from_file_location(
                "fdc_calibrate", Path(__file__).parent / "fdc_calibrate.py"
            )
            _cal_module = importlib.util.module_from_spec(_cal_spec)
            _cal_spec.loader.exec_module(_cal_module)
            cal_report = _cal_module.run_calibration()
            state.setdefault("calibration_history", []).append({
                "scan": state["scans"],
                "composite": cal_report.get("composite_score", 0),
                "ts": datetime.now().isoformat(),
            })
    except Exception:
        pass

    save_state(state)

    report = generate_report(state, scan_results, orders)
    if scalp_entries or scalp_exits or state.get("scalp_positions"):
        report += scalp_summary(
            state.get("scalp_positions", {}), state.get("scalp_scans", 0),
            state.get("scalp_exits", [])[-5:])
    if state.get("polymarket_positions") or pm_entries or pm_settlements:
        report += polymarket_summary(state, pm_settlements or [])
    if state.get("alt_positions") or alt_entries or alt_exits:
        report += alt_summary(state, alt_entries or [], alt_exits or [])

    # Smart Money summary
    if state.get("smart_money_positions") or smart_entries:
        report += smart_money_summary(state, smart_entries)

    # Arb summary
    if arb_tick is not None:
        try:
            _arb_spec2 = importlib.util.spec_from_file_location(
                "fdc_arb_summary", Path(__file__).parent / "fdc_arb.py"
            )
            _arb_mod2 = importlib.util.module_from_spec(_arb_spec2)
            _arb_spec2.loader.exec_module(_arb_mod2)
            report += _arb_mod2.arb_summary(state, arb_tick)
        except Exception:
            pass

    # Neural plasticity stats
    try:
        neural = _get_neural()
        if neural.network.updates > 0:
            stats = neural.stats()
            report += (
                f"\n🧠 Neural Layer | {stats['updates']} updates | "
                f"LR: {stats['learning_rate']:.6f} | "
                f"Loss: {stats['avg_loss']:.4f} | "
                f"Acc: {stats['rolling_accuracy']:.1%}\n"
            )
    except Exception:
        pass

    # Meta-controller report
    try:
        meta = _get_meta()
        report += meta.report()
    except Exception:
        pass

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    (OUTPUT_DIR / f"report_{ts}.md").write_text(report)
    (OUTPUT_DIR / f"scan_{ts}.json").write_text(json.dumps({
        "scan_results": scan_results, "orders": orders,
        "state_summary": {
            "capital": state["capital"], "total_pnl": state["total_pnl"],
            "positions": len(state["positions"]), "scans": state["scans"],
        }}, indent=2, default=str))

    print(report)
    return report


def run_continuous():
    """Continuous loop."""
    _check_ccxt()
    print("🚀 FDC — Quad-Track + CCXT-ready")
    print(f"   Tracks: Swing | Scalp | Polymarket | Alt Farm")
    print(f"   CCXT: {_CCXT_STATUS}")
    print(f"   Scan: {SCAN_INTERVAL_MINUTES} min | Ctrl+C to stop\n")

    while True:
        try:
            run_once()
            time.sleep(SCAN_INTERVAL_MINUTES * 60)
        except KeyboardInterrupt:
            print("\n👋 Stopped.")
            break
        except Exception as e:
            print(f"❌ {e}", file=sys.stderr)
            time.sleep(300)


if __name__ == "__main__":
    if "--once" in sys.argv:
        _check_ccxt()
        run_once()
    elif "--continuous" in sys.argv or "-c" in sys.argv:
        run_continuous()
    else:
        print(__doc__)
