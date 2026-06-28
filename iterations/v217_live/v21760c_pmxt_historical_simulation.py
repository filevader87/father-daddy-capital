#!/usr/bin/env python3
"""V21.7.60c PMXT Historical Trade Simulation — replay all bots on historical data.
Simulates BTC 5m, BTC 15m, ETH 5m, SOL 5m, XRP 5m, and Weather across historical 1s observer data.
"""
import json, os, math
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, Counter

BASE = Path(__file__).resolve().parent.parent.parent
OUT = BASE / "output/v21760_out_of_sample_and_pmxt"
OUT.mkdir(parents=True, exist_ok=True)

QUOTE_FILE = BASE / "output/v21751_persistent_1s_observer/quote_state_1s.jsonl"

def safe_f(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

def write_json(path, data):
    with open(str(path), "w") as f: json.dump(data, f, indent=2, default=str)

def write_jsonl(path, data):
    with open(str(path), "w") as f:
        for r in data:
            f.write(json.dumps(r, default=str) + "\n")

NOW = datetime.now(timezone.utc).isoformat()

# ── Strategies to simulate ────────────────────────────────────────────
# Each strategy: (name, asset_filter, interval_filter, side_filter, entry_bucket, scalp_threshold)
STRATEGIES = [
    ("BTC_5m_30_60c_3c_scalp", "BTC", "5m", None, (0.30, 0.60), 0.03),
    ("BTC_5m_30_60c_2c_scalp", "BTC", "5m", None, (0.30, 0.60), 0.02),
    ("BTC_5m_30_60c_1c_scalp", "BTC", "5m", None, (0.30, 0.60), 0.01),
    ("BTC_5m_12_30c_3c_scalp", "BTC", "5m", None, (0.12, 0.30), 0.03),
    ("BTC_5m_60_85c_3c_scalp", "BTC", "5m", None, (0.60, 0.85), 0.03),
    ("ETH_5m_30_60c_3c_scalp", "ETH", "5m", None, (0.30, 0.60), 0.03),
    ("SOL_5m_30_60c_3c_scalp", "SOL", "5m", None, (0.30, 0.60), 0.03),
    ("XRP_5m_30_60c_3c_scalp", "XRP", "5m", None, (0.30, 0.60), 0.03),
    ("XRP_5m_DOWN_30_60c_3c_scalp", "XRP", "5m", "DOWN", (0.30, 0.60), 0.03),
    ("BTC_15m_30_60c_3c_scalp", "BTC", "15m", None, (0.30, 0.60), 0.03),
    ("BTC_15m_12_30c_3c_scalp", "BTC", "15m", None, (0.12, 0.30), 0.03),
    ("ETH_15m_30_60c_3c_scalp", "ETH", "15m", None, (0.30, 0.60), 0.03),
    ("ALL_5m_30_60c_2c_scalp", None, "5m", None, (0.30, 0.60), 0.02),
    ("ALL_5m_30_60c_1c_scalp", None, "5m", None, (0.30, 0.60), 0.01),
    ("ALL_5m_8_30c_3c_scalp", None, "5m", None, (0.08, 0.30), 0.03),
]

TIME_STOP_TTE = 15
SIZE_USD = 5.0

print("=" * 70)
print("V21.7.60c PMXT HISTORICAL TRADE SIMULATION — ALL BOTS")
print("PAPER ONLY — NO LIVE ORDERS — NO WALLET SPEND")
print("=" * 70)

# ── Run simulation for each strategy ──────────────────────────────────
all_results = {}
all_trades = {}

for strat_name, asset_f, interval_f, side_f, bucket, scalp_thresh in STRATEGIES:
    bucket_min, bucket_max = bucket
    positions = {}  # market_slug_side -> position
    settled = []
    
    count = 0
    with open(str(QUOTE_FILE)) as f:
        for line in f:
            d = json.loads(line)
            if not d.get("is_current_window") or not d.get("active"):
                continue
            bid = safe_f(d.get("best_bid"))
            ask = safe_f(d.get("best_ask"))
            if bid <= 0 or ask <= 0: continue
            
            asset = d.get("asset", "?")
            side = d.get("side", "?")
            slug = d.get("market_slug", "")
            interval = "15m" if "15m" in slug else "5m"
            tte = safe_f(d.get("time_to_expiry_seconds"))
            spread = safe_f(d.get("spread"))
            bid_depth_raw = d.get("bid_depth_top5", 0)
            if isinstance(bid_depth_raw, list):
                bid_depth = sum(safe_f(row[1]) for row in bid_depth_raw if isinstance(row, (list, tuple)) and len(row) >= 2)
            else:
                bid_depth = safe_f(bid_depth_raw)
            ts = d.get("timestamp", "")
            
            # Apply strategy filters
            if asset_f and asset != asset_f: continue
            if interval_f and interval != interval_f: continue
            if side_f and side != side_f: continue
            
            token_price = bid if side == "UP" else ask
            pos_key = f"{slug}_{side}"
            
            # Check open positions
            if pos_key in positions:
                pos = positions[pos_key]
                
                # Scalp exit
                if bid >= pos["entry_price"] + scalp_thresh:
                    pos["exit_price"] = bid
                    pos["exit_reason"] = "SCALP_EXIT"
                    pos["exit_timestamp"] = ts
                    contracts = safe_f(pos.get("contracts"))
                    pos["net_pnl"] = round((bid - pos["entry_price"]) * contracts, 4)
                    pos["status"] = "SETTLED"
                    settled.append(pos)
                    del positions[pos_key]
                    continue
                
                # Time stop
                if tte <= TIME_STOP_TTE:
                    pos["exit_price"] = bid
                    pos["exit_reason"] = "TIME_STOP"
                    pos["exit_timestamp"] = ts
                    contracts = safe_f(pos.get("contracts"))
                    pos["net_pnl"] = round((bid - pos["entry_price"]) * contracts, 4)
                    pos["status"] = "SETTLED"
                    settled.append(pos)
                    del positions[pos_key]
                    continue
                
                # Expiry
                if tte <= 0:
                    pos["exit_price"] = 0
                    pos["exit_reason"] = "EXPIRY"
                    pos["exit_timestamp"] = ts
                    pos["net_pnl"] = -SIZE_USD
                    pos["status"] = "SETTLED"
                    settled.append(pos)
                    del positions[pos_key]
                    continue
                
                if bid > safe_f(pos.get("max_bid", 0)):
                    pos["max_bid"] = bid
            
            # New entry
            elif bucket_min <= token_price <= bucket_max:
                if spread <= 0.05 and tte >= 30:
                    contracts = SIZE_USD / ask if ask > 0 else 0
                    positions[pos_key] = {
                        "strategy": strat_name, "asset": asset, "interval": interval,
                        "side": side, "market_slug": slug, "entry_timestamp": ts,
                        "entry_price": ask, "entry_bid": bid, "entry_spread": spread,
                        "size_usd": SIZE_USD, "contracts": round(contracts, 4),
                        "tte_at_entry": tte, "max_bid": bid, "status": "OPEN",
                        "real_order": False
                    }
    
    # Close remaining as expired
    for pos in positions.values():
        pos["exit_price"] = 0
        pos["exit_reason"] = "EXPIRY_UNRESOLVED"
        pos["net_pnl"] = -SIZE_USD
        pos["status"] = "OPEN_UNRESOLVED"
        settled.append(pos)
    
    # Compute metrics
    total_pos = len(settled)
    scalp_exits = sum(1 for p in settled if p.get("exit_reason") == "SCALP_EXIT")
    time_stops = sum(1 for p in settled if p.get("exit_reason") == "TIME_STOP")
    expiries = sum(1 for p in settled if "EXPIRY" in p.get("exit_reason", ""))
    
    pnls_list = [safe_f(p.get("net_pnl")) for p in settled if p.get("net_pnl") is not None]
    total_pnl = sum(pnls_list)
    wins = sum(1 for p in pnls_list if p > 0)
    gross_profit = sum(p for p in pnls_list if p > 0)
    gross_loss = abs(sum(p for p in pnls_list if p < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
    
    cumulative = 0; peak = 0; max_dd = 0
    for p in pnls_list:
        cumulative += p
        if cumulative > peak: peak = cumulative
        dd = peak - cumulative
        if dd > max_dd: max_dd = dd
    
    all_results[strat_name] = {
        "total_positions": total_pos, "scalp_exits": scalp_exits,
        "time_stops": time_stops, "expiries": expiries,
        "scalp_exit_rate": round(scalp_exits / total_pos, 4) if total_pos else 0,
        "total_pnl": round(total_pnl, 2), "PF": round(pf, 4),
        "max_DD": round(max_dd, 2), "win_rate": round(wins / len(pnls_list), 4) if pnls_list else 0,
        "positive": total_pnl > 0,
        "edge_survives": total_pnl > 0 and pf >= 1.0
    }
    all_trades[strat_name] = settled
    
    print(f"  {strat_name:<35} pos={total_pos:>4} scalp={scalp_exits:>3} pnl=${total_pnl:>8.2f} PF={pf:.3f} {'✓' if total_pnl > 0 and pf >= 1.0 else '✗'}")

# ── Write outputs ─────────────────────────────────────────────────────
# Write all trades
all_trades_flat = []
for strat, trades in all_trades.items():
    for t in trades:
        t["strategy"] = strat
        all_trades_flat.append(t)
write_jsonl(OUT / "pmxt_historical_simulation_trades.jsonl", all_trades_flat)

# Write summary report
positive_strategies = [k for k, v in all_results.items() if v["edge_survives"]]
report = {
    "module": "V21.7.60c",
    "timestamp": NOW,
    "real_orders_allowed": False,
    "live_authorization_suspended": True,
    "wallet_spend": 0,
    "strategies_tested": len(STRATEGIES),
    "strategy_results": all_results,
    "positive_edge_strategies": positive_strategies,
    "all_negative": len(positive_strategies) == 0,
    "best_strategy": max(all_results.items(), key=lambda x: x[1]["total_pnl"])[0] if all_results else "none",
    "worst_strategy": min(all_results.items(), key=lambda x: x[1]["total_pnl"])[0] if all_results else "none",
    "total_trades_simulated": len(all_trades_flat),
    "classification": "PMXT_HISTORICAL_SIMULATION_COMPLETE",
    "note": "All simulation. Zero real orders. Zero wallet spend."
}
write_json(OUT / "pmxt_historical_simulation_report.json", report)

print(f"\n{'='*70}")
print("PMXT HISTORICAL SIMULATION COMPLETE")
print(f"{'='*70}")
print(f"\nStrategies tested: {len(STRATEGIES)}")
print(f"Total trades simulated: {len(all_trades_flat)}")
print(f"Positive edge strategies: {positive_strategies if positive_strategies else 'NONE'}")
print(f"Best: {report['best_strategy']}")
print(f"Worst: {report['worst_strategy']}")
print(f"\nAll paper. Zero real orders. Zero wallet spend.")