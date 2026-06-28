import numpy as np
try:
    from tensorflow.keras.models import Sequential
    from tensorflow.keras.layers import LSTM, Dense
except ImportError:
    # Dummy TensorFlow stub for testing
    class _DummyTF:
        class Tensor:
            def __init__(self, value): self.value = value
            def numpy(self): return self.value
        class function:
            def __init__(self, fn): self.fn = fn
            def __call__(self, *args, **kwargs): return self.fn(*args, **kwargs)
        class distribute:
            class MirroredStrategy:
                def __init__(self): pass
                def scope(self): return self
                def __enter__(self): pass
                def __exit__(self, exc_type, exc, tb): pass
        @staticmethod
        def map_fn(fn, elems, fn_output_signature=None):
            return list(map(fn, elems))
        @staticmethod
        def constant(value):
            return _DummyTF.Tensor(value)
    tf = _DummyTF()
    Sequential = type('Sequential', (), {'__init__': lambda self: None})
    LSTM = type('LSTM', (), {'__init__': lambda self, *args, **kwargs: None})
    Dense = type('Dense', (), {'__init__': lambda self, *args, **kwargs: None})
from sklearn.preprocessing import MinMaxScaler
import yfinance as yf
import datetime

class LSTMPredictor:
    def __init__(self, symbol, lookback=50):
        self.symbol = symbol
        self.lookback = lookback
        self.scaler = MinMaxScaler(feature_range=(0, 1))
        self.model = self.build_model()

    def build_model(self):
        model = Sequential()
        model.add(LSTM(units=50, return_sequences=True, input_shape=(self.lookback, 1)))
        model.add(LSTM(units=50))
        model.add(Dense(1))
        model.compile(optimizer='adam', loss='mean_squared_error')
        return model

    def fetch_data(self, period="90d", interval="1h"):
        data = yf.download(self.symbol, period=period, interval=interval, progress=False)
        if "Close" not in data or len(data["Close"]) < self.lookback:
            return None
        return data["Close"].values.reshape(-1, 1)

    def prepare_data(self, data):
        scaled_data = self.scaler.fit_transform(data)
        x, y = [], []
        for i in range(self.lookback, len(scaled_data)):
            x.append(scaled_data[i - self.lookback:i, 0])
            y.append(scaled_data[i, 0])
        return np.array(x), np.array(y)

    def train(self):
        raw_data = self.fetch_data()
        if raw_data is None:
            return False
        x, y = self.prepare_data(raw_data)
        x = np.reshape(x, (x.shape[0], x.shape[1], 1))
        self.model.fit(x, y, epochs=3, batch_size=8, verbose=0)
        return True

    def predict_next(self):
        raw_data = self.fetch_data()
        if raw_data is None or len(raw_data) < self.lookback:
            return None
        last_sequence = raw_data[-self.lookback:]
        last_sequence_scaled = self.scaler.transform(last_sequence)
        x_input = np.reshape(last_sequence_scaled, (1, self.lookback, 1))
        predicted_scaled = self.model.predict(x_input, verbose=0)
        return self.scaler.inverse_transform(predicted_scaled)[0][0]
