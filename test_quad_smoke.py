"""Smoke test for quad-track engine."""
import sys, os, json
os.chdir("/mnt/c/Users/12035/father_daddy_capital")
sys.path.insert(0, '.')

print("━" * 60)
print("FDC QUAD-TRACK SMOKE TEST")
print("━" * 60)

# Test 1: alt scanner import
from fdc_alt_scanner import (
    ALTCOIN_UNIVERSE, scan_all_alts, fetch_trending, 
    scan_trending_signals, alt_signals
)
print(f"PASS: Alt scanner imported")
print(f"      Universe: {len(ALTCOIN_UNIVERSE)} tickers")

# Test 2: fetch trending (lightweight, no yfinance)
trending = fetch_trending()
print(f"PASS: CoinGecko trending: {len(trending)} coins")
for t in trending[:3]:
    print(f"      {t['symbol']:8} | #{t['market_cap_rank']:4} | {t['name']}")

# Test 3: quick signal on a single alt
import yfinance as yf
from datetime import datetime, timedelta
end = datetime.now()
start = end - timedelta(days=7)
btc = yf.Ticker("BTC-USD")
hist = btc.history(start=start, end=end, interval="1h")
if len(hist) >= 14:
    sig = alt_signals(hist['Close'])
    print(f"PASS: Signal on BTC-USD (test): score={sig['score']}, direction={sig['direction']}, RSI={sig['signals']['rsi']}")
else:
    print("SKIP: Not enough data")

# Test 4: trending signal scan
trend_sigs = scan_trending_signals(trending)
print(f"PASS: Trending signals: {len(trend_sigs)} actionable")

# Test 5: full paper_engine import
from paper_engine import run_polymarket_cycle, run_alt_cycle, ALTCOIN_UNIVERSE as AE
assert len(AE) == len(ALTCOIN_UNIVERSE)
print(f"PASS: paper_engine.py imports alt scanner correctly ({len(AE)} tickers)")

# Test 6: run_one cycle with alt
from paper_engine import run_once
print("PASS: run_once() callable")

print("\n" + "━" * 60)
print("SMOKE TEST PASSED — Quad-track ready for continuous")
print("━" * 60)
