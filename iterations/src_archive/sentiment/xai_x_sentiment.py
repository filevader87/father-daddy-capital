"""
V19.9 xAI/X Sentiment Diagnostic Module

DIAGNOSTIC ONLY — sentiment may NOT:
  - Increase adjusted_probability
  - Open trades
  - Override EV gates

Sentiment may ONLY:
  - Veto UP bounce when bearish_context or panic_context detected
  - Log diagnostic context for postmortem analysis

Uses x_search transport if configured, otherwise falls back to stub.
Cache TTL: 30-60 seconds.
"""

import json
import time
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

logger = logging.getLogger(__name__)

# ── Configuration ──
SENTIMENT_CACHE_TTL = 45  # seconds
SENTIMENT_LOG_DIR = Path(__file__).parent.parent.parent / "paper_trading" / "sentiment"

# Assets to monitor
SENTIMENT_ASSETS = {
    "BTC": ["BTC", "Bitcoin", "bitcoin", "BTCUSD"],
    "ETH": ["ETH", "Ethereum", "ethereum", "ETHUSD"],
    "SOL": ["SOL", "Solana", "solana", "SOLUSD"],
    "XRP": ["XRP", "Ripple", "ripple", "XRPUSD"],
}

# Panic/bearish terms that signal negative context
PANIC_TERMS = [
    "crash", "dump", "liquidation", "capitulation", "collapse",
    "plunge", "freefall", "bloodbath", "massacre", "panic sell",
    "flash crash", "bear trap", "death spiral", "sell off",
    "whale dump", "rug pull", "insolvency", "bankrupt",
    "ban", "banned", "sec crackdown", "fbi", "fraud",
    "hack", "exploit", "drain", "depeg", "contagion",
]

BEARISH_TERMS = [
    "bearish", "short", "resistance", "rejection", "overbought",
    "sell wall", "double top", "head and shoulders", "descending",
    "lower low", "lower high", "downtrend", "correction",
    "weak support", "breakdown", "bear flag", "dead cat",
]

BULLISH_TERMS = [
    "bullish", "breakout", "support", "accumulation", "bottom",
    "reversal", "bounce", "recovery", "higher low", " ATH",
    "moon", "pump", "buy wall", "golden cross", "uptrend",
]


class SentimentCache:
    """Simple TTL cache for sentiment results."""
    
    def __init__(self, ttl=SENTIMENT_CACHE_TTL):
        self._cache = {}  # key -> (result, timestamp)
        self.ttl = ttl
    
    def get(self, key):
        if key in self._cache:
            result, ts = self._cache[key]
            if time.time() - ts < self.ttl:
                return result
            del self._cache[key]
        return None
    
    def set(self, key, result):
        self._cache[key] = (result, time.time())


