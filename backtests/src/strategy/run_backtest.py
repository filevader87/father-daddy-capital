import pandas as pd
from src.signal_engine import SignalEngine

class BacktestRunner:
    def __init__(self, historical_data, api_mock):
        self.data = historical_data
        self.api = api_mock
        self.signal_engine = SignalEngine()
        self.balance = 10000
        self.holdings = 0
        self.trade_log = []

    def execute_trade(self, signal, reasoning):
        qty = signal['qty']
        price = reasoning.get('price', 0)
        if signal['side'] == 'buy' and self.balance >= price * qty:
            self.balance -= price * qty
            self.holdings += qty
        elif signal['side'] == 'sell' and self.holdings >= qty:
            self.balance += price * qty
            self.holdings -= qty
        self.trade_log.append({
            "action": signal['side'],
            "price": price,
            "qty": qty,
            "balance": self.balance,
            "holdings": self.holdings,
            "timestamp": reasoning.get("timestamp", "")
        })

    def run(self):
        for i in range(len(self.data)):
            signal, reasoning = self.signal_engine.get_crypto_signal(self.api)
            if signal:
                self.execute_trade(signal, reasoning)
        return pd.DataFrame(self.trade_log)
