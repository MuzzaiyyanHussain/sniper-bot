"""
token_scanner.py
================
Listens to pump.fun token launches via PumpPortal WebSocket.
Detects new tokens within seconds of launch — not minutes.
Falls back to DexScreener polling if WebSocket is unavailable.
"""

import time
import math
import re
import asyncio
import logging
import json
import requests
import websockets
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


def parse_sol_amount(event: dict, max_reasonable_sol: float = 1_000.0) -> float | None:
    """Return the documented SOL-spend field, or None when it is absent.

    PumpPortal's `initialBuy` is a token amount, not SOL. This function never
    falls back to it. Invalid supplied values raise ValueError so callers can
    skip that event rather than silently trusting a zero.
    """
    raw = event.get("solAmount")
    if raw is None:
        return None
    try:
        amount = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid solAmount: {raw!r}") from exc
    if not math.isfinite(amount) or amount < 0 or amount > max_reasonable_sol:
        raise ValueError(f"implausible solAmount: {raw!r}")
    return amount


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
        "raydium", "orca", "meteora", "pump.fun", "pumpfun", "pumpswap", "fluxbeam"
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
    initial_buy_sol: float | None = None   # SOL spent by creator, if supplied by the event

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

        # Initial buy check (0-10). Unknown is neutral: do not treat missing
        # event data as a zero-SOL creator buy.
        if t.initial_buy_sol is not None:
            if t.initial_buy_sol < 1:
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


    def get_pairs_for_mint(self, mint: str, timeout: float = 5.0) -> list:
        """Fetch pairs and distinguish API failures from valid empty results."""
        try:
            resp = self.session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=min(5.0, timeout),
            )
            resp.raise_for_status()
            data = resp.json()

            if not isinstance(data, dict):
                log.warning(
                    "DexScreener unexpected response for %s: expected object",
                    mint[:8],
                )
                return []

            pairs = data.get("pairs")
            if pairs is None:
                return []

            if not isinstance(pairs, list):
                log.warning(
                    "DexScreener invalid pairs field for %s",
                    mint[:8],
                )
                return []

            return pairs

        except requests.RequestException as e:
            log.warning(
                "DexScreener HTTP/network failure for %s: %s: %s",
                mint[:8], type(e).__name__, e,
            )
            return []
        except ValueError as e:
            log.warning(
                "DexScreener invalid JSON for %s: %s",
                mint[:8], e,
            )
            return []


    def parse_pair(
        self,
        pair: dict,
        initial_buy_sol: float | None = None,
        expected_mint: str | None = None,
    ) -> "TokenCandidate | None":
        """Parse DexScreener data, rejecting malformed/non-finite numeric values."""
        try:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                return None

            base = pair.get("baseToken")
            if not isinstance(base, dict):
                return None

            mint = base.get("address", "")
            if not isinstance(mint, str) or not mint:
                return None
            if expected_mint and mint.lower() != expected_mint.lower():
                return None

            def number(value, field, default=0.0):
                if value is None or value == "":
                    return default
                try:
                    result = float(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"Invalid {field}") from exc
                if not math.isfinite(result):
                    raise ValueError(f"Non-finite {field}")
                return result

            def nonnegative(value, field, default=0.0):
                result = number(value, field, default)
                if result < 0:
                    raise ValueError(f"Negative {field}")
                return result

            created_at = pair.get("pairCreatedAt")
            now_ms = time.time() * 1000
            if created_at is not None:
                created_at = number(created_at, "pairCreatedAt")
                if created_at > now_ms + 5 * 60 * 1000:
                    log.warning(
                        "Future/invalid pairCreatedAt for %s; skipping pair",
                        mint[:8],
                    )
                    return None
                age_min = max(0.0, (now_ms - created_at) / 60_000)
                log.info(
                    "AGE DEBUG | mint=%s | createdAt_ms=%s | now_ms=%.0f | age_min=%.2f",
                    mint[:8], created_at, now_ms, age_min,
                )
            else:
                age_min = 999.0
                log.info(
                    "AGE DEBUG | mint=%s | createdAt_ms=missing | age_min=%.2f",
                    mint[:8], age_min,
                )

            liquidity_data = pair.get("liquidity")
            liquidity_raw = (
                liquidity_data.get("usd")
                if isinstance(liquidity_data, dict)
                else None
            )
            if liquidity_raw is None:
                log.warning(
                    "SKIP-CANDIDATE: liquidity unavailable from DexScreener "
                    "| mint=%s | dex=%s | pair=%s | price_usd=%s "
                    "| eligibility=FAIL-CLOSED",
                    mint,
                    pair.get("dexId", "unknown"),
                    pair.get("pairAddress", "unknown"),
                    pair.get("priceUsd", "unknown"),
                )
                return None
            liquidity = nonnegative(liquidity_raw, "liquidity")

            volume = pair.get("volume") or {}
            price_change = pair.get("priceChange") or {}
            txns = pair.get("txns") or {}
            if not all(
                isinstance(item, dict)
                for item in (volume, price_change, txns)
            ):
                return None

            txns_5m = txns.get("m5") or {}
            if not isinstance(txns_5m, dict):
                return None

            price_usd = nonnegative(pair.get("priceUsd"), "priceUsd")
            volume_5m = nonnegative(volume.get("m5"), "volume.m5")
            volume_1h = nonnegative(volume.get("h1"), "volume.h1")
            price_change_5m = number(
                price_change.get("m5"), "priceChange.m5"
            )
            price_change_1h = number(
                price_change.get("h1"), "priceChange.h1"
            )

            market_cap_raw = pair.get("marketCap")
            if market_cap_raw is None or market_cap_raw == "":
                market_cap_raw = pair.get("fdv")
            market_cap = nonnegative(market_cap_raw, "marketCap/fdv")

            buys = number(txns_5m.get("buys"), "txns.m5.buys")
            sells = number(txns_5m.get("sells"), "txns.m5.sells")
            if (
                buys < 0
                or sells < 0
                or not buys.is_integer()
                or not sells.is_integer()
            ):
                return None

            return TokenCandidate(
                mint=mint,
                symbol=base.get("symbol", "???"),
                name=base.get("name", "Unknown"),
                dex=str(pair.get("dexId") or "").lower(),
                pair_address=str(pair.get("pairAddress") or ""),
                price_usd=price_usd,
                liquidity_usd=liquidity,
                volume_5m=volume_5m,
                volume_1h=volume_1h,
                price_change_5m=price_change_5m,
                price_change_1h=price_change_1h,
                market_cap=market_cap,
                txns_5m_buys=int(buys),
                txns_5m_sells=int(sells),
                age_minutes=age_min,
                initial_buy_sol=initial_buy_sol,
                url=pair.get("url") or f"https://dexscreener.com/solana/{mint}",
            )
        except (TypeError, ValueError, OverflowError) as exc:
            log.warning("Invalid pair data; skipping pair: %s", exc)
            return None
        except Exception as exc:
            log.debug("Parse error: %s", exc)
            return None


