import base64
import struct

import requests
from solders.pubkey import Pubkey

RPC = "https://api.mainnet-beta.solana.com"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MINT = "mZFQaqWDG2wYuyforqEZXMV4orKTmA1Dskb4G5kpump"
CURVE = "8HdJjVXghWDUmumnZJNC5f1EGQ1sPSMMZ84kVaC9vXk3"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
DISC = bytes([23, 183, 248, 55, 96, 216, 172, 96])

TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}


def _decode_account_data(account):
    data = account.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], str):
        raise RuntimeError("Malformed account data encoding")
    try:
        return base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Invalid base64 account data") from exc


def read_multiple_accounts(addresses):
    """Fetch all accounts in one RPC response so they share a context slot."""
    response = requests.post(
        RPC,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getMultipleAccounts",
            "params": [
                addresses,
                {"encoding": "base64", "commitment": "confirmed"},
            ],
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("error"):
        raise RuntimeError(f"RPC error: {payload['error']}")

    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("value"), list):
        raise RuntimeError("Malformed getMultipleAccounts response")
    if len(result["value"]) != len(addresses):
        raise RuntimeError("RPC account count does not match requested addresses")

    accounts = []
    for address, account in zip(addresses, result["value"]):
        if account is None:
            raise RuntimeError(f"Account not found: {address}")
        accounts.append((account, _decode_account_data(account)))

    context = result.get("context")
    if not isinstance(context, dict) or not isinstance(context.get("slot"), int):
        raise RuntimeError("RPC response is missing context slot")
    return context["slot"], accounts


def decode_accounts(mint_account, mint_data, curve_account, curve_data):
    if mint_account.get("owner") not in TOKEN_PROGRAMS:
        raise RuntimeError("Unrecognized mint token program")
    if len(mint_data) < 45:
        raise RuntimeError("Mint data too short")

    decimals = mint_data[44]
    if decimals > 18:
        raise RuntimeError("Unreasonable mint decimals")
    supply_raw = struct.unpack_from("<Q", mint_data, 36)[0]

    if curve_account.get("owner") != PUMP:
        raise RuntimeError("Curve owner mismatch")
    if len(curve_data) != 166 or curve_data[:8] != DISC:
        raise RuntimeError("Curve layout validation failed")

    values = [
        struct.unpack_from("<Q", curve_data, 8 + 8 * i)[0]
        for i in range(5)
    ]
    virtual_token, virtual_quote, real_token, real_quote, curve_supply = values
    complete = curve_data[48]
    if complete not in (0, 1):
        raise RuntimeError("Invalid complete flag")

    return {
        "decimals": decimals,
        "mint_supply_raw": supply_raw,
        "mint_supply": supply_raw / 10**decimals,
        "virtual_token_reserves_raw": virtual_token,
        "virtual_token_reserves": virtual_token / 10**decimals,
        "virtual_quote_reserves_raw": virtual_quote,
        "virtual_quote_reserves": virtual_quote / 1e9,
        "real_token_reserves_raw": real_token,
        "real_token_reserves": real_token / 10**decimals,
        "real_quote_reserves_raw": real_quote,
        "real_quote_reserves": real_quote / 1e9,
        "curve_supply_raw": curve_supply,
        "curve_supply": curve_supply / 10**decimals,
        "complete": complete,
        "liquidity_usd": None,
    }


def derive_curve_address(mint: str) -> str:
    """Derive the Pump.fun bonding-curve PDA for a mint."""
    pda, _bump = Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(PUMP),
    )
    return str(pda)


def derive_curve_token_account(curve: str, mint: str, token_program: str) -> str:
    """Derive the curve authority's associated token account for this mint."""
    ata, _bump = Pubkey.find_program_address(
        [
            bytes(Pubkey.from_string(curve)),
            bytes(Pubkey.from_string(token_program)),
            bytes(Pubkey.from_string(mint)),
        ],
        Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM),
    )
    return str(ata)


