
# src/decision/memory_bank.py

import os
import json
from datetime import datetime

class MemoryBank:
    def __init__(self, memory_file="memory_bank.json", max_length=500):
        self.memory_file = memory_file
        self.max_length = max_length
        self.memory = self.load_memory()

    def load_memory(self):
        if os.path.exists(self.memory_file):
            with open(self.memory_file, "r") as f:
                return json.load(f)
        return []

    def save_memory(self):
        with open(self.memory_file, "w") as f:
            json.dump(self.memory, f, indent=2)

    def add(self, experience):
        experience["timestamp"] = datetime.now().isoformat()
        self.memory.append(experience)
        if len(self.memory) > self.max_length:
            self.memory = self.memory[-self.max_length:]
        self.save_memory()

    def recall(self, n=5):
        return self.memory[-n:]

    def clear(self):
        self.memory = []
        self.save_memory()
