#!/usr/bin/env python3
"""
FDC World Cup Bot — Historical Backtest
=========================================
Backtests the Elo+Poisson model against WC 2018 and WC 2022 group stage results.

Metrics:
  - Match winner accuracy (1X2)
  - Over/Under 2.5 accuracy
  - BTTS accuracy
  - Brier score for match winner
  - Log loss
  - Simulated PnL (betting at model edge vs market odds)

The backtest:
  1. For each historical match, compute model probabilities
  2. Compare to "synthetic market" odds (derived from closing odds patterns)
  3. Simulate paper trades with $2 position size
  4. Track W/L, PnL, Brier score, calibration
"""

import sys
import json
import math
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.worldcup.match_model import compute_match_probabilities, update_elo
from src.worldcup.historical_data import get_historical_matches, WC_2022_GROUP_STAGE, WC_2018_GROUP_STAGE

OUTPUT_DIR = PROJECT_ROOT / "output" / "worldcup_bot"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── Synthetic market odds ───
# For backtesting, we simulate "market" odds by adding noise to true probabilities
# This simulates the inefficiency we'd exploit in real markets
# Real PM markets typically have 2-5% margin and some mispricing

def synthetic_market_odds(model_probs: Dict, noise_level: float = 0.12, seed: int = 0) -> Dict:
    """
    Generate synthetic market odds by adding noise to model probabilities.
    This simulates the market inefficiency we'd exploit.

    noise_level: standard deviation of Gaussian noise added to probabilities
    seed: random seed for reproducibility (use match index for variety)
    """
    import random
    random.seed(seed)

    p_home = model_probs["p_home_win"]
    p_draw = model_probs["p_draw"]
    p_away = model_probs["p_away_win"]

    # Add noise
    p_home_mkt = max(0.01, min(0.99, p_home + random.gauss(0, noise_level)))
    p_draw_mkt = max(0.01, min(0.99, p_draw + random.gauss(0, noise_level)))
    p_away_mkt = max(0.01, min(0.99, p_away + random.gauss(0, noise_level)))

    # Normalize (add 5% margin)
    total = p_home_mkt + p_draw_mkt + p_away_mkt
    margin = 1.05
    p_home_mkt = p_home_mkt / total * margin
    p_draw_mkt = p_draw_mkt / total * margin
    p_away_mkt = p_away_mkt / total * margin

    # O/U 2.5
    ou_over = model_probs["over_under"]["over_2.5"]
    ou_over_mkt = max(0.01, min(0.99, ou_over + random.gauss(0, noise_level)))

    # BTTS
    btts_yes = model_probs["btts_yes"]
    btts_yes_mkt = max(0.01, min(0.99, btts_yes + random.gauss(0, noise_level)))

    return {
        "p_home_win": p_home_mkt,
        "p_draw": p_draw_mkt,
        "p_away_win": p_away_mkt,
        "over_2.5": ou_over_mkt,
        "under_2.5": 1 - ou_over_mkt,
        "btts_yes": btts_yes_mkt,
        "btts_no": 1 - btts_yes_mkt,
    }


def brier_score(predicted: float, actual: float) -> float:
    """Brier score for a single prediction: (predicted - actual)^2"""
    return (predicted - actual) ** 2


def log_loss(predicted: float, actual: float) -> float:
    """Log loss for a single prediction."""
    p = max(1e-10, min(1 - 1e-10, predicted))
    if actual == 1:
        return -math.log(p)
    return -math.log(1 - p)