def decode_token_account(account, data, expected_mint, expected_authority, expected_token_program=None):
    if account.get("owner") not in TOKEN_PROGRAMS:
        raise RuntimeError("Curve token account owner is not a recognized token program")
    if expected_token_program is not None and account.get("owner") != expected_token_program:
        raise RuntimeError("Curve token account owner does not match mint token program")
    if len(data) < 72:
        raise RuntimeError("Curve token account data too short")

    mint_bytes = bytes(Pubkey.from_string(expected_mint))
    authority_bytes = bytes(Pubkey.from_string(expected_authority))
    if data[:32] != mint_bytes:
        raise RuntimeError("Curve token account mint mismatch")
    if data[32:64] != authority_bytes:
        raise RuntimeError("Curve token account authority mismatch")

    return {
        "token_program": account["owner"],
        "mint": str(Pubkey.from_bytes(data[:32])),
        "authority": str(Pubkey.from_bytes(data[32:64])),
        "amount_raw": struct.unpack_from("<Q", data, 64)[0],
    }


def verify():
    expected_curve = derive_curve_address(MINT)
    if expected_curve != CURVE:
        raise RuntimeError("Derived bonding curve PDA mismatch")

    # Derive ATA using the token program that owns the mint. Fetch the mint
    # first to determine that owner, then fetch all three accounts together.
    # The second snapshot is authoritative for all compared account data.
    import requests as _requests

    response = _requests.post(
        RPC,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [MINT, {"encoding": "base64", "commitment": "confirmed"}],
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"RPC error: {payload['error']}")
    mint_account = payload.get("result", {}).get("value")
    if mint_account is None:
        raise RuntimeError(f"Account not found: {MINT}")
    token_program = mint_account.get("owner")
    if token_program not in TOKEN_PROGRAMS:
        raise RuntimeError("Unrecognized mint token program")

    ata = derive_curve_token_account(CURVE, MINT, token_program)
    slot, accounts = read_multiple_accounts([MINT, CURVE, ata])
    (mint_account, mint_data), (curve_account, curve_data), (ata_account, ata_data) = accounts
    if mint_account.get("owner") != token_program:
        raise RuntimeError("Mint token program changed between RPC snapshots")
    decoded = decode_accounts(mint_account, mint_data, curve_account, curve_data)
    token = decode_token_account(
        ata_account, ata_data, MINT, CURVE, expected_token_program=mint_account.get("owner")
    )

    print("READ-ONLY VERIFICATION")
    print("RPC context slot:", slot)
    print("Mint:", MINT)
    print("Curve:", CURVE)
    print("Curve token account:", ata)
    print("Curve PDA: PASS")
    print("Curve owner/discriminator: PASS")
    print("Curve token account mint/authority: PASS")
    print("Mint decimals:", decoded["decimals"])
    print("Curve complete:", decoded["complete"])
    print("Real token reserves (raw):", decoded["real_token_reserves_raw"])
    print("Curve token account balance (raw):", token["amount_raw"])
    print("DexScreener liquidity: NOT inferred from curve reserves")
    print("Live trading: NOT performed")

    reserve_delta = token["amount_raw"] - decoded["real_token_reserves_raw"]
    print("ATA balance minus real_token_reserves (raw):", reserve_delta)
    print("Reserve relationship: REPORTED ONLY; not assumed equal")
    print("Verification scope: account/PDA/layout checks, not token safety")

    # Pump.fun documents real_token_reserves and token_total_supply as
    # separate bonding-curve fields. Do not assert equality with the ATA's
    # full token balance; report both values for analysis instead.
    return {
        "slot": slot,
        **decoded,
        "curve_token_account": token,
        "ata_minus_real_reserves_raw": reserve_delta,
    }


if __name__ == "__main__":
    try:
        verify()
    except (requests.RequestException, RuntimeError, ValueError, struct.error) as exc:
        print("VERIFICATION FAILED:", exc)
        raise SystemExit(1)
