
import xgboost as xgb
import numpy as np

class XGBoostForecaster:
    def __init__(self):
        self.model = xgb.XGBRegressor(objective='reg:squarederror', n_estimators=100)

    def train(self, X, y):
        self.model.fit(X, y)

    def predict(self, X):
        return self.model.predict(X)

    def prepare_data(self, prices, window=10):
        X, y = [], []
        for i in range(len(prices) - window):
            X.append(prices[i:i + window])
            y.append(prices[i + window])
        return np.array(X), np.array(y)
