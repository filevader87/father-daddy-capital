import subprocess
import json
from typing import Dict, Any
import os
from src.logger import logger

class SentimentAnalyst:
    """Python wrapper for SentimentAnalystAgent.js"""
    
    def __init__(self):
        self.js_path = os.path.join(os.path.dirname(__file__), 'SentimentAnalystAgent.js')
        
    def analyze_sentiment(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run sentiment analysis using the JavaScript agent.
        
        Args:
            market_data: Dictionary containing market data
            
        Returns:
            Dict[str, Any]: Sentiment analysis results
        """
        try:
            # Convert market data to JSON string
            market_data_json = json.dumps(market_data)
            
            # Run the Node.js script and capture output
            result = subprocess.run(
                ['node', '-e', f"require('{self.js_path}').analyzeSentiment({market_data_json})"],
                capture_output=True,
                text=True,
                check=True
            )
            
            # Parse the JSON output
            sentiment = json.loads(result.stdout)
            
            # Calculate normalized sentiment score
            score = self._normalize_sentiment_score(sentiment)
            
            return {
                'raw_sentiment': sentiment,
                'normalized_score': score,
                'timestamp': sentiment.get('timestamp', None)
            }
            
        except Exception as e:
            logger.error("Error running sentiment analysis", extra={"error": str(e)})
            return None
            
    def _normalize_sentiment_score(self, sentiment: Dict[str, Any]) -> float:
        """
        Normalize sentiment score to range [-1, 1].
        
        Args:
            sentiment: Raw sentiment analysis results
            
        Returns:
            float: Normalized sentiment score
        """
        # Extract sentiment indicators
        overall = sentiment.get('overall', 'neutral')
        score = sentiment.get('score', 0)
        
        # Convert categorical sentiment to numeric
        sentiment_map = {
            'very_bearish': -1.0,
            'bearish': -0.5,
            'neutral': 0.0,
            'bullish': 0.5,
            'very_bullish': 1.0
        }
        
        # Combine categorical and numeric scores
        if isinstance(score, (int, float)):
            numeric_score = score
        else:
            numeric_score = 0.0
            
        categorical_score = sentiment_map.get(overall, 0.0)
        
        # Weight the scores (60% numeric, 40% categorical)
        final_score = numeric_score * 0.6 + categorical_score * 0.4
        
        # Ensure the score is in [-1, 1] range
        return max(-1.0, min(1.0, final_score))
        
    def get_market_data(self) -> Dict[str, Any]:
        """
        Fetch market data using the JavaScript agent.
        
        Returns:
            Dict[str, Any]: Market data
        """
        try:
            # Run the Node.js script and capture output
            result = subprocess.run(
                ['node', '-e', f"require('{self.js_path}').getMarketData()"],
                capture_output=True,
                text=True,
                check=True
            )
            
            # Parse the JSON output
            return json.loads(result.stdout)
            
        except Exception as e:
            logger.error("Error fetching market data", extra={"error": str(e)})
            return None

    def fetch_market_data(self, symbol: str) -> Dict[str, Any]:
        try:
            # Fetch market data implementation
            pass
        except Exception as e:
            logger.error("Error fetching market data", extra={"error": str(e), "symbol": symbol})
            return None 