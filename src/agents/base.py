from abc import ABC, abstractmethod

class AgentInterface(ABC):
    @abstractmethod
    def fetch_data(self):
        """Fetch raw data for processing."""
        pass

    @abstractmethod
    def preprocess(self, raw):
        """Preprocess raw data into features."""
        pass

    @abstractmethod
    def predict(self, features):
        """Generate predictions from features."""
        pass

    @abstractmethod
    def act(self, signal):
        """Execute actions based on predictions."""
        pass 