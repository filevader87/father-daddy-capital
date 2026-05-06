# Quickstart Guide

This guide starts Father Daddy Capital in a local paper-trading environment without Docker.

## 1. Set Up Python

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

## 2. Configure Runtime

Use `config/trading.yaml` as the primary config file. Do not create a committed `.env` file.

For paper trading:

```powershell
$env:CONFIG_PATH = "config/trading.yaml"
$env:TRADING_MODE = "paper"
```

For live trading, inject credentials through your shell or process manager:

```powershell
$env:ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
$env:ALPACA_API_KEY = "<secret>"
$env:ALPACA_SECRET_KEY = "<secret>"
```

## 3. Run Smoke Verification

```powershell
python -m pytest tests/python/test_runtime_contracts.py -q
python -B -c "import src.main; print('ok')"
```

## 4. Launch Paper Trading

```powershell
.\scripts\run_local.ps1 -Mode paper
```

Logs are written under `logs/` at runtime. Logs are ignored and should not be committed.

## Troubleshooting

If startup fails:

1. Confirm `config/trading.yaml` exists.
2. Confirm no `.env` file is present in the repo root.
3. Run `python -m pytest tests/python/test_runtime_contracts.py -q`.
4. Check generated runtime logs under `logs/`.
