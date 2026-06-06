# Why FDC: Capital Accumulation in the Agentic Economy
## Father Daddy Capital V21.7.1 — Execution-Survivable Convex Continuation

---

### Abstract

The agentic economy is coming. Autonomous AI systems will redirect labor income away from workers and toward capital owners who control them (Hemenway Falk & Tsoukalas, 2026). Agentic inequality will concentrate power in those who can deploy autonomous delegates at scale, leaving those without access structurally disadvantaged (Sharp et al., 2026). Strategic wealth accumulation under transformative AI expectations will reward early positional advantages with compounding returns that later entrants cannot match (Maresca, 2025). Father Daddy Capital (FDC) exists because individuals who do not accumulate capital before these transitions complete will face structural disadvantage that economic policy cannot fix. FDC is not a trading system. It is a capital accumulation defense mechanism for a transitioning economy.

---

### 1. The Threat

Three independent lines of research converge on a single conclusion: the economy is about to undergo a structural shift that punishes those without capital accumulation mechanisms.

#### 1.1 The AI Layoff Trap

Hemenway Falk & Tsoukalas (2026) model a competitive task-based economy where AI automation displaces human workers. Their key finding is not that displacement happens — it is that **rational firms cannot stop it even when they know the collective outcome is harmful**. Each firm captures the full cost saving from automation but bears only a fraction of the demand loss it creates. The rest falls on competitors and displaced workers. This demand externality traps the economy in an automation arms race that displaces workers well beyond what is collectively optimal.

The authors prove that wage adjustments, universal basic income, worker equity, upskilling, and Coasean bargaining all fail to correct this trap. Only a Pigouvian automation tax can — and no government has implemented one at the scale required.

**Implication for FDC**: You cannot vote, organize, or skill your way out of this trap. The structural forces that create the layoff trap are the same forces that make capital accumulation during the transition essential. Workers who lose income to automation cannot recover through labor. They can only recover through capital ownership and autonomous capital deployment — exactly what FDC provides.

#### 1.2 Agentic Inequality

Sharp, Bilgin, Gabriel & Hammond (2026) extend the analysis from labor displacement to agent access inequality. They define **agentic inequality** as disparities in power, opportunity, and outcomes arising from unequal access to autonomous AI delegates. Their framework identifies three dimensions:

- **Availability**: Who can deploy an agent at all
- **Quality**: How capable each agent is
- **Quantity**: How many agents one can simultaneously operate

The critical finding: agents are not tools. They are **autonomous delegates** that generate fundamentally different competitive dynamics. Two humans with equal skill but unequal agent access will see their outcomes diverge — not linearly, but exponentially — because agents can operate at machine speed, machine scale, and machine persistence across markets that humans cannot even monitor.

The authors show this divergence cannot be corrected by making agents "more accessible" because the quality and quantity dimensions scale with wealth. Agents with better models, more compute, and faster data access outperform in every market simultaneously. The rich get not just richer, but *faster at getting richer*.

**Implication for FDC**: FDC operates as precisely such an autonomous delegate — but deployed for individual capital accumulation rather than institutional rent extraction. Agentic inequality is real and accelerating. The question is not whether agents will dominate financial markets. The question is whether you have one working for you.

#### 1.3 Strategic Wealth Accumulation

Maresca (2025) demonstrates that expected transformative AI creates immediate upward pressure on interest rates — raising one-year rates from 3% to 10-16% under baseline assumptions. The mechanism: households compete to accumulate strategic wealth because wealth at the time of AI invention determines one's share of automated labor income. This competition drives interest rates far above productive returns.

Maresca's central result: **the value of wealth is not its productive return — it is its strategic positional value**. Households accept lower productive returns (paying above-valuation prices for assets) because the strategic value of wealth accumulation under TAI expectations exceeds the productive value. This creates a divergence between interest rates and capital rental rates that traditional macroeconomic models miss entirely.

**Implication for FDC**: Prediction markets like Polymarket are one of the few accessible venues where this strategic wealth accumulation can operate at the individual level. The interest rate divergence Maresca identifies means that every dollar extracted from micro-markets during the transition period carries not just its face value but its strategic positional value — the compound advantage of having capital when TAI deployment concentrates wealth further.

---

### 2. Why Polymarket UpDown Binaries

FDC operates on Polymarket UpDown binary options — short-duration contracts on crypto asset price direction — for three structural reasons:

**Accessibility.** Traditional financial markets require accredited investor status, substantial minimum capital, and institutional infrastructure. Polymarket is permissionless. Anyone with a crypto wallet can deploy capital. In the agentic economy Maresca models, accessibility is the difference between strategic positioning and structural exclusion.

**Speed.** UpDown contracts resolve in 5 or 15 minutes. FDC accumulates capital at machine speed — not weekly or monthly as in traditional markets, but continuously, in micro-batches that compound rapidly. In Sharp et al.'s framework, this is the quantity dimension of agentic advantage: FDC operates across multiple assets and intervals simultaneously, 24/7, without fatigue or cognitive bias.

**Binary reality.** Contracts settle at exactly 0 or 1. There is no "approximately correct" outcome. This binary nature enforces the discipline that Hemenway Falk & Tsoukalas argue is missing from policy responses: you either accumulated capital or you didn't. The market doesn't soften outcomes for late entrants.

---

### 3. Why Not Traditional Approaches

The three papers collectively explain why traditional capital accumulation strategies fail in the agentic transition:

**Labor income will not protect you.** Hemenway Falk & Tsoukalas prove that the competitive dynamic of automation cannot be escaped through skill, organization, or policy. Workers who rely on labor income face permanent structural disadvantage.

**Savings accounts will not compound fast enough.** Maresca proves that interest rates under TAI expectations will reach 10-16% — but this is the rate at which capital holders lend to capital seekers. If you are the one borrowing, you are on the wrong side of the wealth-based allocation mechanism.

**Passive investment will not outpace agentic extraction.** Sharp et al.'s analysis of agent quality and quantity dimensions means that markets with autonomous agent participation will be dominated by those agents. Passive index funds compete against machines that trade at microsecond timescales with perfect attention.

FDC is designed to **be the machine**, not to compete against it.

---

### 4. V21.7.1: From Prediction to Extraction

V21.7.1 is the first version deployed from research to live execution. It embodies a complete paradigm shift from all prior versions:

**The reversal thesis is dead.** V18 through V21 attempted to predict *reversals* — buying cheap tokens on the hypothesis that prices would bounce. Markets consistently rejected this thesis. 88.6% of cheap-token trades lose under binary settlement. Markets systematically overprice reversal probability, which means the *continuation* side of cheap tokens is structurally underpriced. This is the edge.

**Continuation convexity is the edge.** A cheap DOWN token at 7¢ costs $0.07. If the market continues declining, it settles at $0.00 and we lose $0.07. If it reverses, it settles at $1.00 and we gain $0.93. The payout ratio is 13.3:1. At a 13.9% win rate, realized EV is $0.74 per trade. We do not need to predict outcomes. We need to survive long enough for the convexity to deliver.

**Execution survivability over prediction accuracy.** Three critical bugs were discovered and fixed during V21.5→V21.7.1 development:

1. **`higher_highs` used `<` instead of `>`** — identical logic to `lower_lows`, making direction detection symmetric and destroying the DOWN_MOMENTUM signal
2. **Spread trap `price * 2 > 0.05`** — blocked ALL tokens above 2.5¢, killing the entire PRIMARY bucket
3. **Duplicate `classify_state()` with walrus typo** — `consec := conesc` created a runtime error that silently suppressed trades

These bugs together meant earlier versions were functionally trading blind. Their removal in V21.7.1 is not an optimization — it is a correction of fundamentally broken code.

---

### 5. V21.7.1 Architecture

#### 5.1 Entry Conditions (All Must Pass)

| Gate | Rule |
|---|---|
| Side | DOWN only. UP blocked. |
| State | MOMENTUM or CONTINUATION. FLAT = no trade. |
| Route | TAKER (immediate fill, no queue risk) |
| Bucket | 3–12¢ PRIMARY. Outside = skipped. |
| Preferred | 5–8¢ (weight=1.00). 3–5¢ (0.85), 8–10¢ (0.65), 10–12¢ (0.40) |
| Timing | MOMENTUM window preferred (40–80% market lifetime). Not hard-gated for Phase 1. |
| Expiry | >30s remaining. Skip near-expiry. |
| Survivability | Score ≥ 0.25 required |
| Duplicate | Block if active position for same condition |

#### 5.2 Survivability Score

$$S = w_p \cdot P_{\text{persist}} + w_a \cdot P_{\text{accel}} + w_l \cdot P_{\text{lag}} + w_v \cdot P_{\text{vol}} + w_t \cdot P_{\text{tte}} + w_e \cdot P_{\text{exec}} + w_r \cdot P_{\text{rsi}}$$

