#!/usr/bin/env python3
"""
FDC V19.7 Runner — Paper & Live Trading Daemon
Runs on 5m intervals. Handles graceful startup/shutdown, order placement,
settlement, and state persistence.

Modes:
  paper  — 48h validation, no real orders
  live   — real CLOB orders, requires wallet funding
  
Usage:
  python3 fdc_runner.py paper     # Paper trading (default)
  python3 fdc_runner.py live      # Live trading (requires wallet)
  python3 fdc_runner.py once      # Single scan, paper mode
  python3 fdc_runner.py status     # Show current state
  
Author: Hugh (3rd of 5) + Riker
Date: 2026-05-29
"""

import json, os, signal, sys, time, threading, traceback
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).parent
STATE = REPO / "output" / "pm_state.json"
LOCK_FILE = REPO / "output" / ".fdc_lock"
LOG_FILE = REPO / "output" / "fdc_runner.log"

# ══════════════════════════════════════════════════════════════════════════════
# MODE CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
PAPER_MODE = True  # Default paper, overridden by CLI arg
BANKROLL_START = 320.0  # $320 funded bankroll

def log(msg, level="INFO"):
    ts = datetime.now().isoformat()
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass


def load_state():
    """Load state with graceful defaults for missing fields."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
            # Ensure all required fields exist
            defaults = {
                "bankroll": BANKROLL_START,
                "total_pnl": 0,
                "wins": 0,
                "losses": 0,
                "positions": {},
                "journal": [],
                "scans": 0,
                "daily_pnl": 0,
                "daily_date": datetime.now().strftime("%Y-%m-%d"),
                "exit_stats": {"stop_loss": 0, "time_decay": 0, "expiry": 0},
                "bankroll_peak": BANKROLL_START,
                "mode": "paper",
                "start_time": datetime.now().isoformat(),
                "version": "V19.7",
            }
            for k, v in defaults.items():
                if k not in state:
                    state[k] = v
            return state
        except json.JSONDecodeError:
            log(f"Corrupted state file, backing up and creating fresh", "WARN")
            backup = STATE.with_suffix(".json.bak")
            STATE.rename(backup)
    return {
        "bankroll": BANKROLL_START,
        "total_pnl": 0,
        "wins": 0,
        "losses": 0,
        "positions": {},
        "journal": [],
        "scans": 0,
        "daily_pnl": 0,
        "daily_date": datetime.now().strftime("%Y-%m-%d"),
        "exit_stats": {"stop_loss": 0, "time_decay": 0, "expiry": 0},
        "bankroll_peak": BANKROLL_START,
        "mode": "paper",
        "start_time": datetime.now().isoformat(),
        "version": "V19.7",
    }


def save_state(state):
    """Atomically save state to prevent corruption."""
    state["scans"] = state.get("scans", 0) + 1
    state["last_scan"] = datetime.now().isoformat()
    # Reset daily P&L at midnight
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_pnl"] = 0
    # Atomic write: write to temp, then rename
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.rename(STATE)


# ══════════════════════════════════════════════════════════════════════════════
# PROCESS LOCK — prevent multiple instances
# ══════════════════════════════════════════════════════════════════════════════
_running = True

def acquire_lock():
    """Acquire process lock. Returns True if we got it."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        pid = LOCK_FILE.read_text().strip()
        try:
            os.kill(int(pid), 0)  # Check if process exists
            log(f"Another instance running (PID {pid})", "ERROR")
            return False
        except (ProcessLookupError, ValueError):
            log(f"Stale lock (PID {pid}), removing", "WARN")
            LOCK_FILE.unlink()
    LOCK_FILE.write_text(str(os.getpid()))
    return True

def release_lock():
    """Release process lock."""
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass

