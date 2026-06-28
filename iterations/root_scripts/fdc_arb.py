#!/usr/bin/env python3
"""
Father Daddy Capital — Complete-Set Arbitrage Engine
=====================================================
Paper-only arbitrage on Polymarket BTC/ETH daily contracts.

Strategy:
  Polymarket YES/NO contracts trade via AMM (not thick CLOB orderbooks).
  The CLOB book endpoint returns default wide spreads for these contracts.
  
  Instead, we use Gamma API mid-prices as fill-price estimates:
    - If mid(YES) + mid(NO) < $1.00 by ≥1%, simulate buying both legs
    - Settlement at $1.00 per complete pair
    
  This is a paper approximation. Live execution would use CTF minting
  (mint complete sets for $1.00, sell individual tokens at AMM prices).

Zero real USDC. All trades simulated paper until live gates met.
"""

import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── API ────────────────────────────────────────────────────────────────────
GAMMA  = "https://gamma-api.polymarket.com"
OUTPUT = Path("/mnt/c/Users/12035/father_daddy_capital/output")
STATE  = Path("/mnt/c/Users/12035/father_daddy_capital/output/arb_state.json")

# ─── Configuration ──────────────────────────────────────────────────────────
SCAN_SECONDS          = 120       # 2-min scan
INITIAL_BANKROLL      = 200.0
MIN_EDGE              = 0.01      # 1% minimum
MAX_POSITIONS         = 8         # Max concurrent positions
MAX_POSITION_SHARES   = 20        # Max shares per contract
MAX_TOTAL_EXPOSURE    = 0.50      # 50% of bankroll max
MIN_VOLUME_USD        = 5000

# ─── Helpers ────────────────────────────────────────────────────────────────

def _get(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-fdc/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def _parse(val):
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


# ─── Discovery ──────────────────────────────────────────────────────────────

def discover_markets() -> list[dict]:
    """Find active BTC/ETH above/below daily contracts."""
    assets = ["Bitcoin", "Ethereum"]
    today = datetime.now()
    markets = []
    seen = set()

    for asset in assets:
        for offset in range(4):
            target = today + timedelta(days=offset)
            query = f"{asset} above on {target.strftime('%B')} {target.day}"

            try:
                q = urllib.parse.quote(query)
                data = _get(f"{GAMMA}/public-search?q={q}")
                for evt in data.get("events", []):
                    for m in evt.get("markets", []):
                        cid = m.get("conditionId", "")
                        if m.get("closed") or cid in seen:
                            continue
                        seen.add(cid)

                        vol = float(m.get("volume", 0))
                        if vol < MIN_VOLUME_USD:
                            continue

                        prices = _parse(m.get("outcomePrices", []))
                        if not isinstance(prices, list) or len(prices) < 2:
                            continue

                        end_ts = m.get("endDate", "")
                        end_dt = None
                        if end_ts:
                            try:
                                end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                            except ValueError:
                                pass

                        slug = evt.get("slug", "")
                        asset_tag = "btc" if "bitcoin" in slug.lower() else "eth"

                        markets.append({
                            "condition_id": cid,
                            "question": m.get("question", ""),
                            "slug": slug,
                            "series": f"{asset_tag}-day",
                            "yes_price": float(prices[0]),
                            "no_price": float(prices[1]),
                            "volume": vol,
                            "end_time": end_dt,
                        })
            except Exception:
                continue

    return markets


# ─── Edge Detection ─────────────────────────────────────────────────────────

def compute_edge(yes_price: float, no_price: float) -> dict:
    """Check for complete-set arbitrage edge using mid-prices."""
    cost = yes_price + no_price
    edge = 1.0 - cost
    return {
        "cost": round(cost, 4),
        "edge": round(edge, 4),
        "tradeable": edge >= MIN_EDGE,
    }


# ─── Settlement ─────────────────────────────────────────────────────────────

def is_ended(mkt: dict, now: datetime) -> bool:
    end = mkt.get("end_time")
    if end is None:
        return False
    if isinstance(end, str):
        try:
            end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            return False
    return now > end


def settle_positions(state: dict) -> list[dict]:
    """Settle expired contracts."""
    now = datetime.now(timezone.utc)
    positions = state.get("positions", {})
    settled = []

    for cid, pos in list(positions.items()):
        mkt = pos.get("market", {})
        if not is_ended(mkt, now):
            continue

        yes_shares = pos.get("yes_shares", 0)
        no_shares = pos.get("no_shares", 0)
        sets = min(yes_shares, no_shares)

        avg_yes = pos.get("avg_cost_yes", 0.5)
        avg_no = pos.get("avg_cost_no", 0.5)
        total_cost = (yes_shares * avg_yes) + (no_shares * avg_no)
        complete_value = sets * 1.0  # $1.00 per complete set
        excess_value = 0.0  # paper: don't value excess directionally
        total_pnl = complete_value + excess_value - total_cost

        settled.append({
            "condition_id": cid,
            "question": mkt.get("question", ""),
            "series": mkt.get("series", "unknown"),
            "sets": sets,
            "yes_shares": yes_shares,
            "no_shares": no_shares,
            "total_cost": round(total_cost, 2),
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl / max(total_cost, 1) * 100, 1),
            "settled_at": now.isoformat(),
        })

        del positions[cid]

    return settled


