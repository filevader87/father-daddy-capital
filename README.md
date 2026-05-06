# Father Daddy Capital

Local-first crypto trading research and paper-trading infrastructure.

Current status: paper-trading foundation. Live trading is gated behind explicit environment variables and manual confirmation. Do not treat this repository as production-live until the remaining items in `docs/PRODUCTION_READINESS.md` are complete.

## Setup

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

Runtime configuration lives in `config/trading.yaml`. Secrets are not loaded from `.env`; inject them through your shell, service manager, or secret store. Use `.env.example` only as a variable reference.

## Run

```powershell
# Paper trading
.\scripts\run_local.ps1 -Mode paper

# Tests
.\scripts\run_local.ps1 -Mode test

# Live trading, requires ALPACA_BASE_URL, ALPACA_API_KEY, and ALPACA_SECRET_KEY
.\scripts\run_local.ps1 -Mode live
```

Linux/macOS users can use:

```bash
./deploy.sh paper
./deploy.sh test
```

## Architecture Direction

The supported runtime path is:

1. `config/trading.yaml` as the primary configuration source.
2. `src.market_data` for normalized OHLCV data.
3. `src.agents` for signal generation.
4. `src.risk.risk_manager.RiskManager` for deterministic risk gates and position sizing.
5. `src.main.TradingSystem` for orchestration.

Legacy modules still exist and are being consolidated. Avoid building new features on duplicated config loaders, duplicate trading interfaces, or old runtime logs.

## Safety Rules

- Paper mode is the default.
- Live mode must fail closed when credentials are missing.
- Real credentials must never be committed.
- Generated files, logs, local models, state, coverage output, and virtual environments do not belong in version control.
- Any exchange connector must be tested in paper mode with deterministic risk gates before live execution is enabled.

## Verification

```powershell
python -m pytest tests/python/test_runtime_contracts.py -q
python -B -c "import src.main; print('ok')"
```

## License

MIT License. Trading involves risk of loss. This project is for research and controlled paper-trading unless explicitly hardened for live deployment.
