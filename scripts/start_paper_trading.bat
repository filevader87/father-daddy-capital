@echo off
setlocal enabledelayedexpansion

echo Setting up Paper Trading Environment...

:: Create logs directory if it doesn't exist
if not exist "logs" mkdir logs

:: Set default environment variables if not already set
if "%EMAIL_USER%"=="" set "EMAIL_USER=paper_trading@example.com"
if "%EMAIL_RECIPIENT%"=="" set "EMAIL_RECIPIENT=your_email@example.com"
if "%TELEGRAM_BOT_TOKEN%"=="" set "TELEGRAM_BOT_TOKEN=your_telegram_token"
if "%TELEGRAM_CHAT_ID%"=="" set "TELEGRAM_CHAT_ID=your_telegram_chat_id"
if "%SLACK_WEBHOOK_URL%"=="" set "SLACK_WEBHOOK_URL=your_slack_webhook"

:: Create a temporary .env file
echo Creating environment file...
(
echo EMAIL_USER=%EMAIL_USER%
echo EMAIL_RECIPIENT=%EMAIL_RECIPIENT%
echo TELEGRAM_BOT_TOKEN=%TELEGRAM_BOT_TOKEN%
echo TELEGRAM_CHAT_ID=%TELEGRAM_CHAT_ID%
echo SLACK_WEBHOOK_URL=%SLACK_WEBHOOK_URL%
) > .env

:: Initialize paper trading environment
echo Initializing paper trading environment...
python scripts/init_paper_trading.py
if errorlevel 1 (
    echo Failed to initialize paper trading environment.
    pause
    exit /b 1
)

:: Start paper trading system
echo Starting paper trading system...
python scripts/run_paper_trading.py
if errorlevel 1 (
    echo Failed to start paper trading system.
    pause
    exit /b 1
)

pause 