import importlib

import pandas as pd


def test_config_loads_unified_trading_yaml():
    from src.config import TradingConfig

    config = TradingConfig.load_from_file("config/trading.yaml")

    assert config.mode == "paper"
    assert config.symbols == ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD"]
    assert config.MAX_POSITION_SIZE == 0.1
    assert config.MAX_LEVERAGE == 2.0
    assert config.MAX_DRAWDOWN == 0.15


def test_risk_metric_functions_are_exported():
    from src.utils.risk_metrics import calculate_sharpe_ratio, calculate_var

    returns = [-0.02, -0.01, 0.0, 0.01, 0.02]

    assert calculate_var(returns, confidence_level=0.8) == -0.012
    assert isinstance(calculate_sharpe_ratio(returns), float)


def test_risk_manager_agent_contract():
    from src.risk.risk_manager import RiskManager

    manager = RiskManager({"max_position_size": 0.1, "max_drawdown": 0.15})
    signal = {"symbol": "BTCUSD", "signal": "BUY", "confidence": 0.8, "price": 50000.0}

    assert manager.validate_signal(signal, {}, 100000.0) is True
    assert manager.calculate_position_size(signal, 100000.0, {}) == 0.16
    assert manager.can_place_order("BTCUSD", 0.1, 50000.0, "buy") is True


def test_main_module_imports_without_side_effects():
    module = importlib.import_module("src.main")

    assert hasattr(module, "TradingSystem")


def test_yfinance_market_data_symbol_mapping():
    from src.market_data.yfinance_provider import YFinanceMarketDataProvider

    provider = YFinanceMarketDataProvider(symbols=["BTCUSD", "ETHUSD", "AAPL"])

    assert provider.to_provider_symbol("BTCUSD") == "BTC-USD"
    assert provider.to_provider_symbol("ETHUSD") == "ETH-USD"
    assert provider.to_provider_symbol("AAPL") == "AAPL"


def test_yfinance_market_data_normalizes_ohlcv(monkeypatch):
    from src.market_data.yfinance_provider import YFinanceMarketDataProvider

    raw = pd.DataFrame(
        {
            "Open": [1.0, 2.0],
            "High": [2.0, 3.0],
            "Low": [0.5, 1.5],
            "Close": [1.5, 2.5],
            "Volume": [100, 200],
        }
    )

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, period, interval):
            assert self.symbol == "BTC-USD"
            assert period == "5d"
            assert interval == "1h"
            return raw

    monkeypatch.setattr("src.market_data.yfinance_provider.yf.Ticker", FakeTicker)

    provider = YFinanceMarketDataProvider(symbols=["BTCUSD"])
    data = provider.fetch_symbol("BTCUSD", period="5d", interval="1h")

    assert list(data.columns) == ["open", "high", "low", "close", "volume"]
    assert data["close"].tolist() == [1.5, 2.5]
