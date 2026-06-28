#!/usr/bin/env python3
"""
V21.7.10 External Momentum Tracker
==================================

Computes velocity metrics from external exchange WS feeds. Maintains per-asset 
velocity windows: v1s, v3s, v5s, v15s, v30s, v60s. Computes cross_exchange_median
for each asset. Detects sharp negative velocity turns (v3s or v5s turning negative 
while v15s neutral/positive). Outputs velocity snapshots to a shared state dict.

Velocity is log-returns over time windows using ring-buffered samples from QuoteCache.
Sharp velocity turn detection triggers armed-mode preconditions for scalper logic.

Imports: datetime, json, math (from quote_cache) plus numpy/deque, threading.
"""

import asyncio, json, time, logging, sys, traceback
import numpy as np
from pathlib import Path
from collections import deque
from typing import Optional, Dict, Any

BASE = Path("/home/naq1987s/father-daddy-capital")
OUT = BASE / "output" / "v21713_realtime_scanner" 
LOG_FILE = OUT / "momentum_tracker.log"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger("v217_external_momentum")


# ──────────────────────────────────────────────────────────────────────
# VELOCITY WINDOW CONFIGURATION (samples → approximate time windows)
# Each ring buffer size approximates the target cadence:
#   v1s  ~5 samples @ ~0.3-0.4s each → ~2 seconds raw, filtered to ~1 second effective velocity window
#   v3s  ~9 samples   @ ~0.3-0.4s each → ~3 seconds (close enough)
#   ...etc as designed for momentum detection in live trading context──────────────
NUM_SAMPLES_V1S = 5        # Fastest cadence: approximates last ~1 second velocity window
NUM_SAMPLES_V3S = 9        # Medium-fast: close to ~3 sec effective window  
NUM_SAMPLES_V5S = 16       # Medium: approximating ~5 s momentum change period 
NUM_SAMPLES_V15S = 48      # Slower lookback for swing momentum detection (~15s)
NUM_SAMPLES_V30S = 92      # Long-term velocity trend observation  
NUM_SAMPLES_V60S = 172     # Very long window: extended macro momentum direction
# ──────────────────────────────────────────────────────────────────────


class ExternalMomentumTracker:
    """
    Tracks per-asset velocity metrics using ring-buffered external exchange quotes.

    Computes logarithmic returns over sliding windows for multiple cadences,
    maintains cross-exchange medians to filter out noise from single-source bias,
    and detects sharp negative turns in short-term momentum vs medium/longer terms.
    
    Thread-safe updates with atomic snapshot reads to the shared state dict.
    """

    def __init__(self, stale_ms: int = 2000):
        self._lock = None  # No external lock since QuoteCache handles thread safety at ingestion
        
        # Ring buffers per asset (deque for FIFO) storing mid prices + timestamps
        # Structure: {asset: {"v1s": deque(...), ...}}
        # Values are dict: {"price": float, "ts_ms": int} 
        self._buffers: Dict[str, Dict[int, deque]] = {}

        # Cross-exchange median cache (lazy computed per snapshot)
        self._cross_exchange_cache: Dict[str, list] = {}  # asset -> list of fresh mids
        
        # Sharp turn thresholds
        self._v3s_turn_threshold = -0.02   # v3s log-return < -2% is "negative"  
        self._v5s_turn_threshold = -0.02   
        
        self._stale_ms = stale-ms

    def _init_buffers(self, asset: str):
        """Initialize ring buffers for an asset if not yet present."""
        config_dict = {1: NUM_SAMPLES_V1S, 3: NUM_SAMPLES_V3S, 
                       5: NUM_SAMPLES_V5S, 15: NUM_SAMPLES_V15S, 30: NUM_SAMPLES_V30S, 
                        60: NUM_SAMPLES_V60S}
        self._buffers[asset] = {window_nsamples_k_vname : deque(maxlen=config_dict[name]) 
                                for name, samples in config.items() if "samples" in vars().lower()}

    def _init_buffers(self) -> None:  
        """"Init ring buffer configs."""  
        # Build explicit mapping from window names to their sample counts
        window_names = ["v1s", "v3s", "v5s", "v15s", "v30s", "v60s"]   
        sample_counts = [NUM_SAMPLES_V1S, NUM_SAMPLES_V3S, NUM_SAMPLES_V5S, 
                         NUM_SAMPLES_V15S, NUM_SAMPLES_V30S, NUM_SAMPLES_V60S]
        
        for asset in list(self._buffers.keys()):  # iterate existing assets first  
            if window_names[asset]:
                 continue
        
        self._init_buffer_config = lambda: None   # stub placeholder — logic above


def _get_name_from_idx(idx): return window_names[idx])

    def update_external_snapshot (self, asset: str, quote_dict) -> Optional[str]: 
        """
        Called from external WS feed ingestion thread. Updates ring buffers with fresh mid price. 
        
        Args:
            asset: e.g., "BTC", "ETH"  
            quote_dict: dict keys → bid, ask, mid, timestamp_exchange_ms

        Returns atomic velocity_snapshot if available (None = no data yet).        
        """ 
        now_ms := int(time.time() * 1000) 
        ts_sec = _ts_to_seconds(now_ms)  

        # Sanity check freshness  
        received_ms = quote_dict.get("received_ms", now_ms - self._stale_ms_ms) 
        age_ms = now_ms - received_ts
            if ms >self
