# Father Daddy Capital (FDC)

Multi-arm hedge fund trading algo for capital accumulation.

## Architecture

```
fdc/
├── arms/                           # Trading arms (each independent)
│   ├── polymarket/                 # ✅ LIVE — Polymarket prediction markets
│   │   ├── scalper.py              #   Crypto 5m up/down binaries (EV-first, dual-mode)
│   │   ├── weather_bot.py          #   Temperature markets (Gumbel + EV)
│   │   ├── world_cup_bot.py        #   World Cup 2026 match markets (Elo + Poisson)
│   │   ├── clob_client.py          #   Polymarket CLOB client
│   │   ├── pm_live.py              #   Live order submission
│   │   ├── city_registry.py        #   Weather city metadata
│   │   ├── settlement_rounding.py  #   Settlement rounding rules
│   │   └── isotonic_calibration.py #   Probability calibration
│   ├── crypto_short_term/          # ⏸ INACTIVE — LSTM + Q-learning crypto/stock
│   ├── capital_management/         # ⏸ INACTIVE — Portfolio allocation, Kelly sizing
│   └── chaos/                      # ⏸ NOT IMPLEMENTED — Black swan event trader
├── core/
│   └── risk_gateway.py             # Portfolio-level risk management across arms
├── src/                            # Shared libraries (weather lib, polyweather)
├── iterations/                     # Historical versions (V18–V21.7.63, archived)
├── output/                         # Runtime state, logs, trade records
└── docs/                           # Documentation
```

## Arms

### Polymarket (LIVE)
- **Scalper** (V21.7.76): Crypto 5-minute up/down binaries on Polymarket. Dual-mode signals: REVERSAL (RSI<25→UP, RSI>75→DOWN, entry 10-40¢) and CERTAINTY (momentum continuation, entry 70¢+). EV-ranked, Kelly-sized, 5 risk layers including $40 hard floor.
- **Weather Bot** (V21.7.76): Daily temperature markets across 50 cities. Gumbel distribution for daily maxima, conformal + isotonic calibration, EV-first signal selection, NO-side only (YES historically 0% WR).
- **World Cup Bot**: FIFA World Cup 2026 match markets. Elo ratings + Poisson xG model. Dormant — activates during tournament.

### Crypto/Stock Short-Term (INACTIVE)
LSTM + Q-learning agents for short-term crypto and stock trading. Requires live exchange connectivity (CCXT/Binance). Not deployed.

### Capital Management (INACTIVE)
Portfolio allocation across arms using Kelly criterion. Cross-arm risk gateway. Not deployed.

### Chaos Trader (NOT IMPLEMENTED)
Black swan event trader — high-conviction asymmetric bets on tail-risk events. Design pending.

## Risk Management

Each arm has independent risk controls:
- Max consecutive losses → halt
- Max daily loss → halt
- Hard drawdown floor → permanent halt
- Balance check before orders

Portfolio-level risk gateway (`core/risk_gateway.py`):
- 20% max portfolio drawdown
- $10 max daily loss across all arms
- Per-arm capital allocation

## Environment

`.env` at `/mnt/c/Users/12035/father_daddy_capital/.env`:
- `PM_WALLET_PRIVATE_KEY` — Polymarket wallet private key
- Proxy wallet: `0xaF7B21FE2B18745aE1b2fA2F6F00B0fC4EF3F70b`

## License

MIT