def run_backtest():
    """Run the full historical backtest."""
    matches = get_historical_matches()
    print(f"Backtesting on {len(matches)} WC group stage matches (2018 + 2022)")
    print()

    # Dynamic Elo — update after each match
    from src.worldcup.elo_ratings import ELO_RATINGS, get_elo
    elo = dict(ELO_RATINGS)  # Copy to mutate

    results = []
    total_pnl = 0.0
    wins = 0
    losses = 0
    total_brier = 0.0
    total_logloss = 0.0
    match_winner_correct = 0
    ou_correct = 0
    btts_correct = 0
    total_trades = 0

    POSITION_SIZE = 2.0
    MIN_EDGE_PP = 6.0  # V21.7.58: Aligned with live bot thresholds (was 10)

    for i, (home, away, h_goals, a_goals, stage, year) in enumerate(matches):
        # Get current Elo ratings
        home_elo = elo.get(home, 1500)
        away_elo = elo.get(away, 1500)

        # Compute model probabilities
        model_probs = compute_match_probabilities(home, away)

        # Generate synthetic market odds
        market_odds = synthetic_market_odds(model_probs, seed=i+1)

        # Determine actual results
        if h_goals > a_goals:
            actual_home_win = 1
            actual_draw = 0
            actual_away_win = 0
        elif h_goals == a_goals:
            actual_home_win = 0
            actual_draw = 1
            actual_away_win = 0
        else:
            actual_home_win = 0
            actual_draw = 0
            actual_away_win = 1

        total_goals = h_goals + a_goals
        actual_over_25 = 1 if total_goals > 2.5 else 0
        actual_btts = 1 if h_goals > 0 and a_goals > 0 else 0

        # Match winner accuracy
        model_pred_winner = max(
            ("home", model_probs["p_home_win"]),
            ("draw", model_probs["p_draw"]),
            ("away", model_probs["p_away_win"]),
            key=lambda x: x[1]
        )
        actual_winner = "home" if actual_home_win else ("draw" if actual_draw else "away")
        if model_pred_winner[0] == actual_winner:
            match_winner_correct += 1

        # O/U 2.5 accuracy
        model_pred_ou = "over" if model_probs["over_under"]["over_2.5"] > 0.5 else "under"
        actual_ou = "over" if actual_over_25 else "under"
        if model_pred_ou == actual_ou:
            ou_correct += 1

        # BTTS accuracy
        model_pred_btts = "yes" if model_probs["btts_yes"] > 0.5 else "no"
        actual_btts_val = "yes" if actual_btts else "no"
        if model_pred_btts == actual_btts_val:
            btts_correct += 1

        # Brier score for match winner (3-way)
        brier = (
            brier_score(model_probs["p_home_win"], actual_home_win) +
            brier_score(model_probs["p_draw"], actual_draw) +
            brier_score(model_probs["p_away_win"], actual_away_win)
        ) / 3.0
        total_brier += brier

        # Log loss for match winner
        ll = (
            log_loss(model_probs["p_home_win"], actual_home_win) +
            log_loss(model_probs["p_draw"], actual_draw) +
            log_loss(model_probs["p_away_win"], actual_away_win)
        ) / 3.0
        total_logloss += ll

        # Simulate trades — bet on match winner, O/U 2.5, BTTS
        match_pnl = 0.0
        match_trades = 0

        # Match winner trade
        for outcome, model_p, market_p, actual in [
            ("home_win", model_probs["p_home_win"], market_odds["p_home_win"], actual_home_win),
            ("draw", model_probs["p_draw"], market_odds["p_draw"], actual_draw),
            ("away_win", model_probs["p_away_win"], market_odds["p_away_win"], actual_away_win),
        ]:
            edge = (model_p - market_p) * 100
            if edge > MIN_EDGE_PP:
                # Bet YES at market price
                cost = POSITION_SIZE * market_p
                payout = POSITION_SIZE if actual == 1 else 0
                pnl = payout - cost
                match_pnl += pnl
                match_trades += 1
                total_trades += 1
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1

        # O/U 2.5 trade
        edge_ou = (model_probs["over_under"]["over_2.5"] - market_odds["over_2.5"]) * 100
        if abs(edge_ou) > MIN_EDGE_PP:
            if edge_ou > 0:
                # Bet over
                cost = POSITION_SIZE * market_odds["over_2.5"]
                payout = POSITION_SIZE if actual_over_25 else 0
            else:
                # Bet under
                cost = POSITION_SIZE * market_odds["under_2.5"]
                payout = POSITION_SIZE if not actual_over_25 else 0
            pnl = payout - cost
            match_pnl += pnl
            match_trades += 1
            total_trades += 1
            if pnl > 0:
                wins += 1
            else:
                losses += 1

        # BTTS trade
        edge_btts = (model_probs["btts_yes"] - market_odds["btts_yes"]) * 100
        if abs(edge_btts) > MIN_EDGE_PP:
            if edge_btts > 0:
                cost = POSITION_SIZE * market_odds["btts_yes"]
                payout = POSITION_SIZE if actual_btts else 0
            else:
                cost = POSITION_SIZE * market_odds["btts_no"]
                payout = POSITION_SIZE if not actual_btts else 0
            pnl = payout - cost
            match_pnl += pnl
            match_trades += 1
            total_trades += 1
            if pnl > 0:
                wins += 1
            else:
                losses += 1

        total_pnl += match_pnl

        # Update Elo ratings
        new_home_elo, new_away_elo = update_elo(home_elo, away_elo, h_goals, a_goals, k=40)
        elo[home] = new_home_elo
        elo[away] = new_away_elo

        results.append({
            "match": f"{home} vs {away}",
            "year": year,
            "score": f"{h_goals}-{a_goals}",
            "model_pred": model_pred_winner[0],
            "actual": actual_winner,
            "correct": model_pred_winner[0] == actual_winner,
            "model_probs": {
                "home_win": round(model_probs["p_home_win"], 3),
                "draw": round(model_probs["p_draw"], 3),
                "away_win": round(model_probs["p_away_win"], 3),
            },
            "market_odds": {
                "home_win": round(market_odds["p_home_win"], 3),
                "draw": round(market_odds["p_draw"], 3),
                "away_win": round(market_odds["p_away_win"], 3),
            },
            "brier": round(brier, 4),
            "trades": match_trades,
            "pnl": round(match_pnl, 2),
        })

    # ─── Summary ───
    n = len(matches)
    print("=" * 70)
    print("BACKTEST RESULTS — Elo+Poisson Model vs WC 2018+2022 Group Stage")
    print("=" * 70)
    print()
    print(f"Matches: {n}")
    print(f"Trades: {total_trades} (wins={wins}, losses={losses})")
    print(f"Win rate: {wins/(wins+losses)*100:.1f}%" if wins + losses > 0 else "N/A")
    print(f"PnL: ${total_pnl:+.2f}")
    print(f"ROI: {total_pnl/(total_trades*POSITION_SIZE)*100:.1f}%" if total_trades > 0 else "N/A")
    print()
    print("Model Accuracy:")
    print(f"  Match winner (1X2): {match_winner_correct}/{n} = {match_winner_correct/n*100:.1f}%")
    print(f"  Over/Under 2.5:     {ou_correct}/{n} = {ou_correct/n*100:.1f}%")
    print(f"  BTTS:               {btts_correct}/{n} = {btts_correct/n*100:.1f}%")
    print()
    print(f"Average Brier score: {total_brier/n:.4f}")
    print(f"Average Log loss:    {total_logloss/n:.4f}")
    print()

    # Per-year breakdown
    for year in [2018, 2022]:
        year_results = [r for r in results if r["year"] == year]
        year_correct = sum(1 for r in year_results if r["correct"])
        year_pnl = sum(r["pnl"] for r in year_results)
        year_trades = sum(r["trades"] for r in year_results)
        year_brier = sum(r["brier"] for r in year_results) / len(year_results)
        print(f"  {year}: {year_correct}/{len(year_results)} = {year_correct/len(year_results)*100:.1f}% | "
              f"PnL=${year_pnl:+.2f} | trades={year_trades} | brier={year_brier:.4f}")

    print()

    # Save results
    report = {
        "total_matches": n,
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / (wins + losses) if wins + losses > 0 else 0,
        "pnl": total_pnl,
        "roi_pct": total_pnl / (total_trades * POSITION_SIZE) * 100 if total_trades > 0 else 0,
        "match_winner_accuracy": match_winner_correct / n,
        "ou_accuracy": ou_correct / n,
        "btts_accuracy": btts_correct / n,
        "avg_brier": total_brier / n,
        "avg_logloss": total_logloss / n,
        "per_year": {},
        "results": results,
    }

    for year in [2018, 2022]:
        year_results = [r for r in results if r["year"] == year]
        year_correct = sum(1 for r in year_results if r["correct"])
        year_pnl = sum(r["pnl"] for r in year_results)
        year_trades = sum(r["trades"] for r in year_results)
        year_brier = sum(r["brier"] for r in year_results) / len(year_results)
        report["per_year"][year] = {
            "matches": len(year_results),
            "correct": year_correct,
            "accuracy": year_correct / len(year_results),
            "pnl": year_pnl,
            "trades": year_trades,
            "brier": year_brier,
        }

    report_path = OUTPUT_DIR / "wc_backtest_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved: {report_path}")

    return report


if __name__ == "__main__":
    run_backtest()