class XaiXSentimentProvider:
    """
    xAI/X sentiment diagnostic provider.
    
    Query strategy:
    1. Try x_search via configured transport (xurl CLI or API)
    2. If unavailable, mark insufficient_data=True
    3. Cache results for SENTIMENT_CACHE_TTL seconds
    
    NEVER uses sentiment as a trade trigger.
    NEVER increases probability from sentiment.
    """
    
    def __init__(self, xurl_config=None):
        self.cache = SentimentCache()
        self.xurl_config = xurl_config
        self.query_count = 0
        self.error_count = 0
        self.insufficient_count = 0
        
    def _try_xurl_search(self, query, max_results=20):
        """Try to search X via xurl CLI."""
        try:
            import subprocess
            result = subprocess.run(
                ["xurl", "search", query, "--max-results", str(max_results)],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        return None
    
    def _try_xai_search(self, query):
        """Try to use xAI API for search."""
        # xAI search not directly available via simple API call
        # This is a placeholder for future xAI integration
        return None
    
    def _compute_sentiment(self, posts, asset_key):
        """Compute sentiment score from raw posts."""
        if not posts:
            return {
                "asset": asset_key,
                "sentiment_score": 0,
                "bullish_count": 0,
                "bearish_count": 0,
                "neutral_count": 0,
                "post_count": 0,
                "attention_spike_zscore": 0,
                "panic_terms_detected": [],
                "spam_score": 0,
                "insufficient_data": True,
            }
        
        bullish = 0
        bearish = 0
        neutral = 0
        panic_terms = []
        
        for post in posts:
            text = post.get("text", "").lower() if isinstance(post, dict) else str(post).lower()
            
            # Check for panic terms
            for term in PANIC_TERMS:
                if term in text:
                    panic_terms.append(term)
                    bearish += 2  # Panic terms weighted 2x
                    break
            else:
                # Check for bearish terms
                for term in BEARISH_TERMS:
                    if term in text:
                        bearish += 1
                        break
                else:
                    # Check for bullish terms
                    for term in BULLISH_TERMS:
                        if term in text:
                            bullish += 1
                            break
                    else:
                        neutral += 1
        
        total = bullish + bearish + neutral
        if total == 0:
            sentiment_score = 0
        else:
            sentiment_score = (bullish - bearish) / total  # Range: -1 to +1
        
        # Attention spike (z-score) - would need historical baseline
        attention_spike = 0  # Stub: no historical baseline yet
        
        # Spam detection - simple heuristic
        spam_score = 0  # Stub
        
        return {
            "asset": asset_key,
            "sentiment_score": round(sentiment_score, 3),
            "bullish_count": bullish,
            "bearish_count": bearish,
            "neutral_count": neutral,
            "post_count": total,
            "attention_spike_zscore": attention_spike,
            "panic_terms_detected": list(set(panic_terms)),
            "spam_score": spam_score,
            "insufficient_data": total < 5,
        }
    
    def get_sentiment(self, asset_key):
        """
        Get sentiment diagnostic for an asset.
        
        Returns dict with:
        - sentiment_score: -1 (bearish) to +1 (bullish)
        - bullish_count, bearish_count, neutral_count
        - attention_spike_zscore
        - panic_terms_detected
        - source_ids_or_urls
        - age_seconds
        - insufficient_data
        - spam_score
        
        SENTIMENT MAY NOT:
        - Increase adjusted_probability
        - Open trades
        - Override EV gates
        """
        cache_key = f"sentiment_{asset_key}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            cached["age_seconds"] = round(time.time() - cached.get("_cache_time", time.time()), 1)
            return cached
        
        self.query_count += 1
        terms = SENTIMENT_ASSETS.get(asset_key, [asset_key])
        query = " OR ".join(f'"{t}"' for t in terms[:3])
        
        # Try xurl search
        raw = self._try_xurl_search(query, max_results=20)
        
        if raw is not None:
            posts = raw if isinstance(raw, list) else raw.get("results", raw.get("data", []))
            result = self._compute_sentiment(posts, asset_key)
            result["source"] = "xurl"
            result["source_ids"] = [p.get("id", "") for p in posts[:5] if isinstance(p, dict)]
        else:
            # No X data available
            self.insufficient_count += 1
            result = {
                "asset": asset_key,
                "sentiment_score": 0,
                "bullish_count": 0,
                "bearish_count": 0,
                "neutral_count": 0,
                "post_count": 0,
                "attention_spike_zscore": 0,
                "panic_terms_detected": [],
                "source_ids_or_urls": [],
                "age_seconds": 0,
                "insufficient_data": True,
                "source": "none",
                "spam_score": 0,
            }
        
        result["_cache_time"] = time.time()
        self.cache.set(cache_key, result)
        return result
    
    def get_veto_context(self, asset_key):
        """
        Get veto context for UP-bounce decision.
        
        Returns:
        - sentiment_context: "bullish_context" | "neutral_context" | "bearish_context" | "panic_context" | "insufficient_data"
        - veto: True if sentiment should block UP bounce
        - veto_reason: str or None
        - diagnostic: full sentiment dict
        
        Veto rules (diagnostic only, never opens trades):
        - bearish_context → blocks UP bounce (with logged reason)
        - panic_context → blocks UP bounce (with logged reason)
        - bullish_context → no veto (but NEVER increases probability)
        - insufficient_data → no veto (cannot confirm or deny)
        """
        diagnostic = self.get_sentiment(asset_key)
        
        # ── Determine context ──
        if diagnostic.get("insufficient_data", True):
            context = "insufficient_data"
            veto = False
            veto_reason = None
        elif diagnostic["panic_terms_detected"]:
            context = "panic_context"
            veto = True
            veto_reason = f"panic_terms_detected:{','.join(diagnostic['panic_terms_detected'][:3])}"
        elif diagnostic["sentiment_score"] <= -0.3:
            context = "bearish_context"
            veto = True
            veto_reason = f"bearish_sentiment_{diagnostic['sentiment_score']:.2f}"
        elif diagnostic["sentiment_score"] >= 0.3:
            context = "bullish_context"
            veto = False
            veto_reason = None
            # NOTE: bullish_context does NOT increase probability
            # It only means we don't veto
        else:
            context = "neutral_context"
            veto = False
            veto_reason = None
        
        return {
            "sentiment_context": context,
            "veto": veto,
            "veto_reason": veto_reason,
            "diagnostic": diagnostic,
        }


def log_sentiment(diagnostic, log_dir=None):
    """Log sentiment diagnostic to JSONL file."""
    if log_dir is None:
        log_dir = SENTIMENT_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **diagnostic,
    }
    # Remove internal cache time
    log_entry.pop("_cache_time", None)
    
    log_file = log_dir / "sentiment_diagnostic.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(log_entry) + "\n")


