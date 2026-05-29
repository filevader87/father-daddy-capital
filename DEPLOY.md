# FDC V19.7 Dashboard — Netlify Deployment Guide

## Quick Deploy (5 minutes)

### Option A: Static Netlify Site + API Proxy

1. **Create repo directory**:
```bash
mkdir fdc-dashboard && cd fdc-dashboard
```

2. **Download the dashboard HTML**:
The file `dashboard.html` is self-contained (all CSS/JS inline, Chart.js from CDN).

3. **netlify.toml** — Create this file:
```toml
[build]
  publish = "."

[[redirects]]
  from = "/api/*"
  to = "http://YOUR_SERVER_IP:8197/api/:splat"
  status = 200
  force = true

[[headers]]
  for = "/*"
  [headers.values]
    X-Frame-Options = "DENY"
    X-Content-Type-Options = "nosniff"
```

4. **Deploy**:
```bash
netlify deploy --prod --dir=.
```

### Option B: Netlify Functions (no external server needed)

If you can't expose port 8197 publicly, use Netlify Functions:

1. Create `netlify/functions/state.js`:
```javascript
exports.handler = async (event) => {
  const https = require('https');
  const http = require('http');
  
  return new Promise((resolve) => {
    http.get('http://YOUR_SERVER_IP:8197/api/state', (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        resolve({
          statusCode: 200,
          headers: {
            'Access-Control-Allow-Origin': '*',
            'Content-Type': 'application/json'
          },
          body: data
        });
      });
    }).on('error', (err) => {
      resolve({
        statusCode: 500,
        body: JSON.stringify({ error: err.message })
      });
    });
  });
};
```

2. Update `dashboard.html` API endpoint:
```javascript
// Change line ~210 in dashboard.html:
const API = '/api/state';
// Remove the port-based URL
```

3. Deploy with `netlify deploy --prod`.

## What You're Deploying

- **File**: `dashboard.html` — Single self-contained HTML file
- **Features**:
  - Dark trading theme (#0a0e17 bg, #00d4aa accent)
  - Bankroll card with P&L, ROI, peak tracking
  - Win rate card (W/L, WR%)
  - Drawdown card with circuit breaker status (green/yellow/red)
  - Live bankroll chart (Chart.js, last 100 points)
  - Open positions tracker (side, price, bet, RSI, EV)
  - Trade history (last 20, P&L colored green/red)
  - Risk config display (cold/warm/proven tiers, DD levels)
  - Signal log
  - Auto-refreshes every 30 seconds

- **API Requirements**: The dashboard fetches from `/api/state` which serves `pm_state.json`

## Current Setup (for reference)

- Dashboard API running on port 8197 (`fdc_dashboard_api.py`)
- Paper trading daemon active (5min scan interval)
- Watchdog checking every 15min
- State file: `output/pm_state.json`
- Bankroll: $320, Mode: PAPER

## Contact

Hugh (3rd of 5) built this. Questions → ping Father Daddy.