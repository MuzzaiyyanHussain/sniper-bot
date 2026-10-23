"""
token_scanner.py
================
Listens to pump.fun token launches via PumpPortal WebSocket.
Detects new tokens within seconds of launch — not minutes.
Falls back to DexScreener polling if WebSocket is unavailable.
"""

import time
import math
import asyncio
import logging
import json
import requests
import websockets
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════════
@dataclass
class ScannerConfig:
    # Age window — with WebSocket we catch tokens in seconds
    max_age_minutes:      int   = 5        # skip if >3 min (already pumped)
    min_age_minutes:      int   = 0         # no minimum — catch at birth

    # Liquidity — pump.fun pools start very small
    min_liquidity_usd:    float = 2_000
    max_liquidity_usd:    float = 500_000

    # Volume
    min_volume_5m_usd:    float = 500

    # Price action
    min_price_change_5m:  float = -30.0     # allow slight dip on entry
    max_price_change_5m:  float = 300.0

    # Market cap
    min_market_cap_usd:   float = 1_000
    max_market_cap_usd:   float = 10_000_000

    # Transactions
    min_txns_5m:          int   = 12
    min_buys_5m:          int   = 8
    min_buy_sell_ratio:   float = 1.3

    # Holder safety
    max_top10_holder_pct: float = 80.0
    min_holder_count:     int   = 30

    # Allowed DEXes
    allowed_dexes: list = field(default_factory=lambda: [
        "raydium", "orca", "meteora", "pump.fun", "fluxbeam", "pumpswap"
    ])

    # WebSocket settings
    ws_endpoints: list = field(default_factory=lambda: [
        "wss://pumpportal.fun/api/data",   # primary — free, no key
        "wss://pumpdev.io/ws",             # fallback
    ])
    max_parallel_evaluations: int = 4

    # How long to wait for DexScreener pair data after token launch (seconds)
    pair_wait_sec:        int   = 25

    # Fallback polling interval if WebSocket fails
    scan_interval_sec:    int   = 30

    # Initial buy filter — skip tokens where dev bought too much at launch
    max_initial_buy_sol:  float = 20.0      # skip if dev bought >20 SOL at launch. Was 50.


# ════════════════════════════════════════════════════════════════════════════════
# TOKEN CANDIDATE
# ════════════════════════════════════════════════════════════════════════════════
@dataclass
class TokenCandidate:
    mint:            str
    symbol:          str
    name:            str
    dex:             str
    pair_address:    str
    price_usd:       float
    liquidity_usd:   float
    volume_5m:       float
    volume_1h:       float
    price_change_5m: float
    price_change_1h: float
    market_cap:      float
    txns_5m_buys:    int
    txns_5m_sells:   int
    age_minutes:     float
    holder_count:    int   = 0
    top10_pct:       float = 0.0
    score:           float = 0.0
    url:             str   = ""
    initial_buy_sol: float = 0.0   # dev's initial buy at launch

    @property
    def buy_sell_ratio(self) -> float:
        return self.txns_5m_buys / max(self.txns_5m_sells, 1)


# ════════════════════════════════════════════════════════════════════════════════
# RUG DETECTOR
# ════════════════════════════════════════════════════════════════════════════════
class RugDetector:
    SCAM_KEYWORDS = [
        "guaranteed", "100x", "airdrop", "giveaway",
        "claim", "presale", "stealth launch", "safemoon",
    ]

    @staticmethod
    def is_suspicious_name(name: str, symbol: str) -> bool:
        combined = (name + " " + symbol).lower()
        return any(kw in combined for kw in RugDetector.SCAM_KEYWORDS)

    @staticmethod
    def has_honeypot_pattern(t: TokenCandidate) -> bool:
        return t.txns_5m_buys > 50 and t.txns_5m_sells < 2

    @staticmethod
    def has_wash_trading(t: TokenCandidate) -> bool:
        if t.liquidity_usd > 0:
            return (t.volume_5m / t.liquidity_usd) > 50
        return False

    def check(self, t: TokenCandidate) -> tuple:
        if self.is_suspicious_name(t.name, t.symbol):
            return True, f"suspicious name: {t.name}"
        if self.has_honeypot_pattern(t):
            return True, "honeypot pattern (buys >> sells)"
        if self.has_wash_trading(t):
            return True, "wash trading (volume >> liquidity)"
        return False, ""


