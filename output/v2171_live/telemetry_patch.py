"""
V21.7.1 TELEMETRY PATCH — No-Trade Reason Classification + Bucket Occupancy + Near-Miss + Scarcity Reports

This patch adds telemetry to v2171_live_runner.py per directive §3-7.
It does NOT modify any live entry rules per §2.
"""

# ═══════════════════════════════════════════════════════════════════════
# §3: NO-TRADE REASON CATEGORIES
# ═══════════════════════════════════════════════════════════════════════

NOTRADE_REASONS = [
    "bucket_below_floor",      # down_mid < 0.03
    "bucket_above_cap",        # down_mid >= 0.12
    "wrong_state",             # signal not DOWN_MOMENTUM/DOWN_CONTINUATION
    "low_survivability",       # survivability < 0.05
    "too_near_expiry",         # expires_in < 30s
    "duplicate_position",      # already have position on this condition
    "stale_quote",             # no orderbook data
    "no_book",                 # no bids/asks in orderbook
    "no_active_market",        # no contract found
    "execution_rejected",      # kill switch or order failed
    "risk_limit_block",        # daily/weekly/consecutive loss limit hit
    "spread_too_wide",         # spread > 25% of price
    "no_momentum",             # vol_imbalance not bearish enough
]

# ═══════════════════════════════════════════════════════════════════════
# §4: BUCKET OCCUPANCY TRACKING
# ═══════════════════════════════════════════════════════════════════════

BUCKET_RANGES = {
    "0_3c":      (0.000, 0.030),
    "3_5c":      (0.030, 0.050),
    "5_8c":      (0.050, 0.080),
    "8_12c":     (0.080, 0.120),
    "12_20c":    (0.120, 0.200),
    "20_40c":    (0.200, 0.400),
    "above_40c": (0.400, 999.0),
}

# ═══════════════════════════════════════════════════════════════════════
# §5: ELIGIBLE BUCKET SECONDS
# ═══════════════════════════════════════════════════════════════════════

# eligible_bucket_seconds: total seconds where DOWN ask ∈ [0.03, 0.12)
# preferred_bucket_seconds: total seconds where DOWN ask ∈ [0.05, 0.08)

# ═══════════════════════════════════════════════════════════════════════
# §6: NEAR-MISS DEFINITION
# ═══════════════════════════════════════════════════════════════════════

# A near-miss is any scan where at least 3 of the following are true:
#   asset = BTC
#   side = DOWN
#   bucket within 0.03-0.12
#   state = MOMENTUM or near-MOMENTUM (vol_imbalance < -0.05)
#   survivability within 20% of threshold (>= 0.04)
#   time_to_expiry > 30s
#   book is fresh (has bids and asks)
#   no duplicate position

NEAR_MISS_THRESHOLD = 3  # out of 8 criteria

# ═══════════════════════════════════════════════════════════════════════
# §7: SCARCITY REPORT — EVERY 30 MINUTES
# ═══════════════════════════════════════════════════════════════════════

SCARCITY_REPORT_INTERVAL = 1800  # 30 minutes in seconds

BOTTLLENECK_TYPES = [
    "BUCKET_SCARCITY",
    "STATE_SCARCITY",
    "SURVIVABILITY_SCARCITY",
    "EXECUTION_SCARCITY",
    "RISK_BLOCK",
    "UNKNOWN",
]