import os
import requests

CRYPTO_PANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY")

def fetch_crypto_news():
    try:
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTO_PANIC_API_KEY}&filter=hot"
        response = requests.get(url)
        data = response.json()
        headlines = [item["title"] for item in data.get("results", [])]
        return headlines
    except Exception as e:
        print(f"Error fetching CryptoPanic news: {e}")
        return []
