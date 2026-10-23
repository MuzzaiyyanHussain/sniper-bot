"""
Jupiter.ag Compounding Strategy Bot
====================================
Strategy: Scan for new token launches → buy via Jupiter → poll price
→ sell at 2x → save 40% → reinvest 60% → repeat.

Requirements: pip install solders requests python-dotenv base58 websockets
"""

import os
import sys
import time
import json
import csv
import base64
import logging
import requests
import base58
from datetime import datetime
from decimal import Decimal
from dotenv import load_dotenv

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from token_scanner import TokenScanner, ScannerConfig, TokenCandidate, DexScreenerClient

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════════
class Config:
    WALLET_PRIVATE_KEY:    str   = os.getenv("WALLET_PRIVATE_KEY", "")
    STARTING_AMOUNT_USDC:  float = float(os.getenv("STARTING_AMOUNT_USDC", "4.00"))
    WIN_MULTIPLIER:        float = float(os.getenv("WIN_MULTIPLIER", "2.0"))
    REINVEST_PCT:          float = float(os.getenv("REINVEST_PCT", "0.60"))
    SAVE_PCT:              float = 1.0 - float(os.getenv("REINVEST_PCT", "0.60"))
    INPUT_MINT:            str   = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
    TOKEN_MODE:            str   = os.getenv("TOKEN_MODE", "scan")
    FIXED_OUTPUT_MINT:     str   = os.getenv("FIXED_OUTPUT_MINT", "So11111111111111111111111111111111111111112")
    SCAN_MAX_WAIT_MIN:     int   = int(os.getenv("SCAN_MAX_WAIT_MIN", "15"))
    POLL_INTERVAL_SEC:     int   = int(os.getenv("POLL_INTERVAL_SEC", "8"))
    PRICE_TARGET_MULT:     float = float(os.getenv("PRICE_TARGET_MULT", "2.0"))
    STOP_LOSS_MULT:        float = float(os.getenv("STOP_LOSS_MULT", "0.82"))
    MAX_CYCLES:            int   = int(os.getenv("MAX_CYCLES", "30"))
    SLIPPAGE_BPS:          int   = int(os.getenv("SLIPPAGE_BPS", "50"))
    PRIORITY_FEE_LAMPORTS: int   = int(os.getenv("PRIORITY_FEE_LAMPORTS", "1000"))
    SOLANA_RPC_URL:        str   = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

    # ── Play floor — prevents the death spiral ────────────────────────────────
    # If the play amount drops below this after a loss, reset it to
    # STARTING_AMOUNT_USDC. This is the single most important safety
    # feature — without it, a single large opening loss (like USWR's
    # -86.2% in this run) can permanently cripple all future cycles even
    # when the win rate and win size are actually healthy.
    MIN_PLAY_AMOUNT:       float = float(os.getenv("MIN_PLAY_AMOUNT", "1.00"))

    # ── Consecutive loss pause ─────────────────────────────────────────────────
    # After N straight losses, pause before the next scan.
    MAX_CONSEC_LOSSES:     int   = int(os.getenv("MAX_CONSEC_LOSSES", "2"))
    LOSS_PAUSE_SEC:        int   = int(os.getenv("LOSS_PAUSE_SEC", "300"))

    # ── Jupiter v6 API (api.jup.ag — NOT quote-api.jup.ag) ───────────────────
    JUPITER_QUOTE_URL:     str   = "https://api.jup.ag/swap/v1/quote"
    JUPITER_SWAP_URL:      str   = "https://api.jup.ag/swap/v1/swap"
    DRY_RUN:               bool  = os.getenv("DRY_RUN", "true").lower() == "true"

    # ── Liquidity / dead-pool handling ────────────────────────────────────────
    # Below this, a pool is considered unsellable (no real market depth) —
    # we stop hammering Jupiter for quotes and alert instead of retrying.
    MIN_SELLABLE_LIQUIDITY_USD: float = float(os.getenv("MIN_SELLABLE_LIQUIDITY_USD", "25"))
    # Once a position is confirmed dead-pool, back off hard between rechecks
    # instead of retrying every 60s forever.
    DEAD_POOL_RECHECK_SEC:      int   = int(os.getenv("DEAD_POOL_RECHECK_SEC", "600"))


# ════════════════════════════════════════════════════════════════════════════════
# STRATEGY CALCULATOR
# ════════════════════════════════════════════════════════════════════════════════
class StrategyCalc:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def cycle_result(self, play_amount: float, win: bool) -> dict:
        if not win:
            return {"play_amount": play_amount, "winnings": 0.0,
                    "next_play": play_amount, "saved": 0.0, "win": False}
        winnings  = play_amount * self.cfg.WIN_MULTIPLIER
        next_play = winnings * self.cfg.REINVEST_PCT
        saved     = winnings * self.cfg.SAVE_PCT
        return {"play_amount": play_amount, "winnings": winnings,
                "next_play": next_play, "saved": saved, "win": True}

    def project(self, days: int = 30) -> list:
        rows = []
        play = self.cfg.STARTING_AMOUNT_USDC
        total_saved = 0.0
        for d in range(1, days + 1):
            r = self.cycle_result(play, win=True)
            total_saved += r["saved"]
            rows.append({**r, "day": d, "total_saved": total_saved})
            play = r["next_play"]
        return rows


