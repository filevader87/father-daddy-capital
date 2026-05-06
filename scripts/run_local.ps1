param(
    [ValidateSet("paper", "live", "backtest", "test")]
    [string]$Mode = "paper",
    [string]$ConfigPath = "config/trading.yaml"
)

$ErrorActionPreference = "Stop"

if (Test-Path ".env") {
    throw ".env exists but is not loaded automatically. Inject secrets through the shell, Windows Credential Manager, or your process supervisor."
}

if (-not (Test-Path $ConfigPath)) {
    throw "Configuration file not found: $ConfigPath"
}

if ($Mode -eq "live") {
    foreach ($required in @("ALPACA_BASE_URL", "ALPACA_API_KEY", "ALPACA_SECRET_KEY")) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($required))) {
            throw "Missing required live trading environment variable: $required"
        }
    }

    $confirmation = Read-Host "LIVE TRADING MODE. Type yes to continue"
    if ($confirmation -ne "yes") {
        Write-Host "Live trading cancelled."
        exit 0
    }
}

$env:CONFIG_PATH = $ConfigPath
$env:TRADING_MODE = $Mode
$env:PYTHONPATH = (Get-Location).Path

if ($Mode -eq "test") {
    python -m pytest tests/python -q
    exit $LASTEXITCODE
}

python -m src.main --mode=$Mode --config=$ConfigPath
