import json

# Check dashboard data
try:
    with open('dashboard_data.json') as f:
        data = json.load(f)
    print(f'Dashboard data: {len(data)} sections refreshed')
    for k in data:
        v = data[k]
        if isinstance(v, list):
            print(f'  {k}: {len(v)} items')
        elif isinstance(v, dict):
            print(f'  {k}: {len(v)} keys')
        else:
            print(f'  {k}: {v}')
except Exception as e:
    print(f'Dashboard data: {e}')

# Check audit log
try:
    with open('audit_log.json') as f:
        audit = json.load(f)
    print(f'Audit log: {len(audit)} events')
    if audit:
        print(f'  Latest: {audit[-1].get("event","?")} at {audit[-1].get("timestamp","?")}')
except Exception as e:
    print(f'Audit log: {e}')

# Check alerts
try:
    with open('alerts.json') as f:
        alerts = json.load(f)
    print(f'Alerts: {len(alerts)} active')
except Exception as e:
    print(f'Alerts: {e}')