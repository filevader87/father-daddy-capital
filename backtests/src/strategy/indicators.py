import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler

class EnhancedIndicators:
    def __init__(self):
        self.ml_model = RandomForestClassifier(n_estimators=100, random_state=42)
        self.scaler = StandardScaler()
        
    def calculate_vwap(self, df, window=20, sensitivity=0.005):
        """Calculate VWAP with sensitivity threshold."""
        df['vwap'] = (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()
        df['vwap_signal'] = ((df['close'] - df['vwap']) / df['vwap']).abs() > sensitivity
        return df
    
    def calculate_adx(self, df, period=14, threshold=30):
        """Calculate ADX with enhanced threshold."""
        high = df['high']
        low = df['low']
        close = df['close']
        
        # Calculate TR and DM
        df['TR'] = np.maximum(high - low, np.maximum(abs(high - close.shift(1)), abs(low - close.shift(1))))
        df['+DM'] = np.where((high - high.shift(1)) > (low.shift(1) - low), np.maximum(high - high.shift(1), 0), 0)
        df['-DM'] = np.where((low.shift(1) - low) > (high - high.shift(1)), np.maximum(low.shift(1) - low, 0), 0)
        
        # Calculate smoothed TR and DM
        df['TR' + str(period)] = df['TR'].rolling(window=period).sum()
        df['+DM' + str(period)] = df['+DM'].rolling(window=period).sum()
        df['-DM' + str(period)] = df['-DM'].rolling(window=period).sum()
        
        # Calculate +DI and -DI
        df['+DI' + str(period)] = 100 * df['+DM' + str(period)] / df['TR' + str(period)]
        df['-DI' + str(period)] = 100 * df['-DM' + str(period)] / df['TR' + str(period)]
        
        # Calculate ADX
        df['DX'] = 100 * abs(df['+DI' + str(period)] - df['-DI' + str(period)]) / (df['+DI' + str(period)] + df['-DI' + str(period)])
        df['ADX'] = df['DX'].rolling(window=period).mean()
        
        df['adx_signal'] = df['ADX'] > threshold
        return df
    
    def calculate_rsi(self, df, period=14, overbought=70, oversold=30):
        """Calculate RSI with trend confirmation."""
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # Add trend confirmation
        df['sma20'] = df['close'].rolling(window=20).mean()
        df['trend_up'] = df['close'] > df['sma20']
        
        # Generate RSI signals with trend confirmation
        df['rsi_oversold'] = (df['RSI'] < oversold) & df['trend_up']
        df['rsi_overbought'] = (df['RSI'] > overbought) & ~df['trend_up']
        return df
    
    def calculate_ml_score(self, df):
        """Calculate ML-based trade quality score."""
        # Feature engineering
        df['returns'] = df['close'].pct_change()
        df['volatility'] = df['returns'].rolling(window=20).std()
        df['volume_ma_ratio'] = df['volume'] / df['volume'].rolling(window=20).mean()
        
        # Prepare features
        features = ['RSI', 'ADX', 'volatility', 'volume_ma_ratio']
        X = df[features].fillna(0)
        
        # Generate target (simple example - can be enhanced)
        df['future_returns'] = df['returns'].shift(-1)
        y = (df['future_returns'] > 0).astype(int)
        
        # Train model on historical data
        train_size = int(len(df) * 0.8)
        X_train = X[:train_size]
        y_train = y[:train_size]
        
        self.scaler.fit(X_train)
        X_train_scaled = self.scaler.transform(X_train)
        self.ml_model.fit(X_train_scaled, y_train)
        
        # Generate predictions for all data
        X_scaled = self.scaler.transform(X)
        df['ml_score'] = self.ml_model.predict_proba(X_scaled)[:, 1]
        return df 