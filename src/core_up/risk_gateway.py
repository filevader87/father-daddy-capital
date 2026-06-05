"""
Risk Gateway - Risk management and gate checks before execution

Provides:
- Risk threshold checks
- Position limit validation
- Exposure limits
- Stop loss/take profit validation
- Risk score calculation
"""

import math


class RiskGateway:
    """
    Gateway for risk management checks
    
    Checks:
    1. Account risk: max position size, total exposure
    2. Trade risk: stop loss, take profit
    3. Position risk: diversification, concentration
    4. System risk: circuit breakers, emergency stops
    """
    
    def __init__(self, config=None):
        """
        Initialize risk gateway
        
        Args:
            config: risk configuration dict
        """
        self._config = config or {
            'max_position_percentage': 0.25,  # Max 25% per position
            'max_total_exposure': 1.0,  # Max 100% total exposure
            'max_daily_loss': 0.05,  # Max 5% daily loss
            'stop_loss_pct': -0.05,  # Default 5% stop loss
            'take_profit_pct': 0.10,  # Default 10% take profit
            'circuit_breaker_pct': -0.10,  # Circuit break at -10%
        }
    
    @property
    def config(self):
        """Risk configuration"""
        return self._config
    
    def check_position_sizing(self, current_position, new_signal):
        """
        Check if position size is within limits
        
        Args:
            current_position: current position size (in shares or value)
            new_signal: signal dict with 'value', 'confidence'
        
        Returns:
            bool: True if position is within limits
        """
        if new_signal.get('action') in ('hold', 'neutral'):
            return True
        
        position_pct = abs(current_position)
        max_position = self._config.get('max_position_percentage', 0.25)
        
        position_pct = position_pct / 1.0  # Assume full equity
        return position_pct <= max_position
    
    def check_total_exposure(self, total_position):
        """Check total exposure doesn't exceed limit"""
        exposure = abs(total_position)
        max_exposure = self._config.get('max_total_exposure', 1.0)
        return exposure <= max_exposure
    
    def check_stop_loss(self, entry_price, current_price):
        """Check if stop loss is hit"""
        stop_loss_pct = self._config.get('stop_loss_pct', -0.05)
        stop_loss_price = entry_price * (1 + stop_loss_pct)
        return current_price >= stop_loss_price
    
    def check_take_profit(self, entry_price, current_price):
        """Check if take profit reached"""
        tp_pct = self._config.get('take_profit_pct', 0.10)
        tp_price = entry_price * (1 + tp_pct)
        return current_price >= tp_price
    
    def calculate_risk_score(self, signals, current_positions):
        """
        Calculate overall risk score
        
        Args:
            signals: list of signal dicts
            current_positions: dict of current positions
        
        Returns:
            float: risk score (higher = more dangerous)
        """
        signals = signals if signals else []
        
        if not signals:
            return 0.0
        
        # Signal volatility (spread between buy and sell signals)
        buy_signals = [s for s in signals if s.get('action') == 'buy']
        sell_signals = [s for s in signals if s.get('action') == 'sell']
        
        buy_confidence = sum(s.get('confidence', 0) for s in buy_signals)
        sell_confidence = sum(s.get('confidence', 0) for s in sell_signals)
        
        # Signal conflict risk
        signal_conflict = abs(buy_confidence - sell_confidence) / max(buy_confidence + sell_confidence, 0.01)
        
        # Position concentration risk
        position_count = len([pos for pos in current_positions.values() if pos != 0])
        position_concentration = min(position_count / 10, 1.0)
        
        # Overall risk score (0-1)
        risk_score = (signal_conflict * 0.4 + position_concentration * 0.4)
        
        return risk_score
    
    def check_circuit_breaker(self, daily_loss_pct):
        """Check circuit breaker trigger"""
        cb_pct = self._config.get('circuit_breaker_pct', -0.10)
        return daily_loss_pct <= cb_pct
    
    def check_all_risks(self, signals, current_positions, daily_loss_pct = 0):
        """
        Run all risk checks
        
        Returns:
            dict with:
                - allowed: bool
                - reasons: list of blocking reasons
                - risk_score: calculated risk score
        """
        risks = []
        signal_score = self.calculate_risk_score(signals, current_positions)
        
        # Check each signal
        for signal in signals:
            if signal.get('action') in ('buy', 'sell'):
                if not self.check_position_sizing(0, signal):
                    risks.append('Position size limit reached')
                if not self.check_total_exposure(0):
                    risks.append('Total exposure limit reached')
        
        # Check stop loss
        for position_value, position_info in current_positions.items():
            if position_info.get('entry_price'):
                if self.check_stop_loss(position_info['entry_price'], position_info['current_price']):
                    risks.append(f"Stop loss hit for {position_value}")
        
        # Check circuit breaker
        if self.check_circuit_breaker(daily_loss_pct):
            risks.append('Circuit breaker triggered')
        
        return {
            'allowed': len(risks) == 0,
            'reasons': risks,
            'risk_score': signal_score,
        }