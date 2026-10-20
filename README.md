# Jupiter Compounding Bot

Automates your compounding strategy on Jupiter.ag (Solana).

**Strategy:** Buy token with USDC → wait for 2x → sell → save 40% → reinvest 60% → repeat.

---

## Setup (5 minutes)

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure your .env
```bash
cp .env.example .env
nano .env   # or open in any editor
```

Fill in:
- `WALLET_PRIVATE_KEY` — export from Phantom: Settings → Security → Export Private Key
- Leave `DRY_RUN=true` until you've tested

### 3. (Recommended) Get a free RPC endpoint
The public Solana RPC is rate-limited and unreliable for bots.
Sign up at **https://www.helius.dev** (free tier is plenty) and paste the URL into `SOLANA_RPC_URL`.

### 4. (Optional) Get Birdeye API key
Birdeye gives more reliable price feeds than Jupiter's fallback.
Free key at **https://birdeye.so** → Developer → API Keys.
Paste into `BIRDEYE_API_KEY`.

### 5. Run in dry-run mode first
```bash
python bot.py
```

You'll see the full projection table and simulated cycles with no real transactions.

### 6. Go live
Once you're happy with the dry run:
```bash
# In .env, change:
DRY_RUN=false
```
Then run again. **Keep the terminal open** — if it closes, restart and it picks up from `journal.json`.

---

## How it works

```
Cycle 1:  Buy $7.77 USDC of SOL
          → Monitor price every 10 seconds
          → Price hits 2x entry price
          → Sell all SOL back to USDC
          → Receive ~$15.54 USDC
          → Save $6.22 (40%)
          → Reinvest $9.32 (60%) → next cycle

Cycle 2:  Buy $9.32 USDC of SOL
          → ... repeat
```

Matches the spreadsheet exactly: each win compounds 60% forward, 40% is locked.

---

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Main bot |
| `.env` | Your config (never share this) |
| `.env.example` | Template |
| `journal.json` | Auto-created — tracks all cycles and state |
| `bot.log` | Full trade log |

---

## Realistic expectations ⚠️

This bot **executes the compounding math correctly**, but the 2x target is not guaranteed:

- SOL price moves — it may take hours, days, or never hit 2x
- If price drops significantly, you'll sell at a loss (or hold until it recovers)
- Enable `STOP_LOSS_MULT=0.8` to auto-sell if price drops 20% (limits downside)
- Gas/slippage costs ~0.5% per swap — over 30 cycles this compounds too

**Start small. Test with $7.77 as the spreadsheet shows.**

---

## Customising the token

Change `OUTPUT_MINT` in `.env` to trade any Solana token:

```
# Volatile memecoins (higher chance of 2x, higher chance of 0)
BONK = DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263
WIF  = EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm
JUP  = JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN
```

---

## Emergency stop

Press `Ctrl+C` at any time. State is saved in `journal.json`.
The bot will resume from where it left off on next start.

If you have an open position and want to exit manually:
go to **jup.ag**, connect your wallet, and swap back to USDC.
Then delete `journal.json` and restart.
