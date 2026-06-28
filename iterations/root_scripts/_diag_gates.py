#!/usr/bin/env python3
"""Quick diagnostic: run 1 cycle of the full gate stack to see what's being blocked."""
import sys, json
sys.path.insert(0, '.')
sys.path.insert(0, 'src')

from pm_engine_v19_8 import (
    discover_contracts_multi, enhanced_signal, fetch_asset_candles,
    get_clob_book_depth, classify_token_state, compute_downtrend_veto,
    ASSET_MAP
)
from src.regime.regime_classifier import classify_regime, Regime, BLOCKED_REGIMES
from src.microstructure.orderbook_transition import (
    OrderbookTransitionTracker, compute_transition_score, MINIMUM_TRANSITION_THRESHOLD
)
from src.microstructure.probability_lag import ProbabilityLagTracker
from collections import Counter

blocked = Counter()
passed = 0

tracker = OrderbookTransitionTracker()
prices_cache = {}
for asset_key in ['BTC']:
    try:
        prices_cache[asset_key] = fetch_asset_candles(asset_key, interval='5m')
        print(f"{asset_key}: {len(prices_cache[asset_key])} candles")
    except Exception as e:
        print(f"{asset_key}: error - {e}")

contracts_dict = discover_contracts_multi(asset_key='BTC')
contracts = []
for k, v in contracts_dict.items():
    if isinstance(v, list):
        contracts.extend(v)
print(f"Total contracts: {len(contracts)}")

