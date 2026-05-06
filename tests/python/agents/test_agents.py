import pytest
from unittest.mock import patch, MagicMock
from src.agents.trading.base_agent import BaseAgent
from src.control_plane.execution_agent import ExecutionAgent
from src.logger import logger
from src.agents.short_term.crypto_aets import CryptoAETS

def test_crypto_agent_decision():
    agent = CryptoAETS()
    action = agent.act(market_data={"price": 100})
    assert action in ["BUY", "SELL", "HOLD"]