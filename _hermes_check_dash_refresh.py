#!/usr/bin/env python3
"""Check dashboard data file freshness and audit trail."""
import os, datetime, json

os.chdir("/mnt/c/Users/12035/father_daddy_capital")

# Check data dir
data_dir = "data"
if os.path.isdir(data_dir):
    for f in sorted(os.listdir(data_dir)):
        fp = os.path.join(data_dir, f)
        if os.path.isfile(fp):
            sz = os.path.getsize(fp)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {f}: {sz} bytes, modified {mtime}")

# Check audit log
audit_dir = "logs"
if os.path.isdir(audit_dir):
    for f in sorted(os.listdir(audit_dir)):
        fp = os.path.join(audit_dir, f)
        if os.path.isfile(fp):
            sz = os.path.getsize(fp)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {f}: {sz} bytes, modified {mtime}")

# Check dashboard server status
print("\nDashboard server check:")
import urllib.request
try:
    resp = urllib.request.urlopen("http://localhost:8645/", timeout=5)
    print(f"  Dashboard HTTP status: {resp.status}")
except Exception as e:
    print(f"  Dashboard unreachable: {e}")