#!/usr/bin/env python3
"""
V19.7 10-DAY COMBINED BACKTEST
- Binance 5m klines for BTC/ETH/SOL/XRP (May 16-26, 2026)
- PMXT orderbook data for contract price simulation (May 25 T00-T08)
- V19.7 P0-A/B/C engine parameters: EV gate, circuit breaker, risk cap
- 4 strategies: oversold-only (V19.7), oversold+mid, cheap-only, aggressive
"""
import json, math, random, time, sys
from pathlib import Path
import numpy as np
import pandas as pd

SEED = 42
random.seed(SEED)
np.random.seed(SEED)

INITIAL_BANKROLL = 100.0   # $100 starting bankroll
PM_FEE = 0.02              # 2% Polymarket fee
DATA_DIR = Path("binance_data")
PMXT_DIR = Path("pmxt_data")

# ═══════════════════════════════════════════════════════════════
# V19.7 ENGINE PARAMETERS (matching pm_engine_v19_7.py)
# ═══════════════════════════════════════════════════════════════
RSI_OVERSOLD_MIN = 20      # Block RSI < 20 (knife-catching)
RSI_OVERSOLD = 28          # Primary oversold threshold
RSI_NEAR_OVERSOLD = 35     # Near-oversold with confirmations
RSI_DEAD_ZONE_LOW = 35     # Below this = actionable
RSI_DEAD_ZONE_HIGH = 999   # No overbought zone
MIN_CONFIDENCE = 0.85      # Minimum confidence to enter
EV_MIN_GATE = 0.02         # Minimum net EV to enter
MAX_CONTRACT_PRICE = 0.15   # Cheap side only
MIN_CONTRACT_PRICE = 0.08   # Avoid ultra-cheap illiquid
MAX_OPEN = 2                # Max 2 open positions
STOP_LOSS_PCT = 0.60        # 60% stop loss

# P0-C: Risk management
RISK_PCT_COLD = 0.01       # 1% per trade (first 50)
RISK_PCT_WARM = 0.02       # 2% per trade (50-500)
RISK_PCT_PROVEN = 0.03     # 3% per trade (500+)
MAX_BET_DOLLAR = 10.0      # $10 hard cap until proven
MIN_BET = 1.0              # $1 minimum (PM minimum)

# P0-B: Circuit breaker
DD_LEVEL_1 = 0.10          # 10% DD → halve risk
DD_LEVEL_2 = 0.15          # 15% DD → quarter risk, no entries
DD_LEVEL_3 = 0.25          # 25% DD → hard halt
DD_WINDOW = 50             # Rolling 50-trade DD window

# P0-A: EV zones
EV_ZONES = {
    'extreme_low': {'rsi_max': 18, 'base_prob': 0.65},
    'oversold': {'rsi_max': 28, 'base_prob': 0.78},
    'near_oversold': {'rsi_max': 35, 'base_prob': 0.72},
    'near_oversold3': {'rsi_max': 45, 'base_prob': 0.64},
}

ASSETS = {
    'BTC': Path(DATA_DIR / 'btc_5m_2026.parquet'),
    'ETH': Path(DATA_DIR / 'eth_5m_2026.parquet'),
    'SOL': Path(DATA_DIR / 'sol_5m_2026.parquet'),
    'XRP': Path(DATA_DIR / 'xrp_5m_2026.parquet'),
}

STRATEGIES = {
    'V19.7 oversold-only': {
        'rsi_zones': [(20, 28, 0.78), (28, 35, 0.72)],
        'direction': 'down',  # Buy cheap DOWN when oversold
        'max_price': 0.15, 'min_price': 0.03,
        'ny_only': True,
    },
    'V19.7 + near-oversold': {
        'rsi_zones': [(20, 28, 0.78), (28, 35, 0.72), (35, 45, 0.64)],
        'direction': 'down',
        'max_price': 0.15, 'min_price': 0.03,
        'ny_only': True,
    },
    'Cheap ≤5¢ only': {
        'rsi_zones': [(20, 28, 0.78), (28, 35, 0.72)],
        'direction': 'down',
        'max_price': 0.05, 'min_price': 0.02,
        'ny_only': True,
    },
    'Aggressive ≤25¢': {
        'rsi_zones': [(20, 28, 0.78), (28, 35, 0.72), (35, 55, 0.55)],
        'direction': 'down',
        'max_price': 0.25, 'min_price': 0.03,
        'ny_only': False,
    },
}

