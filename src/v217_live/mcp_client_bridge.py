"""
MCP Client Bridge — Lets FDC runners call MCP servers as Python functions.

Usage in runners:
    from mcp_client_bridge import MCPRouter
    router = MCPRouter()
    
    # Get BTC spot from ccxt
    price = await router.call("ccxt", "get_ticker", {"symbol": "BTC/USDT", "exchange": "binance"})
    
    # Get Polymarket orderbook
    ob = await router.call("polymarket", "get_orderbook", {"market": "btc-5m-down"})
    
    # Get on-chain data
    bal = await router.call("onchain", "get_balance", {"address": "0x..."})
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, Optional

from mcp import ClientSession, StdioServerParameters  # type: ignore[attr-defined]
from mcp.client.stdio import stdio_client

log = logging.getLogger("mcp_bridge")

# ─── Server definitions matching config.yaml ───
MCP_SERVERS: Dict[str, dict] = {
    "polymarket": {"command": "npx", "args": ["-y", "polymarket-agent-mcp"]},
    "ccxt": {"command": "npx", "args": ["-y", "@mcpfun/mcp-server-ccxt"]},
    "codex": {"command": "npx", "args": ["-y", "@codex-data/codex-mcp"]},
    "onchain": {"command": "npx", "args": ["-y", "@bankless/onchain-mcp"]},
    "evmscope": {"command": "npx", "args": ["-y", "evmscope"]},
    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
    "sqlite": {"command": "uvx", "args": ["mcp-server-sqlite"]},
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem",
              "/home/naq1987s/father-daddy-capital"]},
    "notion": {"command": "npx", "args": ["-y", "@notionhq/notion-mcp-server"]},
    "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp"]},
    "playwright": {"command": "npx", "args": ["-y", "@executeautomation/mcp-playwright"]},
}


class MCPServerConnection:
    """Manages a persistent stdio connection to one MCP server."""
    
    def __init__(self, name: str, params: dict):
        self.name = name
        self.params = StdioServerParameters(**params)
        self._session: Optional[ClientSession] = None
        self._read_stream = None
        self._write_stream = None
        self._context_manager = None
        self._tools_cache: Optional[Dict[str, Any]] = None
    
    async def connect(self) -> bool:
        """Start the MCP server process and establish session."""
        try:
            self._context_manager = stdio_client(self.params)
            self._read_stream, self._write_stream = await self._context_manager.__aenter__()
            self._session = ClientSession(self._read_stream, self._write_stream)
            await self._session.__aenter__()
            await self._session.initialize()
            log.info(f"MCP [{self.name}] connected")
            return True
        except Exception as e:
            log.error(f"MCP [{self.name}] connect failed: {e}")
            self._session = None
            return False
    
    async def disconnect(self):
        """Close the MCP server connection."""
        try:
            if self._session:
                await self._session.__aexit__(None, None, None)
            if self._context_manager:
                await self._context_manager.__aexit__(None, None, None)
        except Exception as e:
            log.warning(f"MCP [{self.name}] disconnect error: {e}")
        self._session = None
    
    async def list_tools(self) -> Dict[str, Any]:
        """List all available tools from this server."""
        if not self._session:
            return {}
        if self._tools_cache:
            return self._tools_cache
        try:
            result = await self._session.list_tools()
            self._tools_cache = {t.name: t for t in result.tools}
            return self._tools_cache
        except Exception as e:
            log.error(f"MCP [{self.name}] list_tools failed: {e}")
            return {}
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a specific tool on this MCP server."""
        if not self._session:
            raise RuntimeError(f"MCP [{self.name}] not connected")
        try:
            result = await self._session.call_tool(tool_name, arguments)
            # Extract text content from result
            if result.content:
                for item in result.content:
                    if hasattr(item, 'text') and item.text:  # type: ignore[union-attr]
                        try:
                            return json.loads(item.text)  # type: ignore[union-attr]
                        except (json.JSONDecodeError, TypeError):
                            return item.text  # type: ignore[union-attr]
                return str(result.content)
            return None
        except Exception as e:
            log.error(f"MCP [{self.name}] call_tool({tool_name}) failed: {e}")
            raise
    
    @property
    def is_connected(self) -> bool:
        return self._session is not None


