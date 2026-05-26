#!/usr/bin/env python3
"""Check the dashboard server health."""
import json, urllib.request

try:
    resp = urllib.request.urlopen('http://localhost:8645/', timeout=5)
    print(f'Dashboard status: HTTP {resp.status}')
except Exception as e:
    print(f'Dashboard check: {e}')

try:
    resp2 = urllib.request.urlopen('http://localhost:8645/api/status', timeout=5)
    print(resp2.read().decode()[:1000])
except Exception as e2:
    print(f'/api/status: {e2}')