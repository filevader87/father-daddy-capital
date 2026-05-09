#!/usr/bin/env python3
"""
FDC — CCXT Smoke Test (Phase 1A)
=================================
Validates:
  1. ccxt version constraint
  2. Pool connects to accessible exchanges
  3. MarketDataService fetches normalized tickers
  4. Symbol mapping works
  5. Graceful degradation

Phase 1A: coinbase, kraken, okx, gate (Binance/Bybit geoblocked for this account).

Run: python3 test_ccxt_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src" / "trading"))

# Exchanges to test (Binance/Bybit geoblocked here)
EXCHANGES = ["coinbase", "kraken", "okx", "gate"]
PRIMARY = "coinbase"
FAILURES = 0

def ok(msg): 
    global FAILURES; print(f"  ✅ {msg}")
def fail(msg): 
    global FAILURES; FAILURES += 1; print(f"  ❌ {msg}")


async def test_version():
    print("\n─ Version ─")
    import ccxt
    v = ccxt.__version__
    parts = tuple(map(int, v.split(".")[:2]))
    assert (4, 0) <= parts < (5, 0), f"ccxt {v} outside [4.0, 5.0)"
    ok(f"ccxt {v}")

async def test_pool():
    print("\n─ Pool Connect ─")
    from ccxt_adapter import AdapterPool
    pool = AdapterPool(exchange_ids=EXCHANGES)
    try:
        await pool.__aenter__()
        healthy = pool.healthy_exchanges
        ok(f"{len(healthy)}/{len(EXCHANGES)} connected: {healthy}")
        assert len(healthy) >= 1, "no exchanges connected"
    except Exception as e:
        fail(f"pool: {e}")
    finally:
        await pool.__aexit__()

async def test_ticker():
    print("\n─ Ticker ─")
    from ccxt_adapter import AdapterPool
    from market_data import MarketDataService
    pool = AdapterPool(exchange_ids=[PRIMARY])
    try:
        await pool.__aenter__()
        svc = MarketDataService(pool)
        t = await svc.get_ticker("BTC/USD", PRIMARY)
        assert t.last > 0, f"bad last: {t.last}"
        ok(f"BTC/USD ${t.last:,.2f} bid={t.bid} ask={t.ask}")
    except Exception as e:
        fail(f"ticker: {e}")
    finally:
        await pool.__aexit__()

async def test_multi_ticker():
    print("\n─ Multi-Ticker ─")
    from ccxt_adapter import AdapterPool
    from market_data import MarketDataService
    pool = AdapterPool(exchange_ids=[PRIMARY])
    try:
        await pool.__aenter__()
        svc = MarketDataService(pool)
        tickers = await svc.get_tickers(["BTC/USD", "ETH/USD", "SOL/USD"])
        ok(f"{len(tickers)} tickers")
    except Exception as e:
        fail(f"multi-ticker: {e}")
    finally:
        await pool.__aexit__()

async def test_ohlcv():
    print("\n─ OHLCV ─")
    from ccxt_adapter import AdapterPool
    from market_data import MarketDataService
    pool = AdapterPool(exchange_ids=[PRIMARY])
    try:
        await pool.__aenter__()
        svc = MarketDataService(pool)
        ohlcv = await svc.get_ohlcv("BTC/USD", PRIMARY, "15m", limit=20)
        assert len(ohlcv.df) > 0, "empty"
        ok(f"{len(ohlcv.df)} candles, close ${ohlcv.df['close'].iloc[-1]:,.2f}")
    except Exception as e:
        fail(f"ohlcv: {e}")
    finally:
        await pool.__aexit__()

async def test_symbols():
    print("\n─ Symbols ─")
    from ccxt_adapter import AdapterPool
    from market_data import MarketDataService
    pool = AdapterPool(exchange_ids=[PRIMARY])
    try:
        await pool.__aenter__()
        svc = MarketDataService(pool)
        pairs = svc.find_symbols("BTC")
        ok(f"'BTC' matches on {PRIMARY}: {len(pairs.get(PRIMARY,[]))} pairs")
        info = svc.get_symbol_info("BTC/USD", PRIMARY)
        assert info, "no symbol info"
        ok(f"price precision: {info.get('precision',{}).get('price','?')}")
    except Exception as e:
        fail(f"symbols: {e}")
    finally:
        await pool.__aexit__()

async def test_degradation():
    print("\n─ Degradation ─")
    from ccxt_adapter import AdapterPool
    pool = AdapterPool(exchange_ids=[PRIMARY, "fake"])
    try:
        await pool.__aenter__()
        assert PRIMARY in pool.healthy_exchanges, f"{PRIMARY} not healthy"
        assert "fake" not in pool.healthy_exchanges, "fake made it in"
        ok(f"survived fake exchange, kept {PRIMARY}")
    except Exception as e:
        fail(f"degradation: {e}")
    finally:
        await pool.__aexit__()

async def main():
    print("=" * 50 + "\nFDC CCXT PHASE 1A SMOKE\n" + "=" * 50)
    for test in [
        test_version, test_pool, test_ticker, test_multi_ticker,
        test_ohlcv, test_symbols, test_degradation,
    ]:
        await test()
    print("=" * 50)
    if FAILURES:
        print(f"{FAILURES} FAILURE(S)")
        return 1
    print("ALL PASSED ✅")
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
