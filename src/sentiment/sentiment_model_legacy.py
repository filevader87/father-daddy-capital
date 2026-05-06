import random
from nltk.sentiment.vader import SentimentIntensityAnalyzer
import nltk

# Download the VADER lexicon (run once)
nltk.download('vader_lexicon')

class SentimentAnalyzer:
    def __init__(self):
        self.analyzer = SentimentIntensityAnalyzer()

    def fetch_news_headlines(self, keyword="crypto"):
        # For simulation, return a list of sample headlines.
        headlines = [
            f"{keyword} sees major pump after announcement",
            f"Traders warn of potential {keyword} crash",
            f"{keyword} market remains volatile amid global uncertainty",
            f"{keyword} gains popularity among institutional investors",
            f"Retail investors panic over {keyword} volatility"
        ]
        return headlines

    def analyze_sentiment(self, headlines):
        # Use VADER to compute a compound sentiment score for each headline.
        scores = [self.analyzer.polarity_scores(headline)['compound'] for headline in headlines]
        avg_score = sum(scores) / len(scores) if scores else 0
        # Map the average score to discrete values.
        if avg_score >= 0.05:
            return 1
        elif avg_score <= -0.05:
            return -1
        else:
            return 0

    def get_sentiment_score(self, keyword="crypto"):
        headlines = self.fetch_news_headlines(keyword)
        return self.analyze_sentiment(headlines)
