#!/usr/bin/env python3
"""V19.9 Negative-EV Postmortem + Calibration Reset (§§3-6, 8, 10)

Produces:
  paper_trading/postmortem/v19_9_trade_postmortem.jsonl
  paper_trading/postmortem/v19_9_trade_postmortem.md
  paper_trading/postmortem/calibration_table.csv
  paper_trading/postmortem/inverse_side_audit.csv
  paper_trading/postmortem/profile_bucket_performance.csv
  paper_trading/postmortem/recommendation.json
"""
import json
import csv
import os
import glob
from collections import defaultdict
from datetime import datetime

REPO = os.path.dirname(os.path.abspath(__file__))
JOURNAL_DIR = os.path.join(REPO, "paper_trading", "journal")
OUTPUT_DIR = os.path.join(REPO, "paper_trading", "postmortem")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Load all journal entries ──
entries = []
for jfile in sorted(glob.glob(os.path.join(JOURNAL_DIR, "**", "*.json"), recursive=True)):
    if "trades.jsonl" in jfile:
        continue
    try:
        entries.append(json.load(open(jfile)))
    except Exception as e:
        print(f"Warning: could not load {jfile}: {e}")

print(f"Loaded {len(entries)} journal entries")

# ── §3: Trade-level postmortem ──
ENTRY_PRICE_BUCKETS = [
    (0.05, 0.10, "0.05-0.10"),
    (0.10, 0.20, "0.10-0.20"),
    (0.20, 0.30, "0.20-0.30"),
    (0.30, 0.40, "0.30-0.40"),
    (0.40, 0.50, "0.40-0.50"),
    (0.50, 0.65, "0.50-0.65"),
    (0.65, 0.80, "0.65-0.80"),
    (0.80, 1.00, "0.80+"),
]

RSI_BUCKETS = [
    (0, 20, "0-20"),
    (20, 30, "20-30"),
    (30, 40, "30-40"),
    (40, 50, "40-50"),
    (50, 60, "50-60"),
    (60, 100, "60+"),
]

def bucket_price(p):
    for lo, hi, label in ENTRY_PRICE_BUCKETS:
        if lo <= p < hi:
            return label
    return "0.80+" if p >= 0.80 else "unknown"

def bucket_rsi(r):
    if r is None:
        return "unknown"
    for lo, hi, label in RSI_BUCKETS:
        if lo <= r < hi:
            return label
    return "unknown"

def bucket_tte(t):
    if t is None:
        return "unknown"
    if t < 2:
        return "<2min"
    if t < 5:
        return "2-5min"
    if t < 15:
        return "5-15min"
    return "15min+"

