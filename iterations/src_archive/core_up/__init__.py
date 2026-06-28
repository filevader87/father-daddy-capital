"""
FDC V19.6 Core Up - Central Orchestration Layer

Provides unified agent coordination, risk gating, and execution logic.
Replaces legacy generate_signal_v188 with structured, pluggable design.

Architecture:
1. SignalGateway: Pluggable signal handlers (RSI, AETS, momentum, etc.)
2. RiskGateway: Risk gate checks before execution
3. DecisionGraph: Decision flow logic
4. ExecutionEngine: Trade execution (paper/live)
5. RegimeHandler: Market regime adaptation
6. MemoryRegistry: Shared memory banks
7. StateManager: State synchronization
"""

from .agent_orchestrator import AgentOrchestrator
from .signal_gateway import SignalGateway
from .risk_gateway import RiskGateway
from .decision_graph import DecisionGraph
from .execution_engine import ExecutionEngine
from .regime_handler import RegimeHandler
from .memory_registry import MemoryRegistry
from .state_manager import StateManager

__all__ = [
    "AgentOrchestrator",
    "SignalGateway",
    "RiskGateway",
    "DecisionGraph",
    "ExecutionEngine",
    "RegimeHandler",
    "MemoryRegistry",
    "StateManager",
]