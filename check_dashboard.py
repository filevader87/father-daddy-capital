import urllib.request
try:
    r = urllib.request.urlopen('http://localhost:8645/', timeout=5)
    print(f'Dashboard status: {r.status}')
except Exception as e:
    print(f'Dashboard check failed: {e}')