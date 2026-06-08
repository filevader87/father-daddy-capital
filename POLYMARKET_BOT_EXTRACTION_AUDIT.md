# FDC V20.1 — Selective Extraction Report

## Polymarket-bot → FDC Integration Items

**Source repo:** https://github.com/MrFadiAi/Polymarket-bot  
**Version:** v3.1 (Jan 2026) | **Stars:** 138 | **Language:** TypeScript/Node.js  
**License:** MIT | **Last commit:** 5 months ago  
**Deps:** `@polymarket/clob-client ^5.1.3`, `ethers ^5`, `ws ^8`

---

## Full Audit Summary

| Dimension | Score | Notes |
|-----------|-------|-------|
| Code quality | 7/10 | Well-structured TypeScript, comprehensive types, inconsistent error handling |
| Security | 4/10 | Plain-text PKs, unauthed dashboard, no input validation on WebSocket commands |
| Test coverage | 3/10 | Only integration tests with dummy keys, no unit tests for strategy logic |
| FDC relevance | 6/10 | Same market structure (BTC Up/Down), CLOB patterns directly transferable |
| Maintenance | 5/10 | Last commit 5 months ago, no CI/CD, single maintainer |

**Verdict: Selective extraction, not full integration.**

---

## 1. Derive-First API Key Auth Pattern

**Source:** `src/services/trading-service.ts:220-236`  
**Current FDC approach:** `py_clob_client.create_or_derive_api_key()` — calls `createApiKey()` first, then falls back to `deriveApiKey()`. This hits a **400 error** every time for wallets that already have API keys, which is every wallet that has ever traded on Polymarket. The error is caught and handled, but it produces noise in logs and wastes a network round-trip on every startup.

**Port:**

```python
async def derive_or_create_api_key(self):
    # Try derive first (most common case — existing keys)
    derived = await self.clob_client.derive_api_key()
    if derived and derived.key:
        return derived
    # Derive failed — first-time wallet, create new key
    created = await self.clob_client.create_api_key()
    if not created or not created.key:
        raise AuthError("Failed to create or derive API key. Wallet may not be registered.")
    return created
```

**Why we need it:** Every time our `PMLiveClient` initializes, we get a 400 burst in logs. Worse — if Polymarket ever rate-limits failed `createApiKey` calls, our auth path breaks. Derive-first is the correct approach because most wallets (including ours, `0xD4a3...f090`) already have derived keys. This changes a noisy fallback into a clean primary path.

---

## 2. Tick Size & neg_risk Caching

**Source:** `src/services/trading-service.ts:252-275`  
**Current FDC gap:** Our `PMLiveClient.place_order()` passes `tick_size="0.01"` as a hardcoded string on every order. Polymarket CLOB rejects orders with incorrect tick sizes — some tokens use `0.01`, others use `0.001` or `0.0001`. Similarly, `neg_risk` determines whether the order uses the `NEG_RISK_CTF_EXCHANGE` contract address or the standard `CTF_CONTRACT`. We hardcode one path.

**Port:**

```python
class TickSizeCache:
    def __init__(self, clob_client):
        self._cache = {}   # token_id -> tick_size string
        self._neg_risk = {}  # token_id -> bool
        self._client = clob_client

    async def get_tick_size(self, token_id: str) -> str:
        if token_id not in self._cache:
            self._cache[token_id] = await self._client.get_tick_size(token_id)
        return self._cache[token_id]

    async def is_neg_risk(self, token_id: str) -> bool:
        if token_id not in self._neg_risk:
            self._neg_risk[token_id] = await self._client.get_neg_risk(token_id)
        return self._neg_risk[token_id]
```

**Why we need it:** Two scenarios will kill our orders in live mode. (1) We enter a BTC 5-min UP/DOWN market where tick size is `0.001` but we send `0.01` — CLOB rejects it. No fill, no trade, the signal is wasted. (2) A market is `neg_risk=true` but we send the order to the standard CTF contract — it either fails silently or executes on the wrong contract. Both are **silent failures** that look like "no signal" in our logs. Caching these per-token-id prevents both and costs almost nothing (one API call per token, ever).

---

## 3. CTF Split/Merge/Redeem Operations

**Source:** `src/clients/ctf-client.ts` (1,100+ lines)  
**Current FDC gap:** We have **zero settlement code**. When a 15-min UP/DOWN contract resolves, our position tracker marks it "closed" and calculates PnL on paper. But we never actually claim the winnings on-chain. If we buy UP at 0.55 and BTC goes up, the UP token settles to 1.0 — we're owed $0.45 per share. Without calling `redeemPositions()` on the CTF contract, that USDC sits locked in the contract forever.

**Port scope:**

- `splitPosition()` — USDC → YES + NO tokens (needed for hedging, not our current strategy)
- `mergePositions()` — YES + NO → USDC (needed if we ever hold both sides)
- `redeemPositions()` — **The critical one.** Claim winning tokens after resolution.
- `approve()` — ERC-1155 approval for CTF contract before any operation
- Proper `neg_risk` dispatch — use `NEG_RISK_CTF_EXCHANGE` address for multi-outcome markets

**Key contract addresses from their code:**

```
CTF_CONTRACT = 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
NEG_RISK_CTF_EXCHANGE = 0xC5d563A36AE78145C45a50134d48A1215220f80a
USDC.e (bridged) = [REDACTED_USDCe]
Native USDC = [REDACTED_USDC]
```

