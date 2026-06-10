#!/usr/bin/env python3
"""Compare current supervisor state report with previous cycle."""
import json, os, shutil

PREV = 'output/supervisor/v21712_supervisor_state_report.prev.json'
CURR = 'output/supervisor/v21712_supervisor_state_report.json'
GATE_PREV = 'output/supervisor/v21712_live_expansion_gate.prev.json'
GATE_CURR = 'output/supervisor/v21712_live_expansion_gate.json'

changes = []

# Classification change
if os.path.exists(PREV):
    p = json.load(open(PREV))
    c = json.load(open(CURR))
    if p.get('classification') != c.get('classification'):
        changes.append(f"CLASSIFICATION_CHANGED: {p.get('classification')} -> {c.get('classification')}")
else:
    changes.append("NO_PREV_STATE_REPORT (first cycle)")

# Gate changes
if os.path.exists(GATE_PREV):
    gp = json.load(open(GATE_PREV))
    gc = json.load(open(GATE_CURR))
    gate_keys = ['crypto_live_real_allowed', 'weather_live_allowed', 'scalper_micro_live_allowed', 'swarm_allowed', 'global_live_expansion_allowed']
    for k in gate_keys:
        if gp.get(k) != gc.get(k):
            changes.append(f"GATE_CHANGED: {k}: {gp.get(k)} -> {gc.get(k)}")
else:
    changes.append("NO_PREV_GATE_FILE (first cycle)")

# Save current as prev
shutil.copy(CURR, PREV)
shutil.copy(GATE_CURR, GATE_PREV)

if changes:
    for ch in changes:
        print(ch)
else:
    print("NO_CHANGES")