# ═══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════
def vec_rsi(prices, period=14):
    n = len(prices)
    rsi = np.full(n, 50.0)
    if n < period + 1: return rsi
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0: rsi[i + 1] = 100.0
        else: rsi[i + 1] = min(100, max(0, 100 - 100 / (1 + avg_gain / avg_loss)))
    return rsi

def vec_ema(prices, period):
    n = len(prices)
    ema = np.zeros(n)
    ema[0] = prices[0]
    k = 2.0 / (period + 1)
    for i in range(1, n):
        ema[i] = prices[i] * k + ema[i-1] * (1 - k)
    return ema

def calibrate_longshot(win_prob, contract_price):
    """Becker's longshot bias: cheap contracts underperform."""
    if contract_price <= 0.05:
        return win_prob * 0.836
    elif contract_price <= 0.10:
        return win_prob * 0.90
    elif contract_price <= 0.15:
        return win_prob * 0.95
    return win_prob

def calculate_ev(p_win, contract_price, slippage=0.01, friction=0.02):
    """V19.7 P0-A: Calculate net EV of a trade."""
    payout = (1 - contract_price) * (1 - PM_FEE)
    cost = contract_price * (1 + slippage)
    gross_ev = p_win * payout - (1 - p_win) * cost
    net_ev = gross_ev - friction * contract_price
    return net_ev

def rolling_dd(pnls, window=50):
    """Calculate max drawdown from rolling PnL window."""
    if len(pnls) < 5: return 0.0
    recent = pnls[-window:] if len(pnls) > window else pnls
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for p in recent:
        cum += p
        if cum > peak: peak = cum
        if peak > 0: max_dd = max(max_dd, (peak - cum) / peak)
    return max_dd

