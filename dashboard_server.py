#!/usr/bin/env python3
"""
FDC Dashboard HTTP Server
==========================
Serves the live trading dashboard and metrics as HTTP endpoints.
Reads files produced by MonitoringPipeline.

Endpoints:
  GET /            — Dashboard (plain text)
  GET /json        — Dashboard + metrics as JSON
  GET /alerts      — Active alerts as JSON
  GET /health      — Health check (200 if pipeline recent)

Author: Hugh (3rd of 5)
Date: 2026-05-16
"""

import json, time, os
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

OUT_DIR = Path("/mnt/c/Users/12035/father_daddy_capital/output")
DASHBOARD = OUT_DIR / "dashboard.txt"
ALERTS = OUT_DIR / "alerts.json"
METRICS = OUT_DIR / "metrics.json"
HOST = "0.0.0.0"
PORT = 8645          # Dedicated dashboard port
MAX_AGE_SECONDS = 300  # Stale if older than 5 min

class DashboardHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/":
            self._serve_dashboard()
        elif self.path == "/json":
            self._serve_json()
        elif self.path == "/alerts":
            self._serve_alerts()
        elif self.path == "/health":
            self._serve_health()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write("404 — / /json /alerts /health\n".encode())

    def log_message(self, format, *args):
        pass  # Silent

    def _serve_dashboard(self):
        if DASHBOARD.exists():
            content = DASHBOARD.read_text()
            age = time.time() - DASHBOARD.stat().st_mtime
            if age > MAX_AGE_SECONDS:
                content += f"\n\n⚠ Dashboard stale: {age:.0f}s old\n"
        else:
            content = "Dashboard not yet generated.\n"
        self._respond(200, content, "text/plain; charset=utf-8")

    def _serve_json(self):
        data = {
            "ts": time.time(),
            "dashboard": DASHBOARD.read_text() if DASHBOARD.exists() else "",
            "alerts": json.loads(ALERTS.read_text()) if ALERTS.exists() else {},
        }
        if DASHBOARD.exists():
            data["dashboard_age_s"] = round(time.time() - DASHBOARD.stat().st_mtime, 1)
            data["stale"] = data["dashboard_age_s"] > MAX_AGE_SECONDS

        self._respond(200, json.dumps(data, indent=2), "application/json")

    def _serve_alerts(self):
        alerts = {"alerts": [], "active": 0}
        if ALERTS.exists():
            alerts = json.loads(ALERTS.read_text())
        self._respond(200, json.dumps(alerts, indent=2), "application/json")

    def _serve_health(self):
        ok = False
        age = None
        if DASHBOARD.exists():
            age = time.time() - DASHBOARD.stat().st_mtime
            ok = age < MAX_AGE_SECONDS
        status = 200 if ok else 503
        body = json.dumps({
            "healthy": ok,
            "dashboard_age_s": round(age, 1) if age else None,
            "max_age_s": MAX_AGE_SECONDS,
        })
        self._respond(status, body, "application/json")

    def _respond(self, status: int, body: str, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), DashboardHandler)
    print(f"📊 FDC Dashboard → http://{HOST}:{PORT}")
    print(f"   Endpoints: / /json /alerts /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
