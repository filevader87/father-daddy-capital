#!/usr/bin/env python3
"""
V21.7.26 — Persistent CLOB Client
===================================
Connection-pooled HTTP client for Gamma API + CLOB book fetches.
Reuses TCP/TLS connections to eliminate per-request handshake overhead.

Classification: V21.7.26_PERSISTENT_CLOB_CLIENT
"""

import json
import logging
import time
from typing import Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib3
from urllib3.util.retry import Retry
from urllib3.util.timeout import Timeout

sys_path_fix = True  # marker

log = logging.getLogger('persistent_clob_client')

# ─── Configuration ───
GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

POOL_CONFIG = {
    "max_connections": 16,
    "connection_pool_per_host": 8,
    "timeout_connect": 0.5,
    "timeout_read": 1.0,
    "retry_count": 1,
}

# ─── Singleton Pool ───
_pool: Optional[urllib3.PoolManager] = None
_pool_stats = {
    "requests_total": 0,
    "requests_success": 0,
    "requests_failed": 0,
    "pool_created_ts": None,
}


def get_pool() -> urllib3.PoolManager:
    """Get or create the persistent connection pool."""
    global _pool
    if _pool is None:
        retry = Retry(
            total=POOL_CONFIG["retry_count"],
            backoff_factor=0.1,
            status_forcelist=[502, 503, 504],
        )
        timeout = Timeout(
            connect=POOL_CONFIG["timeout_connect"],
            read=POOL_CONFIG["timeout_read"],
        )
        _pool = urllib3.PoolManager(
            num_pools=4,  # gamma, clob, + 2 spare
            maxsize=POOL_CONFIG["connection_pool_per_host"],
            retries=retry,
            timeout=timeout,
            headers={"User-Agent": "FDC/21.7.26", "Accept": "application/json"},
            block=False,
        )
        _pool_stats["pool_created_ts"] = time.time()
        log.info(f"Persistent pool created: maxsize={POOL_CONFIG['connection_pool_per_host']}")
    return _pool


def http_get_persistent(url: str, timeout: float = 2.0) -> Optional[dict]:
    """Fast HTTP GET using persistent connection pool."""
    _pool_stats["requests_total"] += 1
    pool = get_pool()
    try:
        resp = pool.request("GET", url, timeout=timeout)
        if resp.status == 200:
            _pool_stats["requests_success"] += 1
            return json.loads(resp.data.decode())
        else:
            _pool_stats["requests_failed"] += 1
            log.debug(f"HTTP {resp.status} for {url[:80]}")
            return None
    except (urllib3.exceptions.TimeoutError,
            urllib3.exceptions.MaxRetryError,
            urllib3.exceptions.ProtocolError,
            json.JSONDecodeError,
            OSError) as e:
        _pool_stats["requests_failed"] += 1
        log.debug(f"HTTP error for {url[:60]}: {type(e).__name__}")
        return None


def get_pool_stats() -> dict:
    """Return pool statistics."""
    return {
        "pool_active": _pool is not None,
        "requests_total": _pool_stats["requests_total"],
        "requests_success": _pool_stats["requests_success"],
        "requests_failed": _pool_stats["requests_failed"],
        "success_rate": (
            _pool_stats["requests_success"] / max(_pool_stats["requests_total"], 1)
        ),
        "pool_created_ts": _pool_stats.get("pool_created_ts"),
        "pool_uptime_s": (
            time.time() - _pool_stats["pool_created_ts"]
            if _pool_stats.get("pool_created_ts") else 0
        ),
        "config": POOL_CONFIG,
    }


def close_pool():
    """Close the persistent pool (for clean shutdown)."""
    global _pool
    if _pool:
        _pool.clear()
        _pool = None
        log.info("Persistent pool closed")


# ─── Batch Book Fetch ───

def fetch_books_batch(token_ids: List[str], sides: Optional[List[str]] = None,
                      max_workers: int = 8) -> Dict[str, dict]:
    """Fetch order books for multiple tokens concurrently using persistent pool.
    
    Args:
        token_ids: List of CLOB token IDs
        sides: Optional list of sides (UP/DOWN) parallel to token_ids
        max_workers: Thread pool size
    
    Returns:
        Dict mapping token_id -> raw book data
    """
    if sides is None:
        sides = ["UNKNOWN"] * len(token_ids)
    
    results = {}
    
    def fetch_one(args):
        tid, side = args
        url = f"{CLOB_HOST}/book?token_id={tid}"
        return tid, side, http_get_persistent(url, timeout=2.0)
    
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_one, (tid, side)): tid
                   for tid, side in zip(token_ids, sides)}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                _, side, data = future.result()
                if data:
                    results[tid] = data
            except Exception:
                pass
    
    return results


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from book_normalizer import normalize_for_entry
    
    # Quick test
    print("V21.7.26 Persistent CLOB Client")
    print(f"Config: {POOL_CONFIG}")
    
    # Test pool creation
    pool = get_pool()
    print(f"Pool created: {type(pool).__name__}")
    
    # Test concurrent book fetch
    test_tokens = [
        ("6929245624406702636726887010843459457114", "DOWN"),
        ("2174267920344460808664920286419983965041", "UP"),
    ]
    
    start = time.time()
    books = fetch_books_batch(
        [t[0] for t in test_tokens],
        [t[1] for t in test_tokens],
        max_workers=4,
    )
    elapsed = (time.time() - start) * 1000
    
    print(f"\nFetched {len(books)} books in {elapsed:.0f}ms")
    for tid, book in books.items():
        norm = normalize_for_entry(book, token_id=tid, side="DOWN")
        print(f"  {tid[:20]}... ask={norm.get('best_ask')} bid={norm.get('best_bid')} "
              f"valid={norm.get('is_valid')} src={norm.get('price_source')}")
    
    stats = get_pool_stats()
    print(f"\nPool stats: {stats['requests_total']} req, {stats['success_rate']:.1%} success, "
          f"{stats['requests_failed']} failed")
    
    close_pool()