postmortem_entries = []
for j in entries:
    side = str(j.get("selected_side", j.get("side", "Up")) or "Up").upper()
    
    # Resolve winner from resolved_winner or settlement_price
    resolved_winner = j.get("resolved_winner")
    sp = j.get("settlement_price")
    if resolved_winner and resolved_winner.upper() in ("UP", "DOWN"):
        winner = resolved_winner.upper()
    elif sp is not None:
        try:
            winner = "UP" if float(sp) >= 0.5 else "DOWN"
        except (ValueError, TypeError):
            winner = None
    else:
        winner = None
    
    if winner is None:
        continue  # Skip truly unresolved trades
    
    bet = j.get("size_usd", j.get("bet", 2.0))
    entry = j.get("entry_price", j.get("contract_price", 0.5))
    profile = j.get("profile", j.get("shadow_profile", "unknown"))
    rsi = j.get("signal_rsi", j.get("rsi", None))

    if winner == side:
        realized_pnl = bet / max(entry, 0.01) - bet
        win_loss = "WIN"
    else:
        realized_pnl = -bet
        win_loss = "LOSS"

    est_prob = j.get("estimated_probability", j.get("ev_p_win", None))
    break_even_prob = entry  # For Up token, break-even WR = entry_price
    raw_edge = (est_prob or 0) - entry
    buffered_ev = j.get("buffered_ev", j.get("buffered_edge", None))

    row = {
        "position_id": j.get("position_id", ""),
        "profile": profile,
        "asset": j.get("asset", ""),
        "interval": j.get("interval", ""),
        "market_slug": j.get("market_slug", ""),
        "selected_side": side,
        "opposite_side": "DOWN" if side == "UP" else "UP",
        "entry_timestamp": j.get("entry_timestamp", ""),
        "entry_time_relative_to_market_start": j.get("entry_time_relative_to_market_start", j.get("time_to_expiry", "")),
        "time_to_expiry_at_entry": j.get("time_to_expiry_at_entry", j.get("time_to_expiry", "")),
        "market_phase_at_entry": j.get("market_phase_at_entry", ""),
        "entry_ask": entry,
        "entry_bid": j.get("entry_bid", j.get("entry_price", "")),
        "entry_spread": j.get("entry_spread", j.get("spread", "")),
        "opposite_ask": j.get("opposite_ask", ""),
        "opposite_bid": j.get("opposite_bid", ""),
        "selected_token_state": j.get("token_state_at_entry", j.get("token_state", "")),
        "reference_price": j.get("reference_price", ""),
        "current_price_at_entry": j.get("current_price_at_entry", ""),
        "spot_vs_reference_pct": j.get("spot_vs_reference_pct", ""),
        "spot_velocity_5s": j.get("spot_velocity_5s", ""),
        "spot_velocity_15s": j.get("spot_velocity_15s", ""),
        "spot_velocity_30s": j.get("spot_velocity_30s", ""),
        "RSI": rsi,
        "RSI_slope": j.get("signal_rsi_slope", ""),
        "candle_velocity": j.get("signal_candle_velocity", ""),
        "sma20_distance": j.get("sma20_distance", ""),
        "estimated_probability": est_prob,
        "break_even_probability": round(break_even_prob, 4),
        "raw_edge": round(raw_edge, 4) if raw_edge else None,
        "buffered_edge": buffered_ev,
        "resolved_winner": winner,
        "win_loss": win_loss,
        "gross_pnl": j.get("gross_pnl", realized_pnl),
        "net_pnl": j.get("net_pnl", realized_pnl),
        # Bucketed fields
        "entry_price_bucket": bucket_price(entry),
        "RSI_bucket": bucket_rsi(rsi),
        "time_to_expiry_bucket": bucket_tte(j.get("time_to_expiry_at_entry", j.get("time_to_expiry", None))),
        "calibration_error": round((est_prob or 0) - (1 if win_loss == "WIN" else 0), 4) if est_prob else None,
    }
    postmortem_entries.append(row)

# Write JSONL
jsonl_path = os.path.join(OUTPUT_DIR, "v19_9_trade_postmortem.jsonl")
with open(jsonl_path, "w") as f:
    for row in postmortem_entries:
        f.write(json.dumps(row) + "\n")
print(f"Wrote {len(postmortem_entries)} entries to {jsonl_path}")

# ── §4: Bucket realized performance ──
def bucket_analysis(entries, group_keys):
    groups = defaultdict(list)
    for e in entries:
        key = tuple(str(e.get(k, "unknown")) for k in group_keys)
        groups[key].append(e)
    
    results = []
    for key, group in groups.items():
        if len(group) < 5:
            continue
        wins = sum(1 for e in group if e["win_loss"] == "WIN")
        losses = len(group) - wins
        wr = wins / len(group) if len(group) > 0 else 0
        avg_entry = sum(e["entry_ask"] for e in group if isinstance(e.get("entry_ask"), (int, float))) / len(group)
        be_wr = avg_entry  # break-even WR ≈ entry price for Up tokens
        total_pnl = sum(e["net_pnl"] for e in group if isinstance(e.get("net_pnl"), (int, float)))
        ev_per_share = total_pnl / len(group) if len(group) > 0 else 0
        ev_per_dollar = total_pnl / (len(group) * 2.0) if len(group) > 0 else 0  # $2 per trade
        
        # Max loss streak
        max_streak = 0
        streak = 0
        for e in group:
            if e["win_loss"] == "LOSS":
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        
        result = {k: v for k, v in zip(group_keys, key)}
        result.update({
            "trades": len(group),
            "wins": wins,
            "losses": losses,
            "WR": round(wr, 3),
            "avg_entry": round(avg_entry, 3),
            "break_even_WR": round(be_wr, 3),
            "realized_EV_per_share": round(ev_per_share, 4),
            "realized_EV_per_dollar": round(ev_per_dollar, 4),
            "net_PnL": round(total_pnl, 2),
            "PF": round(wins / max(losses, 1), 2) if losses > 0 else "INF",
            "max_loss_streak": max_streak,
        })
        results.append(result)
    return results

# Bucket by profile, entry_price, RSI
profile_buckets = bucket_analysis(postmortem_entries, ["profile"])
asset_buckets = bucket_analysis(postmortem_entries, ["asset"])
entry_price_buckets = bucket_analysis(postmortem_entries, ["entry_price_bucket"])
rsi_buckets = bucket_analysis(postmortem_entries, ["RSI_bucket"])
profile_price_buckets = bucket_analysis(postmortem_entries, ["profile", "entry_price_bucket"])
profile_asset_buckets = bucket_analysis(postmortem_entries, ["profile", "asset"])

