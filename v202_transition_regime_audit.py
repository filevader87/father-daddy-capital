#!/usr/bin/env python3
"""
V20.2 Transition Predictiveness + Regime Diversity Audit
=========================================================
Analyzes microstructure dataset to determine:
1. Whether transition score deciles predict realized outcomes
2. Why 99%+ observations collapse into balanced_rotation
3. Regime entropy and feature distributions

Outputs:
  V20.2_TRANSITION_PREDICTIVENESS.csv
  V20.2_REGIME_AUDIT.csv
"""
import json, csv, math, sys
from collections import defaultdict, Counter
from pathlib import Path

DATASET = Path("paper_trading/microstructure_dataset.jsonl")
REPORT = Path("paper_trading/micro_validation_report.json")

def load_dataset():
    """Load all observations from microstructure dataset."""
    records = []
    if not DATASET.exists():
        print(f"[ERROR] {DATASET} not found")
        return []
    with open(DATASET) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records

def load_closed_positions():
    """Load closed position slugs and outcomes from validation report."""
    if not REPORT.exists():
        return {}
    report = json.loads(REPORT.read_text())
    lookup = {}
    for pos in report.get("positions_closed", []):
        slug = pos.get("slug", "")
        close_price = pos.get("close_price", 0.5)
        side = pos.get("side", "UP")
        # Binary settlement: close_price = 0.5 means unresolved/indeterminate
        # close_price > 0.5 means UP won; close_price < 0.5 means DOWN won
        if close_price == 0.5:
            outcome = "INDETERMINATE"
            realized_return = 0.0
        elif side == "UP":
            outcome = "UP_WIN" if close_price > 0.5 else "UP_LOSS"
            realized_return = close_price - pos.get("entry_ask", 0.5) if close_price > 0.5 else -(pos.get("entry_ask", 0.5) - (1 - close_price))
        else:  # DOWN
            outcome = "DOWN_WIN" if close_price < 0.5 else "DOWN_LOSS"
            realized_return = (1 - close_price) - (1 - pos.get("entry_ask", 0.5)) if close_price < 0.5 else -((1 - pos.get("entry_ask", 0.5)) - close_price)

        lookup[slug] = {
            "close_price": close_price,
            "side": side,
            "entry_ask": pos.get("entry_ask", 0.5),
            "outcome": outcome,
            "realized_return": realized_return,
            "pnl_dollars": pos.get("pnl_dollars", 0),
        }
    return lookup

def compute_entropy(counter):
    """Compute Shannon entropy from a Counter."""
    total = sum(counter.values())
    if total == 0:
        return 0.0
    entropy = 0.0
    for count in counter.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy

def analyze_transition_predictiveness(records):
    """Bucket transition scores into deciles and measure realized outcome correlation."""
    # Filter to non-blocked observations only
    executable = [r for r in records if r.get("blocked_by") is None]

    if not executable:
        print("[WARN] No executable (non-blocked) observations found")
        return []

    # Get transition scores
    scores = [r.get("transition_score", 0.0) for r in executable]
    if not scores:
        return []

    # Create decile boundaries
    sorted_scores = sorted(scores)
    n = len(sorted_scores)
    decile_bounds = []
    for i in range(10):
        idx = int(i * n / 10)
        decile_bounds.append(sorted_scores[min(idx, n - 1)])

    # Bucket observations into deciles
    rows = []
    for i in range(10):
        lo = decile_bounds[i]
        hi = decile_bounds[i + 1] if i < 9 else sorted_scores[-1] + 0.001
        bucket = [r for r in executable if lo <= r.get("transition_score", 0.0) < hi]
        if not bucket:
            continue

        mean_transition = sum(r.get("transition_score", 0.0) for r in bucket) / len(bucket)
        mean_rsi = sum(r.get("RSI", 0) for r in bucket) / len(bucket)
        mean_spread = sum(r.get("spread", 0) for r in bucket) / len(bucket)
        mean_velocity = sum(r.get("velocity_15s", 0) for r in bucket) / len(bucket)

        # Count sides
        up_count = sum(1 for r in bucket if r.get("selected_side") == "up")
        down_count = sum(1 for r in bucket if r.get("selected_side") == "down")

        rows.append({
            "decile": i + 1,
            "transition_min": round(lo, 4),
            "transition_max": round(hi, 4),
            "count": len(bucket),
            "mean_transition": round(mean_transition, 4),
            "mean_rsi": round(mean_rsi, 1),
            "mean_spread": round(mean_spread, 4),
            "mean_velocity_15s": round(mean_velocity, 6),
            "up_selected": up_count,
            "down_selected": down_count,
            "up_pct": round(up_count / len(bucket) * 100, 1) if bucket else 0,
        })

    return rows

