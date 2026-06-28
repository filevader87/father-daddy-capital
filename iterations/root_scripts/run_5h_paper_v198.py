#!/usr/bin/env python3
"""V19.8 5-Hour Paper Loop — reads state from state_CORE_UP.json, not cycle_results."""
import sys, os, json, time, traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/mnt/c/Users/12035/father_daddy_capital')
from paper_trader_v19_8 import run_paper_cycle

DURATION = 18000  # 5 hours
CYCLE_INTERVAL = 20
STATE_FILE = Path('/mnt/c/Users/12035/father_daddy_capital/paper_trading/state_CORE_UP.json')

def read_state():
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}

start = time.time()
cycle = 0
errors = 0
last_summary = start

print(f'=== V19.8 5-HOUR PAPER LOOP ===')
print(f'Start: {datetime.now(timezone.utc).isoformat()}')
print(f'Duration: {DURATION}s, Interval: {CYCLE_INTERVAL}s')
print(f'Live orders: DISABLED')
print(f'Mode: PAPER')
print()

while (time.time() - start) < DURATION:
    cycle += 1
    cycle_start = time.time()
    try:
        run_paper_cycle()
    except Exception as ex:
        errors += 1
        print(f'  CYCLE {cycle} ERROR: {ex}')
        traceback.print_exc()
        if errors > 10:
            print('FATAL: >10 errors, aborting')
            break

    elapsed = time.time() - cycle_start
    remaining = max(CYCLE_INTERVAL - elapsed, 2)

    now = time.time()
    if now - last_summary >= 300 or cycle == 1:
        s = read_state()
        if s:
            rsi_prices = {}
            # Try to get current RSI from latest cycle file
            try:
                cyc_files = sorted(Path('/mnt/c/Users/12035/father_daddy_capital/paper_trading').glob('cycle_*.json'))
                if cyc_files:
                    with open(cyc_files[-1]) as f:
                        cyc = json.load(f)
                    prices = cyc.get('prices', {})
                    for ak in ['BTC', 'ETH', 'SOL', 'XRP']:
                        rsi_prices[ak] = prices.get(ak, {}).get('rsi', '?')
            except:
                pass

            print(f'  C{cycle}: exec={s.get("executable_opportunities",0)} '
                  f'blk={s.get("blocked_trade_candidates",0)} '
                  f'fa_d={s.get("blocked_by_false_dislocation",0)} '
                  f'dorm={s.get("blocked_by_dormant_longshot",0)} '
                  f'mkt={s.get("unique_markets_seen",0)} '
                  f'book={s.get("book_checks_attempted",0)} '
                  f'RSI={rsi_prices} '
                  f'err={errors}')
        last_summary = now

    time.sleep(remaining)

# Final report
elapsed_total = time.time() - start
s = read_state()
print()
print(f'=== 5-HOUR PAPER LOOP COMPLETE ===')
print(f'Cycles: {cycle}, Errors: {errors}, Runtime: {elapsed_total/60:.1f} min')
if s:
    lat = s.get('cycle_latency', {})
    bd = lat.get('breakdown', {})
    ph = s.get('market_phases_seen', {})
    lr = s.get('liquidity_report', {})
    mk = s.get('maker_diagnostics', [])
    dev = s.get('diagnostic_ev', [])

    print(f'executable_opportunities: {s.get("executable_opportunities", 0)}')
    print(f'paper_trades_opened: {s.get("paper_trades_opened", 0)}')
    print(f'paper_trades_resolved: {s.get("paper_trades_resolved", 0)}')
    print(f'blocked_trade_candidates: {s.get("blocked_trade_candidates", 0)}')
    print(f'blocked_by_false_dislocation: {s.get("blocked_by_false_dislocation", 0)}')
    print(f'blocked_by_bad_market_phase: {s.get("blocked_by_bad_market_phase", 0)}')
    print(f'blocked_by_dormant_longshot: {s.get("blocked_by_dormant_longshot", 0)}')
    print(f'blocked_by_nearly_decided: {s.get("blocked_by_nearly_decided", 0)}')
    print(f'unique_markets_seen: {s.get("unique_markets_seen", 0)}')
    print(f'market_phases_seen: {ph}')
    print(f'book_checks_attempted: {s.get("book_checks_attempted", 0)}')
    print(f'book_checks_executable: {s.get("book_checks_executable", 0)}')
    print(f'avg_cycle_latency: {lat.get("avg_cycle_duration",0):.1f}s')
    print(f'discovery_latency: {bd.get("discovery",0):.2f}s')
    print(f'ccxt_latency: {bd.get("ccxt_fetch",0):.2f}s')
    print(f'liquidity_report: {list(lr.keys())[:8] if lr else "empty"}')
    print(f'maker_diagnostics: {len(mk)} entries')
    print(f'diagnostic_ev: {len(dev)} entries')
    print(f'token_states_seen: {s.get("token_states_seen",{})}')

    # Classification
    if errors > 0:
        cls = 'A_RUNTIME_FAILURE'
    elif s.get('executable_opportunities', 0) >= 10:
        cls = 'A_COLLECTING_EXECUTABLE_OPPORTUNITIES'
    elif s.get('blocked_trade_candidates', 0) > 0 or s.get('unique_markets_seen', 0) > 0:
        cls = 'A_COLLECTING_MARKET_AND_SIGNAL_DATA'
    elif s.get('paper_trades_opened', 0) > 0:
        cls = 'B_PAPER_VALIDATED'
    else:
        cls = 'A_MARKET_DISCOVERY_ONLY'

    print(f'Classification: {cls}')

    out_path = Path('/mnt/c/Users/12035/father_daddy_capital/paper_trading/5h_loop_state.json')
    with open(out_path, 'w') as f:
        json.dump(s, f, indent=2, default=str)
    print(f'State saved: {out_path}')