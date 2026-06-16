import json, os

prev_path = 'output/supervisor/v21712_supervisor_state_report.prev.json'
curr_path = 'output/supervisor/v21712_supervisor_state_report.json'

prev = json.load(open(prev_path))
curr = json.load(open(curr_path))

changes = []

# classification
if prev['classification'] != curr['classification']:
    changes.append(f'CLASSIFICATION: {prev["classification"]} -> {curr["classification"]}')

# expansion gates - find prev gate file
prev_gate_path = None
for p in ['output/supervisor/v21712_live_expansion_gate.prev.json',
          'output/supervisor/.prev_v21712_live_expansion_gate.json']:
    if os.path.exists(p):
        prev_gate_path = p
        break

if prev_gate_path:
    prev_gates = json.load(open(prev_gate_path))
    curr_gates = json.load(open('output/supervisor/v21712_live_expansion_gate.json'))
    gate_keys = ['crypto_live_real_allowed', 'weather_live_allowed', 'scalper_micro_live_allowed', 'swarm_allowed', 'global_live_expansion_allowed']
    for k in gate_keys:
        if k in prev_gates and k in curr_gates:
            if prev_gates[k] != curr_gates[k]:
                changes.append(f'GATE {k}: {prev_gates[k]} -> {curr_gates[k]}')

# per-cell halted
for cell in ['crypto', 'weather', 'scalper']:
    if cell in prev and cell in curr:
        if prev[cell].get('halted') != curr[cell].get('halted'):
            changes.append(f'{cell} HALTED: {prev[cell].get("halted")} -> {curr[cell].get("halted")}')

if changes:
    for c in changes:
        print(c)
else:
    print('NO_CHANGES')