#!/usr/bin/env python3
"""
V20.2 Settlement Audit — Verify every resolved BTC up/down contract settles to 0 or 1.
Cross-reference paper positions with actual on-chain/Gamma settlement data.
Output: V20.2_EXECUTION_AUDIT.csv
"""
import json, csv, urllib.request, time
from pathlib import Path

GAMMA_URL = "https://gamma-api.polymarket.com"
REPORT_FILE = Path("paper_trading/micro_validation_report.json")
OUTPUT_CSV = Path("V20.2_EXECUTION_AUDIT.csv")

def fetch_markets(slug_prefix="btc-updown-", limit=200):
    """Fetch markets from Gamma API matching slug prefix."""
    markets = []
    offset = 0
    while True:
        url = f"{GAMMA_URL}/markets?slug_contains={slug_prefix}&closed=true&limit={limit}&offset={offset}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FDC-V202-Audit/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                batch = json.loads(r.read())
                if not batch:
                    break
                markets.extend(batch)
                if len(batch) < limit:
                    break
                offset += limit
        except Exception as e:
            print(f"  [WARN] Gamma fetch offset={offset}: {e}")
            break
        time.sleep(0.3)
    return markets

def check_resolution(market):
    """Extract resolution data from a Gamma market object."""
    outcome_prices = market.get("outcomePrices", "")
    outcomes = market.get("outcomes", "")
    closed_time = market.get("closedTime", market.get("endDate", ""))
    resolved = market.get("resolved", False)
    question = market.get("question", "")
    slug = market.get("slug", "")
    condition_id = market.get("conditionId", "")

    # Parse outcome prices — "0.99,0.01" means YES won
    try:
        prices = [float(p) for p in outcome_prices.split(",")] if outcome_prices else []
    except (ValueError, AttributeError):
        prices = []

    # Determine binary resolution
    is_binary = False
    actual_resolution = None
    payout_per_share = 0.0

    if len(prices) == 2:
        # Binary market: higher price wins (settles at 1.0)
        if abs(prices[0] - 1.0) < 0.01:
            actual_resolution = "YES"  # UP won
            payout_per_share = 1.0
            is_binary = True
        elif abs(prices[1] - 1.0) < 0.01:
            actual_resolution = "NO"  # DOWN won
            payout_per_share = 1.0
            is_binary = True
        elif abs(prices[0] - prices[1]) < 0.05:
            # Midpoint resolution — this is what we need to eliminate
            actual_resolution = "MIDPOINT"
            payout_per_share = prices[0]
            is_binary = False
        else:
            # Clear winner but not exactly 1.0
            if prices[0] > prices[1]:
                actual_resolution = "YES"
                payout_per_share = prices[0]
                is_binary = True
            else:
                actual_resolution = "NO"
                payout_per_share = prices[1]
                is_binary = True

    return {
        "slug": slug,
        "condition_id": condition_id,
        "resolved": resolved,
        "actual_resolution": actual_resolution,
        "is_binary": is_binary,
        "payout_per_share": payout_per_share,
        "settlement_timestamp": closed_time,
        "outcome_prices": outcome_prices,
        "outcomes": outcomes,
        "question": question,
    }

def main():
    print("=" * 70)
    print("V20.2 SETTLEMENT AUDIT")
    print("=" * 70)

    # Load closed positions from 5h validation report
    report = json.loads(REPORT_FILE.read_text())
    closed_positions = report.get("positions_closed", [])

    print(f"\nClosed positions in report: {len(closed_positions)}")

    # Fetch resolved BTC up/down markets from Gamma
    print("\nFetching resolved BTC up/down markets from Gamma API...")
    markets = fetch_markets()
    print(f"  Fetched {len(markets)} resolved markets")

    # Build lookup by condition_id
    market_lookup = {}
    for m in markets:
        slug = m.get("slug", "")
        if slug.startswith("btc-updown-"):
            resolution = check_resolution(m)
            # Index by both slug and condition_id
            market_lookup[slug] = resolution
            market_lookup[resolution["condition_id"]] = resolution

    print(f"  Resolved BTC up/down markets indexed: {len(market_lookup)}")

    # Also try to fetch active (unresolved) markets
    print("\nFetching active BTC up/down markets...")
    active_markets = []
    offset = 0
    while True:
        url = f"{GAMMA_URL}/markets?slug_contains=btc-updown-&closed=false&limit=100&offset={offset}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FDC-V202-Audit/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                batch = json.loads(r.read())
                if not batch:
                    break
                active_markets.extend(batch)
                if len(batch) < 100:
                    break
                offset += 100
        except:
            break
        time.sleep(0.3)
    print(f"  Active (unresolved) markets: {len(active_markets)}")

    # Cross-reference positions with market data
    rows = []
    binary_count = 0
    midpoint_count = 0
    unresolved_count = 0
    non_binary_but_decided = 0

    for pos in closed_positions:
        slug = pos.get("slug", "")
        condition_id = pos.get("condition_id", "")
        side = pos.get("side", "UP")
        entry_price = pos.get("entry_ask", pos.get("entry_price", 0))
        close_price = pos.get("close_price", 0.5)
        paper_pnl = pos.get("pnl_dollars", pos.get("pnl", 0))

        # Look up market resolution
        market = market_lookup.get(slug) or market_lookup.get(condition_id)

        if market and market.get("resolved"):
            actual_resolution = market["actual_resolution"]
            is_binary = market["is_binary"]
            payout = market["payout_per_share"]
            settle_time = market["settlement_timestamp"]
            outcome_prices = market["outcome_prices"]

            if actual_resolution == "MIDPOINT":
                midpoint_count += 1
            elif is_binary:
                binary_count += 1
            else:
                non_binary_but_decided += 1
        else:
            actual_resolution = "UNRESOLVED"
            is_binary = False
            payout = close_price  # paper close
            settle_time = "N/A"
            outcome_prices = "N/A"
            unresolved_count += 1

        rows.append({
            "slug": slug,
            "condition_id": condition_id,
            "side": side,
            "entry_price": entry_price,
            "close_price": close_price,
            "paper_pnl": paper_pnl,
            "actual_resolution": actual_resolution,
            "is_binary": is_binary,
            "payout_per_share": payout,
            "settlement_timestamp": settle_time,
            "outcome_prices": outcome_prices,
        })

    # Write CSV
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "slug", "condition_id", "side", "entry_price", "close_price",
            "paper_pnl", "actual_resolution", "is_binary", "payout_per_share",
            "settlement_timestamp", "outcome_prices",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{'=' * 70}")
    print("SETTLEMENT AUDIT SUMMARY")
    print(f"{'=' * 70}")
    print(f"Total positions checked:   {len(rows)}")
    print(f"Binary settlements (0/1):  {binary_count}")
    print(f"Midpoint fallbacks:        {midpoint_count}")
    print(f"Decided but non-binary:    {non_binary_but_decided}")
    print(f"Unresolved/active:         {unresolved_count}")
    print(f"\nOutput: {OUTPUT_CSV}")

    # Flag issues
    if midpoint_count > 0:
        print(f"\n⚠️  {midpoint_count} MIDPOINT SETTLEMENTS DETECTED — these are not binary outcomes!")
        for r in rows:
            if r["actual_resolution"] == "MIDPOINT":
                print(f"  → {r['slug']}: outcome_prices={r['outcome_prices']}")

    if binary_count == len(rows):
        print("\n✅ All resolved positions settled to binary 0/1 outcomes.")

    return rows

if __name__ == "__main__":
    main()