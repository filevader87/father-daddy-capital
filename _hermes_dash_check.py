#!/usr/bin/env python3
import urllib.request
try:
    r = urllib.request.urlopen('http://localhost:8645/', timeout=5)
    print(f'Dashboard HTTP status: {r.status}')
except Exception as e:
    print(f'Dashboard check failed: {e}')