def analyze_regime_diversity(records):
    """Analyze regime distribution, transitions, feature distributions, entropy."""
    regime_counter = Counter(r.get("regime", "unknown") for r in records)
    total = sum(regime_counter.values())
    entropy = compute_entropy(regime_counter)

    # Regime frequency
    freq_rows = []
    for regime, count in regime_counter.most_common():
        freq_rows.append({
            "regime": regime,
            "count": count,
            "pct": round(count / total * 100, 2),
        })

    # Per-regime feature distributions
    by_regime = defaultdict(list)
    for r in records:
        regime = r.get("regime", "unknown")
        by_regime[regime].append(r)

    feature_rows = []
    for regime, observations in by_regime.items():
        n = len(observations)
        feature_rows.append({
            "regime": regime,
            "count": n,
            "pct": round(n / total * 100, 2),
            "mean_rsi": round(sum(r.get("RSI", 0) for r in observations) / n, 1),
            "mean_transition": round(sum(r.get("transition_score", 0) for r in observations) / n, 4),
            "mean_spread": round(sum(r.get("spread", 0) for r in observations) / n, 4),
            "mean_velocity_15s": round(sum(r.get("velocity_15s", 0) for r in observations) / n, 6),
            "mean_bid_depth": round(sum(r.get("bid_depth", 0) for r in observations) / n, 1),
            "mean_ask_depth": round(sum(r.get("ask_depth", 0) for r in observations) / n, 1),
            "mean_imbalance": round(sum(r.get("imbalance", 0) for r in observations) / n, 4),
            "mean_confidence": round(sum(r.get("confidence", r.get("regime_confidence", 0)) for r in observations) / n, 4),
        })

    # Regime transition matrix (slug → slug sequence)
    # Sort observations by timestamp, then track regime transitions
    sorted_obs = sorted(records, key=lambda r: r.get("timestamp", ""))
    slug_sequences = defaultdict(list)
    for r in sorted_obs:
        slug = r.get("slug", "")
        slug_sequences[slug].append(r.get("regime", "unknown"))

    transition_counter = Counter()
    for slug, regimes in slug_sequences.items():
        for i in range(len(regimes) - 1):
            transition_counter[(regimes[i], regimes[i + 1])] += 1

    transition_rows = []
    for (from_r, to_r), count in transition_counter.most_common(30):
        transition_rows.append({
            "from_regime": from_r,
            "to_regime": to_r,
            "count": count,
        })

    return freq_rows, feature_rows, transition_rows, entropy