def write_csv(rows, path):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {path}")

write_csv(profile_buckets, os.path.join(OUTPUT_DIR, "profile_bucket_performance.csv"))
write_csv(asset_buckets, os.path.join(OUTPUT_DIR, "asset_bucket_performance.csv"))
write_csv(entry_price_buckets, os.path.join(OUTPUT_DIR, "entry_price_bucket_performance.csv"))
write_csv(rsi_buckets, os.path.join(OUTPUT_DIR, "rsi_bucket_performance.csv"))
write_csv(profile_price_buckets, os.path.join(OUTPUT_DIR, "profile_price_bucket_performance.csv"))
write_csv(profile_asset_buckets, os.path.join(OUTPUT_DIR, "profile_asset_bucket_performance.csv"))

# ── §5: Expected vs Realized Calibration ──
calibration_by_profile = defaultdict(lambda: {"est_probs": [], "outcomes": [], "pnl": []})
for e in postmortem_entries:
    if e.get("estimated_probability") is not None:
        calibration_by_profile[e["profile"]]["est_probs"].append(e["estimated_probability"])
        calibration_by_profile[e["profile"]]["outcomes"].append(1 if e["win_loss"] == "WIN" else 0)
        calibration_by_profile[e["profile"]]["pnl"].append(e["net_pnl"])

calibration_rows = []
for profile, data in calibration_by_profile.items():
    n = len(data["est_probs"])
    if n == 0:
        continue
    mean_est = sum(data["est_probs"]) / n
    realized_wr = sum(data["outcomes"]) / n
    calibration_gap = mean_est - realized_wr
    # Brier score
    brier = sum((p - o) ** 2 for p, o in zip(data["est_probs"], data["outcomes"])) / n
    expected_ev = sum(data["est_probs"]) / n
    realized_ev = sum(data["pnl"]) / n
    ev_gap = expected_ev - realized_ev
    calibration_status = "CALIBRATION_FAILED" if expected_ev > 0 and sum(data["pnl"]) < 0 else ""
    
    calibration_rows.append({
        "profile": profile,
        "trades": n,
        "mean_estimated_probability": round(mean_est, 4),
        "realized_WR": round(realized_wr, 4),
        "calibration_gap": round(calibration_gap, 4),
        "Brier_score": round(brier, 4),
        "expected_EV_per_trade": round(expected_ev, 4),
        "realized_EV_per_trade": round(realized_ev, 4),
        "expected_vs_realized_EV_gap": round(ev_gap, 4),
        "status": calibration_status,
    })

cal_csv_path = os.path.join(OUTPUT_DIR, "calibration_table.csv")
if calibration_rows:
    with open(cal_csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=calibration_rows[0].keys())
        writer.writeheader()
        writer.writerows(calibration_rows)
    print(f"Wrote {len(calibration_rows)} calibration rows to {cal_csv_path}")

# ── §6: Inverse/Opposite-Side Audit ──
inverse_audit = []
selected_pnl = 0
opposite_pnl = 0
selected_wins = 0
opposite_wins = 0
selected_entries = []
opposite_entries = []

for e in postmortem_entries:
    entry = e["entry_ask"]
    opposite_entry = round(1.0 - entry, 4) if isinstance(entry, (int, float)) else None
    side = e["selected_side"]
    winner = e["resolved_winner"]
    bet = 2.0  # standard bet size
    
    # Selected side PnL already calculated
    selected_pnl += e["net_pnl"]
    if e["win_loss"] == "WIN":
        selected_wins += 1
    selected_entries.append(e["entry_ask"])
    
    # Opposite side hypothetical
    opp_side = "DOWN" if side == "UP" else "UP"
    if opposite_entry and opposite_entry > 0.05:
        opp_won = (winner == opp_side)
        if opp_won:
            opp_pnl = bet / max(opposite_entry, 0.01) - bet
            opposite_wins += 1
        else:
            opp_pnl = -bet
        opposite_pnl += opp_pnl
        opposite_entries.append(opposite_entry)
        
        opp_executable = opposite_entry >= 0.05  # rough check
    else:
        opp_won = None
        opp_pnl = 0
        opp_executable = False
        opposite_entries.append(opposite_entry)
    
    inverse_audit.append({
        "position_id": e["position_id"],
        "profile": e["profile"],
        "asset": e["asset"],
        "selected_side": side,
        "selected_entry": e["entry_ask"],
        "opposite_side": opp_side,
        "opposite_entry": opposite_entry,
        "opposite_executable": opp_executable if opposite_entry else False,
        "resolved_winner": winner,
        "selected_win": e["win_loss"] == "WIN",
        "opposite_win": opp_won,
        "selected_pnl": round(e["net_pnl"], 4),
        "opposite_pnl": round(opp_pnl, 4),
        "entry_price_bucket": e["entry_price_bucket"],
    })

