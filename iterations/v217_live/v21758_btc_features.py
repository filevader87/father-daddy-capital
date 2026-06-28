"""V21.7.58 BTC 15m Feature Engineering — RVI + Funding Rate + VPIN"""
import json, math, time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
import requests

# Feature constants
RVI_THRESHOLD = 0.30       # |RVI| > 0.30 = directional signal
FUNDING_BULLISH = -0.0003  # Funding < -0.03% → upward bias
FUNDING_BEARISH = 0.0005   # Funding > 0.05% → mean reversion (longs overlevered)
VPIN_HIGH = 0.30           # VPIN > 0.30 = high toxicity
VPIN_BUCKET_SIZE = 60      # 60 trades per VPIN bucket

class BTCFeatureEngine:
    def __init__(self):
        self.recent_trades = deque(maxlen=500)
        self.vpin_buckets = deque(maxlen=10)
        self.current_bucket = {"buy_vol": 0, "sell_vol": 0, "count": 0}
        self.last_funding = None
        self.last_funding_ts = 0
    
    def compute_rvi(self, bid_depth: float, ask_depth: float) -> float:
        """Relative Volume Imbalance: (bid-ask)/(bid+ask). Range [-1, 1]."""
        total = bid_depth + ask_depth
        if total <= 0:
            return 0.0
        return (bid_depth - ask_depth) / total
    
    def get_funding_rate(self) -> Optional[Dict]:
        """Fetch BTC funding rate from Binance (free, no API key needed).
        GET https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT
        Returns: {'last_funding_rate': float, 'mark_price': float, 'ts': epoch}
        """
        try:
            # Cache for 60 seconds
            now = time.time()
            if self.last_funding and (now - self.last_funding_ts) < 60:
                return self.last_funding
            
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                params={"symbol": "BTCUSDT"},
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                result = {
                    "last_funding_rate": float(data.get("lastFundingRate", 0)),
                    "mark_price": float(data.get("markPrice", 0)),
                    "ts": now,
                }
                self.last_funding = result
                self.last_funding_ts = now
                return result
        except Exception:
            pass
        return None
    
    def update_vpin(self, price: float, volume: float, is_buy: bool):
        """Update VPIN bucket with a trade."""
        if is_buy:
            self.current_bucket["buy_vol"] += volume
        else:
            self.current_bucket["sell_vol"] += volume
        self.current_bucket["count"] += 1
        
        if self.current_bucket["count"] >= VPIN_BUCKET_SIZE:
            self.vpin_buckets.append(self.current_bucket.copy())
            self.current_bucket = {"buy_vol": 0, "sell_vol": 0, "count": 0}
    
    def compute_vpin(self) -> float:
        """Volume-Synchronized Probability of Informed Trading.
        VPIN = sum(|buy_vol - sell_vol|) / sum(buy_vol + sell_vol) over buckets.
        Range [0, 1]. High = informed flow.
        """
        if not self.vpin_buckets:
            return 0.0
        total_abs = sum(abs(b["buy_vol"] - b["sell_vol"]) for b in self.vpin_buckets)
        total_vol = sum(b["buy_vol"] + b["sell_vol"] for b in self.vpin_buckets)
        if total_vol <= 0:
            return 0.0
        return total_abs / total_vol
    
    def generate_signal(self, bid_depth: float, ask_depth: float, 
                        btc_price: float = None) -> Dict:
        """Generate composite BTC directional signal.
        
        Returns:
            {
                'rvi': float,           # [-1, 1]
                'rvi_signal': str,      # 'BULLISH', 'BEARISH', 'NEUTRAL'
                'funding_rate': float,  # from Binance
                'funding_signal': str,  # 'BULLISH', 'BEARISH', 'NEUTRAL'
                'vpin': float,          # [0, 1]
                'vpin_signal': str,     # 'HIGH', 'NORMAL'
                'composite_direction': str,  # 'UP', 'DOWN', 'NEUTRAL'
                'confidence': float,    # [0, 1]
            }
        """
        rvi = self.compute_rvi(bid_depth, ask_depth)
        funding_data = self.get_funding_rate()
        vpin = self.compute_vpin()
        
        # RVI signal
        if rvi > RVI_THRESHOLD:
            rvi_signal = "BULLISH"
        elif rvi < -RVI_THRESHOLD:
            rvi_signal = "BEARISH"
        else:
            rvi_signal = "NEUTRAL"
        
        # Funding rate signal
        funding_rate = 0.0
        funding_signal = "NEUTRAL"
        if funding_data:
            funding_rate = funding_data["last_funding_rate"]
            if funding_rate < FUNDING_BULLISH:
                funding_signal = "BULLISH"  # Shorts paying → upward pressure
            elif funding_rate > FUNDING_BEARISH:
                funding_signal = "BEARISH"  # Longs overlevered → mean reversion
        
        # VPIN signal
        if vpin > VPIN_HIGH:
            vpin_signal = "HIGH"
        else:
            vpin_signal = "NORMAL"
        
        # Composite direction (2 of 3 agreement)
        signals = [rvi_signal, funding_signal]
        bullish_count = sum(1 for s in signals if s == "BULLISH")
        bearish_count = sum(1 for s in signals if s == "BEARISH")
        
        if bullish_count >= 2:
            direction = "UP"
            confidence = min(1.0, abs(rvi) * 2 + vpin)
        elif bearish_count >= 2:
            direction = "DOWN"
            confidence = min(1.0, abs(rvi) * 2 + vpin)
        elif bullish_count == 1 and bearish_count == 0:
            direction = "UP"
            confidence = abs(rvi) * 0.5
        elif bearish_count == 1 and bullish_count == 0:
            direction = "DOWN"
            confidence = abs(rvi) * 0.5
        else:
            direction = "NEUTRAL"
            confidence = 0.0
        
        return {
            "rvi": round(rvi, 4),
            "rvi_signal": rvi_signal,
            "funding_rate": funding_rate,
            "funding_signal": funding_signal,
            "vpin": round(vpin, 4),
            "vpin_signal": vpin_signal,
            "composite_direction": direction,
            "confidence": round(confidence, 4),
        }
