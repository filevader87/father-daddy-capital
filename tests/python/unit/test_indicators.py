import pytest
import pandas as pd
import numpy as np
from src.utils.indicators import (
    compute_rsi,
    compute_macd,
    compute_obv,
    compute_volatility,
    compute_vwap,
    compute_volume_profile,
    compute_ichimoku,
    compute_fibonacci,
    fetch_stock_indicators,
    fetch_crypto_indicators
)

def test_compute_rsi():
    # Create a longer price series to ensure enough data for RSI calculation
    prices = pd.Series([
        100, 102, 104, 103, 105, 107, 106, 108, 110, 109,
        111, 113, 112, 114, 116, 115, 117, 119, 118, 120,
        122, 121, 123, 125, 124, 126, 128, 127, 129, 130
    ])
    rsi = compute_rsi(prices)
    assert isinstance(rsi, float)
    assert 0 <= rsi <= 100

def test_compute_macd():
    # Create a simple price series
    prices = pd.Series([100, 102, 104, 103, 105, 107, 106, 108, 110, 109])
    macd, signal = compute_macd(prices)
    assert isinstance(macd, pd.Series)
    assert isinstance(signal, pd.Series)
    assert len(macd) == len(prices)
    assert len(signal) == len(prices)

def test_compute_obv():
    # Create simple price and volume series
    prices = pd.Series([100, 102, 104, 103, 105])
    volumes = pd.Series([1000, 2000, 1500, 3000, 2500])
    obv = compute_obv(prices, volumes)
    assert isinstance(obv, pd.Series)
    assert len(obv) == len(prices)

def test_compute_volatility():
    # Create a simple price series
    prices = pd.Series([100, 102, 104, 103, 105, 107, 106, 108, 110, 109])
    volatility = compute_volatility(prices)
    assert isinstance(volatility, pd.Series)
    assert len(volatility) == len(prices)

def test_compute_vwap():
    # Create simple price and volume series
    prices = pd.Series([100, 102, 104, 103, 105])
    volumes = pd.Series([1000, 2000, 1500, 3000, 2500])
    vwap = compute_vwap(prices, volumes)
    assert isinstance(vwap, pd.Series)
    assert len(vwap) == len(prices)

def test_compute_volume_profile():
    # Create simple price and volume series
    prices = pd.Series([100, 102, 104, 103, 105])
    volumes = pd.Series([1000, 2000, 1500, 3000, 2500])
    profile = compute_volume_profile(prices, volumes)
    assert isinstance(profile, pd.Series)

def test_compute_ichimoku():
    # Create a simple price series
    prices = pd.Series([100, 102, 104, 103, 105, 107, 106, 108, 110, 109])
    conversion, base, span_a, span_b = compute_ichimoku(prices)
    assert isinstance(conversion, pd.Series)
    assert isinstance(base, pd.Series)
    assert isinstance(span_a, pd.Series)
    assert isinstance(span_b, pd.Series)
    assert len(conversion) == len(prices)
    assert len(base) == len(prices)
    assert len(span_a) == len(prices)
    assert len(span_b) == len(prices)

def test_compute_fibonacci():
    # Create a simple price series
    prices = pd.Series([100, 102, 104, 103, 105, 107, 106, 108, 110, 109])
    levels = compute_fibonacci(prices)
    assert isinstance(levels, dict)
    assert all(key in levels for key in ['0%', '23.6%', '38.2%', '50%', '61.8%', '100%'])

@pytest.mark.skip(reason="Requires internet connection and market hours")
def test_fetch_stock_indicators():
    # Test with a known stock symbol
    result = fetch_stock_indicators("AAPL", period="1d", interval="1h")
    assert result is not None
    assert isinstance(result, pd.DataFrame)
    assert all(col in result.columns for col in ['Close', 'RSI', 'MACD', 'MACD_Hist'])

def test_fetch_crypto_indicators():
    # Test with a crypto symbol
    result = fetch_crypto_indicators("BTC-USD")
    assert result is not None
    assert isinstance(result, pd.DataFrame)
    assert all(col in result.columns for col in ['Close', 'RSI', 'MACD', 'MACD_Hist']) 