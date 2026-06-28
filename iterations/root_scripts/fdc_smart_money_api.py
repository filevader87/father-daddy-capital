#!/usr/bin/env python3
"""
FDC Smart Money API Connector
Bridges fdc_smart_money_filter.py with live Polymarket Data API.
Polls leaderboard, fetches wallet profiles, generates scored signals.

Usage:
    from fdc_smart_money_api import poll_smart_money_signals
    signals = poll_smart_money_signals(min_confidence=0.50)

Author: Hugh (3rd of 5)
Date: 2026-05-15
"""

from __future__ import annotations
import json
import urllib.request
import time
from typing import Optional, List, Dict
from datetime import datetime

from fdc_smart_money_filter import (
    WalletProfile, SmartMoneySignal, QualityGates,
    evaluate_wallet_quality, score_wallet, compute_signal_confidence,
    should_copy_trade, categorize_market, parse_leaderboard_entry,
    GAMMA_API, DATA_API,
)

# ─── API Helpers ───────────────────────────────────────────────────────────


def _get(url: str) -> dict:
    """Fetch JSON from Polymarket APIs."""
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-fdc/4.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _cache_response(cache_file: str, data: dict, ttl: int = 300):
    """Write cached API response."""
    cache_path = f"/tmp/fdc_cache_{cache_file}.json"
    with open(cache_path, "w") as f:
        json.dump({"ts": time.time(), "data": data}, f)


