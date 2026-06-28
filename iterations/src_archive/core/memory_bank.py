import json
from datetime import datetime
from typing import Dict, Any, Optional, List
from src.utils.logger import get_logger

logger = get_logger(__name__)

class MemoryBank:
    def __init__(self, persistence_file: str = 'memory_bank.json'):
        self.memory: Dict[str, Dict[str, Any]] = {}
        self.persistence_file = persistence_file
        self.load_persistence()

    async def initialize(self):
        return None

    async def add_memory(self, episode: str, memory: Dict[str, Any]):
        if episode not in self.memory:
            self.memory[episode] = {}
        key = str(len(self.memory[episode]))
        self.memory[episode][key] = {
            "value": memory,
            "timestamp": datetime.now().isoformat(),
        }
        self.save_persistence()

    async def get_memories(self, episode: str) -> List[Dict[str, Any]]:
        return [entry["value"] for entry in self.memory.get(episode, {}).values()]
        
    def store(self, agent_name: str, key: str, value: Any, timestamp: Optional[datetime] = None):
        """Store data in memory bank with optional timestamp"""
        if agent_name not in self.memory:
            self.memory[agent_name] = {}
            
        if timestamp is None:
            timestamp = datetime.now()
            
        self.memory[agent_name][key] = {
            'value': value,
            'timestamp': timestamp.isoformat()
        }
        
        self.save_persistence()
        
    def retrieve(self, agent_name: str, key: str) -> Optional[Any]:
        """Retrieve data from memory bank"""
        if agent_name not in self.memory:
            return None
            
        data = self.memory[agent_name].get(key)
        if data is None:
            return None
            
        return data['value']
        
    def retrieve_with_timestamp(self, agent_name: str, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve data with its timestamp"""
        if agent_name not in self.memory:
            return None
            
        return self.memory[agent_name].get(key)
        
    def retrieve_all(self, agent_name: str) -> Dict[str, Any]:
        """Retrieve all data for an agent"""
        return self.memory.get(agent_name, {})
        
    async def clear(self, agent_name: Optional[str] = None):
        """Clear memory bank, optionally for a specific agent"""
        if agent_name is None:
            self.memory = {}
        else:
            self.memory[agent_name] = {}
        self.save_persistence()
        
    def save_persistence(self):
        """Save memory bank to file"""
        try:
            with open(self.persistence_file, 'w') as f:
                json.dump(self.memory, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save memory bank: {str(e)}")
            
    def load_persistence(self):
        """Load memory bank from file"""
        try:
            with open(self.persistence_file, 'r') as f:
                self.memory = json.load(f)
        except FileNotFoundError:
            logger.info("No existing memory bank file found")
        except Exception as e:
            logger.error(f"Failed to load memory bank: {str(e)}")
            
    def get_agents(self) -> List[str]:
        """Get list of all agents in memory bank"""
        return list(self.memory.keys())
        
    def get_keys(self, agent_name: str) -> List[str]:
        """Get list of all keys for an agent"""
        return list(self.memory.get(agent_name, {}).keys())
        
    def get_latest_timestamp(self, agent_name: str, key: str) -> Optional[datetime]:
        """Get the latest timestamp for a key"""
        data = self.retrieve_with_timestamp(agent_name, key)
        if data is None:
            return None
        return datetime.fromisoformat(data['timestamp'])
