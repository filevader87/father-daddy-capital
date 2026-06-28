from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
import asyncio
import logging
from enum import Enum
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
import json
from pathlib import Path
from src.config import TradingConfig

# Load configuration
config = TradingConfig.load_from_file()

class AlertChannel(Enum):
    """Alert notification channels."""
    EMAIL = "email"
    SMS = "sms"
    SLACK = "slack"
    TELEGRAM = "telegram"
    LOG = "log"

class AlertLevel(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"

@dataclass
class AlertConfig:
    """Configuration for alert channels."""
    email_config: Optional[Dict[str, Any]] = None
    sms_config: Optional[Dict[str, Any]] = None
    slack_config: Optional[Dict[str, Any]] = None
    telegram_config: Optional[Dict[str, Any]] = None
    log_config: Optional[Dict[str, Any]] = None

@dataclass
class Alert:
    """Risk alert data structure."""
    timestamp: datetime
    level: str
    symbol: str
    metric: str
    value: float
    threshold: float
    message: str
    channels: List[AlertChannel]
    action_taken: Optional[str] = None

class RiskAlertSystem:
    """Risk alert system for monitoring and notifying about risk events."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the risk alert system.
        
        Args:
            config (Dict[str, Any], optional): Configuration dictionary
        """
        self.config = self._load_config(config)
        self.logger = logging.getLogger("RiskAlertSystem")
        self.alerts = []
        self.alert_history = []
        self.alert_history: List[Alert] = []
        self.active_alerts: Dict[str, List[Alert]] = {}
        self.notification_queue = asyncio.Queue()
        self.processing_task = None
        
    def _load_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate alert configuration."""
        default_config = {
            'alert_thresholds': {
                'var_alert': config.VAR_THRESHOLD,
                'drawdown_alert': config.MAX_DRAWDOWN,
                'liquidity_alert': config.LIQUIDITY_THRESHOLD,
                'concentration_alert': config.MAX_POSITION_RISK,
                'correlation_alert': config.CORRELATION_THRESHOLD
            },
            'notification': {
                'email_enabled': True,
                'slack_enabled': True,
                'telegram_enabled': False,
                'min_severity': AlertLevel.WARNING
            },
            'alert_history': {
                'max_history': 1000,
                'retention_days': 30
            }
        }
        
        if config:
            default_config.update(config)
        return default_config
        
    async def start(self):
        """Start the alert processing system."""
        self.processing_task = asyncio.create_task(self._process_alerts())
        self.logger.info("Risk alert system started")
        
    async def stop(self):
        """Stop the alert processing system."""
        if self.processing_task:
            self.processing_task.cancel()
            try:
                await self.processing_task
            except asyncio.CancelledError:
                pass
        self.logger.info("Risk alert system stopped")
        
    async def send_alert(self,
                        level: str,
                        symbol: str,
                        metric: str,
                        value: float,
                        threshold: float,
                        message: str,
                        channels: List[AlertChannel],
                        action_taken: Optional[str] = None):
        """Send a risk alert through specified channels."""
        alert = Alert(
            timestamp=datetime.now(),
            level=level,
            symbol=symbol,
            metric=metric,
            value=value,
            threshold=threshold,
            message=message,
            channels=channels,
            action_taken=action_taken
        )
        
        # Add to active alerts
        if symbol not in self.active_alerts:
            self.active_alerts[symbol] = []
        self.active_alerts[symbol].append(alert)
        
        # Add to history
        self.alert_history.append(alert)
        
        # Queue for processing
        await self.notification_queue.put(alert)
        
    async def _process_alerts(self):
        """Process alerts from the queue."""
        while True:
            try:
                alert = await self.notification_queue.get()
                
                # Process through each channel
                for channel in alert.channels:
                    try:
                        if channel == AlertChannel.EMAIL:
                            await self._send_email(alert)
                        elif channel == AlertChannel.SMS:
                            await self._send_sms(alert)
                        elif channel == AlertChannel.SLACK:
                            await self._send_slack(alert)
                        elif channel == AlertChannel.TELEGRAM:
                            await self._send_telegram(alert)
                        elif channel == AlertChannel.LOG:
                            self._log_alert(alert)
                    except Exception as e:
                        self.logger.error(f"Error sending alert through {channel}: {e}")
                        
                self.notification_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error processing alert: {e}")
                
    async def _send_email(self, alert: Alert):
        """Send alert via email."""
        if not self.config['notification']['email_enabled']:
            return
            
        msg = MIMEMultipart()
        msg['From'] = self.config['email_config']['from_email']
        msg['To'] = self.config['email_config']['to_email']
        msg['Subject'] = f"Risk Alert: {alert.level} - {alert.symbol}"
        
        body = f"""
        Risk Alert Details:
        Time: {alert.timestamp}
        Level: {alert.level}
        Symbol: {alert.symbol}
        Metric: {alert.metric}
        Value: {alert.value}
        Threshold: {alert.threshold}
        Message: {alert.message}
        Action Taken: {alert.action_taken or 'None'}
        """
        
        msg.attach(MIMEText(body, 'plain'))
        
        with smtplib.SMTP(
            self.config['email_config']['smtp_server'],
            self.config['email_config']['smtp_port']
        ) as server:
            server.starttls()
            server.login(
                self.config['email_config']['username'],
                self.config['email_config']['password']
            )
            server.send_message(msg)
            
    async def _send_sms(self, alert: Alert):
        """Send alert via SMS."""
        if not self.config['notification']['sms_enabled']:
            return
            
        # Implement SMS sending logic using your preferred provider
        # Example using Twilio:
        # from twilio.rest import Client
        # client = Client(self.config['sms_config']['account_sid'],
        #                self.config['sms_config']['auth_token'])
        # client.messages.create(
        #     to=self.config['sms_config']['to_number'],
        #     from_=self.config['sms_config']['from_number'],
        #     body=f"Risk Alert: {alert.message}"
        # )
        
    async def _send_slack(self, alert: Alert):
        """Send alert via Slack."""
        if not self.config['notification']['slack_enabled']:
            return
            
        message = {
            "text": f"*Risk Alert: {alert.level}*\n"
                   f"Symbol: {alert.symbol}\n"
                   f"Metric: {alert.metric}\n"
                   f"Value: {alert.value}\n"
                   f"Threshold: {alert.threshold}\n"
                   f"Message: {alert.message}"
        }
        
        requests.post(
            self.config['slack_config']['webhook_url'],
            json=message
        )
        
    async def _send_telegram(self, alert: Alert):
        """Send alert via Telegram."""
        if not self.config['notification']['telegram_enabled']:
            return
            
        message = (
            f"*Risk Alert: {alert.level}*\n"
            f"Symbol: {alert.symbol}\n"
            f"Metric: {alert.metric}\n"
            f"Value: {alert.value}\n"
            f"Threshold: {alert.threshold}\n"
            f"Message: {alert.message}"
        )
        
        requests.post(
            f"https://api.telegram.org/bot{self.config['telegram_config']['bot_token']}/sendMessage",
            json={
                "chat_id": self.config['telegram_config']['chat_id'],
                "text": message,
                "parse_mode": "Markdown"
            }
        )
        
    def _log_alert(self, alert: Alert):
        """Log alert to file."""
        if not self.config['notification']['log_enabled']:
            return
            
        log_path = Path(self.config['log_config']['path'])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(log_path, 'a') as f:
            f.write(
                f"{alert.timestamp.isoformat()} | "
                f"{alert.level} | "
                f"{alert.symbol} | "
                f"{alert.metric} | "
                f"{alert.value} | "
                f"{alert.threshold} | "
                f"{alert.message}\n"
            )
            
    def get_active_alerts(self, symbol: Optional[str] = None) -> List[Alert]:
        """Get active alerts for a symbol or all symbols."""
        if symbol:
            return self.active_alerts.get(symbol, [])
        return [alert for alerts in self.active_alerts.values() for alert in alerts]
        
    def get_alert_history(self,
                         start_time: Optional[datetime] = None,
                         end_time: Optional[datetime] = None) -> List[Alert]:
        """Get alert history with optional time filters."""
        history = self.alert_history
        
        if start_time:
            history = [alert for alert in history if alert.timestamp >= start_time]
            
        if end_time:
            history = [alert for alert in history if alert.timestamp <= end_time]
            
        return history
        
    def clear_alerts(self, symbol: Optional[str] = None):
        """Clear alerts for a symbol or all symbols."""
        if symbol:
            self.active_alerts.pop(symbol, None)
        else:
            self.active_alerts.clear() 