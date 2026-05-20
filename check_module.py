#!/usr/bin/env python3
import monitoring, inspect, os

# Find where monitoring stores its data
src = inspect.getfile(monitoring)
print('Module file:', src)
mp = monitoring.MonitoringPipeline
methods = [m for m in dir(mp) if not m.startswith('_')]
print('Methods:', methods)