#!/usr/bin/env python3
"""
FDC Altcoin Scanner — Volatility Farm + Trending/Hype integration
Scans 50+ altcoins and CoinGecko trending, feeds into paper_engine.py.

Philosophy: volatility IS the product. Same signal stack × higher volatility = 
geometric alpha multiplication. A 0.5% edge on PEPE at 40% daily vol >>> 
a 0.5% edge on BTC at 2% daily vol.
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys

# ─── Configuration ───────────────────────────────────────────────────────────

# Volatility Farm — all yfinance-supported crypto pairs
ALTCOIN_UNIVERSE = [
    # Major alts (always liquid)
    "SOL-USD", "AVAX-USD", "DOGE-USD", "XRP-USD", "ADA-USD", "DOT-USD",
    "LINK-USD", "SHIB-USD", "LTC-USD", "BCH-USD",
    "ATOM-USD", "FIL-USD", "NEAR-USD", "ARB-USD", "OP-USD",
    # L1/L2 (high vol, deep enough liquidity)
    "SEI-USD", "INJ-USD", "TIA-USD", "ICP-USD",
    "RUNE-USD", "ALGO-USD", "HBAR-USD", 
    "EGLD-USD", "QNT-USD", "XTZ-USD", "THETA-USD",
    # DeFi
    "AAVE-USD", "MKR-USD", "LDO-USD", "SNX-USD", "CRV-USD",
    "1INCH-USD", "SUSHI-USD", "DYDX-USD",
    # Meme (extreme vol, high reward)
    "BONK-USD", "FLOKI-USD", "BOME-USD",
    # AI
    "FET-USD", "RENDER-USD",
    # Trending/other
    "JTO-USD", "JUP-USD", "PYTH-USD", "HNT-USD", "KAS-USD",
    "ONDO-USD", "ENA-USD", "WIF-USD",
]

# Deduplicate
ALTCOIN_UNIVERSE = list(dict.fromkeys(ALTCOIN_UNIVERSE))

MAX_ALT_POSITIONS = 5          # Max concurrent alt positions
MAX_ALT_ALLOCATION_PCT = 0.05  # 5% per altcoin position
ALT_STOP_LOSS_PCT = 0.015      # 1.5% stop (tighter — these move fast)
ALT_TAKE_PROFIT_PCT = 0.03     # 3% target
ALT_MIN_CONFIDENCE = 0.45      # Lower bar than swing — volume compensates
ALT_LIFETIME_MINUTES = 90      # Max position age before forced exit

# Trending/Hype
COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"


# ─── Signal Generation (alt-tuned) ───────────────────────────────────────────

def alt_signals(prices: pd.Series) -> dict:
    """Signal stack tuned for high-volatility altcoins.
    Uses RSI(7), tighter MACD, volume expansion bias."""
    if len(prices) < 14:
        return {"score": 0, "confidence": 0, "direction": "neutral", "signals": {}}

    # RSI(7) — faster, catches altcoin reversals earlier
    delta = prices.diff()
    gain = delta.where(delta > 0, 0.0).rolling(7).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(7).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = float(100 - (100 / (1 + rs)).iloc[-1])

    # MACD (6/13/5) — faster than standard 12/26/9
    ema6 = prices.ewm(span=6).mean()
    ema13 = prices.ewm(span=13).mean()
    macd = ema6 - ema13
    signal_line = macd.ewm(span=5).mean()
    macd_signal = 1 if macd.iloc[-1] > signal_line.iloc[-1] else -1

    # Price relative to recent range
    lookback_20 = max(1, len(prices) - 20)
    recent_high = prices.iloc[lookback_20:].max()
    recent_low = prices.iloc[lookback_20:].min()
    current = float(prices.iloc[-1])
    range_position = (current - recent_low) / max(recent_high - recent_low, 1e-9)  # 0-1

    # Volume expansion check (using high-low range as proxy)
    recent_range = max(prices.iloc[-5:]) - min(prices.iloc[-5:])
    prior_range = max(prices.iloc[-10:-5]) - min(prices.iloc[-10:-5]) if len(prices) >= 10 else recent_range
    vol_expanding = recent_range > prior_range * 1.3

    # Momentum: last 3 candles direction
    recent_closes = prices.iloc[-4:]
    cand_up = sum(1 for i in range(1, len(recent_closes)) 
                  if recent_closes.iloc[i] > recent_closes.iloc[i-1])
    momentum_up = cand_up >= 2

    # ── Score aggregation (alt-weighted) ──
    score = 0.0
    direction = "neutral"

    # Oversold bounce signal (highest weight for alts — buy the dip)
    if rsi < 25 and momentum_up:
        score = 0.60 + (25 - rsi) / 50
        direction = "up"
    elif rsi < 30 and momentum_up and vol_expanding:
        score = 0.50
        direction = "up"
    # Overbought fade
    elif rsi > 78 and not momentum_up:
        score = -0.55
        direction = "down"
    elif rsi > 72 and not momentum_up and vol_expanding:
        score = -0.45
        direction = "down"
    # MACD crossover with volume
    elif macd_signal > 0 and momentum_up and vol_expanding:
        score = 0.35
        direction = "up"
    elif macd_signal < 0 and not momentum_up and vol_expanding:
        score = -0.35
        direction = "down"

    confidence = min(0.85, max(0.25, abs(score) * 1.1))

    return {
        "score": round(score, 3),
        "confidence": round(confidence, 3),
        "direction": direction,
        "price": round(current, 6),
        "signals": {
            "rsi": round(rsi, 1),
            "macd": macd_signal,
            "range_position": round(range_position, 3),
            "vol_expanding": vol_expanding,
            "momentum_3_candles": momentum_up,
        }
    }


# ─── Altcoin Market Scan ─────────────────────────────────────────────────────

def scan_altcoin(symbol: str) -> dict | None:
    """Scan a single altcoin. Returns None if data unavailable."""
    try:
        end = datetime.now()
        start = end - timedelta(days=7)  # 7 days of hourly data
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start, end=end, interval="1h")
        if len(hist) < 14:
            return None

        prices = hist['Close']
        signal = alt_signals(prices)
        signal["symbol"] = symbol
        signal["volume_24h"] = float(hist['Volume'].iloc[-24:].sum()) if len(hist) >= 24 else 0
        signal["change_24h"] = round((prices.iloc[-1] / prices.iloc[-min(24, len(prices))] - 1) * 100, 1)
        return signal
    except Exception:
        return None


def scan_all_alts(parallel: bool = True) -> list[dict]:
    """Scan all altcoins. Returns ranked by |score| descending."""
    results = []

    if parallel:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(scan_altcoin, sym): sym for sym in ALTCOIN_UNIVERSE}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
    else:
        for sym in ALTCOIN_UNIVERSE:
            result = scan_altcoin(sym)
            if result:
                results.append(result)

    # Filter: must have direction and confidence
    results = [r for r in results 
               if r["direction"] != "neutral" and r["confidence"] >= ALT_MIN_CONFIDENCE]

    # Sort by absolute score descending
    results.sort(key=lambda x: abs(x["score"]), reverse=True)

    return results[:MAX_ALT_POSITIONS]  # Top N only


# ─── Trending / Hype Scanner ──────────────────────────────────────────────────

def fetch_trending() -> list[dict]:
    """Get CoinGecko trending coins. Returns [{symbol, name, market_cap_rank, score}]."""
    try:
        req = urllib.request.Request(COINGECKO_TRENDING, 
                                      headers={"User-Agent": "hermes-fdc/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        trending = []
        for coin in data.get("coins", []):
            item = coin["item"]
            trending.append({
                "symbol": item["symbol"].upper(),
                "name": item["name"],
                "coin_id": item["id"],
                "market_cap_rank": item.get("market_cap_rank", 0),
                "score": item.get("score", 0),
            })

        return trending[:7]
    except Exception:
        return []


def scan_trending_signals(trending: list[dict]) -> list[dict]:
    """Run signal stack on trending coins. Build yfinance symbol from coin_id."""
    results = []
    for coin in trending:
        symbol = f"{coin['symbol']}-USD"
        if symbol in ALTCOIN_UNIVERSE:
            # Already in alt universe, use that scan result
            result = scan_altcoin(symbol)
        else:
            # Try yfinance — might not have this pair
            result = scan_altcoin(symbol)

        if result:
            result["_source"] = "trending"
            result["_coin_id"] = coin.get("coin_id", "")
            result["_trending_score"] = coin.get("score", 0)
            results.append(result)

    # Filter for actionable signals
    results = [r for r in results 
               if r["direction"] != "neutral" and r["confidence"] >= 0.35]

    results.sort(key=lambda x: abs(x["score"]), reverse=True)
    return results[:3]


# ─── Position Management ──────────────────────────────────────────────────────

def evaluate_alt_entries(scan_results: list[dict], state: dict, 
                          trending_results: list[dict] | None = None) -> list[dict]:
    """
    Evaluate altcoin entries from both volatility farm and trending.
    Applies position sizing, checks existing positions, generates entry orders.
    """
    all_candidates = list(scan_results)
    if trending_results:
        all_candidates.extend(trending_results)

    # Deduplicate by symbol
    seen = set()
    unique = []
    for r in all_candidates:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            unique.append(r)
    unique.sort(key=lambda x: abs(x["score"]), reverse=True)

    alt_positions = state.get("alt_positions", {})
    alt_entries = []

    for candidate in unique:
        sym = candidate["symbol"]
        if sym in alt_positions:
            continue  # Already in a position

        direction = candidate["direction"]
        confidence = candidate["confidence"]
        price = candidate["price"]
        score = candidate["score"]

        # Position size: Kelly-inspired, capped at MAX_ALT_ALLOCATION_PCT
        available = state.get("capital", 100000)
        base_size = available * MAX_ALT_ALLOCATION_PCT
        signal_str = abs(score)
        kelly_frac = signal_str * confidence * 0.8  # Half-Kelly for safety
        position_value = min(base_size, base_size * kelly_frac)

        shares = position_value / max(price, 1e-9)

        entry = {
            "symbol": sym,
            "action": f"ALT_{direction.upper()}",
            "price": price,
            "shares": round(shares, 2),
            "value": round(position_value, 2),
            "score": score,
            "confidence": confidence,
            "rsi": candidate["signals"]["rsi"],
            "stop_loss": round(price * (1 - ALT_STOP_LOSS_PCT) if direction == "up" 
                              else price * (1 + ALT_STOP_LOSS_PCT), 6),
            "take_profit": round(price * (1 + ALT_TAKE_PROFIT_PCT) if direction == "up"
                                else price * (1 - ALT_TAKE_PROFIT_PCT), 6),
            "entry_time": datetime.now().isoformat(),
            "max_lifetime": f"{ALT_LIFETIME_MINUTES}m",
            "_source": candidate.get("_source", "vol_farm"),
            "volume_24h": candidate.get("volume_24h", 0),
            "change_24h": candidate.get("change_24h", 0),
        }

        alt_entries.append(entry)

        if len(alt_entries) >= MAX_ALT_POSITIONS:
            break

    return alt_entries


def check_alt_exits(state: dict, current_prices: dict[str, float]) -> list[dict]:
    """
    Check alt positions for stop-loss, take-profit, or time-based exits.
    current_prices: {symbol: current_price} from latest scan.
    """
    alt_positions = state.get("alt_positions", {})
    exits = []
    now = datetime.now()

    for sym, pos in list(alt_positions.items()):
        current_price = current_prices.get(sym)
        if current_price is None:
            continue

        entry_price = pos["price"]
        entry_time = datetime.fromisoformat(pos["entry_time"])
        direction = "up" if "UP" in pos["action"] else "down"

        # Stop-loss hit
        hit_stop = False
        hit_target = False

        if direction == "up":
            if current_price <= pos["stop_loss"]:
                hit_stop = True
            elif current_price >= pos["take_profit"]:
                hit_target = True
        else:  # down (short proxy)
            if current_price >= pos["stop_loss"]:
                hit_stop = True
            elif current_price <= pos["take_profit"]:
                hit_target = True

        # Time stop
        age_min = (now - entry_time).total_seconds() / 60
        time_stop = age_min >= ALT_LIFETIME_MINUTES

        if hit_stop or hit_target or time_stop:
            pnl_pct = (current_price - entry_price) / entry_price
            if direction == "down":
                pnl_pct = -pnl_pct  # Short proxy — we profit when price drops

            pnl_dollar = pos["value"] * pnl_pct

            reason = "TP" if hit_target else ("SL" if hit_stop else "TIME")
            exits.append({
                "symbol": sym,
                "action": pos["action"],
                "entry_price": entry_price,
                "exit_price": current_price,
                "pnl_pct": round(pnl_pct * 100, 2),
                "pnl_dollar": round(pnl_dollar, 2),
                "exit_reason": reason,
                "hold_minutes": round(age_min, 0),
                "exit_time": now.isoformat(),
            })

            del alt_positions[sym]

            # Update capital + P&L
            state["capital"] = state.get("capital", 100000) + pnl_dollar
            state["alt_pnl"] = state.get("alt_pnl", 0) + pnl_dollar

            # Record to journal
            state.setdefault("trade_journal", [])
            state["trade_journal"].append({
                "type": "altcoin",
                "symbol": sym,
                "pnl_dollar": round(pnl_dollar, 2),
                "pnl_pct": round(pnl_pct * 100, 2),
                "exit_reason": reason,
                "timestamp": now.isoformat(),
            })

    return exits


# ─── Summary ──────────────────────────────────────────────────────────────────

def alt_summary(state: dict, entries: list[dict], exits: list[dict]) -> str:
    """Generate altcoin section for the daily report."""
    lines = ["\n🪙 Altcoin Volatility Farm"]

    positions = state.get("alt_positions", {})
    total_pnl = state.get("alt_pnl", 0)

    if exits:
        lines.append("  Closed:")
        for e in exits:
            emoji = "🟢" if e["pnl_dollar"] > 0 else "🔴"
            lines.append(
                f"    {emoji} {e['symbol']}: {e['pnl_pct']:+.1f}% "
                f"(${e['pnl_dollar']:+,.2f}) | {e['exit_reason']} @ {e['hold_minutes']:.0f}m"
            )

    if entries:
        lines.append(f"  New entries ({len(entries)}):")
        for e in entries:
            src_tag = "🔥" if e.get("_source") == "trending" else "  "
            lines.append(
                f"    {src_tag} {e['symbol']}: {e['action']} @ ${e['price']:.6f} "
                f"| ${e['value']:.2f} | score={e['score']:.3f}"
            )

    if positions:
        lines.append(f"  Open ({len(positions)}):")
        for sym, pos in positions.items():
            lines.append(
                f"    {sym}: {pos['action']} @ ${pos['price']:.6f} "
                f"| ${pos['value']:.2f}"
            )

    if not positions and not entries and not exits:
        lines.append("  No positions. Scanning each cycle.")

    lines.append(f"  Cumulative P&L: ${total_pnl:+,.2f}")
    return "\n".join(lines)


# ─── Main Cycle (called from paper_engine.py) ─────────────────────────────────

def run_alt_cycle(state: dict) -> tuple[list, list, list]:
    """
    Full altcoin cycle: scan volatility farm + trending → entries → exits.
    Returns (new_entries, exits, trending_entries).
    """
    state.setdefault("alt_positions", {})
    state.setdefault("alt_pnl", 0)
    state.setdefault("alt_scans", 0)
    state["alt_scans"] += 1

    print(f"  🪙 Scanning {len(ALTCOIN_UNIVERSE)} altcoins...")

    # ── 1. Scan volatility farm ──
    scan_results = scan_all_alts(parallel=True)
    print(f"     Vol farm signals: {len(scan_results)} actionable")

    # ── 2. Scan trending ──
    trending = fetch_trending()
    trending_signals = scan_trending_signals(trending)
    if trending_signals:
        names = [t["symbol"] for t in trending_signals]
        print(f"     Trending signals: {len(trending_signals)} ({', '.join(names)})")
    else:
        print(f"     Trending: no actionable signals")

    # ── 3. Evaluate entries ──
    entries = evaluate_alt_entries(scan_results, state, trending_signals)

    # ── 4. Apply entries to state ──
    for entry in entries:
        state["alt_positions"][entry["symbol"]] = entry
        # Track alt capital separately — don't reduce main capital
        state["alt_invested"] = state.get("alt_invested", 0) + entry["value"]

    # ── 5. Check exits ──
    # Build current price map from scan results + trending
    price_map = {r["symbol"]: r["price"] for r in scan_results}
    for t in trending_signals:
        price_map[t["symbol"]] = t["price"]
    exits = check_alt_exits(state, price_map)

    return entries, exits, trending_signals


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        state = {"capital": 100000, "alt_positions": {}, "alt_pnl": 0}
        entries, exits, trending = run_alt_cycle(state)
        print(f"\nEntries: {len(entries)}, Exits: {len(exits)}, Trending: {len(trending)}")
        print(alt_summary(state, entries, exits))

    elif "--trending" in sys.argv:
        trending = fetch_trending()
        for t in trending:
            print(f"  {t['symbol']:8} | #{t['market_cap_rank']:4} | {t['name']}")

    elif "--universe" in sys.argv:
        print(f"Altcoin universe: {len(ALTCOIN_UNIVERSE)} pairs")
        for i, sym in enumerate(ALTCOIN_UNIVERSE, 1):
            print(f"  {i:2}. {sym}")

    else:
        print(__doc__)
