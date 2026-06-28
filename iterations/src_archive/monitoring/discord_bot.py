import discord
from discord.ext import commands, tasks
import asyncio
from typing import Dict, Optional
from datetime import datetime
from src.utils.logger import get_logger
from src.config import TradingConfig as config
from src.monitoring.system_monitor import system_monitor
from src.utils.risk_manager import risk_manager

logger = get_logger(__name__)

class TradingBot(commands.Bot):
    def __init__(self, command_prefix: str = "!"):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=command_prefix, intents=intents)
        
        self.status_channel: Optional[discord.TextChannel] = None
        self.alert_channel: Optional[discord.TextChannel] = None
        self.last_status: Dict = {}
        
        # Add commands
        self.add_command(self.status)
        self.add_command(self.metrics)
        self.add_command(self.positions)
        self.add_command(self.trades)
        self.add_command(self.alerts)
        self.add_command(self.help_cmd)
        
    async def setup_hook(self):
        """Setup bot when ready"""
        self.status_update.start()
        self.monitor_alerts.start()
        
    @tasks.loop(minutes=5)
    async def status_update(self):
        """Update status channel with system metrics"""
        if not self.status_channel:
            return
            
        metrics = system_monitor.get_system_metrics()
        if metrics == self.last_status:
            return
            
        self.last_status = metrics
        
        embed = discord.Embed(
            title="System Status",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name="CPU Usage",
            value=f"{metrics['cpu_percent']:.1f}%",
            inline=True
        )
        embed.add_field(
            name="Memory Usage",
            value=f"{metrics['memory_percent']:.1f}%",
            inline=True
        )
        embed.add_field(
            name="Disk Usage",
            value=f"{metrics['disk_percent']:.1f}%",
            inline=True
        )
        
        await self.status_channel.send(embed=embed)
        
    @tasks.loop(minutes=1)
    async def monitor_alerts(self):
        """Monitor system for alerts"""
        if not self.alert_channel:
            return
            
        metrics = system_monitor.get_system_metrics()
        
        # Check CPU threshold
        if metrics['cpu_percent'] > config.get_monitoring_threshold('cpu'):
            await self.alert_channel.send(
                f"⚠️ High CPU Usage: {metrics['cpu_percent']:.1f}%"
            )
            
        # Check memory threshold
        if metrics['memory_percent'] > config.get_monitoring_threshold('memory'):
            await self.alert_channel.send(
                f"⚠️ High Memory Usage: {metrics['memory_percent']:.1f}%"
            )
            
        # Check disk threshold
        if metrics['disk_percent'] > config.get_monitoring_threshold('disk'):
            await self.alert_channel.send(
                f"⚠️ High Disk Usage: {metrics['disk_percent']:.1f}%"
            )
            
    @commands.command()
    async def status(self, ctx):
        """Get current system status"""
        metrics = system_monitor.get_system_metrics()
        
        embed = discord.Embed(
            title="Current System Status",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name="CPU Usage",
            value=f"{metrics['cpu_percent']:.1f}%",
            inline=True
        )
        embed.add_field(
            name="Memory Usage",
            value=f"{metrics['memory_percent']:.1f}%",
            inline=True
        )
        embed.add_field(
            name="Disk Usage",
            value=f"{metrics['disk_percent']:.1f}%",
            inline=True
        )
        
        await ctx.send(embed=embed)
        
    @commands.command()
    async def metrics(self, ctx):
        """Get trading metrics"""
        metrics = risk_manager.get_daily_metrics()
        
        embed = discord.Embed(
            title="Trading Metrics",
            color=discord.Color.gold(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name="Daily Trades",
            value=str(metrics['trades_count']),
            inline=True
        )
        embed.add_field(
            name="Total Risk",
            value=f"${metrics['total_risk']:,.2f}",
            inline=True
        )
        embed.add_field(
            name="Daily P&L",
            value=f"${metrics['daily_pnl']:,.2f}",
            inline=True
        )
        
        await ctx.send(embed=embed)
        
    @commands.command()
    async def positions(self, ctx):
        """Get current positions"""
        positions = risk_manager.positions
        
        if not positions:
            await ctx.send("No active positions")
            return
            
        embed = discord.Embed(
            title="Current Positions",
            color=discord.Color.purple(),
            timestamp=datetime.now()
        )
        
        for symbol, position in positions.items():
            metrics = risk_manager.get_position_metrics(symbol)
            embed.add_field(
                name=symbol,
                value=f"Qty: {position['qty']}\nAvg Price: ${position['avg_price']:.2f}\nP&L: ${metrics.get('unrealized_pnl', 0):,.2f}",
                inline=True
            )
            
        await ctx.send(embed=embed)
        
    @commands.command()
    async def trades(self, ctx, limit: int = 10):
        """Get recent trades"""
        trades = risk_manager.trades[-limit:]
        
        if not trades:
            await ctx.send("No recent trades")
            return
            
        embed = discord.Embed(
            title=f"Last {limit} Trades",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        
        for trade in trades:
            embed.add_field(
                name=f"{trade['symbol']} - {trade['timestamp'].strftime('%H:%M:%S')}",
                value=f"Action: {trade['action']}\nQty: {trade['qty']}\nPrice: ${trade['price']:.2f}\nP&L: ${trade['pnl']:,.2f}",
                inline=False
            )
            
        await ctx.send(embed=embed)
        
    @commands.command()
    async def alerts(self, ctx, channel: discord.TextChannel):
        """Set alert channel"""
        self.alert_channel = channel
        await ctx.send(f"Alert channel set to {channel.mention}")
        
    @commands.command()
    async def help_cmd(self, ctx):
        """Show available commands"""
        embed = discord.Embed(
            title="Trading Bot Commands",
            color=discord.Color.blue(),
            description="Available commands for monitoring and control"
        )
        
        commands = {
            "!status": "Get current system status",
            "!metrics": "Get trading metrics",
            "!positions": "Get current positions",
            "!trades [limit]": "Get recent trades (default: 10)",
            "!alerts #channel": "Set alert channel",
            "!help": "Show this help message"
        }
        
        for cmd, desc in commands.items():
            embed.add_field(name=cmd, value=desc, inline=False)
            
        await ctx.send(embed=embed)

# Create bot instance
bot = TradingBot()

async def start_bot(token: str):
    """Start the Discord bot"""
    try:
        await bot.start(token)
    except Exception as e:
        logger.error(f"Discord bot error: {str(e)}")
        raise 