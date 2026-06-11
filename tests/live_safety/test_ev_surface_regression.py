"""
V21.7.24 — EV Surface Price Regression Test
=============================================
EV surface must use normalized best_ask, not raw asks[0].

Classification: P0 execution-safety regression
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'v217_live'))
from book_normalizer import normalize_orderbook


class TestEVSurfaceRegression:
    """EV surface must not use raw ask order."""

    def test_ev_uses_normalized_ask(self):
        """Market probability must come from normalized best_ask."""
        book = {
            "asks": [{"price": "0.99", "size": "3000"}, {"price": "0.54", "size": "100"}],
            "bids": [{"price": "0.46", "size": "200"}, {"price": "0.53", "size": "100"}],
        }
        result = normalize_orderbook(book, token_id="UP", side="UP")
        # best_ask=0.54, not 0.99
        assert result.best_ask == 0.54
        assert result.best_bid == 0.53
        # midpoint = (0.54 + 0.53) / 2 = 0.535
        assert result.midpoint == pytest.approx(0.535, abs=1e-6)

    def test_bucket_classification_uses_normalized(self):
        """Bucket classification uses normalized price, not raw first."""
        # A market with best ask at 0.05 (inside 3-8¢ bucket)
        book = {
            "asks": [{"price": "0.99", "size": "3000"}, {"price": "0.05", "size": "100"}],
            "bids": [{"price": "0.03", "size": "200"}],
        }
        result = normalize_orderbook(book, token_id="DOWN", side="DOWN")
        assert result.best_ask == 0.05
        assert 0.03 <= result.best_ask <= 0.08, "Must be inside 3-8¢ bucket"

    def test_ev_not_based_on_worst_ask(self):
        """EV calculation using 0.99 as ask would give wrong probability."""
        book = {
            "asks": [{"price": "0.99", "size": "3000"}, {"price": "0.54", "size": "100"}],
            "bids": [{"price": "0.53", "size": "200"}, {"price": "0.46", "size": "300"}],
        }
        result = normalize_orderbook(book, token_id="UP", side="UP")
        # WRONG EV (using raw 0.99): implied prob = ~0.99 → nearly certain
        # CORRECT EV (using normalized 0.54): implied prob = ~0.54 → coin flip
        assert result.best_ask == 0.54, "EV must use 0.54, not 0.99"


class TestSweeperPriceIntegrity:
    """Sweeper logic must not detect false 99¢ candidate."""

    def test_sweeper_not_triggered_by_raw_ask(self):
        """
        Sweeper watches 99.2-99.8¢ range.
        Raw asks[0]=0.99 would falsely trigger sweeper.
        Normalized best_ask=0.46 must NOT trigger it.
        """
        book = {
            "asks": [{"price": "0.99", "size": "654"}, {"price": "0.46", "size": "187.51"}],
            "bids": [{"price": "0.44", "size": "141.96"}],
        }
        result = normalize_orderbook(book, token_id="DOWN", side="DOWN")
        
        # Sweeper range: 0.992 <= midpoint <= 0.998
        # With normalized prices: midpoint = (0.46 + 0.44) / 2 = 0.45
        sweeper_range_lo = 0.992
        sweeper_range_hi = 0.998
        
        assert not (sweeper_range_lo <= result.midpoint <= sweeper_range_hi), \
            f"Sweeper would falsely trigger on midpoint={result.midpoint}"
        assert result.best_ask != 0.99, "best_ask must be 0.46, not raw 0.99"
        assert result.best_ask == 0.46

    def test_sweeper_correctly_detects_near_resolution(self):
        """When true near-resolution: sweeper must detect correctly."""
        book = {
            "asks": [{"price": "0.995", "size": "100"}],
            "bids": [{"price": "0.990", "size": "200"}],
        }
        result = normalize_orderbook(book, token_id="UP", side="UP")
        # midpoint = (0.995 + 0.990) / 2 = 0.9925 → IN sweeper range
        assert 0.992 <= result.midpoint <= 0.998, \
            f"Near-resolution midpoint {result.midpoint} should be in sweeper range"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])