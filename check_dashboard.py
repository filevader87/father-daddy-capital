#!/usr/bin/env python3
import requests
r = requests.get('http://localhost:8645')
print(r.status_code)