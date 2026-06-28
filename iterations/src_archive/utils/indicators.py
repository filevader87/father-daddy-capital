import yfinance as yf
import pandas as pd
import numpy as np
import random
from datetime import datetime, timedelta
import talib

def compute_rsi(close_series: pd.Series, timeperiod: int = 14) -> float:
    """Compute Relative Strength Index (RSI)"""
    try:
        arr = close_series.values.astype(np.float64)
        rsi = talib.RSI(arr, timeperiod=timeperiod)
        return float(rsi[-1])
    except ImportError:
        delta = close_series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=timeperiod).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=timeperiod).mean()
        rs = gain / loss
        return float(100 - (100 / (1 + rs))[-1])

def compute_macd(prices, fast_period=12, slow_period=26, signal_period=9):
    """Compute MACD (Moving Average Convergence Divergence)"""
    try:
        arr = prices.values.astype(np.float64)
        macd, signal, hist = talib.MACD(arr, fastperiod=fast_period, slowperiod=slow_period, signalperiod=signal_period)
        return pd.Series(macd, index=prices.index), pd.Series(signal, index=prices.index)
    except ImportError:
        exp1 = prices.ewm(span=fast_period, adjust=False).mean()
        exp2 = prices.ewm(span=slow_period, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=signal_period, adjust=False).mean()
        return macd, signal

def compute_obv(prices, volumes):
    """Compute On-Balance Volume (OBV)"""
    try:
        arr = prices.values.astype(np.float64)
        vol_arr = volumes.values.astype(np.float64)
        obv = talib.OBV(arr, vol_arr)
        return pd.Series(obv, index=prices.index)
    except ImportError:
        obv = np.zeros(len(prices))
        obv[0] = volumes[0]
        
        for i in range(1, len(prices)):
            if prices[i] > prices[i-1]:
                obv[i] = obv[i-1] + volumes[i]
            elif prices[i] < prices[i-1]:
                obv[i] = obv[i-1] - volumes[i]
            else:
                obv[i] = obv[i-1]
        
        return pd.Series(obv, index=prices.index)

def compute_volatility(prices, period=20):
    """Compute Volatility using standard deviation of returns"""
    try:
        # ATR requires high, low, close prices
        # Since we only have close prices, we'll use them for all three
        arr = prices.values.astype(np.float64)
        atr = talib.ATR(arr, arr, arr, timeperiod=period)
        return pd.Series(atr, index=prices.index)
    except ImportError:
        returns = prices.pct_change()
        return returns.rolling(window=period).std() * np.sqrt(252)  # Annualized volatility

def compute_vwap(prices, volumes):
    """Compute Volume Weighted Average Price (VWAP)"""
    return (prices * volumes).cumsum() / volumes.cumsum()

def compute_volume_profile(prices, volumes, bins=20):
    """Compute Volume Profile"""
    price_bins = pd.cut(prices, bins=bins)
    return volumes.groupby(price_bins, observed=True).sum()

def compute_ichimoku(prices, conversion_period=9, base_period=26, lagging_period=52, displacement=26):
    """Compute Ichimoku Cloud indicators"""
    try:
        high = prices.rolling(window=conversion_period).max().values.astype(np.float64)
        low = prices.rolling(window=conversion_period).min().values.astype(np.float64)
        close = prices.values.astype(np.float64)
        
        conversion_line = talib.STOCH(high, low, close, fastk_period=conversion_period, slowk_period=1, slowd_period=1)[0]
        base_line = talib.STOCH(high, low, close, fastk_period=base_period, slowk_period=1, slowd_period=1)[0]
        leading_span_a = (conversion_line + base_line) / 2
        leading_span_b = talib.STOCH(high, low, close, fastk_period=lagging_period, slowk_period=1, slowd_period=1)[0]
        
        return (pd.Series(conversion_line, index=prices.index),
                pd.Series(base_line, index=prices.index),
                pd.Series(leading_span_a, index=prices.index),
                pd.Series(leading_span_b, index=prices.index))
    except ImportError:
        conversion_line = (prices.rolling(window=conversion_period).max() + 
                          prices.rolling(window=conversion_period).min()) / 2
        base_line = (prices.rolling(window=base_period).max() + 
                    prices.rolling(window=base_period).min()) / 2
        leading_span_a = (conversion_line + base_line) / 2
        leading_span_b = (prices.rolling(window=lagging_period).max() + 
                         prices.rolling(window=lagging_period).min()) / 2
        return conversion_line, base_line, leading_span_a, leading_span_b

