"""
Jupiter.ag Compounding Strategy Bot
====================================
Strategy: Scan for new token launches → buy via Jupiter → poll price
→ sell at 2x → save 40% → reinvest 60% → repeat.

Requirements: pip install solders requests python-dotenv base58 websockets
"""

import os
import math
import sys
import time
import json
import csv
import base64
import shutil
import logging
import requests
import base58
from datetime import datetime
from decimal import Decimal
from dotenv import load_dotenv

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from token_scanner import TokenScanner, ScannerConfig, TokenCandidate, DexScreenerClient
from chain import (
    RpcClient, RpcError, PumpPortalLocalClient, WSOL_MINT,
    bonding_curve_address, parse_bonding_curve,
)

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


def parse_bool_env(value: str, name: str = "DRY_RUN") -> bool:
    """Parse an explicit boolean environment value; reject ambiguous input."""
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"Invalid {name} value {value!r}; use true/false, 1/0, yes/no, or on/off"
    )


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
    MAX_CYCLES:            int   = int(os.getenv("MAX_CYCLES", "0"))  # 0 = run until Ctrl+C
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
    DRY_RUN:               bool  = parse_bool_env(os.getenv("DRY_RUN", "true"))

    # ── Live-trading safety gate (only consulted when DRY_RUN is false) ───────
    # Kept as raw strings so a bad value is reported by validate_live_config()
    # instead of crashing at import time.
    LIVE_TRADING_CONFIRM:  str   = os.getenv("LIVE_TRADING_CONFIRM", "")
    MAX_POSITION_USDC:     str   = os.getenv("MAX_POSITION_USDC", "")
    MAX_DAILY_LOSS_USDC:   str   = os.getenv("MAX_DAILY_LOSS_USDC", "")
    KILL_SWITCH_FILE:      str   = os.getenv("KILL_SWITCH_FILE", "KILL_SWITCH")

    # ── Pump.fun bonding-curve route (live only; off unless explicitly enabled)
    CURVE_TRADING:         bool  = parse_bool_env(os.getenv("CURVE_TRADING", "false"), "CURVE_TRADING")
    CURVE_BUY_SLIPPAGE_PCT: float = float(os.getenv("CURVE_BUY_SLIPPAGE_PCT", "15"))
    CURVE_SELL_SLIPPAGES_PCT: tuple = (15.0, 30.0, 50.0)
    CURVE_PRIORITY_FEE_SOL: float = float(os.getenv("CURVE_PRIORITY_FEE_SOL", "0.0001"))
    MIN_SOL_FOR_FEES:      float = float(os.getenv("MIN_SOL_FOR_FEES", "0.01"))
    MIN_SOL_FOR_SELL:      float = float(os.getenv("MIN_SOL_FOR_SELL", "0.003"))

    # ── Transaction settlement ────────────────────────────────────────────────
    # A blockhash is valid for ~150 blocks; a signature with no status is only
    # declared failed after the chain passes that window (never before).
    BLOCKHASH_VALIDITY_BLOCKS: int = 150
    CONFIRM_POLL_SEC:      float = float(os.getenv("CONFIRM_POLL_SEC", "2"))
    CONFIRM_MAX_WAIT_SEC:  float = float(os.getenv("CONFIRM_MAX_WAIT_SEC", "90"))

    # ── Liquidity / dead-pool handling ────────────────────────────────────────
    # Below this, a pool is considered unsellable (no real market depth) —
    # we stop hammering Jupiter for quotes and alert instead of retrying.
    MIN_SELLABLE_LIQUIDITY_USD: float = float(os.getenv("MIN_SELLABLE_LIQUIDITY_USD", "25"))
    # Once a position is confirmed dead-pool, back off hard between rechecks
    # instead of retrying every 60s forever.
    DEAD_POOL_RECHECK_SEC:      int   = int(os.getenv("DEAD_POOL_RECHECK_SEC", "600"))
    # If no usable price is seen for this long while holding a position, give
    # up waiting and attempt an exit instead of polling forever. 0 disables.
    MAX_NO_PRICE_SEC:           int   = int(os.getenv("MAX_NO_PRICE_SEC", "120"))


# ════════════════════════════════════════════════════════════════════════════════
# LIVE-TRADING SAFETY GATE
# ════════════════════════════════════════════════════════════════════════════════
LIVE_CONFIRM_PHRASE = "I UNDERSTAND THIS USES REAL MONEY AND I CAN LOSE ALL OF IT"
# Upper sanity bounds for operator-supplied limits. A larger value is treated
# as a likely typo and rejected rather than silently accepted.
LIVE_MAX_POSITION_CEILING_USDC = 100.0
LIVE_MAX_DAILY_LOSS_CEILING_USDC = 500.0


class LiveTradingConfigError(RuntimeError):
    """Live trading was requested without valid, complete safeguards."""


class JournalCorruptError(RuntimeError):
    """The journal could not be read and live mode refuses to guess state."""


def _parse_limit(value, name: str, ceiling: float, problems: list) -> float | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        problems.append(f"{name} is required for live trading")
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        problems.append(f"{name} is not a valid number")
        return None
    if not math.isfinite(number) or number <= 0:
        problems.append(f"{name} must be a finite number greater than 0")
        return None
    if number > ceiling:
        problems.append(f"{name} exceeds the safety ceiling of {ceiling:g} USDC")
        return None
    return number


def validate_live_config(cfg: Config) -> None:
    """Fail closed unless every live-trading safeguard is present and valid.

    A no-op in dry-run. Messages never include the wallet key or the entered
    confirmation text.
    """
    if cfg.DRY_RUN:
        return
    problems: list = []
    if (cfg.LIVE_TRADING_CONFIRM or "") != LIVE_CONFIRM_PHRASE:
        problems.append("LIVE_TRADING_CONFIRM does not exactly match the required phrase")
    max_position = _parse_limit(
        cfg.MAX_POSITION_USDC, "MAX_POSITION_USDC",
        LIVE_MAX_POSITION_CEILING_USDC, problems)
    _parse_limit(
        cfg.MAX_DAILY_LOSS_USDC, "MAX_DAILY_LOSS_USDC",
        LIVE_MAX_DAILY_LOSS_CEILING_USDC, problems)
    if not str(cfg.KILL_SWITCH_FILE or "").strip():
        problems.append("KILL_SWITCH_FILE must be set")
    if not cfg.WALLET_PRIVATE_KEY:
        problems.append("WALLET_PRIVATE_KEY is required for live trading")
    if max_position is not None and cfg.STARTING_AMOUNT_USDC > max_position:
        problems.append("STARTING_AMOUNT_USDC exceeds MAX_POSITION_USDC")
    if problems:
        raise LiveTradingConfigError(
            "Live trading refused: " + "; ".join(problems))