class BirdeyeHolderClient:
    """Fetch verified Solana holder metrics; missing/invalid data stays unknown."""
    BASE_URL = "https://public-api.birdeye.so"

    def __init__(self, api_key: str, timeout: float = 6.0):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "X-API-KEY": self.api_key,
            "x-chain": "solana",
        })
        self._cache: dict[str, tuple[float, int, float]] = {}
        self.cache_ttl_sec = 30.0
        self._rate_limit_until = 0.0

    @staticmethod
    def _find_number(obj, aliases):
        """Search nested API payloads for a known field alias, without guessing values."""
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key.lower() in aliases and value is not None:
                    try:
                        number = float(value)
                        if math.isfinite(number):
                            return number
                    except (TypeError, ValueError, OverflowError):
                        pass
            for value in obj.values():
                found = BirdeyeHolderClient._find_number(value, aliases)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = BirdeyeHolderClient._find_number(value, aliases)
                if found is not None:
                    return found
        return None

    def get_metrics(self, mint: str) -> tuple[int, float] | None:
        """Return (current holder count, top-10 supply %) or None if unverified.

        The API's holder_summary and top10_holder are parsed separately so a
        tagged cohort's count/percentage cannot accidentally be mistaken for
        total holders or top-10 concentration.
        """
        if not self.api_key:
            log.debug("Birdeye API key missing; holder metrics unavailable")
            return None
        now = time.monotonic()
        if now < self._rate_limit_until:
            log.debug("Birdeye holder lookup cooldown active for %s", mint[:8])
            return None
        cached = self._cache.get(mint)
        if cached and now - cached[0] < self.cache_ttl_sec:
            return cached[1], cached[2]

        try:
            response = self.session.get(
                f"{self.BASE_URL}/token/v1/holder-profile",
                params={"token_address": mint, "include_zero_balance": "false"},
                timeout=self.timeout,
            )
            if response.status_code == 429:
                response_received_at = time.monotonic()
                retry_after = response.headers.get("Retry-After")
                try:
                    cooldown = max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    cooldown = self.cache_ttl_sec
                self._rate_limit_until = response_received_at + cooldown
                retry_after_log = repr(retry_after)[:100]
                log.warning(
                    "Birdeye holder lookup rate-limited for %s; "
                    "Retry-After=%s; cooldown %.1fs",
                    mint[:8], retry_after_log, cooldown,
                )
                return None
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("success") is False:
                return None
            data = payload.get("data", payload)
            if not isinstance(data, dict):
                return None

            holder_summary = data.get("holder_summary")
            token_summary = data.get("token")
            # Birdeye's live holder-profile response nests top-10 metrics at
            # data.token.top10_holder (not data.top10_holder).
            top10_summary = data.get("top10_holder")
            if top10_summary is None and isinstance(token_summary, dict):
                top10_summary = token_summary.get("top10_holder")
            # API versions may expose total holder count in token or holder_summary.
            count_aliases = {
                "holder_count", "holdercount", "total_holder", "total_holders",
                "totalholder", "totalholders", "holder_num", "holders_count",
            }
            count = self._find_number(holder_summary, count_aliases)
            if count is None:
                count = self._find_number(token_summary, count_aliases)
            if count is None:
                # Some response versions put total count directly on data.
                count = self._find_number({k: v for k, v in data.items()
                                           if k not in {"tags", "top10_holder", "holder_summary"}},
                                          count_aliases)

            concentration_aliases = {
                "top10holderpercent", "top_10_holder_percent", "top10_holder_percent",
                "top10holderpercentage", "top_10_holder_percentage", "percent_of_supply",
                "percentage_of_supply", "supply_percent", "supply_percentage",
            }
            concentration = self._find_number(top10_summary, concentration_aliases)
            if concentration is None:
                # Explicitly named token-level concentration is also valid.
                concentration = self._find_number(token_summary, {
                    "top10holderpercent", "top_10_holder_percent", "top10_holder_percent",
                })
            if concentration is None:
                # Optional fallback only. Some API keys cannot access token_security;
                # that failure must not discard valid holder-profile data.
                try:
                    security_response = self.session.get(
                        f"{self.BASE_URL}/defi/token_security",
                        params={"address": mint}, timeout=self.timeout,
                    )
                    security_response.raise_for_status()
                    security_payload = security_response.json()
                    if isinstance(security_payload, dict):
                        concentration = self._find_number(
                            security_payload.get("data", security_payload),
                            {"top10holderpercent", "top_10_holder_percent", "top10_holder_percent"},
                        )
                except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                    log.debug("Optional token-security fallback unavailable for %s: %s", mint[:8], exc)

            if (count is None or concentration is None or count < 1
                    or not count.is_integer()):
                log.info("Birdeye holder response lacks valid metrics for %s", mint[:8])
                return None
            # Values are percentages in documented responses; normalize 0..1 ratios.
            if 0.0 <= concentration <= 1.0:
                concentration *= 100.0
            if not 0.0 <= concentration <= 100.0:
                return None
            result = (int(count), float(concentration))
            self._cache[mint] = (now, result[0], result[1])
            return result
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            log.info("Birdeye holder lookup unavailable for %s: %s", mint[:8], exc)
            return None


