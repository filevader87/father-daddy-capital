import json
import asyncio
from pathlib import Path
from typing import List, Dict, Any
import logging
from datetime import datetime
import argparse

from src.rl.q_table_manager import QTableManager
from src.agents.short_term.stock_aets import StockAETS
from src.core.memory_bank import MemoryBank

class ReplayTrainer:
    """Performs batch training using memory bank data."""
    
    def __init__(self,
                 memory_bank_path: str = "memory_bank.json",
                 model_name: str = "stock_aets",
                 batch_size: int = 32,
                 epochs: int = 10):
        self.memory_bank_path = Path(memory_bank_path)
        self.model_name = model_name
        self.batch_size = batch_size
        self.epochs = epochs
        self.logger = logging.getLogger(__name__)
        self.q_table_manager = QTableManager()
        self.memory_bank = MemoryBank()
        
    async def load_memory_bank(self) -> List[Dict[str, Any]]:
        """Load experiences from memory bank."""
        if not self.memory_bank_path.exists():
            raise FileNotFoundError(f"Memory bank not found at {self.memory_bank_path}")
            
        with open(self.memory_bank_path) as f:
            data = json.load(f)
            
        self.logger.info(f"Loaded {len(data)} experiences from memory bank")
        return data
        
    async def train_batch(self, experiences: List[Dict[str, Any]]):
        """Train on a batch of experiences."""
        agent = StockAETS()
        
        # Load latest Q-table if exists
        try:
            q_table, metadata = self.q_table_manager.load_q_table(self.model_name)
            agent.q_table = q_table
            self.logger.info("Loaded existing Q-table")
        except FileNotFoundError:
            self.logger.info("No existing Q-table found, starting fresh")
            
        # Train on experiences
        for epoch in range(self.epochs):
            self.logger.info(f"Starting epoch {epoch + 1}/{self.epochs}")
            
            for i in range(0, len(experiences), self.batch_size):
                batch = experiences[i:i + self.batch_size]
                
                for experience in batch:
                    state = experience['state']
                    action = experience['action']
                    reward = experience['reward']
                    next_state = experience['next_state']
                    done = experience['done']
                    
                    await agent.update_explicit(
                        state=state,
                        action=action,
                        reward=reward,
                        next_state=next_state,
                        done=done
                    )
                    
            # Save Q-table after each epoch
            metadata = {
                'epoch': epoch + 1,
                'batch_size': self.batch_size,
                'timestamp': datetime.now().isoformat(),
                'experiences_processed': len(experiences)
            }
            
            self.q_table_manager.save_q_table(
                q_table=agent.q_table,
                model_name=self.model_name,
                metadata=metadata
            )
            
            self.logger.info(f"Completed epoch {epoch + 1}")
            
    async def run(self):
        """Run the replay training process."""
        try:
            experiences = await self.load_memory_bank()
            await self.train_batch(experiences)
            self.logger.info("Replay training completed successfully")
        except Exception as e:
            self.logger.error(f"Error during replay training: {e}")
            raise
            
async def main():
    parser = argparse.ArgumentParser(description="Replay training script")
    parser.add_argument("--memory-bank", default="memory_bank.json",
                       help="Path to memory bank file")
    parser.add_argument("--model-name", default="stock_aets",
                       help="Name of the model to train")
    parser.add_argument("--batch-size", type=int, default=32,
                       help="Batch size for training")
    parser.add_argument("--epochs", type=int, default=10,
                       help="Number of training epochs")
    
    args = parser.parse_args()
    
    trainer = ReplayTrainer(
        memory_bank_path=args.memory_bank,
        model_name=args.model_name,
        batch_size=args.batch_size,
        epochs=args.epochs
    )
    
    await trainer.run()
    
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main()) 