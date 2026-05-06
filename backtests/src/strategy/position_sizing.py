import numpy as np
import pandas as pd

class PositionSizer:
    def __init__(self, risk_factor=0.02, kelly_fraction=0.5):
        self.risk_factor = risk_factor
        self.kelly_fraction = kelly_fraction
    
    def calculate_atr(self, df, period=14):
        """Calculate Average True Range."""
        high = df['high']
        low = df['low']
        close = df['close']
        
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        tr = pd.DataFrame({'tr1': tr1, 'tr2': tr2, 'tr3': tr3}).max(axis=1)
        atr = tr.rolling(window=period).mean()
        return atr
    
    def kelly_criterion(self, win_rate, win_loss_ratio):
        """Calculate Kelly Criterion position size."""
        q = 1 - win_rate
        kelly_size = (win_rate / q) - (1 / win_loss_ratio)
        # Apply fraction to make more conservative
        kelly_size = max(0, min(1, kelly_size * self.kelly_fraction))
        return kelly_size
    
    def calculate_position_size(self, df, capital, win_rate=0.5, win_loss_ratio=2.0):
        """Calculate position size based on multiple factors."""
        # Calculate ATR-based volatility adjustment
        df['atr'] = self.calculate_atr(df)
        df['atr_pct'] = df['atr'] / df['close']
        
        # Volatility scaling factor (inverse relationship)
        vol_scale = 1 / (df['atr_pct'] * 100)  # Normalize to reasonable range
        vol_scale = vol_scale / vol_scale.mean()  # Center around 1
        
        # Kelly Criterion base size
        kelly_size = self.kelly_criterion(win_rate, win_loss_ratio)
        
        # Quality score scaling (assuming ml_score exists from indicators)
        quality_scale = df['ml_score'] if 'ml_score' in df.columns else 1.0
        
        # Combine all factors
        base_position = capital * self.risk_factor
        df['position_size'] = base_position * kelly_size * vol_scale * quality_scale
        
        # Apply reasonable limits
        df['position_size'] = df['position_size'].clip(0, capital * 0.25)  # Max 25% of capital per position
        
        return df
    
    def adjust_for_drawdown(self, df, max_drawdown=-0.1):
        """Adjust position sizes based on drawdown protection."""
        # Calculate running equity curve
        df['equity_curve'] = (1 + df['returns']).cumprod()
        
        # Calculate drawdown
        df['rolling_max'] = df['equity_curve'].rolling(window=252, min_periods=1).max()
        df['drawdown'] = (df['equity_curve'] - df['rolling_max']) / df['rolling_max']
        
        # Scale positions based on drawdown
        drawdown_scale = np.where(
            df['drawdown'] < max_drawdown,
            1 + (df['drawdown'] - max_drawdown),  # Reduce position size in drawdown
            1.0
        )
        
        df['position_size'] = df['position_size'] * drawdown_scale
        return df 