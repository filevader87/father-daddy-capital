#!/usr/bin/env python3
"""V21.7.23 Canary Watcher Launcher — runs the watcher with unbuffered output."""
import sys
import os

# Force unbuffered output
os.environ['PYTHONUNBUFFERED'] = '1'

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from v21723_btc15m_canary_watcher import run_watcher

if __name__ == "__main__":
    run_watcher()