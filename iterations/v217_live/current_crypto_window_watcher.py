#!/usr/bin/env python3
"""
V21.7.10 Current Crypto Window Watcher — §8
Continuously monitors 5m/15m market availability every 5s.
"""
import json, time, datetime, os, urllib.request, logging

log = logging.getLogger("window_watcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler("output/v21710_discovery/watcher.log"),
                              logging.StreamHandler()])

OUTPUT_DIR = "output/v21710_discovery"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
WATCHL = f"{OUTPUT_DIR}/current_window_watch.jsonl"

def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "FDC-v21710"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def check_window(asset, interval, window, now):
    current = (now // window) * window
    results = []
    for offset in [0, 1]:  # current + next
        ts = current + (offset * window)
        slug = f"{asset}-updown-{interval}-{ts}"
        found = False
        cid = ""
        up_tid = ""
        down_tid = ""
        reject = ""
        
        try:
            data = _get(f"{GAMMA_URL}/events?slug={slug}")
            if isinstance(data, list) and data:
                ev = data[0]
                for m in ev.get("markets", []):
                    cid = m.get("conditionId", "")
                    raw = m.get("clobTokenIds", "[]")
                    if isinstance(raw, str):
                        tids = json.loads(raw)
                    else:
                        tids = raw
                    if len(tids) >= 2:
                        up_tid = tids[0][:40]
                        down_tid = tids[1][:40]
                        found = True
                    else:
                        reject = "no_clobTokenIds"
            else:
                reject = "event_not_found"
        except Exception as e:
            reject = str(e)[:60]
        
        results.append({
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "asset": asset.upper(),
            "interval": interval,
            "expected_slug": slug,
            "event_found": found,
            "market_found": found,
            "condition_id": cid[:30] if cid else "",
            "up_token_id": up_tid,
            "down_token_id": down_tid,
            "ws_subscription_status": "AVAILABLE" if found else "N/A",
            "time_to_expiry_s": ts + window - now,
            "reject_reason": reject,
        })
    return results

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log.info("Window watcher STARTING")
    
    cycle = 0
    while True:
        now = int(time.time())
        rows = []
        for asset in ["btc", "eth", "sol", "xrp"]:
            for interval, window in [("5m", 300), ("15m", 900)]:
                rows.extend(check_window(asset, interval, window, now))
        
        with open(WATCHL, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        
        cycle += 1
        found_count = sum(1 for r in rows if r["event_found"])
        if cycle % 12 == 0:  # Log every ~60s
            log.info(f"Watch cycle {cycle}: {found_count}/{len(rows)} markets found")
        
        time.sleep(5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Window watcher STOPPED")