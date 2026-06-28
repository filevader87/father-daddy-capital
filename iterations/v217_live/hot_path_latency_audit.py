#!/usr/bin/env python3
"""
V21.7.19 — Hot-Path Latency Audit
====================================
Measures execution latency at each stage of the FDC hot path:
feed_receive → cache_update → signal → order_object → signing → submit → ack

Classification: DIAGNOSTIC
Does NOT modify live execution. Measurement only.
"""

import json, time, logging, sys, os, platform, subprocess
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

OUT_DIR = Path("/home/naq1987s/father-daddy-capital/output/v21719_execution_edges")
OUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('hot_path_latency')


def measure_logging_overhead(iterations=1000):
    """Measure logging overhead per call."""
    import io
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    test_log = logging.getLogger('latency_test')
    test_log.addHandler(handler)
    test_log.setLevel(logging.DEBUG)
    
    start = time.perf_counter()
    for _ in range(iterations):
        test_log.info("latency test message")
    elapsed = time.perf_counter() - start
    
    test_log.removeHandler(handler)
    return elapsed / iterations * 1000  # ms per call


def measure_json_overhead(iterations=1000):
    """Measure JSON serialization overhead per call."""
    test_obj = {
        'timestamp': int(time.time() * 1000),
        'asset': 'BTC', 'interval': '15m', 'side': 'DOWN',
        'token_id': '1234567890abcdef',
        'mid_price': 0.0523, 'spread': 0.01,
        'best_bid': 0.05, 'best_ask': 0.06,
        'velocity': 0.00123, 'imbalance': 0.45,
    }
    
    start = time.perf_counter()
    for _ in range(iterations):
        json.dumps(test_obj)
    elapsed = time.perf_counter() - start
    return elapsed / iterations * 1000


def measure_dict_lookup_overhead(iterations=100000):
    """Measure dict lookup overhead."""
    d = {f"key_{i}": i for i in range(1000)}
    keys = list(d.keys())
    
    start = time.perf_counter()
    for i in range(iterations):
        _ = d[keys[i % len(keys)]]
    elapsed = time.perf_counter() - start
    return elapsed / iterations * 1000


def measure_numpy_overhead(iterations=1000):
    """Measure numpy array creation overhead."""
    start = time.perf_counter()
    for _ in range(iterations):
        arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        _ = np.mean(arr)
    elapsed = time.perf_counter() - start
    return elapsed / iterations * 1000


def measure_network_latency():
    """Measure network round-trip to Polymarket CLOB."""
    import urllib.request
    
    latencies = []
    for _ in range(3):
        start = time.perf_counter()
        try:
            req = urllib.request.Request(
                "https://clob.polymarket.com/time",
                headers={'User-Agent': 'FDC/21.7.19'}
            )
            resp = urllib.request.urlopen(req, timeout=5)
            resp.read()
            latencies.append((time.perf_counter() - start) * 1000)
        except Exception:
            latencies.append(-1)
    
    return latencies


def measure_gamma_latency():
    """Measure Gamma API latency."""
    import urllib.request
    
    latencies = []
    for _ in range(3):
        start = time.perf_counter()
        try:
            req = urllib.request.Request(
                "https://gamma-api.polymarket.com/markets?limit=1",
                headers={'User-Agent': 'FDC/21.7.19'}
            )
            resp = urllib.request.urlopen(req, timeout=5)
            resp.read()
            latencies.append((time.perf_counter() - start) * 1000)
        except Exception:
            latencies.append(-1)
    
    return latencies


def measure_order_build_latency(iterations=100):
    """Measure order object construction overhead."""
    # Simulate OrderSpec construction
    from dataclasses import dataclass
    
    @dataclass
    class FakeOrderSpec:
        token_id: str
        side: str
        price: float
        size: float
        tick_size: str = "0.01"
    
    start = time.perf_counter()
    for i in range(iterations):
        spec = FakeOrderSpec(
            token_id=f"token_{i}",
            side="BUY",
            price=0.05 + i * 0.001,
            size=1.0,
        )
        _ = spec.token_id, spec.side, spec.price, spec.size
    elapsed = time.perf_counter() - start
    return elapsed / iterations * 1000