# ── Module-level singleton ──
_provider = None

def get_sentiment_provider():
    """Get or create the module-level sentiment provider."""
    global _provider
    if _provider is None:
        _provider = XaiXSentimentProvider()
    return _provider


def get_sentiment(asset_key):
    """Convenience function: get sentiment for an asset."""
    return get_sentiment_provider().get_sentiment(asset_key)


def get_sentiment_veto(asset_key):
    """Convenience function: get veto context for an asset."""
    return get_sentiment_provider().get_sentiment(asset_key)


# ── §10 V20: Sentiment Regime Classification ──
SENTIMENT_REGIMES = {
    "panic": "extreme bearish, panic terms detected, crowd selling",
    "euphoric": "extreme bullish, FOMO terms detected, crowd buying",
    "neutral": "no strong sentiment signal, insufficient data",
    "continuation": "sentiment aligned with current trend (bearish in downtrend, bullish in uptrend)",
    "reversal_attempt": "sentiment shifting against trend (bullish in downtrend, bearish in uptrend)",
}


def classify_sentiment_regime(sentiment_result: dict, price_trend: str = "neutral") -> str:
    """
    Classify sentiment into a regime for microstructure context.
    
    Args:
        sentiment_result: output from get_sentiment()
        price_trend: "up", "down", or "neutral" — based on recent price action
    
    Returns:
        One of: panic, euphoric, neutral, continuation, reversal_attempt
    
    Sentiment may VETO (block UP trades during panic/continuation-bear) 
    but may NEVER open trades or increase probability.
    """
    if sentiment_result is None:
        return "neutral"
    
    score = sentiment_result.get("sentiment_score", 0.0) or 0.0
    panic = sentiment_result.get("panic_terms_detected", False)
    attention_z = sentiment_result.get("attention_spike_zscore", 0.0) or 0.0
    bullish_count = sentiment_result.get("bullish_count", 0) or 0
    bearish_count = sentiment_result.get("bearish_count", 0) or 0
    insufficient = sentiment_result.get("insufficient_data", True)
    
    # Insufficient data → neutral (no veto)
    if insufficient:
        return "neutral"
    
    # Panic: extreme bearish + panic terms
    if panic and score <= -0.4:
        return "panic"
    
    # Euphoric: extreme bullish + attention spike
    if score >= 0.6 and attention_z > 1.5:
        return "euphoric"
    
    # Sentiment aligned with trend = continuation
    if price_trend == "down" and score <= -0.2:
        return "continuation"
    if price_trend == "up" and score >= 0.2:
        return "continuation"
    
    # Sentiment shifting against trend = reversal attempt
    if price_trend == "down" and score >= 0.3:
        return "reversal_attempt"
    if price_trend == "up" and score <= -0.3:
        return "reversal_attempt"
    
    return "neutral"