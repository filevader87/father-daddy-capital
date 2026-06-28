from typing import Dict, Optional, List, Any
from datetime import datetime, timedelta
import asyncio
from src.utils.logger import get_logger
from src.utils.config_loader import config_loader
from src.risk.risk_manager import risk_manager, RiskManager
from src.monitoring.system_monitor import SystemMonitor
from src.monitoring.discord_bot import bot
from src.utils.performance_optimizer import performance_optimizer
from src.backtest.backtester import Backtester
from src.core.memory_bank import MemoryBank
from src.core.decision_graph import DecisionGraph
from src.core.regime_tracker import RegimeTracker
from src.core.analysis import SystemAnalyzer
from src.core.event_bus import EventBus
from src.core.service_registry import service_registry
from src.core.agent_protocol import AgentProtocol
from src.core.circuit_breaker import CircuitBreaker
from src.core.config_manager import ConfigManager
from src.config import TradingConfig as config

logger = get_logger(__name__)

class Coordinator:
    def __init__(self):
        self.is_running = False
        self.trading_enabled = False
        self.last_health_check = datetime.now()
        self.system_metrics: Dict = {}
        self.trading_metrics: Dict = {}
        self.performance_metrics: Dict = {}
        
        # Initialize core components
        self.memory_bank = MemoryBank()
        self.decision_graph = DecisionGraph()
        self.regime_tracker = RegimeTracker()
        self.system_analyzer = SystemAnalyzer(
            self.memory_bank,
            self.decision_graph,
            self.regime_tracker
        )
        
        # Initialize monitoring and circuit breakers
        self.event_bus = service_registry.get_event_bus()
        self.system_monitor = SystemMonitor(self.event_bus)
        self.circuit_breakers = {
            'risk_manager': CircuitBreaker('risk_manager'),
            'trading_engine': CircuitBreaker('trading_engine'),
            'data_feed': CircuitBreaker('data_feed'),
            'order_execution': CircuitBreaker('order_execution')
        }
        
        # Component registry
        self.registered_components: Dict[str, object] = {}
        self.agent_protocol = service_registry.get_agent_protocol()
        self.risk_manager = service_registry.get_risk_manager()
        self.component_states: Dict[str, Dict[str, Any]] = {}
        self.interaction_log: List[Dict[str, Any]] = []
        self.coordination_task = None
        self.config_manager = service_registry.get_config_manager()
        
    async def initialize(self):
        """Initialize all system components"""
        try:
            # Initialize configuration
            config_loader.load_config()
            
            # Initialize performance monitoring
            performance_optimizer.reset_profiling()
            
            # Initialize risk management
            risk_manager.reset_daily_metrics()
            
            # Register core components
            self.register_component('memory_bank', self.memory_bank)
            self.register_component('decision_graph', self.decision_graph)
            self.register_component('regime_tracker', self.regime_tracker)
            self.register_component('risk_manager', risk_manager)
            self.register_component('performance_optimizer', performance_optimizer)
            self.register_component('system_monitor', self.system_monitor)
            self.register_component('system_analyzer', self.system_analyzer)
            
            # Start monitoring system
            await self.system_monitor.start()
            
            # Start Discord bot
            discord_token = config_loader.get('discord.token')
            if discord_token:
                asyncio.create_task(bot.start(discord_token))
                
            # Subscribe to system events
            self.event_bus.subscribe("system_state_change", self._handle_state_change)
            self.event_bus.subscribe("risk_alert", self._handle_risk_alert)
            self.event_bus.subscribe("agent_message", self._handle_agent_message)
            self.event_bus.subscribe("system_alert", self._handle_system_alert)
            
            # Start coordination loop
            self.coordination_task = asyncio.create_task(self._coordination_loop())
            
            logger.info("System components initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize system: {str(e)}")
            return False
            
    def register_component(self, name: str, component: object):
        """Register a system component"""
        self.registered_components[name] = component
        logger.info(f"Component registered: {name}")
        
    def get_component(self, name: str) -> Optional[object]:
        """Get a registered component"""
        return self.registered_components.get(name)
        
    async def start_trading(self):
        """Start trading operations"""
        if not self.trading_enabled:
            self.trading_enabled = True
            logger.info("Trading operations started")
            await self._notify_status_change("Trading operations started")
            
    async def stop_trading(self):
        """Stop trading operations"""
        if self.trading_enabled:
            self.trading_enabled = False
            logger.info("Trading operations stopped")
            await self._notify_status_change("Trading operations stopped")
            
    async def run_health_check(self):
        """Run system health check"""
        try:
            # Get system metrics
            self.system_metrics = self.system_monitor.get_system_metrics()
            
            # Get trading metrics
            self.trading_metrics = risk_manager.get_daily_metrics()
            
            # Get performance metrics
            self.performance_metrics = performance_optimizer.get_performance_report()
            
            # Update memory bank with current state
            self.memory_bank.store('system', 'metrics', {
                'system': self.system_metrics,
                'trading': self.trading_metrics,
                'performance': self.performance_metrics
            })
            
            # Run system analysis
            analysis_results = self._run_system_analysis()
            
            # Check for critical issues
            if self._check_critical_issues():
                await self._handle_critical_issues()
                
            self.last_health_check = datetime.now()
            logger.info("Health check completed successfully")
            
        except Exception as e:
            logger.error(f"Health check failed: {str(e)}")
            
    def _run_system_analysis(self) -> Dict:
        """Run comprehensive system analysis"""
        return {
            'decision_quality': self.system_analyzer.analyze_decision_quality(timedelta(days=1)),
            'regime_quality': self.system_analyzer.analyze_regime_quality(),
            'memory_quality': self.system_analyzer.analyze_memory_quality(),
            'system_efficiency': self.system_analyzer.analyze_system_efficiency(),
            'risk_efficiency': self.system_analyzer.analyze_risk_efficiency()
        }
            
    def _check_critical_issues(self) -> bool:
        """Check for critical system issues"""
        # Check system resources
        if (self.system_metrics['cpu_percent'] > config_loader.get_monitoring_threshold('cpu') or
            self.system_metrics['memory_percent'] > config_loader.get_monitoring_threshold('memory')):
            return True
            
        # Check trading metrics
        if (self.trading_metrics['max_drawdown'] > config_loader.get_trading_risk_limit('drawdown') or
            self.trading_metrics['total_risk'] > config_loader.get_trading_risk_limit('daily_risk')):
            return True
            
        return False
        
    async def _handle_critical_issues(self):
        """Handle critical system issues"""
        # Stop trading if enabled
        if self.trading_enabled:
            await self.stop_trading()
            
        # Send critical alert
        await self._notify_status_change("Critical system issues detected. Trading stopped.")
        
    async def _notify_status_change(self, message: str):
        """Notify status changes through Discord"""
        if bot.alert_channel:
            await bot.alert_channel.send(f"⚠️ {message}")
            
    async def run_backtest(self, strategy, data, start_date=None, end_date=None):
        """Run backtest with coordinated components"""
        try:
            backtester = Backtester()
            results = backtester.run_backtest(data, strategy, start_date, end_date)
            
            # Log performance metrics
            performance_optimizer.profile(backtester._calculate_performance_metrics)
            
            # Update risk manager with backtest results
            risk_manager.update_backtest_metrics(results)
            
            # Store backtest results in memory bank
            self.memory_bank.store('backtest', 'results', results)
            
            # Log decisions from backtest
            for trade in results['trades']:
                self.decision_graph.log_decision(
                    state=trade['state'],
                    action=trade['action'],
                    regime=trade['regime'],
                    reward=trade['reward']
                )
                
            return results
        except Exception as e:
            logger.error(f"Backtest failed: {str(e)}")
            raise
            
    def get_system_status(self) -> Dict:
        """Get comprehensive system status"""
        return {
            'system_metrics': self.system_metrics,
            'trading_metrics': self.trading_metrics,
            'performance_metrics': self.performance_metrics,
            'trading_enabled': self.trading_enabled,
            'last_health_check': self.last_health_check,
            'memory_bank': self.memory_bank.retrieve_all('system'),
            'decision_graph': self.decision_graph.get_graph(),
            'regime_tracker': self.regime_tracker.get_log(),
            'analysis': self._run_system_analysis()
        }

    async def _coordination_loop(self):
        """Main coordination loop."""
        while True:
            try:
                # Check system health
                await self._check_system_health()
                
                # Check circuit breakers
                await self._check_circuit_breakers()
                
                # Synchronize component states
                await self._synchronize_states()
                
                # Process pending interactions
                await self._process_interactions()
                
                await asyncio.sleep(1)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in coordination loop: {e}")
                
    async def _check_system_health(self):
        """Check health of all system components."""
        health_status = {}
        
        # Check agent protocol
        agent_status = self.agent_protocol.get_agent_status()
        health_status["agents"] = {
            "total": len(agent_status),
            "active": sum(1 for status in agent_status.values() if status["registered"])
        }
        
        # Check risk manager
        risk_status = self.risk_manager.get_status()
        health_status["risk"] = {
            "alerts": len(risk_status.get("active_alerts", [])),
            "metrics": risk_status.get("metrics", {})
        }
        
        # Publish health status
        await self.event_bus.publish("system_health", health_status)
        
    async def _synchronize_states(self):
        """Synchronize states between components."""
        # Get current states
        agent_states = self.agent_protocol.get_agent_status()
        risk_states = self.risk_manager.get_status()
        
        # Update component states
        self.component_states.update({
            "agents": agent_states,
            "risk": risk_states
        })
        
        # Broadcast state updates
        await self.agent_protocol.broadcast(
            "coordinator",
            {
                "type": "state_update",
                "states": self.component_states
            }
        )
        
    async def _process_interactions(self):
        """Process pending component interactions."""
        # Get pending interactions from log
        pending = [i for i in self.interaction_log if not i.get("processed")]
        
        for interaction in pending:
            try:
                # Process based on interaction type
                if interaction["type"] == "risk_alert":
                    await self._process_risk_alert(interaction)
                elif interaction["type"] == "agent_request":
                    await self._process_agent_request(interaction)
                elif interaction["type"] == "state_change":
                    await self._process_state_change(interaction)
                    
                interaction["processed"] = True
                interaction["processed_at"] = datetime.now()
                
            except Exception as e:
                logger.error(f"Error processing interaction: {e}")
                interaction["error"] = str(e)
                
    async def _handle_state_change(self, event):
        """Handle system state change events."""
        self.interaction_log.append({
            "type": "state_change",
            "component": event.data.get("component"),
            "state": event.data.get("state"),
            "timestamp": datetime.now(),
            "processed": False
        })
        
    async def _handle_risk_alert(self, event):
        """Handle risk alert events."""
        self.interaction_log.append({
            "type": "risk_alert",
            "alert": event.data,
            "timestamp": datetime.now(),
            "processed": False
        })
        
        # Notify relevant agents
        await self.agent_protocol.broadcast(
            "coordinator",
            {
                "type": "risk_alert",
                "alert": event.data
            }
        )
        
    async def _handle_agent_message(self, event):
        """Handle agent message events."""
        self.interaction_log.append({
            "type": "agent_request",
            "message": event.data,
            "timestamp": datetime.now(),
            "processed": False
        })
        
    async def _process_risk_alert(self, interaction):
        """Process risk alert interactions."""
        alert = interaction["alert"]
        
        # Check if alert requires agent action
        if alert.get("level") in ["CRITICAL", "HIGH"]:
            # Request agent response
            await self.agent_protocol.broadcast(
                "coordinator",
                {
                    "type": "action_required",
                    "alert": alert,
                    "priority": "high"
                }
            )
            
    async def _process_agent_request(self, interaction):
        """Process agent request interactions."""
        message = interaction["message"]
        
        # Route request to appropriate component
        if message.get("target") == "risk_manager":
            response = await self.risk_manager.handle_request(message)
            await self.agent_protocol.send_message(
                "coordinator",
                message["sender"],
                {
                    "type": "response",
                    "content": response
                }
            )
            
    async def _process_state_change(self, interaction):
        """Process state change interactions."""
        component = interaction["component"]
        state = interaction["state"]
        
        # Update risk parameters if needed
        if component == "market_regime":
            await self.risk_manager.adjust_risk_parameters(state)
            
    async def _handle_system_alert(self, event):
        """Handle system alert events."""
        alerts = event.data.get("alerts", [])
        for alert in alerts:
            logger.warning(f"System alert: {alert}")
            if bot.alert_channel:
                await bot.alert_channel.send(f"⚠️ {alert}")
                
    async def _check_circuit_breakers(self):
        """Check circuit breaker status."""
        for name, breaker in self.circuit_breakers.items():
            metrics = breaker.get_metrics()
            if metrics["state"] == "open":
                logger.warning(f"Circuit breaker {name} is open")
                await self._handle_circuit_breaker_open(name)
            elif metrics["state"] == "half_open":
                logger.info(f"Circuit breaker {name} is in half-open state")
                await self._handle_circuit_breaker_half_open(name)
                
    async def _handle_circuit_breaker_open(self, name: str):
        """Handle open circuit breaker."""
        # Stop trading if critical component
        if name in ["risk_manager", "trading_engine"]:
            await self.stop_trading()
            
        # Notify through Discord
        if bot.alert_channel:
            await bot.alert_channel.send(
                f"⚠️ Circuit breaker {name} is open. Component is temporarily disabled."
            )
            
    async def _handle_circuit_breaker_half_open(self, name: str):
        """Handle half-open circuit breaker."""
        # Attempt recovery
        component = self.get_component(name)
        if component:
            try:
                # Test component functionality
                if hasattr(component, "health_check"):
                    await component.health_check()
                # If successful, circuit breaker will close automatically
            except Exception as e:
                logger.error(f"Recovery attempt failed for {name}: {e}")
                
    def get_system_health(self) -> Dict[str, Any]:
        """Get comprehensive system health status."""
        return {
            "system_metrics": self.system_monitor.get_system_metrics(),
            "component_metrics": {
                name: self.system_monitor.get_component_metrics(name)
                for name in self.registered_components
            },
            "circuit_breakers": {
                name: breaker.get_metrics()
                for name, breaker in self.circuit_breakers.items()
            },
            "trading_enabled": self.trading_enabled,
            "last_health_check": self.last_health_check
        }
        
    async def stop(self):
        """Stop the coordinator and all components."""
        if self.coordination_task:
            self.coordination_task.cancel()
            try:
                await self.coordination_task
            except asyncio.CancelledError:
                pass
                
        await self.system_monitor.stop()
        self.is_running = False
        logger.info("Coordinator stopped")

# Create singleton instance
coordinator = Coordinator() 