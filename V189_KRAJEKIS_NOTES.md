# V18.9 Krajekis Playbook Integration
## Gaps Identified vs Krajekis Playbook

### Missing (HIGH priority):
1. **Time-window filter**: Only trade 5-10 min left (mid-window). V18.9 currently trades at 1.5-4.5 min left for 5m, 3-13 min for 15m. Krajekis says avoid early window (too random), prefer mid-window where structure formed.
2. **Daily loss limit**: Engine has MAX_DAILY_LOSS=$8 but V18.9 paper trader has NONE. Could blow up on a bad day.
3. **VWAP + EMA confluence**: We only use RSI+direction+regime. VWAP deviation and EMA21/50 alignment would add critical confluence scoring.
4. **ATR volatility regime**: We use simple trending/ranging/volatile. ATR-based vol classification (low vol = buy expensive 70-95¢, high vol = buy cheap 5-20¢) directly adjusts entry pricing.
5. **Session time-of-day**: No session logic. Asia (low vol, mean-reversion), NY overlap (high vol, directional), etc.

### Already Have (✅):
- RSI zones + direction (core signal)
- Blacklist (ranging, weak signals)
- Regime filter (trending_up, trending_down, ranging, volatile)
- Position sizing by tier (3%, 6%, 10%)
- Kill switch on bankroll drawdown
- Stop-loss, take-profit, trailing stop exits

### Implementation Plan:
1. Add VWAP + EMA21/50 computation to pm_engine_v18_8.py
2. Add ATR(14) to vol regime classification
3. Add session time-of-day classifier (UTC → EST)
4. Add time-window filter: prefer 5-10 min left for 15m, 2-4 min left for 5m
5. Add daily loss limit to paper trader
6. Add confluence scoring (0-10): RSI + EMA alignment + VWAP position + MACD + session + ATR
7. Only trade when confluence ≥ 7/10