#!/usr/bin/env python3
"""
V20.3 GLOBAL LIVE BLOCK — Section 1
=====================================
Hard-blocks ALL live trading paths.

Any attempt to place a live order MUST raise:
    RuntimeError("LIVE_BLOCKED_REALITY_ALIGNMENT_FAILED")

Status flags:
  LIVE_ENABLED = False
  MICRO_LIVE_ENABLED = False
  PRODUCTION_ENABLED = False
  PROMOTION_FREEZE = True
  REALITY_ALIGNMENT_FAILED = True

These flags MUST NOT be changed until V20.3_BINARY_REALITY_VALIDATION passes.

Author: Hugh (3rd of 5) for Father Daddy Capital
"""

# ══════════════════════════════════════════════════════════════════
# GLOBAL LIVE BLOCK — Section 1
# ══════════════════════════════════════════════════════════════════

LIVE_ENABLED = False                    # DO NOT FLIP
MICRO_LIVE_ENABLED = False              # DO NOT FLIP
PRODUCTION_ENABLED = False              # DO NOT FLIP
PROMOTION_FREEZE = True                 # Block all promotions
REALITY_ALIGNMENT_FAILED = True         # V20.2 found fatal PnL mismatch

# ══════════════════════════════════════════════════════════════════
# Hard Block Enforcement
# ══════════════════════════════════════════════════════════════════

LIVE_BLOCK_MESSAGE = (
    "LIVE_BLOCKED_REALITY_ALIGNMENT_FAILED: "
    "V20.2 found paper PnL (-$0.48) vs real PnL (-$22.00). "
    "Binary settlement rebuild required. "
    "See V20.3_BINARY_REBUILD_REQUIRED."
)


def enforce_live_block():
    """Raise RuntimeError if any live path is attempted.
    
    This function MUST be called before any order placement.
    It checks the global live block flags and raises immediately
    if any reality alignment issue is unresolved.
    """
    if REALITY_ALIGNMENT_FAILED:
        raise RuntimeError(LIVE_BLOCK_MESSAGE)
    if not LIVE_ENABLED:
        raise RuntimeError(
            f"LIVE_BLOCKED: LIVE_ENABLED={LIVE_ENABLED}. "
            f"Cannot place live orders."
        )
    if not MICRO_LIVE_ENABLED:
        raise RuntimeError(
            f"MICRO_LIVE_BLOCKED: MICRO_LIVE_ENABLED={MICRO_LIVE_ENABLED}. "
            f"Cannot place micro-live orders."
        )
    if PROMOTION_FREEZE:
        raise RuntimeError(
            f"PROMOTION_BLOCKED: PROMOTION_FREEZE={PROMOTION_FREEZE}. "
            f"Cannot promote paper configurations to live."
        )


def check_live_status() -> dict:
    """Return current live block status for diagnostics."""
    return {
        "LIVE_ENABLED": LIVE_ENABLED,
        "MICRO_LIVE_ENABLED": MICRO_LIVE_ENABLED,
        "PRODUCTION_ENABLED": PRODUCTION_ENABLED,
        "PROMOTION_FREEZE": PROMOTION_FREEZE,
        "REALITY_ALIGNMENT_FAILED": REALITY_ALIGNMENT_FAILED,
        "live_blocked": True,
        "reason": "V20.2 reality alignment failed. Paper PnL -$0.48 vs real PnL -$22.00. "
                  "All settlements must use binary 0/1, not midpoint 0.50.",
        "required_action": "Pass V20.3_BINARY_REALITY_VALIDATION before any live path.",
        "status_flags": {
            "RETIRED": [
                "V20.1_REPAIR_VALIDATED",
                "BTC_BALANCED_50_60_LIVE_READY",
                "MICRO_LIVE_READY",
            ],
            "ACTIVE": [
                "V20.2_REALITY_ALIGNMENT_FAILED",
                "V20.3_BINARY_REBUILD_REQUIRED",
            ],
            "PENDING": [
                "V20.3_BINARY_REALITY_VALIDATION",
            ],
        },
    }


# ══════════════════════════════════════════════════════════════════
# Section 12: Kill Old Classifications
# ══════════════════════════════════════════════════════════════════

RETIRED_CLASSIFICATIONS = {
    "V20.1_REPAIR_VALIDATED": "RETIRED — V20.2 found paper reality mismatch",
    "BTC_BALANCED_50_60_LIVE_READY": "RETIRED — UP win rate 18.2% (2/11), not 50%",
    "MICRO_LIVE_READY": "RETIRED — live blocked until V20.3 validation passes",
}

ACTIVE_CLASSIFICATIONS = {
    "V20.2_REALITY_ALIGNMENT_FAILED": "Paper PnL masked 9/11 total losses via midpoint 0.50",
    "V20.3_BINARY_REBUILD_REQUIRED": "All modules rebuilt for binary settlement",
}

PENDING_CLASSIFICATIONS = {
    "V20.3_BINARY_REALITY_VALIDATION": "Must pass before any live path re-enabled",
}


def get_classification_status() -> dict:
    """Return classification status for all V20.x tags."""
    return {
        "retired": RETIRED_CLASSIFICATIONS,
        "active": ACTIVE_CLASSIFICATIONS,
        "pending": PENDING_CLASSIFICATIONS,
    }