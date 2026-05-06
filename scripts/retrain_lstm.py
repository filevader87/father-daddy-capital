#!/usr/bin/env python3
import json
import os
import torch
from src.models.lstm_trainer import LSTMTrainer

# Load LSTM config
with open('config/trading_config.json') as f:
    cfg = json.load(f)
lstm_cfg = cfg.get('lstm', {})

# Train and validate
trainer = LSTMTrainer(lstm_cfg)
trainer.train()
metrics = trainer.validate()   # returns dict with 'sharpe' and 'profit'

# Load previous best metrics
metrics_path = 'models/lstm_best_metrics.json'
if os.path.exists(metrics_path):
    with open(metrics_path) as mf:
        best = json.load(mf)
else:
    best = {'sharpe': -1e9, 'profit': -1e9}

# Compare and swap
improved = False
if metrics['sharpe'] > best['sharpe'] or metrics['profit'] > best['profit']:
    os.makedirs('models', exist_ok=True)
    torch.save(trainer.model.state_dict(), 'models/lstm_model.pt')
    with open(metrics_path, 'w') as mf:
        json.dump(metrics, mf, indent=2)
    print(f"[RETRAIN] Model updated: Sharpe={metrics['sharpe']:.2f}, Profit={metrics['profit']:.2f}")
    improved = True
else:
    print(f"[RETRAIN] No improvement: Sharpe={metrics['sharpe']:.2f}, Profit={metrics['profit']:.2f}")

# Exit code signals success (0) or no-change (1)
exit(0 if improved else 1) 