class MCPRouter:
    """
    Central router for FDC bots to call MCP servers.
    
    Supports both async (native) and sync (thread-wrapped) calls.
    """
    
    def __init__(self, servers: Optional[list] = None):
        """
        Args:
            servers: List of server names to connect. None = all.
        """
        self._connections: Dict[str, MCPServerConnection] = {}
        self._server_names = servers or list(MCP_SERVERS.keys())
    
    async def boot(self) -> Dict[str, bool]:
        """Connect to all configured servers. Returns {name: connected}."""
        results = {}
        for name in self._server_names:
            if name not in MCP_SERVERS:
                log.warning(f"MCP server '{name}' not defined, skipping")
                results[name] = False
                continue
            conn = MCPServerConnection(name, MCP_SERVERS[name])
            try:
                connected = await asyncio.wait_for(conn.connect(), timeout=30)
            except (asyncio.TimeoutError, Exception) as e:
                log.warning(f"MCP [{name}] boot timeout/error: {e}")
                connected = False
                try:
                    await conn.disconnect()
                except Exception:
                    pass
                conn._session = None
            if connected:
                self._connections[name] = conn
            results[name] = connected
        log.info(f"MCP Router: {sum(results.values())}/{len(results)} servers connected")
        return results
    
    async def shutdown(self):
        """Disconnect all servers."""
        for conn in self._connections.values():
            await conn.disconnect()
        self._connections.clear()
    
    async def call(self, server: str, tool: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Call a tool on a specific MCP server."""
        args: Dict[str, Any] = arguments if arguments is not None else {}
        if server not in self._connections:
            raise KeyError(f"Server '{server}' not connected. Available: {list(self._connections.keys())}")
        return await self._connections[server].call_tool(tool, args)
    
    async def list_tools(self, server: str) -> Dict[str, Any]:
        """List tools available on a specific server."""
        if server not in self._connections:
            return {}
        return await self._connections[server].list_tools()
    
    async def list_all_tools(self) -> Dict[str, Dict[str, Any]]:
        """List tools from all connected servers."""
        result = {}
        for name, conn in self._connections.items():
            result[name] = await conn.list_tools()
        return result
    
    # ─── Sync wrappers for non-async runners ───
    
    def call_sync(self, server: str, tool: str, arguments: Dict[str, Any] = None) -> Any:
        """Synchronous wrapper — runs call() in a new event loop."""
        return asyncio.run(self.call(server, tool, arguments or {}))
    
    def boot_sync(self) -> Dict[str, bool]:
        """Synchronous boot wrapper."""
        return asyncio.run(self.boot())
    
    def shutdown_sync(self):
        """Synchronous shutdown wrapper."""
        return asyncio.run(self.shutdown())


# ─── Convenience: Pre-built domain routers ───

class CryptoMCP(MCPRouter):
    """Router pre-configured for crypto/trading servers only."""
    
    CRYPTO_SERVERS = ["polymarket", "ccxt"]  # Boot these first; others lazy
    
    def __init__(self):
        super().__init__(servers=self.CRYPTO_SERVERS)
    
    # ─── Polymarket shortcuts ───
    
    async def pm_orderbook(self, token_id: str) -> Any:
        return await self.call("polymarket", "get-orderbook", {"token_id": token_id})
    
    async def pm_markets(self, slug: str) -> Any:
        return await self.call("polymarket", "get-markets", {"slug": slug})
    
    async def pm_smart_money(self) -> Any:
        return await self.call("polymarket", "traders.discover", {})
    
    # ─── CCXT shortcuts ───
    
    async def spot_price(self, symbol: str = "BTC/USDT", exchange: str = "binance") -> Any:
        return await self.call("ccxt", "get-ticker", {"symbol": symbol, "exchange": exchange})
    
    async def spot_prices_multi(self, symbols: list, exchange: str = "binance") -> Dict[str, Any]:
        """Fetch multiple spot prices concurrently."""
        tasks = [self.spot_price(s, exchange) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {s: r for s, r in zip(symbols, results) if not isinstance(r, Exception)}
    
    async def ohlcv(self, symbol: str = "BTC/USDT", timeframe: str = "5m",
                    exchange: str = "binance", limit: int = 100) -> Any:
        return await self.call("ccxt", "get-ohlcv", {
            "symbol": symbol, "timeframe": timeframe,
            "exchange": exchange, "limit": limit
        })
    
    # ─── On-chain shortcuts ───
    
    async def eth_balance(self, address: str, chain: str = "polygon") -> Any:
        return await self.call("onchain", "get_balance", {"address": address, "chain": chain})
    
    async def token_price(self, token_address: str, chain: str = "polygon") -> Any:
        return await self.call("evmscope", "get_token_price", {"address": token_address, "chain": chain})


class DataMCP(MCPRouter):
    """Router for data/utility servers."""
    
    DATA_SERVERS = ["fetch", "sqlite", "filesystem"]
    
    def __init__(self):
        super().__init__(servers=self.DATA_SERVERS)
    
    async def web_fetch(self, url: str) -> Any:
        return await self.call("fetch", "fetch", {"url": url})
    
    async def sql_query(self, db_path: str, query: str) -> Any:
        return await self.call("sqlite", "query", {"db_path": db_path, "query": query})


# ─── CLI test ───

async def _test():
    """Quick test: boot servers, list tools, call one."""
    router = CryptoMCP()
    print("Booting crypto MCP servers...")
    results = await router.boot()
    for name, ok in results.items():
        print(f"  {name}: {'✓' if ok else '✗'}")
    
    connected = {k for k, v in results.items() if v}
    if connected:
        print(f"\nListing tools from {len(connected)} servers...")
        all_tools = await router.list_all_tools()
        for name, tools in all_tools.items():
            print(f"\n  [{name}] {len(tools)} tools:")
            for tname, t in list(tools.items())[:5]:
                desc = (t.description or "")[:60]
                print(f"    - {tname}: {desc}")
            if len(tools) > 5:
                print(f"    ... +{len(tools)-5} more")
    
    await router.shutdown()
    print("\nDone.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    asyncio.run(_test())