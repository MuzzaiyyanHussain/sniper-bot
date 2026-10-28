"""
Read-only Solana RPC helpers plus transaction *builders* for the bot.

Nothing in this module signs or sends a transaction. Signing and submission
stay in bot.py behind the live-trading safety gate.
"""

import base64
import math
import struct
from decimal import Decimal

import requests

# Pump.fun program id matched the owner of one live curve account; broader
# account/version compatibility has not been established.
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPPORTAL_TRADE_LOCAL_URL = "https://pumpportal.fun/api/trade-local"


class RpcError(RuntimeError):
    """The RPC call failed or returned unusable data. Callers must fail closed."""


class RpcClient:
    def __init__(self, url: str, session=None, timeout: float = 15.0):
        self.url = url
        self.session = session or requests.Session()
        self.timeout = timeout

    def call(self, method: str, params: list):
        try:
            resp = self.session.post(
                self.url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            raise RpcError(f"{method} request failed: {type(exc).__name__}") from exc
        if not isinstance(body, dict) or body.get("error") is not None:
            raise RpcError(f"{method} returned an error")
        if "result" not in body:
            raise RpcError(f"{method} returned no result")
        return body["result"]

    def block_height(self) -> int:
        height = self.call("getBlockHeight", [{"commitment": "confirmed"}])
        if isinstance(height, bool) or not isinstance(height, int) or height < 0:
            raise RpcError("getBlockHeight returned an invalid value")
        return height

    def signature_status(self, signature: str):
        """Return None when the cluster has no record, else the status dict."""
        result = self.call(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}]
        )
        values = result.get("value") if isinstance(result, dict) else None
        if not isinstance(values, list) or len(values) != 1:
            raise RpcError("getSignatureStatuses returned an invalid value")
        return values[0]

    def sol_balance(self, owner: str) -> int:
        """Exact lamport balance at confirmed commitment."""
        result = self.call("getBalance", [owner, {"commitment": "confirmed"}])
        value = result.get("value") if isinstance(result, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RpcError("getBalance returned an invalid value")
        return value

    def transaction_sol_delta(self, signature: str, owner: str):
        """Return (owner lamport delta, fee lamports) from confirmed metadata.

        Returns None if the transaction is not found. Raises RpcError on any
        missing or inconsistent metadata so callers never invent a fill.
        """
        tx = self.call(
            "getTransaction",
            [signature, {"encoding": "json", "commitment": "confirmed",
                         "maxSupportedTransactionVersion": 0}],
        )
        if tx is None:
            return None
        try:
            meta = tx["meta"]
            keys = list(tx["transaction"]["message"]["accountKeys"])
            loaded = meta.get("loadedAddresses") or {}
            keys += list(loaded.get("writable", [])) + list(loaded.get("readonly", []))
            pre, post, fee = meta["preBalances"], meta["postBalances"], meta["fee"]
            if meta.get("err") is not None:
                raise RpcError("transaction failed on-chain")
            if not (len(keys) == len(pre) == len(post)):
                raise RpcError("inconsistent transaction balance metadata")
            idx = keys.index(owner)
            if not all(isinstance(v, int) and not isinstance(v, bool)
                       for v in (pre[idx], post[idx], fee)):
                raise RpcError("non-integer transaction balance metadata")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RpcError("incomplete transaction metadata") from exc
        return post[idx] - pre[idx], fee

    def account_data(self, address: str, expected_owner: str | None = None):
        """Raw account bytes at confirmed commitment, or None if it doesn't exist."""
        result = self.call(
            "getAccountInfo",
            [address, {"encoding": "base64", "commitment": "confirmed"}],
        )
        value = result.get("value") if isinstance(result, dict) else None
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RpcError("getAccountInfo returned an invalid account")
        if expected_owner is not None and value.get("owner") != expected_owner:
            raise RpcError("getAccountInfo returned an unexpected account owner")
        try:
            return base64.b64decode(value["data"][0], validate=True)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RpcError("getAccountInfo returned unparseable data") from exc

    def token_balance(self, owner: str, mint: str) -> int:
        """Exact base-unit balance summed over the owner's accounts for a mint.

        Works for both the Token and Token-2022 programs (filtered by mint).
        """
        result = self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        accounts = result.get("value") if isinstance(result, dict) else None
        if not isinstance(accounts, list):
            raise RpcError("getTokenAccountsByOwner returned an invalid value")
        total = 0
        for account in accounts:
            try:
                amount = account["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
                total += int(amount)
            except (KeyError, TypeError, ValueError) as exc:
                raise RpcError("unparseable token account") from exc
        return total


class PumpPortalLocalClient:
    """Builds UNSIGNED pump.fun / PumpSwap transactions via PumpPortal's Local API.

    The returned base64 transaction goes through the same sign/send/reconcile
    pipeline as Jupiter transactions. Interface taken from
    https://pumpportal.fun/local-trading-api/trading-api/ (POST form data,
    binary body). Not exercised against the live service in this project.
    """

    VALID_POOLS = {
        "pump", "pump-amm", "raydium", "raydium-cpmm",
        "launchlab", "bonk", "auto",
    }

    def __init__(self, session=None, url: str = PUMPPORTAL_TRADE_LOCAL_URL):
        self.session = session or requests.Session()
        self.url = url

    def build_transaction(self, public_key: str, action: str, mint: str,
                          amount: float, denominated_in_sol: bool,
                          slippage_pct: float, priority_fee_sol: float,
                          pool: str = "pump") -> str:
        if action not in ("buy", "sell"):
            raise ValueError("action must be 'buy' or 'sell'")
        if pool not in self.VALID_POOLS:
            raise ValueError(f"unsupported pool {pool!r}")
        if not public_key or not mint:
            raise ValueError("public_key and mint are required")
        for name, value in (("amount", amount), ("slippage_pct", slippage_pct)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float, Decimal))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite number > 0")
        if (
            isinstance(priority_fee_sol, bool)
            or not isinstance(priority_fee_sol, (int, float))
            or not math.isfinite(priority_fee_sol)
            or priority_fee_sol < 0
        ):
            raise ValueError("priority_fee_sol must be finite and >= 0")
        if not isinstance(denominated_in_sol, bool):
            raise ValueError("denominated_in_sol must be a bool")
        resp = self.session.post(
            self.url,
            data={
                "publicKey": public_key,
                "action": action,
                "mint": mint,
                "amount": format(amount, "f") if isinstance(amount, Decimal) else amount,
                "denominatedInSol": "true" if denominated_in_sol else "false",
                "slippage": slippage_pct,
                "priorityFee": priority_fee_sol,
                "pool": pool,
            },
            timeout=15,
        )
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"PumpPortal build failed (HTTP {resp.status_code})")
        return base64.b64encode(resp.content).decode("ascii")


