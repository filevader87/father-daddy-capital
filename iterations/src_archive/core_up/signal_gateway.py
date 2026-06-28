"""
Signal Gateway - Pluggable signal generation handlers

Provides:
- Interface for signal generation modules
- Registration of signal sources
- Unified signal format
- Signal validation
"""

from .agent_protocol import SignalHandlerProtocol


class SignalGateway:
    """
    Gateway for pluggable signal generation
    
    Signal providers:
    - RSI signal
    - AETS signal
    - Momentum signal
    - Sentiment signal
    - CORE_UP signal
    
    Each provider implements SignalHandlerProtocol
    """
    
    def __init__(self):
        self._handlers = {}
        self._default_handler = None
    
    @property
    def registered_handlers(self):
        """Return list of registered signal handlers"""
        return list(self._handlers.keys())
    
    def register(self, handler, name=None):
        """Register a signal handler"""
        if name is None:
            name = handler.__class__.__name__
        
        self._handlers[name] = handler
        return name
    
    def get_handler(self, name):
        """Get a specific signal handler"""
        return self._handlers.get(name)
    
    def handle_signal(self, market_data, handler_name=None):
        """
        Handle signal generation
        
        Args:
            market_data: Market data for signal generation
            handler_name: Specific handler to use, or first registered
    
        Returns:
            Signal dict or None
        """
        if handler_name:
            handler = self.get_handler(handler_name)
            if handler:
                return handler.generate_signal(market_data)
            return None
        
        # Use default or first handler
        handler = self._default_handler or next(iter(self._handlers.values()), None)
        if handler:
            return handler.generate_signal(market_data)
        return None
    

class SignalHandlerProtocol:
    """
    Protocol for signal generation handlers
    
    Each handler implements:
    - generate_signal(market_data) -> Signal
    - name (property)
    - description (property)
    - is_active (property, for enabling/disabling)
    """
    def __init__(self):
        self.name = None
        self.description = None
        self.is_active = True
    
    def generate_signal(self, market_data):
        """
        Generate signal from market data
        
        Returns:
            dict with:
                - action: 'buy', 'sell', 'hold', 'neutral'
                - value: numeric signal value
                - confidence: 0-1
                - reason: explanation of signal
        """
        raise NotImplementedError
    
    async def generate_signal_async(self, market_data):
        """Async version for non-blocking signal generation"""
        return self.generate_signal(market_data)
    
    def set_active(self, active: bool):
        """Enable/disable signal handler"""
        self.is_active = active
        return self.is_active