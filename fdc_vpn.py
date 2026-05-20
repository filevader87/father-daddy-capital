#!/usr/bin/env python3
"""
Father Daddy Capital — Polymarket VPN Config & Live Trading Proxy

Sets up NordVPN connection for bypassing US geoblock on Polymarket.
Routes Polymarket API calls through VPN/proxy to avoid 403 errors on POST endpoints.

Usage:
  python3 fdc_vpn.py --status          # Check VPN status
  python3 fdc_vpn.py --connect         # Connect to optimal server
  python3 fdc_vpn.py --disconnect      # Disconnect VPN
  python3 fdc_vpn.py --test            # Test PM API access through VPN
  python3 fdc_vpn.py --rotate          # Rotate to new server (evade detection)
"""

import subprocess
import json
import urllib.request
import urllib.parse
import time
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

# ─── Configuration ────────────────────────────────────────────────────────────
# NordVPN countries that work for Polymarket (non-US, crypto-friendly, fast)
PREFERRED_COUNTRIES = [
    "Switzerland",    # CH — crypto-friendly, strong privacy
    "Netherlands",    # NL — major exchange hub, low latency
    "Germany",         # DE — fast, reliable
    "Canada",          # CA — close to US, low latency
    "United Kingdom",  # UK — English-language, crypto OK
    "Panama",          # PA — offshore, privacy-focused
]

# NordVPN server groups optimized for streaming (stable, residential-like)
OPTIMAL_GROUPS = ["P2P", "Standard", "Double VPN"]

# Alternative: manual OpenVPN config path
OPENVPN_CONFIG_DIR = Path.home() / ".nordvpn" / "configs"

# ─── NordVPN CLI Commands ────────────────────────────────────────────────────

def nordvpn_status() -> dict:
    """Check current NordVPN connection status."""
    try:
        result = subprocess.run(["nordvpn", "status"], capture_output=True, text=True, timeout=10)
        output = result.stdout.strip()
        connected = "Connected" in output or "connected" in output.lower()
        server = None
        country = None
        ip = None
        if connected:
            for line in output.split("\n"):
                if "Server:" in line or "server:" in line.lower():
                    server = line.split(":")[-1].strip()
                if "Country:" in line or "country:" in line.lower():
                    country = line.split(":")[-1].strip()
                if "IP:" in line or "Your new IP:" in line:
                    ip = line.split(":")[-1].strip()
        return {"connected": connected, "server": server, "country": country, "ip": ip, "raw": output}
    except FileNotFoundError:
        return {"connected": False, "error": "nordvpn CLI not installed", "raw": ""}
    except Exception as e:
        return {"connected": False, "error": str(e), "raw": ""}


def nordvpn_connect(country: str = None) -> dict:
    """Connect to NordVPN, optionally specifying country."""
    status = nordvpn_status()
    if status.get("connected"):
        return {"success": True, "message": f"Already connected to {status.get('server', 'unknown')}", "status": status}

    cmd = ["nordvpn", "connect"]
    if country:
        cmd.append(country)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        # Wait for connection
        time.sleep(5)
        new_status = nordvpn_status()
        if new_status.get("connected"):
            return {"success": True, "message": f"Connected to {new_status.get('server', 'unknown')}", "status": new_status}
        else:
            return {"success": False, "message": result.stdout + result.stderr, "status": new_status}
    except FileNotFoundError:
        return {"success": False, "message": "nordvpn CLI not installed. Install with: sudo apt install nordvpn", "status": status}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Connection timeout (60s)", "status": status}