def handle_signal(signum, frame):
    """Graceful shutdown on SIGTERM/SIGINT."""
    global _running
    _running = False
    log(f"Received signal {signum}, shutting down gracefully...", "WARN")

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# ══════════════════════════════════════════════════════════════════════════════
# ORDER EXECUTION — Wire PMLiveClient into engine
# ══════════════════════════════════════════════════════════════════════════════
def place_entry_order(entry, state, paper=True):
    """Place an entry order via PMLiveClient.
    
    In paper mode: simulate fill at mid-price.
    In live mode: submit to CLOB.
    
    Returns: dict with order_id, status, fill_price, fill_size
    """
    if paper:
        # Paper mode: simulate fill at contract price
        return {
            "order_id": f"paper_{int(time.time()*1000)}",
            "status": "FILLED",
            "fill_price": entry["contract_price"],
            "fill_size": entry["bet"],
            "mode": "PAPER",
            "timestamp": datetime.now().isoformat(),
        }
    
    # Live mode: use PMLiveClient
    from fdc_pm_live import PMLiveClient
    client = PMLiveClient()
    init_status = client.init()
    if not init_status.get("ready"):
        log(f"Live client not ready: {init_status}", "ERROR")
        return {"status": "REJECTED", "error": str(init_status)}
    
    # Get token_id for the contract
    # Polymarket uses condition_id -> token_id mapping
    # For now, we need to discover the token_id
    token_id = entry.get("token_id")
    if not token_id:
        # Try to discover from the CLOB
        import urllib.request
        try:
            cid = entry.get("conditionId", "")
            url = f"https://clob.polymarket.com/markets?condition_id={cid}"
            req = urllib.request.Request(url, headers={"User-Agent": "fdc/19.7"})
            with urllib.request.urlopen(req, timeout=10) as r:
                markets = json.loads(r.read())
            if isinstance(markets, list) and len(markets) > 0:
                side_idx = 0 if entry.get("side") == "Up" else 1
                token_id = markets[0].get("tokens", [{}])[side_idx].get("token_id")
        except Exception as e:
            log(f"Token ID discovery failed: {e}", "WARN")
    
    if not token_id:
        return {"status": "REJECTED", "error": "No token_id found"}
    
    # Place the order
    result = client.place_order(
        token_id=token_id,
        side="BUY",
        price=entry["contract_price"],
        size=entry["bet"],
    )
    return result


def place_exit_order(pos, exit_info, state, paper=True):
    """Place an exit/sell order for a position.
    
    In paper mode: simulate fill.
    In live mode: submit sell to CLOB.
    """
    if paper:
        return {
            "order_id": f"paper_exit_{int(time.time()*1000)}",
            "status": "FILLED",
            "exit_value": exit_info.get("exit_value", 0),
            "pnl": exit_info.get("pnl", 0),
            "mode": "PAPER",
        }
    
    # Live mode
    from fdc_pm_live import PMLiveClient
    client = PMLiveClient()
    init_status = client.init()
    if not init_status.get("ready"):
        return {"status": "REJECTED", "error": str(init_status)}
    
    token_id = pos.get("token_id", pos.get("conditionId", ""))
    result = client.place_order(
        token_id=token_id,
        side="SELL",
        price=exit_info.get("cur_price", pos.get("contract_price", 0.5)),
        size=pos.get("bet", 1),
    )
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RUN LOOP
# ══════════════════════════════════════════════════════════════════════════════
def run_scan(state, paper=True):
    """Execute one scan cycle. Returns (entries, exits, skip_info, signal)."""
    from pm_engine_v19_7 import (
        fetch_5m, btc_signal, discover_contracts, evaluate_entries,
        process_exits, check_settlements, save_state as engine_save,
        _check_kill_switch, _rolling_drawdown, _init_live,
        PAPER_BANKROLL, INITIAL_BANKROLL
    )
    
    # Fetch signal
    prices = fetch_5m()
    if not prices:
        return [], [], [], None
    
    sig = btc_signal(prices)
    contracts = discover_contracts()
    
    # Process exits first
    exit_settled = process_exits(state, contracts)
    for s in exit_settled:
        pnl = s.get("pnl", 0)
        state["total_pnl"] += pnl
        state["bankroll"] += pnl
        state["daily_pnl"] = state.get("daily_pnl", 0) + pnl
        if pnl > 0:
            state["wins"] = state.get("wins", 0) + 1
            state["journal"].append({
                "ts": datetime.now().isoformat(),
                "type": f"exit_{s.get('exit_type', 'stop_loss')}",
                "pnl": pnl,
                "question": s.get("question", ""),
                "mode": "PAPER" if paper else "LIVE",
            })
        else:
            state["losses"] = state.get("losses", 0) + 1
            state["journal"].append({
                "ts": datetime.now().isoformat(),
                "type": f"exit_{s.get('exit_type', 'stop_loss')}",
                "pnl": pnl,
                "question": s.get("question", ""),
                "mode": "PAPER" if paper else "LIVE",
            })
    
    # Check settlements
    settled = check_settlements(state, sig.get("price", 0))
    for s in settled:
        pnl = s.get("pnl", 0)
        state["total_pnl"] += pnl
        state["bankroll"] += pnl
        state["daily_pnl"] = state.get("daily_pnl", 0) + pnl
        if pnl > 0:
            state["wins"] = state.get("wins", 0) + 1
        else:
            state["losses"] = state.get("losses", 0) + 1
        state["journal"].append({
            "ts": datetime.now().isoformat(),
            "type": "settle",
            "pnl": pnl,
            "question": s.get("question", ""),
            "mode": "PAPER" if paper else "LIVE",
        })
    
    # Evaluate new entries
    entries, skip_info = evaluate_entries(sig, contracts, state)
    
    # Place orders for entries
    for entry in entries:
        key = f"{entry['conditionId'][:16]}_{entry['side']}"
        
        # Place the order (paper or live)
        order_result = place_entry_order(entry, state, paper=paper)
        entry["order_result"] = order_result
        entry["mode"] = "PAPER" if paper else "LIVE"
        
        if order_result.get("status") in ("FILLED", "SIMULATED"):
            # Track position in state
            state["positions"][key] = entry
            # Deduct bet from bankroll
            state["bankroll"] -= entry["bet"]
            log(f"ENTRY {entry['side']} {entry.get('question','')[:40]} "
                f"@ {entry['contract_price']:.3f} ${entry['bet']:.2f} "
                f"[{order_result['status']}]", "TRADE")
        else:
            log(f"ORDER FAILED: {order_result}", "WARN")
            state["journal"].append({
                "ts": datetime.now().isoformat(),
                "type": "order_failed",
                "error": str(order_result),
                "mode": "PAPER" if paper else "LIVE",
            })
    
    # Track bankroll peak
    br_peak = state.get("bankroll_peak", state["bankroll"])
    if state["bankroll"] > br_peak:
        state["bankroll_peak"] = state["bankroll"]
    
    # Save state
    save_state(state)
    
    return entries, exit_settled + settled, skip_info, sig


