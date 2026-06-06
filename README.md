# Father Daddy Capital

**Execution-survivable convex continuation organism for Polymarket UpDown binaries.**

Current status: **LIVE — Phase 1 micro-live**. V21.7.1 deployed on BTC 5m/15m DOWN_MOMENTUM contracts, fixed $1 positions, TAKER route only.

## V21.7.1 Configuration

| Parameter | Value |
|---|---|
| Side | DOWN only |
| State | MOMENTUM, CONTINUATION |
| Bucket | 3–12¢ PRIMARY |
| Preferred | 5–8¢ (weight=1.0) |
| Route | TAKER |
| Position | $1.00 fixed |
| Kill switches | $15/day, $50/week, 60 consec losses |

## Simulation Results (PMXT, 360 trades)

| Metric | Value |
|---|---|
| Win Rate | 13.9% |
| Profit Factor | 2.10 |
| Realized EV | $0.74/trade |
| Payout Ratio | 12.99x |
| ROI | +267.6% |
| Sharpe | 2.77 |
| MC Profitable | 99.8% |

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
```

Runtime configuration: `config/trading.yaml`. Secrets injected via `.env` (never committed).

## Run

```bash
# Live runner (V21.7.1)
python src/v217_live/v2171_live_runner.py --live --max-iterations 1200 --scan-interval 5

# Paper mode (default)
python src/v217_live/v2171_live_runner.py --max-iterations 100 --scan-interval 5

# PMXT simulation
python v2171_pmxt_2000_trade_sim.py
```

## Architecture

```
src/v217_live/
  v2171_live_runner.py    # Live deployment organism (Phase 1)

v2171_pmxt_2000_trade_sim.py  # V21.7.1 PMXT backtesting

fdc_pm_live.py           # Polymarket CLOB integration, wallet, auth

output/
  v2171_pmxt_2000_trade_sim.json  # Simulation results
  v2171_live/
    state.json            # Live runner state
    trades.jsonl           # Trade log
    v2171_live_console.log # Console output
```

## Key Design Decisions

- **Reversal thesis is dead.** 88.6% of cheap-token trades lose under binary settlement. Markets overprice reversal probability. Continuation convexity is the edge.
- **DOWN only.** UP extraction is blocked. The structurally underpriced side is DOWN continuation.
- **Binary settlement only.** No synthetic midpoints, no interpolated closes. Cheap tokens settle to $0.00, rich to $1.00.
- **60-loss kill switch** (not 8). V21.7 observed 29 consecutive losses inside a profitable regime (PF 2.10). Loss streaks are structural for low-WR/high-payout strategies.
- **Survivability ranking.** Trades ranked by `realized_ev × fill_probability × slippage_survival × queue_survival × payout_asymmetry × bucket_weight`. Not prediction accuracy.

## Safety Rules

- Paper mode is the default. `--live` flag required for real execution.
- Live mode must fail closed when credentials are missing.
- Real credentials must never be committed.
- Kill switches: $15 daily, $50 weekly, 60 consecutive losses, 30 trades/day, 100 trade cap (Phase 1).
- Hard reversion to paper if realized EV < 0 over 100 trades or PF < 1.0.

## Documentation

- [`docs/why_fdc_theoretical_foundations.md`](docs/why_fdc_theoretical_foundations.md) — Research paper: defensive capital accumulation in the agentic economy
- [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md) — Production readiness checklist
- [`POLYMARKET_BOT_EXTRACTION_AUDIT.md`](POLYMARKET_BOT_EXTRACTION_AUDIT.md) — V20/V21 audit findings
- [`PRODUCTION_PROFILE_V19_7f.md`](PRODUCTION_PROFILE_V19_7f.md) — V19.7f production profile

## License

MIT License. Trading involves risk of loss. This project is for research and controlled paper-trading unless explicitly hardened for live deployment.