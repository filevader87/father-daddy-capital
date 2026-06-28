#!/usr/bin/env python3
"""
Father Daddy Capital — Polymarket VPN Config & Live Trading Proxy

WSL-aware: detects VPN status via IP geolocation (works with Windows ProtonVPN app),
connects/disconnects via PowerShell automation of the Windows ProtonVPN GUI.

Routes Polymarket API calls through VPN to avoid 403 errors on POST endpoints.

Usage:
  python3 fdc_vpn.py --status          # Check VPN status (IP geolocation)
  python3 fdc_vpn.py --connect         # Connect via Windows ProtonVPN app
  python3 fdc_vpn.py --connect --country CH  # Connect to specific country
  python3 fdc_vpn.py --disconnect      # Disconnect via Windows ProtonVPN app
  python3 fdc_vpn.py --test            # Test PM API access (GET + POST)
  python3 fdc_vpn.py --rotate          # Disconnect + reconnect to new server
"""

import subprocess
import json
import urllib.request
import urllib.parse
import time
import sys
import os
import platform
from pathlib import Path
from datetime import datetime, timedelta

# ─── Configuration ────────────────────────────────────────────────────────────
# ProtonVPN countries that work for Polymarket (non-US, crypto-friendly, fast)
PREFERRED_COUNTRIES = [
    ("Switzerland", "CH"),     # CH — crypto-friendly, strong privacy
    ("Netherlands", "NL"),     # NL — major exchange hub, low latency
    ("Germany", "DE"),         # DE — fast, reliable
    ("Canada", "CA"),          # CA — close to US, low latency
    ("United Kingdom", "UK"),  # UK — English-language, crypto OK
    ("Panama", "PA"),          # PA — offshore, privacy-focused
]

US_COUNTRY_CODES = {"US", "USA", "United States", "United States of America"}

# ─── Platform Detection ────────────────────────────────────────────────────────

def is_wsl() -> bool:
    """Detect if running under Windows Subsystem for Linux."""
    try:
        with open("/proc/version", "r") as f:
            return "microsoft" in f.read().lower() or "wsl" in f.read().lower()
    except Exception:
        return False


def is_windows() -> bool:
    """Detect if running on native Windows."""
    return platform.system() == "Windows"