def _read_cache(cache_file: str, ttl: int = 300) -> Optional[dict]:
    """Read cached API response if fresh."""
    cache_path = f"/tmp/fdc_cache_{cache_file}.json"
    try:
        with open(cache_path) as f:
            entry = json.load(f)
        if time.time() - entry["ts"] < ttl:
            return entry["data"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None


# ─── Leaderboard Polling ───────────────────────────────────────────────────


def fetch_leaderboard(
    period: str = "week",
    limit: int = 50,
    use_cache: bool = True,
) -> List[dict]:
    """
    Fetch Polymarket leaderboard data.
    
    Note: Polymarket does not expose a public leaderboard REST endpoint.
    Uses two fallback strategies:
    1. Gamma activity endpoint — scrape top-volume wallets
    2. Goldsky subgraph — query on-chain PnL data
    
    Returns:
        Raw wallet entries sorted by volume/PnL
    """
    cache_key = f"leaderboard_{period}_{limit}"
    if use_cache:
        cached = _read_cache(cache_key, ttl=600)  # 10-min cache
        if cached:
            return cached

    # Strategy: fetch high-volume markets, then get top traders from activity
    entries = []
    try:
        # Get top markets by volume
        events_url = f"{GAMMA_API}/events?active=true&closed=false&order=volume_24hr&ascending=false&limit=10"
        markets_data = _get(events_url)
        
        seen_wallets = set()
        for event in (markets_data if isinstance(markets_data, list) else []):
            for market in event.get("markets", [])[:3]:
                cid = market.get("conditionId", "")
                if not cid:
                    continue
                # Fetch activity for this market's top traders
                try:
                    activity = _get(f"{GAMMA_API}/activity?conditionId={cid}&limit=20")
                    for trade in (activity if isinstance(activity, list) else []):
                        addr = trade.get("user", trade.get("address", ""))
                        if addr in seen_wallets:
                            continue
                        seen_wallets.add(addr)
                        entries.append({
                            "address": addr,
                            "volume": float(trade.get("size", 0)) * float(trade.get("price", 0)),
                            "tradeCount": 1,
                            "pnl": float(trade.get("pnl", 0)),
                        })
                except Exception:
                    continue
                if len(entries) >= limit:
                    break
            if len(entries) >= limit:
                break
    except Exception as e:
        print(f"Leaderboard discovery failed: {e}")

    _cache_response(cache_key, entries)
    return entries[:limit]


def fetch_wallet_activity(address: str, limit: int = 50) -> List[dict]:
    """
    Fetch recent trades for a wallet from Gamma API.

    Returns:
        List of trade dicts: [{title, side, price, size, timestamp, conditionId, ...}]
    """
    cache_key = f"activity_{address[:12]}"
    cached = _read_cache(cache_key, ttl=120)
    if cached:
        return cached

    url = f"{GAMMA_API}/activity?user={address}&limit={limit}"
    try:
        data = _get(url)
        trades = data if isinstance(data, list) else data.get("trades", data.get("data", []))
        _cache_response(cache_key, trades)
        return trades
    except Exception as e:
        print(f"Activity fetch failed for {address[:10]}: {e}")
        return []


# ─── Wallet Profiling ──────────────────────────────────────────────────────


def profile_wallets(
    min_pnl: float = 1000,
    min_trade_count: int = 10,
    top_n: int = 20,
) -> List[WalletProfile]:
    """
    Fetch leaderboard, filter by quality, return scored WalletProfiles.

    Args:
        min_pnl: minimum PnL to consider
        min_trade_count: minimum number of trades
        top_n: how many top wallets to profile

    Returns:
        Sorted list of WalletProfiles (best first)
    """
    entries = fetch_leaderboard(period="week", limit=max(100, top_n * 3))
    profiles = []

    for i, entry in enumerate(entries):
        if len(profiles) >= top_n:
            break

        pnl = float(entry.get("pnl", 0))
        volume = float(entry.get("volume", 0))
        trade_count = int(entry.get("tradeCount", entry.get("trade_count", 0)))

        # Coarse pre-filter
        if pnl < min_pnl or trade_count < min_trade_count:
            continue

        profile = WalletProfile(
            address=entry.get("address", ""),
            name=entry.get("userName", entry.get("user_name", ""))
                 or entry.get("address", "")[:8],
            pnl=pnl,
            volume=volume,
            trade_count=trade_count,
            win_rate=float(entry.get("winRate", entry.get("win_rate", 0))),
            profit_factor=float(entry.get("profitFactor", entry.get("profit_factor", 0))),
            avg_win=float(entry.get("avgWin", entry.get("avg_win", 0))),
            avg_loss=float(entry.get("avgLoss", entry.get("avg_loss", 0))),
            max_win=float(entry.get("maxWin", entry.get("max_win", 0))),
            max_loss=float(entry.get("maxLoss", entry.get("max_loss", 0))),
            winning_markets=int(entry.get("winningMarkets", entry.get("winning_markets", 0))),
            losing_markets=int(entry.get("losingMarkets", entry.get("losing_markets", 0))),
            rank=i + 1,
        )

        # Apply quality gates
        gates = evaluate_wallet_quality(profile)
        profile.score = score_wallet(profile, gates)
        profiles.append(profile)

    return sorted(profiles, key=lambda p: p.score, reverse=True)


# ─── Signal Generation ─────────────────────────────────────────────────────


def poll_smart_money_signals(
    min_confidence: float = 0.50,
    min_trade_size: float = 5.0,
    max_signals: int = 5,
    categories: Optional[List[str]] = None,
) -> List[SmartMoneySignal]:
    """
    Main entry: poll leaderboard, filter wallets, fetch their recent trades,
    generate scored copy-trade signals.

    Args:
        min_confidence: minimum signal confidence (0-1)
        min_trade_size: minimum USDC value
        max_signals: max signals to return
        categories: optional category filter (e.g. ['crypto'])

    Returns:
        Sorted list of SmartMoneySignals (best confidence first)
    """
    profiles = profile_wallets(min_pnl=1000, top_n=15)
    if not profiles:
        return []

    signals = []

    for profile in profiles:
        if len(signals) >= max_signals * 2:  # fetch more, then filter
            break

        gates = evaluate_wallet_quality(profile)
        if gates.gate_count < 3:
            continue  # skip weak wallets

        # Fetch their recent trades
        trades = fetch_wallet_activity(profile.address, limit=10)
        if not trades:
            continue

        for trade in trades:
            title = trade.get("title", trade.get("market", ""))
            side = trade.get("side", "BUY")
            price = float(trade.get("price", 0))
            size = float(trade.get("size", 0))
            usdc_value = price * size

            if usdc_value < min_trade_size:
                continue

            sig = SmartMoneySignal(
                wallet=profile,
                condition_id=trade.get("conditionId", trade.get("condition_id", "")),
                market_title=title,
                token_id=trade.get("tokenId", trade.get("asset_id", "")),
                side=side,
                outcome=trade.get("outcome", ""),
                price=price,
                size=size,
                category=categorize_market(title),
                timestamp=datetime.now(),
                confidence=compute_signal_confidence(profile, gates),
            )

            copy, _ = should_copy_trade(
                sig, min_confidence=min_confidence, min_trade_size=min_trade_size,
                allowed_categories=categories,
            )
            if copy:
                signals.append(sig)

    # Sort by confidence, take top N
    return sorted(signals, key=lambda s: s.confidence, reverse=True)[:max_signals]


# ─── FDC Track 5 Runner ────────────────────────────────────────────────────


def run_smart_money_cycle(state: dict) -> tuple:
    """
    FDC Track 5 cycle: poll smart money, generate paper entries.
    Called from paper_engine.py each scan.

    Args:
        state: main FDC state dict with "capital", "positions", etc.

    Returns:
        (smart_money_entries: list, smart_money_signals: list)
    """
    state.setdefault("smart_money_position_count", 0)
    state.setdefault("smart_money_pnl", 0.0)
    state.setdefault("smart_money_positions", {})
    state.setdefault("smart_money_journal", [])

    try:
        signals = poll_smart_money_signals(
            min_confidence=0.55,
            max_signals=3,
            categories=["crypto"],  # FDC focus
        )
    except Exception as e:
        print(f"Smart money poll failed: {e}")
        return [], []

    entries = []
    positions = state.get("smart_money_positions", {})
    max_positions = 5
    bankroll = state.get("capital", 100000)

    for sig in signals:
        if len(positions) + len(entries) >= max_positions:
            break

        # Size: scale the smart money's bet to our bankroll
        scale = bankroll / 100000.0  # relative to $100K bankroll
        bet = min(250.0, sig.confidence * scale * 500)

        key = f"{sig.condition_id[:16]}_{sig.side}"
        if key in positions:
            continue

        entries.append({
            "action": f"SMART_{sig.side}",
            "market_title": sig.market_title,
            "condition_id": sig.condition_id,
            "token_id": sig.token_id,
            "outcome": sig.outcome,
            "price": sig.price,
            "size": sig.size,
            "bet": round(bet, 2),
            "confidence": sig.confidence,
            "category": sig.category,
            "wallet_name": sig.wallet.name,
            "wallet_score": sig.wallet.score,
            "wallet_rank": sig.wallet.rank,
            "timestamp": sig.timestamp.isoformat(),
        })

        positions[key] = entries[-1]

    # Update state
    state["smart_money_positions"] = positions
    state["smart_money_position_count"] = len(positions)

    return entries, signals


# ─── Reporting ─────────────────────────────────────────────────────────────


def smart_money_summary(state: dict, entries: list) -> str:
    """Generate Smart Money section for the report."""
    lines = ["\n🕵️ Track 5: Smart Money Signals"]

    positions = state.get("smart_money_positions", {})
    pnl = state.get("smart_money_pnl", 0.0)

    if entries:
        lines.append(f"  New signals ({len(entries)}):")
        for e in entries:
            lines.append(
                f"    ⚡ {e['action']}: ${e['bet']} @ {e['price']:.3f} | "
                f"conf={e['confidence']:.2f} | wallet={e['wallet_name']} (score={e['wallet_score']:.0f})"
            )

    if positions:
        lines.append(f"  Open ({len(positions)}):")
        for key, pos in list(positions.items())[-5:]:
            lines.append(
                f"    📌 {pos.get('wallet_name','?')} | "
                f"${pos.get('bet',0)} | conf={pos.get('confidence',0):.2f}"
            )

    if not entries and not positions:
        lines.append("  No qualifying signals. Min gate: 3/5 quality.")

    lines.append(f"  Total P&L: ${pnl:+,.2f}")
    return "\n".join(lines)


# ─── Quick Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Smart Money Signal Test ===\n")
    signals = poll_smart_money_signals(max_signals=3)
    if signals:
        for s in signals:
            print(f"  {s.wallet.name} (score={s.wallet.score:.0f})")
            print(f"    Trade: {s.side} {s.size} @ {s.price:.3f}")
            print(f"    Market: {s.market_title[:60]}")
            print(f"    Category: {s.category}")
            print(f"    Confidence: {s.confidence:.3f}")
            print()
    else:
        print("  No signals found (API may be unreachable or no qualifying wallets)")
