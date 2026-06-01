#!/usr/bin/env python3
"""V19.8 MarketScheduleCache — caches discovery results with TTL-based refresh.

Reduces cycle time by only calling providers when their TTL expires.
Target: discovery <= 20% of cycle, cache_hit_rate >= 80%.
"""

import json, os, time, re, threading
from datetime import datetime, timezone, timedelta

sys_path_dir = '/mnt/c/Users/12035/father_daddy_capital'
if sys_path_dir not in __import__('sys').path:
    __import__('sys').path.insert(0, sys_path_dir)

import discovery_providers as dp
import pm_engine_v19_7 as eng

# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

CACHE_FILE = os.path.join(os.path.dirname(__file__) or ".", "paper_trading", "cache", "market_schedule_cache.json")
CACHE_DIR = os.path.dirname(CACHE_FILE)

# Provider TTL in seconds
PROVIDER_TTL = {
    "slug_provider": 120,           # 2 minutes
    "discover_markets": 120,        # 2 minutes (main heavy call)
    "tag_explorer": 600,            # 10 minutes
    "gamma_markets_provider": 600,  # 10 minutes
}

# ══════════════════════════════════════════════════════════════════════════════
# MarketScheduleCache
# ══════════════════════════════════════════════════════════════════════════════

class MarketScheduleCache:
    def __init__(self):
        self.entries = {}         # keyed by slug
        self.last_refresh = {}    # provider_name -> timestamp
        self.cache_hits = 0
        self.cache_misses = 0
        self.timing = {
            "time_spent_slug_resolution": 0,
            "time_spent_tag_explorer": 0,
            "time_spent_gamma_markets": 0,
            "time_spent_discover_markets": 0,
            "time_spent_book_fetch": 0,
            "time_spent_spot_fetch": 0,
        }
        self._load()

    def _load(self):
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, "r") as f:
                    data = json.load(f)
                self.entries = data.get("entries", {})
                self.last_refresh = data.get("last_refresh", {})
                self.cache_hits = data.get("cache_hits", 0)
                self.cache_misses = data.get("cache_misses", 0)
        except Exception:
            self.entries = {}
            self.last_refresh = {}

    def save(self):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump({
                    "entries": self.entries,
                    "last_refresh": self.last_refresh,
                    "cache_hits": self.cache_hits,
                    "cache_misses": self.cache_misses,
                }, f, default=str)
        except Exception:
            pass

    def _needs_refresh(self, provider_name):
        last = self.last_refresh.get(provider_name, 0)
        ttl = PROVIDER_TTL.get(provider_name, 120)
        return (time.time() - last) >= ttl

    def _mark_refreshed(self, provider_name):
        self.last_refresh[provider_name] = time.time()

    @staticmethod
    def _parse_outcome_price(outcome_prices_str, index, default=0):
        """Parse outcomePrices string like '[\"0.505\", \"0.495\"]' into float."""
        if not outcome_prices_str:
            return default
        try:
            import ast
            prices = ast.literal_eval(outcome_prices_str)
            if isinstance(prices, list) and len(prices) > index:
                return float(prices[index])
        except Exception:
            pass
        # Fallback: strip quotes/brackets manually
        try:
            cleaned = outcome_prices_str.strip("[]").replace('"', '').replace("'", '')
            parts = [p.strip() for p in cleaned.split(",")]
            if len(parts) > index:
                return float(parts[index])
        except Exception:
            pass
        return default

    def _ingest_slug_result(self, result, asset_key, interval):
        """Ingest markets from slug_provider result dict."""
        if not isinstance(result, dict):
            return
        markets = result.get("markets", [])
        for m in markets:
            slug = m.get("slug", "") or m.get("_slug", "")
            if not slug:
                continue
            existing = self.entries.get(slug, {})
            # slug_provider returns rich market dicts with price data
            clob_tids = m.get("clobTokenIds", "")
            tids = []
            try:
                import ast
                tids = ast.literal_eval(clob_tids) if clob_tids else []
            except Exception:
                pass
            
            self.entries[slug] = {
                "slug": slug,
                "asset": asset_key.upper() if len(asset_key) <= 4 else asset_key,
                "interval": interval,
                "status": existing.get("status", "ACTIVE" if m.get("active") else "FUTURE"),
                "conditionId": m.get("conditionId", existing.get("conditionId", "")),
                "UP_token_id": tids[0] if len(tids) > 0 else existing.get("UP_token_id", ""),
                "DOWN_token_id": tids[1] if len(tids) > 1 else existing.get("DOWN_token_id", ""),
                "market_start_time": m.get("eventStartTime", m.get("startDate", existing.get("market_start_time", ""))),
                "market_end_time": m.get("endDate", existing.get("market_end_time", "")),
                "up_price": self._parse_outcome_price(m.get("outcomePrices", ""), 0, existing.get("up_price", 0)),
                "down_price": self._parse_outcome_price(m.get("outcomePrices", ""), 1, existing.get("down_price", 0)),
                "up_bid": m.get("bestBid", existing.get("up_bid", 0)),
                "down_bid": m.get("bestBid", existing.get("down_bid", 0)),
                "spread": m.get("spread", existing.get("spread", 0)),
                "liquidity": m.get("liquidityNum", m.get("liquidityClob", existing.get("liquidity", 0))),
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
                "source_provider": existing.get("source_provider", "slug_provider"),
            }

    def _ingest_discover_result(self, result, asset_key):
        """Ingest markets from discover_markets result dict."""
        if not isinstance(result, dict):
            return
        for market_list in [result.get("valid", []), result.get("future", [])]:
            for m in market_list:
                if not isinstance(m, dict):
                    continue
                slug = m.get("slug", "") or m.get("_slug", "")
                if not slug:
                    continue
                existing = self.entries.get(slug, {})
                classification = m.get("classification", "")
                
                if classification in (dp.CLASS_WRONG_INSTRUMENT, dp.CLASS_AMBIGUOUS):
                    self.entries[slug] = {
                        "slug": slug,
                        "asset": m.get("asset", asset_key),
                        "interval": m.get("interval", ""),
                        "status": "REJECTED",
                        "conditionId": m.get("conditionId", ""),
                        "last_seen_at": datetime.now(timezone.utc).isoformat(),
                        "source_provider": "discover_markets",
                        "reject_reason": classification,
                    }
                    continue
                
                self.entries[slug] = {
                    "slug": slug,
                    "asset": m.get("asset", asset_key),
                    "interval": m.get("interval", existing.get("interval", "")),
                    "status": self._compute_status(m, existing),
                    "conditionId": m.get("conditionId", m.get("condition_id", existing.get("conditionId", ""))),
                    "UP_token_id": m.get("UP_token_id", m.get("up_token_id", existing.get("UP_token_id", ""))),
                    "DOWN_token_id": m.get("DOWN_token_id", m.get("down_token_id", existing.get("DOWN_token_id", ""))),
                    "market_start_time": m.get("market_start_time", m.get("start_date", existing.get("market_start_time", ""))),
                    "market_end_time": m.get("market_end_time", m.get("end_date", existing.get("market_end_time", ""))),
                    "up_price": m.get("up_price", existing.get("up_price", 0)),
                    "down_price": m.get("down_price", existing.get("down_price", 0)),
                    "up_bid": m.get("up_bid", existing.get("up_bid", 0)),
                    "down_bid": m.get("down_bid", existing.get("down_bid", 0)),
                    "spread": m.get("spread", existing.get("spread", 0)),
                    "liquidity": m.get("liquidity", existing.get("liquidity", 0)),
                    "last_seen_at": datetime.now(timezone.utc).isoformat(),
                    "source_provider": existing.get("source_provider", "discover_markets"),
                }

    def _compute_status(self, market, existing=None):
        """Compute status from market dict."""
        classification = market.get("classification", "")
        if classification in (dp.CLASS_WRONG_INSTRUMENT, dp.CLASS_AMBIGUOUS):
            return "REJECTED"
        if classification in (dp.CLASS_CLOSED, dp.CLASS_EXPIRED):
            return "EXPIRED"
        
        start_str = market.get("market_start_time", market.get("start_date", ""))
        end_str = market.get("market_end_time", market.get("end_date", ""))
        now = datetime.now(timezone.utc)
        
        try:
            if start_str:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if now < start_dt:
                    interval = market.get("interval", existing.get("interval", "5m") if existing else "5m")
                    look_ahead = timedelta(minutes=10 if interval == "5m" else 30)
                    return "PREWATCH" if (start_dt - now) <= look_ahead else "FUTURE"
            if end_str:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if now > end_dt:
                    return "EXPIRED"
        except Exception:
            pass
        
        if market.get("active", True):
            return "ACTIVE"
        return existing.get("status", "FUTURE") if existing else "FUTURE"

    def refresh(self, force=False):
        """Refresh cache from providers whose TTL expired. Returns timing dict."""
        timing = {}
        
        # Slug provider — fast, provides rich CLOB data
        if force or self._needs_refresh("slug_provider"):
            t0 = time.time()
            for asset_key in eng.ASSETS:
                for interval in ["5m", "15m"]:
                    try:
                        result = dp.slug_provider(asset_key=asset_key, interval=interval, look_ahead=5)
                        self._ingest_slug_result(result, asset_key, interval)
                    except Exception:
                        pass
            self._mark_refreshed("slug_provider")
            timing["time_spent_slug_resolution"] = time.time() - t0
            self.timing["time_spent_slug_resolution"] = timing["time_spent_slug_resolution"]

        # discover_markets — slow (~13s), provides classified markets
        if force or self._needs_refresh("discover_markets"):
            t0 = time.time()
            for asset_key in eng.ASSETS:
                try:
                    result = dp.discover_markets(asset_key=asset_key, look_ahead=5)
                    self._ingest_discover_result(result, asset_key)
                except Exception:
                    pass
            self._mark_refreshed("discover_markets")
            timing["time_spent_discover_markets"] = time.time() - t0
            self.timing["time_spent_discover_markets"] = timing["time_spent_discover_markets"]

        # Tag explorer — for conditionId/token resolution
        if force or self._needs_refresh("tag_explorer"):
            t0 = time.time()
            try:
                result = dp.tag_explorer(max_pages=2)
                self._ingest_discover_result({"valid": result if isinstance(result, list) else []}, "ALL")
            except Exception:
                pass
            self._mark_refreshed("tag_explorer")
            timing["time_spent_tag_explorer"] = time.time() - t0
            self.timing["time_spent_tag_explorer"] = timing["time_spent_tag_explorer"]

        # Gamma markets — for resolution data
        if force or self._needs_refresh("gamma_markets_provider"):
            t0 = time.time()
            try:
                result = dp.gamma_markets_provider(max_pages=3, page_size=500)
                self._ingest_discover_result({"valid": result if isinstance(result, list) else []}, "ALL")
            except Exception:
                pass
            self._mark_refreshed("gamma_markets_provider")
            timing["time_spent_gamma_markets"] = time.time() - t0
            self.timing["time_spent_gamma_markets"] = timing["time_spent_gamma_markets"]

        self._update_statuses()
        self.expire_old_entries()
        self.save()
        return timing

    def _update_statuses(self):
        """Update FUTURE/PREWATCH/ACTIVE/EXPIRED based on current time."""
        now = datetime.now(timezone.utc)
        for slug, entry in self.entries.items():
            if entry.get("status") in ("REJECTED",):
                continue
            start_str = entry.get("market_start_time", "")
            end_str = entry.get("market_end_time", "")
            if not start_str:
                continue
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                interval = entry.get("interval", "5m")
                look_ahead = timedelta(minutes=10 if interval == "5m" else 30)
                
                if now < start_dt:
                    entry["status"] = "PREWATCH" if (start_dt - now) <= look_ahead else "FUTURE"
                    continue
                
                if end_str:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    if now > end_dt and (now - end_dt).total_seconds() > 1800:
                        entry["status"] = "EXPIRED"
                    else:
                        entry["status"] = "ACTIVE"
                else:
                    entry["status"] = "ACTIVE"
            except Exception:
                pass

    def get_active_markets(self, asset=None):
        """Get ACTIVE and PREWATCH markets, filtered by asset if specified."""
        results = []
        for slug, entry in self.entries.items():
            if entry.get("status") not in ("ACTIVE", "PREWATCH"):
                continue
            if asset and entry.get("asset", "").upper() != asset.upper():
                continue
            results.append(entry)
        self.cache_hits += len(results)
        return results

    def get_valid_markets(self, asset=None, interval=None):
        """Get valid (non-rejected, non-expired) markets."""
        results = []
        for entry in self.entries.values():
            if entry.get("status") in ("REJECTED", "EXPIRED"):
                continue
            if asset and entry.get("asset", "").upper() != asset.upper():
                continue
            if interval and entry.get("interval") != interval:
                continue
            results.append(entry)
        return results

    def expire_old_entries(self):
        """Remove EXPIRED entries >30 min past end."""
        to_remove = []
        for slug, entry in self.entries.items():
            if entry.get("status") == "EXPIRED":
                end_str = entry.get("market_end_time", "")
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        if (datetime.now(timezone.utc) - end_dt).total_seconds() > 1800:
                            to_remove.append(slug)
                    except Exception:
                        pass
        for slug in to_remove:
            del self.entries[slug]

    def get_cache_stats(self):
        """Return cache statistics."""
        total = len(self.entries)
        by_status, by_asset = {}, {}
        for entry in self.entries.values():
            s = entry.get("status", "UNKNOWN")
            by_status[s] = by_status.get(s, 0) + 1
            a = entry.get("asset", "UNKNOWN")
            by_asset[a] = by_asset.get(a, 0) + 1
        total_ops = self.cache_hits + self.cache_misses
        hit_rate = self.cache_hits / total_ops if total_ops > 0 else 0
        return {
            "total_entries": total,
            "by_status": by_status,
            "by_asset": by_asset,
            "cache_hit_rate": round(hit_rate, 4),
            "total_hits": self.cache_hits,
            "total_misses": self.cache_misses,
            "provider_last_refresh": {
                k: datetime.fromtimestamp(v, tz=timezone.utc).isoformat() 
                for k, v in self.last_refresh.items()
            },
        }

    def upsert_contract(self, contract):
        """Insert or update a contract dict in the cache."""
        slug = contract.get("slug", "")
        if not slug:
            return
        existing = self.entries.get(slug, {})
        classification = contract.get("classification", "")
        
        if classification in (dp.CLASS_WRONG_INSTRUMENT, dp.CLASS_AMBIGUOUS):
            self.entries[slug] = {"slug": slug, "status": "REJECTED", "reject_reason": classification,
                                  "last_seen_at": datetime.now(timezone.utc).isoformat()}
            return
        
        self.entries[slug] = {
            "slug": slug,
            "asset": contract.get("asset", existing.get("asset", "")),
            "interval": contract.get("interval", existing.get("interval", "")),
            "status": self._compute_status(contract, existing),
            "conditionId": contract.get("conditionId", contract.get("condition_id", existing.get("conditionId", ""))),
            "UP_token_id": contract.get("UP_token_id", contract.get("up_token_id", existing.get("UP_token_id", ""))),
            "DOWN_token_id": contract.get("DOWN_token_id", contract.get("down_token_id", existing.get("DOWN_token_id", ""))),
            "market_start_time": contract.get("market_start_time", contract.get("start_date", existing.get("market_start_time", ""))),
            "market_end_time": contract.get("market_end_time", contract.get("end_date", existing.get("market_end_time", ""))),
            "up_price": contract.get("up_price", existing.get("up_price", 0)),
            "down_price": contract.get("down_price", existing.get("down_price", 0)),
            "up_bid": contract.get("up_bid", existing.get("up_bid", 0)),
            "down_bid": contract.get("down_bid", existing.get("down_bid", 0)),
            "spread": contract.get("spread", existing.get("spread", 0)),
            "liquidity": contract.get("liquidity", existing.get("liquidity", 0)),
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "source_provider": existing.get("source_provider", "unknown"),
        }


