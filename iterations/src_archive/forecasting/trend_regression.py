# src/forecasting/trend_regression.py

import numpy as np
from sklearn.linear_model import LinearRegression

def predict_with_trend(price_series):
    if len(price_series) < 2:
        return price_series[-1]

    X = np.arange(len(price_series)).reshape(-1, 1)
    y = np.array(price_series).reshape(-1, 1)

    model = LinearRegression()
    model.fit(X, y)

    next_step = np.array([[len(price_series)]])
    prediction = model.predict(next_step)

    return prediction[0][0]
