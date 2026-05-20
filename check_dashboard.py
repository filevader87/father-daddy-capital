#!/usr/bin/env python3
import requests
try:
    r = requests.get('http://localhost:8645/', timeout=5)
    print(f'Dashboard status: {r.status_code}')
except Exception as e:
    print(f'Dashboard unreachable: {e}')