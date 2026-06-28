#!/usr/bin/env python3
import json, urllib.request

# Fetch dashboard JSON
req = urllib.request.urlopen("http://localhost:8645/json")
data = json.loads(req.read().decode())

print(f"Dashboard timestamp: {data.get('ts', 'N/A')}")
dashboard_text = data.get('dashboard', '')
# Print just the first few readable lines
for line in dashboard_text.split('\n')[:15]:
    print(line)

# Fetch alerts
req2 = urllib.request.urlopen("http://localhost:8645/alerts")
alerts = json.loads(req2.read().decode())
print(f"\nAlerts: active={alerts.get('active_alerts',0)}, total_fired={alerts.get('total_fired',0)}, trading_paused={alerts.get('trading_paused',False)}")