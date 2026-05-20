#!/usr/bin/env python3
import json
import os

base = "/mnt/c/Users/12035/father_daddy_capital"

# Check dashboard data freshness
dash_path = os.path.join(base, "data/dashboard.json")
if os.path.exists(dash_path):
    with open(dash_path) as f:
        dash = json.load(f)
    print("Dashboard timestamp:", dash.get("last_updated", "N/A"))
    print("Portfolio value:", dash.get("portfolio_value", "N/A"))
    print("Top holdings:", len(dash.get("holdings", [])))
else:
    print("dashboard.json not found")

# Check audit trail
audit_path = os.path.join(base, "data/audit_trail.json")
if os.path.exists(audit_path):
    with open(audit_path) as f:
        audit = json.load(f)
    print("Audit entries:", len(audit) if isinstance(audit, list) else "N/A")
else:
    print("audit_trail.json not found")

# Check alerts
alerts_path = os.path.join(base, "data/alerts.json")
if os.path.exists(alerts_path):
    with open(alerts_path) as f:
        alerts = json.load(f)
    print("Active alerts:", len(alerts) if isinstance(alerts, list) else "N/A")
else:
    print("alerts.json not found")