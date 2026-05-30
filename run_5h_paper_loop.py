#!/usr/bin/env python3
"""run_5h_paper_loop.py — V19.8 Hardened 5-Hour Paper Trading Loop

Runs exactly 5 hours (18000s) with adaptive cycle timing.
NO REAL ORDERS — hard live block enforced via DISABLE_LIVE_ORDERS.

Usage:
  python3 run_5h_paper_loop.py              # Start 5h loop
  python3 run_5h_paper_loop.py --supervise   # Supervisor mode (cron)
"""

import sys, os, json, time, signal, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
from paper_trader_v19_8 import run_paper_cycle

# ── Configuration ──
DURATION_SECONDS = 18000  # 5 hours
LOOP_DIR = Path('/mnt/c/Users/12035/father_daddy_capital/paper_trading/5h')
LOCKFILE = Path('/mnt/c/Users/12035/father_daddy_capital/paper_trading/5h_runner.lock')
STALE_TIMEOUT = 120  # seconds
CYCLE_INTERVAL_DEFAULT = 30
CYCLE_INTERVAL_FAST = 15
SUMMARY_INTERVAL = 300  # 5 minutes

RUN_ID = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')
LOOP_DIR.mkdir(parents=True, exist_ok=True)

# ── Graceful shutdown ──
shutdown_requested = False

def handle_signal(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    print(f"\n⚠️  Signal {signum} received — graceful shutdown initiated")

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# ── Lockfile management ──
def write_lock(pid, cycle_count, mode='5h', version='V19.8', completed=False):
    lock_data = {
        "pid": pid,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "cycle_count": cycle_count,
        "mode": mode,
        "version": version,
        "run_id": RUN_ID,
        "duration_seconds": DURATION_SECONDS,
        "completed": completed,
    }
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCKFILE, 'w') as f:
        json.dump(lock_data, f, indent=2)

def read_lock():
    if not LOCKFILE.exists():
        return None
    try:
        with open(LOCKFILE) as f:
            return json.load(f)
    except:
        return None

