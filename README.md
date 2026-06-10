# Father Daddy Capital

**Execution-survivable convex continuation organism for Polymarket UpDown binaries.**

Current status: **LIVE — V21.7.1 runner + V21.7.13 real-time scanner + Weather V2.1 (TEMP QUARANTINED).**

## Active Bots

| Bot | Version | Mode | Status | Description |
|---|---|---|---|---|
| Live Runner | V21.7.1 | Armed | 🟡 Running | BTC/XRP/SOL/ETH 5m/15m DOWN_MOMENTUM scanner + executor |
| Real-Time Scanner | V21.7.13 | Build | 🟡 Running | WebSocket-first momentum scanner, 4-exchange feeds + PM REST poll |
| Weather Runner | V2.1 | Paper | 🔴 Halted | Temperature entries BLOCKED (V21.7.14). Settlement-only mode. |
| Rain Shadow | V2.3 | Shadow | 🟡 Discovery | Precipitation market shadow cell. No paper/live entries. |
| V19.8 Supervisor | V19.8 | — | 🟡 Cron | 5H loop monitor, every 1m |
| V2171 Supervisor | V21.7.1 | — | 🟡 Cron | Live runner health check, every 10m |

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

## V21.7.13 Real-Time Scanner

WebSocket-first architecture with asyncio.Lock for event-loop safety.

**Feeds:**
- **Binance Spot** — `bookTicker` WS, BTC/ETH/SOL/XRP
- **Bybit Perp** — `tickers` WS, BTC/ETH/SOL/XRP
- **OKX Perp** — `tickers` WS, BTC/ETH/SOL/XRP
- **Coinbase Spot** — `ticker` WS, BTC/ETH/SOL/XRP
- **Polymarket CLOB** — REST 5s poll (WS unreliable, reconnects every 6s)

**Key design:**
- `asyncio.Lock` on QuoteCacheV2 — single lock per snapshot cycle
- `await asyncio.sleep(0)` yield points in all WS handlers prevent event loop starvation
- `asyncio.gather` parallel HTTP for PM REST poll (0.3s/cycle vs 48s sequential)
- Ring-buffered velocity tracking: v1s, v3s, v5s, v15s, v30s, v60s
- Cross-exchange median price per asset
- Readiness gate: `pm_p50 < 6000ms` AND `ext_p95 < 5000ms`

## Weather Bot V2.1

Weather market paper trader with risk-tiered city management.

**⚠️ V21.7.14 CONTAINMENT: Temperature entries HALTED.** 0W/5L, -$7.60 drawdown (58.5%). Settlement-only mode.
Root cause: forecast sigma=0.3°C vs 3-12°C actual errors. All 5 losses bet on heat during a European cold anomaly.

**Risk tiers:** TRADE / QUALIFY / BLOCKED — position size, edge threshold, and σ adjustment per tier.
**Settlement:** WU-style rounding (0.5 rounds up), HKO-floor cities use floor rounding.
**Data source:** Open-Meteo daily max/min forecasts.
**Markets:** Polymarket weather contracts (temperature yes/no).
**Halt config:** `output/weather_bot/v2_3_halt_config.json`

## Rain Shadow Cell V2.3

Precipitation market shadow-only research cell. No paper/live entries until 25 resolved shadow events with PF ≥ 1.25.

- Market discovery: Polymarket rain/precipitation contracts via Gamma API
- Forecast: Open-Meteo precipitation probability + amount
- Edge model: `prob_rain - yes_price` with 20pp minimum
- Classification: `RAIN_MARKET_SHADOW_ONLY`
- Promotion requires: 25 resolved events, PF ≥ 1.25, 0 settlement/rule/timezone errors

## MCP Server Integrations

FDC runners can call MCP servers as Python functions via `mcp_client_bridge.py`:

```python
from mcp_client_bridge import MCPRouter
router = MCPRouter()

# Get BTC spot from ccxt
price = await router.call("ccxt", "get_ticker", {"symbol": "BTC/USDT", "exchange": "binance"})

# Get Polymarket orderbook
ob = await router.call("polymarket", "get_orderbook", {"market": "btc-5m-down"})

# Get on-chain data
bal = await router.call("onchain", "get_balance", {"address": "0x..."})
```

**Configured servers:**

