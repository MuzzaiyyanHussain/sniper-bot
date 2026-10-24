import base64
import struct
import requests
from solders.pubkey import Pubkey

RPC = "https://api.mainnet-beta.solana.com"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MINT = "mZFQaqWDG2wYuyforqEZXMV4orKTmA1Dskb4G5kpump"
CURVE = "8HdJjVXghWDUmumnZJNC5f1EGQ1sPSMMZ84kVaC9vXk3"
DISC = bytes([23, 183, 248, 55, 96, 216, 172, 96])

TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}


def read_account(address):
    response = requests.post(
        RPC,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [address, {"encoding": "base64", "commitment": "confirmed"}],
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("error"):
        raise RuntimeError(f"RPC error: {payload['error']}")

    account = payload.get("result", {}).get("value")
    if account is None:
        raise RuntimeError(f"Account not found: {address}")

    data = account.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], str):
        raise RuntimeError("Malformed account data encoding")

    try:
        raw = base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Invalid base64 account data") from exc

    return account, raw


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
    virtual_token, virtual_sol, real_token, real_sol, curve_supply = values
    complete = curve_data[48]

    if complete not in (0, 1):
        raise RuntimeError("Invalid complete flag")

    return {
        "decimals": decimals,
        "mint_supply": supply_raw / 10**decimals,
        "virtual_token_reserves": virtual_token / 10**decimals,
        "virtual_quote_reserves": virtual_sol / 1e9,
        "real_token_reserves": real_token / 10**decimals,
        "real_quote_reserves": real_sol / 1e9,
        "curve_supply": curve_supply / 10**decimals,
        "complete": complete,
        "liquidity_usd": None,
    }


def derive_curve_address(mint: str) -> str:
    """Derive the Pump.fun bonding-curve PDA for a mint."""
    program = Pubkey.from_string(PUMP)
    mint_pubkey = Pubkey.from_string(mint)
    pda, _bump = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint_pubkey)],
        program,
    )
    return str(pda)


def verify():
    expected_curve = derive_curve_address(MINT)
    if expected_curve != CURVE:
        raise RuntimeError("Derived bonding curve PDA mismatch")

    mint_account, mint_data = read_account(MINT)
    curve_account, curve_data = read_account(CURVE)
    result = decode_accounts(mint_account, mint_data, curve_account, curve_data)

    print("READ-ONLY VERIFICATION")
    print("Mint:", MINT)
    print("Curve:", CURVE)
    print("Curve owner/discriminator: PASS")
    for key, value in result.items():
        print(f"{key}: {value}")
    print("DexScreener liquidity: NOT inferred from curve reserves")
    print("Live trading: NOT performed")
    return result


if __name__ == "__main__":
    try:
        verify()
    except (requests.RequestException, RuntimeError, ValueError, struct.error) as exc:
        print("VERIFICATION FAILED:", exc)
        raise SystemExit(1)
