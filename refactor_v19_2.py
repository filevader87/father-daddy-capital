#!/usr/bin/env python3
"""Refactor paper_trade_v19_2.py to extract scan_single_asset function."""

with open('/mnt/c/Users/12035/father_daddy_capital/paper_trade_v19_2.py') as f:
    lines = f.readlines()

# Find key line indices (0-based)
run_scan_idx = None
rsi_idx = None
main_idx = None

for i, line in enumerate(lines):
    if 'def run_scan():' in line:
        run_scan_idx = i
    if '# RSI zone label' in line and i > 700:
        rsi_idx = i
    if 'def main_loop():' in line:
        main_idx = i

print(f"run_scan at {run_scan_idx+1}, RSI zone at {rsi_idx+1}, main_loop at {main_idx+1}")

# Extract the body from RSI zone to main_loop
body = lines[rsi_idx:main_idx]

# Indent body by 4 more spaces (inside the for loop then scan_single_asset)
indented_body = []
for line in body:
    if line.strip():
        indented_body.append('    ' + line)
    else:
        indented_body.append(line)

# Build scan_single_asset function
scan_func = [
    '\n\ndef scan_single_asset(asset: str, state: dict, journal) -> None:\n',
    '    """Scan a single asset (BTC/ETH/SOL/XRP) for trading signals."""\n',
    '    # Fetch candles and compute indicators for this asset\n',
    '    candles = fetch_candles_multi(asset, \'5m\', 100)\n',
    '    if not candles:\n',
    '        log(f"  ⚠️ Could not fetch {asset} candles, skipping")\n',
    '        return\n',
    '\n',
    '    prices = [c[\'close\'] for c in candles]\n',
    '    log(f"  {asset}: ${prices[-1]:,.4f} | {len(candles)} candles")\n',
    '\n',
] + indented_body + ['\n\n']

# Build new run_scan function (simplified)
new_run_scan = [
    'def run_scan():\n',
    '    """Scan all configured assets for trading signals."""\n',
    '    journal = TradeJournal()\n',
    '    resolve_positions()\n',
    '    state = load_state()\n',
    '\n',
    '    # Check daily loss limit\n',
    '    daily_reset = state.get("daily_reset", "")\n',
    '    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")\n',
    '    if daily_reset != today:\n',
    '        state["daily_losses"] = 0\n',
    '        state["daily_loss_amount"] = 0.0\n',
    '        state["daily_trades"] = 0\n',
    '        state["daily_reset"] = today\n',
    '        save_state(state)\n',
    '\n',
    '    # Check daily loss limit (V19.1: increased from $20 to $28.73)\n',
    '    if state.get("daily_loss_amount", 0) >= DAILY_LOSS_LIMIT:\n',
    '        log(f"  🛑 Daily loss limit reached: ${state.get(\'daily_loss_amount\', 0):.2f} >= ${DAILY_LOSS_LIMIT}")\n',
    '        state["last_scan"] = datetime.now(timezone.utc).isoformat()\n',
    '        save_state(state)\n',
    '        return\n',
    '\n',
    '    # Kill switch\n',
    '    if state.get("bankroll", 0) < 5.0:\n',
    '        log(f"  🛑 Kill switch: bankroll ${state.get(\'bankroll\', 0):.2f} < minimum")\n',
    '        state["last_scan"] = datetime.now(timezone.utc).isoformat()\n',
    '        save_state(state)\n',
    '        return\n',
    '\n',
    '    # V19.2: Scan all assets\n',
    '    assets_to_scan = ["BTC", "ETH", "SOL", "XRP"]\n',
    '    for asset in assets_to_scan:\n',
    '        scan_single_asset(asset, state, journal)\n',
    '\n',
    '\n',
]

# Assemble the new file
# Part 1: Everything before run_scan
part1 = lines[:run_scan_idx]
# Part 2: scan_single_asset function
part2 = scan_func
# Part 3: new run_scan
part3 = new_run_scan
# Part 4: main_loop and everything after
part4 = lines[main_idx:]

new_file = part1 + part2 + part3 + part4

with open('/mnt/c/Users/12035/father_daddy_capital/paper_trade_v19_2.py', 'w') as f:
    f.writelines(new_file)

print(f"Written {len(new_file)} lines")
print("Done!")