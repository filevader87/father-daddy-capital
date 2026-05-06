import json
import numpy as np
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
import logging

class QTableManager:
    """Manages persistence and loading of Q-tables."""
    
    def __init__(self, 
                 base_path: str = "models/q_tables",
                 autosave_interval: int = 1000):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.autosave_interval = autosave_interval
        self.last_save_count = 0
        self.logger = logging.getLogger(__name__)

    async def initialize(self):
        self.base_path.mkdir(parents=True, exist_ok=True)

    async def save(self, model_name: str, q_table: Dict[str, np.ndarray], metadata: Optional[Dict[str, Any]] = None):
        self.save_q_table(q_table, model_name, metadata)

    async def load(self, model_name: str):
        return self.load_q_table(model_name)

    async def list_models(self) -> List[str]:
        return list(self.get_available_models().keys())

    async def clear(self):
        for file_path in self.base_path.glob("*.json"):
            file_path.unlink()
        
    def save_q_table(self, 
                    q_table: Dict[str, np.ndarray],
                    model_name: str,
                    metadata: Optional[Dict[str, Any]] = None):
        """Save Q-table to disk with metadata."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = self.base_path / f"{model_name}_{timestamp}.json"
        
        # Convert numpy arrays to lists for JSON serialization
        serializable_q_table = {
            state: value.tolist() 
            for state, value in q_table.items()
        }
        
        data = {
            'q_table': serializable_q_table,
            'metadata': metadata or {},
            'timestamp': timestamp,
            'model_name': model_name
        }
        
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=2)
            
        self.logger.info(f"Saved Q-table to {file_path}")
        return file_path
        
    def load_q_table(self, 
                    model_name: str,
                    version: Optional[str] = None) -> Dict[str, np.ndarray]:
        """Load Q-table from disk."""
        if version:
            file_path = self.base_path / f"{model_name}_{version}.json"
        else:
            # Get latest version
            files = list(self.base_path.glob(f"{model_name}_*.json"))
            if not files:
                raise FileNotFoundError(f"No Q-table found for model {model_name}")
            file_path = max(files, key=lambda x: x.stat().st_mtime)
            
        with open(file_path) as f:
            data = json.load(f)
            
        # Convert lists back to numpy arrays
        q_table = {
            state: np.array(value)
            for state, value in data['q_table'].items()
        }
        
        self.logger.info(f"Loaded Q-table from {file_path}")
        return q_table, data['metadata']
        
    def should_autosave(self, episode_count: int) -> bool:
        """Check if autosave should be triggered."""
        if episode_count - self.last_save_count >= self.autosave_interval:
            self.last_save_count = episode_count
            return True
        return False
        
    def get_available_models(self) -> Dict[str, List[str]]:
        """Get list of available models and their versions."""
        models = {}
        for file_path in self.base_path.glob("*.json"):
            parts = file_path.stem.rsplit('_', 2)
            model_name = parts[0]
            version = "_".join(parts[1:]) if len(parts) > 1 else ""
            if model_name not in models:
                models[model_name] = []
            models[model_name].append(version)
        return models
        
    def delete_model(self, model_name: str, version: Optional[str] = None):
        """Delete a model version or all versions."""
        if version:
            file_path = self.base_path / f"{model_name}_{version}.json"
            if file_path.exists():
                file_path.unlink()
                self.logger.info(f"Deleted {file_path}")
        else:
            for file_path in self.base_path.glob(f"{model_name}_*.json"):
                file_path.unlink()
                self.logger.info(f"Deleted {file_path}")
                
    def get_model_metadata(self, model_name: str, version: str) -> Dict[str, Any]:
        """Get metadata for a specific model version."""
        file_path = self.base_path / f"{model_name}_{version}.json"
        with open(file_path) as f:
            data = json.load(f)
        return data['metadata'] 