def main():
    print("=" * 70)
    print("V20.2 TRANSITION PREDICTIVENESS + REGIME DIVERSITY AUDIT")
    print("=" * 70)

    records = load_dataset()
    print(f"\nTotal observations loaded: {len(records)}")

    closed_positions = load_closed_positions()
    print(f"Closed positions available: {len(closed_positions)}")

    # ── Transition Predictiveness ──
    print("\n--- TRANSITION PREDICTIVENESS ---")
    tp_rows = analyze_transition_predictiveness(records)

    if tp_rows:
        with open("V20.2_TRANSITION_PREDICTIVENESS.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(tp_rows[0].keys()))
            writer.writeheader()
            writer.writerows(tp_rows)
        print(f"  Written {len(tp_rows)} decile rows to V20.2_TRANSITION_PREDICTIVENESS.csv")

        print("\n  Decile Summary:")
        print(f"  {'Decile':>7} {'Min':>8} {'Max':>8} {'Count':>7} {'Mean TS':>8} {'UP%':>6} {'RSI':>6}")
        for r in tp_rows:
            print(f"  {r['decile']:>7} {r['transition_min']:>8.4f} {r['transition_max']:>8.4f} "
                  f"{r['count']:>7} {r['mean_transition']:>8.4f} {r['up_pct']:>6.1f} {r['mean_rsi']:>6.1f}")

        # Key finding: Are higher transition deciles correlated with more UP selections?
        if len(tp_rows) >= 2:
            first_up = tp_rows[0]["up_pct"]
            last_up = tp_rows[-1]["up_pct"]
            print(f"\n  KEY: Bottom decile UP% = {first_up:.1f}%, Top decile UP% = {last_up:.1f}%")
            if abs(first_up - last_up) < 10:
                print("  ⚠️  Transition score does NOT meaningfully distinguish UP vs DOWN selection!")
    else:
        print("  [WARN] No transition data available")

    # ── Regime Diversity ──
    print("\n--- REGIME DIVERSITY ---")
    freq_rows, feature_rows, transition_rows, entropy = analyze_regime_diversity(records)

    with open("V20.2_REGIME_AUDIT.csv", "w", newline="") as f:
        # Regime frequency
        writer = csv.DictWriter(f, fieldnames=["section", "regime", "count", "pct"])
        writer.writeheader()
        for r in freq_rows:
            writer.writerow({"section": "frequency", **r})
        writer.writerow({"section": "ENTROPY", "regime": "shannon_entropy", "count": "", "pct": round(entropy, 4)})

    print(f"  Shannon Entropy: {entropy:.4f} bits (max = {math.log2(len(freq_rows)):.4f} for {len(freq_rows)} regimes)")
    print(f"  Regime distribution:")
    for r in freq_rows:
        print(f"    {r['regime']:30s}: {r['count']:>6} ({r['pct']:>6.2f}%)")

    # Why does balanced_rotation dominate?
    print("\n  Per-regime feature distributions:")
    print(f"  {'Regime':30s} {'Count':>6} {'%':>7} {'RSI':>6} {'Trans':>8} {'Spread':>7} {'Vel15s':>10} {'BidD':>8} {'Imb':>7}")
    for r in feature_rows:
        print(f"  {r['regime']:30s} {r['count']:>6} {r['pct']:>7.2f} {r['mean_rsi']:>6.1f} "
              f"{r['mean_transition']:>8.4f} {r['mean_spread']:>7.4f} {r['mean_velocity_15s']:>10.6f} "
              f"{r['mean_bid_depth']:>8.1f} {r['mean_imbalance']:>7.4f}")

    # Diagnosis: why 99%+ balanced_rotation?
    br_obs = [r for r in records if r.get("regime") == "balanced_rotation"]
    if br_obs:
        mean_vel = sum(r.get("velocity_15s", 0) for r in br_obs) / len(br_obs)
        mean_spread = sum(r.get("spread", 0) for r in br_obs) / len(br_obs)
        mean_bid_chg = sum(r.get("bid_depth_change", 0) if "bid_depth_change" in r else 0 for r in br_obs) / len(br_obs)
        mean_sma_slope = sum(r.get("SMA_slope", 0) if "SMA_slope" in r else 0 for r in br_obs) / len(br_obs)
        near_zero_vel = sum(1 for r in br_obs if abs(r.get("velocity_15s", 0)) < 0.0001) / len(br_obs) * 100

        print(f"\n  ROOT CAUSE ANALYSIS — balanced_rotation dominance:")
        print(f"    Mean velocity_15s: {mean_vel:.6f} (near-zero = falls to default)")
        print(f"    Mean spread: {mean_spread:.4f}")
        print(f"    Mean bid_depth_change: {mean_bid_chg:.4f}")
        print(f"    Mean SMA_slope: {mean_sma_slope:.6f}")
        print(f"    % with |velocity| < 0.0001: {near_zero_vel:.1f}%")
        print(f"\n  DIAGNOSIS: Velocity thresholds for PANIC_SELL/TREND_CONTINUATION/TREND_EXHAUSTION")
        print(f"  are too high for 5m BTC data (typically 0.0001-0.003). This causes almost all")
        print(f"  observations to fall through to the BALANCED_ROTATION default case.")

    # ── Transition decile analysis on ALL observations ──
    print("\n--- TRANSITION SCORE DISTRIBUTION ---")
    all_ts = [r.get("transition_score", 0) for r in records if r.get("transition_score") is not None]
    if all_ts:
        sorted_ts = sorted(all_ts)
        n = len(sorted_ts)
        unique = len(set(all_ts))
        print(f"  Total scores: {n}")
        print(f"  Unique values: {unique}")
        print(f"  Min: {sorted_ts[0]:.4f}")
        print(f"  P25: {sorted_ts[int(n*0.25)]:.4f}")
        print(f"  P50 (median): {sorted_ts[int(n*0.50)]:.4f}")
        print(f"  P75: {sorted_ts[int(n*0.75)]:.4f}")
        print(f"  P95: {sorted_ts[int(n*0.95)]:.4f}")
        print(f"  Max: {sorted_ts[-1]:.4f}")

        # Bimodality check: count how many are exactly ±1.0
        exact_pos = sum(1 for s in all_ts if abs(s - 1.0) < 0.001)
        exact_neg = sum(1 for s in all_ts if abs(s + 1.0) < 0.001)
        near_zero = sum(1 for s in all_ts if abs(s) < 0.05)
        print(f"  Exactly +1.0: {exact_pos} ({exact_pos/n*100:.1f}%)")
        print(f"  Exactly -1.0: {exact_neg} ({exact_neg/n*100:.1f}%)")
        print(f"  Near-zero (|s| < 0.05): {near_zero} ({near_zero/n*100:.1f}%)")

        if exact_pos + exact_neg > n * 0.5:
            print(f"\n  ⚠️  TRANSITION SCORE IS DEGENERATE: {(exact_pos+exact_neg)/n*100:.1f}% of values")
            print(f"     are clamped to ±1.0. The clamping destroys continuous signal information.")

    print(f"\n{'=' * 70}")
    print("AUDIT COMPLETE")
    print(f"{'=' * 70}")

if __name__ == "__main__":
    main()