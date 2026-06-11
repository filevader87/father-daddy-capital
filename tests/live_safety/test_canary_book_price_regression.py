"""
V21.7.24 — Canary Book Price Regression Test
=============================================
Regression test for the CLOB asks[0] bug that caused best_ask=0.99 instead of 0.46.

HARD FAIL if:
- best_ask = 0.99 (raw first ask)
- spread = 0.55 or 0.98 (nonsensical)

Classification: P0 execution-safety regression
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'v217_live'))
from book_normalizer import normalize_orderbook, normalize_for_entry


class TestCanaryBookPriceRegression:
    """
    Regression test for the CLOB asks[0] bug.
    
    The CLOB API returns asks in DESCENDING order: [0.99, 0.98, 0.70, 0.46]
    Code was taking asks[0] as best_ask, getting 0.99 instead of 0.46.
    This caused 3+ hours of false NO_TRADE_CORRECT readings.
    """

    @pytest.fixture
    def clob_descending_book(self):
        """Exact fixture from the bug report."""
        return {
            "asks": [
                {"price": "0.99", "size": "3619"},
                {"price": "0.98", "size": "654"},
                {"price": "0.70", "size": "200"},
                {"price": "0.47", "size": "377.01"},
                {"price": "0.46", "size": "187.51"},
            ],
            "bids": [
                {"price": "0.10", "size": "500"},
                {"price": "0.40", "size": "1500.61"},
                {"price": "0.41", "size": "629.99"},
                {"price": "0.42", "size": "290"},
                {"price": "0.43", "size": "159.49"},
                {"price": "0.44", "size": "141.96"},
            ],
        }

    def test_best_ask_is_min_not_first(self, clob_descending_book):
        """best_ask must be 0.46, NOT 0.99 (the raw first element)."""
        result = normalize_orderbook(clob_descending_book, token_id="DOWN", side="DOWN")
        assert result.best_ask == 0.46, f"HARD FAIL: best_ask={result.best_ask}, expected 0.46"
        assert result.best_ask != 0.99, "HARD FAIL: best_ask=0.99 is the WRONG (worst) ask"

    def test_best_bid_is_max_not_first(self, clob_descending_book):
        """best_bid must be 0.44, NOT 0.10 (the raw first element)."""
        result = normalize_orderbook(clob_descending_book, token_id="DOWN", side="DOWN")
        assert result.best_bid == 0.44, f"HARD FAIL: best_bid={result.best_bid}, expected 0.44"

    def test_spread_is_tight(self, clob_descending_book):
        """Spread must be ~0.02, NOT 0.98 or 0.55 (nonsensical)."""
        result = normalize_orderbook(clob_descending_book, token_id="DOWN", side="DOWN")
        assert result.spread is not None
        assert result.spread != pytest.approx(0.98, abs=0.01), "HARD FAIL: spread=0.98 is the BUG value"
        assert result.spread != pytest.approx(0.55, abs=0.01), "HARD FAIL: spread=0.55 is wrong"
        assert result.spread == pytest.approx(0.02, abs=0.01), f"Expected spread ~0.02, got {result.spread}"

    def test_bucket_classification_outside(self, clob_descending_book):
        """Ask=0.46 is OUTSIDE the 3-8¢ bucket. Must classify correctly."""
        result = normalize_orderbook(clob_descending_book, token_id="DOWN", side="DOWN")
        # 0.46 is NOT in [0.03, 0.08]
        assert not (0.03 <= result.best_ask <= 0.08), \
            f"Ask {result.best_ask} should be outside 3-8¢ bucket"

    def test_raw_first_ask_differs_from_best(self, clob_descending_book):
        """Sanity check: raw_first_ask must differ from best_ask."""
        result = normalize_orderbook(clob_descending_book, token_id="DOWN", side="DOWN")
        assert result.raw_first_ask == 0.99
        assert result.best_ask == 0.46
        assert abs(result.raw_first_ask - result.best_ask) > 0.5, \
            "Delta should be large (>0.5) confirming the normalizer is critical"

    def test_entry_normalizer_consistent(self, clob_descending_book):
        """normalize_for_entry must produce same results as normalize_orderbook."""
        result = normalize_for_entry(clob_descending_book, token_id="DOWN", side="DOWN")
        assert result["best_ask"] == 0.46
        assert result["best_bid"] == 0.44
        assert result["spread"] == pytest.approx(0.02, abs=0.01)
        assert result["is_valid"] is True
        assert result["price_source"] == "NORMALIZED_BOOK"

    def test_inside_bucket_regression(self):
        """If ask IS inside bucket (e.g., 0.05), normalizer must report it correctly."""
        book = {
            "asks": [{"price": "0.08", "size": "100"}, {"price": "0.05", "size": "200"}],
            "bids": [{"price": "0.04", "size": "300"}, {"price": "0.03", "size": "400"}],
        }
        result = normalize_orderbook(book, token_id="DOWN", side="DOWN")
        assert result.best_ask == 0.05
        assert 0.03 <= result.best_ask <= 0.08, "Ask should be inside 3-8¢ bucket"
        assert result.best_bid == 0.04


if __name__ == "__main__":
    pytest.main([__file__, "-v"])