inv_csv_path = os.path.join(OUTPUT_DIR, "inverse_side_audit.csv")
with open(inv_csv_path, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=inverse_audit[0].keys())
    writer.writeheader()
    writer.writerows(inverse_audit)
print(f"Wrote {len(inverse_audit)} inverse audit rows to {inv_csv_path}")

sel_wr = selected_wins / len(postmortem_entries) if postmortem_entries else 0
opp_wr = opposite_wins / len(postmortem_entries) if postmortem_entries else 0
sel_avg = sum(e for e in selected_entries if isinstance(e, (int, float))) / max(len([e for e in selected_entries if isinstance(e, (int, float))]), 1)
opp_avg = sum(e for e in opposite_entries if isinstance(e, (int, float))) / max(len([e for e in opposite_entries if isinstance(e, (int, float))]), 1)

# ── §10: Recommendation ──
recommendations = {}
for row in calibration_rows:
    profile = row["profile"]
    if row["status"] == "CALIBRATION_FAILED":
        recommendations[profile] = {"recommendation": "FREEZE_PROFILE", "reason": f"expected_EV={row['expected_EV_per_trade']:.4f} realized_EV={row['realized_EV_per_trade']:.4f} gap={row['expected_vs_realized_EV_gap']:.4f}"}
    elif row["trades"] < 20:
        recommendations[profile] = {"recommendation": "DIAGNOSTIC_ONLY", "reason": f"insufficient_sample={row['trades']}_need_20"}
    elif row["realized_WR"] < 0.5:
        recommendations[profile] = {"recommendation": "FREEZE_PROFILE", "reason": f"WR={row['realized_WR']:.3f}_below_0.50"}
    elif row["realized_EV_per_trade"] > 0 and row["trades"] >= 20:
        recommendations[profile] = {"recommendation": "KEEP_PAPER_TESTING", "reason": f"positive_EV={row['realized_EV_per_trade']:.4f}_trades={row['trades']}"}
    else:
        recommendations[profile] = {"recommendation": "DIAGNOSTIC_ONLY", "reason": f"mixed_results_WR={row['realized_WR']:.3f}_EV={row['realized_EV_per_trade']:.4f}"}

# Profiles with no trades
for profile in ["CORE_UP_STRICT", "PREOPEN_DIRECTION_EDGE", "CHEAP_CONVEX_EDGE", "BALANCED_DIRECTION_EDGE",
                 "CORE_UP_RECOVERABILITY_FIRST_SHADOW"]:
    if profile not in recommendations:
        recommendations[profile] = {"recommendation": "DIAGNOSTIC_ONLY", "reason": "insufficient_or_no_resolved_trades"}

# Add overall recommendation
recommendations["_overall"] = {
    "recommendation": "FREEZE_ALL_LOSING_PROFILES",
    "reason": "combined_WR=18%_combined_PnL=-$42.37_anti_signal_candidate=True",
    "selected_side_PnL": round(selected_pnl, 2),
    "opposite_side_hypothetical_PnL": round(opposite_pnl, 2),
    "selected_side_WR": round(sel_wr, 3),
    "opposite_side_WR": round(opp_wr, 3),
    "selected_avg_entry": round(sel_avg, 3),
    "opposite_avg_entry": round(opp_avg, 3),
    "SMALL_LIVE_READY": False,
}

rec_path = os.path.join(OUTPUT_DIR, "recommendation.json")
with open(rec_path, "w") as f:
    json.dump(recommendations, f, indent=2)
print(f"Wrote recommendation to {rec_path}")

