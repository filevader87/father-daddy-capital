import os
import base64
import json
import pytest
import requests
from datetime import datetime, timedelta
from solana.rpc.api import Client
from solana.transaction import Transaction
from solana.keypair import Keypair

from src.utils.solana_dex_interface import (
    SolanaDEXInterface,
    OrderSimulationError,
    DEXExecutionError
)

# Sample keypair for tests (32-byte zero array)
DUMMY_SECRET = base64.b64encode(bytes([0]*32)).decode()

@pytest.fixture(autouse=True)
def env_vars(tmp_path, monkeypatch):
    """Prepare environment variables and dummy keypair file."""
    # Write dummy keypair file
    keyfile = tmp_path / "keypair.json"
    keyfile.write_text(DUMMY_SECRET)
    monkeypatch.setenv("SOLANA_RPC_URL", "https://api.testnet.solana.com")
    monkeypatch.setenv("JUPITER_API_URL", "https://quote-api.testnet.jup.ag/v4")
    monkeypatch.setenv("SOLANA_WALLET_KEYPAIR", str(keyfile))
    monkeypatch.setenv("SOL_MINT_ADDRESS", "So11111111111111111111111111111111111111112")
    monkeypatch.setenv("USDC_MINT_ADDRESS", "Es9vMFrumi1Vkz…")
    monkeypatch.setenv("USDT_MINT_ADDRESS", "H6QvhMMD…")
    monkeypatch.setenv("JUPITER_SLIPPAGE_BPS", "50")
    return

@pytest.fixture
def dex_interface(monkeypatch):
    """Instantiate the interface with mocked network calls."""
    # Mock solana RPC client
    class DummyClient:
        def __init__(self, rpc_url):
            pass
        def send_raw_transaction(self, raw, opts=None):
            return {"result": "FAKE_TX_SIG"}
        def confirm_transaction(self, sig):
            return {"result": True}
    monkeypatch.setattr(Client, "__init__", lambda self, url: None)
    
    # Create the interface
    interface = SolanaDEXInterface(
        rpc_url=os.getenv("SOLANA_RPC_URL"),
        jupiter_api=os.getenv("JUPITER_API_URL"),
        wallet_keypair_path=os.getenv("SOLANA_WALLET_KEYPAIR"),
        slippage_bps=int(os.getenv("JUPITER_SLIPPAGE_BPS"))
    )
    
    # Replace its client with our dummy
    interface.client = DummyClient(None)

    # Monkey‐patch HTTP calls in requests
    class DummyResponse:
        def __init__(self, data):
            self._data = data
        def json(self):
            return self._data

    def fake_get(url, params=None):
        # Simulate /quote endpoint
        assert url.endswith("/quote")
        return DummyResponse({
            "data": [{
                "outAmount": 200_000,
                "priceImpact": {"price": 1.98}
            }]
        })

    def fake_post(url, json=None):
        # Simulate /swap endpoint
        assert url.endswith("/swap")
        return DummyResponse({
            "swapTransaction": base64.b64encode(Transaction().serialize()).decode(),
            "priceImpact": {"price": 1.98}
        })

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)

    return interface

def test_get_dex_price(dex_interface):
    price = dex_interface.get_dex_price("SOL/USDC", amount=1.0)
    # Fake outAmount = 200_000, amount_ui = 1*10^6 => price = 200_000/1_000_000 = 0.2
    assert pytest.approx(price, rel=1e-3) == 0.2

def test_get_dex_price_caching(dex_interface):
    # First call should hit the API
    price1 = dex_interface.get_dex_price("SOL/USDC", amount=1.0)
    
    # Second call should use cache
    price2 = dex_interface.get_dex_price("SOL/USDC", amount=1.0)
    
    assert price1 == price2

def test_simulate_order(dex_interface):
    simulation = dex_interface.simulate_order("SOL/USDC", qty=1.0, side="BUY")
    
    assert "simulated_price" in simulation
    assert "estimated_gas" in simulation
    assert "price_impact" in simulation
    assert "slippage" in simulation
    assert "timestamp" in simulation
    
    assert simulation["simulated_price"] == pytest.approx(0.2)
    assert simulation["estimated_gas"] == 5000
    assert simulation["slippage"] == 0.005

def test_simulate_order_high_price_impact(dex_interface, monkeypatch):
    # Mock get_dex_price to return different prices for different amounts
    def mock_get_dex_price(self, symbol, amount):
        return 0.2 if amount == 1.0 else 0.1
    
    monkeypatch.setattr(SolanaDEXInterface, "get_dex_price", mock_get_dex_price)
    
    simulation = dex_interface.simulate_order("SOL/USDC", qty=1.0, side="BUY")
    assert simulation["price_impact"] > 0.01

def test_place_dex_order_success(dex_interface, monkeypatch):
    # Ensure that Transaction.deserialize and .serialize work
    monkeypatch.setattr(Transaction, "deserialize", lambda data: Transaction())
    
    result = dex_interface.place_dex_order("SOL/USDC", qty=1.0, side="BUY")
    
    assert "tx_sig" in result
    assert result["price"] == pytest.approx(1.98)
    assert "simulation" in result

def test_place_dex_order_retry(dex_interface, monkeypatch):
    # Mock failed transactions
    fail_count = 0
    def mock_send_raw_transaction(raw, opts=None):
        nonlocal fail_count
        fail_count += 1
        if fail_count < 3:
            raise Exception("Transaction failed")
        return {"result": "FAKE_TX_SIG"}

    monkeypatch.setattr(Transaction, "deserialize", lambda data: Transaction())
    monkeypatch.setattr(dex_interface.client, "send_raw_transaction", mock_send_raw_transaction)
    
    result = dex_interface.place_dex_order("SOL/USDC", qty=1.0, side="BUY", max_retries=3)
    assert result["tx_sig"] == "FAKE_TX_SIG"
    assert fail_count == 3

def test_place_dex_order_max_retries_exceeded(dex_interface, monkeypatch):
    # Mock always failing transaction
    def mock_send_raw_transaction(self, raw, opts=None):
        raise Exception("Transaction failed")
    
    monkeypatch.setattr(Transaction, "deserialize", lambda data: Transaction())
    monkeypatch.setattr(dex_interface.client, "send_raw_transaction", mock_send_raw_transaction)
    
    with pytest.raises(DEXExecutionError):
        dex_interface.place_dex_order("SOL/USDC", qty=1.0, side="BUY", max_retries=2)

def test_parse_symbol_buy_and_sell(dex_interface):
    # Test symbol parsing in buy mode
    in_mint, out_mint = dex_interface._parse_symbol("SOL/USDC", sell=False)
    assert in_mint == os.getenv("SOL_MINT_ADDRESS")
    assert out_mint == os.getenv("USDC_MINT_ADDRESS")
    
    # Test symbol parsing in sell mode
    in_mint2, out_mint2 = dex_interface._parse_symbol("SOL/USDC", sell=True)
    assert in_mint2 == os.getenv("USDC_MINT_ADDRESS")
    assert out_mint2 == os.getenv("SOL_MINT_ADDRESS")
