import torch
import numpy as np
from random import random
from src.models.lstm_model import LSTMModel
from src.models.ltc_cell import LTCCell
from src.utils.synthetic_dna import SyntheticDNA
from src.utils.data_loader import load_training_data
from src.config import TradingConfig
import logging
import mlflow

# Set up MLflow
mlflow.set_experiment("FatherDaddyCapital")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def train_models():
    """Retrain models with synthetic crash sequences."""
    # Load training data
    training_data = load_training_data()
    dna = SyntheticDNA()
    
    # Initialize models
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    lstm_model = LSTMModel().to(device)
    ltc_model = LTCCell(units=16).to(device)
    
    # Training parameters
    batch_size = 32
    num_epochs = 10
    learning_rate = 0.001
    
    # Initialize optimizers
    lstm_optimizer = torch.optim.Adam(lstm_model.parameters(), lr=learning_rate)
    ltc_optimizer = torch.optim.Adam(ltc_model.parameters(), lr=learning_rate)
    
    # Training loop
    for epoch in range(num_epochs):
        logger.info(f"Starting epoch {epoch + 1}/{num_epochs}")
        
        # Shuffle data
        np.random.shuffle(training_data)
        
        for i in range(0, len(training_data), batch_size):
            batch = []
            targets = []
            
            # Create batch with synthetic sequences
            for j in range(i, min(i + batch_size, len(training_data))):
                if random() < 0.1:  # 10% chance of synthetic crash
                    crash_seq = dna.generate_crash()
                    batch.append(crash_seq)
                    # For synthetic sequences, target is to predict the crash
                    targets.append(-1.0)  # Negative return target
                else:
                    real_window = training_data[j]
                    batch.append(real_window['features'])
                    targets.append(real_window['target'])
            
            # Convert to tensors
            batch = torch.tensor(batch, dtype=torch.float32).to(device)
            targets = torch.tensor(targets, dtype=torch.float32).to(device)
            
            # Train LSTM
            lstm_optimizer.zero_grad()
            lstm_pred, _ = lstm_model(batch)
            lstm_loss = torch.nn.functional.mse_loss(lstm_pred.squeeze(), targets)
            lstm_loss.backward()
            lstm_optimizer.step()
            
            # Train LTC
            ltc_optimizer.zero_grad()
            h = torch.zeros(16, device=device)
            ltc_pred = []
            for t in range(batch.size(1)):
                x_t = batch[:, t, :]
                h = ltc_model(x_t, h)
                ltc_pred.append(h)
            ltc_pred = torch.stack(ltc_pred)
            ltc_loss = torch.nn.functional.mse_loss(ltc_pred[-1].mean(dim=0), targets)
            ltc_loss.backward()
            ltc_optimizer.step()
            
            if (i // batch_size) % 10 == 0:
                logger.info(f"Batch {i//batch_size}: LSTM Loss: {lstm_loss.item():.4f}, LTC Loss: {ltc_loss.item():.4f}")
                # Log metrics to MLflow
                mlflow.log_metrics({
                    "lstm_loss": lstm_loss.item(),
                    "ltc_loss": ltc_loss.item()
                }, step=i//batch_size)
    
    # Save models
    torch.save(lstm_model.state_dict(), 'models/lstm_model.pth')
    torch.save(ltc_model.state_dict(), 'models/ltc_model.pth')
    logger.info("Models saved successfully")
    
    # Log models and parameters to MLflow
    mlflow.pytorch.log_model(lstm_model, "models/lstm_agent")
    mlflow.log_params({
        "use_ltc": config.USE_LTC,
        "use_swarm": config.USE_SWARM,
        "batch_size": batch_size,
        "num_epochs": num_epochs,
        "learning_rate": learning_rate,
        "device": str(device)
    })

if __name__ == "__main__":
    train_models() 