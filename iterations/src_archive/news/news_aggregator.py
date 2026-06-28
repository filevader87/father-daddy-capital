from src.news.crypto_news import fetch_crypto_news
from src.news.stock_news import fetch_stock_news
from src.news.news_utils import score_sentiment

def aggregate_news_sentiment(crypto_symbol="BTC", stock_symbol="AAPL"):
    crypto_headlines = fetch_crypto_news()
    stock_headlines = fetch_stock_news(stock_symbol)
    crypto_sentiment = score_sentiment(crypto_headlines)
    stock_sentiment = score_sentiment(stock_headlines)
    return {
        "crypto": crypto_sentiment,
        "stock": stock_sentiment,
        "crypto_headlines": crypto_headlines,
        "stock_headlines": stock_headlines,
    }
