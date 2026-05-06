import pytest
from unittest.mock import patch, MagicMock
from typing import Dict, Any, Optional
from src.trading_interface import (
    place_order,
    get_position,
    get_latest_price,
    get_crypto_price,
    get_market_data
)
from src.utils.api_manager import api_manager
from src.utils.solana_dex_interface import solana_dex_interface
from src.logger import logger

@pytest.fixture
def mock_api_manager() -> MagicMock:
    """Mock API manager for testing"""
    with patch('src.utils.api_manager.api_manager') as mock:
        mock.get_api_key.return_value = "test_key"
        mock.make_request.return_value = MagicMock(
            json=lambda: {"ask_price": 100.0},
            status_code=200
        )
        yield mock

@pytest.fixture
def mock_logger() -> MagicMock:
    """Mock logger for testing"""
    with patch('src.logger.logger') as mock:
        yield mock

def test_place_order_success(mock_api_manager: MagicMock, mock_logger: MagicMock) -> None:
    """Test successful order placement"""
    result: Dict[str, Any] = place_order("AAPL", 1.0, "buy")
    assert result["status"] != "simulated"
    mock_logger.log_trade.assert_called_once()

def test_place_order_fallback(mock_api_manager: MagicMock, mock_logger: MagicMock) -> None:
    """Test order placement fallback to simulation"""
    mock_api_manager.make_request.side_effect = Exception("API Error")
    result: Dict[str, Any] = place_order("AAPL", 1.0, "buy")
    assert result["status"] == "simulated"
    mock_logger.log_trade.assert_called_once()

def test_get_position_success(mock_api_manager: MagicMock) -> None:
    """Test successful position retrieval"""
    mock_api_manager.make_request.return_value.json.return_value = {
        "symbol": "AAPL",
        "qty": 10
    }
    position: Optional[Dict[str, Any]] = get_position("AAPL")
    assert position is not None
    assert position["symbol"] == "AAPL"
    assert position["qty"] == 10

def test_get_position_failure(mock_api_manager: MagicMock) -> None:
    """Test position retrieval failure"""
    mock_api_manager.make_request.side_effect = Exception("API Error")
    position: Optional[Dict[str, Any]] = get_position("AAPL")
    assert position is None

def test_get_latest_price_stock(mock_api_manager: MagicMock) -> None:
    """Test stock price retrieval"""
    price: Optional[float] = get_latest_price("AAPL")
    assert price == 100.0

def test_get_latest_price_crypto(mock_api_manager: MagicMock) -> None:
    """Test crypto price retrieval"""
    mock_api_manager.make_request.return_value.json.return_value = {
        "bitcoin": {"usd": 50000.0}
    }
    price: Optional[float] = get_latest_price("BTCUSD")
    assert price == 50000.0

def test_get_crypto_price_fallback(mock_api_manager: MagicMock) -> None:
    """Test crypto price fallback to simulation"""
    mock_api_manager.make_request.side_effect = Exception("API Error")
    price: float
    source: str
    price, source = get_crypto_price("BTCUSD")
    assert source == "SIM"
    assert isinstance(price, float) 