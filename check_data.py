#!/usr/bin/env python3
"""Check data file timestamps."""
import os
from datetime import datetime

data_dir = os.path.join(os.path.dirname(__file__), 'data')
for f in ['dashboard.json', 'audit_trail.json', 'monitoring_state.json']:
    fp = os.path.join(data_dir, f)
    if os.path.exists(fp):
        mtime = os.path.getmtime(fp)
        print(f'{f}: updated {datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")}')
    else:
        print(f'{f}: not found at {fp}')