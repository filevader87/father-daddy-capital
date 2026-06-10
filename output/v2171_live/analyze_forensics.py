import json

# Count forensics entries by state_current
states = {}
vol = 0
for line in open('/home/naq1987s/father-daddy-capital/output/v2171_live/state_gate_forensics.jsonl'):
    d = json.loads(line)
    s = d.get('state_current', 'UNKNOWN')
    states[s] = states.get(s, 0) + 1
    vol += 1
print('Total forensics lines:', vol)
for k, v in sorted(states.items(), key=lambda x: -x[1]):
    print(f'  {k}: {v}')

print()

# Count shadow counterfactual outcomes
resolved_ct = 0
unresolved_ct = 0
wins = 0
losses = 0
total = 0
for line in open('/home/naq1987s/father-daddy-capital/output/v2171_live/shadow_counterfactual_events.jsonl'):
    d = json.loads(line)
    total += 1
    if d.get('resolved'):
        resolved_ct += 1
        if d.get('win') == True:
            wins += 1
        elif d.get('win') == False:
            losses += 1
    else:
        unresolved_ct += 1
print(f'Shadow events: {total} total, {resolved_ct} resolved, {unresolved_ct} unresolved')
print(f'  Shadow wins: {wins}, Shadow losses: {losses}')