**Why we need it:** This is a **blind spot in our entire pipeline**. We track PnL in a JSON file but never withdraw from the Polymarket contract. At $2/trade and 20-30 trades in micro validation, that's up to $60 in positions that may resolve as wins. If the UP token we bought at 0.55 settles at 1.0, we're owed $0.45 × shares. Without `redeemPositions()`, that money is unrecoverable. The Polymarket-bot repo handles this with background redemption queues and auto-merge after DipArb legs complete. We need at minimum a `redeem_resolved_positions()` function that we call after each contract expiry window.

---

## 4. USDC.e Address Validation

**Source:** `src/clients/ctf-client.ts:9-47`  
**Current FDC gap:** Polymarket on Polygon uses **USDC.e (bridged USDC)** at address `[REDACTED_USDCe]`, NOT native USDC at `[REDACTED_USDC]`. Our wallet currently shows 0 USDC. When you deposit $50, if you send native USDC instead of USDC.e, our CLOB orders will fail with "insufficient balance" even though the wallet shows a USDC balance.

**Port:**

```python
USDC_E_ADDRESS = "[REDACTED_USDCe]"  # Bridged USDC.e
NATIVE_USDC_ADDRESS = "[REDACTED_USDC]"  # Native USDC

async def check_usdc_e_balance(self):
    """Check USDC.e (bridged) balance — the ONLY token Polymarket CTF accepts."""
    balance = await self.w3.eth.get_balance_at(USDC_E_ADDRESS, self.wallet.address)
    return balance

async def swap_native_to_bridged(self, amount):
    """Swap native USDC → USDC.e via DEX if needed."""
    ...
```

**Why we need it:** This is a **deposit failure waiting to happen**. Coinbase, Binance, and most exchanges default to sending **native USDC** on Polygon. If you deposit $50 of native USDC, our `check_wallet()` might show a balance, but CLOB orders will fail because the CTF contract only accepts USDC.e. The Polymarket-bot includes a `SwapService` to convert between the two. We need (a) a balance check that distinguishes the two tokens, (b) a clear error message if only native USDC is present, and (c) ideally a swap path so deposits aren't wasted. Without this, your $50 deposit could appear to work but produce zero fills.

---

## 5. DipArb Market Slug Parsing & Auto-Rotation

**Source:** `src/services/dip-arb-types.ts:287-340`, `src/services/dip-arb-service.ts:300-400`  
**Current FDC gap:** Our contract discovery uses `discover_contracts_multi("BTC")` which relies on Gamma API queries and manual slug construction. The DipArb service has a **robust slug parser** (`btc-updown-15m-{unix_timestamp}`) that:

- Extracts underlying asset from slug
- Extracts duration (5m vs 15m) from slug
- Calculates expiry from the embedded timestamp
- Auto-rotates to the next round when the current one expires
- Maintains a pending-redemption queue for resolved contracts

**Port scope:**

```python
def parse_slug(slug: str) -> dict:
    """Parse 'btc-updown-15m-1780615800' into components."""
    parts = slug.rsplit("-", 1)
    prefix, ts = parts[0], int(parts[1])
    underlying = prefix.split("-")[0].upper()  # "BTC"
    duration = int(prefix.split("-")[-1].rstrip("m"))  # 15
    expiry = datetime.fromtimestamp(ts, tz=timezone.utc)
    return {"underlying": underlying, "duration_minutes": duration, "expiry": expiry}

def auto_rotate(slug: str) -> str | None:
    """Given an expiring slug, compute the next round's slug."""
    parsed = parse_slug(slug)
    next_ts = parsed["expiry"]  # Next round starts when current expires
    return f"{parsed['underlying'].lower()}-updown-{parsed['duration_minutes']}m-{int(next_ts.timestamp())}"
```

**Why we need it:** Our micro validation run currently shows contracts expiring and positions settling, but we have **no auto-rotation**. When `btc-updown-15m-1780615800` expires, we need to seamlessly discover `btc-updown-15m-1780615900` without waiting for a full `discover_contracts_multi()` API call. The DipArb's auto-rotation logic also tracks pending redemptions — when a market resolves, it queues the claim for the next cycle. Without this, our bot will sit idle between contract rounds, missing the first 30-60 seconds of each new 15-minute window where the best edge exists. At 20-30 trades over 4 hours, each minute of delay after round rotation directly costs trade opportunities.

---

## Priority Order

| # | Item | Impact | Effort | Priority |
|---|------|--------|--------|----------|
| 4 | USDC.e validation | **Deposit fails if wrong token** | 2h | **P0 — before funding** |
| 3 | CTF redemption | **Money locked forever without it** | 4h | **P0 — before first live settle** |
| 2 | Tick size + neg_risk cache | **Orders silently rejected** | 1h | P1 — before scaling |
| 1 | Derive-first auth | **400 errors on every startup** | 30min | P1 — before scaling |
| 5 | Slug parsing + auto-rotation | **Missing first 60s of each round** | 3h | P2 — efficiency gain |

**P0 items must be in place before you deposit $50.** Without USDC.e validation, your deposit might not work. Without CTF redemption, any winning position's payout stays locked in the contract.

---

## What We Do NOT Integrate

| Pattern | Why Skip |
|---------|----------|
| Smart Money copy trading | Not our edge — we use signal-based directional |
| Full TypeScript/Node.js runtime | We're Python, not worth porting |
| Dashboard (unauthed WebSocket) | Security risk — zero auth on port 3001 |
| Their risk management config | Too coarse (5% daily) for micro validation |
| Binance K-line technicals | We have our own RSI + regime + transition signal stack |
| ArbitrageService (YES+NO < $1) | Not our strategy |

---

*Report generated for Father Daddy Capital V20.1 micro live validation.*  
*Source: MrFadiAi/Polymarket-bot v3.1, audited 2026-06-04.*