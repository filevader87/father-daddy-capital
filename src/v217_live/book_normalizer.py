"""
V21.7.24 — Book Normalizer
==========================
Shared module for normalizing Polymarket CLOB order book data.

CRITICAL: The CLOB API returns asks in DESCENDING order (worst first)
and bids in ASCENDING order (worst first). This module enforces:
  best_ask = min(ask prices)
  best_bid = max(bid prices)
  spread = best_ask - best_bid

No module may use raw asks[0] or bids[0] for live pricing.
All must call normalize_orderbook() or an approved wrapper.

Classification: P0 execution-safety infrastructure
"""

import time
import json
import logging
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger('book_normalizer')

# ─── Output paths ───
PROJECT_ROOT = Path("/home/naq1987s/father-daddy-capital")
SANITY_LOG = PROJECT_ROOT / "output" / "v21724_price_integrity" / "runtime_book_sanity.jsonl"


@dataclass
class NormalizedBook:
    """Normalized order book with validated prices."""
    token_id: str
    condition_id: Optional[str]
    side: str  # UP or DOWN
    raw_bid_count: int
    raw_ask_count: int
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]
    midpoint: Optional[float]
    bid_depth_at_best: Optional[float]
    ask_depth_at_best: Optional[float]
    total_bid_depth: float
    total_ask_depth: float
    book_timestamp: Optional[str]
    received_at: str
    book_age_ms: Optional[float]
    source: str
    is_valid: bool
    reject_reason: str
    # Runtime sanity fields
    raw_first_ask: Optional[float]
    raw_first_bid: Optional[float]
    price_source: str  # NORMALIZED_BOOK or UNSET

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_orderbook(
    raw_book: Dict[str, Any],
    token_id: str = "",
    condition_id: Optional[str] = None,
    side: str = "UNKNOWN",
    source: str = "PM_CLOB_READ",
    book_age_ms: Optional[float] = None,
    received_at: Optional[str] = None,
) -> NormalizedBook:
    """
    Normalize a raw CLOB order book response.

    Enforces:
      best_bid = max(bid prices) — highest price buyer pays
      best_ask = min(ask prices) — lowest price seller accepts
      spread = best_ask - best_bid (must be >= 0 for valid book)

    Args:
        raw_book: Dict with 'asks' and 'bids' lists, each containing
                  dicts with 'price' and 'size' keys.
        token_id: The CLOB token ID.
        condition_id: The market condition ID.
        side: UP or DOWN.
        source: Quote source label.
        book_age_ms: Age of the book data in ms.
        received_at: ISO timestamp when received.

    Returns:
        NormalizedBook with validated prices.
    """
    if received_at is None:
        received_at = datetime.now(timezone.utc).isoformat()

    asks = raw_book.get("asks", [])
    bids = raw_book.get("bids", [])

    # Track raw first elements for sanity logging
    raw_first_ask = float(asks[0]["price"]) if asks else None
    raw_first_bid = float(bids[0]["price"]) if bids else None

    # Parse all prices
    ask_prices = []
    ask_sizes = []
    for level in asks:
        try:
            p = float(level.get("price", 0))
            s = float(level.get("size", 0))
            if 0.0 <= p <= 1.0:
                ask_prices.append(p)
                ask_sizes.append(s)
        except (ValueError, TypeError):
            continue

    bid_prices = []
    bid_sizes = []
    for level in bids:
        try:
            p = float(level.get("price", 0))
            s = float(level.get("size", 0))
            if 0.0 <= p <= 1.0:
                bid_prices.append(p)
                bid_sizes.append(s)
        except (ValueError, TypeError):
            continue

    # Empty side check
    if not ask_prices or not bid_prices:
        return NormalizedBook(
            token_id=token_id,
            condition_id=condition_id,
            side=side,
            raw_bid_count=len(bids),
            raw_ask_count=len(asks),
            best_bid=None,
            best_ask=None,
            spread=None,
            midpoint=None,
            bid_depth_at_best=None,
            ask_depth_at_best=None,
            total_bid_depth=sum(bid_sizes) if bid_sizes else 0.0,
            total_ask_depth=sum(ask_sizes) if ask_sizes else 0.0,
            book_timestamp=raw_book.get("timestamp"),
            received_at=received_at,
            book_age_ms=book_age_ms,
            source=source,
            is_valid=False,
            reject_reason="EMPTY_BID_OR_ASK_SIDE",
            raw_first_ask=raw_first_ask,
            raw_first_bid=raw_first_bid,
            price_source="NORMALIZED_BOOK",
        )

    # ─── CORE NORMALIZATION ───
    best_ask = min(ask_prices)   # Lowest ask = best price to buy
    best_bid = max(bid_prices)   # Highest bid = best price to sell
    spread = best_ask - best_bid
    midpoint = (best_bid + best_ask) / 2.0

    # Depth at best prices
    ask_depth_at_best = sum(
        s for p, s in zip(ask_prices, ask_sizes) if abs(p - best_ask) < 0.001
    )
    bid_depth_at_best = sum(
        s for p, s in zip(bid_prices, bid_sizes) if abs(p - best_bid) < 0.001
    )

    # ─── Validation ───
    is_valid = True
    reject_reason = ""

    if spread < 0:
        is_valid = False
        reject_reason = "CROSSED_OR_MISPARSED_BOOK"
    elif spread > 0.5:
        is_valid = False
        reject_reason = f"SPREAD_TOO_WIDE_{spread:.4f}"
    elif not (0.0 <= best_ask <= 1.0):
        is_valid = False
        reject_reason = f"ASK_OUT_OF_RANGE_{best_ask}"
    elif not (0.0 <= best_bid <= 1.0):
        is_valid = False
        reject_reason = f"BID_OUT_OF_RANGE_{best_bid}"
    elif best_bid > best_ask:
        is_valid = False
        reject_reason = "CROSSED_BOOK_BID_EXCEEDS_ASK"

    result = NormalizedBook(
        token_id=token_id,
        condition_id=condition_id,
        side=side,
        raw_bid_count=len(bids),
        raw_ask_count=len(asks),
        best_bid=round(best_bid, 6),
        best_ask=round(best_ask, 6),
        spread=round(spread, 6),
        midpoint=round(midpoint, 6),
        bid_depth_at_best=round(bid_depth_at_best, 4),
        ask_depth_at_best=round(ask_depth_at_best, 4),
        total_bid_depth=round(sum(bid_sizes), 4),
        total_ask_depth=round(sum(ask_sizes), 4),
        book_timestamp=raw_book.get("timestamp"),
        received_at=received_at,
        book_age_ms=book_age_ms,
        source=source,
        is_valid=is_valid,
        reject_reason=reject_reason,
        raw_first_ask=raw_first_ask,
        raw_first_bid=raw_first_bid,
        price_source="NORMALIZED_BOOK",
    )

    # ─── Runtime sanity logging ───
    if raw_first_ask is not None and abs(raw_first_ask - best_ask) > 0.01:
        SANITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SANITY_LOG, "a") as f:
            json.dump({
                "timestamp": received_at,
                "warning": "RAW_ASK_ORDER_DIFFERS_FROM_BEST_ASK",
                "token_id": token_id[:40],
                "raw_first_ask": raw_first_ask,
                "normalized_best_ask": best_ask,
                "delta": round(raw_first_ask - best_ask, 4),
                "source": source,
            }, f)
            f.write("\n")
        log.warning(
            f"RAW_ASK_ORDER_DIFFERS_FROM_BEST_ASK: "
            f"raw_first={raw_first_ask} vs normalized_best={best_ask} "
            f"delta={raw_first_ask - best_ask:.4f}"
        )

    if raw_first_bid is not None and abs(raw_first_bid - best_bid) > 0.01:
        SANITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SANITY_LOG, "a") as f:
            json.dump({
                "timestamp": received_at,
                "warning": "RAW_BID_ORDER_DIFFERS_FROM_BEST_BID",
                "token_id": token_id[:40],
                "raw_first_bid": raw_first_bid,
                "normalized_best_bid": best_bid,
                "delta": round(raw_first_bid - best_bid, 4),
                "source": source,
            }, f)
            f.write("\n")

    return result