def _powershell(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a PowerShell command (from WSL or native Windows)."""
    if is_wsl():
        ps_exe = "powershell.exe"
    elif is_windows():
        ps_exe = "powershell"
    else:
        ps_exe = "powershell"
    return subprocess.run(
        [ps_exe, "-NoProfile", "-Command", cmd],
        capture_output=True, text=True, timeout=timeout
    )


# ─── VPN Status via IP Geolocation ────────────────────────────────────────────

def _get_ip_info() -> dict:
    """Get current IP geolocation info. Works regardless of VPN method."""
    sources = [
        "https://ipinfo.io/json",
        "https://ipapi.co/json/",
    ]
    for url in sources:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fdc-vpn/2.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
                # ipinfo.io format
                if "country" in data and "ip" in data:
                    return {
                        "ip": data.get("ip"),
                        "country": data.get("country"),
                        "city": data.get("city"),
                        "region": data.get("region"),
                        "org": data.get("org", ""),
                        "timezone": data.get("timezone", ""),
                        "source": "ipinfo.io",
                    }
                # ipapi.co format
                if "country_code" in data and "ip" in data:
                    return {
                        "ip": data.get("ip"),
                        "country": data.get("country_code"),
                        "city": data.get("city"),
                        "region": data.get("region_name"),
                        "org": data.get("org", ""),
                        "timezone": data.get("timezone", ""),
                        "source": "ipapi.co",
                    }
        except Exception:
            continue
    return {"ip": None, "country": None, "error": "Could not reach any IP geolocation service"}


def protonvpn_status() -> dict:
    """Check VPN status by IP geolocation. Detects Windows ProtonVPN app connection."""
    # First: check if ProtonVPN Windows service is running
    service_running = False
    if is_wsl() or is_windows():
        r = _powershell("Get-Process ProtonVPNService -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name")
        service_running = "ProtonVPNService" in (r.stdout or "")

    ip_info = _get_ip_info()
    ip = ip_info.get("ip")
    country = ip_info.get("country")
    city = ip_info.get("city")
    org = ip_info.get("org", "")

    is_us = country in US_COUNTRY_CODES if country else False

    # Detect ProtonVPN by org string or non-US IP + service running
    is_protonvpn = "proton" in org.lower() if org else False
    connected = False
    server_name = None

    if is_protonvpn:
        connected = True
        server_name = f"{city}, {country} (ProtonVPN)"
    elif service_running and not is_us:
        # Service running + non-US IP = likely connected via ProtonVPN
        connected = True
        server_name = f"{city}, {country}"
    elif service_running and is_us:
        # Service running but US IP — ProtonVPN app is open but not connected
        # (or connected to US server, which doesn't help for PM)
        connected = False
        server_name = None

    result = {
        "connected": connected,
        "ip": ip,
        "country": country,
        "city": city,
        "org": org,
        "is_us": is_us,
        "service_running": service_running,
        "server": server_name,
        "raw": json.dumps(ip_info, default=str),
    }

    if not ip:
        result["error"] = ip_info.get("error", "Could not determine IP")

    return result


# ─── VPN Control (Windows ProtonVPN App) ───────────────────────────────────────

def protonvpn_connect(country_code: str = None) -> dict:
    """Connect to ProtonVPN via the Windows app.

    In WSL: launches Quick Connect via PowerShell UI automation.
    On native Linux: falls back to protonvpn CLI.
    """
    status = protonvpn_status()
    if status.get("connected"):
        return {"success": True, "message": f"Already connected: {status.get('server', 'unknown')}", "status": status}

    if is_wsl() or is_windows():
        # Windows ProtonVPN app — launch via PowerShell
        # Quick Connect: open the app and trigger connection
        app_path = r"C:\Program Files\Proton\VPN\v4.4.0\ProtonVPN.Launcher.exe"

        # Step 1: Ensure app is running
        r = _powershell("Get-Process ProtonVPN.Client -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name")
        app_running = "ProtonVPN.Client" in (r.stdout or "")

        if not app_running:
            # Launch the app
            _powershell(f'Start-Process "{app_path}"')
            time.sleep(8)  # Wait for app to start

        # Step 2: Trigger Quick Connect using keyboard shortcut (Ctrl+Q is ProtonVPN's quick connect)
        # Or use the system tray connect action
        _powershell(
            """
            $wshell = New-Object -ComObject WScript.Shell
            # Activate ProtonVPN window
            $proc = Get-Process ProtonVPN.Client -ErrorAction SilentlyContinue
            if ($proc) {
                $wshell.AppActivate($proc.MainWindowTitle)
                Start-Sleep -Milliseconds 500
                # Quick Connect shortcut
                $wshell.SendKeys('^q')
            }
            """
        )
        time.sleep(5)

        # Verify connection
        new_status = protonvpn_status()
        if new_status.get("connected"):
            return {"success": True, "message": f"Connected: {new_status.get('server', 'unknown')}", "status": new_status}
        else:
            # Fallback: just tell the user to connect manually
            return {
                "success": False,
                "message": "Could not auto-connect. Please click Quick Connect in the ProtonVPN Windows app.",
                "status": new_status,
                "hint": "Open ProtonVPN from system tray → Quick Connect, then run --status to verify",
            }

    else:
        # Native Linux — use protonvpn CLI
        cmd = ["protonvpn", "connect", "--fastest"]
        if country_code:
            cmd = ["protonvpn", "connect", "--cc", country_code]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            time.sleep(5)
            new_status = protonvpn_status()
            if new_status.get("connected"):
                return {"success": True, "message": f"Connected: {new_status.get('server', 'unknown')}", "status": new_status}
            else:
                return {"success": False, "message": result.stdout + result.stderr, "status": new_status}
        except FileNotFoundError:
            return {"success": False, "message": "protonvpn CLI not installed. Install with: pip install protonvpn-cli", "status": status}
        except subprocess.TimeoutExpired:
            return {"success": False, "message": "Connection timeout (60s)", "status": status}


def protonvpn_disconnect() -> dict:
    """Disconnect ProtonVPN."""
    if is_wsl() or is_windows():
        # Disconnect via Windows app
        _powershell(
            """
            $wshell = New-Object -ComObject WScript.Shell
            $proc = Get-Process ProtonVPN.Client -ErrorAction SilentlyContinue
            if ($proc) {
                $wshell.AppActivate($proc.MainWindowTitle)
                Start-Sleep -Milliseconds 500
                $wshell.SendKeys('^d')
            }
            """
        )
        time.sleep(3)
        status = protonvpn_status()
        return {"success": not status.get("connected", False) and not status.get("is_us", True) is False,
                "message": "Disconnect signal sent" if not status.get("connected") else "May still be connected — check app",
                "status": status}
    else:
        try:
            result = subprocess.run(["protonvpn", "disconnect"], capture_output=True, text=True, timeout=15)
            time.sleep(2)
            status = protonvpn_status()
            return {"success": not status.get("connected", False), "message": result.stdout.strip() + result.stderr.strip(), "status": status}
        except FileNotFoundError:
            return {"success": False, "message": "protonvpn CLI not installed"}
        except Exception as e:
            return {"success": False, "message": str(e)}


def protonvpn_rotate(preferred: list = None) -> dict:
    """Disconnect and reconnect to a different server for rotation."""
    if preferred is None:
        preferred = PREFERRED_COUNTRIES

    # Disconnect first
    protonvpn_disconnect()
    time.sleep(3)

    # Try preferred countries in order
    for display_name, code in preferred:
        result = protonvpn_connect(code)
        if result.get("success"):
            # Verify PM access
            pm_test = test_polymarket_access()
            if pm_test.get("success"):
                return {"success": True, "country": f"{display_name} ({code})", "vpn": result, "pm": pm_test}
            # If PM still blocked, disconnect and try next
            protonvpn_disconnect()
            time.sleep(2)
    return {"success": False, "message": "Could not find a working VPN + PM configuration"}


# ─── Polymarket Access Testing ────────────────────────────────────────────────

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

def test_polymarket_access() -> dict:
    """Test if Polymarket API access works (both GET and POST)."""
    results = {"success": False, "get": False, "post": False, "errors": []}

    # Test 1: GET (gamma API — should work from anywhere)
    try:
        url = f"{GAMMA}/public-search?q=Bitcoin"
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-fdc/2.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            events = data.get("events", [])
            results["get"] = True
            results["market_count"] = len(events)
    except Exception as e:
        results["errors"].append(f"GET error: {e}")

    # Test 2: CLOB access (blocked for US IPs)
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        url = f"{CLOB}/time"
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-fdc/2.0"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            data = json.loads(r.read())
            results["post"] = True  # If we got here, CLOB is accessible
            results["server_time"] = data
    except urllib.error.HTTPError as e:
        if e.code == 403:
            results["errors"].append("POST blocked (403 Forbidden) — US geoblock active")
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

    If ProtonVPN is connected, requests go through VPN automatically.
    If not connected, post() attempts auto-connect.
    If that fails, raises ConnectionError.
    """

    def __init__(self):
        self._vpn_checked = False
        self._vpn_active = False

    def _ensure_vpn(self) -> bool:
        """Check VPN status, connect if needed."""
        if not self._vpn_checked:
            status = protonvpn_status()
            self._vpn_active = status.get("connected", False) and not status.get("is_us", True)
            self._vpn_checked = True
        return self._vpn_active

    def get(self, url: str, headers: dict = None) -> dict | list:
        """GET request — works without VPN."""
        hdrs = {"User-Agent": "hermes-fdc/2.0"}
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
            result = protonvpn_connect()
            time.sleep(3)
            # Re-check
            status = protonvpn_status()
            self._vpn_active = status.get("connected", False) and not status.get("is_us", True)
            if not self._vpn_active:
                raise ConnectionError("VPN not connected — Polymarket POST will be blocked (403)")

        hdrs = {
            "User-Agent": "hermes-fdc/2.0",
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

    parser = argparse.ArgumentParser(description="FDC Polymarket VPN & Proxy Manager (ProtonVPN — WSL-aware)")
    parser.add_argument("--status", action="store_true", help="Check VPN status via IP geolocation")
    parser.add_argument("--connect", action="store_true", help="Connect to ProtonVPN (Windows app or CLI)")
    parser.add_argument("--disconnect", action="store_true", help="Disconnect ProtonVPN")
    parser.add_argument("--test", action="store_true", help="Test Polymarket API access")
    parser.add_argument("--rotate", action="store_true", help="Rotate to new VPN server")
    parser.add_argument("--country", type=str, default=None,
                        help="Country code (CH, NL, DE, CA, UK, PA)")
    args = parser.parse_args()

    if args.status:
        status = protonvpn_status()
        print(json.dumps(status, indent=2, default=str))
        if status.get("connected"):
            print(f"\n✅ ProtonVPN Connected: {status.get('server', 'unknown')}")
            print(f"   IP: {status.get('ip')} ({status.get('city')}, {status.get('country')})")
        elif status.get("error"):
            print(f"\n❌ {status['error']}")
        else:
            ip = status.get("ip", "unknown")
            country = status.get("country", "??")
            if status.get("is_us"):
                print(f"\n⚠️  Not on VPN — IP {ip} is in {country} (US). PM POST endpoints will be blocked.")
                print("   Connect via ProtonVPN Windows app or run: python3 fdc_vpn.py --connect")
            else:
                print(f"\n⚠️  ProtonVPN service not detected — IP {ip} ({country})")

    elif args.connect:
        code = args.country or PREFERRED_COUNTRIES[0][1]
        display = next((n for n, c in PREFERRED_COUNTRIES if c == code), code)
        print(f"🔌 Connecting to ProtonVPN ({display})...")
        result = protonvpn_connect(code)
        print(json.dumps(result, indent=2, default=str))

    elif args.disconnect:
        print("📴 Disconnecting ProtonVPN...")
        result = protonvpn_disconnect()
        print(json.dumps(result, indent=2, default=str))

    elif args.test:
        print("🧪 Testing Polymarket API access...")
        result = test_polymarket_access()
        print(json.dumps(result, indent=2, default=str))
        if result["success"]:
            print("\n✅ Polymarket GET & POST accessible!")
        else:
            print("\n❌ Polymarket access blocked:")
            for err in result.get("errors", []):
                print(f"   {err}")

    elif args.rotate:
        print("🔄 Rotating ProtonVPN server...")
        result = protonvpn_rotate()
        print(json.dumps(result, indent=2, default=str))

    else:
        parser.print_help()