# ════════════════════════════════════════════════════════════════════════════════
# WEBSOCKET LISTENER
# ════════════════════════════════════════════════════════════════════════════════
class PumpFunListener:
    """
    Connects to PumpPortal WebSocket and listens for new token creation events.
    When a token is created, waits for DexScreener pair data, then returns it.
    """

    def __init__(self, cfg: ScannerConfig, birdeye_api_key: str = ""):
        self.cfg = cfg
        self.dex = DexScreenerClient()
        self.holders = BirdeyeHolderClient(birdeye_api_key)
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
        if t.holder_count <= 0 or t.top10_pct <= 0.0:
            return False, "holder data unavailable or unverified"
        if t.holder_count < cfg.min_holder_count:
            return False, f"too few holders ({t.holder_count})"
        if t.top10_pct > cfg.max_top10_holder_pct:
            return False, f"top-10 holders too concentrated ({t.top10_pct:.1f}%)"
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
        if (t.initial_buy_sol is not None
                and t.initial_buy_sol > cfg.max_initial_buy_sol):
            return False, f"dev bought too much ({t.initial_buy_sol:.1f} SOL)"
        return True, ""

    def _evaluate_mint(self, mint: str, name: str, symbol: str,
                       initial_buy_sol: float | None) -> "TokenCandidate | None":
        """Wait for DexScreener pair data, then evaluate the token."""
        log.info("   🔎 Checking pair data — %s (%s...)", symbol, mint[:8])

        # Honor the configured wait. The HTTP request itself has a bounded timeout.
        deadline = time.monotonic() + max(1, self.cfg.pair_wait_sec)
        matching_pairs = []

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            pairs = self.dex.get_pairs_for_mint(mint, timeout=remaining)
            matching_pairs = [
                pair for pair in pairs
                if pair.get("chainId") == "solana"
                and (pair.get("baseToken", {}).get("address") or "").lower()
                == mint.lower()
            ]
            has_liquidity = any(
                isinstance(pair.get("liquidity"), dict)
                and pair["liquidity"].get("usd") is not None
                for pair in matching_pairs
            )
            if matching_pairs and has_liquidity:
                break

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1.0, remaining))

        if not matching_pairs:
            log.info(
                "   ⚠️ No matching pairs found for %s after retries — skipping",
                symbol,
            )
            return None

        # Evaluate each exact-mint pair independently. An old/high-liquidity
        # pool must not hide another pair that may pass the existing filters.
        eligible = []

        for pair in matching_pairs:
            candidate = self.dex.parse_pair(
                pair, initial_buy_sol, expected_mint=mint
            )
            if candidate is None:
                continue

            holder_metrics = self.holders.get_metrics(candidate.mint)
            if holder_metrics is not None:
                candidate.holder_count, candidate.top10_pct = holder_metrics

            is_rug, reason = self.rug.check(candidate)
            if is_rug:
                log.warning(
                    "   RUG detected %s on %s: %s",
                    symbol, candidate.dex, reason
                )
                continue

            ok, reason = self._passes_filters(candidate)
            if not ok:
                log.info(
                    "   SKIP %s on %s: %s",
                    symbol, candidate.dex, reason
                )
                continue

            candidate.score = self.scorer.score(candidate)
            eligible.append(candidate)

        if not eligible:
            return None

        return max(eligible, key=lambda candidate: candidate.score)

    async def _listen_ws(self, endpoint: str, result_queue: asyncio.Queue,
                         stop_event: asyncio.Event):
        """Connect to PumpPortal and evaluate new-token events concurrently."""
        log.info(f"📡 Connecting to {endpoint}...")
        if not hasattr(self, "_pending_mints"):
            self._pending_mints = set()
            self._evaluated_mints = set()
            self._eval_semaphore = asyncio.Semaphore(self.cfg.max_parallel_evaluations)
            self._eval_tasks = set()

        try:
            async with websockets.connect(endpoint, ping_interval=20,
                                          ping_timeout=10) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                log.info("   ✅ Subscribed to new token launches")

                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        await ws.ping()
                        continue

                    try:
                        data = json.loads(msg)
                        if not isinstance(data, dict):
                            raise ValueError("WebSocket event must be a JSON object")
                        tx_type = data.get("txType") or data.get("type", "")
                        mint = data.get("mint") or data.get("address", "")
                        name = str(data.get("name") or "Unknown")
                        symbol = str(data.get("symbol") or "???")
                        if not isinstance(mint, str) or not mint.strip():
                            log.debug("Skipping WebSocket event without a mint")
                            continue

                        # PumpPortal's creation event uses `initialBuy` for the
                        # token amount and `solAmount` for SOL spent. Never use
                        # initialBuy as a SOL fallback. Missing solAmount remains
                        # unknown; malformed or absurd values reject this event.
                        try:
                            sol_amt = parse_sol_amount(data)
                        except ValueError as e:
                            log.warning("Skipping %s: %s", symbol, e)
                            continue

                        if not (tx_type in ("create", "mint", "") or "mint" in data):
                            continue

                        log.info("\n🚀 NEW TOKEN: %s (%s)", symbol, name)
                        log.info("   Mint:        %s", mint)
                        if sol_amt is None:
                            log.info("   Initial buy: unknown (solAmount not supplied)")
                        else:
                            log.info("   Initial buy: %.4f SOL", sol_amt)

                        if (mint in self._pending_mints
                                or mint in self._evaluated_mints):
                            continue
                        self._pending_mints.add(mint)

                        async def evaluate_and_queue(token_mint, token_name, token_symbol, token_sol_amt):
                            try:
                                async with self._eval_semaphore:
                                    if stop_event.is_set():
                                        return
                                    loop = asyncio.get_running_loop()
                                    candidate = await loop.run_in_executor(
                                        None, self._evaluate_mint, token_mint, token_name,
                                        token_symbol, token_sol_amt,
                                    )
                                    if candidate and not stop_event.is_set():
                                        await result_queue.put(candidate)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                log.exception("Token evaluation failed for %s", token_mint)
                            finally:
                                self._pending_mints.discard(token_mint)
                                self._evaluated_mints.add(token_mint)

                        task = asyncio.create_task(
                            evaluate_and_queue(mint, name, symbol, sol_amt)
                        )
                        self._eval_tasks.add(task)
                        task.add_done_callback(self._eval_tasks.discard)
                    except (json.JSONDecodeError, ValueError, TypeError) as e:
                        # Bad individual messages must not tear down the socket.
                        log.warning("Skipping malformed WebSocket message: %s", e)
                    except Exception as e:
                        log.warning("   WS message processing error: %s", e)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("   WS connection failed (%s): %s", endpoint, e)
        finally:
            # When the scan ends, stop async evaluations and wait for them. Any
            # executor work already running cannot be forcibly killed; the pair
            # lookup itself has bounded request/retry timeouts.
            if stop_event.is_set():
                tasks = list(getattr(self, "_eval_tasks", set()))
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

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
        liquidity_retries = {}
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
                pairs = self.dex.get_pairs_for_mint(mint)
                matching_pairs = [
                    pair for pair in pairs
                    if pair.get("chainId") == "solana"
                    and (pair.get("baseToken", {}).get("address") or "").lower()
                    == mint.lower()
                ]
                has_valid_liquidity = any(
                    isinstance(pair.get("liquidity"), dict)
                    and pair["liquidity"].get("usd") is not None
                    for pair in matching_pairs
                )
                has_missing_liquidity = any(
                    not isinstance(pair.get("liquidity"), dict)
                    or pair["liquidity"].get("usd") is None
                    for pair in matching_pairs
                )
                if has_missing_liquidity and not has_valid_liquidity:
                    retries = liquidity_retries.get(mint, 0)
                    if retries < 1:
                        liquidity_retries[mint] = retries + 1
                        continue
                    seen.add(mint)
                    continue

                seen.add(mint)
                for pair in pairs:
                    t = self.dex.parse_pair(pair, None, expected_mint=mint)
                    if not t:
                        continue
                    holder_metrics = self.holders.get_metrics(t.mint)
                    if holder_metrics is not None:
                        t.holder_count, t.top10_pct = holder_metrics
                    is_rug, rug_reason = self.rug.check(t)
                    if is_rug:
                        log.warning("   [Fallback] RUG detected %s: %s", t.symbol, rug_reason)
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
            # Each find_best call owns fresh asyncio primitives; reusing a
            # semaphore across asyncio.run() event loops can bind it to an
            # already-closed loop on later bot cycles.
            self._pending_mints = set()
            self._evaluated_mints = set()
            self._eval_semaphore = asyncio.Semaphore(self.cfg.max_parallel_evaluations)
            self._eval_tasks = set()
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
        self.listener = PumpFunListener(cfg, birdeye_api_key=birdeye_api_key)

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
    log.info(f"   Holders:     {t.holder_count} | Top-10: {t.top10_pct:.1f}%")
    log.info(f"   Initial buy: {t.initial_buy_sol:.2f} SOL" if t.initial_buy_sol is not None else "   Initial buy: unknown")
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