def kill_switch_active(cfg: Config) -> bool:
    """True when the kill-switch file exists. A missing path setting counts as active."""
    path = str(cfg.KILL_SWITCH_FILE or "").strip()
    return True if not path else os.path.exists(path)


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


# Stop-breach classification (used by CompoundingBot.monitor_price).
SUSTAINED_DROP_PCT = 15.0   # breach tick must be at least this far under the recent average
PRIOR_DECLINE_PCT = 5.0     # previous tick must already be this far under the readings before it


def is_sustained_crash(recent_prices: list, price: float) -> bool:
    """True only when the price was already falling before this tick AND this
    tick is >= SUSTAINED_DROP_PCT below the recent average. A single anomalous
    tick from a flat baseline is not sustained, however large the jump is.
    Comparisons are cross-multiplied so exact boundary values are not lost to
    float division error."""
    if len(recent_prices) < 2:
        return False
    prior = recent_prices[-1]
    baseline = sum(recent_prices[:-1]) / (len(recent_prices) - 1)
    average = sum(recent_prices) / len(recent_prices)
    if baseline <= 0 or average <= 0:
        return False
    already_falling = prior * 100.0 <= baseline * (100.0 - PRIOR_DECLINE_PCT)
    return already_falling and price * 100.0 <= average * (100.0 - SUSTAINED_DROP_PCT)


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

    def sign_transaction(self, raw_tx_b64: str, keypair: Keypair) -> tuple:
        """Sign locally; returns (signed_b64, signature). Never touches the network.

        The signature is known before sending, so it can be journaled first.
        """
        if self.cfg.DRY_RUN:
            raise RuntimeError("Safety block: signing is disabled while DRY_RUN is enabled")
        tx = VersionedTransaction.from_bytes(base64.b64decode(raw_tx_b64))
        signed_tx = VersionedTransaction(tx.message, [keypair])
        encoded = base64.b64encode(bytes(signed_tx)).decode("utf-8")
        return encoded, str(signed_tx.signatures[0])

    def submit_encoded(self, encoded: str) -> str:
        """Broadcast an already-signed transaction (blocked in dry-run)."""
        if self.cfg.DRY_RUN:
            raise RuntimeError("Safety block: sending is disabled while DRY_RUN is enabled")
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

    def sign_and_send(self, raw_tx_b64: str, keypair: Keypair) -> str:
        """Deserialize, sign, and broadcast transaction unless dry-run is active."""
        if self.cfg.DRY_RUN:
            raise RuntimeError("Safety block: sign_and_send is disabled while DRY_RUN is enabled")
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
        """Fetch verified on-chain mint decimals; never guess on RPC failure."""
        if token_mint in self._decimals_cache:
            return self._decimals_cache[token_mint]
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getAccountInfo",
                "params": [token_mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            }
            resp = requests.post(self.cfg.SOLANA_RPC_URL, json=payload, timeout=8)
            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict) or body.get("error"):
                raise RuntimeError("RPC returned an error while reading mint decimals")
            value = body.get("result", {}).get("value")
            if not isinstance(value, dict):
                raise RuntimeError("Mint account was not found by the RPC")
            info = value.get("data", {}).get("parsed", {}).get("info", {})
            decimals = info.get("decimals")
            if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
                raise RuntimeError("RPC response did not contain valid mint decimals")
            self._decimals_cache[token_mint] = decimals
            return decimals
        except Exception as exc:
            log.warning("Cannot verify decimals for %s; aborting safely: %s", token_mint[:8], exc)
            raise RuntimeError(f"Cannot verify token decimals for {token_mint[:8]}; refusing to calculate token amount") from exc

    def get_price_and_liquidity(self, token_mint: str) -> tuple[float, float | None]:
        """Return a validated (price_usd, liquidity_usd) pair.

        Invalid numeric data fails closed. Jupiter can provide a price fallback
        only when DexScreener has no matching Solana pair; its liquidity remains
        unknown (None), never implicitly healthy.
        """
        try:
            pairs = self.dex.get_pairs_for_mint(token_mint)
            sol_pairs = [
                p for p in pairs
                if isinstance(p, dict)
                and p.get("chainId") == "solana"
                and isinstance(p.get("baseToken"), dict)
                and (p["baseToken"].get("address") or "").lower() == token_mint.lower()
            ]
        except Exception as e:
            log.debug(f"DexScreener price lookup failed for {token_mint[:8]}: {e}")
            sol_pairs = []

        if sol_pairs:
            valid_pairs = []
            for pair in sol_pairs:
                try:
                    liquidity_data = pair.get("liquidity")
                    liquidity_raw = liquidity_data.get("usd") if isinstance(liquidity_data, dict) else 0
                    liquidity = float(liquidity_raw or 0)
                    if not math.isfinite(liquidity) or liquidity < 0:
                        continue
                    valid_pairs.append((liquidity, pair))
                except (TypeError, ValueError, OverflowError):
                    continue

            if not valid_pairs:
                log.warning(f"Invalid DexScreener liquidity for {token_mint[:8]}; rejecting price.")
                return 0.0, None

            liquidity, best = max(valid_pairs, key=lambda item: item[0])
            try:
                price = float(best.get("priceUsd") or 0)
            except (TypeError, ValueError, OverflowError):
                price = 0.0

            if not math.isfinite(price) or price <= 0:
                log.warning(f"Invalid DexScreener price for {token_mint[:8]}; rejecting price.")
                return 0.0, liquidity
            return price, liquidity

        # Fallback is for a mint not yet indexed by DexScreener. Liquidity
        # is unknown, so callers must not treat this as evidence of a healthy pool.
        try:
            jup = JupiterClient(self.cfg)
            quote = jup.get_quote(self.cfg.INPUT_MINT, token_mint, usdc_to_lamports(1.0))
            out_amount = int(quote["outAmount"])
            if out_amount <= 0:
                return 0.0, None

            token_decimals = self.get_token_decimals(token_mint)
            if not isinstance(token_decimals, int) or not 0 <= token_decimals <= 18:
                log.warning(f"Invalid token decimals for {token_mint[:8]}; rejecting Jupiter price.")
                return 0.0, None

            tokens_per_usdc = out_amount / (10 ** token_decimals)
            price = 1.0 / tokens_per_usdc if tokens_per_usdc > 0 else 0.0

            if not math.isfinite(price) or price <= 0 or price > 1_000_000 or price < 1e-15:
                log.warning(f"Invalid or implausible Jupiter price for {token_mint[:8]}; discarding.")
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

    def __init__(self, strict: bool = False):
        # strict (live mode): never auto-recover from a corrupt journal, since a
        # stale backup could hide an open position or pending transaction.
        self.strict = strict
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

    @staticmethod
    def _fresh_state() -> dict:
        return {
            "cycle": 0,
            "play_amount_usdc": None,
            "total_saved_usdc": 0.0,
            "total_winnings_usdc": 0.0,
            "open_position": None,
            "pending_tx": None,
            "consec_losses": 0,
            "history": [],
        }

    def _read_file(self, path: str) -> dict:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("journal root must be an object")
        for key, value in self._fresh_state().items():
            data.setdefault(key, value)
        data.setdefault("consec_losses", 0)
        if isinstance(data["cycle"], bool) or not isinstance(data["cycle"], int) or data["cycle"] < 0:
            raise ValueError("journal cycle is invalid")
        for key in ("total_saved_usdc", "total_winnings_usdc"):
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"journal {key} is invalid")
        play_amount = data["play_amount_usdc"]
        if play_amount is not None and (
            isinstance(play_amount, bool)
            or not isinstance(play_amount, (int, float))
            or not math.isfinite(play_amount)
            or play_amount < 0
        ):
            raise ValueError("journal play_amount_usdc is invalid")
        position = data["open_position"]
        if position is not None and (
            not isinstance(position, dict)
            or not isinstance(position.get("token_mint"), str)
            or not position.get("token_mint")
            or isinstance(position.get("token_amount"), bool)
            or not isinstance(position.get("token_amount"), (int, float))
            or not math.isfinite(position["token_amount"])
            or position["token_amount"] <= 0
            or isinstance(position.get("usdc_spent"), bool)
            or not isinstance(position.get("usdc_spent"), (int, float))
            or not math.isfinite(position["usdc_spent"])
            or position["usdc_spent"] < 0
        ):
            raise ValueError("journal open_position is invalid")
        pending = data["pending_tx"]
        if pending is not None:
            if (
                not isinstance(pending, dict)
                or pending.get("kind") not in {"buy", "sell"}
                or pending.get("state") not in {"sent", "unknown", "confirmed", "anomaly"}
                or not isinstance(pending.get("mint"), str)
                or not pending.get("mint")
                or not isinstance(pending.get("signature"), str)
                or not pending.get("signature")
                or not isinstance(pending.get("meta"), dict)
            ):
                raise ValueError("journal pending_tx is invalid")
            for key in ("pre_token", "pre_usdc", "expiry_height"):
                value = pending.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"journal pending_tx {key} is invalid")
            if "pre_sol" in pending:
                value = pending["pre_sol"]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("journal pending_tx pre_sol is invalid")
        return data

    def _load(self) -> dict:
        if not os.path.exists(self.FILE):
            if self.strict:
                raise JournalCorruptError(
                    f"{self.FILE} is missing. Live mode will not start with an empty "
                    "journal because that could hide an existing position or transaction. "
                    "Initialize and verify the journal in dry-run."
                )
            return self._fresh_state()
        try:
            return self._read_file(self.FILE)
        except (OSError, ValueError) as exc:
            if self.strict:
                raise JournalCorruptError(
                    f"{self.FILE} is unreadable ({type(exc).__name__}). Live mode will "
                    f"not auto-recover: inspect the file and {self.FILE}.bak, then "
                    f"repair or remove it deliberately."
                ) from exc
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            quarantined = f"{self.FILE}.corrupt-{stamp}"
            os.replace(self.FILE, quarantined)
            log.error("Journal unreadable; moved to %s", quarantined)
            try:
                data = self._read_file(self.FILE + ".bak")
                log.warning("Recovered journal from %s.bak", self.FILE)
                return data
            except (OSError, ValueError):
                log.warning("No usable journal backup; starting a fresh dry-run journal")
                return self._fresh_state()

    def save(self):
        """Write-and-replace so a crash never leaves a half-written journal."""
        tmp = self.FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(self.FILE):
            shutil.copyfile(self.FILE, self.FILE + ".bak")
        os.replace(tmp, self.FILE)

    @staticmethod
    def _today() -> str:
        return datetime.now().astimezone().date().isoformat()

    def realized_pnl_today(self) -> float:
        daily = self.data.get("daily_pnl") or {}
        if daily.get("date") != self._today():
            return 0.0
        return float(daily.get("pnl_usdc", 0.0))

    def record_pnl(self, pnl_usdc: float):
        self.data["daily_pnl"] = {
            "date": self._today(),
            "pnl_usdc": self.realized_pnl_today() + pnl_usdc,
        }

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
        validate_live_config(cfg)
        self.calc   = StrategyCalc(cfg)
        self.jup    = JupiterClient(cfg)
        self.oracle = PriceOracle(cfg)
        self.j      = Journal(strict=not cfg.DRY_RUN)
        self.rpc    = RpcClient(cfg.SOLANA_RPC_URL)
        self.portal = PumpPortalLocalClient()
        self.scanner = TokenScanner(ScannerConfig(), birdeye_api_key=os.getenv("BIRDEYE_API_KEY", ""))

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
        raise RuntimeError("Unable to verify USDC balance after RPC retries")

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
        raise RuntimeError("Unable to verify SOL balance after RPC retries")

    def check_balances(self, required_usdc: float) -> bool:
        try:
            usdc = self.get_usdc_balance()
            sol = self.get_sol_balance()
        except Exception as e:
            log.error(f"Cannot verify wallet balances; refusing to trade: {e}")
            return False
        log.info(f"💳 Wallet — USDC: ${usdc:.4f}  |  SOL: {sol:.4f}")

        # Zero USDC and zero SOL is not sufficient funding to trade.
        # Fail closed; do not infer an RPC failure from a zero balance.
        if usdc == 0.0 and sol == 0.0:
            log.error(
         "Balance check failed or wallet has no funds. "
         "Refusing to trade until balances are verified."
      )
            return False

        if usdc < required_usdc:
            log.error(f"❌ Insufficient USDC: have ${usdc:.4f}, need ${required_usdc:.4f}. Top up and restart.")
            return False
        if sol < 0.0001:
            log.error(f"❌ SOL too low ({sol:.4f}). Need at least 0.01 SOL for gas. Top up and restart.")
            return False
        if sol < 0.005:
            log.warning(f"⚠️  Low SOL ({sol:.4f}). Recommend topping up.")
        return True

    # ── Transaction state, settlement and reconciliation ─────────────────────
    # pending_tx lifecycle (live only; at most one at a time):
    #   sent  → confirmed → (finalized into open_position / closed position)
    #   sent  → cleared on proven failure (on-chain error, or blockhash expired
    #           with no status)
    #   sent  → unknown on RPC failure/timeout: blocks ALL new trades and any
    #           re-send until a later reconcile proves the outcome.
    #   confirmed/unknown → anomaly if on-chain balances contradict the trade.
    # The signature is journaled BEFORE the send, so a crash at any point
    # leaves enough information to determine what happened from the chain.
    def _clear_pending(self, why: str):
        p = self.j.data.get("pending_tx") or {}
        log.warning("Pending %s tx cleared (%s): %s", p.get("kind"), why, str(p.get("signature"))[:20])
        self.j.data["pending_tx"] = None
        self.j.save()

    def _settle_pending(self, wait_sec: float | None = None) -> str:
        """Return 'confirmed' | 'failed' | 'unknown' | 'none' using chain data only."""
        p = self.j.data.get("pending_tx")
        if not p:
            return "none"
        if p.get("state") == "confirmed":
            return "confirmed"
        sig = p.get("signature")
        if not sig:
            self._clear_pending("never sent")
            return "failed"
        wait = self.cfg.CONFIRM_MAX_WAIT_SEC if wait_sec is None else wait_sec
        polls = max(1, int(wait // max(self.cfg.CONFIRM_POLL_SEC, 0.1)) + 1)
        try:
            for i in range(polls):
                status = self.rpc.signature_status(sig)
                if status is None and self.rpc.block_height() > p["expiry_height"]:
                    # Blockhash window is over; one final look closes the race
                    # where the transaction landed just before expiry.
                    status = self.rpc.signature_status(sig)
                    if status is None:
                        self._clear_pending("blockhash expired, transaction never landed")
                        return "failed"
                if status is not None:
                    confirmation = status.get("confirmationStatus")
                    terminal = (
                        confirmation in ("confirmed", "finalized")
                        or ("confirmations" in status and status["confirmations"] is None)
                    )
                    if status.get("err") and terminal:
                        self._clear_pending(f"failed on-chain: {status['err']}")
                        return "failed"
                    if terminal and not status.get("err"):
                        p["state"] = "confirmed"
                        self.j.save()
                        return "confirmed"
                if i < polls - 1:
                    time.sleep(self.cfg.CONFIRM_POLL_SEC)
        except RpcError as exc:
            log.error("RPC error while settling %s: %s", sig[:20], exc)
        p["state"] = "unknown"
        self.j.save()
        log.error(
            "Transaction %s is unresolved. New trades and re-sends are blocked "
            "until it is reconciled. Check https://solscan.io/tx/%s", sig[:20], sig
        )
        return "unknown"

    def _submit_and_settle(self, kind: str, mint: str, raw_tx_b64: str, meta: dict) -> str:
        """Sign, journal, send and settle one transaction. Never re-sends.

        Returns 'confirmed' | 'failed' | 'unknown' | 'aborted'. 'aborted' means
        nothing was signed or sent.
        """
        if self.j.data.get("pending_tx"):
            log.error("Refusing to send: an earlier transaction is unresolved")
            return "aborted"
        try:
            pre_token = self.rpc.token_balance(self.pubkey, mint)
            pre_usdc = self.rpc.token_balance(self.pubkey, self.cfg.INPUT_MINT)
            pre_sol = self.rpc.sol_balance(self.pubkey) if meta.get("route") == "pump" else None
            height = self.rpc.block_height()
        except RpcError as exc:
            log.error("Cannot read pre-trade balances (%s); not sending", exc)
            return "aborted"
        try:
            signed, sig = self.jup.sign_transaction(raw_tx_b64, self.keypair)
        except Exception as exc:
            log.error("Signing failed (%s); not sending", type(exc).__name__)
            return "aborted"
        self.j.data["pending_tx"] = {
            "kind": kind, "state": "sent", "mint": mint, "signature": sig,
            "pre_token": pre_token, "pre_usdc": pre_usdc,
            "expiry_height": height + self.cfg.BLOCKHASH_VALIDITY_BLOCKS,
            "created_at": datetime.now().astimezone().isoformat(),
            "meta": meta,
        }
        if pre_sol is not None:
            self.j.data["pending_tx"]["pre_sol"] = pre_sol
        self.j.save()
        try:
            self.jup.submit_encoded(signed)
        except Exception as exc:
            # Even an RPC error response may be a duplicate/already-processed
            # signature or a lost response after acceptance. Resolve by signature;
            # never discard the durable pending record based on the send exception.
            log.warning("Send result ambiguous (%s); settling by signature", type(exc).__name__)
        log.info("   TX: https://solscan.io/tx/%s", sig)
        return self._settle_pending()

    def _chain_fill(self, p: dict):
        """Confirmed fill from chain data: (post_token, quote_delta_usd, extras).

        quote_delta_usd is the signed change in the wallet's quote asset in USD
        terms (USDC delta for Jupiter, SOL delta incl. fees for the curve route).
        Returns None when data is unavailable (caller retries on reconcile) and
        raises ValueError when the data is present but unusable (anomaly).
        """
        meta = p["meta"]
        post_token = self.rpc.token_balance(self.pubkey, p["mint"])
        if meta.get("route") != "pump":
            post_usdc = self.rpc.token_balance(self.pubkey, self.cfg.INPUT_MINT)
            return post_token, (post_usdc - p["pre_usdc"]) / (10 ** USDC_DECIMALS), {}
        sol_usd = meta.get("sol_usd")
        if (isinstance(sol_usd, bool) or not isinstance(sol_usd, (int, float))
                or not math.isfinite(sol_usd) or sol_usd <= 0):
            raise ValueError("missing SOL/USD rate for a SOL-denominated trade")
        res = self.rpc.transaction_sol_delta(p["signature"], self.pubkey)
        if res is None:
            raise RpcError("confirmed transaction not yet retrievable")
        sol_delta, fee = res
        return post_token, (sol_delta / 1e9) * sol_usd, {
            "sol_delta_lamports": sol_delta, "fee_lamports": fee, "sol_usd": sol_usd,
        }

    def _finalize_buy(self) -> dict | None:
        """Turn a confirmed buy into an open position from on-chain balance deltas."""
        p = self.j.data.get("pending_tx")
        if not p or p.get("kind") != "buy" or p.get("state") != "confirmed":
            return None
        meta = p["meta"]
        try:
            post_token, quote_delta, extras = self._chain_fill(p)
        except RpcError as exc:
            log.error("Buy confirmed but chain data unavailable (%s); will retry on reconcile", exc)
            return None
        except ValueError as exc:
            p["state"] = "anomaly"
            self.j.save()
            log.error("Buy confirmed but fill is unusable (%s). Manual review required.", exc)
            return None
        delta = post_token - p["pre_token"]
        usdc_spent = -quote_delta
        if delta <= 0 or usdc_spent <= 0:
            p["state"] = "anomaly"
            self.j.save()
            log.error("Buy confirmed but balances do not match (token Δ=%s, quote Δ=%s). "
                      "Manual review required; trading blocked.", delta, quote_delta)
            return None
        decimals = meta["token_decimals"]
        token_amount = delta / (10 ** decimals)
        entry_price = usdc_spent / token_amount
        position = {
            "buy_tx": p["signature"],
            "usdc_spent": usdc_spent,
            "token_amount": token_amount,
            "token_decimals": decimals,
            "token_symbol": meta["symbol"],
            "entry_price": entry_price,
            "target_price": entry_price * self.cfg.PRICE_TARGET_MULT,
            "stop_price": entry_price * self.cfg.STOP_LOSS_MULT if self.cfg.STOP_LOSS_MULT > 0 else 0,
            "token_mint": p["mint"],
            "dex_url": meta.get("dex_url", ""),
            "fill_source": "onchain",
            "quoted_token_amount": meta.get("quoted_token_amount"),
            "quoted_entry_price": meta.get("quoted_entry_price"),
        }
        if meta.get("route") == "pump":
            position.update({"route": "pump", **extras})
        self.j.data["open_position"] = position
        self.j.data["pending_tx"] = None
        self.j.save()
        log.info("   Filled: %.6f %s for $%.4f (on-chain)", token_amount, meta["symbol"], usdc_spent)
        return position

    def _finalize_sell(self) -> float | None:
        """Close the position from on-chain deltas; returns actual USDC received."""
        p = self.j.data.get("pending_tx")
        if not p or p.get("kind") != "sell" or p.get("state") != "confirmed":
            return None
        try:
            post_token, quote_delta, _extras = self._chain_fill(p)
        except RpcError as exc:
            log.error("Sell confirmed but chain data unavailable (%s); will retry on reconcile", exc)
            return None
        except ValueError as exc:
            p["state"] = "anomaly"
            self.j.save()
            log.error("Sell confirmed but fill is unusable (%s). Manual review required.", exc)
            return None
        sold_raw = p["pre_token"] - post_token
        is_curve = p["meta"].get("route") == "pump"
        # A curve sell's SOL delta is net of fees, so a tiny sale can be slightly
        # negative; clamp it. A negative USDC delta is never legitimate.
        if quote_delta < 0 and is_curve:
            quote_delta = 0.0
        if sold_raw < p["meta"]["amount_raw"] or quote_delta < 0 or post_token != 0:
            p["state"] = "anomaly"
            self.j.save()
            log.error(
                "Sell confirmed but balances do not match (token Δ=%s, remaining=%s, "
                "quote Δ=%s). Manual review required; trading blocked.",
                sold_raw, post_token, quote_delta,
            )
            return None

        position = self.j.data.get("open_position") or {}
        meta = p["meta"]
        proceeds = quote_delta
        spent = float(position.get("usdc_spent", meta.get("usdc_spent", 0.0)))
        reason = meta.get("reason", "recovered_sell")
        cycle = meta.get("cycle", self.j.data["cycle"] + 1)
        exit_price = meta.get("exit_price", 0.0)
        win = reason == "target"
        next_play = proceeds * self.cfg.REINVEST_PCT if win else proceeds
        saved = proceeds * self.cfg.SAVE_PCT if win else 0.0

        self.j.data["cycle"] = cycle
        self.j.data["play_amount_usdc"] = next_play
        self.j.data["total_saved_usdc"] += saved
        self.j.data["total_winnings_usdc"] += proceeds
        self.j.data["consec_losses"] = (
            0 if win else self.j.data.get("consec_losses", 0) + 1
        )
        self.j.record_pnl(proceeds - spent)
        self.j.data["open_position"] = None
        self.j.data["pending_tx"] = None
        self.j.log_cycle({
            "cycle": cycle,
            "token": position.get("token_symbol", meta.get("symbol", "?")),
            "token_mint": position.get("token_mint", p["mint"]),
            "dex_url": position.get("dex_url", ""),
            "play_amount": spent,
            "winnings": proceeds,
            "next_play": next_play,
            "saved": saved,
            "total_saved": self.j.data["total_saved_usdc"],
            "total_winnings": self.j.data["total_winnings_usdc"],
            "win": win,
            "reason": reason,
            "exit_price": exit_price,
        })
        log.info("   Sell accounted from confirmed balance deltas.")
        return proceeds

    def reconcile_pending(self) -> dict:
        """Resolve any journaled transaction from chain data. Safe to call anytime.
        """
        p = self.j.data.get("pending_tx")
        if not p:
            return {"state": "none"}
        if p.get("state") == "anomaly":
            log.error("Pending transaction is flagged for manual review; trading blocked.")
            return {"state": "anomaly"}
        kind = p["kind"]
        outcome = self._settle_pending()
        if outcome != "confirmed":
            return {"state": outcome, "kind": kind}
        if kind == "buy":
            return {"state": "confirmed" if self._finalize_buy() else "unresolved", "kind": kind}
        proceeds = self._finalize_sell()
        if proceeds is None:
            return {"state": "unresolved", "kind": kind}
        return {"state": "confirmed", "kind": kind, "proceeds": proceeds}

    # ── Pump.fun bonding-curve route ──────────────────────────────────────────
    def _sol_usd(self) -> float:
        """SOL/USD from a live Jupiter SOL→USDC quote. Raises if unusable."""
        quote = self.jup.get_quote(WSOL_MINT, self.cfg.INPUT_MINT, 10 ** 9)
        price = int(quote["outAmount"]) / (10 ** USDC_DECIMALS)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("SOL/USD quote is unusable")
        return price

    def _curve_state(self, mint: str) -> dict | None:
        """Parsed curve, or None when no curve account exists.

        Raises RpcError/ValueError when the state cannot be trusted."""
        data = self.rpc.account_data(bonding_curve_address(mint))
        return None if data is None else parse_bonding_curve(data)

    def _curve_buy(self, usdc_amount: float, token: TokenCandidate, curve: dict) -> dict | None:
        try:
            sol_usd = self._sol_usd()
            sol_lamports = self.rpc.sol_balance(self.pubkey)
            token_decimals = self.oracle.get_token_decimals(token.mint)
        except Exception as exc:
            log.error("Curve buy refused: cannot establish SOL price/balance (%s)", exc)
            return None
        sol_amount = round(usdc_amount / sol_usd, 9)
        if sol_amount <= 0 or sol_lamports / 1e9 < sol_amount + self.cfg.MIN_SOL_FOR_FEES:
            log.error("Curve buy refused: SOL balance cannot cover %.6f SOL plus fees", sol_amount)
            return None
        vtok, vquote = curve["virtual_token_reserves"], curve["virtual_quote_reserves"]
        in_lam = int(sol_amount * 1e9)
        est_tokens = (vtok - (vtok * vquote) // (vquote + in_lam)) / (10 ** token_decimals)
        if est_tokens <= 0:
            log.error("Curve buy refused: estimated output is zero")
            return None
        log.info("   Curve route: %.6f SOL (~$%.4f at $%.2f/SOL), est. %.6f %s before fees",
                 sol_amount, usdc_amount, sol_usd, est_tokens, token.symbol)
        try:
            raw_tx = self.portal.build_transaction(
                self.pubkey, "buy", token.mint, sol_amount, True,
                self.cfg.CURVE_BUY_SLIPPAGE_PCT, self.cfg.CURVE_PRIORITY_FEE_SOL, "pump")
        except Exception as exc:
            log.error("Could not build curve buy (%s); nothing sent", type(exc).__name__)
            return None
        outcome = self._submit_and_settle("buy", token.mint, raw_tx, {
            "route": "pump", "sol_usd": sol_usd, "sol_amount": sol_amount,
            "symbol": token.symbol, "dex_url": token.url,
            "token_decimals": token_decimals, "usdc_amount": usdc_amount,
            "quoted_token_amount": est_tokens,
            "quoted_entry_price": usdc_amount / est_tokens,
        })
        if outcome != "confirmed":
            log.error("Curve buy not confirmed (%s) — no position opened.", outcome)
            return None
        return self._finalize_buy()

    def _curve_sell(self, position: dict, reason: str, exit_price: float,
                    cycle: int | None, amount_raw: int, decimals: int) -> float | None:
        mint = position["token_mint"]
        for attempt, slippage in enumerate(self.cfg.CURVE_SELL_SLIPPAGES_PCT, 1):
            state, value = self._prepare_live_sell(position, amount_raw)
            if state == "sold":
                return value
            if state == "blocked":
                return None
            amount_raw = value
            try:
                sol_usd = self._sol_usd()
                sol_lamports = self.rpc.sol_balance(self.pubkey)
            except Exception as exc:
                log.error("Curve sell refused: cannot establish SOL price/balance (%s)", exc)
                return None
            if sol_lamports / 1e9 < self.cfg.MIN_SOL_FOR_SELL:
                log.error("Curve sell refused: not enough SOL to pay fees. Top up the wallet.")
                return None
            ui_amount = Decimal(amount_raw) / (Decimal(10) ** decimals)
            log.info("   Curve sell attempt %d (slippage %.0f%%)", attempt, slippage)
            try:
                raw_tx = self.portal.build_transaction(
                    self.pubkey, "sell", mint, ui_amount, False, slippage,
                    self.cfg.CURVE_PRIORITY_FEE_SOL, "pump")
            except Exception as exc:
                log.error("Could not build curve sell (%s); nothing sent", type(exc).__name__)
                return None
            outcome = self._submit_and_settle("sell", mint, raw_tx, {
                "route": "pump", "sol_usd": sol_usd,
                "symbol": position.get("token_symbol", "TOKEN"), "amount_raw": amount_raw,
                "usdc_spent": position.get("usdc_spent", 0),
                "reason": reason, "exit_price": exit_price,
                "cycle": cycle if cycle is not None else self.j.data["cycle"] + 1,
            })
            if outcome == "confirmed":
                proceeds = self._finalize_sell()
                if proceeds is None:
                    log.error("   Curve sell confirmed but unreconciled; no resend.")
                return proceeds
            if outcome in ("unknown", "aborted"):
                log.error("   Curve sell outcome %s; refusing to resend until reconciled.", outcome)
                return None
            log.error("   Curve sell failed on-chain (attempt %d)", attempt)
            time.sleep(2)
        log.error("   Curve sell failed after all attempts; position kept for review.")
        return None

    # ── Buy ───────────────────────────────────────────────────────────────────
    def execute_buy(self, usdc_amount: float, token: TokenCandidate) -> dict | None:
        if usdc_amount <= 0:
            raise ValueError("Buy amount must be positive")
        if token is None or not getattr(token, "mint", None):
            raise ValueError("Token candidate is missing a mint address")
        if not self.cfg.DRY_RUN:
            # Defense in depth: run_cycle also checks these before scanning.
            validate_live_config(self.cfg)
            if kill_switch_active(self.cfg):
                raise LiveTradingConfigError("Kill switch active; refusing new live buy")
            if usdc_amount > float(self.cfg.MAX_POSITION_USDC):
                raise LiveTradingConfigError(
                    f"Buy ${usdc_amount:.4f} exceeds MAX_POSITION_USDC; refusing")
            if self.j.data.get("pending_tx"):
                log.error("Refusing buy: an earlier transaction is unresolved")
                return None
            if self.j.data.get("open_position"):
                log.error("Refusing buy: a position is already open")
                return None

        log.info(f"📥 BUY  ${usdc_amount:.4f} USDC → {token.symbol} ({token.mint[:8]}...)")

        if not self.cfg.DRY_RUN and self.cfg.CURVE_TRADING:
            try:
                curve = self._curve_state(token.mint)
            except (RpcError, ValueError) as exc:
                log.error("Curve state unknown (%s); refusing buy", exc)
                return None
            if curve is not None and not curve["complete"]:
                if (curve["quote_mint"] != WSOL_MINT or curve["is_cashback_coin"]
                        or curve["is_mayhem_mode"]):
                    log.error("Curve is not a plain SOL-paired coin (unverified route); refusing buy")
                    return None
                return self._curve_buy(usdc_amount, token, curve)
            # No curve account or a completed curve: fall through to Jupiter.

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
            try:
                raw_tx = self.jup.build_swap_transaction(quote, self.pubkey)
            except Exception as exc:
                log.error("Could not build buy transaction (%s); nothing sent", type(exc).__name__)
                return None
            outcome = self._submit_and_settle("buy", token.mint, raw_tx, {
                "symbol": token.symbol, "dex_url": token.url,
                "token_decimals": token_decimals, "usdc_amount": usdc_amount,
                "quoted_token_amount": token_amount, "quoted_entry_price": entry_price,
            })
            if outcome != "confirmed":
                log.error("Buy not confirmed (%s) — no position opened.", outcome)
                return None
            return self._finalize_buy()

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
    def _prepare_live_sell(self, position: dict, expected_raw: int) -> tuple:
        """Gate every live sell attempt on chain truth. Returns (state, value):
        ('sold', proceeds) | ('blocked', None) | ('ready', raw_amount_to_sell)."""
        if self.j.data.get("pending_tx"):
            res = self.reconcile_pending()
            if res.get("kind") == "sell" and res.get("state") == "confirmed":
                return "sold", res["proceeds"]
            log.error("   Earlier transaction unresolved (%s); not selling.", res.get("state"))
            return "blocked", None
        try:
            balance = self.rpc.token_balance(self.pubkey, position["token_mint"])
        except RpcError as exc:
            log.error("   Cannot read on-chain balance (%s); not selling.", exc)
            return "blocked", None
        if balance <= 0:
            log.error("   On-chain balance is 0 while the journal has an open position. "
                      "Keeping the position blocked for MANUAL REVIEW; no new trade.")
            position["manual_review"] = True
            self.j.data["open_position"] = position
            self.j.save()
            return "blocked", None
        return "ready", min(balance, expected_raw)

    def execute_sell(
        self, position: dict, reason: str = "target",
        exit_price: float = 0.0, cycle: int | None = None,
    ) -> float | None:
        token_amount = position["token_amount"]
        token_mint = position["token_mint"]
        token_decimals = position.get("token_decimals", 6)
        symbol = position.get("token_symbol", "TOKEN")
        usdc_spent = position.get("usdc_spent", 0)

        if token_amount <= 0:
            raise ValueError("Sell amount must be positive")

        log.info(f"📤 SELL {token_amount:.6f} {symbol} → USDC ({reason})")

        amount_lam = int(token_amount * (10 ** token_decimals))

        if not self.cfg.DRY_RUN and position.get("route") == "pump":
            try:
                curve = self._curve_state(token_mint)
            except (RpcError, ValueError) as exc:
                log.error("   Curve state unknown (%s); not selling blind.", exc)
                return None
            if curve is not None and not curve["complete"]:
                return self._curve_sell(position, reason, exit_price, cycle, amount_lam, token_decimals)
            log.warning("   Curve is complete or gone (graduated); trying the Jupiter route.")

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
        elif reason in ("stop_loss", "no_price"):
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
            if not self.cfg.DRY_RUN:
                state, value = self._prepare_live_sell(position, amount_lam)
                if state == "sold":
                    return value
                if state == "blocked":
                    return None
                amount_lam = value
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
            except Exception as e:
                log.error(f"   ❌ Could not build sell transaction ({type(e).__name__}); nothing sent")
                if attempt < len(slippage_schedule):
                    time.sleep(2)
                    continue
                log.error("   All attempts exhausted. Sell manually on jup.ag.")
                return None

            outcome = self._submit_and_settle("sell", token_mint, raw_tx, {
                "symbol": symbol, "amount_raw": amount_lam, "usdc_spent": usdc_spent,
                "reason": reason, "exit_price": exit_price,
                "cycle": cycle if cycle is not None else self.j.data["cycle"] + 1,
            })
            if outcome == "confirmed":
                proceeds = self._finalize_sell()
                if proceeds is not None:
                    return proceeds
                log.error("   Sell confirmed but unreconciled; no resend. Position kept for review.")
                return None
            if outcome in ("unknown", "aborted"):
                log.error(f"   Sell outcome {outcome}; refusing to resend until reconciled.")
                return None
            log.error(f"   Sell failed on-chain (attempt {attempt})")
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

        # ── Stop-loss confirmation ────────────────────────────────────────────
        # A breach exits immediately only if is_sustained_crash() says the price
        # was already falling and this tick is >= SUSTAINED_DROP_PCT under the
        # recent average. Any other breach (including a lone anomalous tick from
        # a flat baseline) must be confirmed by exactly one further breached
        # reading after warmup; a recovery above the stop cancels it.
        RECENT_PRICE_BUFFER = 5  # last readings used as the baseline
        
        # ── Buy warmup ─────────────────────────────────────────────────────────
        # Ignore stop-loss entirely for the first few seconds after a buy.
        # This is specifically to stop the "instant stop-loss" pattern seen
        # in logs (QUIVER #2: stop fired 1.2s after entry with no real price
        # history) — the oracle needs at least one real settled reading
        # before we trust it enough to act on a loss.
        BUY_WARMUP_SEC = 6

        start_time = time.time()
        stop_wick_seen = False  # Track if we saw one wick; exit if still breached next tick
        recent_prices = [position["entry_price"]]  # Rolling buffer of good prices
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
        no_price_since = None  # start of the current run of missing prices

        while True:
            price, liquidity_usd = self.oracle.get_price_and_liquidity(mint)
            now = time.time()
            elapsed = now - start_time

            if price <= 0:
                if no_price_since is None:
                    no_price_since = now
                missing_for = now - no_price_since
                if self.cfg.MAX_NO_PRICE_SEC > 0 and missing_for >= self.cfg.MAX_NO_PRICE_SEC:
                    log.error(
                        f"   🚨 No usable price for {symbol} for {missing_for:.0f}s "
                        f"(limit {self.cfg.MAX_NO_PRICE_SEC}s) — attempting exit instead of waiting."
                    )
                    return "no_price", last_good_price
                log.warning("   Price fetch returned 0 — retrying...")
                time.sleep(self.cfg.POLL_INTERVAL_SEC)
                continue
            no_price_since = None

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
                    if is_sustained_crash(recent_prices, price):
                        log.info(
                            f"   🛑 Stop-loss (sustained crash): ${price:.8f} is "
                            f">={SUSTAINED_DROP_PCT}% under the recent average and the "
                            f"price was already falling — exiting without waiting"
                        )
                        return "stop_loss", price
                    elif not stop_wick_seen:
                        stop_wick_seen = True
                        log.warning(
                            f"   ⚠️  Price at/below stop but not a confirmed crash — "
                            f"confirming on next tick"
                        )
                    else:
                        log.info(
                            f"   🛑 Stop-loss confirmed: still at/below stop "
                            f"(${price:.8f}) on the next tick"
                        )
                        return "stop_loss", price
            else:
                # Price recovered above stop — reset confirmation counter
                if stop_wick_seen:
                    log.info(f"   ✅ Price recovered above stop — resetting")
                stop_wick_seen = False

            # Update recent price buffer (keep last RECENT_PRICE_BUFFER good prices)
            recent_prices.append(price)
            if len(recent_prices) > RECENT_PRICE_BUFFER:
                recent_prices.pop(0)

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

        if not self.cfg.DRY_RUN and self.j.data.get("pending_tx"):
            res = self.reconcile_pending()
            if self.j.data.get("pending_tx"):
                log.error("🛑 Unresolved transaction (%s) — all trading paused until reconciled.",
                          res.get("state"))
                return False

        position = self.j.data.get("open_position")
        if position and position.get("manual_review"):
            log.error("🛑 Open position requires manual review; refusing automated trading.")
            return False

        if not position:
            if not self.cfg.DRY_RUN:
                if kill_switch_active(self.cfg):
                    log.error("🛑 Kill switch active — no new live entries. Remove the file to resume.")
                    return False
                loss_cap = float(self.cfg.MAX_DAILY_LOSS_USDC)
                loss_today = -self.j.realized_pnl_today()
                if loss_today >= loss_cap:
                    log.error(
                        f"🛑 Daily loss cap reached (${loss_today:.2f} >= ${loss_cap:.2f}) — "
                        f"no new live entries today."
                    )
                    return False
                max_position = float(self.cfg.MAX_POSITION_USDC)
                if play_amount > max_position:
                    log.warning(
                        f"⚠️  Play amount ${play_amount:.4f} capped to "
                        f"MAX_POSITION_USDC ${max_position:.4f}"
                    )
                    play_amount = max_position

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
        usdc_received = self.execute_sell(position, reason, exit_price, cycle)
        if usdc_received is None:
            return False

        if not self.cfg.DRY_RUN:
            # _finalize_sell commits the actual proceeds and cycle accounting
            # together with clearing pending_tx/open_position.
            return True

        win = reason == "target"
        self.j.record_pnl(usdc_received - position.get("usdc_spent", play_amount))
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
        elif self.j.data.get("pending_tx"):
            result = self.reconcile_pending()
            if self.j.data.get("pending_tx"):
                log.error(
                    "Startup reconciliation unresolved (%s); refusing to continue.",
                    result.get("state"),
                )
                return

        # Do not display hypothetical compounding as expected/actual profit.
        # MAX_CYCLES=0 (default) means run until Ctrl+C. A positive value limits
        # successful cycles in THIS process, not the cumulative journal counter.
        if self.cfg.DRY_RUN:
            log.info("💳 [DRY RUN] No wallet balance is checked; no real transactions will be sent.")
            log.info("📈 Paper mode uses live quotes/prices where available; results are not guaranteed.")
        elif not self.check_balances(
            0.0 if self.j.data.get("open_position") else self.j.data["play_amount_usdc"]
        ):
            log.error("Wallet balance verification failed; refusing to start live.")
            sys.exit(1)

        completed_this_run = 0
        while self.cfg.MAX_CYCLES <= 0 or completed_this_run < self.cfg.MAX_CYCLES:
            ok = self.run_cycle()
            if ok:
                completed_this_run += 1
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
                    log.warning("No completed cycle (no candidate or quote issue). Retrying in 30s...")
                    time.sleep(30)

        log.info("\n🏁 Cycle limit reached for this run.")
        log.info(f"   Completed this run: {completed_this_run}")
        log.info(f"   Journal cycle count: {self.j.data['cycle']}")
        log.info(f"   Paper-mode cumulative saved estimate: ${self.j.data['total_saved_usdc']:.2f} USDC")
        log.info(f"   Paper-mode cumulative proceeds estimate: ${self.j.data['total_winnings_usdc']:.2f} USDC")

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

    try:
        bot = CompoundingBot(cfg)
    except (LiveTradingConfigError, JournalCorruptError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("\n⏹  Bot stopped by user. State saved in journal.json")
