#!/usr/bin/env python3
"""Check if dashboard server is running at localhost:8645."""
import urllib.request
import urllib.error

try:
    r = urllib.request.urlopen('http://localhost:8645/', timeout=5)
    print(f"Dashboard status: {r.status}")
    content = r.read()
    print(f"Content length: {len(content)} bytes")
except urllib.error.URLError as e:
    print(f"Dashboard unreachable: {e.reason}")
except Exception as e:
    print(f"Dashboard error: {e}")