import random

class SentimentAnalyzer:
    def __init__(self):
        self.positive_keywords = ["bullish", "buy", "moon", "pump", "rally"]
        self.negative_keywords = ["bearish", "sell", "dump", "crash", "fear"]

    def fetch_news_headlines(self, keyword="crypto"):
        # Placeholder: simulate news headlines
        headlines = [
            f"{keyword} sees major pump after announcement",
            f"Traders warn of potential {keyword} crash",
            f"{keyword} market stable amid global uncertainty",
            f"{keyword} gaining popularity among institutions",
            f"Retail investors panic over {keyword} volatility"
        ]
        return random.sample(headlines, k=3)

    def analyze_sentiment(self, headlines):
        score = 0
        for headline in headlines:
            text = headline.lower()
            for pos in self.positive_keywords:
                if pos in text:
                    score += 1
            for neg in self.negative_keywords:
                if neg in text:
                    score -= 1

        if score > 0:
            return 1  # positive
        elif score < 0:
            return -1  # negative
        return 0  # neutral

    def get_sentiment_score(self, keyword="crypto"):
        headlines = self.fetch_news_headlines(keyword)
        return self.analyze_sentiment(headlines)