| Server | Package | Purpose |
|---|---|---|
| `polymarket` | `polymarket-agent-mcp` | PM CLOB: orderbook, orders, positions |
| `ccxt` | `@mcpfun/mcp-server-ccxt` | Crypto exchange tickers (Binance, Bybit, OKX) |
| `codex` | `@codex-data/codex-mcp` | On-chain analytics |
| `onchain` | `@bankless/onchain-mcp` | EVM chain data: balances, transactions |
| `evmscope` | `evmscope` | EVM contract verification |
| `fetch` | `mcp-server-fetch` | HTTP fetch utility |
| `sqlite` | `mcp-server-sqlite` | SQLite query engine |
| `filesystem` | `@modelcontextprotocol/server-filesystem` | File I/O (project dir) |
| `notion` | `@notionhq/notion-mcp-server` | Notion workspace integration |
| `context7` | `@upstash/context7-mcp` | Code context search |
| `playwright` | `@executeautomation/mcp-playwright` | Browser automation |

All servers use stdio transport. Connections are lazy (connect on first call) and persistent.

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
# V21.7.1 Live runner
python src/v217_live/v2171_live_runner.py --live --max-iterations 0 --scan-interval 5

# V21.7.13 Real-time scanner
python src/v217_live/v21713/ws_realtime_scanner.py

# Weather V2.1 paper trader (TEMP ENTRIES HALTED — settlement only)
python src/weather/v1_weather_runner_v21.py --paper --interval 300

# Rain V2.3 shadow cell (discovery only)
python src/weather/v2_3_rain_shadow_cell.py

# PMXT simulation
python v2171_pmxt_2000_trade_sim.py
```

## Architecture

```
src/v217_live/
  v2171_live_runner.py           # Live deployment organism (V21.7.1)
  v21713/
    ws_realtime_scanner.py       # WS-first real-time momentum scanner
    quote_cache_v2.py            # Async tick-level book flow tracker
    external_momentum_tracker.py # Cross-exchange velocity computation
  mcp_client_bridge.py          # MCP server Python bridge
  quote_cache.py                # Legacy synchronous quote cache
  ws_feed_layer.py              # WS feed architecture (reference)
  ws_feed_architecture.py       # WS feed architecture (reference)
  scalper_paper_live_simulator.py   # Scalper paper/live simulator
  bonereaper_activity_mirror.py     # Bonereaper whale tracking mirror
  lag_alpha_monitor.py              # Lag alpha monitor (V21.7)
  lag_alpha_monitor_v2179.py       # Lag alpha monitor (V21.7.9)
  shadow_counterfactual_tracker.py  # Shadow counterfactual settlement
  shadow_cf_replay_settlements.py   # Counterfactual replay
  supervisor_state_reconciler.py    # Supervisor state reconciliation
  current_crypto_window_watcher.py  # Crypto window watcher

src/weather/
  v1_weather_runner.py     # Weather V1 — original runner
  v1_weather_runner_v2.py   # Weather V2 — WU rounding, HKO floor cities
  v1_weather_runner_v21.py  # Weather V2.1 — risk tiers, hindcast, live readiness gate, V21.7.14 halt
  v2_3_rain_shadow_cell.py  # Rain V2.3 — precipitation shadow cell (discovery only)

output/
  v2171_live/           # Live runner state, trades, logs
  v21713_realtime_scanner/  # Scanner output: readiness, books, momentum events
  weather_bot/          # Weather bot state, trades, reports
    v2_3_halt_config.json            # V21.7.14 halt directive
    v2_3_weather_state_reconciliation.json  # Canonical state truth
    v2_3_temperature_failure_audit.json      # Per-trade loss analysis
    v2_3_weather_loss_audit.json             # Aggregate loss summary
    v2_3_weather_live_gate.json              # Live promotion gate
    rain_shadow/                             # Rain shadow cell output
      rain_market_discovery.jsonl
      rain_shadow_events.jsonl
      rain_shadow_settlements.jsonl
      rain_readiness_report.json
  supervisor/
    v21714_weather_supervisor_status.json   # V21.7.14 supervisor status
  v2171_pmxt_2000_trade_sim.json  # PMXT simulation results
```

## Key Design Decisions

- **Reversal thesis is dead.** 88.6% of cheap-token trades lose under binary settlement. Markets overprice reversal probability. Continuation convexity is the edge.
- **DOWN only.** UP extraction is blocked. The structurally underpriced side is DOWN continuation.
- **Binary settlement only.** No synthetic midpoints, no interpolated closes. Cheap tokens settle to $0.00, rich to $1.00.
- **60-loss kill switch** (not 8). V21.7 observed 29 consecutive losses inside a profitable regime (PF 2.10). Loss streaks are structural for low-WR/high-payout strategies.
- **Survivability ranking.** Trades ranked by `realized_ev × fill_probability × slippage_survival × queue_survival × payout_asymmetry × bucket_weight`. Not prediction accuracy.
- **WebSocket-first scanner.** Real-time exchange feeds via WS, PM via REST 5s poll. `asyncio.Lock` prevents event loop starvation. Yield points in all handlers.

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