def print_status(state):
    """Print current engine status."""
    bankroll = state.get("bankroll", 0)
    pnl = state.get("total_pnl", 0)
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    wr = (wins / max(wins + losses, 1)) * 100
    positions = state.get("positions", {})
    open_pos = [p for p in positions.values() if p.get("status") == "open"]
    dd_pct, dd_mult, entries_allowed = _rolling_dd_from_state(state)
    mode = state.get("mode", "paper")
    scans = state.get("scans", 0)
    
    print(f"""
╔══════════════════════════════════════════════════════╗
║            FDC V19.7 — STATUS REPORT                  ║
╠══════════════════════════════════════════════════════╣
║  Mode:       {mode.upper():>8s}                                 ║
║  Bankroll:   ${bankroll:>8.2f}                                 ║
║  P&L:        ${pnl:>+8.2f}                                 ║
║  W/L:        {wins}/{losses} ({wr:.1f}%)                            ║
║  Open pos:   {len(open_pos):>2d}                                       ║
║  DD:         {dd_pct*100:.1f}% (mult: {dd_mult:.2f})                    ║
║  Entries:    {'ALLOWED' if entries_allowed else 'BLOCKED'}                     ║
║  Scans:      {scans:>6d}                                     ║
║  Started:    {state.get('start_time', 'N/A')[:19]}              ║
╚══════════════════════════════════════════════════════╝
""")


def _rolling_dd_from_state(state):
    """Calculate rolling DD from state journal."""
    journal = state.get("journal", [])
    settled = [j for j in journal if j.get("type") in ("settle", "exit_stop_loss", "exit_time_decay", "exit_expiry")]
    recent = settled[-50:] if len(settled) >= 50 else settled
    
    if not recent:
        return 0.0, 1.0, True
    
    from pm_engine_v19_7 import DD_LEVEL_1, DD_LEVEL_2, DD_LEVEL_3
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for j in recent:
        pnl = j.get("pnl", 0)
        cum += pnl
        if cum > peak:
            peak = cum
        if peak > 0:
            max_dd = max(max_dd, (peak - cum) / peak)
    
    if max_dd >= DD_LEVEL_3:
        return max_dd, 0.0, False
    elif max_dd >= DD_LEVEL_2:
        return max_dd, 0.25, False
    elif max_dd >= DD_LEVEL_1:
        return max_dd, 0.5, True
    return max_dd, 1.0, True


