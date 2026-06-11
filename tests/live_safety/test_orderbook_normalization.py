"""
V21.7.24 — Orderbook Normalization Unit Tests
==============================================
Tests that normalize_orderbook() always produces correct best_ask/best_bid
regardless of the ordering of the raw CLOB response.

Classification: P0 execution-safety
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'v217_live'))
from book_normalizer import normalize_orderbook, normalize_for_entry, validate_order_price, NormalizedBook


def make_book(asks, bids, timestamp=None):
    """Helper to build a raw book dict from price lists."""
    book = {
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
    }
    if timestamp:
        book["timestamp"] = timestamp
    return book


class TestNormalizeOrderbook:
    """Core normalization tests."""

    def test_asks_descending(self):
        """CLOB returns asks descending (worst first). best_ask must be min."""
        book = make_book([(0.99, 100), (0.98, 200), (0.70, 300), (0.46, 400)],
                         [(0.44, 500), (0.42, 600)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_ask == 0.46, f"Expected 0.46, got {result.best_ask}"
        assert result.is_valid

    def test_asks_ascending(self):
        """If asks come ascending (already sorted), best_ask still min."""
        book = make_book([(0.46, 400), (0.70, 300), (0.98, 200), (0.99, 100)],
                         [(0.44, 500), (0.42, 600)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_ask == 0.46

    def test_asks_unsorted(self):
        """Random order: best_ask must still be min."""
        book = make_book([(0.70, 300), (0.46, 400), (0.99, 100), (0.98, 200)],
                         [(0.42, 600), (0.44, 500)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_ask == 0.46

    def test_bids_descending(self):
        """CLOB returns bids ascending (worst first). best_bid must be max."""
        book = make_book([(0.46, 400)], [(0.44, 500), (0.42, 600), (0.10, 700)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_bid == 0.44

    def test_bids_ascending(self):
        """If bids come ascending (best first), best_bid still max."""
        book = make_book([(0.46, 400)], [(0.10, 700), (0.42, 600), (0.44, 500)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_bid == 0.44

    def test_bids_unsorted(self):
        """Random order: best_bid must still be max."""
        book = make_book([(0.46, 400)], [(0.42, 600), (0.44, 500), (0.10, 700)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_bid == 0.44

    def test_empty_asks(self):
        """Empty asks: invalid book, no best_ask."""
        book = make_book([], [(0.44, 500)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_ask is None
        assert not result.is_valid
        assert result.reject_reason == "EMPTY_BID_OR_ASK_SIDE"

    def test_empty_bids(self):
        """Empty bids: invalid book, no best_bid."""
        book = make_book([(0.46, 400)], [])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_bid is None
        assert not result.is_valid

    def test_crossed_book(self):
        """Best bid > best ask: crossed book, invalid."""
        book = make_book([(0.46, 400)], [(0.50, 500)])
        result = normalize_orderbook(book, token_id="test_down")
        assert not result.is_valid
        assert "CROSSED" in result.reject_reason

    def test_negative_spread(self):
        """Spread < 0: invalid."""
        book = make_book([(0.40, 100)], [(0.50, 200)])
        result = normalize_orderbook(book, token_id="test_down")
        assert not result.is_valid

    def test_prices_out_of_range(self):
        """Prices outside 0-1 should be filtered."""
        book = make_book([(0.46, 100), (1.5, 200), (-0.1, 300)],
                         [(0.44, 100), (1.5, 200)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_ask == 0.46

    def test_string_prices(self):
        """String prices (CLOB format) should parse correctly."""
        book = {"asks": [{"price": "0.46", "size": "400"}, {"price": "0.99", "size": "100"}],
                "bids": [{"price": "0.44", "size": "500"}, {"price": "0.42", "size": "600"}]}
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_ask == 0.46
        assert result.best_bid == 0.44

    def test_decimal_prices(self):
        """Decimal precision: 0.0301 should not round to 0.03."""
        book = make_book([(0.0301, 100)], [(0.0299, 200)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_ask == pytest.approx(0.0301, abs=1e-6)
        assert result.best_bid == pytest.approx(0.0299, abs=1e-6)

    def test_duplicate_price_levels(self):
        """Multiple levels at same price: best_ask still correct."""
        book = make_book([(0.46, 100), (0.46, 200), (0.50, 300)],
                         [(0.44, 400), (0.44, 500), (0.40, 600)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.best_ask == 0.46
        assert result.best_bid == 0.44

    def test_spread_calculation(self):
        """Spread = best_ask - best_bid."""
        book = make_book([(0.46, 400)], [(0.44, 500)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.spread == pytest.approx(0.02, abs=1e-6)

    def test_midpoint_calculation(self):
        """Midpoint = (best_bid + best_ask) / 2."""
        book = make_book([(0.46, 400)], [(0.44, 500)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.midpoint == pytest.approx(0.45, abs=1e-6)

    def test_raw_first_ask_sanity_log(self):
        """raw_first_ask should capture the raw first element."""
        book = make_book([(0.99, 100), (0.46, 400)], [(0.44, 500)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.raw_first_ask == 0.99
        assert result.best_ask == 0.46
        assert abs(result.raw_first_ask - result.best_ask) > 0.01

    def test_price_source_is_normalized(self):
        """price_source must always be NORMALIZED_BOOK."""
        book = make_book([(0.46, 400)], [(0.44, 500)])
        result = normalize_orderbook(book, token_id="test_down")
        assert result.price_source == "NORMALIZED_BOOK"


class TestNormalizeForEntry:
    """Test the convenience wrapper."""

    def test_returns_dict(self):
        book = make_book([(0.46, 400)], [(0.44, 500)])
        result = normalize_for_entry(book, token_id="test", side="DOWN")
        assert isinstance(result, dict)
        assert result["best_ask"] == 0.46
        assert result["best_bid"] == 0.44
        assert result["price_source"] == "NORMALIZED_BOOK"


class TestValidateOrderPrice:
    """Order price safety check tests."""

    def test_valid_order(self):
        """Order at normalized best_ask within bucket: valid."""
        book = make_book([(0.05, 400)], [(0.04, 500)])
        nb = normalize_orderbook(book, token_id="test")
        result = validate_order_price(0.05, nb)
        assert result["valid"]

    def test_order_price_drift(self):
        """Order price differs from normalized best_ask: invalid."""
        book = make_book([(0.05, 400)], [(0.04, 500)])
        nb = normalize_orderbook(book, token_id="test")
        result = validate_order_price(0.99, nb)
        assert not result["valid"]
        assert "DRIFT" in result["reject_reason"]

    def test_ask_outside_bucket(self):
        """Ask outside 3-8¢ bucket: invalid."""
        book = make_book([(0.46, 400)], [(0.44, 500)])
        nb = normalize_orderbook(book, token_id="test")
        result = validate_order_price(0.46, nb)
        assert not result["valid"]
        assert "OUTSIDE_BUCKET" in result["reject_reason"]

    def test_invalid_book(self):
        """Invalid book: cannot validate order."""
        book = make_book([], [])  # Empty book
        nb = normalize_orderbook(book, token_id="test")
        result = validate_order_price(0.05, nb)
        assert not result["valid"]

    def test_non_normalized_source(self):
        """price_source != NORMALIZED_BOOK: cannot validate."""
        book = make_book([(0.05, 400)], [(0.04, 500)])
        nb = normalize_orderbook(book, token_id="test")
        nb.price_source = "RAW_CLOB"
        result = validate_order_price(0.05, nb)
        assert not result["valid"]
        assert "NOT_NORMALIZED" in result["reject_reason"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])