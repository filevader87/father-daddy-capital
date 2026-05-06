import subprocess
import json
from typing import Dict, Any
import os

class MacroAnalyst:
    """Python wrapper for MacroAnalystAgent.js"""
    
    def __init__(self):
        self.js_path = os.path.join(os.path.dirname(__file__), 'MacroAnalystAgent.js')
        
    def analyze_macro_environment(self) -> Dict[str, Any]:
        """
        Run macro analysis using the JavaScript agent.
        
        Returns:
            Dict[str, Any]: Macro analysis results including economic indicators,
                           market conditions, and recommendations
        """
        try:
            # Run the Node.js script and capture output
            result = subprocess.run(
                ['node', '-e', f"require('{self.js_path}').analyzeMacroEnvironment()"],
                capture_output=True,
                text=True,
                check=True
            )
            
            # Parse the JSON output
            analysis = json.loads(result.stdout)
            
            # Extract key metrics
            metrics = {
                'recession_risk': self._calculate_recession_risk(analysis),
                'market_health': self._calculate_market_health(analysis),
                'risk_appetite': self._calculate_risk_appetite(analysis)
            }
            
            return {
                'analysis': analysis,
                'metrics': metrics
            }
            
        except subprocess.CalledProcessError as e:
            print(f"Error running macro analysis: {e}")
            return {
                'error': str(e),
                'metrics': {
                    'recession_risk': 0.5,  # Default to neutral
                    'market_health': 0.5,
                    'risk_appetite': 0.5
                }
            }
            
    def _calculate_recession_risk(self, analysis: Dict) -> float:
        """Calculate recession risk from analysis data."""
        economic = analysis.get('economic', {})
        
        # Extract relevant indicators
        gdp_growth = economic.get('indicators', {}).get('gdp', {}).get('growth', 0)
        unemployment = economic.get('indicators', {}).get('unemployment', {}).get('rate', 5)
        inflation = economic.get('indicators', {}).get('inflation', {}).get('rate', 2)
        
        # Simple recession risk calculation
        risk = 0.0
        
        # GDP contribution
        if gdp_growth < 0:
            risk += 0.4
        elif gdp_growth < 1:
            risk += 0.2
            
        # Unemployment contribution
        if unemployment > 6:
            risk += 0.3
        elif unemployment > 5:
            risk += 0.2
            
        # Inflation contribution
        if inflation > 5:
            risk += 0.3
        elif inflation > 3:
            risk += 0.1
            
        return min(risk, 1.0)
    
    def _calculate_market_health(self, analysis: Dict) -> float:
        """Calculate overall market health score."""
        market = analysis.get('market', {})
        metrics = market.get('metrics', {})
        
        # Extract metrics
        volatility = metrics.get('volatility', {}).get('value', 0.15)
        sentiment = metrics.get('sentiment', {}).get('value', 0.5)
        liquidity = metrics.get('liquidity', {}).get('score', 0.5)
        
        # Calculate health score (0 to 1)
        health = (
            (1 - volatility) * 0.3 +  # Lower volatility is better
            sentiment * 0.4 +         # Higher sentiment is better
            liquidity * 0.3           # Higher liquidity is better
        )
        
        return max(0.0, min(1.0, health))
    
    def _calculate_risk_appetite(self, analysis: Dict) -> float:
        """Calculate market risk appetite."""
        market = analysis.get('market', {})
        geopolitical = analysis.get('geopolitical', {})
        
        # Extract relevant factors
        sentiment = market.get('metrics', {}).get('sentiment', {}).get('value', 0.5)
        geo_risk = 1.0 if geopolitical.get('riskLevel') == 'high' else 0.5
        
        # Calculate risk appetite (0 to 1)
        appetite = (
            sentiment * 0.6 +     # Market sentiment
            (1 - geo_risk) * 0.4  # Inverse of geopolitical risk
        )
        
        return max(0.0, min(1.0, appetite)) 