# ════════════════════════════════════════════════════════════════════════════════
# SCORER
# ════════════════════════════════════════════════════════════════════════════════
class TokenScorer:
    def score(self, t: TokenCandidate) -> float:
        s = 0.0

        # Momentum (0-30) — being early is better than big % gain
        # At launch price change is often 0 or slightly negative — don't penalise
        chg = min(max(t.price_change_5m, -10), 100)
        s += (max(chg, 0) / 100) * 20
        # Bonus for being very fresh
        if t.age_minutes < 2:
            s += 10
        elif t.age_minutes < 5:
            s += 5

        # Buy pressure (0-25)
        ratio = min(t.buy_sell_ratio, 5)
        s += (ratio / 5) * 25

        # Volume (0-20)
        vol_score = min(math.log10(max(t.volume_5m, 1)) / math.log10(100_000), 1)
        s += vol_score * 20

        # Liquidity sweet spot (0-15)
        if 5_000 <= t.liquidity_usd <= 50_000:
            s += 15
        elif 1_000 <= t.liquidity_usd < 5_000:
            s += 8
        elif 50_000 < t.liquidity_usd <= 200_000:
            s += 10

        # Initial buy check (0-10)
        # Small initial buy = fairer launch
        if t.initial_buy_sol == 0 or t.initial_buy_sol < 1:
            s += 10
        elif t.initial_buy_sol < 5:
            s += 6
        elif t.initial_buy_sol < 20:
            s += 3

        return round(s, 2)


# ════════════════════════════════════════════════════════════════════════════════
# DEXSCREENER  (used to get pair data after WebSocket detects the mint)
# ════════════════════════════════════════════════════════════════════════════════
class DexScreenerClient:
    HEADERS = {"User-Agent": "Mozilla/5.0"}

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)

    def get_pairs_for_mint(self, mint: str) -> list:
        try:
            resp = self.session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=2,
            )
            resp.raise_for_status()
            return resp.json().get("pairs") or []
        except Exception as e:
            log.debug(f"DexScreener error for {mint[:8]}: {e}")
            return []

    def parse_pair(self, pair: dict, initial_buy_sol: float = 0, expected_mint: str | None = None) -> "TokenCandidate | None":
        try:
            if pair.get("chainId") != "solana":
                return None
            base = pair.get("baseToken", {})
            mint = base.get("address", "")
            if not mint:
                return None
            if expected_mint and mint.lower() != expected_mint.lower():
                return None

            created_at = pair.get("pairCreatedAt")
            age_min = ((time.time() - created_at / 1000) / 60) if created_at else 999

            liquidity  = pair.get("liquidity", {}).get("usd", 0) or 0
            volume     = pair.get("volume", {})
            price_chg  = pair.get("priceChange", {})
            txns       = pair.get("txns", {})
            txns_5m    = txns.get("m5", {})
            market_cap = pair.get("marketCap") or pair.get("fdv") or 0

            return TokenCandidate(
                mint             = mint,
                symbol           = base.get("symbol", "???"),
                name             = base.get("name", "Unknown"),
                dex              = pair.get("dexId", "").lower(),
                pair_address     = pair.get("pairAddress", ""),
                price_usd        = float(pair.get("priceUsd") or 0),
                liquidity_usd    = float(liquidity),
                volume_5m        = float(volume.get("m5") or 0),
                volume_1h        = float(volume.get("h1") or 0),
                price_change_5m  = float(price_chg.get("m5") or 0),
                price_change_1h  = float(price_chg.get("h1") or 0),
                market_cap       = float(market_cap),
                txns_5m_buys     = int(txns_5m.get("buys") or 0),
                txns_5m_sells    = int(txns_5m.get("sells") or 0),
                age_minutes      = age_min,
                initial_buy_sol  = initial_buy_sol,
                url              = pair.get("url", f"https://dexscreener.com/solana/{mint}"),
            )
        except Exception as e:
            log.debug(f"Parse error: {e}")
            return None


