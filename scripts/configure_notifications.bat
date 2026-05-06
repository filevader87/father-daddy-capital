@echo off
setlocal enabledelayedexpansion

echo Paper Trading Notification Configuration
echo ======================================
echo.

:: Email Configuration
echo Email Notification Setup
echo ----------------------
set /p EMAIL_USER="Enter sender email address (or press Enter to skip): "
set /p EMAIL_RECIPIENT="Enter recipient email address (or press Enter to skip): "
set /p EMAIL_PASSWORD="Enter email password (or press Enter to skip): "

:: Telegram Configuration
echo.
echo Telegram Notification Setup
echo ------------------------
set /p TELEGRAM_BOT_TOKEN="Enter Telegram bot token (or press Enter to skip): "
set /p TELEGRAM_CHAT_ID="Enter Telegram chat ID (or press Enter to skip): "

:: Slack Configuration
echo.
echo Slack Notification Setup
echo ----------------------
set /p SLACK_WEBHOOK_URL="Enter Slack webhook URL (or press Enter to skip): "

:: Save configuration
echo.
echo Saving configuration...
(
echo EMAIL_USER=%EMAIL_USER%
echo EMAIL_RECIPIENT=%EMAIL_RECIPIENT%
echo EMAIL_PASSWORD=%EMAIL_PASSWORD%
echo TELEGRAM_BOT_TOKEN=%TELEGRAM_BOT_TOKEN%
echo TELEGRAM_CHAT_ID=%TELEGRAM_CHAT_ID%
echo SLACK_WEBHOOK_URL=%SLACK_WEBHOOK_URL%
) > .env

echo.
echo Configuration saved successfully!
echo You can now run start_paper_trading.bat to begin paper trading.
echo.
pause 