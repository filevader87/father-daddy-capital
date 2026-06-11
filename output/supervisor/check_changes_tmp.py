import json

# Current state from the reconciler run
current_weather_bankroll = 9.64
current_crypto_bankroll = 70.0
current_crypto_trades = 0
current_classification = 'V21.7.12_SUPERVISOR_STATE_RECONCILED'
current_weather_halted = False
current_crypto_halted = False
current_crypto_live = False
current_weather_live = False
current_scalper_live = False
current_global_live = False

# Last reported state
with open('/home/naq1987s/father-daddy-capital/output/supervisor/.last_reported_state.json') as f:
    last = json.load(f)

print('=== COMPARISON ===')
print(f'Weather bankroll: last={last["weather_bankroll"]}, current={current_weather_bankroll}, delta={current_weather_bankroll - last["weather_bankroll"]:.2f}')
print(f'Crypto bankroll: last={last["crypto_bankroll"]}, current={current_crypto_bankroll}, delta={current_crypto_bankroll - last["crypto_bankroll"]:.2f}')
print(f'Classification: last={last["classification"]}, current={current_classification}')
print(f'Gates: last_live={last["global_live_expansion_allowed"]}, current={current_global_live}')
print(f'Weather halted: last={last["weather_halted"]}, current={current_weather_halted}')
print(f'Crypto halted: last={last["crypto_halted"]}, current={current_crypto_halted}')
print(f'Crypto trades: last={last["crypto_total_trades"]}, current={current_crypto_trades}')
print()

changes = []
if abs(current_weather_bankroll - last['weather_bankroll']) > 1:
    changes.append(f'WEATHER BANKROLL changed from {last["weather_bankroll"]} to {current_weather_bankroll} (delta {current_weather_bankroll - last["weather_bankroll"]:.2f})')
if current_weather_bankroll < 10:
    changes.append(f'WEATHER BANKROLL below $10: ${current_weather_bankroll}')
if abs(current_crypto_bankroll - last['crypto_bankroll']) > 1:
    changes.append(f'CRYPTO BANKROLL changed from {last["crypto_bankroll"]} to {current_crypto_bankroll}')
if current_classification != last['classification']:
    changes.append(f'CLASSIFICATION changed from {last["classification"]} to {current_classification}')
if current_crypto_trades != last.get('crypto_total_trades', 0):
    changes.append(f'CRYPTO TRADES changed from {last.get("crypto_total_trades", 0)} to {current_crypto_trades}')
if current_weather_halted != last['weather_halted']:
    changes.append(f'WEATHER HALTED changed to {current_weather_halted}')
if current_crypto_halted != last['crypto_halted']:
    changes.append(f'CRYPTO HALTED changed to {current_crypto_halted}')

# Gate changes (only blocked->allowed)
gate_map = {
    'crypto_live_real_allowed': current_crypto_live,
    'weather_live_allowed': current_weather_live,
    'scalper_micro_live_allowed': current_scalper_live,
    'global_live_expansion_allowed': current_global_live,
}
for gate, current_val in gate_map.items():
    if gate in last and not last[gate] and current_val:
        changes.append(f'GATE {gate} changed from blocked to allowed')

if changes:
    print('CHANGES DETECTED:')
    for c in changes:
        print(f'  - {c}')
else:
    print('NO CHANGES')