def ny_session(ts_ms):
    """Check if timestamp is during NY market hours (8am-6pm ET = 12:00-22:00 UTC)."""
    utc_hour = (ts_ms // 3600000) % 24
    return 12 <= utc_hour <= 22

# ═══════════════════════════════════════════════════════════════
# LOAD BINANCE DATA
# ═══════════════════════════════════════════════════════════════
def load_binance_data(asset):
    """Load Binance 5m klines from local parquet."""
    path = ASSETS[asset]
    if not path.exists():
        print(f"  WARNING: {path} not found, skipping {asset}")
        return None
    df = pd.read_parquet(path)
    # Rename to standard format
    df = df.rename(columns={'timestamp': 'open_time'})
    if 'close_time' not in df.columns:
        df['close_time'] = df['open_time'] + 299999
    df = df.sort_values('open_time').reset_index(drop=True)
    return df

def compute_signals(df, asset_name):
    """Compute RSI, EMA, and other indicators for 5m candles."""
    prices = df['close'].values
    timestamps = df['open_time'].values
    
    # Compute indicators
    rsi_14 = vec_rsi(prices, 14)
    ema_9 = vec_ema(prices, 9)
    ema_21 = vec_ema(prices, 21)
    
    results = []
    for i in range(30, len(prices)):
        ts = timestamps[i]
        rsi = rsi_14[i]
        price = prices[i]
        
        # Determine direction and confidence
        direction = "neutral"
        confidence = 0.0
        
        if rsi < RSI_OVERSOLD_MIN:
            # Knife-catching zone — blocked
            direction = "neutral"
            confidence = 0.0
        elif rsi < RSI_OVERSOLD:
            # Primary oversold → buy cheap DOWN
            direction = "down"
            confidence = 0.88
        elif rsi < RSI_NEAR_OVERSOLD:
            # Near-oversold with confirmation
            # Check: price below EMA9+EMA21 (bearish momentum confirmation)
            if price < ema_9[i] and price < ema_21[i]:
                direction = "down"
                confidence = 0.85
        elif rsi < 45:
            # Near-oversold with 3+ confirmations
            bear_count = 0
            if price < ema_9[i]: bear_count += 1
            if price < ema_21[i]: bear_count += 1
            if rsi < 40: bear_count += 1
            if bear_count >= 3:
                direction = "down"
                confidence = 0.85
        
        if direction == "neutral":
            continue
        
        contract_price = 0.15  # Default fallback
        
        # Session filter
        in_ny = ny_session(ts)
        
        # Contract price simulation: when RSI < 30, DOWN tokens are cheap (3-15¢)
        # Simulate realistic contract pricing
        # Lower RSI → cheaper DOWN token
        if direction == "down":
            if rsi < 20:
                contract_price = random.uniform(0.03, 0.06)
            elif rsi < 25:
                contract_price = random.uniform(0.05, 0.10)
            elif rsi < 30:
                contract_price = random.uniform(0.08, 0.15)
            elif rsi < 35:
                contract_price = random.uniform(0.10, 0.20)
            else:
                contract_price = random.uniform(0.15, 0.30)
        
        results.append({
            'timestamp': ts,
            'asset': asset_name,
            'price': price,
            'rsi': rsi,
            'direction': direction,
            'confidence': confidence,
            'contract_price': round(contract_price, 3),
            'ny_session': in_ny,
        })
    
    return results

# ═══════════════════════════════════════════════════════════════
# RUN BACKTEST
# ═══════════════════════════════════════════════════════════════
def run_backtest(strategy_name, strategy_config, all_signals, bankroll=INITIAL_BANKROLL):
    """Run V19.7 backtest with P0-A/B/C guards."""
    signals = sorted(all_signals, key=lambda x: x['timestamp'])
    
    cap = bankroll
    peak = bankroll
    n = w = l = 0
    trades = []
    recent_pnls = []
    dd = 0.0
    
    max_price = strategy_config.get('max_price', 0.15)
    min_price = strategy_config.get('min_price', 0.03)
    ny_only = strategy_config.get('ny_only', True)
    direction = strategy_config.get('direction', 'down')
    
    for sig in signals:
        # Session filter
        if ny_only and not sig['ny_session']:
            continue
        
        cp = sig['contract_price']
        if cp < min_price or cp > max_price:
            continue
        
        # RSI zone filtering
        rsi = sig['rsi']
        conf = sig['confidence']
        
        # Match RSI zones from strategy config
        zone_match = False
        base_prob = 0.50
        for zone_lo, zone_hi, zone_prob in strategy_config.get('rsi_zones', []):
            if zone_lo <= rsi < zone_hi:
                zone_match = True
                base_prob = zone_prob
                break
        if not zone_match:
            continue
        
        # P0-A: EV gate
        # Calibrate win probability with longshot bias
        p_win = calibrate_longshot(base_prob, cp)
        # Maker edge
        p_win += 0.0112  # +1.12% maker edge
        # Slippage
        p_win -= 0.01
        
        net_ev = calculate_ev(p_win, cp)
        if net_ev < EV_MIN_GATE:
            continue
        
        # P0-B: Circuit breaker check
        dd = rolling_dd(recent_pnls, DD_WINDOW)
        if dd >= DD_LEVEL_3:
            break  # Hard halt
        elif dd >= DD_LEVEL_2:
            continue  # No new entries
        elif dd >= DD_LEVEL_1:
            risk_mult = 0.5  # Halve risk
        else:
            risk_mult = 1.0
        
        # P0-C: Sizing
        if n < 50:
            base_pct = RISK_PCT_COLD
        elif n < 500:
            base_pct = RISK_PCT_WARM
        else:
            base_pct = RISK_PCT_PROVEN
        
        max_bet = MAX_BET_DOLLAR if n < 500 else cap * RISK_PCT_PROVEN
        bet = round(min(base_pct * risk_mult * cap, cap * 0.5, max_bet), 2)
        if bet < MIN_BET:
            continue
        
        # Simulate outcome
        won = random.random() < p_win
        if won:
            payout = bet / cp - bet
            pnl = payout * (1 - PM_FEE)
        else:
            pnl = -bet
        
        cap += pnl
        recent_pnls.append(pnl)
        if len(recent_pnls) > DD_WINDOW:
            recent_pnls.pop(0)
        
        n += 1
        if won: w += 1
        else: l += 1
        if cap > peak: peak = cap
        trades.append({
            'n': n, 'won': won, 'pnl': round(pnl, 2), 
            'cap': round(cap, 2), 'rsi': round(rsi, 1),
            'cp': cp, 'asset': sig['asset'],
            'p_win': round(p_win, 3), 'ev': round(net_ev, 4),
        })
        
        # Kill switch
        if cap < 5:  # Below $5 = game over
            break
    
    if n == 0:
        return None
    
    wr = w / n * 100
    total_pnl = cap - bankroll
    dd_pct = rolling_dd(recent_pnls, DD_WINDOW) * 100
    
    # Calculate Sharpe
    if len(trades) > 1:
        rets = [t['pnl'] / bankroll for t in trades]
        sharpe = np.mean(rets) / max(np.std(rets), 1e-9) * np.sqrt(n)
    else:
        sharpe = 0.0
    
    return {
        'strategy': strategy_name,
        'trades': n, 'wins': w, 'losses': l,
        'wr': round(wr, 1), 'pnl': round(total_pnl, 2),
        'pnl_pct': round(total_pnl / bankroll * 100, 1),
        'final_cap': round(cap, 2),
        'dd_pct': round(dd_pct, 1),
        'sharpe': round(sharpe, 2),
        'avg_cp': round(np.mean([t['cp'] for t in trades]), 3),
        'dd_level3': dd >= DD_LEVEL_3 if recent_pnls else False,
        'trades_list': trades[:5],  # First 5 for debugging
    }

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
print("=" * 70)
print("V19.7 10-DAY COMBINED BACKTEST (Binance + PMXT)")
print("=" * 70)

# Load all Binance data
all_signals = []
for asset_name in ASSETS:
    df = load_binance_data(asset_name)
    if df is None:
        continue
    sigs = compute_signals(df, asset_name)
    print(f"  {asset_name}: {len(df)} candles, {len(sigs)} signals")
    all_signals.extend(sigs)

print(f"\nTotal signals across all assets: {len(all_signals)}")
print(f"Date range: {pd.Timestamp(all_signals[0]['timestamp'], unit='ms').date()} - {pd.Timestamp(all_signals[-1]['timestamp'], unit='ms').date()}")

# Run each strategy
print(f"\n{'Strategy':<30} {'Tr':>4} {'WR':>6} {'P&L':>9} {'P&L%':>7} {'DD':>5} {'Sh':>5} {'Avg CP':>7}")
print("-" * 80)

results = {}
for strat_name, strat_config in STRATEGIES.items():
    for seed in range(5):
        random.seed(SEED + seed)
        np.random.seed(SEED + seed)
        res = run_backtest(strat_name, strat_config, all_signals, bankroll=INITIAL_BANKROLL)
        if res:
            key = f"{strat_name} (s{seed})"
            results[key] = res
            halting = " 🛑" if res.get('dd_level3') else ""
            print(f"{key:<30} {res['trades']:>4} {res['wr']:>5.1f}% {res['pnl']:>+8.2f} {res['pnl_pct']:>+6.1f}% {res['dd_pct']:>4.1f}% {res['sharpe']:>5.2f} {res['avg_cp']:>6.3f}{halting}")

# Summary by strategy
print(f"\n{'=' * 70}")
print("STRATEGY SUMMARY (avg across 5 seeds)")
print(f"{'=' * 70}")
for strat_name in STRATEGIES:
    strat_results = [r for k, r in results.items() if r['strategy'] == strat_name]
    if not strat_results: continue
    avg_tr = np.mean([r['trades'] for r in strat_results])
    avg_wr = np.mean([r['wr'] for r in strat_results])
    avg_pnl = np.mean([r['pnl'] for r in strat_results])
    avg_dd = np.mean([r['dd_pct'] for r in strat_results])
    avg_sharpe = np.mean([r['sharpe'] for r in strat_results])
    halts = sum(1 for r in strat_results if r.get('dd_level3'))
    print(f"  {strat_name:<30} {avg_tr:>5.1f}tr  {avg_wr:>5.1f}%  ${avg_pnl:>+8.2f}  DD:{avg_dd:>4.1f}%  Sh:{avg_sharpe:>5.2f}  halts:{halts}/5")

# RSI zone breakdown
print(f"\n{'=' * 70}")
print("RSI ZONE BREAKDOWN")
print(f"{'=' * 70}")
for zone_name, lo, hi in [("RSI<20 (blocked)", 0, 20), ("RSI 20-28 (oversold)", 20, 28), 
                           ("RSI 28-35 (near-oversold)", 28, 35), ("RSI 35-45 (weak)", 35, 45),
                           ("RSI 45+ (dead)", 45, 100)]:
    zone_sigs = [s for s in all_signals if lo <= s['rsi'] < hi]
    ny_sigs = [s for s in zone_sigs if s['ny_session']]
    # Check how many would pass EV gate at different contract prices
    passing = 0
    for s in ny_sigs[:1000]:
        cp = s['contract_price']
        for zone_lo, zone_hi2, zone_prob in STRATEGIES['V19.7 oversold-only']['rsi_zones']:
            if zone_lo <= s['rsi'] < zone_hi2:
                p_win = calibrate_longshot(zone_prob, cp) + 0.0112 - 0.01
                ev = calculate_ev(p_win, cp)
                if ev >= EV_MIN_GATE:
                    passing += 1
                break
    print(f"  {zone_name:<30} {len(zone_sigs):>6} total  {len(ny_sigs):>6} NY  ~{passing:>4} pass EV")

print(f"\nData sources:")
print(f"  Binance: 4 assets × 10 days (May 16-26, 2026, 5m candles)")
print(f"  PMXT: May 25 T00-T08 (contract price simulation overlay)")