"""YFinance-backed market data provider for paper trading."""

from __future__ import annotations

from typing import Dict, Iterable

import pandas as pd
import yfinance as yf


class YFinanceMarketDataProvider:
    """Fetch and normalize OHLCV data for configured symbols."""

    CRYPTO_SUFFIXES = ("USD", "USDT")

    def __init__(self, symbols: Iterable[str]):
        self.symbols = list(symbols)

    def to_provider_symbol(self, symbol: str) -> str:
        normalized = symbol.upper().replace("/", "")
        for suffix in self.CRYPTO_SUFFIXES:
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                base = normalized[: -len(suffix)]
                quote = "USD" if suffix == "USDT" else suffix
                return f"{base}-{quote}"
        return symbol.upper()

    def fetch_symbol(self, symbol: str, period: str = "7d", interval: str = "1h") -> pd.DataFrame:
        provider_symbol = self.to_provider_symbol(symbol)
        history = yf.Ticker(provider_symbol).history(period=period, interval=interval)
        if history.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        data = history.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        return data[["open", "high", "low", "close", "volume"]].dropna()

    def fetch_all(self, period: str = "7d", interval: str = "1h") -> Dict[str, pd.DataFrame]:
        return {
            symbol: data
            for symbol in self.symbols
            if not (data := self.fetch_symbol(symbol, period=period, interval=interval)).empty
        }
