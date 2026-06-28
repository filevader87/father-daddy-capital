import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier

class SignalEngine:
    def __init__(self):
        self.model = self._train_model()

    def _train_model(self):
        np.random.seed(42)
        n_samples = 300
        df = pd.DataFrame({
            "price": np.random.uniform(100, 50000, size=n_samples),
            "rsi": np.random.randint(10, 90, size=n_samples),
            "macd_hist": np.random.normal(0, 1, size=n_samples),
            "volatility": np.random.uniform(0.01, 0.15, size=n_samples),
            "holding": np.random.randint(0, 2, size=n_samples),
            "buying_power": np.random.uniform(100, 10000, size=n_samples),
            "action": np.random.choice(["buy", "sell", "hold"], size=n_samples)
        })

        def label_trade(row):
            if row["action"] == "buy" and row["rsi"] < 30 and row["macd_hist"] > 0:
                return 1
            elif row["action"] == "sell" and row["rsi"] > 70 and row["macd_hist"] < 0:
                return 1
            else:
                return 0

        df["result"] = df.apply(label_trade, axis=1)
        df["action_code"] = df["action"].map({"buy": 0, "sell": 1, "hold": 2})

        features = df[[
            "price", "rsi", "macd_hist", "volatility",
            "holding", "buying_power", "action_code"
        ]]
        labels = df["result"]

        model = RandomForestClassifier(n_estimators=100, random_state=42)
        model.fit(features, labels)
        return model

    def evaluate_trade(self, price, rsi, macd_hist, volatility, holding, buying_power, action):
        action_map = {"buy": 0, "sell": 1, "hold": 2}
        action_code = action_map.get(action, 2)
        features = np.array([[price, rsi, macd_hist, volatility, holding, buying_power, action_code]])
        prob = self.model.predict_proba(features)[0][1]
        decision = "approve" if prob > 0.75 else "reject"
        return decision, prob
