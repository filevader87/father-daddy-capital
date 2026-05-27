# V18.5 Strategy — RSI + Direction Extreme Zones

## Validated on 31 days of Binance 5m candles (9,000 data points)

### Strategy Rules
1. **Severe Oversold (RSI < 25) + BTC DOWN direction → BUY DOWN token**
   - 80.6% WR (129 trades), ~4 trades/day
2. **Severe Overbought (RSI > 75) + BTC UP direction → BUY UP token**
   - 87.1% WR (124 trades), ~4 trades/day
3. **Oversold (RSI 25-30) + BTC DOWN direction → BUY DOWN token** (moderate)
   - 73.9% WR (176 trades), ~6 trades/day
4. **Overbought (RSI 70-75) + BTC UP direction → BUY UP token** (moderate)
   - 69.1% WR (191 trades), ~6 trades/day

### Direction Detection
- 3-candle lookback (15 minutes)
- Minimum 0.03% change for direction signal
- FLAT = no trade

### Entry Parameters
- Max entry price: 15¢ (cheap side of binary)
- Bet size: 10% of bankroll (max $5)
- Only trade when RSI extreme + direction confirmed

### Monte Carlo Results (1000 bankrolls, $100 start)
- **80%+ only**: 83.8% WR, $2,422/week, $10K/month, 0% bust
- **70%+ moderate**: 76.5% WR, $5,287/week, $22K/month, 0% bust

### Key Files
- `pm_engine_v18_5_rsi.py` — Live scanner + Binance backtest
- `binance_backtest_v18_5.py` — Full direction + RSI grid search
- `pm_engine_v18_5.py` — Original Gamma API scanner

### Data Sources
- Binance 5m candles: `btc_5m_candles.json` (9,000 candles, Apr 26 - May 27)
- Gamma API: 303 active BTC Up/Down 5-min markets with UP/DOWN token IDs
- CLOB API: Real-time token prices