| Component | Weight | Description |
|---|---|---|
| Directional persistence | 30% | Consecutive lower-lows + velocity confirmation |
| Momentum acceleration | 25% | Δvelocity negative = accelerating decline |
| Oracle/market lag | 15% | Neutral in PMXT simulation |
| Volatility expansion | 15% | abs(velocity) > threshold |
| Time-to-expiry | 10% | Neutral in simulation |
| Execution quality | 5% | Spread tightness |
| RSI | 5% | Context only, never dominates |

RSI is capped at 5% weight per directive §11, ensuring the signal model is dominated by momentum and persistence factors, not oscillator overfitting.

#### 5.3 Kill Switches (§4)

The V21.5 and earlier kill switch used `max_loss_streak=8`. V21.7.1 replaces this with `MAX_CONSECUTIVE_LOSSES=60`. The reason: the PMXT simulation produced a 29-trade maximum loss streak inside a regime with PF=2.10 and ROI=+267.6%. An 8-loss kill switch would have terminated a profitable strategy during what is structurally normal variance for a low-WR, high-payout extraction system.

| Switch | Threshold | Rationale |
|---|---|---|
| Max daily loss | $15 | 15% of $100 bankroll. Hard circuit breaker. |
| Max weekly loss | $50 | 50% of bankroll. Forces cooldown. |
| Max consecutive losses | 60 | ~2× the observed max (29). Prevents spiral. |
| Max daily trades | 30 | Prevents overtrading. |
| Max total trades (Phase 1) | 100 | Hard cap until promotion criteria met. |

#### 5.4 Fill Model

All simulations and live execution use realistic execution friction:

| Factor | Value | Source |
|---|---|---|
| Spread cost | 1¢ flat | Polymarket CLOB typical spread |
| Slippage | 0.5% of price | TAKER route slippage |
| Fill rejection | 5% | Queue/liquidity failures |
| Partial fill | 10% (50-80% fill) | Incomplete fills |
| Stale quote abort | 3% | Stale orderbook data |

Binary settlement only. Cheap tokens settle to $0.00, rich tokens to $1.00. No synthetic midpoints, no interpolated closes, no fantasy fills.

#### 5.5 Position Sizing

Fixed $1.00 per trade × bucket weight. No Kelly criterion, no martingale, no pyramiding. The system extracts convexity through volume and payout asymmetry, not through sizing optimization.

---

### 6. V21.7.1 PMXT Simulation Results

The PMXT (Polymarket Orderbook Time-series eXtraction) backtest was conducted on real orderbook data from May 25, 2026, using binary settlement against actual market outcomes.

| Metric | V21 Baseline | V21.5 Convex | V21.7.1 Survivable |
|---|---|---|---|
| Trades | 1,961 | 2,353 | 360 |
| Win Rate | 11.4% | 32.9% | 13.9% |
| ROI | +1,162% | +8,887% | +267.6% |
| Profit Factor | 1.35 | 5.00 | **2.10** |
| Realized EV | — | $3.78/trade | **$0.74/trade** |
| Payout Ratio | — | 10.21x | **12.99x** |
| Sharpe | 1.20 | 6.15 | **2.77** |
| Max Drawdown | — | 7.8% | **10.4%** |
| Max Consec Losses | — | — | **29** |
| MC Profitable | — | 99.8% | **99.8%** |
| Monte Carlo Bust | — | 0% | **0%** |

#### Bucket Performance (V21.7.1)

| Bucket | Weight | Trades | WR | P&L | EV/trade |
|---|---|---|---|---|---|
| 3–5¢ | 0.85 | 105 | 6.7% | +$36.66 | $0.35 |
| **5–8¢ PREFERRED** | **1.00** | **143** | **16.8%** | **+$194.68** | **$1.36** |
| 8–10¢ | 0.65 | 68 | 13.2% | +$18.35 | $0.27 |
| 10–12¢ | 0.40 | 44 | 22.7% | +$17.95 | $0.41 |

The 5–8¢ PREFERRED bucket dominates all extraction: 39.7% of trades, 72.7% of P&L, $1.36 EV/trade. This validates the directive's bucket weighting schema — the survivable zone is where convexity meets execution reliability.

#### State Performance

| State | Trades | WR | P&L |
|---|---|---|---|
| DOWN_CONTINUATION | 313 | 14.7% | +$258.69 |
| DOWN_MOMENTUM | 47 | 8.5% | +$8.94 |