for c in contracts:
    up_price = float(c.get('up_price', 0))
    slug = c.get('slug', '?')
    
    # Bucket check
    if not (0.50 <= up_price < 0.60):
        blocked['outside_bucket'] += 1
        continue
    
    # BTC only
    asset = c.get('asset', 'UNKNOWN')
    if asset != 'BTC':
        blocked['not_btc'] += 1
        continue
    
    prices = prices_cache.get('BTC', [])
    if len(prices) < 14:
        blocked['insufficient_prices'] += 1
        continue
    
    try:
        sig = enhanced_signal(prices, asset_key='BTC')
    except Exception as e:
        blocked['signal_error'] += 1
        continue
    
    direction = sig.get('direction', 'neutral')
    confidence = sig.get('confidence', 0)
    rsi = sig.get('rsi', 50)
    
    if direction == 'neutral':
        blocked['neutral_direction'] += 1
        print(f"  {slug}: neutral direction (rsi={rsi:.1f})")
        continue
    if confidence < 0.15:
        blocked['low_confidence'] += 1
        continue
    
    # Book data
    cond_id = c.get('conditionId', '')
    up_tid = c.get('up_token_id', '')
    book = None
    try:
        book = get_clob_book_depth(cond_id, token_id=up_tid)
    except:
        pass
    if not book:
        try:
            book = get_clob_book_depth(cond_id)
        except:
            pass
    if not book:
        blocked['no_book'] += 1
        continue
    
    bid_depth = float(book.get('depth_usd', 0)) / 2
    ask_depth = float(book.get('depth_usd', 0)) / 2
    spread = float(book.get('spread', 0))
    best_bid = float(book.get('best_bid', 0))
    best_ask = float(book.get('best_ask', 0))
    imbalance = (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-9)
    down_price = 1.0 - up_price
    
    # Transition
    ts_result = compute_transition_score(
        bid_depth=bid_depth, ask_depth=ask_depth, spread=spread,
        imbalance=imbalance, up_price=up_price, down_price=down_price,
        up_velocity=float(c.get('up_token_velocity', 0)),
        down_velocity=float(c.get('down_token_velocity', 0)),
        tracker=tracker,
    )
    ts = ts_result.transition_score
    
    if len(tracker._snapshots) >= 3 and ts <= MINIMUM_TRANSITION_THRESHOLD:
        blocked['low_transition'] += 1
        print(f"  {slug}: low transition={ts:.4f}")
        continue
    
    # Regime
    regime_result = classify_regime(
        asset='BTC', spot_price=sig.get('price', 0),
        spot_velocity_5s=sig.get('candle_velocity'),
        spot_velocity_15s=sig.get('spot_velocity_15s', 0),
        spot_velocity_30s=sig.get('spot_velocity_30s', 0),
        RSI=rsi, RSI_slope=sig.get('RSI_slope', 0),
        SMA20=sig.get('SMA20', 0), SMA20_slope=sig.get('SMA20_slope', 0),
        spread=spread, bid_depth=bid_depth, ask_depth=ask_depth,
        imbalance=imbalance, book_depth_total=bid_depth + ask_depth,
        lower_low_count=sig.get('lower_low_count', 0),
        higher_low_count=sig.get('higher_low_count', 0),
        price_vs_reference_pct=sig.get('price_vs_reference_pct', 0),
        time_to_expiry_minutes=c.get('mins_to_expiry'),
        transition_score=ts,
    )
    
    if regime_result.blocked:
        blocked[f'regime_{regime_result.regime.value}'] += 1
        print(f"  {slug}: blocked regime={regime_result.regime.value}")
        continue
    
    # Market state
    regime_name = regime_result.regime.value
    if regime_name in ("trend_continuation", "panic_sell"):
        market_state = "trending"
    elif regime_name in ("trend_exhaustion", "fake_reversal"):
        market_state = "transitioning"
    elif regime_name in ("balanced_rotation", "volatility_compression"):
        market_state = "balanced"
    else:
        market_state = "unknown"
    
    if market_state not in ("balanced", "unknown"):
        blocked[f'market_state_{market_state}'] += 1
        print(f"  {slug}: blocked market_state={market_state}")
        continue
    
    # Reversal
    RSI_slope = sig.get('RSI_slope', 0)
    higher_low_count = sig.get('higher_low_count', 0)
    spot_velocity_15s = sig.get('spot_velocity_15s', 0)
    price_vs_reference_pct = sig.get('price_vs_reference_pct', 0)
    
    signals = {}
    signals['rsi_slope_positive'] = (RSI_slope or 0) > 0
    signals['higher_low'] = (higher_low_count or 0) > 0
    signals['spread_compressing'] = getattr(ts_result, 'spread_compressing', False)
    signals['bid_strengthening'] = getattr(ts_result, 'bid_strengthening', False)
    signals['ask_weakening'] = getattr(ts_result, 'ask_weakening', False)
    signals['positive_velocity'] = (spot_velocity_15s or 0) > 0
    signals['reclaiming_reference'] = (price_vs_reference_pct or 0) > -0.005
    signals['volatility_compression'] = regime_name == "volatility_compression"
    reversal_count = sum(1 for v in signals.values() if v)
    
    if reversal_count < 2:
        blocked['insufficient_reversal'] += 1
        print(f"  {slug}: insufficient reversal={reversal_count}/2 ({dict((k,v) for k,v in signals.items() if v)})")
        continue
    
    # Downtrend veto
    prices_list = prices_cache.get('BTC', [])
    veto_data = compute_downtrend_veto(prices_list, contract=c, reference_price=prices_list[-1] if prices_list else None)
    if veto_data.get('downtrend_active') and not veto_data.get('reversal_confirmed'):
        blocked['downtrend'] += 1
        print(f"  {slug}: downtrend continuation")
        continue
    
    # Token state
    token_state = classify_token_state(c, rsi, direction, prices_list) if len(prices_list) >= 14 else "unknown"
    if isinstance(token_state, str) and token_state in ("false_dislocation", "nearly_decided", "dormant_longshot", "untradeable"):
        blocked[f'token_state_{token_state}'] += 1
        continue
    
    print(f"  ✅ {slug}: dir={direction} rsi={rsi:.1f} ts={ts:.4f} regime={regime_name} reversal={reversal_count} ALL GATES PASSED")
    passed += 1

print(f"\nPassed: {passed}")
print(f"Blocked: {dict(blocked)}")