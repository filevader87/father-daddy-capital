from datetime import datetime
from typing import Dict, List, Optional
from src.utils.logger import get_logger
from enum import Enum
import numpy as np

logger = get_logger(__name__)

class MarketRegime(Enum):
    TRENDING = "trending"
    HIGH_VOLATILITY = "high_volatility"
    HIGH_VOLUME = "high_volume"
    NEUTRAL = "neutral"

class RegimeTracker:
    def __init__(self):
        self.regime_log: List[Dict] = []
        self.current_regimes: Dict[str, str] = {}
        
    def track(self, symbol: str, regime: str, confidence: Optional[float] = None,
              timestamp: Optional[datetime] = None):
        """Track a regime change with optional confidence and timestamp"""
        if timestamp is None:
            timestamp = datetime.now()
            
        regime_entry = {
            "symbol": symbol,
            "regime": regime,
            "confidence": confidence,
            "timestamp": timestamp.isoformat()
        }
        
        self.regime_log.append(regime_entry)
        self.current_regimes[symbol] = regime
        
        logger.info(f"Regime tracked: {symbol} -> {regime} (confidence: {confidence})")
        
    def get_log(self) -> List[Dict]:
        """Get the complete regime log"""
        return self.regime_log
        
    def get_current_regime(self, symbol: str) -> Optional[str]:
        """Get the current regime for a symbol"""
        return self.current_regimes.get(symbol)
        
    def get_regime_history(self, symbol: str) -> List[Dict]:
        """Get regime history for a specific symbol"""
        return [entry for entry in self.regime_log if entry['symbol'] == symbol]
        
    def get_regime_changes(self, symbol: str) -> List[Dict]:
        """Get regime changes for a specific symbol"""
        history = self.get_regime_history(symbol)
        changes = []
        
        for i in range(1, len(history)):
            if history[i]['regime'] != history[i-1]['regime']:
                changes.append({
                    'from': history[i-1]['regime'],
                    'to': history[i]['regime'],
                    'timestamp': history[i]['timestamp'],
                    'confidence': history[i]['confidence']
                })
                
        return changes
        
    def get_regime_stats(self) -> Dict:
        """Get statistics about regimes"""
        if not self.regime_log:
            return {
                "total_entries": 0,
                "symbol_counts": {},
                "regime_counts": {}
            }
            
        symbol_counts = {}
        regime_counts = {}
        
        for entry in self.regime_log:
            symbol = entry['symbol']
            regime = entry['regime']
            
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
            regime_counts[regime] = regime_counts.get(regime, 0) + 1
            
        return {
            "total_entries": len(self.regime_log),
            "symbol_counts": symbol_counts,
            "regime_counts": regime_counts
        }
        
    def get_regime_duration(self, symbol: str, regime: str) -> float:
        """Get the duration of a specific regime for a symbol"""
        history = self.get_regime_history(symbol)
        duration = 0.0
        
        for i in range(len(history)):
            if history[i]['regime'] == regime:
                if i < len(history) - 1:
                    current_time = datetime.fromisoformat(history[i]['timestamp'])
                    next_time = datetime.fromisoformat(history[i+1]['timestamp'])
                    duration += (next_time - current_time).total_seconds()
                else:
                    # Last entry, use current time
                    current_time = datetime.fromisoformat(history[i]['timestamp'])
                    duration += (datetime.now() - current_time).total_seconds()
                    
        return duration
        
    def clear(self):
        """Clear the regime tracker"""
        self.regime_log = []
        self.current_regimes = {}
        logger.info("Regime tracker cleared")