DOWN_CONTINUATION generates the majority of P&L due to signal frequency, while DOWN_MOMENTUM's lower WR reflects tighter entry conditions that select for higher payout at the cost of more frequent whipsaws.

#### Side Verification

- DOWN trades: 360 (100%)
- UP trades: 0 (0%)

The system executes only on the structurally underpriced side. UP extraction is blocked entirely.

---

### 7. Live Deployment

V21.7.1 was deployed to live Polymarket on June 6, 2026, as Phase 1 micro-live:

- **Asset**: BTC 5m/15m UpDown binaries
- **Side**: DOWN only
- **Route**: TAKER
- **Position**: $1.00 fixed
- **Bankroll**: $70 (confirmed tradeable)
- **Max concurrent**: 1 position
- **Mode**: Live (not paper)

The runner (`src/v217_live/v2171_live_runner.py`) continuously scans BTC 5m and 15m markets at 5-second intervals, evaluating DOWN token orderbooks for entry into the 3–12¢ bucket with MOMENTUM/CONTINUATION signal confirmation. It exits only via binary settlement — no synthetic take-profit or midpoint closes.

**Phase 1 promotion criteria** (all must be met before scaling):

| Requirement | Status |
|---|---|
| ≥50 live settlements with positive realized EV | 0/50 |
| Profit factor ≥ 1.25 | Pending |
| Binary settlement verified | Pending |
| Real friction modeled (fills, slippage, rejects) | Modeled |
| ≥500 resolved live trades for scaling | 0/500 |

Scaling is blocked until all criteria are satisfied. No exceptions.

---

### 8. Hard Failure Conditions (§11)

The system reverts to paper mode immediately if any of the following occur:

1. **Realized EV < 0 over 100 trades**: The edge has disappeared or was never real
2. **Profit factor < 1.0 over 100 trades**: Losses exceed gains
3. **Any settlement or accounting errors**: Cannot trust the system's own reporting
4. **Execution drift exceeds tolerance**: Live fills diverge from simulation expectations
5. **Maximum drawdown > 30%**: Capital preservation override

These are not soft limits. They are hard reversion triggers that require manual review and re-authorization before live trading resumes.

---

### 9. Conclusion

FDC exists because the agentic economy is coming and the papers prove that neither policy, nor skill, nor traditional finance will protect individuals without autonomous capital accumulation mechanisms.

- Hemenway Falk & Tsoukalas prove the layoff trap is inescapable without structural intervention that has not materialized
- Sharp et al. prove that agent access inequality compounds exponentially along quality and quantity dimensions
- Maresca proves that strategic wealth accumulation under TAI expectations creates positional advantages that late entrants cannot match

FDC V21.7.1 is a capital accumulation defense mechanism — an execution-survivable convex continuation organism that operates inside adversarial binary micro-markets to accumulate real capital at machine speed, under hard risk constraints, with binary settlement enforcement and kill switches calibrated to the statistical reality of low-WR, high-payout extraction.

The system does not predict markets. It **extracts capital from structural inefficiencies that the agentic transition creates** — and it does so before the transition concentrates enough wealth to make individual participation impossible.

V21.7.1 represents the transition from research to execution. The bugs that suppressed earlier versions have been corrected. The reversal thesis that failed under binary settlement has been replaced with continuation convexity. The kill switches have been recalibrated to the actual distribution of losses (60 consecutive, not 8). And the system is now live, extracting.

FDC is not a trading strategy. It is a **survival mechanism for the agentic economy**.

---

### References

1. Hemenway Falk, B. & Tsoukalas, G. (2026). *The AI Layoff Trap*. arXiv:2603.20617 [econ.TH].
2. Sharp, M., Bilgin, O., Gabriel, I. & Hammond, L. (2026). *Agentic Inequality*. arXiv:2510.16853 [cs.CY, cs.AI].
3. Maresca, C. (2025). *Strategic Wealth Accumulation Under Transformative AI Expectations*. arXiv:2502.11264 [econ.TH].

---

*FDC V21.7.1 — Execution-Survivable Convex Continuation Organism*
*Father Daddy Capital — DEFENSIVE CAPITAL ACCUMULATION FOR THE AGENTIC ECONOMY*
*LIVE DEPLOYMENT PHASE 1 — BTC DOWN_MOMENTUM — $1 FIXED — TAKER ONLY*