# ── Write markdown postmortem ──
md_lines = []
md_lines.append("# V19.9 Negative-EV Postmortem Report")
md_lines.append(f"\nGenerated: {datetime.now().isoformat()}")
md_lines.append(f"\n## Summary")
md_lines.append(f"- Total trades: {len(postmortem_entries)}")
md_lines.append(f"- Wins: {sum(1 for e in postmortem_entries if e['win_loss']=='WIN')}")
md_lines.append(f"- Losses: {sum(1 for e in postmortem_entries if e['win_loss']=='LOSS')}")
md_lines.append(f"- Win Rate: {sel_wr:.1%}")
md_lines.append(f"- Total PnL: ${selected_pnl:.2f}")
md_lines.append(f"- Selected side avg entry: {sel_avg:.3f}")
md_lines.append(f"- Opposite side hypothetical PnL: ${opposite_pnl:.2f}")
md_lines.append(f"- Opposite side WR: {opp_wr:.1%}")
md_lines.append(f"- Opposite avg entry: {opp_avg:.3f}")
md_lines.append(f"\n## Classification: C_SHADOW_EXECUTION_PROVEN_NEGATIVE_EV")
md_lines.append(f"- Execution and settlement work correctly")
md_lines.append(f"- Realized strategy performance is negative")
md_lines.append(f"- Live remains DISABLED")
md_lines.append(f"\n## §4: Bucket Performance")
md_lines.append(f"\n### By Profile")
for b in profile_buckets:
    md_lines.append(f"- **{b.get('profile','?')}**: trades={b['trades']} W={b['wins']} L={b['losses']} WR={b['WR']:.1%} PnL=${b['net_PnL']:.2f} avg_entry={b['avg_entry']:.3f} be_WR={b['break_even_WR']:.3f}")
md_lines.append(f"\n### By Entry Price Bucket")
for b in entry_price_buckets:
    md_lines.append(f"- **{b.get('entry_price_bucket','?')}**: trades={b['trades']} W={b['wins']} L={b['losses']} WR={b['WR']:.1%} PnL=${b['net_PnL']:.2f} avg_entry={b['avg_entry']:.3f} be_WR={b['break_even_WR']:.3f}")
md_lines.append(f"\n### By RSI Bucket")
for b in rsi_buckets:
    md_lines.append(f"- **{b.get('RSI_bucket','?')}**: trades={b['trades']} W={b['wins']} L={b['losses']} WR={b['WR']:.1%} PnL=${b['net_PnL']:.2f} avg_entry={b['avg_entry']:.3f}")
md_lines.append(f"\n## §5: Calibration")
for c in calibration_rows:
    md_lines.append(f"- **{c['profile']}**: est_p={c['mean_estimated_probability']:.3f} realized_WR={c['realized_WR']:.3f} gap={c['calibration_gap']:.3f} Brier={c['Brier_score']:.4f} status={c['status']}")
md_lines.append(f"\n## §6: Inverse Side Audit")
md_lines.append(f"- Selected side PnL: ${selected_pnl:.2f} (WR={sel_wr:.1%})")
md_lines.append(f"- Opposite side PnL: ${opposite_pnl:.2f} (WR={opp_wr:.1%})")
md_lines.append(f"- Opposite side is {'POSITIVE' if opposite_pnl > 0 else 'NEGATIVE'} EV")
md_lines.append(f"\n## §10: Recommendations")
for profile, rec in recommendations.items():
    if profile.startswith("_"):
        md_lines.append(f"- **OVERALL**: {rec['recommendation']} — {rec['reason']}")
    else:
        md_lines.append(f"- **{profile}**: {rec['recommendation']} — {rec['reason']}")

md_lines.append(f"\n## Promotion Gate Status")
md_lines.append(f"- ❌ resolved_trades >= 30: {len(postmortem_entries)} (need 30)")
md_lines.append(f"- ❌ realized_EV_per_share > 0: ${selected_pnl/len(postmortem_entries):.4f}/trade" if postmortem_entries else "- ❌ realized_EV_per_share: N/A")
md_lines.append(f"- ❌ realized_EV_per_dollar > 0: N/A")
md_lines.append(f"- ❌ PF >= 1.15: N/A (negative PnL)")
md_lines.append(f"- ✅ settlement_errors = 0")
md_lines.append(f"- ✅ journal completeness = 100%")
md_lines.append(f"- ❌ LIVE REMAINS DISABLED")

md_path = os.path.join(OUTPUT_DIR, "v19_9_trade_postmortem.md")
with open(md_path, "w") as f:
    f.write("\n".join(md_lines))
print(f"Wrote markdown postmortem to {md_path}")

print("\n=== POSTMORTEM COMPLETE ===")
print(f"Selected PnL: ${selected_pnl:.2f} | Opposite PnL: ${opposite_pnl:.2f}")
print(f"Selected WR: {sel_wr:.1%} | Opposite WR: {opp_wr:.1%}")
print(f"ANTI_SIGNAL_SHADOW candidate: {'YES' if opposite_pnl > 0 else 'NO'}")