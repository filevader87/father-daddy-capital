# src/utils/solana_dex_interface.py

import os
import base64
import time
import requests
from functools import lru_cache
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from solana.rpc.api import Client
from solana.transaction import Transaction
from solana.keypair import Keypair
from solana.rpc.types import TxOpts
from src.utils.logger import get_logger

logger = get_logger(__name__)

class OrderSimulationError(Exception):
    """Custom exception for order simulation failures"""
    pass

class DEXExecutionError(Exception):
    """Custom exception for DEX execution failures"""
    pass

class SolanaDEXInterface:
    def __init__(self, rpc_url: str = None, jupiter_api: str = None, wallet_keypair_path: str = None, slippage_bps: int = 100):
        self.client = Client(os.getenv("SOLANA_RPC_URL", rpc_url or "https://api.mainnet-beta.solana.com"))
        self.jupiter_api = (jupiter_api or "https://quote-api.jup.ag/v4").rstrip("/")
        self.wallet = None
        if wallet_keypair_path:
            try:
                with open(wallet_keypair_path, "r") as f:
                    secret = f.read()
                self.wallet = Keypair.from_secret_key(base64.b64decode(secret))
            except Exception as e:
                logger.warning(f"Failed to load wallet keypair: {e}")
        self.slippage_bps = slippage_bps
        self._price_cache = {}
        self._last_price_update = {}

    @lru_cache(maxsize=100)
    def get_dex_price(self, symbol: str, amount: float = 1.0) -> float:
        """Fetch a quote from Jupiter for amount of input token with caching."""
        cache_key = f"{symbol}_{amount}"
        current_time = datetime.now()
        
        # Check cache
        if cache_key in self._price_cache:
            last_update = self._last_price_update.get(cache_key)
            if last_update and (current_time - last_update) < timedelta(seconds=5):
                return self._price_cache[cache_key]
        
        try:
            in_mint, out_mint = self._parse_symbol(symbol)
            amount_ui = int(amount * 10**6)  # assume USDC/USDT have 6 decimals
            params = {
                "inputMint": in_mint,
                "outputMint": out_mint,
                "amount": amount_ui,
                "slippageBps": self.slippage_bps
            }
            resp = requests.get(f"{self.jupiter_api}/quote", params=params)
            data = resp.json()
            out_amount = data["data"][0]["outAmount"]
            price = out_amount / amount_ui
            
            # Update cache
            self._price_cache[cache_key] = price
            self._last_price_update[cache_key] = current_time
            
            return price
        except Exception as e:
            logger.error(f"Failed to fetch DEX price: {e}")
            raise DEXExecutionError(f"Price fetch failed: {e}")

    def simulate_order(self, symbol: str, qty: float, side: str) -> Dict[str, Any]:
        """Simulate order execution before sending."""
        try:
            # Get quote
            quote = self.get_dex_price(symbol, qty)
            
            # Calculate price impact
            small_qty_price = self.get_dex_price(symbol, qty * 0.1)
            price_impact = abs((quote - small_qty_price) / small_qty_price)
            
            # Estimate gas
            gas_estimate = self._estimate_gas()
            
            return {
                "simulated_price": quote,
                "estimated_gas": gas_estimate,
                "price_impact": price_impact,
                "slippage": self.slippage_bps / 10000,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"Order simulation failed: {e}")
            raise OrderSimulationError(f"Failed to simulate order: {e}")

    def place_dex_order(self, symbol: str, qty: float, side: str, max_retries: int = 3) -> dict:
        """Execute a swap via Jupiter with retry logic."""
        for attempt in range(max_retries):
            try:
                # Simulate order first
                simulation = self.simulate_order(symbol, qty, side)
                
                in_mint, out_mint = self._parse_symbol(symbol, sell=(side.upper()=="SELL"))
                amt_ui = int(qty * 10**6)
                
                # Call swap endpoint
                swap_resp = requests.post(f"{self.jupiter_api}/swap", json={
                    "inputMint": in_mint,
                    "outputMint": out_mint,
                    "amount": amt_ui,
                    "slippageBps": self.slippage_bps,
                    "userPublicKey": str(self.wallet.public_key)
                })
                swap_data = swap_resp.json()
                
                # Validate response
                if "swapTransaction" not in swap_data:
                    raise DEXExecutionError("Invalid swap response")
                
                tx_b64 = swap_data["swapTransaction"]
                executed_price = swap_data["priceImpact"]["price"]
                
                # Deserialize and send transaction
                tx = Transaction.deserialize(base64.b64decode(tx_b64))
                result = self.client.send_raw_transaction(tx.serialize(), opts=TxOpts(skip_preflight=True))
                
                # Wait for confirmation
                self.client.confirm_transaction(result["result"])
                
                return {
                    "tx_sig": result["result"],
                    "price": executed_price,
                    "simulation": simulation
                }
                
            except Exception as e:
                logger.error(f"Order placement attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    raise DEXExecutionError(f"Failed to place order after {max_retries} attempts: {e}")
                time.sleep(2 ** attempt)  # Exponential backoff

    def _estimate_gas(self) -> int:
        """Estimate gas cost for transaction."""
        # This is a simplified estimation. In production, you'd want to use
        # actual gas estimation from the network
        return 5000  # micro-lamports

    def _parse_symbol(self, symbol: str, sell: bool=False) -> Tuple[str, str]:
        """Parse trading pair symbol into mint addresses."""
        base, quote = symbol.split("/")
        mapping = {
            "SOL": os.getenv("SOL_MINT_ADDRESS"),
            "USDC": os.getenv("USDC_MINT_ADDRESS"),
            "USDT": os.getenv("USDT_MINT_ADDRESS")
        }
        in_mint = mapping[base]
        out_mint = mapping[quote]
        if sell:
            return mapping[quote], mapping[base]
        return in_mint, out_mint

# Singleton for easy import
solana_dex_interface = SolanaDEXInterface(
    rpc_url=os.getenv("SOLANA_RPC_URL"),
    jupiter_api=os.getenv("JUPITER_API_URL"),
    wallet_keypair_path=os.getenv("SOLANA_WALLET_KEYPAIR"),
    slippage_bps=int(os.getenv("JUPITER_SLIPPAGE_BPS", 50))
)

