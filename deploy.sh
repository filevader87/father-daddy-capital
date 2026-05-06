#!/bin/bash

# Father Daddy Capital - Local Deployment Script
# Runs the system directly on the host. Docker is intentionally unsupported.

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
MODE="paper"
CONFIG_PATH="config/trading.yaml"
VERBOSE=false

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to show usage
show_usage() {
    echo "Father Daddy Capital - Unified Deployment"
    echo "========================================"
    echo ""
    echo "Usage: $0 [OPTIONS] [MODE]"
    echo ""
    echo "Modes:"
    echo "  paper     - Paper trading (default)"
    echo "  live      - Live trading"
    echo "  backtest  - Backtesting"
    echo "  test      - Run tests"
    echo ""
    echo "Options:"
    echo "  -c, --config PATH    Configuration file path (default: config/trading.yaml)"
    echo "  -v, --verbose        Verbose output"
    echo "  -h, --help           Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 paper                    # Deploy paper trading"
    echo "  $0 live                    # Run live trading after confirmation"
    echo "  $0 backtest -c my_config.yaml  # Run backtest with custom config"
    echo "  $0 test                     # Run tests"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        paper|live|backtest|test)
            MODE="$1"
            shift
            ;;
        -c|--config)
            CONFIG_PATH="$2"
            shift 2
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

# Validate configuration file
if [[ ! -f "$CONFIG_PATH" ]]; then
    print_error "Configuration file not found: $CONFIG_PATH"
    exit 1
fi

# Function to check prerequisites
check_prerequisites() {
    print_status "Checking prerequisites..."
    
    # Check Python version
    if ! python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
    then
        print_error "Python 3.10+ is required"
        exit 1
    fi
    
    # Check if virtual environment exists
    if [[ ! -d "venv" ]]; then
        print_warning "Virtual environment not found. Creating one..."
        python3 -m venv venv
    fi
    
    # Activate virtual environment
    source venv/bin/activate
    
    # Check if requirements are installed
    if ! pip list | grep -q "numpy"; then
        print_status "Installing dependencies..."
        pip install -r requirements.txt
    fi
    
    print_success "Prerequisites check completed"
}

# Function to setup environment
setup_environment() {
    print_status "Setting up environment..."
    
    # Create necessary directories
    mkdir -p logs data state
    
    # Set environment variables
    export CONFIG_PATH="$CONFIG_PATH"
    export TRADING_MODE="$MODE"
    export PYTHONPATH="$PWD/src:$PYTHONPATH"
    
    # Refuse implicit .env loading so secrets are injected by the shell or process manager.
    if [[ -f ".env" ]]; then
        print_error ".env exists but is not loaded automatically. Move secrets to your shell, service manager, or secret store."
        exit 1
    fi
    
    print_success "Environment setup completed"
}

# Function to run tests
run_tests() {
    print_status "Running tests..."
    
    # Run pytest with coverage
    python -m pytest tests/ -v --cov=src --cov-report=term-missing
    
    if [[ $? -eq 0 ]]; then
        print_success "All tests passed"
    else
        print_error "Tests failed"
        exit 1
    fi
}

# Function to deploy paper trading
deploy_paper_trading() {
    print_status "Deploying paper trading..."
    
    # Update config for paper trading
    python -c "
import yaml
with open('$CONFIG_PATH', 'r') as f:
    config = yaml.safe_load(f)
config['trading']['mode'] = 'paper'
config['development']['dry_run'] = True
with open('$CONFIG_PATH', 'w') as f:
    yaml.dump(config, f, default_flow_style=False)
"
    
    # Start the trading system
    python -m src.main --mode=paper --config="$CONFIG_PATH"
}

# Function to deploy live trading
deploy_live_trading() {
    print_warning "LIVE TRADING MODE - This will execute real trades!"
    read -p "Are you sure you want to continue? (yes/no): " confirm
    
    if [[ "$confirm" != "yes" ]]; then
        print_status "Live trading deployment cancelled"
        exit 0
    fi
    
    print_status "Deploying live trading..."

    for required in ALPACA_BASE_URL ALPACA_API_KEY ALPACA_SECRET_KEY; do
        if [[ -z "${!required}" ]]; then
            print_error "Missing required live trading environment variable: $required"
            exit 1
        fi
    done
    
    # Update config for live trading
    python -c "
import yaml
with open('$CONFIG_PATH', 'r') as f:
    config = yaml.safe_load(f)
config['trading']['mode'] = 'live'
config['development']['dry_run'] = False
with open('$CONFIG_PATH', 'w') as f:
    yaml.dump(config, f, default_flow_style=False)
"
    
    python -m src.main --mode=live --config="$CONFIG_PATH"
}

# Function to run backtest
run_backtest() {
    print_status "Running backtest..."
    
    # Update config for backtest
    python -c "
import yaml
with open('$CONFIG_PATH', 'r') as f:
    config = yaml.safe_load(f)
config['trading']['mode'] = 'backtest'
with open('$CONFIG_PATH', 'w') as f:
    yaml.dump(config, f, default_flow_style=False)
"
    
    # Run backtest
    python -m src.backtest --config="$CONFIG_PATH"
}

# Main deployment logic
main() {
    print_status "Father Daddy Capital Deployment"
    print_status "Mode: $MODE"
    print_status "Config: $CONFIG_PATH"
    echo ""
    
    # Check prerequisites
    check_prerequisites
    
    # Setup environment
    setup_environment
    
    # Run tests if in test mode
    if [[ "$MODE" == "test" ]]; then
        run_tests
        exit 0
    fi
    
    # Deploy based on mode
    case $MODE in
        paper)
            deploy_paper_trading
            ;;
        live)
            deploy_live_trading
            ;;
        backtest)
            run_backtest
            ;;
        *)
            print_error "Unknown mode: $MODE"
            show_usage
            exit 1
            ;;
    esac
}

# Run main function
main "$@"
