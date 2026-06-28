import os
import time
from typing import Optional, Tuple, Dict, Any
from datetime import datetime, timedelta
import json
import numpy as np

from src.utils.api_manager import api_manager
from src.utils.config_loader import config_loader
from src.utils.solana_dex_interface import solana_dex_interface, DEXExecutionError, OrderSimulationError
from src.logger import logger
from tenacity import retry, wait_exponential, stop_after_attempt

# Symbols for price and order routing
CRYPTO_SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "RNDRUSD", "XRPUSD", "ADAUSD"]
DEX_SOLANA_SYMBOLS = config_loader.get("dex.solana.symbols")

LOG_DIR = "logs"
TRADE_HISTORY_DIR = "logs/trade_history"

def ensure_directories():
    """Ensure required directories exist"""
    for directory in [LOG_DIR, TRADE_HISTORY_DIR]:
        if not os.path.exists(directory):
            os.makedirs(directory)

def log_trade(symbol: str, qty: float, side: str, price: float, simulated: bool = False, 
             metadata: Optional[Dict[str, Any]] = None) -> None:
    """Log trade execution details with enhanced metadata"""
    ensure_directories()
    
    trade_type = "SIMULATED" if simulated else "LIVE"
    timestamp = datetime.now()
    
    # Log to daily file
    log_file = os.path.join(
        LOG_DIR,
        f"{trade_type}_{symbol}_{timestamp.strftime('%Y-%m-%d')}.log"
    )
    
    log_entry = {
        "timestamp": timestamp.isoformat(),
        "type": trade_type,
        "symbol": symbol,
        "side": side.upper(),
        "quantity": qty,
        "price": price,
        "metadata": metadata or {}
    }
    
    with open(log_file, "a") as f:
        f.write(f"{log_entry}\n")
    
    # Log to trade history
    history_file = os.path.join(
        TRADE_HISTORY_DIR,
        f"{symbol}_history.json"
    )
    
    try:
        if os.path.exists(history_file):
            with open(history_file, "r") as f:
                history = json.load(f)
        else:
            history = []
            
        history.append(log_entry)
        
        with open(history_file, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to update trade history: {e}")

@retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(5))
@api_manager.rate_limit("alpaca_api", 200)
def place_order(
    symbol: str,
    qty: float,
    side: str,
    type: str = "market",
    time_in_force: str = "gtc",
    max_retries: int = 3
) -> dict:
    """Place an order with enhanced error handling and rate limiting"""
    symbol = symbol.upper()
    metadata = {}

    # Solana DEX execution
    if symbol in DEX_SOLANA_SYMBOLS:
        try:
            # Simulate order first
            simulation = solana_dex_interface.simulate_order(symbol, qty, side)
            metadata["simulation"] = simulation
            
            # Check price impact
            if simulation["price_impact"] > 0.01:  # 1% price impact threshold
                logger.warning(f"High price impact detected: {simulation['price_impact']:.2%}")
            
            # Execute order
            res = solana_dex_interface.place_dex_order(symbol, qty, side, max_retries)
            price = res.get("price", 0.0)
            
            # Log successful execution
            log_trade(symbol, qty, side, price, simulated=False, metadata=metadata)
            return {"status": "dex_solana_executed", **res}
            
        except OrderSimulationError as e:
            logger.error(f"Order simulation failed: {e}")
            log_trade(symbol, qty, side, 0.0, simulated=True, 
                     metadata={"error": str(e), "type": "simulation_failed"})
            return {"status": "dex_solana_simulation_failed", "error": str(e)}
            
        except DEXExecutionError as e:
            logger.error(f"DEX execution failed: {e}")
            log_trade(symbol, qty, side, 0.0, simulated=True,
                     metadata={"error": str(e), "type": "execution_failed"})
            return {"status": "dex_solana_failed", "error": str(e)}
            
        except Exception as e:
            logger.error(f"Unexpected error in DEX execution: {e}")
            log_trade(symbol, qty, side, 0.0, simulated=True,
                     metadata={"error": str(e), "type": "unexpected_error"})
            return {"status": "dex_solana_error", "error": str(e)}

    # Centralized (Alpaca) execution
    price = get_latest_price(symbol)
    if price is None:
        raise Exception("Unable to fetch price for order placement.")

    try:
        headers = {
            "APCA-API-KEY-ID": api_manager.get_api_key("alpaca_api"),
            "APCA-API-SECRET-KEY": api_manager.get_api_key("alpaca_secret")
        }
        url = f"{os.getenv('ALPACA_BASE_URL')}/v2/orders"
        order_data = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": type,
            "time_in_force": time_in_force
        }
        response = api_manager.make_request(
            "alpaca_api",
            url,
            method="POST",
            headers=headers,
            params=order_data
        )
        result = response.json()
        log_trade(symbol, qty, side, price, simulated=False, metadata={"order_id": result.get("id")})
        return result
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        log_trade(symbol, qty, side, price, simulated=True,
                 metadata={"error": str(e), "type": "centralized_execution_failed"})
        return {"status": "simulated", "symbol": symbol, "side": side, "qty": qty, "price": price}


@retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(5))
@api_manager.rate_limit("alpaca_api", 200)
def get_position(symbol: str) -> Optional[dict]:
    """Get current position with proper error handling"""
    try:
        headers = {
            "APCA-API-KEY-ID": api_manager.get_api_key("alpaca_api"),
            "APCA-API-SECRET-KEY": api_manager.get_api_key("alpaca_secret")
        }
        url = f"{os.getenv('ALPACA_BASE_URL')}/v2/positions/{symbol}"
        response = api_manager.make_request("alpaca_api", url, headers=headers)
        return response.json()
    except Exception as e:
        logger.error("Failed to get position", extra={"error": str(e)})
        return None


@retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(5))
def get_latest_price(symbol: str) -> Optional[float]:
    """Get latest price with enhanced error handling and fallback"""
    symbol = symbol.upper()

    # Solana DEX price
    if symbol in DEX_SOLANA_SYMBOLS:
        try:
            price = solana_dex_interface.get_dex_price(symbol)
            logger.info(f"Price for {symbol} fetched from [SolanaDEX]: ${price}")
            return price
        except Exception as e:
            logger.error(f"Failed to fetch DEX price for {symbol}: {e}")
            return None

    # Centralized crypto
    if symbol in CRYPTO_SYMBOLS:
        price, source = get_crypto_price(symbol)
        if price:
            logger.info(f"Price for {symbol} fetched from [{source}]: ${price}")
        else:
            logger.warning(f"Price fetch failed for {symbol}. Using fallback.")
        return price

    # Centralized stocks
    try:
        headers = {
            "APCA-API-KEY-ID": api_manager.get_api_key("alpaca_api"),
            "APCA-API-SECRET-KEY": api_manager.get_api_key("alpaca_secret")
        }
        url = f"{os.getenv('ALPACA_BASE_URL')}/v2/stocks/{symbol}/quotes/latest"
        response = api_manager.make_request("alpaca_api", url, headers=headers)
        return float(response.json().get("ask_price", 0))
    except Exception as e:
        logger.error(f"Failed to get stock price: {e}")
        return None


@retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(5))
@api_manager.rate_limit("crypto", 30)
def get_crypto_price(symbol: str) -> Tuple[float, str]:
    """Get crypto price from multiple sources with enhanced fallback"""
    mapping = {
        "BTCUSD": "bitcoin",
        "ETHUSD": "ethereum",
        "SOLUSD": "solana",
        "AVAXUSD": "avalanche-2",
        "RNDRUSD": "render-token",
        "XRPUSD": "ripple",
        "ADAUSD": "cardano"
    }
    cmc_mapping = {
        "BTCUSD": "BTC",
        "ETHUSD": "ETH",
        "SOLUSD": "SOL",
        "AVAXUSD": "AVAX",
        "RNDRUSD": "RNDR",
        "XRPUSD": "XRP",
        "ADAUSD": "ADA"
    }

    # Try CoinGecko first
    coingecko_id = mapping.get(symbol)
    if coingecko_id:
        try:
            url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd"
            response = api_manager.make_request("coingecko", url)
            return float(response.json()[coingecko_id]["usd"]), "CG"
        except Exception as e:
            logger.warning(f"CoinGecko price fetch failed: {e}")

    # Try CoinMarketCap as fallback
    cmc_symbol = cmc_mapping.get(symbol)
    if cmc_symbol and api_manager.get_api_key("coinmarketcap"):
        try:
            headers = {"X-CMC_PRO_API_KEY": api_manager.get_api_key("coinmarketcap")}
            url = f"https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?symbol={cmc_symbol}&convert=USD"
            response = api_manager.make_request("coinmarketcap", url, headers=headers)
            return float(response.json()["data"][cmc_symbol]["quote"]["USD"]["price"]), "CMC"
        except Exception as e:
            logger.warning(f"CoinMarketCap price fetch failed: {e}")

    # Fallback simulated price
    logger.warning(f"All price sources failed for {symbol}, using simulated price")
    return float(100 + hash(symbol) % 100), "SIM"


@retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(5))
def get_market_data(symbol: str, lookback: int = 100) -> Dict[str, Any]:
    """Get comprehensive market data for a symbol."""
    symbol = symbol.upper()
    now = datetime.now()
    
    # Initialize with basic data
    data = {
        "symbol": symbol,
        "timestamp": now.isoformat(),
        "timestep": int(time.time()),
        "price": 0.0,
        "bid": 0.0,
        "ask": 0.0,
        "volume": 0.0,
        "volume_24h": 0.0,
        "market_cap": 0.0,
        "volatility": 0.0,
        "volatility_24h": 0.0,
        "prices": np.zeros(lookback),
        "volumes": np.zeros(lookback),
        "prediction": 0.0
    }
    
    try:
        # Get current price
        price = get_latest_price(symbol)
        if price is None:
            return data
            
        data["price"] = price
        data["bid"] = price * 0.9999  # Simulated bid/ask spread
        data["ask"] = price * 1.0001
        
        # Simulate historical data for testing
        base = price
        timestamps = []
        prices = []
        volumes = []
        
        for i in range(lookback):
            t = now - timedelta(minutes=i)
            p = base * (1 + np.random.normal(0, 0.001))  # 0.1% standard deviation
            v = np.random.uniform(100, 1000)  # Random volume between 100 and 1000
            
            timestamps.append(t)
            prices.append(p)
            volumes.append(v)
            
        prices = np.array(prices)
        volumes = np.array(volumes)
        
        # Calculate metrics
        data["prices"] = prices
        data["volumes"] = volumes
        data["volume"] = volumes[0]
        data["volume_24h"] = np.sum(volumes[:1440]) if len(volumes) >= 1440 else np.sum(volumes)
        data["volatility"] = np.std(np.diff(prices[:30])/prices[:-1][:30]) if len(prices) > 30 else 0
        data["volatility_24h"] = np.std(np.diff(prices)/prices[:-1]) if len(prices) > 1 else 0
        data["market_cap"] = price * 1_000_000  # Simulated market cap
        
        # Add prediction (simulated)
        data["prediction"] = np.random.uniform(-1, 1)
        
    except Exception as e:
        logger.error(f"Error fetching market data for {symbol}: {e}")
        
    return data

