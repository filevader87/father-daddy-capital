
# src/forecasting/horizon_predictor.py

import numpy as np
from statsmodels.tsa.holtwinters import ExponentialSmoothing

def get_horizon_prediction(price_series, forecast_steps=1):
    if len(price_series) < 3:
        return price_series[-1]

    try:
        model = ExponentialSmoothing(
            np.array(price_series),
            trend="add",
            seasonal=None,
            initialization_method="estimated"
        ).fit()

        prediction = model.forecast(forecast_steps)
        return prediction[-1]
    except Exception as e:
        print(f"[Horizon Predictor Error]: {e}")
        return price_series[-1]
