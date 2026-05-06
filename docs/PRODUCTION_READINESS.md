# Production Readiness Plan

Father Daddy Capital is not production-live yet. The current baseline is a safer local paper-trading runtime with importable core modules, deterministic risk gates, and no Docker dependency.

## Required Before Live Crypto Trading

### Critical

- Rotate all credentials that were present in the removed `.env` file.
- Use a real secret store or process supervisor environment injection.
- Add exchange-specific paper adapters before adding live exchange adapters.
- Add immutable trade/audit logging with order request, risk decision, broker response, and portfolio state.
- Fail closed on missing market data, stale prices, invalid balances, excessive slippage, and API uncertainty.
- Add a kill switch that blocks all new orders and liquidations separately.

### High

- Consolidate remaining legacy modules into the supported runtime path.
- Split paper execution from live execution behind a common broker interface.
- Add portfolio accounting that marks open positions to current market prices.
- Add deterministic backtests with fixed fixtures and no network calls.
- Add CI for import smoke tests, unit tests, type checks, linting, and secret scanning.

### Medium

- Add structured JSON logs with correlation IDs for signal, risk, order, and portfolio events.
- Add Prometheus metrics without requiring Docker.
- Add drawdown, exposure, concentration, and exchange-rate-limit dashboards.
- Add replay tooling for incident review.

## Recommended Multi-Agent Runtime

Use a narrow agent contract:

```text
MarketDataProvider -> SignalAgent[] -> DecisionFusion -> RiskManager -> Broker -> AuditLogger
```

Each agent should return a typed signal:

```text
symbol, side, confidence, horizon, strategy, price_reference, evidence, timestamp
```

The risk layer must be deterministic and authoritative. Agents can recommend trades; they must not bypass risk gates or place orders.

## Things To Avoid

- Do not add autonomous live execution until paper mode has end-to-end tests.
- Do not let agents mutate global configuration.
- Do not mix backtest, paper, and live execution paths.
- Do not use unreviewed AI-generated strategies with live capital.
- Do not rely on Docker health checks or deployment files; Docker support has been intentionally removed.
