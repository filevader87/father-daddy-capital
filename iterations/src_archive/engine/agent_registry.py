
# src/engine/agent_registry.py

from src.agents.short_term.crypto_aets import CryptoAETS
from src.agents.short_term.stock_aets import StockAETS

AGENT_REGISTRY = {
    "BTCUSD": CryptoAETS,
    "ETHUSD": CryptoAETS,
    "SOLUSD": CryptoAETS,
    "AVAXUSD": CryptoAETS,
    "RNDRUSD": CryptoAETS,
    "XRPUSD": CryptoAETS,
    "ADAUSD": CryptoAETS,
    "AAPL": StockAETS,
    "MSFT": StockAETS,
    "NVDA": StockAETS,
    "TSLA": StockAETS,
}