def nordvpn_disconnect() -> dict:
    """Disconnect from NordVPN."""
    try:
        result = subprocess.run(["nordvpn", "disconnect"], capture_output=True, text=True, timeout=15)
        time.sleep(2)
        status = nordvpn_status()
        return {"success": not status.get("connected", False), "message": result.stdout.strip(), "status": status}
    except FileNotFoundError:
        return {"success": False, "message": "nordvpn CLI not installed"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def nordvpn_rotate(preferred: list = None) -> dict:
    """Disconnect and reconnect to a different server for rotation."""
    if preferred is None:
        preferred = PREFERRED_COUNTRIES

    # Disconnect first
    nordvpn_disconnect()
    time.sleep(2)

    # Try preferred countries in order
    for country in preferred:
        result = nordvpn_connect(country)
        if result.get("success"):
            # Verify PM access
            pm_test = test_polymarket_access()
            if pm_test.get("success"):
                return {"success": True, "country": country, "vpn": result, "pm": pm_test}
            # If PM still blocked, try next country
    return {"success": False, "message": "Could not find a working VPN + PM configuration"}


# ─── Alternative: OpenVPN Manual Setup ────────────────────────────────────────

def setup_openvpn_config(server: str = "ch", protocol: str = "udp") -> dict:
    """
    Set up manual OpenVPN config for NordVPN.
    Downloads .ovpn config files from NordVPN API.
    
    Args:
        server: Country code (ch, nl, de, ca, uk)
        protocol: udp or tcp
    """
    config_dir = OPENVPN_CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
    
    # NordVPN server recommendation API
    url = f"https://api.nordvpn.com/v1/servers/recommendations?filters[servers_groups][0]=15&filters[servers_technologies][0]=1&filters[servers_countries][0]=1&limit=5"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-fdc/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            servers = json.loads(r.read())
        
        if not servers:
            return {"success": False, "message": "No servers found via API"}
        
        # Download config for best server
        best = servers[0]
        hostname = best.get("hostname", "")
        config_url = f"https://api.nordvpn.com/files/download/v2/{server}.{protocol}.ovpn"
        config_path = config_dir / f"{server}_{protocol}.ovpn"
        
        req = urllib.request.Request(config_url, headers={"User-Agent": "hermes-fdc/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            config_data = r.read()
        
        with open(config_path, "wb") as f:
            f.write(config_data)
        
        return {
            "success": True,
            "server": hostname,
            "config_path": str(config_path),
            "message": f"Downloaded config to {config_path}. Run: sudo openvpn {config_path}"
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


# ─── Polymarket Access Testing ────────────────────────────────────────────────

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

def test_polymarket_access() -> dict:
    """Test if Polymarket API access works (both GET and POST)."""
    results = {"success": False, "get": False, "post": False, "errors": []}
    
    # Test 1: GET (gamma API — should work from anywhere)
    try:
        url = f"{GAMMA}/public-search?q=Bitcoin"
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-fdc/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            events = data.get("events", [])
            results["get"] = True
            results["market_count"] = len(events)
    except Exception as e:
        results["errors"].append(f"GET error: {e}")
    
    # Test 2: POST (CLOB — blocked for US IPs)
    try:
        # Try a simple POST to check geoblock
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        url = f"{CLOB}/time"
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "hermes-fdc/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            results["post"] = True  # If we got here, CLOB is accessible
            results["server_time"] = data
    except urllib.error.HTTPError as e:
        if e.code == 403:
            results["errors"].append(f"POST blocked (403 Forbidden) — US geoblock active")
        else:
            results["errors"].append(f"POST HTTP error: {e.code}")
    except Exception as e:
        results["errors"].append(f"POST error: {e}")
    
    results["success"] = results["get"] and results["post"]
    return results


# ─── Polymarket Trading Proxy ────────────────────────────────────────────────

class PMProxy:
    """
    HTTP proxy wrapper for Polymarket API calls.
    Routes requests through VPN-connected interface.
    
    If NordVPN is connected, requests go through VPN automatically.
    If not connected, falls back to direct (will get 403 on POST).
    """
    
    def __init__(self):
        self._vpn_checked = False
        self._vpn_active = False
    
    def _ensure_vpn(self) -> bool:
        """Check VPN status, connect if needed."""
        if not self._vpn_checked:
            status = nordvpn_status()
            self._vpn_active = status.get("connected", False)
            self._vpn_checked = True
        return self._vpn_active
    
    def get(self, url: str, headers: dict = None) -> dict | list:
        """GET request through VPN."""
        self._ensure_vpn()
        hdrs = {"User-Agent": "hermes-fdc/1.0"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    
    def post(self, url: str, data: dict, headers: dict = None) -> dict:
        """POST request through VPN — this is where 403 geoblock hits."""
        self._ensure_vpn()
        if not self._vpn_active:
            # Try connecting first
            result = nordvpn_connect()
            self._vpn_active = result.get("success", False)
            if not self._vpn_active:
                raise ConnectionError("VPN not connected — Polymarket POST will be blocked (403)")
        
        hdrs = {
            "User-Agent": "hermes-fdc/1.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            hdrs.update(headers)
        
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="FDC Polymarket VPN & Proxy Manager")
    parser.add_argument("--status", action="store_true", help="Check VPN status")
    parser.add_argument("--connect", action="store_true", help="Connect to optimal VPN server")
    parser.add_argument("--disconnect", action="store_true", help="Disconnect VPN")
    parser.add_argument("--test", action="store_true", help="Test Polymarket API access")
    parser.add_argument("--rotate", action="store_true", help="Rotate to new VPN server")
    parser.add_argument("--country", type=str, default=None, help="Specific country for VPN")
    parser.add_argument("--openvpn-setup", action="store_true", help="Download OpenVPN config files")
    args = parser.parse_args()
    
    if args.status:
        status = nordvpn_status()
        print(json.dumps(status, indent=2))
        if status.get("connected"):
            print(f"\n✅ VPN Connected: {status.get('server', 'unknown')}")
        elif status.get("error"):
            print(f"\n❌ {status['error']}")
        else:
            print(f"\n⚠️  VPN Not Connected")
    
    elif args.connect:
        country = args.country or PREFERRED_COUNTRIES[0]
        print(f"🔌 Connecting to NordVPN ({country})...")
        result = nordvpn_connect(country)
        print(json.dumps(result, indent=2, default=str))
    
    elif args.disconnect:
        print("📴 Disconnecting VPN...")
        result = nordvpn_disconnect()
        print(json.dumps(result, indent=2, default=str))
    
    elif args.test:
        print("🧪 Testing Polymarket API access...")
        result = test_polymarket_access()
        print(json.dumps(result, indent=2, default=str))
        if result["success"]:
            print("\n✅ Polymarket GET & POST accessible!")
        else:
            print(f"\n❌ Polymarket access blocked:")
            for err in result.get("errors", []):
                print(f"   {err}")
    
    elif args.rotate:
        print("🔄 Rotating VPN server...")
        result = nordvpn_rotate()
        print(json.dumps(result, indent=2, default=str))
    
    elif args.openvpn_setup:
        print("📥 Setting up OpenVPN configs...")
        country = args.country or "ch"
        result = setup_openvpn_config(country)
        print(json.dumps(result, indent=2, default=str))
    
    else:
        parser.print_help()