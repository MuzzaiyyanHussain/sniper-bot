"""Read-only data recorder for strategy research. No keys, no transactions.

Follows every new pump.fun launch (PumpPortal create feed) and polls each
token's bonding-curve account on-chain (getMultipleAccounts, up to 100 per
request) so the price path of every launch can be replayed offline.

usage: python record_launches.py OUT.jsonl SECONDS [RPC_URL]

Rows: {"k":"c",...} creation, {"k":"p",...} curve change. Only changes are written.
"""
import asyncio, base64, json, struct, sys, time

import requests
import websockets

from chain import bonding_curve_address

OUT = sys.argv[1]
DURATION = float(sys.argv[2])
RPC = sys.argv[3] if len(sys.argv) > 3 else "https://api.mainnet-beta.solana.com"
WATCH_SEC = 600
POLL_SEC = 2.0
URL = "wss://pumpportal.fun/api/data"
CURVE = struct.Struct("<8s5QB32sBB32s")  # disc, 5 u64, complete, creator, mayhem, cashback, quote
DISC = bytes([23, 183, 248, 55, 96, 216, 172, 96])
NATIVE = bytes(32)  # all-zero quote mint == native SOL (System Program address)


def fetch(addresses, session):
    resp = session.post(RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
        "params": [addresses, {"encoding": "base64", "commitment": "confirmed"}],
    }, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(str(body["error"])[:80])
    return body["result"]["value"]


async def main():
    end = time.time() + DURATION
    session = requests.Session()
    watching = {}   # mint -> [pda, until, last(vq,vt,cm)]
    stats = dict(creates=0, polls=0, errors=0)
    loop = asyncio.get_running_loop()
    out = open(OUT, "a", encoding="utf-8")

    async def poller():
        while time.time() < end:
            t0 = time.time()
            for m in [m for m, v in watching.items() if t0 >= v[1]]:
                del watching[m]
            mints = list(watching)
            for i in range(0, len(mints), 100):
                chunk = mints[i:i + 100]
                try:
                    vals = await loop.run_in_executor(
                        None, fetch, [watching[m][0] for m in chunk if m in watching], session)
                except Exception as exc:
                    stats["errors"] += 1
                    print("rpc:", type(exc).__name__, str(exc)[:60], flush=True)
                    await asyncio.sleep(3)
                    continue
                now = time.time()
                live = [m for m in chunk if m in watching]
                for m, v in zip(live, vals):
                    if not v:
                        continue
                    try:
                        raw = base64.b64decode(v["data"][0])
                        disc, vt, vq, rt, rq, sup, cm, _cr, may, cash, qm = CURVE.unpack_from(raw)
                    except Exception:
                        continue
                    if disc != DISC:
                        continue
                    key = (vq, vt, cm)
                    if watching[m][2] != key:
                        watching[m][2] = key
                        stats["polls"] += 1
                        out.write(json.dumps({"k": "p", "t": round(now, 2), "m": m,
                                              "vq": vq, "vt": vt, "rq": rq, "cm": cm,
                                              "fl": may * 2 + cash, "nat": int(qm == NATIVE)}) + "\n")
            out.flush()
            await asyncio.sleep(max(0.0, POLL_SEC - (time.time() - t0)))

    task = asyncio.create_task(poller())
    while time.time() < end:
        try:
            async with websockets.connect(URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                while time.time() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    d = json.loads(raw)
                    if not isinstance(d, dict) or d.get("txType") != "create" or "mint" not in d:
                        continue
                    now = time.time()
                    mint = d["mint"]
                    if mint in watching:
                        continue
                    pda = await loop.run_in_executor(None, bonding_curve_address, mint)
                    watching[mint] = [pda, now + WATCH_SEC, None]
                    stats["creates"] += 1
                    out.write(json.dumps({
                        "k": "c", "t": round(now, 2), "m": mint, "sym": d.get("symbol"),
                        "dev": d.get("solAmount"), "vS": d.get("vSolInBondingCurve"),
                        "vT": d.get("vTokensInBondingCurve"),
                        "cr": d.get("traderPublicKey"), "name": d.get("name"),
                        "pool": d.get("pool"), "uri": d.get("uri"),
                    }) + "\n")
        except Exception as exc:
            print("ws:", type(exc).__name__, str(exc)[:60], flush=True)
            await asyncio.sleep(3)
    await task
    out.close()
    print("done", stats, flush=True)


asyncio.run(main())