# ══════════════════════════════════════════════════════════════════════════════
# CONTINUOUS RUNNER
# ══════════════════════════════════════════════════════════════════════════════
def run_daemon(paper=True, scan_interval=300):
    """Run the engine continuously with graceful shutdown."""
    global _running
    
    mode = "PAPER" if paper else "LIVE"
    state = load_state()
    state["mode"] = mode.lower()
    state["bankroll"] = max(state.get("bankroll", BANKROLL_START), BANKROLL_START)
    save_state(state)
    
    log(f"═" * 60)
    log(f"  FDC V19.7 DAEMON — {mode} MODE")
    log(f"  Bankroll: ${state['bankroll']:,.2f} | Scan: {scan_interval}s")
    log(f"  EV GATE: min_ev=0.02 | DD: 10%→½, 15%→¼+halt, 25%→stop")
    log(f"  RISK: 1% cold → 2% warm → 3% proven | $10 cap")
    log(f"  RSI < 20 BLOCKED | Max positions: 2")
    log(f"═" * 60)
    
    if not acquire_lock():
        log("Could not acquire lock — another instance running?", "ERROR")
        return
    
    # Initialize live client
    from pm_engine_v19_7 import _init_live
    _init_live()
    
    consecutive_errors = 0
    max_errors = 10
    
    try:
        while _running:
            scan_start = time.time()
            try:
                entries, exits, skip_info, sig = run_scan(state, paper=paper)
                consecutive_errors = 0  # Reset on success
                
                if sig:
                    log(f"Signal: {sig.get('direction','?')} @ {sig.get('confidence',0):.2f} "
                        f"RSI={sig.get('rsi',0):.1f} Price={sig.get('price',0):,.0f}")
                if entries:
                    for e in entries:
                        log(f"  → {e['side']} {e.get('question','')[:40]} "
                            f"@ {e['contract_price']:.3f} ${e['bet']:.2f}")
                if exits:
                    for ex in exits:
                        pnl = ex.get("pnl", 0)
                        emoji = "✅" if pnl > 0 else "❌"
                        log(f"  ← {emoji} {ex.get('exit_type','settle')} "
                            f"P&L: ${pnl:+.2f}")
                
                # Kill switch check
                bankroll = state.get("bankroll", 0)
                if bankroll < 5:
                    log(f"💀 BANKROLL DEPLETED: ${bankroll:.2f} — HALTING", "CRIT")
                    break
                
                # DD circuit breaker check
                dd_pct, dd_mult, entries_allowed = _rolling_dd_from_state(state)
                if dd_pct >= 0.25:
                    log(f"🛑 CIRCUIT BREAKER: {dd_pct*100:.1f}% DD — HALTING", "CRIT")
                    break
                
            except Exception as e:
                consecutive_errors += 1
                log(f"Scan error ({consecutive_errors}/{max_errors}): {e}", "ERROR")
                traceback.print_exc()
                if consecutive_errors >= max_errors:
                    log(f"💀 {max_errors} consecutive errors — HALTING", "CRIT")
                    break
                time.sleep(30)
                continue
            
            # Sleep until next scan (interruptible)
            elapsed = time.time() - scan_start
            sleep_time = max(0, scan_interval - elapsed)
            while sleep_time > 0 and _running:
                time.sleep(min(sleep_time, 5))
                sleep_time -= 5
    
    finally:
        # Graceful shutdown
        log(f"Shutting down... saving state")
        save_state(state)
        print_status(state)
        release_lock()
        log(f"State saved. Final bankroll: ${state.get('bankroll',0):,.2f}")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 fdc_runner.py [paper|live|once|status]")
        sys.exit(1)
    
    cmd = sys.argv[1].lower()
    
    if cmd == "paper":
        run_daemon(paper=True, scan_interval=300)  # 5 minutes
    elif cmd == "live":
        print("⚠️  LIVE MODE — real money will be traded!")
        print("⚠️  Press Ctrl+C within 5 seconds to cancel...")
        time.sleep(5)
        run_daemon(paper=False, scan_interval=300)
    elif cmd == "once":
        state = load_state()
        state["mode"] = "paper"
        entries, exits, skip_info, sig = run_scan(state, paper=True)
        print_status(state)
    elif cmd == "status":
        state = load_state()
        print_status(state)
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python3 fdc_runner.py [paper|live|once|status]")
        sys.exit(1)