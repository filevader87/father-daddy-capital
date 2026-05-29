#!/usr/bin/env python3
"""
FDC Watchdog — Monitors engine health, alerts on DD/bankroll/crash conditions.
Runs as a cronjob every 15 minutes. Sends alerts if:
  - Bankroll drops below threshold
  - Circuit breaker triggered (DD ≥ 25%)
  - Engine process not running (crashed)
  - No scans in last 30 minutes (stuck)
  - Daily loss exceeds $20
  
Usage: python3 fdc_watchdog.py
"""
import json, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).parent
STATE = REPO / "output" / "pm_state.json"
LOG_FILE = REPO / "output" / "fdc_runner.log"
LOCK_FILE = REPO / "output" / ".fdc_lock"

BANKROLL_FLOOR = 50.0      # Alert if bankroll < $50
DAILY_LOSS_LIMIT = 20.0    # Alert if daily loss > $20
MAX_STALE_MINUTES = 30     # Alert if no scan in 30 min
DD_HALT_THRESHOLD = 0.25   # Alert if DD ≥ 25%

def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except:
            pass
    return None

def check_process():
    """Check if FDC runner process is alive."""
    if not LOCK_FILE.exists():
        return False, "No lock file — engine not running"
    try:
        pid = int(LOCK_FILE.read_text().strip())
        result = subprocess.run(["kill", "-0", str(pid)], capture_output=True)
        if result.returncode == 0:
            return True, f"Engine running (PID {pid})"
        return False, f"Engine process {pid} not found — CRASHED"
    except (ValueError, FileNotFoundError):
        return False, "Invalid lock file"

def check_bankroll(state):
    """Check bankroll and DD thresholds."""
    alerts = []
    bankroll = state.get("bankroll", 0)
    if bankroll < BANKROLL_FLOOR:
        alerts.append(f"💀 BANKROLL LOW: ${bankroll:.2f} < ${BANKROLL_FLOOR}")
    
    daily_pnl = state.get("daily_pnl", 0)
    if daily_pnl < -DAILY_LOSS_LIMIT:
        alerts.append(f"📉 DAILY LOSS: ${daily_pnl:+.2f} exceeds -${DAILY_LOSS_LIMIT}")
    
    # Rolling DD from journal
    journal = state.get("journal", [])
    settled = [j for j in journal if j.get("type") in ("settle", "exit_stop_loss", "exit_time_decay", "exit_expiry")]
    recent = settled[-50:] if len(settled) >= 50 else settled
    if recent:
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for j in recent:
            cum += j.get("pnl", 0)
            if cum > peak:
                peak = cum
            if peak > 0:
                max_dd = max(max_dd, (peak - cum) / peak)
        if max_dd >= DD_HALT_THRESHOLD:
            alerts.append(f"🛑 CIRCUIT BREAKER: {max_dd*100:.1f}% DD ≥ {DD_HALT_THRESHOLD*100:.0f}%")
        elif max_dd >= 0.15:
            alerts.append(f"⚠️ HIGH DD: {max_dd*100:.1f}%")
    
    return alerts

def check_stale(state):
    """Check if engine is stuck (no recent scans)."""
    last_scan = state.get("last_scan", "")
    if not last_scan:
        return ["⚠️ No scan history — engine may be stuck"]
    
    try:
        last = datetime.fromisoformat(last_scan)
        minutes_ago = (datetime.now() - last).total_seconds() / 60
        if minutes_ago > MAX_STALE_MINUTES:
            return [f"⏰ STALE: Last scan {minutes_ago:.0f} min ago (limit: {MAX_STALE_MINUTES}min)"]
    except:
        return [f"⚠️ Invalid last_scan: {last_scan}"]
    
    return []

def watchdog_check():
    """Full watchdog check. Returns list of alert strings."""
    alerts = []
    
    # 1. Process alive?
    alive, msg = check_process()
    if not alive:
        alerts.append(msg)
    
    # 2. State checks
    state = load_state()
    if state is None:
        alerts.append("❌ No state file — engine never started or state corrupted")
        return alerts
    
    # 3. Bankroll/DD
    alerts.extend(check_bankroll(state))
    
    # 4. Stale check
    alerts.extend(check_stale(state))
    
    # 5. Summary
    bankroll = state.get("bankroll", 0)
    pnl = state.get("total_pnl", 0)
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    positions = len([p for p in state.get("positions", {}).values() if p.get("status") == "open"])
    
    status = "🟢 HEALTHY" if not alerts else "🔴 ALERT"
    print(f"FDC Watchdog {status}")
    print(f"  Bankroll: ${bankroll:.2f} | P&L: ${pnl:+.2f} | W/L: {wins}/{losses} | Open: {positions}")
    
    if alerts:
        print(f"\n  ⚠️ ALERTS:")
        for a in alerts:
            print(f"    {a}")
    else:
        print(f"  ✅ All checks passed")
    
    return alerts

if __name__ == "__main__":
    alerts = watchdog_check()
    sys.exit(len(alerts))  # Exit 0 = healthy, non-zero = alerts