def normalize_for_entry(
    raw_book: Dict[str, Any],
    token_id: str = "",
    side: str = "DOWN",
    source: str = "PM_CLOB_READ",
    book_age_ms: Optional[float] = None,
) -> dict:
    """
    Convenience wrapper for canary entry decisions.

    Returns dict with:
        best_ask, best_bid, spread, midpoint, is_valid, reject_reason,
        price_source, raw_first_ask, raw_first_bid
    """
    nb = normalize_orderbook(
        raw_book, token_id=token_id, side=side,
        source=source, book_age_ms=book_age_ms,
    )
    return {
        "best_ask": nb.best_ask,
        "best_bid": nb.best_bid,
        "spread": nb.spread,
        "midpoint": nb.midpoint,
        "is_valid": nb.is_valid,
        "reject_reason": nb.reject_reason,
        "price_source": nb.price_source,
        "raw_first_ask": nb.raw_first_ask,
        "raw_first_bid": nb.raw_first_bid,
        "ask_depth": nb.ask_depth_at_best,
        "bid_depth": nb.bid_depth_at_best,
        "total_ask_depth": nb.total_ask_depth,
        "total_bid_depth": nb.total_bid_depth,
    }


def validate_order_price(
    limit_price: float,
    normalized_book: NormalizedBook,
    entry_bucket_lo: float = 0.03,
    entry_bucket_hi: float = 0.08,
    max_slippage: float = 0.01,
) -> dict:
    """
    Validate that an order limit price comes from normalized book state.

    Must be called before any live order submission.
    """
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "limit_price": limit_price,
        "normalized_best_ask": normalized_book.best_ask,
        "price_source": normalized_book.price_source,
        "valid": False,
        "reject_reason": "",
    }

    if normalized_book.price_source != "NORMALIZED_BOOK":
        result["reject_reason"] = "ORDER_PRICE_NOT_NORMALIZED"
        return result

    if not normalized_book.is_valid:
        result["reject_reason"] = f"BOOK_INVALID_{normalized_book.reject_reason}"
        return result

    if normalized_book.best_ask is None:
        result["reject_reason"] = "NO_BEST_ASK"
        return result

    # Limit price must be the normalized best ask (for buy/taker)
    if abs(limit_price - normalized_book.best_ask) > max_slippage:
        result["reject_reason"] = (
            f"LIMIT_PRICE_DRIFT: limit={limit_price} vs "
            f"normalized_best_ask={normalized_book.best_ask} "
            f"delta={abs(limit_price - normalized_book.best_ask):.4f} > {max_slippage}"
        )
        return result

    # Must be in entry bucket
    if not (entry_bucket_lo <= normalized_book.best_ask <= entry_bucket_hi):
        result["reject_reason"] = (
            f"ASK_OUTSIDE_BUCKET: {normalized_book.best_ask} "
            f"not in [{entry_bucket_lo}, {entry_bucket_hi}]"
        )
        return result

    result["valid"] = True
    return result