# src/forecasting/ensemble_predictor.py

import numpy as np
from src.forecasting.xgboost_model import XGBoostForecaster
from src.forecasting.trend_regression import predict_with_trend

def get_ensemble_prediction(price_series, market_type="crypto"):
    try:
        trend_pred = predict_with_trend(price_series)
        
        xgb = XGBoostForecaster()
        X, y = xgb.prepare_data(price_series)
        if len(X) == 0:
            return trend_pred  # fallback

        xgb.train(X, y)
        xgb_pred = xgb.predict([X[-1]])[0]

        # Simple average
        return np.mean([xgb_pred, trend_pred])

    except Exception as e:
        print(f"[Ensemble Predictor Error]: {e}")
        return price_series[-1]
