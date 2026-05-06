import os
import requests

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

def fetch_stock_news(symbol="AAPL"):
    try:
        url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from=2023-01-01&to=2023-12-31&token={FINNHUB_API_KEY}"
        response = requests.get(url)
        data = response.json()
        headlines = [item["headline"] for item in data]
        return headlines
    except Exception as e:
        print(f"Error fetching Finnhub news for {symbol}: {e}")
        return []
