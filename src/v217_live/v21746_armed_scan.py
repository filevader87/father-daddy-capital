#!/usr/bin/env python3
"""V21.7.46 Armed Scan — Monitor BTC 15m DOWN ask for entry signal."""
import json, sys, requests, logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("v21746_scan")

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "output" / "v21746_live_cash_rebase"
OUT.mkdir(parents=True, exist_ok=True)

now = datetime.now(timezone.utc)

# ─── Discover BTC 15m market ───
log.info("Discovering BTC 15m market...")
from multi_market_scanner import discover_all_markets
markets = discover_all_markets()
btc_15m = [m for m in markets if 'btc' in m.get('slug', '').lower() and '15m' in m.get('slug', '').lower()]

if not btc_15m:
    result = {"timestamp": now.isoformat(), "status": "NO_MARKET", "down_ask": None, "zone": None}
    print(json.dumps(result))
    sys.exit(0)

m = btc_15m[0]
slug = m.get('slug', '')

# ─── Get fresh orderbook from Gamma ───
log.info(f"Querying Gamma for {slug}...")
r = requests.get(f'https://gamma-api.polymarket.com/markets?slug={slug}', timeout=15)
if r.status_code != 200:
    result = {"timestamp": now.isoformat(), "status": "GAMMA_ERROR", "down_ask": None}
    print(json.dumps(result))
    sys.exit(1)

mkts = r.json()
target = None
for mk in mkts:
    outcomes = mk.get('outcomes', '')
    if 'Up' in str(outcomes) and 'Down' in str(outcomes):
        target = mk
        break

if not target:
    result = {"timestamp": now.isoformat(), "status": "NO_UP_DOWN_MARKET", "down_ask": None}
    print(json.dumps(result))
    sys.exit(1)

# ─── Parse outcomes and token IDs ───
try:
    outcomes = json.loads(target.get('outcomes', '[]')) if isinstance(target.get('outcomes'), str) else target.get('outcomes', [])
except:
    outcomes = []
try:
    prices = json.loads(target.get('outcomePrices', '[]')) if isinstance(target.get('outcomePrices'), str) else target.get('outcomePrices', [])
except:
    prices = []
try:
    token_ids = json.loads(target.get('clobTokenIds', '[]')) if isinstance(target.get('clobTokenIds'), str) else target.get('clobTokenIds', [])
except:
    token_ids = []

# token_ids maps to outcomes in order: token_ids[0] → outcomes[0] ("Up"), token_ids[1] → outcomes[1] ("Down")
down_token_id = None
up_token_id = None
if len(token_ids) >= 2 and len(outcomes) >= 2:
    for i, outcome in enumerate(outcomes):
        if 'down' in str(outcome).lower():
            down_token_id = token_ids[i]
        elif 'up' in str(outcome).lower():
            up_token_id = token_ids[i]

log.info(f"Outcomes: {outcomes}")
log.info(f"Prices: {prices}")
log.info(f"DOWN token: {down_token_id[:20] if down_token_id else 'N/A'}...")
log.info(f"UP token: {up_token_id[:20] if up_token_id else 'N/A'}...")

# ─── TTE ───
end_date = target.get('endDate', '')
tte = 0
try:
    end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
    tte = (end_dt - now).total_seconds()
except:
    pass

# ─── Get CLOB orderbook for DOWN token ───
down_ask = None
up_ask = None
spread = None

if down_token_id:
    r3 = requests.get(f'https://clob.polymarket.com/book?token_id={down_token_id}', timeout=10)
    if r3.status_code == 200:
        book = r3.json()
        asks = sorted(book.get('asks', []), key=lambda x: float(x.get('price', 1)))
        bids = sorted(book.get('bids', []), key=lambda x: float(x.get('price', 0)), reverse=True)
        if asks:
            down_ask = float(asks[0]['price'])
            best_bid = float(bids[0]['price']) if bids else 0
            spread = round(down_ask - best_bid, 4)

if up_token_id:
    r4 = requests.get(f'https://clob.polymarket.com/book?token_id={up_token_id}', timeout=10)
    if r4.status_code == 200:
        book = r4.json()
        asks = sorted(book.get('asks', []), key=lambda x: float(x.get('price', 1)))
        if asks:
            up_ask = float(asks[0]['price'])

# Fallback to outcome prices
down_idx = None
for i, o in enumerate(outcomes):
    if 'down' in str(o).lower():
        down_idx = i
        break
if down_ask is None and down_idx is not None and len(prices) > down_idx:
    down_ask = float(prices[down_idx])

# ─── Determine zone ───
zone = "UNKNOWN"
signal = False
signal_type = None
if down_ask is not None:
    if 0.03 <= down_ask <= 0.08:
        zone = "TAIL_3_8"
        signal = True
        signal_type = "PRIORITY_1_TAIL_CANARY"
    elif 0.08 < down_ask <= 0.12:
        zone = "MICRO_8_12"
        signal = True
        signal_type = "PRIORITY_2_MICRO_CANARY"
    elif down_ask < 0.03:
        zone = "BELOW_RANGE"
    elif 0.12 < down_ask <= 0.50:
        zone = "MIDZONE"
    else:
        zone = "HIGH"

tte_ok = 180 <= tte <= 900
can_submit = signal and tte_ok and (spread is not None and spread <= 0.02)

# ─── Result ───
result = {
    "timestamp": now.isoformat(),
    "version": "V21.7.46",
    "slug": slug,
    "down_ask": down_ask,
    "down_ask_cents": round(down_ask * 100, 1) if down_ask else None,
    "up_ask": up_ask,
    "up_ask_cents": round(up_ask * 100, 1) if up_ask else None,
    "spread_cents": round(spread * 100, 1) if spread else None,
    "zone": zone,
    "signal": signal,
    "signal_type": signal_type,
    "tte_seconds": round(tte),
    "tte_ok": tte_ok,
    "can_submit": can_submit,
    "mode": "MICRO_LIVE_ARMED_NO_SIGNAL",
    "action": "SUBMIT_ORDER" if can_submit else "WAIT",
    "next_scan": "60s",
}

# Write to scan log
with open(OUT / "armed_scan_log.jsonl", "a") as f:
    f.write(json.dumps(result) + "\n")

log.info(f"DOWN ask: {down_ask*100:.1f}¢  zone: {zone}  TTE: {tte:.0f}s  signal: {signal}  can_submit: {can_submit}" if down_ask else f"DOWN ask: N/A  zone: {zone}  TTE: {tte:.0f}s  signal: {signal}  can_submit: {can_submit}")
print(json.dumps(result, indent=2))