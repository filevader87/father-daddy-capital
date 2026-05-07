#!/usr/bin/env python3
"""
Father Daddy Capital — Paper Trading Engine
Partial leash-off mode. All trades are simulated. No real money.
Target: $100/day → $500/day

Strategy: Multi-signal aggregation across momentum, mean-reversion, and trend-following
with position sizing via Kelly-inspired risk allocation.
"""

import yfinance as yf
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
MAX_POSITION_PCT = 0.10       # 10% max per position
MAX_PORTFOLIO_RISK = 0.15      # 15% max total exposure
STOP_LOSS_PCT = 0.05           # 5% stop loss
TAKE_PROFIT_PCT = 0.10         # 10% take profit
LOOKBACK_DAYS = 50             # Historical data window
SCAN_INTERVAL_MINUTES = 60     # How often to scan

OUTPUT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/output")
LOG_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/logs")
STATE_FILE = Path("/mnt/c/Users/12035/father_daddy_capital/output/paper_state.json")

# ─── Import Scalp Engine ─────────────────────────────────────────────────────
# Direct import to avoid src/__init__.py legacy torch dependency chain
import importlib.util
_scalp_spec = importlib.util.spec_from_file_location(
    "scalp_engine", 
    Path(__file__).parent / "src" / "trading" / "scalp_engine.py"
)
_scalp_module = importlib.util.module_from_spec(_scalp_spec)
_scalp_spec.loader.exec_module(_scalp_module)
run_scalp_cycle = _scalp_module.run_scalp_cycle
scalp_summary = _scalp_module.scalp_summary

# ─── Signal Generation ───────────────────────────────────────────────────────

def compute_signals(prices: pd.Series) -> dict:
    """Generate multi-factor signals from price series."""
    if len(prices) < 20:
        return {"score": 0, "signals": {}, "confidence": 0}
    
    # RSI (14-day)
    delta = prices.diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    current_rsi = float(rsi.iloc[-1])
    
    # MACD
    ema12 = prices.ewm(span=12).mean()
    ema26 = prices.ewm(span=26).mean()
    macd = ema12 - ema26
    signal_line = macd.ewm(span=9).mean()
    macd_signal = 1 if macd.iloc[-1] > signal_line.iloc[-1] else -1
    
    # Trend strength (price vs 20/50 SMA)
    sma20 = prices.rolling(20).mean().iloc[-1]
    sma50 = prices.rolling(50).mean().iloc[-1] if len(prices) >= 50 else sma20
    current_price = float(prices.iloc[-1])
    trend_20 = 1 if current_price > sma20 else -1
    trend_50 = 1 if current_price > sma50 else -1
    
    # Volatility (20-day annualized)
    returns = prices.pct_change().dropna()
    volatility = float(returns.rolling(20).std().iloc[-1] * np.sqrt(365))
    
    # Momentum (5-day return)
    momentum_5d = float((prices.iloc[-1] / prices.iloc[-6] - 1)) if len(prices) >= 6 else 0
    
    # Mean reversion signal
    z_score = float((current_price - sma20) / (prices.rolling(20).std().iloc[-1] + 1e-9))
    mean_reversion = -z_score  # negative: buy when below mean
    
    # Composite score
    rsi_signal = 1 if current_rsi < 30 else (-1 if current_rsi > 70 else 0)
    trend_signal = (trend_20 + trend_50) / 2.0
    
    score = (
        rsi_signal * 0.20 +
        macd_signal * 0.25 +
        trend_signal * 0.25 +
        np.clip(momentum_5d * 10, -1, 1) * 0.15 +
        np.clip(mean_reversion * 0.5, -1, 1) * 0.15
    )
    
    confidence = min(0.95, max(0.3, 
        0.6 + abs(score) * 0.3 - volatility * 0.5
    ))
    
    return {
        "score": round(score, 3),
        "rsi": round(current_rsi, 1),
        "volatility": round(volatility, 3),
        "momentum_5d": round(momentum_5d, 4),
        "trend": "up" if trend_20 > 0 else "down",
        "confidence": round(confidence, 3),
        "signals": {
            "rsi": rsi_signal,
            "macd": macd_signal,
            "trend": trend_signal,
            "momentum": round(np.clip(momentum_5d * 10, -1, 1), 3),
            "mean_reversion": round(np.clip(mean_reversion * 0.5, -1, 1), 3),
        }
    }

def scan_market(symbols: list[str], lookback: int = LOOKBACK_DAYS) -> list[dict]:
    """Scan all symbols and return ranked signals."""
    results = []
    end = datetime.now()
    start = end - timedelta(days=lookback)
    
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=start, end=end)
            if len(hist) < 20:
                continue
            
            prices = hist['Close']
            current_price = float(prices.iloc[-1])
            signal = compute_signals(prices)
            
            results.append({
                "symbol": symbol,
                "price": round(current_price, 2),
                "asset_class": "crypto" if "-USD" in symbol else "equity",
                **signal,
            })
        except Exception as e:
            print(f"  ⚠ {symbol}: {e}", file=sys.stderr)
    
    # Sort by absolute signal score
    results.sort(key=lambda x: abs(x["score"]), reverse=True)
    return results


