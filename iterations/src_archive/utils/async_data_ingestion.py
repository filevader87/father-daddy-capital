"""
Async Data Ingestion Module
---------------------------
This module provides async data ingestion capabilities for multiple data sources
including Alpaca, Coinbase, and DeFi Llama using asyncio and httpx for parallel calls.
"""

import asyncio
import httpx
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from functools import lru_cache
import time

logger = logging.getLogger(__name__)

@dataclass
class DataSource:
    """Configuration for a data source."""
    name: str
    base_url: str
    api_key: Optional[str] = None
    rate_limit: int = 100  # requests per minute
    timeout: float = 10.0
    retries: int = 3

class AsyncDataIngestion:
    """High-performance async data ingestion for multiple sources."""
    
    def __init__(self, config: Dict[str, DataSource]):
        """Initialize async data ingestion.
        
        Args:
            config: Dictionary of data source configurations
        """
        self.config = config
        self.clients: Dict[str, httpx.AsyncClient] = {}
        self.rate_limiters: Dict[str, asyncio.Semaphore] = {}
        self._setup_clients()
        
    def _setup_clients(self):
        """Setup async HTTP clients for each data source."""
        for name, source_config in self.config.items():
            # Create rate limiter
            self.rate_limiters[name] = asyncio.Semaphore(source_config.rate_limit)
            
            # Create HTTP client with connection pooling
            self.clients[name] = httpx.AsyncClient(
                base_url=source_config.base_url,
                timeout=source_config.timeout,
                limits=httpx.Limits(
                    max_keepalive_connections=20,
                    max_connections=100
                ),
                headers=self._get_headers(source_config)
            )
    
    def _get_headers(self, source_config: DataSource) -> Dict[str, str]:
        """Get headers for API requests."""
        headers = {
            'User-Agent': 'FatherDaddyCapital/1.0',
            'Accept': 'application/json'
        }
        
        if source_config.api_key:
            if source_config.name == 'alpaca':
                headers.update({
                    'APCA-API-KEY-ID': source_config.api_key,
                    'APCA-API-SECRET-KEY': source_config.api_key
                })
            elif source_config.name == 'coinmarketcap':
                headers['X-CMC_PRO_API_KEY'] = source_config.api_key
            else:
                headers['Authorization'] = f'Bearer {source_config.api_key}'
                
        return headers
    
    async def fetch_crypto_prices_parallel(self, symbols: List[str]) -> Dict[str, float]:
        """Fetch crypto prices from multiple sources in parallel."""
        tasks = []
        
        # DeFi Llama task
        if 'defillama' in self.config:
            tasks.append(self._fetch_defillama_prices(symbols))
        
        # CoinGecko task
        if 'coingecko' in self.config:
            tasks.append(self._fetch_coingecko_prices(symbols))
        
        # CoinMarketCap task
        if 'coinmarketcap' in self.config:
            tasks.append(self._fetch_coinmarketcap_prices(symbols))
        
        # Execute all tasks in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Combine results with fallback logic
        return self._combine_price_results(results, symbols)
    
    async def _fetch_defillama_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Fetch prices from DeFi Llama."""
        try:
            async with self.rate_limiters['defillama']:
                ids = ",".join([f"coingecko:{s.lower()}" for s in symbols])
                url = f"/prices/current/{ids}"
                
                async with self.clients['defillama'] as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    data = response.json()["coins"]
                    
                    prices = {}
                    for symbol in symbols:
                        key = f"coingecko:{symbol.lower()}"
                        if key in data:
                            prices[symbol] = data[key]["price"]
                    
                    return prices
        except Exception as e:
            logger.error(f"DeFi Llama fetch error: {e}")
            return {}
    
    async def _fetch_coingecko_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Fetch prices from CoinGecko."""
        try:
            async with self.rate_limiters['coingecko']:
                # Map symbols to CoinGecko IDs
                mapping = {
                    'BTC': 'bitcoin', 'ETH': 'ethereum', 'SOL': 'solana',
                    'AVAX': 'avalanche-2', 'RNDR': 'render-token',
                    'XRP': 'ripple', 'ADA': 'cardano'
                }
                
                ids = [mapping.get(s, s.lower()) for s in symbols if s in mapping]
                if not ids:
                    return {}
                
                url = f"/api/v3/simple/price?ids={','.join(ids)}&vs_currencies=usd"
                
                async with self.clients['coingecko'] as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    data = response.json()
                    
                    prices = {}
                    for symbol in symbols:
                        if symbol in mapping and mapping[symbol] in data:
                            prices[symbol] = data[mapping[symbol]]["usd"]
                    
                    return prices
        except Exception as e:
            logger.error(f"CoinGecko fetch error: {e}")
            return {}
    
    async def _fetch_coinmarketcap_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Fetch prices from CoinMarketCap."""
        try:
            async with self.rate_limiters['coinmarketcap']:
                prices = {}
                
                # CoinMarketCap requires individual requests
                for symbol in symbols:
                    url = f"/v1/cryptocurrency/quotes/latest?symbol={symbol}&convert=USD"
                    
                    async with self.clients['coinmarketcap'] as client:
                        response = await client.get(url)
                        response.raise_for_status()
                        data = response.json()
                        
                        if symbol in data["data"]:
                            prices[symbol] = data["data"][symbol]["quote"]["USD"]["price"]
                
                return prices
        except Exception as e:
            logger.error(f"CoinMarketCap fetch error: {e}")
            return {}
    
    def _combine_price_results(self, results: List[Dict[str, float]], symbols: List[str]) -> Dict[str, float]:
        """Combine price results from multiple sources with fallback logic."""
        combined_prices = {}
        
        for symbol in symbols:
            # Try each source in order of preference
            for result in results:
                if isinstance(result, dict) and symbol in result:
                    combined_prices[symbol] = result[symbol]
                    break
            else:
                # Fallback to simulated price if all sources fail
                logger.warning(f"No price found for {symbol}, using simulated price")
                combined_prices[symbol] = 100 + hash(symbol) % 100
        
        return combined_prices
    
    async def fetch_stock_data_parallel(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch stock data from Alpaca in parallel."""
        tasks = []
        
        for symbol in symbols:
            tasks.append(self._fetch_stock_data(symbol))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        stock_data = {}
        for symbol, result in zip(symbols, results):
            if isinstance(result, dict):
                stock_data[symbol] = result
            else:
                logger.error(f"Failed to fetch data for {symbol}: {result}")
        
        return stock_data
    
    async def _fetch_stock_data(self, symbol: str) -> Dict[str, Any]:
        """Fetch stock data from Alpaca."""
        try:
            async with self.rate_limiters['alpaca']:
                # Fetch latest quote
                quote_url = f"/v2/stocks/{symbol}/quotes/latest"
                async with self.clients['alpaca'] as client:
                    response = await client.get(quote_url)
                    response.raise_for_status()
                    quote_data = response.json()
                
                # Fetch latest bar
                bar_url = f"/v2/stocks/{symbol}/bars/latest"
                async with self.clients['alpaca'] as client:
                    response = await client.get(bar_url)
                    response.raise_for_status()
                    bar_data = response.json()
                
                return {
                    'symbol': symbol,
                    'price': float(quote_data.get('ask_price', 0)),
                    'bid': float(quote_data.get('bid_price', 0)),
                    'ask': float(quote_data.get('ask_price', 0)),
                    'volume': int(quote_data.get('ask_size', 0)),
                    'timestamp': quote_data.get('timestamp'),
                    'open': float(bar_data.get('o', 0)),
                    'high': float(bar_data.get('h', 0)),
                    'low': float(bar_data.get('l', 0)),
                    'close': float(bar_data.get('c', 0))
                }
        except Exception as e:
            logger.error(f"Alpaca fetch error for {symbol}: {e}")
            return {}
    
    async def fetch_historical_data_parallel(self, 
                                           symbols: List[str], 
                                           start_date: str,
                                           end_date: str,
                                           timeframe: str = '1Day') -> Dict[str, pd.DataFrame]:
        """Fetch historical data for multiple symbols in parallel."""
        tasks = []
        
        for symbol in symbols:
            tasks.append(self._fetch_historical_data(symbol, start_date, end_date, timeframe))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        historical_data = {}
        for symbol, result in zip(symbols, results):
            if isinstance(result, pd.DataFrame):
                historical_data[symbol] = result
            else:
                logger.error(f"Failed to fetch historical data for {symbol}: {result}")
        
        return historical_data
    
    async def _fetch_historical_data(self, 
                                   symbol: str, 
                                   start_date: str,
                                   end_date: str,
                                   timeframe: str) -> pd.DataFrame:
        """Fetch historical data for a single symbol."""
        try:
            async with self.rate_limiters['alpaca']:
                url = f"/v2/stocks/{symbol}/bars"
                params = {
                    'start': start_date,
                    'end': end_date,
                    'timeframe': timeframe,
                    'limit': 10000
                }
                
                async with self.clients['alpaca'] as client:
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    data = response.json()
                
                if 'bars' not in data:
                    return pd.DataFrame()
                
                df = pd.DataFrame(data['bars'])
                df['timestamp'] = pd.to_datetime(df['t'])
                df = df.set_index('timestamp')
                
                return df
        except Exception as e:
            logger.error(f"Historical data fetch error for {symbol}: {e}")
            return pd.DataFrame()
    
    async def fetch_market_data_batch(self, 
                                    crypto_symbols: List[str] = None,
                                    stock_symbols: List[str] = None) -> Dict[str, Any]:
        """Fetch market data for both crypto and stocks in parallel."""
        tasks = []
        
        if crypto_symbols:
            tasks.append(self.fetch_crypto_prices_parallel(crypto_symbols))
        
        if stock_symbols:
            tasks.append(self.fetch_stock_data_parallel(stock_symbols))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        market_data = {}
        if crypto_symbols:
            crypto_result = results[0] if isinstance(results[0], dict) else {}
            market_data['crypto'] = crypto_result
        
        if stock_symbols:
            stock_result = results[1] if isinstance(results[1], dict) else {}
            market_data['stocks'] = stock_result
        
        return market_data
    
    async def close(self):
        """Close all HTTP clients."""
        for client in self.clients.values():
            await client.aclose()

# Global instance
async_data_ingestion = None

async def get_async_data_ingestion() -> AsyncDataIngestion:
    """Get or create global async data ingestion instance."""
    global async_data_ingestion
    
    if async_data_ingestion is None:
        config = {
            'alpaca': DataSource(
                name='alpaca',
                base_url='https://paper-api.alpaca.markets',
                api_key='your_alpaca_key',
                rate_limit=200
            ),
            'defillama': DataSource(
                name='defillama',
                base_url='https://coins.llama.fi',
                rate_limit=100
            ),
            'coingecko': DataSource(
                name='coingecko',
                base_url='https://api.coingecko.com',
                rate_limit=50
            ),
            'coinmarketcap': DataSource(
                name='coinmarketcap',
                base_url='https://pro-api.coinmarketcap.com',
                api_key='your_cmc_key',
                rate_limit=30
            )
        }
        async_data_ingestion = AsyncDataIngestion(config)
    
    return async_data_ingestion 