# ══════════════════════════════════════════════════════════════════════════════
# Integration helpers
# ══════════════════════════════════════════════════════════════════════════════

_msc_instance = None

def get_msc():
    """Get or create the MarketScheduleCache singleton."""
    global _msc_instance
    if _msc_instance is None:
        _msc_instance = MarketScheduleCache()
        _msc_instance.refresh(force=True)
    return _msc_instance


def discover_contracts_cached(asset_key, msc=None):
    """Drop-in replacement for eng.discover_contracts() using cache.
    
    Returns contract list in the same format as eng.discover_contracts().
    """
    if msc is None:
        msc = get_msc()
    
    # Refresh providers whose TTL expired
    msc.refresh()
    
    # Get valid markets for this asset
    asset_upper = asset_key.upper() if len(asset_key) <= 4 else asset_key.upper()
    valid = msc.get_valid_markets(asset=asset_upper)
    
    if not valid:
        msc.cache_misses += 1
        try:
            contracts = eng.discover_contracts(asset_key)
            for c in contracts:
                msc.upsert_contract(c)
            return contracts
        except Exception:
            return []
    
    # Convert to contract-like dicts compatible with downstream
    contracts = []
    for entry in valid:
        c = dict(entry)
        c["condition_id"] = c.get("condition_id") or c.get("conditionId", "")
        c["start_date"] = c.get("start_date") or c.get("market_start_time", "")
        c["end_date"] = c.get("end_date") or c.get("market_end_time", "")
        contracts.append(c)
    
    # Backfill from live if cache has fewer than expected
    if len(contracts) < 2:
        try:
            live_contracts = eng.discover_contracts(asset_key)
            seen = {c.get("slug") for c in contracts if c.get("slug")}
            for lc in live_contracts:
                if lc.get("slug") and lc["slug"] not in seen:
                    contracts.append(lc)
                    msc.upsert_contract(lc)
                    seen.add(lc["slug"])
        except Exception:
            pass
    
    return contracts


if __name__ == "__main__":
    print("MarketScheduleCache — self-test")
    msc = MarketScheduleCache()
    
    print("\n  Refreshing slug_provider only...")
    t0 = time.time()
    for asset_key in eng.ASSETS:
        for interval in ["5m", "15m"]:
            try:
                result = dp.slug_provider(asset_key=asset_key, interval=interval, look_ahead=3)
                msc._ingest_slug_result(result, asset_key, interval)
            except Exception as e:
                print(f"  Error {asset_key}/{interval}: {e}")
    t1 = time.time()
    print(f"  slug_provider total: {t1-t0:.2f}s")
    msc._mark_refreshed("slug_provider")
    msc._update_statuses()
    msc.save()
    
    stats = msc.get_cache_stats()
    print(f"\n  Entries: {stats['total_entries']}")
    print(f"  By status: {stats['by_status']}")
    print(f"  By asset: {stats['by_asset']}")