# ── Bonding-curve state (read-only) ──
# Layout from the official pump-fun/pump-public-docs IDL `BondingCurve` type:
# discriminator, 5 x u64 (virtual token, virtual quote, real token, real quote,
# total supply), bool complete, pubkey creator, bool is_mayhem_mode,
# bool is_cashback_coin, pubkey quote_mint, ... Only this prefix is decoded.
# It parsed successfully for one live mainnet account; broader version
# compatibility has not been established.
BONDING_CURVE_DISCRIMINATOR = bytes([23, 183, 248, 55, 96, 216, 172, 96])
WSOL_MINT = "So11111111111111111111111111111111111111112"
# Observed as quote_mint on live incomplete curves whose reserves are in lamports
# (~30 SOL virtual); treated as native SOL only for read-only price/liquidity.
NATIVE_SOL_QUOTE = "11111111111111111111111111111111"
SOL_QUOTE_MINTS = (WSOL_MINT, NATIVE_SOL_QUOTE)
_CURVE_STRUCT = struct.Struct("<8s5QB32sBB32s")


def bonding_curve_address(mint: str) -> str:
    from solders.pubkey import Pubkey

    pda, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(PUMPFUN_PROGRAM_ID),
    )
    return str(pda)


def parse_bonding_curve(data: bytes) -> dict:
    """Decode curve reserves. Raises ValueError on short/implausible data."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < _CURVE_STRUCT.size:
        raise ValueError("bonding-curve account data too short")
    from solders.pubkey import Pubkey

    (disc, vtok, vquote, rtok, rquote, supply, complete, _creator, mayhem,
     cashback, quote_mint) = _CURVE_STRUCT.unpack_from(bytes(data))
    if disc != BONDING_CURVE_DISCRIMINATOR:
        raise ValueError("not a bonding-curve account")
    if complete not in (0, 1) or mayhem not in (0, 1) or cashback not in (0, 1):
        raise ValueError("bonding-curve flags are invalid")
    if not complete and (vtok <= 0 or vquote <= 0):
        raise ValueError("bonding-curve virtual reserves must be positive")
    return {
        "virtual_token_reserves": vtok,
        "virtual_quote_reserves": vquote,
        "real_token_reserves": rtok,
        "real_quote_reserves": rquote,
        "token_total_supply": supply,
        "complete": bool(complete),
        "is_mayhem_mode": bool(mayhem),
        "is_cashback_coin": bool(cashback),
        "quote_mint": str(Pubkey.from_bytes(quote_mint)),
    }


def curve_price_sol(curve: dict, token_decimals: int = 6) -> float:
    """SOL per whole token from virtual reserves. Needs a SOL/USD feed for USD.

    Only valid for SOL-paired, incomplete curves; anything else fails closed.
    """
    if curve.get("complete") or curve.get("quote_mint") not in SOL_QUOTE_MINTS:
        raise ValueError("curve is complete or not SOL-paired")
    sol = curve["virtual_quote_reserves"] / 1e9
    tokens = curve["virtual_token_reserves"] / (10 ** token_decimals)
    return sol / tokens
