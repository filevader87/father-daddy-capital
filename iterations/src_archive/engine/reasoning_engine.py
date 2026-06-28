from sentiment.sentiment_model import SentimentAnalyzer
import random

class ReasoningEngine:
    def __init__(self):
        self.sentiment_model = SentimentAnalyzer()

    def generate_reasoning(self, signal, indicators, market_context="crypto"):
        sentiment_score = self.sentiment_model.get_sentiment_score(market_context)

        poetic_tone = [
            "A shadow looms over the candlesticks.",
            "The winds of the market whisper uncertainty.",
            "Bullish tides swell against a bearish moon.",
            "Hope flickers in a sea of red.",
            "The algorithm dreams in Fibonacci spirals."
        ]

        paranoid_thoughts = [
            "Is this a trap? The whales are watching.",
            "Too perfect... someone is pulling strings.",
            "Even green candles bleed eventually.",
            "Smart money lies in the shadows.",
            "Fear and greed algorithms are manipulating sentiment again."
        ]

        sentiment_description = {
            1: "Optimism surges through the charts.",
            0: "Sentiment remains a cold equilibrium.",
            -1: "Panic simmers beneath every tick."
        }

        base_reasoning = f"Signal suggests **{signal}**. Indicators show: {indicators}."
        sentiment_line = sentiment_description[sentiment_score]
        poetic_line = random.choice(poetic_tone)
        paranoia_line = random.choice(paranoid_thoughts)

        full_reasoning = f"{base_reasoning}\n{sentiment_line}\n{poetic_line}\n{paranoia_line}"
        return full_reasoning