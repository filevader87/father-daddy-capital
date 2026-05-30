#!/usr/bin/env python3
"""V19.7h Paper Trading Runner — singleton-protected, high-frequency loop.

Runs for ~4.5 minutes (9 cycles at 30s intervals), called by 5-minute cron.
Enforces singleton execution via lockfile with PID tracking and heartbeat.
"""

import os, sys, time, json, signal
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')

LOCK_FILE = Path('/mnt/c/Users/12035/father_daddy_capital/paper_trading/runner.lock')
CYCLE_INTERVAL = 30
MAX_CYCLES = 9
STALE_LOCK_TIMEOUT = 300  # 5 minutes — if heartbeat is older, lock is stale

def acquire_lock():
    """Singleton lock with PID, heartbeat, and stale detection."""
    LOCK_FILE.parent.mkdir(exist_ok=True)
    
    if LOCK_FILE.exists():
        try:
            lock_data = json.loads(LOCK_FILE.read_text())
            pid = lock_data.get("pid", 0)
            started_at = lock_data.get("started_at", "")
            last_heartbeat = lock_data.get("last_heartbeat_at", "")
            cycle_count = lock_data.get("cycle_count", 0)
            
            # Check if PID is still alive
            try:
                os.kill(pid, 0)  # Signal 0 = check existence
                # PID is alive — another runner is active
                heartbeat_time = datetime.fromisoformat(last_heartbeat.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - heartbeat_time).total_seconds()
                if age < STALE_LOCK_TIMEOUT:
                    print(f"LOCKED: Runner PID {pid} is active (heartbeat {age:.0f}s ago, {cycle_count} cycles). Exiting.")
                    sys.exit(0)
                else:
                    print(f"STALE LOCK: PID {pid} heartbeat {age:.0f}s ago (> {STALE_LOCK_TIMEOUT}s). Clearing.")
            except (ProcessLookupError, OSError):
                # PID is dead — lock is stale
                print(f"STALE LOCK: PID {pid} is dead. Clearing.")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"CORRUPT LOCK: {e}. Clearing.")
    
    # Acquire lock
    lock_data = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "cycle_count": 0,
    }
    LOCK_FILE.write_text(json.dumps(lock_data, indent=2))
    return lock_data

def update_heartbeat(cycle_count):
    """Update lockfile heartbeat."""
    lock_data = json.loads(LOCK_FILE.read_text())
    lock_data["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    lock_data["cycle_count"] = cycle_count
    LOCK_FILE.write_text(json.dumps(lock_data, indent=2))

def release_lock():
    """Remove lockfile on exit."""
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass

def graceful_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT — release lock and exit."""
    print(f"\nReceived signal {signum}. Graceful shutdown.")
    release_lock()
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT, graceful_shutdown)

def main():
    lock_data = acquire_lock()
    atexit_registered = False
    try:
        import atexit
        atexit.register(release_lock)
        atexit_registered = True
    except:
        pass
    
    from paper_trader_v19_7i import run_paper_cycle
    
    print(f"Runner PID {os.getpid()} started at {lock_data['started_at']}")
    print(f"Running {MAX_CYCLES} cycles at {CYCLE_INTERVAL}s intervals")
    print(f"Lockfile: {LOCK_FILE}")
    print(f"Stale lock timeout: {STALE_LOCK_TIMEOUT}s")
    print()
    
    for i in range(MAX_CYCLES):
        try:
            print(f"\n{'─'*70}")
            print(f"Cycle {i+1}/{MAX_CYCLES} | {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
            run_paper_cycle()
        except Exception as e:
            print(f"CYCLE ERROR: {e}")
            import traceback
            traceback.print_exc()
        
        update_heartbeat(i + 1)
        
        if i < MAX_CYCLES - 1:
            time.sleep(CYCLE_INTERVAL)
    
    release_lock()
    print(f"\nRunner completed {MAX_CYCLES} cycles. Lock released.")

if __name__ == "__main__":
    main()