def audit_hot_path():
    log.info("Hot-Path Latency Audit starting — MEASUREMENT ONLY")
    
    # System info
    sys_info = {
        'platform': platform.platform(),
        'python_version': platform.python_version(),
        'cpu_count': os.cpu_count(),
        'load_avg': os.getloadavg() if hasattr(os, 'getloadavg') else None,
    }
    
    # Measure each stage
    measurements = {}
    
    log.info("Measuring logging overhead...")
    measurements['logging_overhead_ms'] = round(measure_logging_overhead(), 4)
    
    log.info("Measuring JSON serialization overhead...")
    measurements['json_serialization_overhead_ms'] = round(measure_json_overhead(), 4)
    
    log.info("Measuring dict lookup overhead...")
    measurements['dict_lookup_overhead_ms'] = round(measure_dict_lookup_overhead(), 6)
    
    log.info("Measuring numpy overhead...")
    measurements['numpy_overhead_ms'] = round(measure_numpy_overhead(), 4)
    
    log.info("Measuring order build latency...")
    measurements['order_object_build_ms'] = round(measure_order_build_latency(), 4)
    
    log.info("Measuring CLOB network latency...")
    clob_latencies = measure_network_latency()
    measurements['clob_round_trip_ms'] = [round(l, 2) for l in clob_latencies if l > 0]
    measurements['clob_median_ms'] = round(float(np.median(measurements['clob_round_trip_ms'])), 2) if measurements['clob_round_trip_ms'] else -1
    
    log.info("Measuring Gamma API latency...")
    gamma_latencies = measure_gamma_latency()
    measurements['gamma_round_trip_ms'] = [round(l, 2) for l in gamma_latencies if l > 0]
    measurements['gamma_median_ms'] = round(float(np.median(measurements['gamma_round_trip_ms'])), 2) if measurements['gamma_round_trip_ms'] else -1
    
    # Estimated hot-path total
    # feed_receive → cache_update: ~CLOB latency
    # cache_update → signal: ~numpy + dict lookup
    # signal → order_object: ~order build
    # order_signing: estimate (not measured without wallet)
    # order_submit → ack: ~CLOB latency
    
    feed_to_cache = measurements['clob_median_ms'] if measurements['clob_median_ms'] > 0 else 100
    cache_to_signal = measurements['numpy_overhead_ms'] + measurements['dict_lookup_overhead_ms']
    signal_to_order = measurements['order_object_build_ms']
    order_signing_est = 5.0  # Estimated: EIP-712 signing ~5ms
    order_submit = measurements['clob_median_ms'] if measurements['clob_median_ms'] > 0 else 100
    order_ack = 50.0  # Estimated: CLOB ack ~50ms
    
    total_decision_path = feed_to_cache + cache_to_signal + signal_to_order + order_signing_est + order_submit + order_ack
    
    measurements['estimated_hot_path'] = {
        'feed_receive_to_cache_update_ms': round(feed_to_cache, 2),
        'cache_update_to_signal_ms': round(cache_to_signal, 4),
        'signal_to_order_object_ms': round(signal_to_order, 4),
        'order_signing_est_ms': order_signing_est,
        'order_submit_ms': round(order_submit, 2),
        'order_ack_est_ms': order_ack,
        'total_decision_path_ms': round(total_decision_path, 2),
    }
    
    # Recommendations
    recommendations = []
    if measurements['logging_overhead_ms'] > 0.5:
        recommendations.append("REDUCE_LOGGING: Logging overhead >0.5ms. Consider minimal hot-path logging.")
    if measurements['json_serialization_overhead_ms'] > 0.1:
        recommendations.append("OPTIMIZE_JSON: JSON serialization >0.1ms. Pre-serialize where possible.")
    if measurements['clob_median_ms'] > 200:
        recommendations.append("SEPARATE_FEED: CLOB latency >200ms. Consider separate feed process.")
    if measurements['clob_median_ms'] > 500:
        recommendations.append("SEPARATE_ORDER: CLOB latency >500ms. Separate order process recommended.")
    if total_decision_path > 300:
        recommendations.append("CPU_PINNING: Total decision path >300ms. Consider CPU pinning for hot path.")
    if not recommendations:
        recommendations.append("NO_IMMEDIATE_ACTION: Latency within acceptable bounds for $1 canary.")
    
    # Report
    report = {
        'classification': 'HOT_PATH_LATENCY_AUDIT_COMPLETE',
        'version': 'V21.7.19',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'system': sys_info,
        'measurements': measurements,
        'recommendations': recommendations,
        'live_gates_unchanged': True,
    }
    
    with open(OUT_DIR / 'hot_path_latency_report.json', 'w') as f:
        json.dump(report, f, indent=2, default=str)
    
    log.info(f"Hot-path total: {total_decision_path:.1f}ms")
    log.info(f"Recommendations: {recommendations}")
    return report


if __name__ == '__main__':
    audit_hot_path()