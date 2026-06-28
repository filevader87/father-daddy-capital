#!/usr/bin/env python3
"""
V21.7.33 — Process Affinity Module
====================================
Optional CPU affinity for process-split architecture.
Feed + scanner prioritized. Journal deprioritized.
Does NOT fail if affinity is unsupported.
"""

import os
import logging
from typing import Dict, Optional

log = logging.getLogger('process_affinity')

# ─── Process Role Definitions (Section 7) ───

PROCESS_ROLES = {
    "feed_ingestion": {
        "description": "WS/CLOB book ingestion, quote cache updates",
        "priority": 1,  # Highest priority
        "suggested_core": 0,
    },
    "scanner_decision": {
        "description": "Zone classification, entry signal evaluation",
        "priority": 2,
        "suggested_core": 1,
    },
    "order_executor": {
        "description": "Order submission, position management",
        "priority": 3,
        "suggested_core": 2,
    },
    "journal_reporter": {
        "description": "JSONL writes, status reports, async flush",
        "priority": 4,  # Lowest priority — never blocks order path
        "suggested_core": 3,
    },
}


def get_cpu_count() -> int:
    """Get available CPU count."""
    return os.cpu_count() or 1


def is_affinity_supported() -> bool:
    """Check if CPU affinity is supported (Linux only)."""
    return hasattr(os, "sched_setaffinity")


def get_affinity_mapping() -> Dict[str, Optional[int]]:
    """Map process roles to CPU cores based on available cores.
    
    Priority order: feed_ingestion > scanner_decision > order_executor > journal_reporter
    
    If fewer cores available:
    - 4+ cores: full mapping (core 0-3)
    - 3 cores: journal shares with feed
    - 2 cores: feed+scanner on core 0, executor+journal on core 1
    - 1 core: no affinity (all share)
    """
    nproc = get_cpu_count()
    
    if nproc >= 4:
        return {
            "feed_ingestion": 0,
            "scanner_decision": 1,
            "order_executor": 2,
            "journal_reporter": 3,
        }
    elif nproc == 3:
        return {
            "feed_ingestion": 0,
            "scanner_decision": 1,
            "order_executor": 2,
            "journal_reporter": 0,  # shares with feed (deprioritized)
        }
    elif nproc == 2:
        return {
            "feed_ingestion": 0,
            "scanner_decision": 0,  # shares with feed (both high priority)
            "order_executor": 1,
            "journal_reporter": 1,  # shares with executor (deprioritized)
        }
    else:
        # Single core — no affinity possible
        return {
            "feed_ingestion": None,
            "scanner_decision": None,
            "order_executor": None,
            "journal_reporter": None,
        }


def set_affinity(role: str, pid: Optional[int] = None) -> Dict[str, object]:
    """Set CPU affinity for a process role.
    
    Args:
        role: One of feed_ingestion, scanner_decision, order_executor, journal_reporter
        pid: Process ID (defaults to current process)
    
    Returns:
        dict with classification, affinity_set, core, role
    """
    if role not in PROCESS_ROLES:
        return {
            "classification": "CPU_AFFINITY_UNKNOWN_ROLE",
            "role": role,
            "affinity_set": False,
            "core": None,
            "error": f"Unknown role: {role}. Valid: {list(PROCESS_ROLES.keys())}",
        }

    if not is_affinity_supported():
        return {
            "classification": "CPU_AFFINITY_UNSUPPORTED",
            "role": role,
            "affinity_set": False,
            "core": None,
            "note": "os.sched_setaffinity not available (not Linux or insufficient permissions)",
        }

    mapping = get_affinity_mapping()
    core = mapping.get(role)

    if core is None:
        return {
            "classification": "CPU_AFFINITY_SKIPPED",
            "role": role,
            "affinity_set": False,
            "core": None,
            "note": f"Insufficient CPU cores for affinity mapping ({get_cpu_count()} cores)",
        }

    target_pid = pid or os.getpid()
    
    try:
        os.sched_setaffinity(target_pid, {core})
        log.info(f"CPU affinity set: {role} -> core {core} (pid={target_pid})")
        return {
            "classification": "CPU_AFFINITY_ENABLED",
            "role": role,
            "affinity_set": True,
            "core": core,
            "pid": target_pid,
        }
    except (OSError, PermissionError) as e:
        log.warning(f"CPU affinity failed: {role} -> core {core}: {e}")
        return {
            "classification": "CPU_AFFINITY_FAILED",
            "role": role,
            "affinity_set": False,
            "core": core,
            "error": str(e),
        }


def get_current_affinity() -> Dict[str, object]:
    """Get current process CPU affinity."""
    if not is_affinity_supported():
        return {
            "classification": "CPU_AFFINITY_UNSUPPORTED",
            "affinity": None,
        }
    
    try:
        affinity = os.sched_getaffinity(os.getpid())
        return {
            "classification": "CPU_AFFINITY_QUERY_OK",
            "pid": os.getpid(),
            "affinity": sorted(affinity),
        }
    except (OSError, PermissionError) as e:
        return {
            "classification": "CPU_AFFINITY_QUERY_FAILED",
            "affinity": None,
            "error": str(e),
        }


def classify_affinity_status() -> str:
    """Classify overall affinity status for reporting."""
    nproc = get_cpu_count()
    
    if not is_affinity_supported():
        return "CPU_AFFINITY_UNSUPPORTED"
    elif nproc >= 4:
        return "CPU_AFFINITY_ENABLED"
    elif nproc >= 2:
        return "CPU_AFFINITY_SUPPORTED_FEWER_CORES"
    else:
        return "CPU_AFFINITY_SKIPPED"


if __name__ == "__main__":
    import json
    from pathlib import Path
    
    OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v21733_feed_hotpath_optimizer")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    
    report = {
        "cpu_count": get_cpu_count(),
        "affinity_supported": is_affinity_supported(),
        "classification": classify_affinity_status(),
        "process_mapping": get_affinity_mapping(),
        "current_affinity": get_current_affinity(),
        "process_roles": {k: {**v, "suggested_core": get_affinity_mapping().get(k)} for k, v in PROCESS_ROLES.items()},
        "note": "Affinity not applied to current single-process architecture. Will be applied when process-split architecture is implemented.",
    }
    
    with open(OUT_DIR / "cpu_affinity_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    
    print(json.dumps(report, indent=2, default=str))