#!/usr/bin/env python3
"""V21.7.60b Live Paper Trade Engine — shadow paper positions with real-time CLOB data.
NO LIVE ORDERS. NO WALLET SPEND. PAPER ONLY.
Uses 1s observer quote data to simulate entries with the bucket_30_60c filter.
"""
import json, os, time, math
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

BASE = Path(__file__).resolve().parent.parent.parent
OUT = BASE / "output/v21760_out_of_sample_and_pmxt"
OUT.mkdir(parents=True, exist_ok=True)

QUOTE_FILE = BASE / "output/v21751_persistent_1s_observer/quote_state_1s.jsonl"

def safe_f(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

def write_jsonl(path, data):
    with open(str(path), "a") as f:
        for r in data:
            f.write(json.dumps(r, default=str) + "\n")

def write_json(path, data):
    with open(str(path), "w") as f: json.dump(data, f, indent=2, default=str)

NOW = datetime.now(timezone.utc).isoformat()

# ── Paper trade configuration ─────────────────────────────────────────
# Apply the bucket_30_60c filter (best candidate from V21.7.59)
# Entry: buy at best_ask when token_price in [0.30, 0.60]
# Scalp exit: sell at best_bid when bid >= entry_price + 0.03
# Time stop: exit at TTE <= 15s at current best_bid
# Expiry: settle at 0 or 1

ENTRY_BUCKET_MIN = 0.30
ENTRY_BUCKET_MAX = 0.60
SCALP_THRESHOLD = 0.03
TIME_STOP_TTE = 15  # seconds
MAX_POSITIONS_PER_MARKET = 1
SIZE_USD = 5.0

# ── Replay 1s observer data to simulate paper trades ──────────────────
# Group quotes by market_slug + side to track position lifecycle
positions = {}  # key = market_slug_side -> position dict
settled_positions = []
all_events = []

print("=" * 60)
print("V21.7.60b LIVE PAPER TRADE ENGINE")
print("PAPER ONLY — NO LIVE ORDERS — NO WALLET SPEND")
print("=" * 60)
print(f"\nFilter: bucket_30_60c (token price 30-60¢)")
print(f"Scalp threshold: +3¢ | Time stop: TTE <= {TIME_STOP_TTE}s")
print(f"Size: ${SIZE_USD} per position\n")

count = 0
with open(str(QUOTE_FILE)) as f:
    for line in f:
        d = json.loads(line)
        if not d.get("is_current_window") or not d.get("active"):
            continue
        bid = safe_f(d.get("best_bid"))
        ask = safe_f(d.get("best_ask"))
        if bid <= 0 or ask <= 0:
            continue
        
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
        condition_id = d.get("condition_id")
        selected_token_id = d.get("selected_token_id")
        
        token_price = bid if side == "UP" else ask
        
        pos_key = f"{slug}_{side}"
        
        # Check if we have an open position for this market
        if pos_key in positions:
            pos = positions[pos_key]
            
            # Check scalp exit: bid >= entry + threshold
            if bid >= pos["entry_price"] + SCALP_THRESHOLD:
                pos["exit_price"] = bid
                pos["exit_reason"] = "SCALP_EXIT_3C"
                pos["exit_timestamp"] = ts
                pos["exit_tte"] = tte
                contracts = safe_f(pos.get("contracts"))
                pos["net_pnl"] = round((bid - pos["entry_price"]) * contracts, 4)
                pos["status"] = "PAPER_SETTLED"
                settled_positions.append(pos)
                all_events.append({**pos, "event": "SCALP_EXIT", "record_type": "SHADOW_PAPER_EXIT"})
                del positions[pos_key]
                continue
            
            # Check time stop: TTE <= 15s
            if tte <= TIME_STOP_TTE:
                pos["exit_price"] = bid if bid > 0 else 0
                pos["exit_reason"] = "TIME_STOP"
                pos["exit_timestamp"] = ts
                pos["exit_tte"] = tte
                contracts = safe_f(pos.get("contracts"))
                pos["net_pnl"] = round((bid - pos["entry_price"]) * contracts, 4)
                pos["status"] = "PAPER_SETTLED"
                settled_positions.append(pos)
                all_events.append({**pos, "event": "TIME_STOP", "record_type": "SHADOW_PAPER_EXIT"})
                del positions[pos_key]
                continue
            
            # Check expiry: TTE <= 0
            if tte <= 0:
                pos["exit_price"] = 0  # will be updated by settlement
                pos["exit_reason"] = "EXPIRY"
                pos["exit_timestamp"] = ts
                pos["status"] = "PAPER_EXPIRED"
                settled_positions.append(pos)
                all_events.append({**pos, "event": "EXPIRY", "record_type": "SHADOW_PAPER_SETTLEMENT"})
                del positions[pos_key]
                continue
            
            # Update max bid tracking
            if bid > safe_f(pos.get("max_bid_after_entry", 0)):
                pos["max_bid_after_entry"] = bid
            if bid < safe_f(pos.get("min_bid_after_entry", 999)):
                pos["min_bid_after_entry"] = bid
        
        # Check for new entry: token price in 30-60¢ bucket, no existing position
        elif ENTRY_BUCKET_MIN <= token_price <= ENTRY_BUCKET_MAX:
            if spread <= 0.05 and bid_depth >= 20 and tte >= 30:
                contracts = SIZE_USD / ask if ask > 0 else 0
                positions[pos_key] = {
                    "position_id": f"PAPER-{slug}-{side}-{int(time.time())}",
                    "record_type": "SHADOW_PAPER_POSITION",
                    "asset": asset, "interval": interval, "side": side,
                    "market_slug": slug, "condition_id": condition_id,
                    "selected_token_id": selected_token_id,
                    "entry_timestamp": ts, "entry_price": ask,
                    "entry_bid": bid, "entry_ask": ask, "entry_spread": spread,
                    "entry_quote_source": "PM_CLOB_READ",
                    "size_usd": SIZE_USD, "contracts": round(contracts, 4),
                    "time_to_expiry_at_entry": tte,
                    "max_bid_after_entry": bid, "min_bid_after_entry": bid,
                    "status": "PAPER_OPEN", "real_order": False, "wallet_spend": 0,
                    "entry_bucket": "30-60c"
                }
                all_events.append({**positions[pos_key], "event": "ENTRY", "record_type": "SHADOW_PAPER_POSITION"})

# Settle any remaining open positions as expired
for pos_key, pos in positions.items():
    pos["exit_price"] = 0
    pos["exit_reason"] = "EXPIRY_UNRESOLVED"
    pos["status"] = "PAPER_OPEN_UNRESOLVED"
    settled_positions.append(pos)

# ── Compute results ───────────────────────────────────────────────────
total_positions = len(settled_positions)
scalp_exits = sum(1 for p in settled_positions if p.get("exit_reason") == "SCALP_EXIT_3C")
time_stops = sum(1 for p in settled_positions if p.get("exit_reason") == "TIME_STOP")
expiries = sum(1 for p in settled_positions if "EXPIRY" in p.get("exit_reason", ""))
open_unresolved = sum(1 for p in settled_positions if p.get("status") == "PAPER_OPEN_UNRESOLVED")

pnls_list = [safe_f(p.get("net_pnl")) for p in settled_positions if p.get("net_pnl") is not None]
total_pnl = sum(pnls_list)
wins = sum(1 for p in pnls_list if p > 0)
losses = sum(1 for p in pnls_list if p < 0)
gross_profit = sum(p for p in pnls_list if p > 0)
gross_loss = abs(sum(p for p in pnls_list if p < 0))
pf = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0

# Max drawdown
cumulative = 0; peak = 0; max_dd = 0
for p in pnls_list:
    cumulative += p
    if cumulative > peak: peak = cumulative
    dd = peak - cumulative
    if dd > max_dd: max_dd = dd

# ── Write outputs ─────────────────────────────────────────────────────
write_jsonl(OUT / "live_paper_trades.jsonl", settled_positions)
write_jsonl(OUT / "live_paper_events.jsonl", all_events)

report = {
    "module": "V21.7.60b",
    "timestamp": NOW,
    "filter_applied": "bucket_30_60c",
    "real_orders_allowed": False,
    "live_authorization_suspended": True,
    "wallet_spend": 0,
    "total_paper_positions": total_positions,
    "scalp_exits": scalp_exits,
    "time_stops": time_stops,
    "expiries": expiries,
    "open_unresolved": open_unresolved,
    "wins": wins, "losses": losses,
    "total_pnl": round(total_pnl, 2),
    "PF": round(pf, 4),
    "max_DD": round(max_dd, 2),
    "scalp_exit_rate": round(scalp_exits / total_positions, 4) if total_positions else 0,
    "win_rate": round(wins / len(pnls_list), 4) if pnls_list else 0,
    "classification": "PAPER_TRADE_ENGINE_COMPLETE",
    "note": "All positions are paper. Zero wallet spend. Zero real orders."
}
write_json(OUT / "live_paper_trade_report.json", report)

print(f"Total paper positions: {total_positions}")
print(f"Scalp exits: {scalp_exits} | Time stops: {time_stops} | Expiries: {expiries} | Open: {open_unresolved}")
print(f"PnL: ${total_pnl:.2f} | PF: {pf:.4f} | Max DD: ${max_dd:.2f}")
print(f"Win rate: {wins}/{len(pnls_list)} ({wins/len(pnls_list)*100:.1f}%)" if pnls_list else "No trades")
print(f"\nAll paper. Zero real orders. Zero wallet spend.")
print(f"Output: live_paper_trades.jsonl, live_paper_trade_report.json")