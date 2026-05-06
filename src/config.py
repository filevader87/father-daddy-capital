class TradingConfig:
    """Configuration settings for trading operations."""
    
    # Model flags
    USE_LTC = True
    USE_SWARM = True
    
    # Scheduling
    RISK_REBALANCE_EVERY = 60     # ticks
    REPAIR_CHECK_EVERY = 3600     # seconds
    
    # Risk Management
    MAX_POSITION_SIZE = 0.1
    MAX_LEVERAGE = 3.0
    MAX_DRAWDOWN = 0.2
    MAX_POSITION_RISK = 0.3
    MAX_DAILY_TRADES = 100
    MAX_DAILY_LOSS = 0.1
    MAX_SPREAD = 0.01
    MIN_LIQUIDITY = 100000
    
    # Trading Engine
    MAX_PARALLEL_TRADES = 5
    MAX_SLIPPAGE = 0.001
    
    # Risk Alerts
    VAR_THRESHOLD = 0.05
    LIQUIDITY_THRESHOLD = 0.1
    CORRELATION_THRESHOLD = 0.7
    
    # API Configuration
    API_TIMEOUT = 30
    API_RETRY_DELAY = 5
    MAX_API_RETRIES = 3
    
    # Logging
    LOG_LEVEL = "INFO"
    LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    @classmethod
    def load_from_file(cls):
        """Load configuration from file if needed."""
        return cls()

    # Model repair settings
    REPAIR_THRESHOLD = 0.1            # standard deviation threshold for PnL
    WEIGHT_MAX = 2.0                  # maximum absolute value for model weights 