# ─── Position Sizing ─────────────────────────────────────────────────────────

def calculate_position_size(
    signal_score: float,
    confidence: float,
    volatility: float,
    current_price: float,
    available_capital: float,
) -> float:
    """Kelly-inspired position sizing with risk constraints."""
    # Base size: fraction of capital scaled by signal strength
    base_allocation = available_capital * MAX_POSITION_PCT
    
    # Scale by signal strength and confidence
    signal_strength = abs(signal_score)
    scaled = base_allocation * signal_strength * confidence
    
    # Reduce for high volatility
    vol_penalty = max(0.3, 1.0 - volatility * 2.0)
    scaled *= vol_penalty
    
    # Hard cap at max position
    scaled = min(scaled, available_capital * MAX_POSITION_PCT)
    
    # Convert to shares (round down to integer for equities, fractional for crypto)
    shares = scaled / current_price
    if current_price > 1000:  # Likely crypto
        shares = round(shares, 6)
    else:
        shares = int(shares)
    
    return max(0, shares)


# ─── State Management ────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "capital": INITIAL_CAPITAL,
        "positions": {},       # symbol → {shares, entry_price, entry_date}
        "trade_history": [],
        "total_pnl": 0.0,
        "daily_pnl": {},
        "scans": 0,
        "started": datetime.now().isoformat(),
    }

def save_state(state: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)


# ─── Trading Logic ───────────────────────────────────────────────────────────

def execute_scan(state: dict, scan_results: list[dict]) -> dict:
    """Generate orders from scan results against current state."""
    orders = []
    current_prices = {r["symbol"]: r["price"] for r in scan_results}
    
    # Check stop loss / take profit on existing positions
    for symbol, pos in list(state["positions"].items()):
        if symbol not in current_prices:
            continue
        current_price = current_prices[symbol]
        entry_price = pos["entry_price"]
        pnl_pct = (current_price - entry_price) / entry_price
        
        # Stop loss
        if pnl_pct <= -STOP_LOSS_PCT:
            orders.append({
                "action": "SELL",
                "symbol": symbol,
                "shares": pos["shares"],
                "price": current_price,
                "reason": f"STOP_LOSS ({pnl_pct*100:.1f}%)",
            })
            del state["positions"][symbol]
        
        # Take profit
        elif pnl_pct >= TAKE_PROFIT_PCT:
            orders.append({
                "action": "SELL",
                "symbol": symbol,
                "shares": pos["shares"],
                "price": current_price,
                "reason": f"TAKE_PROFIT ({pnl_pct*100:.1f}%)",
            })
            del state["positions"][symbol]
    
    # Calculate available capital
    invested = sum(
        state["positions"][s]["shares"] * current_prices.get(s, 0)
        for s in state["positions"]
    )
    total_value = state["capital"] + invested
    available = min(state["capital"], total_value * (1 - MAX_PORTFOLIO_RISK))
    available -= invested  # account for existing positions
    
    # Generate new entry orders
    for result in scan_results:
        symbol = result["symbol"]
        if symbol in state["positions"]:
            continue  # Already in position
        if len(state["positions"]) >= 8:
            break  # Max concurrent positions
        
        signal_score = result["score"]
        if abs(signal_score) < 0.3:
            continue  # Not a strong enough signal
        
        shares = calculate_position_size(
            signal_score=signal_score,
            confidence=result["confidence"],
            volatility=result["volatility"],
            current_price=result["price"],
            available_capital=available,
        )
        
        if shares <= 0:
            continue
        
        cost = shares * result["price"]
        if cost > available * 0.5:
            continue
        
        action = "BUY" if signal_score > 0 else "SHORT"
        orders.append({
            "action": action,
            "symbol": symbol,
            "shares": shares,
            "price": result["price"],
            "reason": f"SIGNAL ({signal_score:.2f}, conf={result['confidence']:.2f})",
        })
        
        # Reserve capital for long positions
        if action == "BUY":
            state["positions"][symbol] = {
                "shares": shares,
                "entry_price": result["price"],
                "entry_date": datetime.now().isoformat(),
                "asset_class": result["asset_class"],
            }
            state["capital"] -= cost
            available -= cost
    
    # Apply orders and calculate P&L
    for order in orders:
        if order["action"] == "SELL":
            pos = state["positions"].get(order["symbol"])
            if not pos:
                continue
            pnl = (order["price"] - pos["entry_price"]) * order["shares"]
            state["capital"] += order["price"] * order["shares"]
            state["total_pnl"] += pnl
            
            today = datetime.now().strftime("%Y-%m-%d")
            state["daily_pnl"][today] = state["daily_pnl"].get(today, 0) + pnl
            
            state["trade_history"].append({
                **order,
                "entry_price": pos["entry_price"],
                "pnl": round(pnl, 2),
                "pnl_pct": round((order["price"] - pos["entry_price"]) / pos["entry_price"] * 100, 2),
                "timestamp": datetime.now().isoformat(),
            })
    
    state["scans"] += 1
    return orders


# ─── Reporting ───────────────────────────────────────────────────────────────