# ════════════════════════════════════════════════════════════════════════════════
# WEBSOCKET LISTENER
# ════════════════════════════════════════════════════════════════════════════════
class PumpFunListener:
    """
    Connects to PumpPortal WebSocket and listens for new token creation events.
    When a token is created, waits for DexScreener pair data, then returns it.
    """

    def __init__(self, cfg: ScannerConfig):
        self.cfg = cfg
        self.dex = DexScreenerClient()
        self.rug = RugDetector()
        self.scorer = TokenScorer()

    def _passes_filters(self, t: TokenCandidate) -> tuple:
        cfg = self.cfg
        if t.age_minutes > cfg.max_age_minutes:
            return False, f"too old ({t.age_minutes:.1f}m)"
        if t.liquidity_usd < cfg.min_liquidity_usd:
            return False, f"low liquidity (${t.liquidity_usd:,.0f})"
        if t.liquidity_usd > cfg.max_liquidity_usd:
            return False, f"liquidity too high"
        if t.volume_5m < cfg.min_volume_5m_usd:
            return False, f"low volume (${t.volume_5m:,.0f})"
        if t.price_change_5m < cfg.min_price_change_5m:
            return False, f"dumping ({t.price_change_5m:.1f}%)"
        if t.market_cap < cfg.min_market_cap_usd:
            return False, f"mcap too low"
        if t.market_cap > cfg.max_market_cap_usd:
            return False, f"mcap too high"
        if (t.txns_5m_buys + t.txns_5m_sells) < cfg.min_txns_5m:
            return False, f"low activity ({t.txns_5m_buys + t.txns_5m_sells} txns)"
        if t.txns_5m_buys < cfg.min_buys_5m:
            return False, f"few buys ({t.txns_5m_buys})"
        if t.buy_sell_ratio < cfg.min_buy_sell_ratio:
            return False, f"sell pressure ({t.buy_sell_ratio:.1f}x)"
        if t.dex not in cfg.allowed_dexes:
            return False, f"unknown dex ({t.dex})"
        if t.initial_buy_sol > cfg.max_initial_buy_sol:
            return False, f"dev bought too much ({t.initial_buy_sol:.1f} SOL)"
        return True, ""

    def _evaluate_mint(self, mint: str, name: str, symbol: str,
                       initial_buy_sol: float) -> "TokenCandidate | None":
        """
        Wait for DexScreener to index the pair, then evaluate.
        Called after WebSocket notifies us of a new token.
        """
        log.info(f"   🔎 Checking pair data — {symbol} ({mint[:8]}...)")
        deadline = time.monotonic() + 8
        pairs = []

        while time.monotonic() < deadline:
            pairs = self.dex.get_pairs_for_mint(mint)
            if pairs:
                break
            time.sleep(0.5)

        if not pairs:
            log.info(f"   ⚠️ No pairs found for {symbol} after retries — skipping")
            return None

        pairs_sol = [
            p for p in pairs
            if p.get("chainId") == "solana"
            and (p.get("baseToken", {}).get("address") or "").lower() == mint.lower()
        ]
        if not pairs_sol:
            return None
        best_pair = max(pairs_sol, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)

        t = self.dex.parse_pair(best_pair, initial_buy_sol, expected_mint=mint)
        if not t:
            return None

        # Rug check
        is_rug, reason = self.rug.check(t)
        if is_rug:
            log.warning(f"   🚨 RUG detected {symbol}: {reason}")
            return None

        # Filter check
        ok, reason = self._passes_filters(t)
        if not ok:
            log.info(f"   SKIP {symbol}: {reason}")
            return None

        t.score = self.scorer.score(t)
        return t

    async def _listen_ws(self, endpoint: str, result_queue: asyncio.Queue,
                         stop_event: asyncio.Event):
        """Connect to WebSocket and push qualifying tokens to queue."""
        log.info(f"📡 Connecting to {endpoint}...")
        try:
            async with websockets.connect(endpoint, ping_interval=20,
                                         ping_timeout=10) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                log.info("   ✅ Subscribed to new token launches")

                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)

                        tx_type = data.get("txType") or data.get("type", "")
                        mint = data.get("mint") or data.get("address", "")
                        name = data.get("name", "Unknown")
                        symbol = data.get("symbol", "???")
                        sol_amt = float(data.get("solAmount") or data.get("initialBuy", 0) or 0)

                        if not mint:
                           continue

                        if tx_type in ("create", "mint", "") or "mint" in data:
                           log.info(f"\n🚀 NEW TOKEN: {symbol} ({name})")
                           log.info(f"   Mint:        {mint}")
                           log.info(f"   Initial buy: {sol_amt:.2f} SOL")

                           if not hasattr(self, "_pending_mints"):
                               self._pending_mints = set()
                               self._eval_semaphore = asyncio.Semaphore(self.cfg.max_parallel_evaluations)
                               self._eval_tasks = set()

                           if mint in self._pending_mints:
                               continue

                           self._pending_mints.add(mint)

                           async def evaluate_and_queue(token_mint, token_name, token_symbol, token_sol_amt):
                               try:
                                   async with self._eval_semaphore:
                                       if stop_event.is_set():
                                           return
                                       loop = asyncio.get_running_loop()
                                       candidate = await loop.run_in_executor(
                                           None,
                                           self._evaluate_mint,
                                           token_mint,
                                           token_name,
                                           token_symbol,
                                           token_sol_amt,
                                       )
                                       if candidate and not stop_event.is_set():
                                           await result_queue.put(candidate)
                               except Exception:
                                   log.exception("Token evaluation failed for %s", token_mint)
                               finally:
                                   self._pending_mints.discard(token_mint)

                           task = asyncio.create_task(
                               evaluate_and_queue(mint, name, symbol, sol_amt)
                           )
                           self._eval_tasks.add(task)
                           task.add_done_callback(self._eval_tasks.discard)

                    except asyncio.TimeoutError:
                        await ws.ping()
                    except Exception as e:
                        log.warning(f"   WS message error: {e}")
                        break

        except Exception as e:
            log.warning(f"   WS connection failed ({endpoint}): {e}")

    async def _listen_with_fallback(self, result_queue: asyncio.Queue,
                                    stop_event: asyncio.Event):
        """
        Try WebSocket endpoints with automatic reconnection.
        Cycles through all endpoints indefinitely rather than giving up
        and falling back to polling permanently — the 'NoneType resume_reading'
        error causes a break out of the inner loop, and without a retry loop
        the scanner silently falls to slow DexScreener polling for the rest
        of the scan window.
        """
        ws_attempt = 0
        endpoints = self.cfg.ws_endpoints

        while not stop_event.is_set():
            endpoint = endpoints[ws_attempt % len(endpoints)]
            await self._listen_ws(endpoint, result_queue, stop_event)

            if stop_event.is_set():
                break

            ws_attempt += 1
            # Brief pause before reconnecting to avoid hammering the endpoint
            # on a rapid-disconnect loop
            reconnect_delay = min(5 * (ws_attempt % 4 + 1), 30)
            log.warning(
                f"   WebSocket disconnected (attempt {ws_attempt}) — "
                f"reconnecting in {reconnect_delay}s..."
            )
            await asyncio.sleep(reconnect_delay)

    async def _poll_fallback(self, result_queue: asyncio.Queue,
                             stop_event: asyncio.Event):
        """DexScreener polling fallback when WebSocket is unavailable."""
        seen = set()
        while not stop_event.is_set():
            log.info("🔍 [Fallback] Polling DexScreener...")
            mints = set()
            for url in [
                "https://api.dexscreener.com/token-profiles/latest/v1",
                "https://api.dexscreener.com/token-boosts/latest/v1",
            ]:
                try:
                    resp = requests.get(url, timeout=10)
                    resp.raise_for_status()
                    items = resp.json() if isinstance(resp.json(), list) else []
                    for item in items:
                        m = item.get("tokenAddress", "")
                        if m and item.get("chainId") == "solana":
                            mints.add(m)
                except Exception as e:
                    log.debug(f"Fallback poll error: {e}")

            for mint in mints:
                if mint in seen or stop_event.is_set():
                    continue
                seen.add(mint)
                pairs = self.dex.get_pairs_for_mint(mint)
                for pair in pairs:
                    t = self.dex.parse_pair(pair, 0)
                    if not t:
                        continue
                    is_rug, _ = self.rug.check(t)
                    if is_rug:
                        continue
                    ok, reason = self._passes_filters(t)
                    if not ok:
                        log.info(f"   SKIP {t.symbol}: {reason}")
                        continue
                    t.score = self.scorer.score(t)
                    await result_queue.put(t)
                    break

            await asyncio.sleep(self.cfg.scan_interval_sec)

    def find_best(self, max_wait_minutes: int = 15) -> "TokenCandidate | None":
        """
        Block until a qualifying token is found or timeout.
        Runs the async WebSocket listener in a dedicated event loop.
        """
        result_queue: asyncio.Queue = asyncio.Queue()
        stop_event = asyncio.Event()
        result = [None]

        async def runner():
            listener_task = asyncio.create_task(
                self._listen_with_fallback(result_queue, stop_event)
            )
            try:
                # Wait for first qualifying token or timeout
                candidate = await asyncio.wait_for(
                    result_queue.get(),
                    timeout=max_wait_minutes * 60
                )
                result[0] = candidate
            except asyncio.TimeoutError:
                log.warning(f"No qualifying token found in {max_wait_minutes} minutes")
            finally:
                stop_event.set()
                listener_task.cancel()
                try:
                    await listener_task
                except asyncio.CancelledError:
                    pass

        asyncio.run(runner())
        return result[0]


