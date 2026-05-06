#!/bin/bash

# Create .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "Creating .env file..."
    touch .env
fi

# Function to prompt for and set environment variable
set_env_var() {
    local var_name=$1
    local prompt=$2
    local current_value=$(grep "^$var_name=" .env | cut -d'=' -f2)
    
    if [ -z "$current_value" ]; then
        read -p "$prompt: " value
        echo "$var_name=$value" >> .env
    else
        read -p "$prompt [$current_value]: " value
        if [ ! -z "$value" ]; then
            sed -i "s/^$var_name=.*/$var_name=$value/" .env
        fi
    fi
}

# Set up required environment variables
echo "Setting up environment variables for paper trading..."

# Alpaca API credentials
set_env_var "ALPACA_API_KEY" "Enter Alpaca API Key"
set_env_var "ALPACA_API_SECRET" "Enter Alpaca API Secret"

# Email notification settings
set_env_var "EMAIL_USER" "Enter email address for notifications"
set_env_var "EMAIL_RECIPIENT" "Enter recipient email address"
set_env_var "EMAIL_PASSWORD" "Enter email password"

# Telegram notification settings
set_env_var "TELEGRAM_BOT_TOKEN" "Enter Telegram bot token"
set_env_var "TELEGRAM_CHAT_ID" "Enter Telegram chat ID"

# Slack notification settings
set_env_var "SLACK_WEBHOOK_URL" "Enter Slack webhook URL"

echo "Environment variables have been set up."
echo "You can now start the paper trading system." 