def generate_report(state: dict, scan_results: list[dict], orders: list[dict]) -> str:
    """Generate Markdown report."""
    invested = sum(
        state["positions"][s]["shares"] * state["positions"][s]["entry_price"]
        for s in state["positions"]
    )
    unrealized = sum(
        state["positions"][s]["shares"] * next(
            (r["price"] for r in scan_results if r["symbol"] == s), 
            state["positions"][s]["entry_price"]
        ) - state["positions"][s]["shares"] * state["positions"][s]["entry_price"]
        for s in state["positions"]
    )
    total_equity = state["capital"] + invested + unrealized
    
    today = datetime.now().strftime("%Y-%m-%d")
    daily_pnl = state["daily_pnl"].get(today, 0)
    
    report = f"""📊 Father Daddy Capital — Paper Trading Report
{datetime.now().strftime('%Y-%m-%d %H:%M EST')} | Scan #{state['scans']}

💰 Capital: ${state['capital']:,.2f}
📈 Invested: ${invested:,.2f}
📊 Unrealized: ${unrealized:,.2f}
🏦 Total Equity: ${total_equity:,.2f} ({(total_equity/INITIAL_CAPITAL-1)*100:+.1f}%)
📅 Today P&L: ${daily_pnl:+,.2f}

"""
    
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
            cp = next((r["price"] for r in scan_results if r["symbol"] == sym), pos["entry_price"])
            pnl = (cp - pos["entry_price"]) * pos["shares"]
            pnl_pct = (cp / pos["entry_price"] - 1) * 100
            report += f"  {sym}: {pos['shares']} @ ${pos['entry_price']:.2f} → ${cp:.2f} | ${pnl:+,.2f} ({pnl_pct:+.1f}%)\n"
    
    report += f"\n⏳ Next scan: {SCAN_INTERVAL_MINUTES} min"
    return report


# ─── Main Loop ───────────────────────────────────────────────────────────────

def run_once():
    """Single scan cycle. Returns report string."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    state = load_state()
    
    # ── Scalp track ────────────────────────────────────────────────────
    state.setdefault("scalp_positions", {})
    state.setdefault("scalp_exits", [])
    state.setdefault("scalp_scans", 0)
    
    print(f"🔍 Scanning {len(ALL_SYMBOLS)} swing + {len(state.get('scalp_positions', {}))} scalp positions...")
    scan_results = scan_market(ALL_SYMBOLS)
    
    orders = execute_scan(state, scan_results)
    
    # ── Run scalp cycle ────────────────────────────────────────────────
    scalp_entries, scalp_exits, scalp_signals = run_scalp_cycle(state)
    all_orders = orders + [
        {**e, "_type": "scalp", "_action": "entry"} for e in scalp_entries
    ] + [
        {**x, "_type": "scalp", "_action": "exit"} for x in scalp_exits
    ]
    
    save_state(state)
    
    report = generate_report(state, scan_results, orders)
    if scalp_entries or scalp_exits or state.get("scalp_positions"):
        report += scalp_summary(
            state.get("scalp_positions", {}),
            state.get("scalp_scans", 0),
            state.get("scalp_exits", [])[-5:]
        )
    
    # Save report
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = OUTPUT_DIR / f"report_{timestamp}.md"
    with open(report_path, 'w') as f:
        f.write(report)
    
    # Save raw scan data
    scan_path = OUTPUT_DIR / f"scan_{timestamp}.json"
    with open(scan_path, 'w') as f:
        json.dump({"scan_results": scan_results, "orders": orders, "state_summary": {
            "capital": state["capital"],
            "total_pnl": state["total_pnl"],
            "positions": len(state["positions"]),
            "scans": state["scans"],
        }}, f, indent=2, default=str)
    
    print(report)
    return report

def run_continuous():
    """Continuous paper trading loop."""
    print("🚀 Father Daddy Capital — Paper Trading Engine")
    print(f"   Target: $100/day → $500/day")
    print(f"   Scanning every {SCAN_INTERVAL_MINUTES} min")
    print(f"   Assets: {len(ALL_SYMBOLS)} ({len(ASSETS['crypto'])} crypto + {len(ASSETS['equities'])} equities)")
    print("   Ctrl+C to stop\n")
    
    while True:
        try:
            report = run_once()
            
            # Log summary
            log_path = LOG_DIR / "trading.log"
            with open(log_path, 'a') as f:
                f.write(f"{datetime.now().isoformat()} | scan={report.count('scan')}\n")
            
            time.sleep(SCAN_INTERVAL_MINUTES * 60)
        except KeyboardInterrupt:
            print("\n👋 Paper trading stopped.")
            break
        except Exception as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            time.sleep(300)  # 5 min cooldown on error


if __name__ == "__main__":
    if "--once" in sys.argv:
        report = run_once()
        print(f"\n✅ Single scan complete.")
    elif "--continuous" in sys.argv or "-c" in sys.argv:
        run_continuous()
    else:
        print(__doc__)
        print("Usage: python paper_engine.py --once     (single scan)")
        print("       python paper_engine.py --continuous (run forever)")