def compute_fibonacci(prices, lookback=20):
    """Compute Fibonacci retracement levels"""
    high = prices.rolling(window=lookback).max()
    low = prices.rolling(window=lookback).min()
    diff = high - low
    
    levels = {
        '0%': low,
        '23.6%': high - diff * 0.236,
        '38.2%': high - diff * 0.382,
        '50%': high - diff * 0.5,
        '61.8%': high - diff * 0.618,
        '100%': high
    }
    return levels

def fetch_stock_indicators(symbol, period="30d", interval="1h"):
    try:
        data = yf.download(symbol, period=period, interval=interval, progress=False)
        
        try:
            arr = data['Close'].values.astype(np.float64)
            data['RSI'] = talib.RSI(arr, timeperiod=14)
            macd, signal, hist = talib.MACD(arr, fastperiod=12, slowperiod=26, signalperiod=9)
            data['MACD'] = macd
            data['Signal'] = signal
            data['MACD_Hist'] = hist
        except ImportError:
            delta = data['Close'].diff()
            up = delta.clip(lower=0)
            down = -1 * delta.clip(upper=0)
            avg_gain = up.rolling(window=14).mean()
            avg_loss = down.rolling(window=14).mean()
            rs = avg_gain / avg_loss
            data['RSI'] = 100 - (100 / (1 + rs))

            exp1 = data['Close'].ewm(span=12, adjust=False).mean()
            exp2 = data['Close'].ewm(span=26, adjust=False).mean()
            data['MACD'] = exp1 - exp2
            data['Signal'] = data['MACD'].ewm(span=9, adjust=False).mean()
            data['MACD_Hist'] = data['MACD'] - data['Signal']

        return data[['Close', 'RSI', 'MACD', 'MACD_Hist']].dropna().tail(1)
    except Exception as e:
        print(f"Error fetching stock data for {symbol}: {e}")
        return None

def fetch_crypto_indicators(symbol):
    try:
        # Simulate 100 time steps of crypto prices with noise
        base_price = random.uniform(50, 2000)
        prices = [base_price + np.random.normal(0, 2) for _ in range(100)]
        df = pd.DataFrame(prices, columns=["Close"])
        
        try:
            arr = df['Close'].values.astype(np.float64)
            df['RSI'] = talib.RSI(arr, timeperiod=14)
            macd, signal, hist = talib.MACD(arr, fastperiod=12, slowperiod=26, signalperiod=9)
            df['MACD'] = macd
            df['Signal'] = signal
            df['MACD_Hist'] = hist
        except ImportError:
            delta = df['Close'].diff()
            up = delta.clip(lower=0)
            down = -1 * delta.clip(upper=0)
            avg_gain = up.rolling(window=14).mean()
            avg_loss = down.rolling(window=14).mean()
            rs = avg_gain / avg_loss
            df['RSI'] = 100 - (100 / (1 + rs))

            exp1 = df['Close'].ewm(span=12, adjust=False).mean()
            exp2 = df['Close'].ewm(span=26, adjust=False).mean()
            df['MACD'] = exp1 - exp2
            df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
            df['MACD_Hist'] = df['MACD'] - df['Signal']

        return df[['Close', 'RSI', 'MACD', 'MACD_Hist']].dropna().tail(1)
    except Exception as e:
        print(f"Error simulating crypto data for {symbol}: {e}")
        return None 