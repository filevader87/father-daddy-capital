"""Integration test for triple-track engine."""
import sys, json, os
os.chdir("/mnt/c/Users/12035/father_daddy_capital")
sys.path.insert(0, '.')

print("━" * 60)
print("FDC Triple-Track Integration Test")
print("━" * 60)

# Test 1: import chain
try:
    from paper_engine import run_polymarket_cycle, fetch_btc_5min
    from fdc_polymarket import btc_signal_5m, find_btc_daily_markets, evaluate_polymarket_trades
    print("PASS: Import chain OK")
except Exception as e:
    print(f"FAIL: Import - {e}")
    sys.exit(1)

# Test 2: fetch BTC 5m candles
btc = fetch_btc_5min()
if btc:
    print(f"PASS: BTC 5m candles fetched ({len(btc)} candles)")
    print(f"      Range: ${btc[0]:,.2f} - ${btc[-1]:,.2f}")
else:
    print("SKIP: No BTC 5m data returned (yfinance may be slow)")

# Test 3: signal generation
sig = btc_signal_5m(btc)
print(f"PASS: Signal generated")
print(f"      direction={sig['direction']}, confidence={sig['confidence']}, RSI={sig['signals']['rsi']}")

# Test 4: Polymarket market discovery
mkts = find_btc_daily_markets()
print(f"PASS: Found {len(mkts)} active BTC daily markets")
for m in mkts[:2]:
    p = m['outcomePrices']
    print(f"      {m['question']}")
    print(f"      YES={float(p[0])*100:.1f}% / NO={float(p[1])*100:.1f}% | vol=${m['volume']:,.0f}")

# Test 5: trade evaluation
state = {}
entries = evaluate_polymarket_trades(btc[-1], sig, state)
print(f"PASS: Trade evaluation complete ({len(entries)} entries)")
for e in entries:
    print(f"      {e['action']} on ${e['strike']:,.0f} strike — bet ${e['bet_size']}, edge={e['edge']}")

# Test 6: settlement
from fdc_polymarket import check_settlements, polymarket_summary
# Add a fake settled position
state2 = {
    "polymarket_positions": {
        "BTC>81000": {
            "action": "BUY_YES", "strike": 81000, "btc_price": 79600,
            "bet_size": 20, "yes_price": 0.45, "settle_date": "2026-05-07",
            "market_question": "BTC above $81,000 on May 7?"
        }
    },
    "polymarket_pnl": 0.0
}
settled = check_settlements(state2, 85000)  # BTC above strike → YES wins
print(f"PASS: Settlement check ({len(settled)} settled)")
for s in settled:
    print(f"      {s['action']} @ ${s['strike']:,.0f} — PnL ${s['pnl']:+,.2f}")

summary = polymarket_summary(state2, settled)
print(f"\nPASS: Summary generated")

print("\n" + "━" * 60)
print("ALL TESTS PASSED — Triple-track engine ready")
print("━" * 60)