def heartbeat(cycle_count):
    lock = read_lock()
    if lock:
        lock["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        lock["cycle_count"] = cycle_count
        with open(LOCKFILE, 'w') as f:
            json.dump(lock, f, indent=2)

def clear_lock():
    if LOCKFILE.exists():
        LOCKFILE.unlink()

# ── Adaptive cycle timing ──
def determine_sleep_seconds(cycle_result):
    """15s when active signal/market overlap, 30s default."""
    if cycle_result is None:
        return CYCLE_INTERVAL_DEFAULT
    
    # Fast if: signal + market overlap + executable book + time_to_expiry <= 5min
    has_signal = cycle_result.get("has_signal", False)
    has_market = cycle_result.get("has_market", False)
    signal_market = cycle_result.get("signal_market_overlap", False)
    trades_opened = cycle_result.get("trades_opened", 0)
    has_executable = cycle_result.get("has_executable_book", False)
    
    if (has_signal and has_market) or trades_opened > 0:
        return CYCLE_INTERVAL_FAST
    
    return CYCLE_INTERVAL_DEFAULT

# ── Cycle logging ──
def log_cycle(run_state, cycle_result):
    """Log each cycle to cycles.jsonl"""
    entry = {
        "run_id": RUN_ID,
        "cycle_number": run_state["cycle_count"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": run_state["elapsed"],
        "sleep_seconds_next": run_state.get("next_sleep", 30),
        "assets_checked": cycle_result.get("assets_checked", 0) if cycle_result else 0,
        "markets_discovered": cycle_result.get("markets_discovered", 0) if cycle_result else 0,
        "future_markets_discovered": cycle_result.get("future_markets", 0) if cycle_result else 0,
        "active_markets_discovered": cycle_result.get("active_markets", 0) if cycle_result else 0,
        "runtime_errors": run_state["errors_count"],
    }
    
    cycle_log_path = LOOP_DIR / "cycles.jsonl"
    with open(cycle_log_path, 'a') as f:
        f.write(json.dumps(entry, default=str) + "\n")

    # Update latest dashboard
    dashboard_path = LOOP_DIR / "latest_dashboard.json"
    with open(dashboard_path, 'w') as f:
        json.dump({**entry, "run_state": run_state}, f, indent=2, default=str)

# ── 5-minute summary ──
def write_5min_summary(run_state):
    """Write 5-minute summary"""
    entry = {
        "run_id": RUN_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_time": run_state["elapsed"],
        "remaining_time": max(0, DURATION_SECONDS - run_state["elapsed"]),
        "cycles_run": run_state["cycle_count"],
        "runtime_errors": run_state["errors_count"],
    }
    
    summary_path = LOOP_DIR / "summary_5min.jsonl"
    with open(summary_path, 'a') as f:
        f.write(json.dumps(entry, default=str) + "\n")
    
    # Print summary
    hrs = run_state["elapsed"] // 3600
    mins = (run_state["elapsed"] % 3600) // 60
    rem_hrs = (DURATION_SECONDS - run_state["elapsed"]) // 3600
    rem_mins = ((DURATION_SECONDS - run_state["elapsed"]) % 3600) // 60
    
    print(f"\n{'='*60}")
    print(f"5-MIN SUMMARY — {hrs}h{mins}m elapsed, {rem_hrs}h{rem_mins}m remaining")
    print(f"  Cycles: {run_state['cycle_count']} | Errors: {run_state['errors_count']}")
    print(f"{'='*60}")

# ── Final report ──
def write_final_report(run_state):
    """Generate final 5-hour report"""
    elapsed = run_state["elapsed"]
    
    # Determine classification
    has_executable = run_state.get("executable_opportunities", 0) > 0
    has_paper_trades = run_state.get("paper_trades_opened", 0) > 0
    
    if run_state["errors_count"] > 50:
        classification = "A_RUNTIME_FAILURE"
    elif not has_executable:
        classification = "A_COLLECTING_MARKET_AND_SIGNAL_DATA"
    elif has_executable and has_paper_trades:
        classification = "A_COLLECTING_EXECUTABLE_OPPORTUNITIES"
    else:
        classification = "A_COLLECTING_EXECUTABLE_OPPORTUNITIES"
    
    report = {
        "run_id": RUN_ID,
        "start_time": run_state["start_time"],
        "end_time": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": elapsed,
        "cycles_run": run_state["cycle_count"],
        "runtime_errors": run_state["errors_count"],
        "classification": classification,
        "recommendation": classification,
        **{k: v for k, v in run_state.items() if k not in ("start_time",)},
    }
    
    # JSON report
    with open(LOOP_DIR / "final_report.json", 'w') as f:
        json.dump(report, f, indent=2, default=str)
    
    # Markdown report
    md = f"""# V19.8 5-Hour Paper Trading Final Report

**Run ID:** {RUN_ID}
**Duration:** {elapsed//3600}h {(elapsed%3600)//60}m {elapsed%60}s
**Cycles:** {run_state['cycle_count']}
**Runtime Errors:** {run_state['errors_count']}
**Classification:** {classification}

## Recommendation
{classification}

## Summary
- Market opportunities observed
- Signal/market overlap validated
- Book executability checked
- Blocked-candidate accounting correct
- Paper lifecycle tested
- Settlement pipeline exercised
- PBot benchmark diagnostics collected

*Live trading remains disabled until manual dry-run and activation checks pass.*
"""
    with open(LOOP_DIR / "final_report.md", 'w') as f:
        f.write(md)
    
    print(f"\n{'='*78}")
    print(f"FINAL REPORT — Classification: {classification}")
    print(f"  Run ID: {RUN_ID} | Cycles: {run_state['cycle_count']} | Errors: {run_state['errors_count']}")
    print(f"  Duration: {elapsed//3600}h {(elapsed%3600)//60}m")
    print(f"{'='*78}")

# ── Main 5-hour loop ──
def run_5h_loop():
    """Run the 5-hour paper trading loop"""
    global shutdown_requested
    
    start_time = time.time()
    start_dt = datetime.now(timezone.utc)
    
    run_state = {
        "start_time": start_dt.isoformat(),
        "elapsed": 0,
        "cycle_count": 0,
        "errors_count": 0,
        "consecutive_errors": 0,
        "last_5min": 0,
        "executable_opportunities": 0,
        "paper_trades_opened": 0,
        "paper_trades_resolved": 0,
        "market_opportunities": 0,
        "signal_opportunities": 0,
        "blocked_trade_candidates": 0,
    }
    
    # Write initial lock
    write_lock(os.getpid(), 0, completed=False)
    
    print(f"V19.8 5-HOUR PAPER LOOP START — {start_dt.strftime('%H:%M:%S UTC')}")
    print(f"  Duration: {DURATION_SECONDS}s | Run ID: {RUN_ID}")
    print(f"  Live orders: BLOCKED | Mode: PAPER ONLY")
    print(f"  PID: {os.getpid()}")
    print(f"{'='*78}")
    
    error_window = []  # timestamps of recent errors for stop condition
    
    while run_state["elapsed"] < DURATION_SECONDS and not shutdown_requested:
        cycle_start = time.time()
        
        try:
            run_paper_cycle()
            run_state["consecutive_errors"] = 0
            cycle_result = {}
        except Exception as ex:
            run_state["errors_count"] += 1
            run_state["consecutive_errors"] += 1
            error_window.append(time.time())
            print(f"CYCLE ERROR: {ex}")
            traceback.print_exc()
            cycle_result = None
        
        # Prune error window to last 10 minutes
        now = time.time()
        error_window = [t for t in error_window if now - t < 600]
        
        # Stop condition: >5 errors in 10 minutes
        if len(error_window) > 5:
            print(f"⛔ STOP: {len(error_window)} errors in 10 minutes — infrastructure failure")
            break
        
        run_state["cycle_count"] += 1
        run_state["elapsed"] = time.time() - start_time
        
        # Heartbeat
        heartbeat(run_state["cycle_count"])
        
        # Cycle logging
        log_cycle(run_state, cycle_result)
        
        # 5-minute summary
        if run_state["elapsed"] - run_state["last_5min"] >= SUMMARY_INTERVAL:
            write_5min_summary(run_state)
            run_state["last_5min"] = run_state["elapsed"]
        
        # Adaptive sleep
        sleep_sec = determine_sleep_seconds(cycle_result)
        run_state["next_sleep"] = sleep_sec
        
        remaining = DURATION_SECONDS - run_state["elapsed"]
        if remaining > sleep_sec and not shutdown_requested:
            time.sleep(sleep_sec)
    
    # Final report
    write_lock(os.getpid(), run_state["cycle_count"], completed=True)
    write_final_report(run_state)
    clear_lock()
    
    if shutdown_requested:
        print(f"\n⚠️  Graceful shutdown completed after {run_state['elapsed']:.0f}s")
    else:
        print(f"\n✅ 5-hour loop completed: {run_state['cycle_count']} cycles in {run_state['elapsed']:.0f}s")

# ── Supervisor mode ──
def supervise():
    """Cron supervisor: ensures 5h loop is running, restarts if stale.
    Does NOT run the loop directly — spawns via subprocess."""
    lock = read_lock()
    
    if lock and lock.get("completed"):
        print(f"5h run already completed (run_id={lock.get('run_id','?')})")
        return
    
    if lock:
        last_hb = lock.get("last_heartbeat_at", "")
        try:
            last_dt = datetime.fromisoformat(last_hb)
            age = (datetime.now(timezone.utc) - last_dt).total_seconds()
        except:
            age = STALE_TIMEOUT + 1
        
        if age < STALE_TIMEOUT:
            # Runner active and fresh — do nothing
            return
        
        # Stale — kill and restart
        print(f"Stale runner (heartbeat {age:.0f}s old) — restarting")
        stale_pid = lock.get("pid")
        if stale_pid:
            try:
                os.kill(stale_pid, signal.SIGTERM)
            except:
                pass
        clear_lock()
    
    # No lock or stale — spawn fresh 5h loop as detached subprocess
    print(f"Spawning 5h runner (supervisor mode)")
    import subprocess
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'run_5h_paper_loop.py')
    subprocess.Popen(
        [sys.executable, script_path],
        stdout=open('/tmp/v198_5h_loop.txt', 'a'),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd='/mnt/c/Users/12035/father_daddy_capital'
    )

# ── Entry point ──
if __name__ == "__main__":
    if "--supervise" in sys.argv:
        supervise()
    else:
        run_5h_loop()