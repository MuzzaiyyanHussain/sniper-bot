"""Regression tests for the Pump.fun scanner and dry-run safety helpers."""
import asyncio
import json
import math
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot import (
    CompoundingBot,
    Config,
    Journal,
    PriceOracle,
    StrategyCalc,
    is_sustained_crash,
    parse_bool_env,
)
from token_scanner import (
    PumpFunListener,
    RugDetector,
    ScannerConfig,
    TokenCandidate,
    parse_sol_amount,
)


def make_candidate(**overrides):
    values = dict(
        mint="Mint111111111111111111111111111111111111111",
        symbol="OKAY",
        name="Okay token",
        dex="pump.fun",
        pair_address="pair",
        price_usd=0.3,
        liquidity_usd=10_000.0,
        volume_5m=2_000.0,
        volume_1h=8_000.0,
        price_change_5m=10.0,
        price_change_1h=12.0,
        market_cap=120_000.0,
        txns_5m_buys=40,
        txns_5m_sells=15,
        age_minutes=1.0,
        holder_count=35,
        top10_pct=45.0,
        url="https://example.com/token",
        initial_buy_sol=None,
    )
    values.update(overrides)
    return TokenCandidate(**values)


class TestStrategyAndJournal(unittest.TestCase):
    def test_cycle_result_and_projection(self):
        cfg = Config()
        cfg.STARTING_AMOUNT_USDC = 10.0
        cfg.WIN_MULTIPLIER = 2.0
        cfg.REINVEST_PCT = 0.60
        cfg.SAVE_PCT = 0.40
        calc = StrategyCalc(cfg)

        result = calc.cycle_result(10.0, True)
        self.assertEqual(result["play_amount"], 10.0)
        self.assertEqual(result["winnings"], 20.0)
        self.assertEqual(result["next_play"], 12.0)
        self.assertEqual(result["saved"], 8.0)

        projection = calc.project(days=2)
        self.assertEqual(len(projection), 2)
        self.assertEqual(projection[0]["day"], 1)
        self.assertEqual(projection[1]["play_amount"], 12.0)

    def test_journal_loads_defaults_and_keeps_loss_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                journal = Journal()
                self.assertEqual(journal.data["consec_losses"], 0)
                self.assertIsNone(journal.data["play_amount_usdc"])
                journal.data["consec_losses"] = 2
                journal.save()
                reloaded = Journal()
                self.assertEqual(reloaded.data["consec_losses"], 2)
            finally:
                os.chdir(old_cwd)