# ════════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════════
USDC_DECIMALS = 6

def usdc_to_lamports(usdc: float) -> int:
    return int(Decimal(str(usdc)) * Decimal(10 ** USDC_DECIMALS))

def lamports_to_usdc(lam: int) -> float:
    return lam / (10 ** USDC_DECIMALS)


# ════════════════════════════════════════════════════════════════════════════════
# JUPITER CLIENT
# ════════════════════════════════════════════════════════════════════════════════
class JupiterClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def get_quote(self, input_mint: str, output_mint: str, amount_lamports: int,
                  slippage_bps: int = None) -> dict:
        """Fetch best swap route from Jupiter API v6."""
        params = {
            "inputMint":   input_mint,
            "outputMint":  output_mint,
            "amount":      amount_lamports,
            "slippageBps": slippage_bps if slippage_bps is not None else self.cfg.SLIPPAGE_BPS,
        }
        resp = self.session.get(self.cfg.JUPITER_QUOTE_URL, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def build_swap_transaction(self, quote: dict, user_pubkey: str) -> str:
        """Get serialized swap transaction from Jupiter."""
        payload = {
            "quoteResponse":             quote,
            "userPublicKey":             user_pubkey,
            "wrapAndUnwrapSol":          True,
            "prioritizationFeeLamports": self.cfg.PRIORITY_FEE_LAMPORTS,
            "dynamicComputeUnitLimit":   True,
        }
        resp = self.session.post(self.cfg.JUPITER_SWAP_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()["swapTransaction"]

    def sign_and_send(self, raw_tx_b64: str, keypair: Keypair) -> str:
        """Deserialize, sign, and broadcast transaction."""
        tx_bytes = base64.b64decode(raw_tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        encoded = base64.b64encode(bytes(signed_tx)).decode("utf-8")

        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [encoded, {"encoding": "base64", "skipPreflight": True,
                                  "preflightCommitment": "confirmed", "maxRetries": 3}],
        }
        resp = requests.post(self.cfg.SOLANA_RPC_URL, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"RPC error: {result['error']}")
        return result["result"]

    def confirm_transaction(self, sig: str, max_wait: int = 120) -> bool:
        """Poll until confirmed, finalized, or timeout. Handles RPC timeouts gracefully."""
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [[sig], {"searchTransactionHistory": True}],
                }
                resp = requests.post(self.cfg.SOLANA_RPC_URL, json=payload, timeout=20)
                resp.raise_for_status()
                data = resp.json().get("result", {}).get("value", [None])[0]
                if data:
                    if data.get("err"):
                        log.error(f"TX failed on-chain: {data['err']}")
                        return False
                    if data.get("confirmationStatus") in ("confirmed", "finalized"):
                        log.info(f"   TX confirmed: {sig[:20]}...")
                        return True
            except Exception as e:
                log.warning(f"   TX confirmation check failed (will retry): {e}")
            time.sleep(4)
        # Timed out waiting for confirmation — but the TX may still have
        # landed. Log the signature so it can be checked manually on Solscan.
        log.warning(
            f"   TX confirmation timed out after {max_wait}s. "
            f"Check manually: https://solscan.io/tx/{sig}"
        )
        return False


# ════════════════════════════════════════════════════════════════════════════════
# PRICE ORACLE
# ════════════════════════════════════════════════════════════════════════════════
class PriceOracle:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.dex = DexScreenerClient()
        self._decimals_cache: dict = {}

    def get_token_decimals(self, token_mint: str) -> int:
        """Fetch actual on-chain decimals for a mint. Cached per run."""
        if token_mint in self._decimals_cache:
            return self._decimals_cache[token_mint]
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getAccountInfo",
                "params": [token_mint, {"encoding": "jsonParsed"}],
            }
            resp = requests.post(self.cfg.SOLANA_RPC_URL, json=payload, timeout=8)
            resp.raise_for_status()
            info = resp.json().get("result", {}).get("value", {})
            decimals = info.get("data", {}).get("parsed", {}).get("info", {}).get("decimals")
            if decimals is not None:
                self._decimals_cache[token_mint] = int(decimals)
                return int(decimals)
        except Exception as e:
            log.debug(f"Could not fetch decimals for {token_mint[:8]}: {e}")
        # Safe fallback — most pump.fun tokens use 6 decimals
        self._decimals_cache[token_mint] = 6
        return 6

    def get_price_and_liquidity(self, token_mint: str) -> tuple[float, float | None]:
        """Return (price_usd, liquidity_usd) for a token.

        Primary source: DexScreener — same source already used for token
        discovery in token_scanner.py, so it's consistent and (unlike the
        old Birdeye call, which had no API key configured and was silently
        failing every request) it actually works and gives us real pool
        liquidity, not just a price.

        liquidity_usd is None only if DexScreener couldn't be reached at all
        (network error) — as opposed to 0.0, which means DexScreener *was*
        reached and reports the pool has no liquidity. Callers should treat
        None cautiously (retry) and 0.0/low values as a real dead-pool signal.
        """
        try:
            pairs = self.dex.get_pairs_for_mint(token_mint)
            sol_pairs = [
                p for p in pairs
                if p.get("chainId") == "solana"
                and (p.get("baseToken", {}).get("address") or "").lower() == token_mint.lower()
            ]
            if sol_pairs:
                best = max(sol_pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
                price = float(best.get("priceUsd") or 0)
                liquidity = float(best.get("liquidity", {}).get("usd") or 0)
                if price > 0:
                    return price, liquidity
                return 0.0, liquidity
        except Exception as e:
            log.debug(f"DexScreener price lookup failed for {token_mint[:8]}: {e}")

        # Fallback: Jupiter quote — only used if DexScreener has no pair data
        # at all (e.g. brand new pool not yet indexed). Liquidity is unknown
        # here, so we return None for it rather than pretending it's healthy.
        try:
            jup = JupiterClient(self.cfg)
            quote = jup.get_quote(self.cfg.INPUT_MINT, token_mint, usdc_to_lamports(1.0))
            out_amount      = int(quote["outAmount"])
            token_decimals  = self.get_token_decimals(token_mint)
            tokens_per_usdc = out_amount / (10 ** token_decimals)
            price = 1.0 / tokens_per_usdc if tokens_per_usdc else 0.0

            if price > 0 and (price > 1_000_000 or price < 1e-15):
                log.warning(f"   ⚠️  Implausible price for {token_mint[:8]}: ${price}. Discarding.")
                return 0.0, None
            return price, None
        except Exception as e:
            log.error(f"Jupiter price fallback failed: {e}")
            return 0.0, None

    def get_price_usdc(self, token_mint: str) -> float:
        """Backwards-compatible price-only accessor."""
        price, _ = self.get_price_and_liquidity(token_mint)
        return price


# ════════════════════════════════════════════════════════════════════════════════
# JOURNAL
# ════════════════════════════════════════════════════════════════════════════════
class Journal:
    FILE     = "journal.json"
    CSV_FILE = "trade_log.csv"
    CSV_HEADERS = [
        "datetime", "cycle", "token", "token_mint", "play_amount",
        "winnings", "next_play_amt", "saved", "total_saved",
        "total_winnings", "win", "reason", "exit_price", "dex_url",
    ]

    def __init__(self):
        self.data = self._load()
        self._init_csv()

    def _init_csv(self):
        """Create CSV with headers if it doesn't exist yet."""
        if not os.path.exists(self.CSV_FILE):
            with open(self.CSV_FILE, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.CSV_HEADERS)

    def append_csv(self, row: dict):
        """Append one completed cycle to the CSV log."""
        with open(self.CSV_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                row.get("datetime", ""),
                row.get("cycle", ""),
                row.get("token", ""),
                row.get("token_mint", ""),
                f"{row.get('play_amount', 0):.6f}",
                f"{row.get('winnings', 0):.6f}",
                f"{row.get('next_play', 0):.6f}",
                f"{row.get('saved', 0):.6f}",
                f"{row.get('total_saved', 0):.6f}",
                f"{row.get('total_winnings', 0):.6f}",
                row.get("win", ""),
                row.get("reason", ""),
                f"{row.get('exit_price', 0):.10f}",
                row.get("dex_url", ""),
            ])

    def _load(self) -> dict:
        if os.path.exists(self.FILE):
            with open(self.FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if "consec_losses" not in data:
                data["consec_losses"] = 0
            return data
        return {
            "cycle": 0,
            "play_amount_usdc": None,
            "total_saved_usdc": 0.0,
            "total_winnings_usdc": 0.0,
            "open_position": None,
            "consec_losses": 0,
            "history": [],
        }

    def save(self):
        with open(self.FILE, "w") as f:
            json.dump(self.data, f, indent=2)

    def log_cycle(self, cycle_data: dict):
        ts = datetime.now().astimezone().isoformat()
        self.data["history"].append({**cycle_data, "ts": ts})
        self.save()
        self.append_csv({**cycle_data, "datetime": ts})


# ════════════════════════════════════════════════════════════════════════════════
# MAIN BOT
# ════════════════════════════════════════════════════════════════════════════════
class CompoundingBot:
    def __init__(self, cfg: Config):
        self.cfg    = cfg
        self.calc   = StrategyCalc(cfg)
        self.jup    = JupiterClient(cfg)
        self.oracle = PriceOracle(cfg)
        self.j      = Journal()
        self.scanner = TokenScanner(ScannerConfig())

        if self.cfg.DRY_RUN:
            self.keypair = None
            self.pubkey = "DRY_RUN"
            log.info("Wallet disabled for dry-run mode; no live key material required.")
        else:
            if not cfg.WALLET_PRIVATE_KEY:
                raise ValueError("WALLET_PRIVATE_KEY not set in .env")
            pk_bytes = base58.b58decode(cfg.WALLET_PRIVATE_KEY)
            self.keypair = Keypair.from_bytes(pk_bytes)
            self.pubkey = str(self.keypair.pubkey())
            log.info(f"Wallet loaded: {self.pubkey}")

        if self.j.data["play_amount_usdc"] is None:
            self.j.data["play_amount_usdc"] = cfg.STARTING_AMOUNT_USDC
            self.j.save()

    # ── Balance checks ────────────────────────────────────────────────────────
    def get_usdc_balance(self, retries: int = 4, delay: int = 5) -> float:
        if self.cfg.DRY_RUN:
            return 9999.0
        for attempt in range(1, retries + 1):
            try:
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [self.pubkey, {"mint": self.cfg.INPUT_MINT}, {"encoding": "jsonParsed"}],
                }
                resp = requests.post(self.cfg.SOLANA_RPC_URL, json=payload, timeout=15)
                resp.raise_for_status()
                accounts = resp.json().get("result", {}).get("value", [])
                if not accounts:
                    return 0.0
                return float(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
            except Exception as e:
                if attempt < retries:
                    log.warning(f"USDC balance fetch failed (attempt {attempt}/{retries}): {e} — retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    log.warning(f"USDC balance fetch failed after {retries} attempts: {e}")
        return 0.0

    def get_sol_balance(self, retries: int = 4, delay: int = 5) -> float:
        if self.cfg.DRY_RUN:
            return 9999.0
        for attempt in range(1, retries + 1):
            try:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [self.pubkey]}
                resp = requests.post(self.cfg.SOLANA_RPC_URL, json=payload, timeout=15)
                resp.raise_for_status()
                return resp.json().get("result", {}).get("value", 0) / 1e9
            except Exception as e:
                if attempt < retries:
                    log.warning(f"SOL balance fetch failed (attempt {attempt}/{retries}): {e} — retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    log.warning(f"SOL balance fetch failed after {retries} attempts: {e}")
        return 0.0

    def check_balances(self, required_usdc: float) -> bool:
        usdc = self.get_usdc_balance()
        sol  = self.get_sol_balance()
        log.info(f"💳 Wallet — USDC: ${usdc:.4f}  |  SOL: {sol:.4f}")

        # If both came back as 0.0 it almost certainly means the RPC
        # timed out on both calls (not that the wallet is genuinely empty),
        # since a wallet with $0 USDC and $0 SOL would not be running a
        # live bot. Treat this as a transient RPC failure and keep going
        # rather than halting the bot — the buy itself will fail cleanly
        # if funds truly aren't there.
        if usdc == 0.0 and sol == 0.0:
            log.warning(
                "⚠️  Both USDC and SOL returned 0.0 — likely an RPC timeout "
                "rather than an empty wallet. Proceeding cautiously; the buy "
                "transaction itself will verify funds on-chain."
            )
            return True

        if usdc < required_usdc:
            log.error(f"❌ Insufficient USDC: have ${usdc:.4f}, need ${required_usdc:.4f}. Top up and restart.")
            return False
        if sol < 0.0001:
            log.error(f"❌ SOL too low ({sol:.4f}). Need at least 0.01 SOL for gas. Top up and restart.")
            return False
        if sol < 0.005:
            log.warning(f"⚠️  Low SOL ({sol:.4f}). Recommend topping up.")
        return True

    # ── Buy ───────────────────────────────────────────────────────────────────
    def execute_buy(self, usdc_amount: float, token: TokenCandidate) -> dict | None:
        if usdc_amount <= 0:
            raise ValueError("Buy amount must be positive")
        if token is None or not getattr(token, "mint", None):
            raise ValueError("Token candidate is missing a mint address")

        log.info(f"📥 BUY  ${usdc_amount:.4f} USDC → {token.symbol} ({token.mint[:8]}...)")

        amount_lam = usdc_to_lamports(usdc_amount)
        quote = self.jup.get_quote(self.cfg.INPUT_MINT, token.mint, amount_lam)
        out_amount = int(quote["outAmount"])
        token_decimals = self.oracle.get_token_decimals(token.mint)
        token_amount = out_amount / (10 ** token_decimals)
        if token_amount <= 0:
            log.error(f"   Quote produced zero tokens for {token.symbol}; aborting.")
            return None
        entry_price = usdc_amount / token_amount if token_amount else 0
        target_price = entry_price * self.cfg.PRICE_TARGET_MULT
        stop_price = entry_price * self.cfg.STOP_LOSS_MULT if self.cfg.STOP_LOSS_MULT > 0 else 0

        log.info(f"   Got:    {token_amount:.6f} {token.symbol}")
        log.info(f"   Entry:  ${entry_price:.8f}")
        log.info(f"   Target: ${target_price:.8f}  ({self.cfg.PRICE_TARGET_MULT}x)")
        log.info(f"   Stop:   {'off' if stop_price == 0 else f'${stop_price:.8f}'}")

        if self.cfg.DRY_RUN:
            log.warning("   [DRY RUN] Skipping real transaction.")
            sig = "DRY_RUN_BUY"
        else:
            raw_tx = self.jup.build_swap_transaction(quote, self.pubkey)
            sig = self.jup.sign_and_send(raw_tx, self.keypair)
            log.info(f"   TX: https://solscan.io/tx/{sig}")
            if not self.jup.confirm_transaction(sig):
                log.error("Buy TX not confirmed — aborting cycle.")
                return None

        position = {
            "buy_tx": sig,
            "usdc_spent": usdc_amount,
            "token_amount": token_amount,
            "token_decimals": token_decimals,
            "token_symbol": token.symbol,
            "entry_price": entry_price,
            "target_price": target_price,
            "stop_price": stop_price,
            "token_mint": token.mint,
            "dex_url": token.url,
        }
        self.j.data["open_position"] = position
        self.j.save()
        return position

    # ── Sell ──────────────────────────────────────────────────────────────────
    def execute_sell(self, position: dict, reason: str = "target") -> float | None:
        token_amount = position["token_amount"]
        token_mint = position["token_mint"]
        token_decimals = position.get("token_decimals", 6)
        symbol = position.get("token_symbol", "TOKEN")
        usdc_spent = position.get("usdc_spent", 0)

        if token_amount <= 0:
            raise ValueError("Sell amount must be positive")

        log.info(f"📤 SELL {token_amount:.6f} {symbol} → USDC ({reason})")

        amount_lam = int(token_amount * (10 ** token_decimals))

        if reason == "zero_liquidity":
            log.warning("   Pool liquidity is effectively $0 — probing Jupiter once for an alternate route before giving up...")
            try:
                probe_quote = self.jup.get_quote(
                    token_mint, self.cfg.INPUT_MINT, amount_lam, slippage_bps=2000
                )
                usdc_out = int(probe_quote["outAmount"]) / (10 ** USDC_DECIMALS)
                log.info(f"   ✅ Found a route after all — quote: ${usdc_out:.4f} USDC. Proceeding to sell.")
                slippage_schedule = [2000]
            except Exception as e:
                log.error(f"   ❌ No route found via Jupiter either: {e}")
                log.error(
                    f"   🪦 WRITE-OFF: {symbol} ({token_mint}) — pool is dead, "
                    f"${usdc_spent:.4f} USDC is stuck and cannot be sold programmatically. "
                    f"Verify manually at {position.get('dex_url', 'dexscreener.com')} "
                    f"or https://jup.ag/swap/{token_mint}-{self.cfg.INPUT_MINT}"
                )
                position["dead_pool"] = True
                self.j.data["open_position"] = position
                self.j.save()
                return None
        elif reason == "stop_loss":
            slippage_schedule = [
                self.cfg.SLIPPAGE_BPS,
                500,
                1000,
                3000,
                5000,
            ]
        else:
            slippage_schedule = [
                self.cfg.SLIPPAGE_BPS,
                300,
                500,
            ]

        for attempt, slippage in enumerate(slippage_schedule, 1):
            log.info(f"   Attempt {attempt}/{len(slippage_schedule)} (slippage={slippage}bps = {slippage/100:.1f}%)")
            try:
                quote = self.jup.get_quote(
                    token_mint, self.cfg.INPUT_MINT, amount_lam,
                    slippage_bps=slippage
                )
            except Exception as e:
                log.error(f"   ❌ Sell quote failed: {e}")
                if attempt < len(slippage_schedule):
                    time.sleep(2)
                    continue
                log.error("   Position left open for manual review.")
                return None

            usdc_out = int(quote["outAmount"]) / (10 ** USDC_DECIMALS)
            log.info(f"   Quote: ${usdc_out:.4f} USDC")

            if reason == "target" and usdc_spent > 0 and attempt == 1:
                if usdc_out < usdc_spent * 0.50:
                    log.warning(
                        f"   ⚠️  Quote ${usdc_out:.4f} < 50% of entry. "
                        f"Retrying in {self.cfg.POLL_INTERVAL_SEC}s..."
                    )
                    time.sleep(self.cfg.POLL_INTERVAL_SEC)
                    continue

            if self.cfg.DRY_RUN:
                log.warning("   [DRY RUN] Skipping real transaction.")
                self.j.data["open_position"] = None
                self.j.save()
                return usdc_out

            try:
                raw_tx = self.jup.build_swap_transaction(quote, self.pubkey)
                sig = self.jup.sign_and_send(raw_tx, self.keypair)
                log.info(f"   TX: https://solscan.io/tx/{sig}")
            except RuntimeError as e:
                log.error(f"   ❌ Send failed: {e}")
                if attempt < len(slippage_schedule):
                    log.info("   Retrying with higher slippage...")
                    time.sleep(2)
                    continue
                log.error("   All attempts exhausted. Sell manually on jup.ag.")
                return None

            if self.jup.confirm_transaction(sig):
                self.j.data["open_position"] = None
                self.j.save()
                return usdc_out
            log.error(f"   Sell not confirmed (attempt {attempt})")
            if attempt < len(slippage_schedule):
                time.sleep(2)
                continue
            log.error("   All attempts failed. Sell manually on jup.ag.")
            return None

        log.error("   Sell failed after all attempts. Sell manually on jup.ag.")
        return None

    # ── Monitor ───────────────────────────────────────────────────────────────
    def monitor_price(self, position: dict) -> tuple:
        target = position["target_price"]
        stop   = position["stop_price"]
        mint   = position["token_mint"]
        symbol = position.get("token_symbol", mint[:8])
        log.info(f"👁  Monitoring {symbol} | Target=${target:.8f} | Stop={'off' if stop==0 else f'${stop:.8f}'}")

        # ── Stop-loss confirmation guard ──────────────────────────────────────
        # A stop-loss only fires after CONFIRM_TICKS consecutive readings are
        # at or below the stop price. This protects against two distinct
        # failure modes seen in real logs:
        #   1. A single bad/stale price read right after buying (e.g. a stop
        #      firing 1.2s after entry with zero real price history — almost
        #      certainly an oracle glitch, not a real market move).
        #   2. A genuine but extremely brief wick that recovers immediately
        #      (low-liquidity tokens can spike a single bad print for one
        #      tick due to thin order books, then bounce back).
        # Genuine gap-through dumps (price actually crashing and staying
        # down) still get caught — they simply confirm on the very next tick
        # since the price stays low, costing at most one extra poll interval.
        CONFIRM_TICKS = 2

        # ── Buy warmup ─────────────────────────────────────────────────────────
        # Ignore stop-loss entirely for the first few seconds after a buy.
        # This is specifically to stop the "instant stop-loss" pattern seen
        # in logs (QUIVER #2: stop fired 1.2s after entry with no real price
        # history) — the oracle needs at least one real settled reading
        # before we trust it enough to act on a loss.
        BUY_WARMUP_SEC = 6

        start_time = time.time()
        stop_confirm_count = 0
        last_good_price = position["entry_price"]

        # ── Relative jump sanity check ──────────────────────────────────────
        # Rejects single-tick price jumps beyond MAX_PLAUSIBLE_TICK_JUMP.
        # Set to 100x (not 20x) — genuine rugs CAN drop 95%+ in one tick,
        # which is a 20x drop. The original 20x threshold was incorrectly
        # blocking real crashes (the 🫵 token dropped 98% and the bot refused
        # to sell for hours, stuck in an infinite discard loop).
        # The check is still valuable for catching true oracle bugs (like
        # the CPU $56 reading) but must not be so tight it blocks real dumps.
        MAX_PLAUSIBLE_TICK_JUMP = 100.0

        # Safety valve: if we've discarded more than MAX_DISCARDS consecutive
        # readings as "implausible", the price has genuinely moved to a new
        # level and we should accept the new reality rather than holding forever.
        # This prevents the infinite-loop-on-rug scenario entirely.
        MAX_DISCARDS = 5
        discard_count = 0

        while True:
            price, liquidity_usd = self.oracle.get_price_and_liquidity(mint)
            elapsed = time.time() - start_time

            if price <= 0:
                log.warning("   Price fetch returned 0 — retrying...")
                time.sleep(self.cfg.POLL_INTERVAL_SEC)
                continue

            # ── Dead-pool guard ──────────────────────────────────────────────
            # A pool with (near) zero liquidity can never be sold into,
            # regardless of what price is quoted for it — small-size probe
            # quotes against an empty pool can report wildly distorted
            # prices (this is what caused the $210 misprice on Bool). Catch
            # it here, before any target/stop logic, and stop monitoring —
            # there is nothing further to learn by continuing to poll.
            if liquidity_usd is not None and liquidity_usd < self.cfg.MIN_SELLABLE_LIQUIDITY_USD:
                log.error(
                    f"   🚨 {symbol} liquidity is ${liquidity_usd:,.2f} "
                    f"(below ${self.cfg.MIN_SELLABLE_LIQUIDITY_USD:.0f} sellable floor). "
                    f"Pool cannot be sold into programmatically."
                )
                return "zero_liquidity", price

            # Reject implausible single-tick jumps relative to the last
            # confirmed-good price, rather than trusting any value under
            # the old absolute $1,000,000 ceiling.
            jump_ratio = price / last_good_price if last_good_price > 0 else 1
            if jump_ratio > MAX_PLAUSIBLE_TICK_JUMP or jump_ratio < (1 / MAX_PLAUSIBLE_TICK_JUMP):
                discard_count += 1
                if discard_count >= MAX_DISCARDS:
                    log.warning(
                        f"   ⚠️  Implausible price discarded {discard_count}x in a row "
                        f"(${last_good_price:.8f} → ${price:.8f}, {jump_ratio:.1f}x). "
                        f"Accepting new price as real — token may have genuinely crashed."
                    )
                    # Accept the new price level and reset
                    last_good_price = price
                    discard_count = 0
                else:
                    log.warning(
                        f"   ⚠️  Implausible price jump for {symbol}: "
                        f"${last_good_price:.8f} → ${price:.8f} "
                        f"({jump_ratio:.1f}x in one tick) — discarding ({discard_count}/{MAX_DISCARDS})"
                    )
                time.sleep(self.cfg.POLL_INTERVAL_SEC)
                continue

            pct = ((price - position["entry_price"]) / position["entry_price"]) * 100
            log.info(f"   {symbol}: ${price:.8f}  ({pct:+.1f}%)")

            if price >= target:
                return "target", price

            if stop > 0 and price <= stop:
                if elapsed < BUY_WARMUP_SEC:
                    log.warning(
                        f"   ⏳ Stop-loss level breached during {BUY_WARMUP_SEC}s "
                        f"warmup window ({elapsed:.1f}s in) — ignoring as likely "
                        f"stale read, not acting yet."
                    )
                else:
                    stop_confirm_count += 1
                    if stop_confirm_count >= CONFIRM_TICKS:
                        log.info(
                            f"   🛑 Stop-loss confirmed ({stop_confirm_count}/"
                            f"{CONFIRM_TICKS} consecutive readings at/below stop)"
                        )
                        return "stop_loss", price
                    else:
                        log.warning(
                            f"   ⚠️  Price at/below stop ({stop_confirm_count}/"
                            f"{CONFIRM_TICKS} confirmations) — confirming on next tick"
                        )
            else:
                # Price recovered above stop — reset confirmation counter.
                # This is what protects against single-tick wicks that
                # bounce back (like QUIVER #1's -17.1% dip that recovered
                # to +1.2% before the real crash).
                if stop_confirm_count > 0:
                    log.info(f"   ✅ Price recovered above stop — resetting confirmation counter")
                stop_confirm_count = 0

            last_good_price = price
            time.sleep(self.cfg.POLL_INTERVAL_SEC)

    # ── Cycle ─────────────────────────────────────────────────────────────────
    def run_cycle(self) -> bool:
        play_amount = self.j.data["play_amount_usdc"]
        if play_amount is None:
            play_amount = self.cfg.STARTING_AMOUNT_USDC
            self.j.data["play_amount_usdc"] = play_amount

        if play_amount < self.cfg.MIN_PLAY_AMOUNT and not self.j.data.get("open_position"):
            log.warning(
                f"⚠️  Play amount ${play_amount:.6f} below floor "
                f"${self.cfg.MIN_PLAY_AMOUNT:.2f} — resetting to "
                f"${self.cfg.STARTING_AMOUNT_USDC:.2f}"
            )
            play_amount = self.cfg.STARTING_AMOUNT_USDC
            self.j.data["play_amount_usdc"] = play_amount
            self.j.data["consec_losses"] = 0
            self.j.save()

        consec = self.j.data.get("consec_losses", 0)
        if consec >= self.cfg.MAX_CONSEC_LOSSES and not self.j.data.get("open_position"):
            log.warning(
                f"⏸  {consec} consecutive losses — pausing "
                f"{self.cfg.LOSS_PAUSE_SEC}s before next scan"
            )
            time.sleep(self.cfg.LOSS_PAUSE_SEC)
            self.j.data["consec_losses"] = 0
            self.j.save()

        cycle = self.j.data["cycle"] + 1
        log.info(f"\n{'='*60}")
        log.info(f"  CYCLE {cycle} | Playing ${play_amount:.4f} USDC | "
                 f"Consec losses: {self.j.data.get('consec_losses', 0)}")
        log.info(f"{'='*60}")

        position = self.j.data.get("open_position")

        if not position:
            if self.cfg.DRY_RUN:
                log.info("🔍 Dry-run mode: scanning without checking wallet balances.")
            elif not self.check_balances(play_amount):
                log.error("❌ Insufficient funds. Top up wallet and restart.")
                sys.exit(1)

            if self.cfg.TOKEN_MODE == "scan":
                log.info("🔍 Scanning for best new token launch...")
                token = self.scanner.find_best(max_wait_minutes=self.cfg.SCAN_MAX_WAIT_MIN)
                if token is None:
                    log.warning("No qualifying token found. Skipping cycle.")
                    return False
            else:
                token = TokenCandidate(
                    mint=self.cfg.FIXED_OUTPUT_MINT,
                    symbol="TOKEN", name="Fixed", dex="jupiter",
                    pair_address="", price_usd=0, liquidity_usd=0,
                    volume_5m=0, volume_1h=0, price_change_5m=0,
                    price_change_1h=0, market_cap=0,
                    txns_5m_buys=0, txns_5m_sells=0, age_minutes=0,
                )

            position = self.execute_buy(play_amount, token)
            if not position:
                return False

        reason, exit_price = self.monitor_price(position)
        usdc_received = self.execute_sell(position, reason)
        if usdc_received is None:
            return False

        win = reason == "target"
        if win:
            next_play = usdc_received * self.cfg.REINVEST_PCT
            saved = usdc_received * self.cfg.SAVE_PCT
            self.j.data["consec_losses"] = 0
        else:
            next_play = usdc_received
            saved = 0.0
            self.j.data["consec_losses"] = self.j.data.get("consec_losses", 0) + 1

        self.j.data["cycle"] = cycle
        self.j.data["play_amount_usdc"] = next_play
        self.j.data["total_saved_usdc"] += saved
        self.j.data["total_winnings_usdc"] += usdc_received

        pct = (usdc_received / play_amount - 1) * 100 if play_amount > 0 else 0
        self.j.log_cycle({
            "cycle": cycle, "token": position.get("token_symbol", "?"),
            "token_mint": position.get("token_mint", ""),
            "dex_url": position.get("dex_url", ""),
            "play_amount": play_amount, "winnings": usdc_received,
            "next_play": next_play, "saved": saved,
            "total_saved": self.j.data["total_saved_usdc"],
            "total_winnings": self.j.data["total_winnings_usdc"],
            "win": win, "reason": reason, "exit_price": exit_price,
        })

        icon = "✅" if win else "❌"
        log.info(f"\n  {icon} Cycle {cycle} ({reason.upper()}) — {position.get('token_symbol','?')}")
        log.info(f"     Played:     ${play_amount:.4f} USDC")
        log.info(f"     Received:   ${usdc_received:.4f} USDC  ({pct:+.1f}%)")
        log.info(f"     Saved:      ${saved:.4f} USDC  (total: ${self.j.data['total_saved_usdc']:.4f})")
        log.info(f"     Next play:  ${next_play:.4f} USDC")
        log.info(f"     Consec losses: {self.j.data['consec_losses']}")
        return True

    # ── Run ───────────────────────────────────────────────────────────────────
    def run(self):
        log.info("🚀 CryptoBot starting...")
        if self.cfg.DRY_RUN:
            log.warning("⚠️  DRY RUN MODE — no real transactions will be sent")

        self._print_projection()

        # Startup balance display
        usdc = self.get_usdc_balance()
        sol  = self.get_sol_balance()
        if self.cfg.DRY_RUN:
            log.info(f"💳 Wallet — USDC: ${usdc:.4f}  |  SOL: {sol:.4f} (dry run — not enforced)")
        else:
            if not self.check_balances(self.j.data["play_amount_usdc"]):
                log.error("Fix wallet balance before running live.")
                sys.exit(1)

        while self.j.data["cycle"] < self.cfg.MAX_CYCLES:
            ok = self.run_cycle()
            if not ok:
                open_pos = self.j.data.get("open_position") or {}
                if open_pos.get("dead_pool"):
                    log.error(
                        f"⏸  Position in {open_pos.get('token_symbol','?')} is a confirmed "
                        f"dead pool — ${open_pos.get('usdc_spent', 0):.4f} USDC stuck. "
                        f"Manual action required. Rechecking in "
                        f"{self.cfg.DEAD_POOL_RECHECK_SEC}s (in case liquidity ever returns)."
                    )
                    time.sleep(self.cfg.DEAD_POOL_RECHECK_SEC)
                else:
                    log.error("Cycle failed. Pausing 60s before retry...")
                    time.sleep(60)

        log.info("\n🏁 All cycles complete!")
        log.info(f"   Total saved:    ${self.j.data['total_saved_usdc']:.2f} USDC")
        log.info(f"   Total winnings: ${self.j.data['total_winnings_usdc']:.2f} USDC")

    def _print_projection(self):
        rows = self.calc.project(self.cfg.MAX_CYCLES)
        log.info("\n📊 Projected compounding (assumes 2x every cycle):")
        log.info(f"  {'Day':>4}  {'Play ($)':>12}  {'Win ($)':>12}  {'Saved ($)':>12}  {'Total Saved':>14}")
        for r in rows[:10]:
            log.info(f"  {r['day']:>4}  {r['play_amount']:>12.2f}  {r['winnings']:>12.2f}  {r['saved']:>12.2f}  {r['total_saved']:>14.2f}")
        if len(rows) > 10:
            last = rows[-1]
            log.info(f"  ...  (day {last['day']}: play=${last['play_amount']:,.2f}, total saved=${last['total_saved']:,.2f})")


# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    cfg = Config()
    if not cfg.DRY_RUN and not cfg.WALLET_PRIVATE_KEY:
        print("ERROR: Set WALLET_PRIVATE_KEY in .env before disabling DRY_RUN")
        sys.exit(1)

    bot = CompoundingBot(cfg)
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("\n⏹  Bot stopped by user. State saved in journal.json")