# ─── Tick ───────────────────────────────────────────────────────────────────

def run_tick(state: dict, markets: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    bankroll = state["bankroll"]
    positions = state.setdefault("positions", {})

    # Purge ended
    for cid in list(positions):
        if is_ended(positions[cid].get("market", {}), now):
            del positions[cid]

    # Calculate exposure
    invested = 0.0
    for pos in positions.values():
        invested += (pos.get("yes_shares", 0) * pos.get("avg_cost_yes", 0.5))
        invested += (pos.get("no_shares", 0) * pos.get("avg_cost_no", 0.5))

    tick = {
        "scanned": 0, "signals": 0, "entered": 0,
        "already_in": 0, "max_positions": 0, "errors": 0,
    }

    # Sort by edge descending
    candidates = []
    for m in markets:
        if is_ended(m, now):
            continue
        edge_info = compute_edge(m["yes_price"], m["no_price"])
        if edge_info["tradeable"]:
            candidates.append((m, edge_info))
        tick["scanned"] += 1

    candidates.sort(key=lambda x: x[1]["edge"], reverse=True)

    for market, edge_info in candidates:
        cid = market["condition_id"]

        if cid in positions:
            tick["already_in"] += 1
            continue
        if len(positions) >= MAX_POSITIONS:
            tick["max_positions"] += 1
            break

        yes_price = market["yes_price"]
        no_price = market["no_price"]
        edge = edge_info["edge"]

        # Position sizing: Kelly-inspired, shares proportional to edge
        kelly_frac = min(0.25, edge * 2.0)  # capped at 25% of bankroll
        position_value = bankroll * kelly_frac
        cost_per_pair = yes_price + no_price

        if cost_per_pair <= 0:
            continue

        shares = min(MAX_POSITION_SHARES, math.floor(position_value / cost_per_pair))
        if shares < 1:
            continue

        # Check exposure cap
        new_invested = shares * cost_per_pair
        if invested + new_invested > bankroll * MAX_TOTAL_EXPOSURE:
            continue

        positions[cid] = {
            "market": market,
            "yes_shares": shares,
            "no_shares": shares,
            "avg_cost_yes": yes_price,
            "avg_cost_no": no_price,
            "edge_at_entry": edge,
            "cost_per_pair": cost_per_pair,
            "invested": round(new_invested, 2),
            "entered_at": now.isoformat(),
        }

        invested += new_invested
        tick["entered"] += 1
        tick["signals"] += 1

    state["_invested"] = round(invested, 2)
    return tick


# ─── Summary ────────────────────────────────────────────────────────────────

def summary(state: dict, tick: dict) -> str:
    bankroll = state["bankroll"]
    pnl = state.get("total_pnl", 0)
    positions = state.get("positions", {})
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    scans = state.get("scans", 0)
    invested = state.get("_invested", 0)

    lines = [
        "",
        "📊 COMPLETE-SET ARB (paper)",
        f"   Bankroll: ${bankroll:,.2f} | P&L: ${pnl:+,.2f} | "
        f"W/L: {wins}/{losses} | Scans: {scans}",
        f"   Invested: ${invested:,.2f} | Positions: {len(positions)}",
        "",
        f"   Tick: {tick['scanned']} scanned | {tick['signals']} signals | "
        f"{tick['entered']} entered",
    ]

    if positions:
        lines.append("")
        for cid, pos in list(positions.items())[:5]:
            mkt = pos.get("market", {})
            s = mkt.get("series", "?")
            yes = pos["yes_shares"]
            no = pos["no_shares"]
            edge = pos.get("edge_at_entry", 0)
            lines.append(
                f"   [{s}] {mkt.get('question','')[:70]} | "
                f"YES={pos['avg_cost_yes']:.3f}×{yes} NO={pos['avg_cost_no']:.3f}×{no} | "
                f"edge={edge*100:.1f}%"
            )

    return "\n".join(lines)


# ─── State ──────────────────────────────────────────────────────────────────

def load_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {
        "bankroll": INITIAL_BANKROLL,
        "total_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "positions": {},
        "journal": [],
        "scans": 0,
        "started": datetime.now(timezone.utc).isoformat(),
    }


def save_state(state):
    state["scans"] += 1
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    STATE.write_text(json.dumps(state, indent=2, default=str))


# ─── Main ───────────────────────────────────────────────────────────────────

def run_once(state=None):
    if state is None:
        state = load_state()

    settled = settle_positions(state)
    for s in settled:
        state["total_pnl"] += s["pnl"]
        state["bankroll"] += s["pnl"]
        if s["pnl"] > 0:
            state["wins"] += 1
        elif s["pnl"] < 0:
            state["losses"] += 1
        state.setdefault("journal", []).append({
            "type": "arb_settle",
            "ts": datetime.now(timezone.utc).isoformat(),
            **s,
        })

    markets = discover_markets()
    tick = run_tick(state, markets)
    save_state(state)

    print(summary(state, tick))
    return tick


def run_continuous():
    state = load_state()
    print(f"📊 FDC ARB — {SCAN_SECONDS}s scan | ${state['bankroll']:,.2f} bankroll")
    print("   Ctrl+C to stop\n")

    while True:
        try:
            run_once(state)
            time.sleep(SCAN_SECONDS)
        except KeyboardInterrupt:
            print(f"\n👋 Stopped. ${state['bankroll']:,.2f} | "
                  f"P&L: ${state.get('total_pnl', 0):+,.2f}")
            break
        except Exception as e:
            print(f"❌ {e}", file=sys.stderr)
            time.sleep(30)


# ─── Integration (for paper_engine.py) ──────────────────────────────────────

def run_arb_cycle(state: dict) -> dict:
    """Integrated cycle, uses state['arb_state']."""
    state.setdefault("arb_state", {
        "bankroll": INITIAL_BANKROLL,
        "total_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "positions": {},
        "journal": [],
        "scans": 0,
        "started": datetime.now(timezone.utc).isoformat(),
    })
    arb = state["arb_state"]

    settled = settle_positions(arb)
    for s in settled:
        arb["total_pnl"] += s["pnl"]
        arb["bankroll"] += s["pnl"]
        if s["pnl"] > 0:
            arb["wins"] += 1
        elif s["pnl"] < 0:
            arb["losses"] += 1
        arb.setdefault("journal", []).append({
            "type": "arb_settle",
            "ts": datetime.now(timezone.utc).isoformat(),
            **s,
        })

    markets = discover_markets()
    tick = run_tick(arb, markets)
    arb["scans"] += 1
    arb["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Persist to disk for calibration scorer
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(arb, indent=2, default=str))

    return tick


def arb_summary(state: dict, tick: dict) -> str:
    arb = state.get("arb_state", {})
    bankroll = arb.get("bankroll", INITIAL_BANKROLL)
    pnl = arb.get("total_pnl", 0)
    positions = arb.get("positions", {})
    wins = arb.get("wins", 0)
    losses = arb.get("losses", 0)
    scans = arb.get("scans", 0)

    lines = [
        "",
        "📊 Complete-Set Arb",
        f"   Bankroll: ${bankroll:,.2f} | P&L: ${pnl:+,.2f} | "
        f"W/L: {wins}/{losses} | Scans: {scans}",
    ]

    if tick:
        lines.append(
            f"   Scanned: {tick.get('scanned', 0)} | "
            f"Signals: {tick.get('signals', 0)} | "
            f"Entered: {tick.get('entered', 0)}"
        )

    if positions:
        for cid, pos in list(positions.items())[:3]:
            mkt = pos.get("market", {})
            s = mkt.get("series", "?")
            yes = pos["yes_shares"]
            no = pos["no_shares"]
            edge = pos.get("edge_at_entry", 0)
            lines.append(
                f"   [{s}] {mkt.get('question','')[:60]} | "
                f"{yes}×{pos['avg_cost_yes']:.3f} + {no}×{pos['avg_cost_no']:.3f} | "
                f"edge={edge*100:.1f}%"
            )

    if not positions and (not tick or tick.get("signals", 0) == 0):
        lines.append("   Idle — no edge found.")

    return "\n".join(lines)


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--once" in sys.argv:
        state = load_state()
        tick = run_once(state)
        print(f"\n{json.dumps(tick, indent=2)}")
    elif "--discover" in sys.argv:
        markets = discover_markets()
        print(f"{len(markets)} active contracts:\n")
        for m in sorted(markets, key=lambda x: x["volume"], reverse=True)[:20]:
            edge = compute_edge(m["yes_price"], m["no_price"])
            emoji = "🟢" if edge["tradeable"] else "🔴"
            print(f"  {emoji} {m['question'][:75]} | edge={edge['edge']*100:+.1f}%")
    elif "--reset" in sys.argv:
        STATE.unlink(missing_ok=True)
        print("Arb state reset.")
    elif "--continuous" in sys.argv or "-c" in sys.argv:
        run_continuous()
    else:
        print(__doc__)
