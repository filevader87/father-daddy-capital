#!/usr/bin/env python3
"""
FDC Dashboard API — serves pm_state.json and dashboard.html on port 8197.
Run: python3 fdc_dashboard_api.py
"""
import json, os, time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

REPO = Path(__file__).parent
STATE = REPO / "output" / "pm_state.json"
DASHBOARD = REPO / "dashboard.html"
PORT = 8197

class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/state':
            self.serve_state()
        elif self.path == '/api/status':
            self.serve_status()
        elif self.path == '/' or self.path == '/dashboard.html':
            self.serve_dashboard()
        else:
            self.send_error(404)

    def serve_state(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        if STATE.exists():
            self.wfile.write(STATE.read_bytes())
        else:
            self.wfile.write(json.dumps({"bankroll": 320, "total_pnl": 0, "wins": 0, "losses": 0, "positions": {}, "journal": [], "scans": 0, "daily_pnl": 0, "mode": "paper", "version": "V19.7"}).encode())

    def serve_status(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            state = json.loads(STATE.read_text()) if STATE.exists() else {}
            br = state.get('bankroll', 0)
            w = state.get('wins', 0)
            l = state.get('losses', 0)
            self.wfile.write(json.dumps({
                "bankroll": br, "pnl": state.get('total_pnl', 0),
                "wins": w, "losses": l, "wr": w/max(w+l,1)*100,
                "mode": state.get('mode', 'paper'), "scans": state.get('scans', 0),
                "positions": len([p for p in state.get('positions',{}).values() if p.get('status')=='open']),
            }).encode())
        except Exception as e:
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def serve_dashboard(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        if DASHBOARD.exists():
            self.wfile.write(DASHBOARD.read_bytes())
        else:
            self.wfile.write(b'<h1>Dashboard not found</h1>')

    def log_message(self, format, *args):
        ts = time.strftime('%H:%M:%S')
        print(f"[{ts}] {args[0] if args else ''}")

if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', PORT), DashboardHandler)
    print(f"FDC Dashboard running on http://localhost:{PORT}")
    print(f"API: http://localhost:{PORT}/api/state")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.server_close()