class TestDryRunSafety(unittest.TestCase):
    def setUp(self):
        # Journal/CSV paths are relative; keep tests from writing the real ones.
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_boolean_environment_parser_accepts_explicit_values(self):
        for value in ("true", "TRUE", "1", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(parse_bool_env(value))
        for value in ("false", "FALSE", "0", "no", "off"):
            with self.subTest(value=value):
                self.assertFalse(parse_bool_env(value))

    def test_boolean_environment_parser_rejects_ambiguous_values(self):
        for value in ("", "maybe", "2", "enabled"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_bool_env(value)

    def test_jupiter_send_is_blocked_when_dry_run_is_enabled(self):
        from bot import JupiterClient

        cfg = Config()
        cfg.DRY_RUN = True
        with self.assertRaisesRegex(RuntimeError, "DRY_RUN"):
            JupiterClient(cfg).sign_and_send("not-a-transaction", None)

    def test_compounding_bot_allows_dry_run_without_wallet(self):
        cfg = Config()
        cfg.DRY_RUN = True
        cfg.WALLET_PRIVATE_KEY = ""
        bot = CompoundingBot(cfg)
        self.assertIsNone(bot.keypair)
        self.assertEqual(bot.pubkey, "DRY_RUN")

    def test_run_cycle_in_dry_run_uses_mocked_trade_execution(self):
        cfg = Config()
        cfg.DRY_RUN = True
        cfg.TOKEN_MODE = "scan"
        cfg.SCAN_MAX_WAIT_MIN = 1
        cfg.STARTING_AMOUNT_USDC = 4.0
        cfg.MIN_PLAY_AMOUNT = 1.0
        cfg.MAX_CONSEC_LOSSES = 2
        cfg.LOSS_PAUSE_SEC = 0

        bot = CompoundingBot(cfg)
        bot.check_balances = MagicMock()
        bot.scanner.find_best = MagicMock(return_value=make_candidate())
        bot.execute_buy = MagicMock(return_value={
            "buy_tx": "DRY_RUN_BUY",
            "usdc_spent": 4.0,
            "token_amount": 2.0,
            "token_decimals": 6,
            "token_symbol": "TEST",
            "entry_price": 2.0,
            "target_price": 4.0,
            "stop_price": 1.64,
            "token_mint": "Mint111111111111111111111111111111111111111",
            "dex_url": "https://example.com/token",
        })
        bot.monitor_price = MagicMock(return_value=("target", 4.0))
        bot.execute_sell = MagicMock(return_value=8.0)

        self.assertTrue(bot.run_cycle())
        bot.check_balances.assert_not_called()
        bot.execute_buy.assert_called_once()
        bot.execute_sell.assert_called_once()


class TestPumpPortalAmountParsing(unittest.TestCase):
    def test_uses_sol_amount_not_initial_buy_token_amount(self):
        self.assertEqual(
            parse_sol_amount({"solAmount": 1.25, "initialBuy": 100_000_000}),
            1.25,
        )

    def test_missing_sol_amount_stays_unknown(self):
        self.assertIsNone(parse_sol_amount({"initialBuy": 100_000_000}))

    def test_malformed_sol_amount_is_rejected(self):
        for value in ("not-a-number", {}, "NaN", "Infinity", -1, 1001):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_sol_amount({"solAmount": value})

    def test_zero_sol_amount_is_valid_but_not_missing(self):
        self.assertEqual(parse_sol_amount({"solAmount": 0}), 0.0)


class TestScannerFiltersAndMintMatching(unittest.TestCase):
    def test_actual_scanner_filter_rejects_unknown_holder_data(self):
        cfg = ScannerConfig()
        listener = PumpFunListener(cfg)
        unknown = make_candidate(holder_count=0, top10_pct=0.0)
        ok, reason = listener._passes_filters(unknown)
        self.assertFalse(ok)
        self.assertIn("holder", reason.lower())

    def test_actual_scanner_filter_rejects_too_old_or_oversized_tokens(self):
        cfg = ScannerConfig()
        listener = PumpFunListener(cfg)
        too_old = make_candidate(age_minutes=cfg.max_age_minutes + 1)
        ok, reason = listener._passes_filters(too_old)
        self.assertFalse(ok)
        self.assertIn("too old", reason)

        too_liquid = make_candidate(liquidity_usd=cfg.max_liquidity_usd + 1)
        ok, reason = listener._passes_filters(too_liquid)
        self.assertFalse(ok)
        self.assertIn("liquidity too high", reason)

    def test_unknown_initial_buy_does_not_receive_zero_buy_bonus_or_reject(self):
        cfg = ScannerConfig()
        listener = PumpFunListener(cfg)
        unknown = make_candidate(initial_buy_sol=None)
        known_high = make_candidate(initial_buy_sol=25.0)
        self.assertEqual(listener.scorer.score(unknown), listener.scorer.score(known_high))
        ok, reason = listener._passes_filters(unknown)
        self.assertTrue(ok, reason)

    def test_parse_pair_rejects_mismatched_mint(self):
        listener = PumpFunListener(ScannerConfig())
        pair = {
            "chainId": "solana",
            "baseToken": {
                "address": "WrongMint",
                "symbol": "WRONG",
                "name": "Wrong",
            },
            "pairCreatedAt": 1_700_000_000_000,
            "liquidity": {"usd": 10_000},
        }
        self.assertIsNone(
            listener.dex.parse_pair(pair, 1.0, expected_mint="ExpectedMint")
        )

    def test_parse_pair_missing_liquidity_stays_ineligible(self):
        listener = PumpFunListener(ScannerConfig())
        pair = {
            "chainId": "solana",
            "baseToken": {
                "address": "MintX",
                "symbol": "X",
                "name": "Token X",
            },
            "pairCreatedAt": time.time() * 1000,
            "liquidity": None,
            "volume": {"m5": 2000},
            "priceUsd": "0.001",
            "marketCap": 10000,
            "txns": {"m5": {"buys": 20, "sells": 5}},
        }

        candidate = listener.dex.parse_pair(pair, expected_mint="MintX")
        self.assertIsNone(candidate, "parse_pair should return None for missing liquidity (fail-closed)")


    def test_missing_liquidity_pair_is_retried_until_data_is_available(self):
        listener = PumpFunListener(ScannerConfig(pair_wait_sec=2))
        missing = {
            "chainId": "solana",
            "baseToken": {"address": "MintX"},
            "liquidity": None,
        }
        available = {
            **missing,
            "liquidity": {"usd": 10_000},
        }
        with patch.object(
            listener.dex, "get_pairs_for_mint",
            side_effect=[[missing], [available]],
        ), patch("token_scanner.time.sleep", return_value=None), patch.object(
            listener.dex, "parse_pair", return_value=None
        ) as parse_pair:
            listener._evaluate_mint("MintX", "X", "X", None)
            self.assertEqual(parse_pair.call_args.args[0], available)

    def test_repeated_missing_liquidity_stops_at_deadline(self):
        listener = PumpFunListener(ScannerConfig(pair_wait_sec=1))
        pair = {
            "chainId": "solana",
            "baseToken": {"address": "MintX"},
            "liquidity": None,
        }
        with patch.object(
            listener.dex, "get_pairs_for_mint", return_value=[pair]
        ) as get_pairs, patch("token_scanner.time.sleep", return_value=None), patch(
            "token_scanner.time.monotonic",
            side_effect=[0.0, 0.0, 0.5, 0.5, 1.1, 1.1],
        ), patch.object(
            listener.dex, "parse_pair", return_value=None
        ) as parse_pair:
            self.assertIsNone(
                listener._evaluate_mint("MintX", "X", "X", None)
            )
        self.assertEqual(get_pairs.call_count, 2)
        parse_pair.assert_called_once_with(
            pair, None, expected_mint="MintX"
        )

    def test_dex_request_timeout_is_capped_by_remaining_deadline(self):
        listener = PumpFunListener(ScannerConfig(pair_wait_sec=2))
        pair = {
            "chainId": "solana",
            "baseToken": {"address": "MintX"},
            "liquidity": None,
        }
        response = MagicMock()
        response.json.return_value = {"pairs": [pair]}
        with patch(
            "token_scanner.time.monotonic",
            side_effect=[100.0, 100.0, 102.0, 102.0],
        ), patch.object(
            listener.dex.session, "get", return_value=response
        ) as get, patch.object(
            listener.dex, "parse_pair", return_value=None
        ) as parse_pair:
            self.assertIsNone(
                listener._evaluate_mint("MintX", "X", "X", None)
            )

        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.kwargs["timeout"], 2.0)
        parse_pair.assert_called_once_with(
            pair, None, expected_mint="MintX"
        )

    def test_missing_liquidity_remains_ineligible_after_deadline(self):
        listener = PumpFunListener(ScannerConfig())
        pair = {
            "chainId": "solana",
            "baseToken": {"address": "MintX", "symbol": "X", "name": "X"},
            "pairCreatedAt": time.time() * 1000,
            "liquidity": None,
            "volume": {"m5": 2000},
            "priceUsd": "0.001",
            "marketCap": 10000,
            "txns": {"m5": {"buys": 20, "sells": 5}},
        }
        # After the deadline, missing liquidity should still be rejected (fail-closed)
        candidate = listener.dex.parse_pair(pair, expected_mint="MintX")
        self.assertIsNone(candidate, "Missing liquidity should return None regardless of deadline")


    def test_present_invalid_liquidity_is_not_retried(self):
        listener = PumpFunListener(ScannerConfig(pair_wait_sec=1))
        pair = {
            "chainId": "solana",
            "baseToken": {"address": "MintX"},
            "liquidity": {"usd": float("nan")},
        }
        with patch.object(
            listener.dex, "get_pairs_for_mint", return_value=[pair]
        ) as get_pairs, patch("token_scanner.time.sleep") as sleep:
            self.assertIsNone(
                listener._evaluate_mint("MintX", "X", "X", None)
            )
        get_pairs.assert_called_once()
        sleep.assert_not_called()

    def test_invalid_liquidity_is_rejected(self):
        listener = PumpFunListener(ScannerConfig())
        pair = {
            "chainId": "solana",
            "baseToken": {"address": "MintX"},
            "liquidity": {"usd": float("nan")},
        }

        self.assertIsNone(
            listener.dex.parse_pair(pair, expected_mint="MintX")
        )

    def test_invalid_price_is_rejected(self):
        listener = PumpFunListener(ScannerConfig())
        pair = {
            "chainId": "solana",
            "baseToken": {"address": "MintX"},
            "pairCreatedAt": time.time() * 1000,
            "liquidity": {"usd": 10_000},
            "priceUsd": "NaN",
        }

        self.assertIsNone(
            listener.dex.parse_pair(pair, expected_mint="MintX")
        )

    def test_invalid_volume_is_rejected(self):
        listener = PumpFunListener(ScannerConfig())
        pair = {
            "chainId": "solana",
            "baseToken": {"address": "MintX"},
            "pairCreatedAt": time.time() * 1000,
            "liquidity": {"usd": 10_000},
            "volume": {"m5": "Infinity"},
            "priceUsd": "0.001",
        }

        self.assertIsNone(
            listener.dex.parse_pair(pair, expected_mint="MintX")
        )

    def test_missing_pair_retry_can_recover(self):
        listener = PumpFunListener(ScannerConfig())
        pair = {
            "chainId": "solana",
            "baseToken": {"address": "MintX"},
            "liquidity": {"usd": 1},
        }
        with patch.object(
            listener.dex, "get_pairs_for_mint", side_effect=[[], [pair]]
        ), patch("token_scanner.time.sleep", return_value=None), patch.object(
            listener.dex, "parse_pair", return_value=None
        ) as parse_pair:
            self.assertIsNone(
                listener._evaluate_mint("MintX", "X", "X", None)
            )
            parse_pair.assert_called_once_with(
                pair, None, expected_mint="MintX"
            )


class TestPriceOracleValidation(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.oracle = PriceOracle(self.cfg)
        self.oracle.dex = MagicMock()

    def pair(self, price="0.001", liquidity=10_000):
        return {
            "chainId": "solana",
            "baseToken": {"address": "MintX"},
            "priceUsd": price,
            "liquidity": {"usd": liquidity},
        }

    def test_valid_dex_pair_returns_price_and_liquidity(self):
        self.oracle.dex.get_pairs_for_mint.return_value = [
            self.pair(price="0.001", liquidity=5000)
        ]
        self.assertEqual(
            self.oracle.get_price_and_liquidity("MintX"),
            (0.001, 5000.0),
        )

    def test_non_finite_dex_prices_are_rejected(self):
        for price in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(price=price):
                self.oracle.dex.get_pairs_for_mint.return_value = [
                    self.pair(price=price)
                ]
                self.assertEqual(
                    self.oracle.get_price_and_liquidity("MintX"),
                    (0.0, 10_000.0),
                )

    def test_non_finite_or_negative_liquidity_is_rejected(self):
        for liquidity in (float("nan"), float("inf"), -1):
            with self.subTest(liquidity=liquidity):
                self.oracle.dex.get_pairs_for_mint.return_value = [
                    self.pair(liquidity=liquidity)
                ]
                self.assertEqual(
                    self.oracle.get_price_and_liquidity("MintX"),
                    (0.0, None),
                )

    def test_invalid_dex_liquidity_does_not_use_jupiter_fallback(self):
        self.oracle.dex.get_pairs_for_mint.return_value = [
            self.pair(liquidity=float("nan"))
        ]
        with patch("bot.JupiterClient") as jupiter:
            self.assertEqual(
                self.oracle.get_price_and_liquidity("MintX"),
                (0.0, None),
            )
            jupiter.assert_not_called()

    def test_zero_jupiter_output_is_rejected(self):
        self.oracle.dex.get_pairs_for_mint.return_value = []
        with patch("bot.JupiterClient") as jupiter:
            jupiter.return_value.get_quote.return_value = {"outAmount": "0"}
            self.assertEqual(
                self.oracle.get_price_and_liquidity("MintX"),
                (0.0, None),
            )

    def test_valid_jupiter_fallback_keeps_liquidity_unknown(self):
        self.oracle.dex.get_pairs_for_mint.return_value = []
        with patch("bot.JupiterClient") as jupiter:
            jupiter.return_value.get_quote.return_value = {
                "outAmount": "1000000000"
            }
            self.oracle.get_token_decimals = MagicMock(return_value=6)
            self.assertEqual(
                self.oracle.get_price_and_liquidity("MintX"),
                (0.001, None),
            )


class TestBirdeyeRateLimit(unittest.TestCase):
    def test_429_sets_cooldown_and_skips_requests(self):
        client = PumpFunListener(ScannerConfig()).holders
        client.api_key = "test-secret-key"
        response = MagicMock(status_code=429, headers={"Retry-After": "60"})
        response.raise_for_status.side_effect = AssertionError("must not raise")
        with patch.object(client.session, "get", return_value=response) as get:
            with patch(
                "token_scanner.time.monotonic",
                side_effect=[100.0, 104.0, 120.0],
            ), self.assertLogs("token_scanner", level="WARNING") as captured:
                self.assertIsNone(client.get_metrics("MintX"))
                self.assertIsNone(client.get_metrics("MintY"))
        get.assert_called_once()
        self.assertEqual(client._rate_limit_until, 164.0)
        self.assertIn("Retry-After='60'", captured.output[0])
        self.assertNotIn(client.api_key, captured.output[0])

    def test_invalid_retry_after_keeps_default_cooldown(self):
        client = PumpFunListener(ScannerConfig()).holders
        client.api_key = "test-key"
        response = MagicMock(status_code=429, headers={"Retry-After": "invalid"})
        with patch.object(client.session, "get", return_value=response), patch(
            "token_scanner.time.monotonic",
            side_effect=[100.0, 104.0],
        ):
            self.assertIsNone(client.get_metrics("MintX"))
        self.assertEqual(client._rate_limit_until, 134.0)


class TestWebSocketFailover(unittest.TestCase):
    def test_duplicate_mint_is_evaluated_once_per_scan(self):
        class Socket:
            def __init__(self, stop_event):
                self.stop_event = stop_event
                self.messages = [
                    json.dumps({
                        "txType": "create",
                        "mint": "MintX",
                        "name": "Token X",
                        "symbol": "X",
                    }),
                    json.dumps({
                        "txType": "create",
                        "mint": "MintX",
                        "name": "Token X",
                        "symbol": "X",
                    }),
                ]

            async def send(self, message):
                pass

            async def recv(self):
                if self.messages:
                    message = self.messages.pop(0)
                    if not self.messages:
                        await asyncio.sleep(0.05)
                    return message
                self.stop_event.set()
                raise RuntimeError("test disconnect")

        class Context:
            def __init__(self, stop_event):
                self.socket = Socket(stop_event)

            async def __aenter__(self):
                return self.socket

            async def __aexit__(self, exc_type, exc, tb):
                return False

        async def run_test():
            listener = PumpFunListener(ScannerConfig())
            stop_event = asyncio.Event()
            result_queue = asyncio.Queue()
            with patch(
                "token_scanner.websockets.connect",
                return_value=Context(stop_event),
            ), patch.object(
                listener, "_evaluate_mint", return_value=None
            ) as evaluate:
                await listener._listen_ws("endpoint", result_queue, stop_event)
            evaluate.assert_called_once_with("MintX", "Token X", "X", None)

        asyncio.run(run_test())

    def test_primary_failure_uses_fallback_and_subscribes(self):
        class FailingContext:
            async def __aenter__(self):
                raise RuntimeError("HTTP 502")

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FallbackSocket:
            def __init__(self, stop_event, sent):
                self.stop_event = stop_event
                self.sent = sent

            async def send(self, message):
                self.sent.append(json.loads(message))

            async def recv(self):
                self.stop_event.set()
                raise RuntimeError("test disconnect")

        class SuccessfulContext:
            def __init__(self, stop_event, sent):
                self.socket = FallbackSocket(stop_event, sent)

            async def __aenter__(self):
                return self.socket

            async def __aexit__(self, exc_type, exc, tb):
                return False

        async def run_test():
            listener = PumpFunListener(
                ScannerConfig(ws_endpoints=["primary", "fallback"])
            )
            stop_event = asyncio.Event()
            result_queue = asyncio.Queue()
            sent = []
            contexts = [
                FailingContext(),
                SuccessfulContext(stop_event, sent),
            ]
            with patch(
                "token_scanner.websockets.connect",
                side_effect=contexts,
            ) as connect, patch("token_scanner.asyncio.sleep", return_value=None):
                await listener._listen_with_fallback(result_queue, stop_event)
            self.assertEqual(
                [call.args[0] for call in connect.call_args_list],
                ["primary", "fallback"],
            )
            self.assertEqual(sent, [{"method": "subscribeNewToken"}])

        asyncio.run(run_test())

    def test_fallback_reconsiders_missing_liquidity_once(self):
        async def run_test():
            listener = PumpFunListener(ScannerConfig(scan_interval_sec=0))
            stop_event = asyncio.Event()
            result_queue = asyncio.Queue()
            missing = {
                "chainId": "solana",
                "baseToken": {"address": "MintX"},
                "liquidity": None,
            }
            available = {
                **missing,
                "liquidity": {"usd": 10_000},
            }
            responses = iter([[missing], [available]])
            listener.dex.get_pairs_for_mint = MagicMock(
                side_effect=lambda mint: next(responses)
            )
            candidate = make_candidate(mint="MintX")
            listener.dex.parse_pair = MagicMock(
                return_value=candidate
            )
            listener._passes_filters = MagicMock(return_value=(True, ""))
            listener.rug.check = MagicMock(return_value=(False, ""))
            listener.scorer.score = MagicMock(return_value=10.0)

            profile_response = MagicMock()
            profile_response.json.return_value = [
                {"tokenAddress": "MintX", "chainId": "solana"}
            ]
            sleep_calls = 0

            async def stop_after_second_poll(_interval):
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 2:
                    stop_event.set()

            with patch(
                "token_scanner.requests.get", return_value=profile_response
            ), patch(
                "token_scanner.asyncio.sleep", side_effect=stop_after_second_poll
            ):
                await listener._poll_fallback(result_queue, stop_event)
            listener.dex.get_pairs_for_mint.assert_called()
            self.assertEqual(listener.dex.get_pairs_for_mint.call_count, 2)
            self.assertIs(
                listener.dex.parse_pair.call_args_list[0].args[0], available
            )
            self.assertEqual(result_queue.qsize(), 1)

        asyncio.run(run_test())


class _PricesExhausted(Exception):
    """Raised when the scripted price feed runs out: monitor_price had not exited."""

    def __init__(self, polls):
        super().__init__(f"no exit after {polls} polls")
        self.polls = polls


class TestStopLossMonitor(unittest.TestCase):
    """Deterministic stop-loss scenarios: fake clock, fake oracle, no network."""

    ENTRY = 1.0
    STOP = 0.82  # 18% below entry (default STOP_LOSS_MULT)

    def run_monitor(self, prices, step=8.0, stop=STOP, entry=ENTRY, max_no_price=None):
        """Return (result, polls). Raises _PricesExhausted if no exit occurred."""
        clock = [1_000.0]
        polled = []
        feed = iter(prices)

        def get_price_and_liquidity(_mint):
            try:
                price = next(feed)
            except StopIteration:
                raise _PricesExhausted(len(polled))
            polled.append(price)
            return price, 10_000.0

        def fake_sleep(seconds):
            clock[0] += step

        cfg = Config()
        if max_no_price is not None:
            cfg.MAX_NO_PRICE_SEC = max_no_price
        fake_self = SimpleNamespace(
            oracle=SimpleNamespace(get_price_and_liquidity=get_price_and_liquidity),
            cfg=cfg,
        )
        position = {
            "target_price": 2.0,
            "stop_price": stop,
            "token_mint": "StopLossMint1111",
            "token_symbol": "SL",
            "entry_price": entry,
        }
        with patch("bot.time.time", lambda: clock[0]), \
                patch("bot.time.sleep", fake_sleep):
            result = CompoundingBot.monitor_price(fake_self, position)
        return result, len(polled)

    def assertStillOpen(self, prices, polls, **kwargs):
        with self.assertRaises(_PricesExhausted) as ctx:
            self.run_monitor(prices, **kwargs)
        self.assertEqual(ctx.exception.polls, polls)

    # 1. Sustained crash exits immediately.
    def test_cascading_crash_exits_on_first_breach(self):
        result, polls = self.run_monitor([1.0, 0.90, 0.70])
        self.assertEqual(result, ("stop_loss", 0.70))
        self.assertEqual(polls, 3)

    # 2. A lone anomalous tick is not treated as sustained.
    def test_single_anomalous_tick_waits_and_recovery_reaches_target(self):
        self.assertStillOpen([1.0, 1.0, 0.80], polls=3)
        result, polls = self.run_monitor([1.0, 1.0, 0.80, 1.0, 2.0])
        self.assertEqual(result, ("target", 2.0))
        self.assertEqual(polls, 5)

    # 3. Repeated breached prices: exactly one extra confirmation tick.
    def test_repeated_breach_from_flat_baseline_exits_on_second_tick(self):
        result, polls = self.run_monitor([1.0, 1.0, 0.80, 0.80])
        self.assertEqual(result, ("stop_loss", 0.80))
        self.assertEqual(polls, 4)

    def test_gradual_decline_waits_one_tick_then_exits(self):
        decline = [0.97, 0.93, 0.90, 0.88, 0.81]
        self.assertStillOpen(decline, polls=5)
        result, polls = self.run_monitor(decline + [0.81])
        self.assertEqual(result, ("stop_loss", 0.81))
        self.assertEqual(polls, 6)

    # 4. Recovery between breaches restarts the confirmation.
    def test_recovery_between_breaches_resets_confirmation(self):
        prefix = [0.97, 0.93, 0.90, 0.88, 0.81, 0.90, 0.81]
        self.assertStillOpen(prefix, polls=7)
        result, polls = self.run_monitor(prefix + [0.81])
        self.assertEqual(result, ("stop_loss", 0.81))
        self.assertEqual(polls, 8)
        result, polls = self.run_monitor(prefix + [2.0])
        self.assertEqual(result, ("target", 2.0))

    # 5. Warmup: breaches in the first 6s are ignored and never count as a confirmation.
    def test_breach_during_warmup_is_ignored(self):
        self.assertStillOpen([0.5], polls=1)

    def test_crash_persisting_past_warmup_exits_on_first_trusted_tick(self):
        result, polls = self.run_monitor([0.5, 0.5])
        self.assertEqual(result, ("stop_loss", 0.5))
        self.assertEqual(polls, 2)

    def test_warmup_breaches_do_not_count_as_confirmation(self):
        # step=1s: ticks at t=0..5 are warmup, t=6 is the first trusted tick.
        # The baseline is already flat at 0.5, so it is not a "sustained" drop.
        self.assertStillOpen([0.5] * 7, polls=7, step=1.0)
        result, polls = self.run_monitor([0.5] * 8, step=1.0)
        self.assertEqual(result, ("stop_loss", 0.5))
        self.assertEqual(polls, 8)

    # Take-profit is unchanged.
    def test_target_still_exits_immediately(self):
        result, polls = self.run_monitor([1.0, 2.0])
        self.assertEqual(result, ("target", 2.0))
        self.assertEqual(polls, 2)

    # Missing prices: bounded wait, then an exit is attempted (not an infinite loop).
    def test_missing_prices_resolve_to_no_price_exit_at_limit(self):
        # Ticks are 8s apart; the 16th consecutive missing price is 120s in.
        self.assertStillOpen([1.0] + [0.0] * 14, polls=15, max_no_price=120)
        result, polls = self.run_monitor([1.0] + [0.0] * 40, max_no_price=120)
        self.assertEqual(result, ("no_price", 1.0))
        self.assertEqual(polls, 17)

    def test_pending_breach_is_resolved_when_prices_go_missing(self):
        pending = [0.97, 0.93, 0.90, 0.88, 0.81]
        result, _ = self.run_monitor(pending + [0.0] * 40, max_no_price=120)
        self.assertEqual(result, ("no_price", 0.81))

    def test_valid_price_resets_missing_price_timer(self):
        prices = [0.0] * 10 + [1.0] + [0.0] * 10 + [2.0]
        result, _ = self.run_monitor(prices, max_no_price=120)
        self.assertEqual(result, ("target", 2.0))

    def test_no_price_timeout_can_be_disabled(self):
        self.assertStillOpen([0.0] * 60, polls=60, max_no_price=0)

    def test_no_price_exit_uses_wide_slippage_ladder_like_stop_loss(self):
        def attempts(reason):
            jup = MagicMock()
            jup.get_quote.side_effect = RuntimeError("no route")
            cfg = Config()
            cfg.DRY_RUN = True
            fake_self = SimpleNamespace(cfg=cfg, jup=jup, j=MagicMock())
            position = {
                "token_amount": 1.0, "token_mint": "M" * 10,
                "token_symbol": "T", "usdc_spent": 2.0,
            }
            with patch("bot.time.sleep"):
                self.assertIsNone(CompoundingBot.execute_sell(fake_self, position, reason))
            return [c.kwargs["slippage_bps"] for c in jup.get_quote.call_args_list]

        self.assertEqual(attempts("no_price"), attempts("stop_loss"))
        self.assertEqual(len(attempts("no_price")), 5)
        self.assertEqual(len(attempts("target")), 3)


class TestSustainedCrashBoundary(unittest.TestCase):
    """Exact thresholds for is_sustained_crash (15% drop, 5% prior decline)."""

    # average = 100.0 exactly, prior tick 80 is far below the 105 baseline
    FALLING = [105.0, 105.0, 105.0, 105.0, 80.0]

    def test_exactly_15_percent_below_average_is_sustained(self):
        self.assertEqual(sum(self.FALLING) / 5, 100.0)
        self.assertTrue(is_sustained_crash(self.FALLING, 85.0))

    def test_just_under_15_percent_is_not_sustained(self):
        self.assertFalse(is_sustained_crash(self.FALLING, 85.01))

    def test_just_over_15_percent_is_sustained(self):
        self.assertTrue(is_sustained_crash(self.FALLING, 84.99))

    def test_prior_decline_exactly_5_percent_counts_as_falling(self):
        # baseline 100, prior 95 (exactly 5% down), average 99 -> 15% threshold 84.15
        history = [100.0, 100.0, 100.0, 100.0, 95.0]
        self.assertTrue(is_sustained_crash(history, 84.0))

    def test_prior_decline_under_5_percent_is_not_falling(self):
        history = [100.0, 100.0, 100.0, 100.0, 95.01]
        self.assertFalse(is_sustained_crash(history, 70.0))

    def test_flat_history_never_sustained_even_on_huge_drop(self):
        self.assertFalse(is_sustained_crash([1.0, 1.0, 1.0], 0.01))

    def test_needs_at_least_two_readings(self):
        self.assertFalse(is_sustained_crash([1.0], 0.01))
        self.assertFalse(is_sustained_crash([], 0.01))


if __name__ == "__main__":
    unittest.main()