# ════════════════════════════════════════════════════════════════════════════════
# PUBLIC INTERFACE  (same API as before — bot.py doesn't need changes)
# ════════════════════════════════════════════════════════════════════════════════
class TokenScanner:
    """Drop-in replacement — same interface as the old polling scanner."""

    def __init__(self, cfg: ScannerConfig, birdeye_api_key: str = ""):
        self.cfg      = cfg
        self.listener = PumpFunListener(cfg)

    def find_best(self, max_wait_minutes: int = 15) -> "TokenCandidate | None":
        log.info("📡 Listening for new pump.fun launches via WebSocket...")
        candidate = self.listener.find_best(max_wait_minutes)
        if candidate:
            _log_token(candidate)
        return candidate

    def scan_once(self) -> list:
        """Compatibility method — runs one WebSocket listen cycle."""
        candidate = self.listener.find_best(max_wait_minutes=2)
        return [candidate] if candidate else []


# ════════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════════
def _log_token(t: TokenCandidate):
    log.info(f"\n✅ CANDIDATE SELECTED:")
    log.info(f"   Token:       {t.symbol} ({t.name})")
    log.info(f"   Mint:        {t.mint}")
    log.info(f"   DEX:         {t.dex}")
    log.info(f"   Age:         {t.age_minutes:.1f} minutes")
    log.info(f"   Price:       ${t.price_usd:.8f}")
    log.info(f"   Liquidity:   ${t.liquidity_usd:,.0f}")
    log.info(f"   Volume 5m:   ${t.volume_5m:,.0f}")
    log.info(f"   Chg 5m:      {t.price_change_5m:+.1f}%")
    log.info(f"   Buys/Sells:  {t.txns_5m_buys}/{t.txns_5m_sells} ({t.buy_sell_ratio:.1f}x)")
    log.info(f"   Mcap:        ${t.market_cap:,.0f}")
    log.info(f"   Initial buy: {t.initial_buy_sol:.2f} SOL")
    log.info(f"   Score:       {t.score}/100")
    log.info(f"   URL:         {t.url}")


# ════════════════════════════════════════════════════════════════════════════════
# STANDALONE TEST
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n📡 WebSocket Token Scanner — listening for new pump.fun launches...\n")
    print("Will wait up to 5 minutes for a qualifying token.")
    print("Press Ctrl+C to stop.\n")

    cfg     = ScannerConfig()
    scanner = TokenScanner(cfg)

    try:
        candidate = scanner.find_best(max_wait_minutes=5)
        if candidate:
            print(f"\n🎯 Would buy: {candidate.symbol} @ ${candidate.price_usd:.8f}")
            print(f"   Mint: {candidate.mint}")
            print(f"   Score: {candidate.score}/100")
        else:
            print("\nNo qualifying token found in 5 minutes.")
            print("This is normal — good tokens don't launch every minute.")
            print("In live mode the bot waits up to 15 minutes per cycle.")
    except KeyboardInterrupt:
        print("\n⏹  Stopped.")
