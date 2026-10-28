"""Regression tests for the Pump.fun scanner and dry-run safety helpers."""
import asyncio
import base64
import json
import logging
import math
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import base58
import requests
from solders.keypair import Keypair

from chain import PumpPortalLocalClient, RpcClient, RpcError, parse_bonding_curve
from bot import (
    CompoundingBot,
    Config,
    Journal,
    JournalCorruptError,
    LIVE_CONFIRM_PHRASE,
    LIVE_MAX_DAILY_LOSS_CEILING_USDC,
    LIVE_MAX_POSITION_CEILING_USDC,
    LiveTradingConfigError,
    PriceOracle,
    StrategyCalc,
    is_sustained_crash,
    parse_bool_env,
    validate_live_config,
)
from token_scanner import (
    CurveReserveMetrics,
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
        market_cap=8_000.0,
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

    def _loss_streak_bot(self, dry_run):
        cfg = Config()
        cfg.DRY_RUN = dry_run
        cfg.MAX_CONSEC_LOSSES = 2
        cfg.LOSS_PAUSE_SEC = 300
        cfg.STARTING_AMOUNT_USDC = 4.0
        cfg.MIN_PLAY_AMOUNT = 1.0
        # Pin the live-gate values so the test never depends on a local .env.
        cfg.MAX_DAILY_LOSS_USDC = "4"
        cfg.MAX_POSITION_USDC = "4"
        cfg.KILL_SWITCH_FILE = "NO_SUCH_KILL_SWITCH_FILE"
        bot = CompoundingBot.__new__(CompoundingBot)
        bot.cfg = cfg
        bot.j = SimpleNamespace(
            data={"play_amount_usdc": 4.0, "consec_losses": 3, "open_position": None,
                  "cycle": 0, "pending_tx": None},
            save=MagicMock(),
            realized_pnl_today=MagicMock(side_effect=RuntimeError("stop here")),
        )
        bot.check_balances = MagicMock(side_effect=RuntimeError("stop here"))
        bot.scanner = SimpleNamespace(find_best=MagicMock(side_effect=RuntimeError("stop here")))
        return bot

    def test_loss_streak_pause_is_skipped_in_dry_run(self):
        bot = self._loss_streak_bot(dry_run=True)
        with patch("bot.time.sleep") as sleep, self.assertRaises(RuntimeError):
            bot.run_cycle()
        sleep.assert_not_called()

    def test_loss_streak_pause_still_applies_in_live_mode(self):
        bot = self._loss_streak_bot(dry_run=False)
        with patch("bot.time.sleep") as sleep, self.assertRaises(RuntimeError):
            bot.run_cycle()
        sleep.assert_any_call(300)
        self.assertEqual(bot.j.data["consec_losses"], 0)

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
    def test_market_cap_filter_caps_candidates_at_ten_thousand(self):
        cfg = ScannerConfig()
        listener = PumpFunListener(cfg)

        self.assertEqual(cfg.min_market_cap_usd, 1_000)
        self.assertEqual(cfg.max_market_cap_usd, 10_000)

        at_cap = make_candidate(market_cap=10_000)
        ok, reason = listener._passes_filters(at_cap)
        self.assertTrue(ok, reason)

        above_cap = make_candidate(market_cap=10_001)
        ok, reason = listener._passes_filters(above_cap)
        self.assertFalse(ok)
        self.assertIn("mcap too high", reason)

    def test_creator_buy_cap_is_five_sol_and_applies_to_filters(self):
        cfg = ScannerConfig()
        listener = PumpFunListener(cfg)
        self.assertEqual(cfg.max_initial_buy_sol, 5.0)

        ok, reason = listener._passes_filters(make_candidate(initial_buy_sol=5.0))
        self.assertTrue(ok, reason)
        ok, reason = listener._passes_filters(make_candidate(initial_buy_sol=5.01))
        self.assertFalse(ok)
        self.assertIn("dev bought too much", reason)
        ok, reason = listener._passes_filters(make_candidate(initial_buy_sol=None))
        self.assertTrue(ok, reason)

    def test_holder_thresholds_are_nominal_but_data_is_still_required(self):
        cfg = ScannerConfig()
        listener = PumpFunListener(cfg)
        self.assertEqual(cfg.min_holder_count, 10)
        self.assertEqual(cfg.max_top10_holder_pct, 90.0)

        ok, reason = listener._passes_filters(
            make_candidate(holder_count=10, top10_pct=90.0))
        self.assertTrue(ok, reason)

        ok, reason = listener._passes_filters(
            make_candidate(holder_count=9, top10_pct=50.0))
        self.assertFalse(ok)
        self.assertIn("too few holders", reason)

        ok, reason = listener._passes_filters(
            make_candidate(holder_count=50, top10_pct=90.1))
        self.assertFalse(ok)
        self.assertIn("concentrated", reason)

        ok, reason = listener._passes_filters(
            make_candidate(holder_count=0, top10_pct=0.0))
        self.assertFalse(ok)

    def test_max_age_filter_accepts_five_minutes_and_rejects_older_tokens(self):
        cfg = ScannerConfig()
        listener = PumpFunListener(cfg)

        self.assertEqual(cfg.max_age_minutes, 5)
        for age in (4.99, 5.0):
            with self.subTest(age=age):
                ok, reason = listener._passes_filters(make_candidate(age_minutes=age))
                self.assertTrue(ok, reason)

        ok, reason = listener._passes_filters(make_candidate(age_minutes=5.01))
        self.assertFalse(ok)
        self.assertIn("too old", reason)

    def test_reduced_activity_thresholds_are_applied(self):
        cfg = ScannerConfig()
        listener = PumpFunListener(cfg)
        self.assertEqual(cfg.min_volume_5m_usd, 50)
        self.assertEqual(cfg.min_txns_5m, 2)
        self.assertEqual(cfg.min_buys_5m, 2)
        eligible = make_candidate(
            volume_5m=50, txns_5m_buys=2, txns_5m_sells=0
        )
        ok, reason = listener._passes_filters(eligible)
        self.assertTrue(ok, reason)

        too_little_volume = make_candidate(volume_5m=49)
        ok, reason = listener._passes_filters(too_little_volume)
        self.assertFalse(ok)
        self.assertIn("low volume", reason)

        too_few_transactions = make_candidate(
            volume_5m=50, txns_5m_buys=1, txns_5m_sells=0
        )
        ok, reason = listener._passes_filters(too_few_transactions)
        self.assertFalse(ok)
        self.assertIn("low activity", reason)

        too_few_buys = make_candidate(
            volume_5m=50, txns_5m_buys=1, txns_5m_sells=1
        )
        ok, reason = listener._passes_filters(too_few_buys)
        self.assertFalse(ok)
        self.assertIn("few buys", reason)

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

    def test_parse_pair_uses_verified_curve_metrics_when_dex_liquidity_is_absent(self):
        listener = PumpFunListener(ScannerConfig())
        pair = {
            "chainId": "solana",
            "dexId": "pumpfun",
            "pairAddress": "PairX",
            "baseToken": {"address": "MintX", "symbol": "X", "name": "X"},
            "pairCreatedAt": time.time() * 1000,
            "liquidity": None,
            "priceUsd": "unavailable",
            "marketCap": 10_000,
            "volume": {"m5": 2_000, "h1": 5_000},
            "priceChange": {"m5": 10, "h1": 20},
            "txns": {"m5": {"buys": 20, "sells": 5}},
        }
        metrics = CurveReserveMetrics(
            liquidity_usd=2_500,
            price_usd=0.00001,
            market_cap_usd=10_000,
        )

        candidate = listener.dex.parse_pair(
            pair, expected_mint="MintX", curve_metrics=metrics
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.liquidity_usd, 2_500)
        self.assertEqual(candidate.price_usd, 0.00001)
        self.assertEqual(candidate.liquidity_source, "Pump.fun real quote reserves")

    def test_evaluate_mint_uses_curve_fallback_once_after_dex_wait(self):
        listener = PumpFunListener(ScannerConfig(pair_wait_sec=1))
        pair = {
            "chainId": "solana",
            "dexId": "pumpfun",
            "pairAddress": "PairX",
            "baseToken": {"address": "MintX", "symbol": "X", "name": "X"},
            "pairCreatedAt": time.time() * 1000,
            "liquidity": None,
            "priceUsd": "unavailable",
            "marketCap": 10_000,
            "volume": {"m5": 2_000, "h1": 5_000},
            "priceChange": {"m5": 10, "h1": 20},
            "txns": {"m5": {"buys": 20, "sells": 5}},
        }
        metrics = CurveReserveMetrics(2_500, 0.00001, 10_000)
        listener.curve_metrics_provider = MagicMock(return_value=metrics)
        listener.dex.get_pairs_for_mint = MagicMock(return_value=[pair])
        listener.holders.get_metrics = MagicMock(return_value=(40, 40.0))

        with patch(
            "token_scanner.time.monotonic",
            side_effect=[10.0, 10.0, 10.0, 11.1],
        ), patch("token_scanner.time.sleep"):
            candidate = listener._evaluate_mint("MintX", "X", "X", None)

        listener.curve_metrics_provider.assert_called_once_with("MintX")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.liquidity_source, "Pump.fun real quote reserves")


    def _holder_recheck_pair(self, **overrides):
        pair = {
            "chainId": "solana",
            "dexId": "pumpfun",
            "pairAddress": "PairX",
            "baseToken": {"address": "MintX", "symbol": "X", "name": "X"},
            "pairCreatedAt": time.time() * 1000,
            "liquidity": {"usd": 3_000},
            "priceUsd": "0.00001",
            "marketCap": 10_000,
            "volume": {"m5": 2_000, "h1": 5_000},
            "priceChange": {"m5": 10, "h1": 20},
            "txns": {"m5": {"buys": 20, "sells": 5}},
        }
        pair.update(overrides)
        return pair

    def test_immature_holder_data_is_rechecked_until_it_passes(self):
        listener = PumpFunListener(ScannerConfig(pair_wait_sec=1))
        listener.dex.get_pairs_for_mint = MagicMock(
            return_value=[self._holder_recheck_pair()]
        )
        listener.holders.get_metrics = MagicMock(
            side_effect=[(1, 100.0), (15, 40.0)]
        )

        with patch("token_scanner.time.sleep") as sleep:
            candidate = listener._evaluate_mint("MintX", "X", "X", None)

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.holder_count, 15)
        self.assertEqual(listener.holders.get_metrics.call_count, 2)
        sleep.assert_called_once_with(30.0)

    def test_holder_recheck_is_bounded_and_stays_fail_closed(self):
        listener = PumpFunListener(
            ScannerConfig(pair_wait_sec=1, holder_recheck_attempts=2)
        )
        listener.dex.get_pairs_for_mint = MagicMock(
            return_value=[self._holder_recheck_pair()]
        )
        listener.holders.get_metrics = MagicMock(return_value=None)

        with patch("token_scanner.time.sleep"):
            candidate = listener._evaluate_mint("MintX", "X", "X", None)

        self.assertIsNone(candidate)
        self.assertEqual(listener.holders.get_metrics.call_count, 3)

    def test_find_best_does_not_wait_for_in_flight_evaluations(self):
        listener = PumpFunListener(ScannerConfig())

        async def fake_listen(result_queue, stop_event):
            listener._eval_executor.submit(time.sleep, 2.0)  # slow in-flight work
            await result_queue.put(make_candidate())
            await asyncio.sleep(30)

        started = time.monotonic()
        with patch.object(listener, "_listen_with_fallback", fake_listen):
            candidate = listener.find_best(max_wait_minutes=1)
        elapsed = time.monotonic() - started

        self.assertIsNotNone(candidate)
        self.assertLess(elapsed, 1.0)
        self.assertTrue(listener._stop_requested.is_set())

    def test_evaluation_thread_exits_once_scan_has_stopped(self):
        listener = PumpFunListener(ScannerConfig(pair_wait_sec=5))
        listener.dex.get_pairs_for_mint = MagicMock(return_value=[])
        listener._stop_requested.set()
        self.assertIsNone(listener._evaluate_mint("MintX", "X", "X", None))
        listener.dex.get_pairs_for_mint.assert_not_called()

    def test_holder_recheck_stops_after_sleep_if_scan_ended(self):
        listener = PumpFunListener(ScannerConfig(pair_wait_sec=1))
        listener.dex.get_pairs_for_mint = MagicMock(
            return_value=[self._holder_recheck_pair()]
        )
        listener.holders.get_metrics = MagicMock(return_value=(1, 100.0))

        def stop_during_sleep(_seconds):
            listener._stop_requested.set()

        with patch("token_scanner.time.sleep", stop_during_sleep):
            self.assertIsNone(listener._evaluate_mint("MintX", "X", "X", None))
        self.assertEqual(listener.holders.get_metrics.call_count, 1)

    def test_holder_recheck_skipped_when_other_filter_fails(self):
        listener = PumpFunListener(ScannerConfig(pair_wait_sec=1))
        listener.dex.get_pairs_for_mint = MagicMock(
            return_value=[self._holder_recheck_pair(marketCap=50_000)]
        )
        listener.holders.get_metrics = MagicMock(return_value=(1, 100.0))

        with patch("token_scanner.time.sleep") as sleep:
            candidate = listener._evaluate_mint("MintX", "X", "X", None)

        self.assertIsNone(candidate)
        self.assertEqual(listener.holders.get_metrics.call_count, 1)
        sleep.assert_not_called()

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

    def test_pair_discovery_defaults_use_slower_polling(self):
        cfg = ScannerConfig()
        self.assertEqual(cfg.pair_wait_sec, 35)
        self.assertEqual(cfg.pair_poll_interval_sec, 2.0)

    def test_missing_pair_retries_use_configured_poll_interval(self):
        listener = PumpFunListener(
            ScannerConfig(pair_wait_sec=5, pair_poll_interval_sec=2.0)
        )
        with patch.object(
            listener.dex, "get_pairs_for_mint", return_value=[]
        ) as get_pairs, patch(
            "token_scanner.time.monotonic",
            side_effect=[0.0, 0.0, 0.5, 1.0, 1.5, 2.0, 5.0, 5.0],
        ), patch("token_scanner.time.sleep") as sleep:
            self.assertIsNone(
                listener._evaluate_mint("MintX", "X", "X", None)
            )

        self.assertEqual(get_pairs.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [2.0, 2.0],
        )

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

    def test_executable_price_from_jupiter_sell_quote(self):
        with patch("bot.JupiterClient") as jupiter:
            jupiter.return_value.get_quote.return_value = {"outAmount": "1500000"}
            price = self.oracle.get_executable_price("MintX", 3.0, 6)
        self.assertAlmostEqual(price, 0.5)
        args = jupiter.return_value.get_quote.call_args.args
        self.assertEqual(args, ("MintX", self.cfg.INPUT_MINT, 3_000_000))

    def test_executable_price_is_none_when_quote_unusable(self):
        with patch("bot.JupiterClient") as jupiter:
            client = jupiter.return_value
            client.get_quote.return_value = {"outAmount": "0"}
            self.assertIsNone(self.oracle.get_executable_price("MintX", 3.0, 6))
            client.get_quote.return_value = {}
            self.assertIsNone(self.oracle.get_executable_price("MintX", 3.0, 6))
            client.get_quote.side_effect = RuntimeError("no route")
            self.assertIsNone(self.oracle.get_executable_price("MintX", 3.0, 6))
        for amount in (0, -1.0, float("nan")):
            self.assertIsNone(self.oracle.get_executable_price("MintX", amount, 6))

    def test_missing_dex_liquidity_is_unknown_not_zero(self):
        for liquidity_field in (None, {}, {"usd": None}):
            with self.subTest(liquidity=liquidity_field):
                pair = self.pair(price="0.001")
                pair["liquidity"] = liquidity_field
                self.oracle.dex.get_pairs_for_mint.return_value = [pair]
                self.assertEqual(
                    self.oracle.get_price_and_liquidity("MintX"),
                    (0.001, None),
                )

    def test_explicit_zero_dex_liquidity_stays_zero(self):
        self.oracle.dex.get_pairs_for_mint.return_value = [
            self.pair(price="0.001", liquidity=0)
        ]
        self.assertEqual(
            self.oracle.get_price_and_liquidity("MintX"),
            (0.001, 0.0),
        )

    def test_known_liquidity_pair_is_preferred_over_unknown_pair(self):
        unknown = self.pair(price="0.5")
        unknown["liquidity"] = None
        self.oracle.dex.get_pairs_for_mint.return_value = [
            unknown, self.pair(price="0.001", liquidity=5000)
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

    def test_dust_and_oversized_initial_buys_are_not_evaluated_but_unknown_and_real_buys_are(self):
        events = [
            {"txType": "create", "mint": "Zero", "name": "Z", "symbol": "Z", "solAmount": 0},
            {"txType": "create", "mint": "Dust", "name": "D", "symbol": "D", "solAmount": 0.05},
            {"txType": "create", "mint": "Real", "name": "R", "symbol": "R", "solAmount": 2.0},
            {"txType": "create", "mint": "AtCap", "name": "C", "symbol": "C", "solAmount": 5.0},
            {"txType": "create", "mint": "Whale", "name": "W", "symbol": "W", "solAmount": 9.8},
            {"txType": "create", "mint": "Unknown", "name": "U", "symbol": "U"},
        ]

        class Socket:
            def __init__(self, stop_event):
                self.stop_event = stop_event
                self.messages = [json.dumps(e) for e in events]

            async def send(self, message):
                pass

            async def recv(self):
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.sleep(0.05)
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
            with patch(
                "token_scanner.websockets.connect",
                return_value=Context(stop_event),
            ), patch.object(
                listener, "_evaluate_mint", return_value=None
            ) as evaluate:
                await listener._listen_ws("endpoint", asyncio.Queue(), stop_event)
            self.assertEqual(
                sorted(call.args[0] for call in evaluate.call_args_list),
                ["AtCap", "Real", "Unknown"],
            )

        asyncio.run(run_test())

    def test_launch_that_waited_too_long_for_a_slot_is_dropped(self):
        class Socket:
            def __init__(self, stop_event):
                self.stop_event = stop_event
                self.messages = [
                    json.dumps({"txType": "create", "mint": mint,
                                "name": mint, "symbol": mint})
                    for mint in ("MintA", "MintB")
                ]

            async def send(self, message):
                pass

            async def recv(self):
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.sleep(0.6)
                self.stop_event.set()
                raise RuntimeError("test disconnect")

        class Context:
            def __init__(self, stop_event):
                self.socket = Socket(stop_event)

            async def __aenter__(self):
                return self.socket

            async def __aexit__(self, exc_type, exc, tb):
                return False

        def slow_evaluate(*_args):
            time.sleep(0.25)
            return None

        async def run_test():
            listener = PumpFunListener(
                ScannerConfig(max_parallel_evaluations=1, max_queue_wait_sec=0.05)
            )
            stop_event = asyncio.Event()
            with patch(
                "token_scanner.websockets.connect",
                return_value=Context(stop_event),
            ), patch.object(
                listener, "_evaluate_mint", side_effect=slow_evaluate
            ) as evaluate:
                await listener._listen_ws("endpoint", asyncio.Queue(), stop_event)
            self.assertEqual(evaluate.call_count, 1)
            self.assertEqual(evaluate.call_args.args[0], "MintA")

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

    def run_monitor(
        self, prices, step=8.0, stop=STOP, entry=ENTRY, max_no_price=None,
        journal=None,
    ):
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
        if journal is None:
            journal = SimpleNamespace(data={}, save=MagicMock())
        fake_self = SimpleNamespace(
            oracle=SimpleNamespace(get_price_and_liquidity=get_price_and_liquidity),
            cfg=cfg,
            j=journal,
        )
        position = {
            "target_price": 2.0,
            "stop_price": stop,
            "token_mint": "StopLossMint1111",
            "token_symbol": "SL",
            "entry_price": entry,
            "trailing_active": False,
            "trailing_peak_price": None,
        }
        with patch("bot.time.time", lambda: clock[0]), \
                patch("bot.time.sleep", fake_sleep):
            result = CompoundingBot.monitor_price(fake_self, position)
        return result, len(polled)

    def assertStillOpen(self, prices, polls, **kwargs):
        with self.assertRaises(_PricesExhausted) as ctx:
            self.run_monitor(prices, **kwargs)
        self.assertEqual(ctx.exception.polls, polls)

    # Fast executable price beats a stale DexScreener price.
    def test_monitor_prefers_executable_price_over_stale_dex_price(self):
        clock = [1_000.0]
        exec_feed = iter([1.0, 0.90, 0.70])
        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)
            clock[0] += 3.0

        cfg = Config()
        fake_self = SimpleNamespace(
            oracle=SimpleNamespace(
                get_price_and_liquidity=lambda _m: (1.0, 10_000.0),
                get_executable_price=lambda *_a: next(exec_feed),
            ),
            cfg=cfg,
            j=SimpleNamespace(data={}, save=MagicMock()),
        )
        position = {
            "target_price": 2.0, "stop_price": self.STOP,
            "token_mint": "StopLossMint1111", "token_symbol": "SL",
            "entry_price": 1.0, "token_amount": 10.0, "token_decimals": 6,
            "trailing_active": False, "trailing_peak_price": None,
        }
        with patch("bot.time.time", lambda: clock[0]), \
                patch("bot.time.sleep", fake_sleep):
            result = CompoundingBot.monitor_price(fake_self, position)

        self.assertEqual(result, ("stop_loss", 0.70))
        self.assertEqual(set(slept), {cfg.MONITOR_POLL_SEC})

    def test_monitor_falls_back_to_dex_price_without_executable_quote(self):
        clock = [1_000.0]
        dex_feed = iter([1.0, 0.90, 0.70])
        fake_self = SimpleNamespace(
            oracle=SimpleNamespace(
                get_price_and_liquidity=lambda _m: (next(dex_feed), 10_000.0),
                get_executable_price=lambda *_a: None,
            ),
            cfg=Config(),
            j=SimpleNamespace(data={}, save=MagicMock()),
        )
        position = {
            "target_price": 2.0, "stop_price": self.STOP,
            "token_mint": "StopLossMint1111", "token_symbol": "SL",
            "entry_price": 1.0, "token_amount": 10.0, "token_decimals": 6,
            "trailing_active": False, "trailing_peak_price": None,
        }

        def fake_sleep(_seconds):
            clock[0] += 3.0

        with patch("bot.time.time", lambda: clock[0]), \
                patch("bot.time.sleep", fake_sleep):
            result = CompoundingBot.monitor_price(fake_self, position)

        self.assertEqual(result, ("stop_loss", 0.70))

    def test_default_trailing_arms_at_1_25x_and_sells_on_20pct_pullback(self):
        self.assertEqual(Config.PRICE_TARGET_MULT, 1.25)
        self.assertEqual(Config.TRAILING_DRAWDOWN_PCT, 20.0)
        clock = [1_000.0]
        feed = iter([1.0, 1.3, 1.6, 1.27, 1.2])  # arms 1.3, peak 1.6, stop 1.28
        fake_self = SimpleNamespace(
            oracle=SimpleNamespace(
                get_price_and_liquidity=lambda _m: (next(feed), 10_000.0),
            ),
            cfg=Config(),
            j=SimpleNamespace(data={}, save=MagicMock()),
        )
        position = {
            "target_price": 1.0 * Config.PRICE_TARGET_MULT, "stop_price": self.STOP,
            "token_mint": "TrailMint1111", "token_symbol": "TR",
            "entry_price": 1.0, "token_amount": 10.0, "token_decimals": 6,
            "trailing_active": False, "trailing_peak_price": None,
        }

        def fake_sleep(_seconds):
            clock[0] += 3.0

        with patch("bot.time.time", lambda: clock[0]), \
                patch("bot.time.sleep", fake_sleep):
            result = CompoundingBot.monitor_price(fake_self, position)

        self.assertEqual(result, ("trailing_stop", 1.2))
        self.assertEqual(position["trailing_peak_price"], 1.6)

    # 1. Sustained crash exits immediately.
    def test_cascading_crash_exits_on_first_breach(self):
        result, polls = self.run_monitor([1.0, 0.90, 0.70])
        self.assertEqual(result, ("stop_loss", 0.70))
        self.assertEqual(polls, 3)

    # 2. A lone anomalous tick is not treated as sustained.
    def test_single_anomalous_tick_waits_and_target_activates_trailing(self):
        self.assertStillOpen([1.0, 1.0, 0.80], polls=3)
        self.assertStillOpen([1.0, 1.0, 0.80, 1.0, 2.0], polls=5)

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
        self.assertStillOpen(prefix + [2.0], polls=8)

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

    def test_trailing_stop_tracks_new_high_and_exits_after_confirmed_pullback(self):
        result, polls = self.run_monitor([1.0, 2.0, 2.5, 2.2, 2.0, 2.0])
        self.assertEqual(result, ("trailing_stop", 2.0))
        self.assertEqual(polls, 6)

    def test_trailing_stop_holds_while_price_keeps_rising(self):
        self.assertStillOpen([1.0, 2.0, 2.5, 3.0, 2.5], polls=5)

    def test_trailing_stop_confirmation_resets_after_recovery(self):
        prices = [1.0, 2.0, 2.5, 1.9, 2.1, 1.9]
        self.assertStillOpen(prices, polls=6)
        result, polls = self.run_monitor(prices + [1.9])
        self.assertEqual(result, ("trailing_stop", 1.9))
        self.assertEqual(polls, 7)

    def test_trailing_peak_is_persisted_in_open_position(self):
        journal = SimpleNamespace(data={}, save=Mock())
        with self.assertRaises(_PricesExhausted):
            self.run_monitor(
                [1.0, 2.0, 2.5], journal=journal
            )
        position = journal.data["open_position"]
        self.assertTrue(position["trailing_active"])
        self.assertEqual(position["trailing_peak_price"], 2.5)
        self.assertGreaterEqual(journal.save.call_count, 2)

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
        prices = [0.0] * 10 + [1.0] + [0.0] * 10 + [2.0] + [0.0] * 40
        result, _ = self.run_monitor(prices, max_no_price=120)
        self.assertEqual(result, ("no_price", 2.0))

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


class TestLiveTradingGate(unittest.TestCase):
    PHRASE = LIVE_CONFIRM_PHRASE

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        # Throwaway in-memory key; never funded, never leaves this process.
        self.key = base58.b58encode(bytes(Keypair())).decode()

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def live_cfg(self, **overrides):
        cfg = Config()
        cfg.DRY_RUN = False
        cfg.LIVE_TRADING_CONFIRM = self.PHRASE
        cfg.MAX_POSITION_USDC = "5"
        cfg.MAX_DAILY_LOSS_USDC = "10"
        cfg.KILL_SWITCH_FILE = "KILL_SWITCH"
        cfg.WALLET_PRIVATE_KEY = self.key
        cfg.STARTING_AMOUNT_USDC = 2.0
        cfg.MIN_PLAY_AMOUNT = 1.0
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    def live_bot(self, **overrides):
        # Live-mode startup requires a deliberate, existing journal.
        Journal().save()
        bot = CompoundingBot(self.live_cfg(**overrides))
        bot.check_balances = MagicMock(return_value=True)
        bot.scanner.find_best = MagicMock(return_value=make_candidate())
        return bot

    def test_dry_run_is_the_default_and_needs_no_live_settings(self):
        with patch.dict(os.environ):
            os.environ.pop("DRY_RUN", None)
            self.assertTrue(parse_bool_env(os.getenv("DRY_RUN", "true")))
        cfg = Config()
        cfg.DRY_RUN = True
        cfg.LIVE_TRADING_CONFIRM = ""
        cfg.MAX_POSITION_USDC = ""
        cfg.MAX_DAILY_LOSS_USDC = ""
        cfg.WALLET_PRIVATE_KEY = ""
        validate_live_config(cfg)
        bot = CompoundingBot(cfg)
        self.assertEqual(bot.pubkey, "DRY_RUN")

    def test_import_does_not_route_test_logs_to_bot_log(self):
        self.assertFalse(any(
            isinstance(handler, logging.FileHandler)
            and os.path.basename(handler.baseFilename) == "bot.log"
            for handler in logging.getLogger().handlers
        ))

    def test_valid_live_config_is_accepted(self):
        validate_live_config(self.live_cfg())

    def test_wrong_or_missing_confirmation_is_refused(self):
        for value in ("", "yes", self.PHRASE.lower(), self.PHRASE + " ", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(LiveTradingConfigError, "LIVE_TRADING_CONFIRM"):
                    validate_live_config(self.live_cfg(LIVE_TRADING_CONFIRM=value))

    def test_invalid_max_position_is_refused(self):
        bad = ("", "  ", None, "abc", "0", "-1", "nan", "inf", "-inf", True, "100.01", "1e9")
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaisesRegex(LiveTradingConfigError, "MAX_POSITION_USDC"):
                    validate_live_config(self.live_cfg(MAX_POSITION_USDC=value))

    def test_invalid_daily_loss_cap_is_refused(self):
        bad = ("", "  ", None, "abc", "0", "-5", "nan", "inf", True, "500.01")
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaisesRegex(LiveTradingConfigError, "MAX_DAILY_LOSS_USDC"):
                    validate_live_config(self.live_cfg(MAX_DAILY_LOSS_USDC=value))

    def test_ceiling_values_are_accepted(self):
        validate_live_config(self.live_cfg(
            MAX_POSITION_USDC=str(LIVE_MAX_POSITION_CEILING_USDC),
            MAX_DAILY_LOSS_USDC=str(LIVE_MAX_DAILY_LOSS_CEILING_USDC),
        ))

    def test_missing_wallet_key_or_kill_switch_path_is_refused(self):
        with self.assertRaisesRegex(LiveTradingConfigError, "WALLET_PRIVATE_KEY"):
            validate_live_config(self.live_cfg(WALLET_PRIVATE_KEY=""))
        for value in ("", "  ", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(LiveTradingConfigError, "KILL_SWITCH_FILE"):
                    validate_live_config(self.live_cfg(KILL_SWITCH_FILE=value))

    def test_starting_amount_above_position_limit_is_refused(self):
        with self.assertRaisesRegex(LiveTradingConfigError, "STARTING_AMOUNT_USDC"):
            validate_live_config(self.live_cfg(STARTING_AMOUNT_USDC=6.0))

    def test_bot_construction_refuses_invalid_live_config(self):
        with self.assertRaises(LiveTradingConfigError):
            CompoundingBot(self.live_cfg(MAX_POSITION_USDC=""))
        self.assertIsNotNone(self.live_bot())

    def test_refusal_message_does_not_leak_secrets(self):
        secret_phrase = "super-secret-wrong-phrase"
        with self.assertRaises(LiveTradingConfigError) as ctx:
            validate_live_config(self.live_cfg(
                LIVE_TRADING_CONFIRM=secret_phrase, MAX_POSITION_USDC=""))
        message = str(ctx.exception)
        self.assertNotIn(secret_phrase, message)
        self.assertNotIn(self.key, message)

    def test_kill_switch_blocks_new_live_entry_and_scan(self):
        bot = self.live_bot()
        open("KILL_SWITCH", "w").close()
        bot.execute_buy = MagicMock()
        self.assertFalse(bot.run_cycle())
        bot.scanner.find_best.assert_not_called()
        bot.execute_buy.assert_not_called()

    def test_kill_switch_blocks_execute_buy_directly(self):
        bot = self.live_bot()
        open("KILL_SWITCH", "w").close()
        bot.jup.get_quote = MagicMock()
        with self.assertRaisesRegex(LiveTradingConfigError, "Kill switch"):
            bot.execute_buy(1.0, make_candidate())
        bot.jup.get_quote.assert_not_called()

    def test_kill_switch_does_not_affect_dry_run(self):
        cfg = Config()
        cfg.DRY_RUN = True
        bot = CompoundingBot(cfg)
        open("KILL_SWITCH", "w").close()
        bot.scanner.find_best = MagicMock(return_value=None)
        bot.run_cycle()
        bot.scanner.find_best.assert_called_once()

    def test_execute_buy_refuses_amount_over_position_limit(self):
        bot = self.live_bot()
        bot.jup.get_quote = MagicMock()
        with self.assertRaisesRegex(LiveTradingConfigError, "MAX_POSITION_USDC"):
            bot.execute_buy(5.01, make_candidate())
        bot.jup.get_quote.assert_not_called()

    def test_compounded_play_amount_is_capped_at_position_limit(self):
        bot = self.live_bot()
        bot.j.data["play_amount_usdc"] = 50.0
        bot.execute_buy = MagicMock(return_value=None)
        bot.run_cycle()
        self.assertEqual(bot.execute_buy.call_args.args[0], 5.0)
        bot.check_balances.assert_called_once_with(5.0)

    def test_daily_loss_cap_blocks_new_entries(self):
        bot = self.live_bot()
        bot.j.record_pnl(-10.0)
        bot.execute_buy = MagicMock()
        self.assertFalse(bot.run_cycle())
        bot.scanner.find_best.assert_not_called()
        bot.execute_buy.assert_not_called()

    def test_loss_below_cap_still_allows_entry(self):
        bot = self.live_bot()
        bot.j.record_pnl(-9.99)
        bot.execute_buy = MagicMock(return_value=None)
        bot.run_cycle()
        bot.execute_buy.assert_called_once()

    def test_daily_loss_resets_on_a_new_day(self):
        journal = Journal()
        journal.data["daily_pnl"] = {"date": "2000-01-01", "pnl_usdc": -999.0}
        self.assertEqual(journal.realized_pnl_today(), 0.0)

    def test_completed_cycle_records_realized_pnl(self):
        cfg = Config()
        cfg.DRY_RUN = True
        bot = CompoundingBot(cfg)
        bot.scanner.find_best = MagicMock(return_value=make_candidate())
        bot.execute_buy = MagicMock(return_value={
            "buy_tx": "sig", "usdc_spent": 2.0, "token_amount": 1.0,
            "token_decimals": 6, "token_symbol": "T", "entry_price": 2.0,
            "target_price": 4.0, "stop_price": 1.6, "token_mint": "MintX",
            "dex_url": "",
        })
        bot.monitor_price = MagicMock(return_value=("stop_loss", 1.0))
        bot.execute_sell = MagicMock(return_value=1.5)
        self.assertTrue(bot.run_cycle())
        self.assertAlmostEqual(bot.j.realized_pnl_today(), -0.5)


class FakeRpc:
    def __init__(self):
        self.balances = {}
        self.height = 1000
        self.status = None
        self.fail_balance = False
        self.fail_status = False
        self.sol = 1_000_000_000
        self.tx_delta = None
        self.fail_tx = False
        self.account = None
        self.fail_account = False

    def sol_balance(self, owner):
        return self.sol

    def transaction_sol_delta(self, sig, owner):
        if self.fail_tx:
            raise RpcError("metadata unavailable")
        return self.tx_delta

    def account_data(self, address, expected_owner=None):
        if self.fail_account:
            raise RpcError("account down")
        return self.account

    def token_balance(self, owner, mint):
        if self.fail_balance:
            raise RpcError("balance down")
        return self.balances.get(mint, 0)

    def block_height(self):
        return self.height

    def signature_status(self, sig):
        if self.fail_status:
            raise RpcError("status down")
        return self.status


class TestJournalIntegrity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()

    def test_save_is_atomic_and_keeps_backup(self):
        j = Journal()
        j.data["cycle"] = 1
        j.save()
        j.data["cycle"] = 2
        j.save()
        self.assertFalse(os.path.exists("journal.json.tmp"))
        with open("journal.json") as f:
            self.assertEqual(json.load(f)["cycle"], 2)
        with open("journal.json.bak") as f:
            self.assertEqual(json.load(f)["cycle"], 1)

    def test_dry_run_recovers_corrupt_journal_from_backup(self):
        j = Journal()
        j.data["cycle"] = 7
        j.save()
        j.save()
        with open("journal.json", "w") as f:
            f.write('{"cycle": 8, "tru')
        recovered = Journal(strict=False)
        self.assertEqual(recovered.data["cycle"], 7)
        self.assertTrue(any(n.startswith("journal.json.corrupt-") for n in os.listdir(".")))

    def test_dry_run_corrupt_without_backup_starts_fresh(self):
        with open("journal.json", "w") as f:
            f.write("not json")
        self.assertEqual(Journal(strict=False).data["cycle"], 0)

    def test_live_mode_refuses_corrupt_journal_and_leaves_it_untouched(self):
        with open("journal.json", "w") as f:
            f.write("{broken")
        with self.assertRaises(JournalCorruptError):
            Journal(strict=True)
        with open("journal.json") as f:
            self.assertEqual(f.read(), "{broken")

    def test_live_mode_refuses_missing_journal(self):
        with self.assertRaisesRegex(JournalCorruptError, "is missing"):
            Journal(strict=True)

    def test_live_mode_refuses_valid_json_with_invalid_transaction_state(self):
        with open("journal.json", "w") as f:
            json.dump({"cycle": 0, "pending_tx": {"state": "sent"}}, f)
        with self.assertRaisesRegex(JournalCorruptError, "unreadable"):
            Journal(strict=True)


class TestTransactionReconciliation(TestLiveTradingGate):
    USDC = None

    def setUp(self):
        super().setUp()
        self.bot = self.live_bot(CONFIRM_MAX_WAIT_SEC=0, CONFIRM_POLL_SEC=0.1)
        self.rpc = FakeRpc()
        self.bot.rpc = self.rpc
        self.cand = make_candidate()
        self.usdc = self.bot.cfg.INPUT_MINT
        self.rpc.balances = {self.usdc: 10_000_000}
        self.bot.oracle.get_token_decimals = MagicMock(return_value=6)
        jup = MagicMock()
        jup.get_quote.return_value = {"outAmount": "1000000000"}
        jup.build_swap_transaction.return_value = "rawtx"
        jup.sign_transaction.return_value = ("signed", "SIG1")
        self.bot.jup = jup
        self.confirmed = {"confirmationStatus": "confirmed", "err": None}

    def land_buy(self, *_):
        self.rpc.balances[self.cand.mint] = 1_000_000_000
        self.rpc.balances[self.usdc] = 8_000_000
        self.rpc.status = self.confirmed

    def test_buy_is_journaled_before_send_and_fills_from_chain(self):
        seen = {}

        def submit(signed):
            with open("journal.json") as f:
                seen["pending"] = json.load(f)["pending_tx"]
            self.land_buy()
            return "SIG1"

        self.bot.jup.submit_encoded.side_effect = submit
        pos = self.bot.execute_buy(2.0, self.cand)
        self.assertEqual(seen["pending"]["signature"], "SIG1")
        self.assertEqual(seen["pending"]["state"], "sent")
        self.assertEqual(pos["fill_source"], "onchain")
        self.assertAlmostEqual(pos["token_amount"], 1000.0)
        self.assertAlmostEqual(pos["usdc_spent"], 2.0)
        self.assertIsNone(self.bot.j.data["pending_tx"])

    def test_buy_refused_when_price_ran_past_market_cap_cap(self):
        # candidate: price 0.3 @ mcap 8,000. A $2 quote for 5 tokens = 0.4/token,
        # implying mcap ~10,667, above the 10,000 cap.
        self.bot.jup.get_quote.return_value = {"outAmount": "5000000"}
        with self.assertLogs("bot", level="WARNING") as logs:
            self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.assertIn("ran up since evaluation", "\n".join(logs.output))
        self.bot.jup.build_swap_transaction.assert_not_called()
        self.bot.jup.submit_encoded.assert_not_called()
        self.assertIsNone(self.bot.j.data.get("pending_tx"))

    def test_buy_refused_when_entry_ran_up_past_limit_even_within_market_cap(self):
        cand = make_candidate(price_usd=0.3, market_cap=4_000.0)
        # $2 for 5.0 tokens = 0.40 -> 1.33x the evaluated price; implied mcap
        # ~5,333 is still far below the cap, so only the run-up limit refuses.
        self.bot.jup.get_quote.return_value = {"outAmount": "5000000"}
        with self.assertLogs("bot", level="WARNING") as logs:
            self.assertIsNone(self.bot.execute_buy(2.0, cand))
        output = "\n".join(logs.output)
        self.assertIn("ran up since evaluation", output)
        self.assertIn("1.33x", output)
        self.bot.jup.build_swap_transaction.assert_not_called()
        self.bot.jup.submit_encoded.assert_not_called()
        self.assertIsNone(self.bot.j.data.get("pending_tx"))

    def test_buy_allowed_when_entry_runup_is_within_limit(self):
        cand = make_candidate(price_usd=0.3, market_cap=4_000.0)
        # $2 for 5.4 tokens = 0.3704 -> 1.23x, under the 1.25x limit.
        self.bot.jup.get_quote.return_value = {"outAmount": "5400000"}
        self.bot.jup.submit_encoded.side_effect = lambda signed: (self.land_buy(), "SIG1")[1]
        self.assertIsNotNone(self.bot.execute_buy(2.0, cand))

    def test_entry_runup_limit_default_is_1_25x(self):
        self.assertEqual(ScannerConfig().max_entry_runup_ratio, 1.25)

    def test_buy_allowed_when_entry_stays_within_market_cap_cap(self):
        # 6 tokens for $2 = 0.333/token -> implied mcap ~8,889 (< 10,000).
        self.bot.jup.get_quote.return_value = {"outAmount": "6000000"}
        self.bot.jup.submit_encoded.side_effect = lambda signed: (self.land_buy(), "SIG1")[1]
        self.assertIsNotNone(self.bot.execute_buy(2.0, self.cand))

    def test_buy_refused_when_entry_market_cap_cannot_be_verified(self):
        bad = make_candidate(price_usd=0.0)
        with self.assertLogs("bot", level="ERROR") as logs:
            self.assertIsNone(self.bot.execute_buy(2.0, bad))
        self.assertIn("Cannot verify entry market cap", "\n".join(logs.output))
        self.bot.jup.build_swap_transaction.assert_not_called()

    def test_direct_buy_is_refused_while_position_is_open(self):
        self.bot.j.data["open_position"] = self.position()
        with self.assertLogs("bot", level="ERROR"):
            self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.bot.jup.get_quote.assert_not_called()

    def test_actual_fill_differs_from_quote(self):
        def submit(signed):
            self.land_buy()
            self.rpc.balances[self.cand.mint] = 500_000_000
            return "SIG1"

        self.bot.jup.submit_encoded.side_effect = submit
        pos = self.bot.execute_buy(2.0, self.cand)
        self.assertAlmostEqual(pos["token_amount"], 500.0)
        self.assertAlmostEqual(pos["entry_price"], 0.004)
        self.assertAlmostEqual(pos["quoted_token_amount"], 1000.0)

    def test_confirm_timeout_leaves_unknown_and_blocks_new_buy(self):
        self.bot.jup.submit_encoded.return_value = "SIG1"
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "unknown")
        self.bot.jup.build_swap_transaction.reset_mock()
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.bot.jup.build_swap_transaction.assert_not_called()
        self.assertFalse(self.bot.run_cycle())

    def test_blockhash_expiry_clears_only_after_expiry_height(self):
        self.bot.jup.submit_encoded.return_value = "SIG1"
        self.bot.execute_buy(2.0, self.cand)
        self.rpc.height = 1150
        self.assertEqual(self.bot.reconcile_pending()["state"], "unknown")
        self.rpc.height = 1151
        self.assertEqual(self.bot.reconcile_pending()["state"], "failed")
        self.assertIsNone(self.bot.j.data["pending_tx"])

    def test_onchain_error_clears_pending(self):
        self.rpc.status = {"confirmationStatus": "confirmed", "err": {"x": 1}}
        self.bot.jup.submit_encoded.return_value = "SIG1"
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.assertIsNone(self.bot.j.data["pending_tx"])

    def test_failed_buy_retries_once_with_fresh_wider_quote(self):
        attempts = []

        def submit(_signed):
            attempts.append(1)
            if len(attempts) == 1:
                self.rpc.status = {"confirmationStatus": "confirmed", "err": {"x": 1}}
            else:
                self.land_buy()
            return "SIG1"

        self.bot.jup.submit_encoded.side_effect = submit
        pos = self.bot.execute_buy(2.0, self.cand)
        self.assertIsNotNone(pos)
        self.assertEqual(self.bot.jup.get_quote.call_count, 2)
        self.assertEqual(self.bot.jup.get_quote.call_args.kwargs["slippage_bps"], 500)
        self.assertEqual(len(attempts), 2)

    def test_failed_buy_retry_is_limited_to_one_attempt(self):
        self.rpc.status = {"confirmationStatus": "confirmed", "err": {"x": 1}}
        self.bot.jup.submit_encoded.return_value = "SIG1"
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.assertEqual(self.bot.jup.submit_encoded.call_count, 2)
        self.assertIsNone(self.bot.j.data["pending_tx"])

    def test_unknown_buy_state_is_never_retried(self):
        self.bot.jup.submit_encoded.return_value = "SIG1"
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.assertEqual(self.bot.jup.submit_encoded.call_count, 1)
        self.assertEqual(self.bot.jup.get_quote.call_count, 1)

    def test_legacy_finalized_status_is_confirmed(self):
        self.rpc.status = {"confirmations": None, "err": None}

        def submit(_signed):
            self.rpc.balances[self.cand.mint] = 1_000_000_000
            self.rpc.balances[self.usdc] = 8_000_000

        self.bot.jup.submit_encoded.side_effect = submit
        position = self.bot.execute_buy(2.0, self.cand)
        self.assertIsNotNone(position)

    def test_processed_failure_is_not_treated_as_final(self):
        self.rpc.status = {"confirmationStatus": "processed", "err": {"x": 1}}
        self.bot.jup.submit_encoded.return_value = "SIG1"
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "unknown")

    def test_rpc_error_while_settling_stays_unknown(self):
        self.rpc.fail_status = True
        self.bot.jup.submit_encoded.return_value = "SIG1"
        self.bot.execute_buy(2.0, self.cand)
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "unknown")

    def test_unreadable_pre_balance_sends_nothing(self):
        self.rpc.fail_balance = True
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.bot.jup.submit_encoded.assert_not_called()
        self.assertIsNone(self.bot.j.data.get("pending_tx"))

    def test_rpc_rejection_is_reconciled_instead_of_clearing_pending(self):
        self.bot.jup.submit_encoded.side_effect = RuntimeError("RPC error: nope")
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "unknown")

    def test_ambiguous_send_error_is_settled_by_signature_not_resent(self):
        def submit(signed):
            self.land_buy()
            raise requests.ConnectionError("reset")

        self.bot.jup.submit_encoded.side_effect = submit
        pos = self.bot.execute_buy(2.0, self.cand)
        self.assertIsNotNone(pos)
        self.assertEqual(self.bot.jup.submit_encoded.call_count, 1)

    def test_restart_recovers_pending_buy_without_rebuilding(self):
        self.bot.jup.submit_encoded.return_value = "SIG1"
        self.bot.execute_buy(2.0, self.cand)
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "unknown")

        restarted = CompoundingBot(self.live_cfg(CONFIRM_MAX_WAIT_SEC=0))
        restarted.rpc = self.rpc
        restarted.jup = MagicMock()
        self.assertEqual(restarted.j.data["pending_tx"]["signature"], "SIG1")
        self.land_buy()
        res = restarted.reconcile_pending()
        self.assertEqual(res["state"], "confirmed")
        self.assertAlmostEqual(restarted.j.data["open_position"]["token_amount"], 1000.0)
        restarted.jup.build_swap_transaction.assert_not_called()
        restarted.jup.submit_encoded.assert_not_called()

    def test_confirmed_buy_with_mismatched_balances_is_flagged(self):
        def submit(signed):
            self.rpc.status = self.confirmed
            return "SIG1"

        self.bot.jup.submit_encoded.side_effect = submit
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "anomaly")
        self.assertIsNone(self.bot.j.data.get("open_position"))
        self.assertFalse(self.bot.run_cycle())

    def position(self):
        return {
            "buy_tx": "B", "usdc_spent": 2.0, "token_amount": 1000.0,
            "token_decimals": 6, "token_symbol": "TKN", "entry_price": 0.002,
            "target_price": 0.004, "stop_price": 0.0016,
            "token_mint": self.cand.mint, "dex_url": "",
        }

    def test_sell_uses_onchain_balance_and_books_actual_proceeds(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000
        self.bot.jup.get_quote.return_value = {"outAmount": "3000000"}

        def submit(signed):
            self.rpc.balances[self.cand.mint] = 0
            self.rpc.balances[self.usdc] += 2_500_000
            self.rpc.status = self.confirmed
            return "SIG1"

        self.bot.jup.submit_encoded.side_effect = submit
        proceeds = self.bot.execute_sell(pos, "target")
        self.assertAlmostEqual(proceeds, 2.5)
        self.assertEqual(self.bot.jup.get_quote.call_args[0][2], 900_000_000)
        self.assertIsNone(self.bot.j.data["open_position"])
        self.assertEqual(self.bot.j.data["cycle"], 1)
        self.assertAlmostEqual(self.bot.j.realized_pnl_today(), 0.5)

    def test_sell_never_resends_while_previous_is_unknown(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 1_000_000_000
        self.bot.jup.get_quote.return_value = {"outAmount": "3000000"}
        self.bot.jup.submit_encoded.return_value = "SIG1"
        self.assertIsNone(self.bot.execute_sell(pos, "stop_loss"))
        self.assertEqual(self.bot.jup.submit_encoded.call_count, 1)
        self.assertIsNone(self.bot.execute_sell(pos, "stop_loss"))
        self.assertEqual(self.bot.jup.submit_encoded.call_count, 1)

    def test_sell_retries_with_new_tx_only_after_proven_failure(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 1_000_000_000
        self.bot.jup.get_quote.return_value = {"outAmount": "3000000"}
        self.bot.jup.sign_transaction.side_effect = [("s1", "SIG1"), ("s2", "SIG2")]
        calls = []

        def submit(signed):
            calls.append(signed)
            if signed == "s1":
                self.rpc.status = {"confirmationStatus": "confirmed", "err": {"e": 1}}
            else:
                self.rpc.balances[self.cand.mint] = 0
                self.rpc.balances[self.usdc] += 2_000_000
                self.rpc.status = self.confirmed
            return "x"

        self.bot.jup.submit_encoded.side_effect = submit
        with patch("bot.time.sleep"):
            self.assertAlmostEqual(self.bot.execute_sell(pos, "stop_loss"), 2.0)
        self.assertEqual(calls, ["s1", "s2"])

    def test_restart_reconciles_sell_and_accounts_cycle_once(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.bot.j.data["pending_tx"] = {
            "kind": "sell", "state": "unknown", "mint": self.cand.mint,
            "signature": "SELLSIG", "pre_token": 1_000_000_000,
            "pre_usdc": 8_000_000, "expiry_height": 1150,
            "meta": {
                "symbol": "TKN", "amount_raw": 1_000_000_000,
                "usdc_spent": 2.0, "reason": "target", "exit_price": 0.004,
                "cycle": 1,
            },
        }
        self.bot.j.save()
        self.rpc.balances[self.cand.mint] = 0
        self.rpc.balances[self.usdc] = 10_500_000
        self.rpc.status = self.confirmed

        restarted = CompoundingBot(self.live_cfg(CONFIRM_MAX_WAIT_SEC=0))
        restarted.rpc = self.rpc
        result = restarted.reconcile_pending()

        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(restarted.j.data["cycle"], 1)
        self.assertIsNone(restarted.j.data["pending_tx"])
        self.assertIsNone(restarted.j.data["open_position"])
        self.assertAlmostEqual(restarted.j.realized_pnl_today(), 0.5)
        self.assertAlmostEqual(restarted.j.data["play_amount_usdc"], 1.5)
        self.assertEqual(len(restarted.j.data["history"]), 1)

    def test_zero_onchain_balance_keeps_position_blocked_for_manual_review(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 0
        self.assertIsNone(self.bot.execute_sell(pos, "target"))
        self.bot.jup.submit_encoded.assert_not_called()
        self.assertIs(self.bot.j.data["open_position"], pos)
        self.assertTrue(pos["manual_review"])

    def test_confirmed_sell_with_remaining_tokens_stays_blocked(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 1_000_000_000
        self.bot.jup.get_quote.return_value = {"outAmount": "3000000"}

        def submit(_signed):
            self.rpc.balances[self.cand.mint] = 100
            self.rpc.balances[self.usdc] += 2_000_000
            self.rpc.status = self.confirmed

        self.bot.jup.submit_encoded.side_effect = submit
        with patch("bot.time.sleep"):
            self.assertIsNone(self.bot.execute_sell(pos, "stop_loss"))
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "anomaly")
        self.assertIs(self.bot.j.data["open_position"], pos)
        self.assertEqual(self.bot.j.data["cycle"], 0)

    def test_unreadable_balance_blocks_sell(self):
        pos = self.position()
        self.rpc.fail_balance = True
        self.assertIsNone(self.bot.execute_sell(pos, "stop_loss"))
        self.bot.jup.submit_encoded.assert_not_called()

    def test_kill_switch_still_allows_exit(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        open("KILL_SWITCH", "w").close()
        self.rpc.balances[self.cand.mint] = 1_000_000_000
        self.bot.jup.get_quote.return_value = {"outAmount": "3000000"}

        def submit(signed):
            self.rpc.balances[self.cand.mint] = 0
            self.rpc.balances[self.usdc] += 3_000_000
            self.rpc.status = self.confirmed
            return "x"

        self.bot.jup.submit_encoded.side_effect = submit
        self.assertAlmostEqual(self.bot.execute_sell(pos, "stop_loss"), 3.0)
        with self.assertRaises(LiveTradingConfigError):
            self.bot.execute_buy(1.0, self.cand)

    def test_startup_does_not_require_entry_usdc_to_manage_open_position(self):
        self.bot.j.data["open_position"] = self.position()
        self.bot.cfg.MAX_CYCLES = 1
        self.bot.run_cycle = MagicMock(return_value=True)
        self.bot.check_balances = MagicMock(return_value=True)
        self.bot.run()
        self.bot.check_balances.assert_called_once_with(0.0)


class TestCurveRoute(TestLiveTradingGate):
    """Pump.fun bonding-curve route, wired through the pending-tx pipeline (mocks only)."""

    def setUp(self):
        super().setUp()
        from decimal import Decimal
        self.Decimal = Decimal
        self.bot = self.live_bot(
            CONFIRM_MAX_WAIT_SEC=0, CONFIRM_POLL_SEC=0.1, CURVE_TRADING=True)
        self.rpc = FakeRpc()
        self.bot.rpc = self.rpc
        self.cand = make_candidate()
        self.usdc = self.bot.cfg.INPUT_MINT
        self.rpc.account = TestChainHelpers._curve_bytes()
        self.bot.oracle.get_token_decimals = MagicMock(return_value=6)
        jup = MagicMock()
        jup.get_quote.return_value = {"outAmount": "150000000"}  # $150 per SOL
        jup.build_swap_transaction.return_value = "jupraw"
        jup.sign_transaction.return_value = ("signed", "SIG1")
        self.bot.jup = jup
        self.bot.portal = MagicMock()
        self.bot.portal.build_transaction.return_value = "portalraw"
        self.confirmed = {"confirmationStatus": "confirmed", "err": None}

    def land_buy(self, *_):
        self.rpc.balances[self.cand.mint] = 1_000_000_000
        self.rpc.tx_delta = (-13_500_000, 5000)
        self.rpc.status = self.confirmed
        return "SIG1"

    def position(self, **extra):
        pos = {
            "buy_tx": "B", "usdc_spent": 2.0, "token_amount": 1000.0,
            "token_decimals": 6, "token_symbol": "TKN", "entry_price": 0.002,
            "target_price": 0.004, "stop_price": 0.0016,
            "token_mint": self.cand.mint, "dex_url": "", "route": "pump",
        }
        pos.update(extra)
        return pos

    def test_curve_route_is_off_by_default(self):
        self.assertFalse(Config.CURVE_TRADING)

    def test_curve_reserve_fallback_uses_confirmed_wsol_reserves(self):
        self.bot._curve_state = MagicMock(return_value={
            "complete": False,
            "quote_mint": "So11111111111111111111111111111111111111112",
            "is_cashback_coin": False,
            "is_mayhem_mode": False,
            "virtual_token_reserves": 1_000_000_000_000,
            "virtual_quote_reserves": 30_000_000_000,
            "real_quote_reserves": 25_000_000_000,
            "token_total_supply": 1_000_000_000_000_000,
        })
        self.bot._sol_usd = MagicMock(return_value=150.0)
        self.bot._curve_sol_usd_cache = None
        self.bot.oracle.get_token_decimals = MagicMock(return_value=6)

        metrics = self.bot._curve_reserve_metrics(self.cand.mint)

        self.assertEqual(metrics.liquidity_usd, 3_750.0)
        self.assertAlmostEqual(metrics.price_usd, 0.0045)
        self.assertAlmostEqual(metrics.market_cap_usd, 4_500_000.0)

    def test_curve_reserve_fallback_rejects_non_wsol_quotes(self):
        self.bot._curve_state = MagicMock(return_value={
            "complete": False,
            "quote_mint": self.bot.cfg.INPUT_MINT,
            "is_cashback_coin": False,
            "is_mayhem_mode": False,
            "virtual_token_reserves": 1_000_000_000_000,
            "virtual_quote_reserves": 30_000_000_000,
            "real_quote_reserves": 25_000_000_000,
            "token_total_supply": 1_000_000_000_000_000,
        })
        self.bot._sol_usd = MagicMock(return_value=150.0)
        self.bot.oracle.get_token_decimals = MagicMock(return_value=6)

        with self.assertLogs("bot", level="INFO") as logs:
            self.assertIsNone(self.bot._curve_reserve_metrics(self.cand.mint))
        self.assertIn("quote mint is not SOL", "\n".join(logs.output))
        self.bot._sol_usd.assert_not_called()

    def test_curve_reserve_fallback_accepts_native_sol_quote_but_buy_route_stays_wsol_only(self):
        from chain import NATIVE_SOL_QUOTE
        self.bot._curve_state = MagicMock(return_value={
            "complete": False,
            "quote_mint": NATIVE_SOL_QUOTE,
            "is_cashback_coin": False,
            "is_mayhem_mode": False,
            "virtual_token_reserves": 1_000_000_000_000,
            "virtual_quote_reserves": 30_000_000_000,
            "real_quote_reserves": 25_000_000_000,
            "token_total_supply": 1_000_000_000_000_000,
        })
        self.bot._sol_usd = MagicMock(return_value=150.0)
        self.bot._curve_sol_usd_cache = None

        metrics = self.bot._curve_reserve_metrics(self.cand.mint)

        self.assertEqual(metrics.liquidity_usd, 3_750.0)
        self.bot.jup.build_swap_transaction.assert_not_called()
        with self.assertLogs("bot", level="ERROR"):
            self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.bot.portal.build_transaction.assert_not_called()

    def test_curve_reserve_fallback_rejects_untouched_curve_with_clear_reason(self):
        self.bot._curve_state = MagicMock(return_value={
            "complete": False,
            "quote_mint": "11111111111111111111111111111111",
            "is_cashback_coin": False,
            "is_mayhem_mode": False,
            "virtual_token_reserves": 1_073_000_000_000_000,
            "virtual_quote_reserves": 30_000_000_000,
            "real_quote_reserves": 0,
            "token_total_supply": 1_000_000_000_000_000,
        })
        self.bot._sol_usd = MagicMock(return_value=150.0)
        self.bot.oracle.get_token_decimals = MagicMock(return_value=6)

        with self.assertLogs("bot", level="INFO") as logs:
            self.assertIsNone(self.bot._curve_reserve_metrics(self.cand.mint))

        output = "\n".join(logs.output)
        self.assertIn("no real quote reserves yet", output)
        self.assertNotIn("derived metrics", output)
        self.bot._sol_usd.assert_not_called()
        self.bot.oracle.get_token_decimals.assert_not_called()

    def test_curve_reserve_fallback_rejects_completed_curve_with_zero_reserves(self):
        self.rpc.account = TestChainHelpers._curve_bytes(
            complete=True, vtok=0, vquote=0)

        with self.assertLogs("bot", level="INFO") as logs:
            self.assertIsNone(self.bot._curve_reserve_metrics(self.cand.mint))

        self.assertIn("curve is complete", "\n".join(logs.output))
        self.bot.jup.get_quote.assert_not_called()

    def test_curve_reserve_fallback_logs_value_error_reason(self):
        self.bot._curve_state = MagicMock(
            side_effect=ValueError("not a bonding-curve account"))

        with self.assertLogs("bot", level="WARNING") as logs:
            self.assertIsNone(self.bot._curve_reserve_metrics(self.cand.mint))

        self.assertIn(
            "validation: not a bonding-curve account",
            "\n".join(logs.output),
        )

    def test_curve_buy_fills_from_sol_delta_and_keeps_quote_separate(self):
        self.bot.jup.submit_encoded.side_effect = self.land_buy
        pos = self.bot.execute_buy(2.0, self.cand)
        args = self.bot.portal.build_transaction.call_args[0]
        self.assertEqual(args[1:5], ("buy", self.cand.mint, round(2.0 / 150, 9), True))
        self.assertEqual(args[7], "pump")
        self.bot.jup.build_swap_transaction.assert_not_called()
        self.assertEqual(pos["route"], "pump")
        self.assertEqual(pos["fill_source"], "onchain")
        self.assertAlmostEqual(pos["usdc_spent"], 0.0135 * 150)
        self.assertAlmostEqual(pos["token_amount"], 1000.0)
        self.assertEqual(pos["fee_lamports"], 5000)
        self.assertNotAlmostEqual(pos["quoted_token_amount"], pos["token_amount"])
        self.assertIsNone(self.bot.j.data["pending_tx"])

    def test_curve_buy_journals_pending_with_pre_sol_before_send(self):
        seen = {}

        def submit(signed):
            with open("journal.json") as f:
                seen["p"] = json.load(f)["pending_tx"]
            return self.land_buy()

        self.bot.jup.submit_encoded.side_effect = submit
        self.bot.execute_buy(2.0, self.cand)
        self.assertEqual(seen["p"]["pre_sol"], 1_000_000_000)
        self.assertEqual(seen["p"]["meta"]["route"], "pump")

    def test_unreadable_curve_state_refuses_buy(self):
        self.rpc.fail_account = True
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.rpc.fail_account = False
        self.rpc.account = b"garbage"
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.bot.portal.build_transaction.assert_not_called()
        self.bot.jup.build_swap_transaction.assert_not_called()
        self.assertIsNone(self.bot.j.data["pending_tx"])

    def test_unverified_curve_kinds_are_refused(self):
        usdc_quote = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        for data in (TestChainHelpers._curve_bytes(cashback=1),
                     TestChainHelpers._curve_bytes(quote=usdc_quote)):
            self.rpc.account = data
            self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.bot.portal.build_transaction.assert_not_called()
        self.bot.jup.build_swap_transaction.assert_not_called()

    def test_graduated_or_missing_curve_uses_jupiter(self):
        self.bot.jup.get_quote.return_value = {"outAmount": "1000000000"}
        for account in (TestChainHelpers._curve_bytes(complete=True), None):
            self.rpc.account = account
            self.bot.jup.submit_encoded.side_effect = None
            self.bot.jup.build_swap_transaction.reset_mock()
            self.rpc.status = None
            self.bot.execute_buy(2.0, self.cand)
            self.bot.jup.build_swap_transaction.assert_called_once()
            self.bot.j.data["pending_tx"] = None
        self.bot.portal.build_transaction.assert_not_called()

    def test_buy_refused_without_sol_price_or_fee_balance(self):
        self.bot.jup.get_quote.side_effect = RuntimeError("no quote")
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.bot.jup.get_quote.side_effect = None
        self.rpc.sol = 5_000_000
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.bot.portal.build_transaction.assert_not_called()
        self.assertIsNone(self.bot.j.data["pending_tx"])

    def test_buy_stays_pending_until_transaction_metadata_is_available(self):
        def submit(signed):
            self.land_buy()
            self.rpc.fail_tx = True
            return "SIG1"

        self.bot.jup.submit_encoded.side_effect = submit
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "confirmed")
        self.assertIsNone(self.bot.j.data.get("open_position"))
        self.assertFalse(self.bot.run_cycle())
        # A restarted bot reconciles from the journal once metadata appears.
        self.rpc.fail_tx = False
        bot2 = CompoundingBot(self.live_cfg(CURVE_TRADING=True))
        bot2.rpc = self.rpc
        self.assertEqual(bot2.reconcile_pending()["state"], "confirmed")
        self.assertEqual(bot2.j.data["open_position"]["route"], "pump")
        self.assertIsNone(bot2.j.data["pending_tx"])

    def test_buy_with_no_tokens_or_no_sol_spent_is_an_anomaly(self):
        for tokens, delta in ((0, (-13_500_000, 5000)), (1_000_000_000, (0, 5000)),
                              (1_000_000_000, (5_000_000, 5000))):
            self.bot.j.data["pending_tx"] = None
            self.rpc.balances[self.cand.mint] = 0

            def submit(signed, tokens=tokens, delta=delta):
                self.rpc.balances[self.cand.mint] = tokens
                self.rpc.tx_delta = delta
                self.rpc.status = self.confirmed
                return "SIG1"

            self.bot.jup.submit_encoded.side_effect = submit
            self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
            self.assertEqual(self.bot.j.data["pending_tx"]["state"], "anomaly")

    def test_buy_missing_sol_rate_in_journal_is_an_anomaly(self):
        self.bot.jup.submit_encoded.side_effect = self.land_buy
        self.bot.j.save()
        orig = self.bot._chain_fill

        def broken(p):
            p["meta"]["sol_usd"] = None
            return orig(p)

        self.bot._chain_fill = broken
        self.assertIsNone(self.bot.execute_buy(2.0, self.cand))
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "anomaly")

    def land_sell(self, delta=(2_990_000_000 // 100, 5000)):
        def submit(signed):
            self.rpc.balances[self.cand.mint] = 0
            self.rpc.tx_delta = delta
            self.rpc.status = self.confirmed
            return "SIG1"
        return submit

    def test_curve_sell_uses_exact_balance_and_books_sol_proceeds(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000
        self.bot.jup.submit_encoded.side_effect = self.land_sell((20_000_000, 5000))
        proceeds = self.bot.execute_sell(pos, "target")
        self.assertAlmostEqual(proceeds, 3.0)
        args = self.bot.portal.build_transaction.call_args[0]
        self.assertEqual(args[1], "sell")
        self.assertEqual(args[3], self.Decimal("900"))
        self.assertIs(args[4], False)
        self.assertIsNone(self.bot.j.data["open_position"])
        self.assertEqual(self.bot.j.data["cycle"], 1)
        self.assertAlmostEqual(self.bot.j.realized_pnl_today(), 1.0)

    def test_curve_sell_needs_no_jupiter_sell_quote(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000
        self.bot.jup.submit_encoded.side_effect = self.land_sell((20_000_000, 5000))
        self.bot.execute_sell(pos, "stop_loss")
        for call in self.bot.jup.get_quote.call_args_list:
            self.assertEqual(call[0][0], "So11111111111111111111111111111111111111112")
        self.bot.jup.build_swap_transaction.assert_not_called()

    def test_curve_sell_clamps_negative_net_proceeds_but_closes(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000
        self.bot.jup.submit_encoded.side_effect = self.land_sell((-1000, 5000))
        self.assertEqual(self.bot.execute_sell(pos, "stop_loss"), 0.0)
        self.assertIsNone(self.bot.j.data["open_position"])

    def test_curve_sell_with_remaining_tokens_is_an_anomaly(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000

        def submit(signed):
            self.rpc.balances[self.cand.mint] = 1
            self.rpc.tx_delta = (20_000_000, 5000)
            self.rpc.status = self.confirmed

        self.bot.jup.submit_encoded.side_effect = submit
        self.assertIsNone(self.bot.execute_sell(pos, "target"))
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "anomaly")

    def test_curve_sell_never_resends_when_outcome_unknown(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000
        self.rpc.status = None
        self.assertIsNone(self.bot.execute_sell(pos, "stop_loss"))
        self.assertEqual(self.bot.portal.build_transaction.call_count, 1)
        self.assertEqual(self.bot.j.data["pending_tx"]["state"], "unknown")

    def test_curve_sell_retries_only_after_proven_failure(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000
        self.rpc.status = {"confirmationStatus": "confirmed", "err": {"InstructionError": 1}}
        with patch("bot.time.sleep"):
            self.assertIsNone(self.bot.execute_sell(pos, "stop_loss"))
        slippages = [c[0][5] for c in self.bot.portal.build_transaction.call_args_list]
        self.assertEqual(slippages, list(self.bot.cfg.CURVE_SELL_SLIPPAGES_PCT))
        self.assertIsNotNone(self.bot.j.data["open_position"])

    def test_curve_sell_refused_without_fee_sol_or_sol_price(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000
        self.rpc.sol = 1000
        self.assertIsNone(self.bot.execute_sell(pos, "stop_loss"))
        self.rpc.sol = 1_000_000_000
        self.bot.jup.get_quote.side_effect = RuntimeError("no quote")
        self.assertIsNone(self.bot.execute_sell(pos, "stop_loss"))
        self.bot.portal.build_transaction.assert_not_called()
        self.assertIsNone(self.bot.j.data["pending_tx"])

    def test_curve_sell_with_unreadable_curve_state_does_not_sell_blind(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000
        self.rpc.fail_account = True
        self.assertIsNone(self.bot.execute_sell(pos, "stop_loss"))
        self.bot.portal.build_transaction.assert_not_called()
        self.bot.jup.build_swap_transaction.assert_not_called()

    def test_graduated_position_exits_through_jupiter_with_usdc_accounting(self):
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000
        self.rpc.balances[self.usdc] = 1_000_000
        self.rpc.account = TestChainHelpers._curve_bytes(complete=True)
        self.bot.jup.get_quote.return_value = {"outAmount": "3000000"}

        def submit(signed):
            self.rpc.balances[self.cand.mint] = 0
            self.rpc.balances[self.usdc] += 2_500_000
            self.rpc.status = self.confirmed

        self.bot.jup.submit_encoded.side_effect = submit
        self.assertAlmostEqual(self.bot.execute_sell(pos, "target"), 2.5)
        self.bot.portal.build_transaction.assert_not_called()

    def test_kill_switch_blocks_curve_buy_but_not_curve_sell(self):
        open("KILL_SWITCH", "w").close()
        with self.assertRaises(LiveTradingConfigError):
            self.bot.execute_buy(2.0, self.cand)
        pos = self.position()
        self.bot.j.data["open_position"] = pos
        self.rpc.balances[self.cand.mint] = 900_000_000
        self.bot.jup.submit_encoded.side_effect = self.land_sell((20_000_000, 5000))
        self.assertAlmostEqual(self.bot.execute_sell(pos, "stop_loss"), 3.0)

    def test_journal_rejects_invalid_pre_sol(self):
        self.bot.j.data["pending_tx"] = {
            "kind": "buy", "state": "sent", "mint": "m", "signature": "s",
            "pre_token": 0, "pre_usdc": 0, "pre_sol": -1, "expiry_height": 1, "meta": {},
        }
        self.bot.j.save()
        with self.assertRaisesRegex(JournalCorruptError, "unreadable"):
            Journal(strict=True)


class TestChainHelpers(unittest.TestCase):
    def test_token_balance_sums_accounts(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"result": {"value": [
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "5"}}}}}},
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "7"}}}}}},
        ]}}
        self.assertEqual(RpcClient("http://x", session=session).token_balance("o", "m"), 12)

    def test_rpc_error_raises(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"error": {"code": -1}}
        with self.assertRaises(RpcError):
            RpcClient("http://x", session=session).block_height()

    def test_build_transaction_rejects_bad_input(self):
        client = PumpPortalLocalClient(session=MagicMock())
        with self.assertRaises(Exception):
            client.build_transaction("pk", "hold", "mint", 1, True, 5, 0.001)

    def test_build_transaction_uses_documented_pool_values_and_binary_response(self):
        session = MagicMock()
        session.post.return_value.status_code = 200
        session.post.return_value.content = b"unsigned transaction"
        client = PumpPortalLocalClient(session=session)
        result = client.build_transaction("pk", "buy", "mint", 0.1, True, 10, 0.001, "launchlab")
        self.assertEqual(
            base64.b64decode(result),
            b"unsigned transaction",
        )
        self.assertEqual(session.post.call_args.kwargs["data"]["denominatedInSol"], "true")

    @staticmethod
    def _curve_bytes(complete=False, quote=None, vtok=1_000_000_000_000,
                     vquote=30_000_000_000, disc=None, cashback=0):
        import struct
        from solders.pubkey import Pubkey
        from chain import BONDING_CURVE_DISCRIMINATOR, WSOL_MINT
        return struct.pack(
            "<8s5QB32sBB32s",
            disc or BONDING_CURVE_DISCRIMINATOR, vtok, vquote, 5, 6, 7,
            complete, bytes(32), 0, cashback,
            bytes(Pubkey.from_string(quote or WSOL_MINT)),
        ) + bytes(40)

    def test_parse_bonding_curve_idl_layout(self):
        from chain import curve_price_sol
        curve = parse_bonding_curve(self._curve_bytes())
        self.assertFalse(curve["complete"])
        self.assertEqual(curve["real_quote_reserves"], 6)
        self.assertEqual(curve["quote_mint"], "So11111111111111111111111111111111111111112")
        self.assertAlmostEqual(curve_price_sol(curve, 6), 30.0 / 1_000_000.0)

    def test_parse_bonding_curve_fails_closed(self):
        from chain import curve_price_sol
        with self.assertRaises(ValueError):
            parse_bonding_curve(self._curve_bytes(disc=bytes(8)))
        with self.assertRaises(ValueError):
            parse_bonding_curve(b"short")
        with self.assertRaises(ValueError):
            parse_bonding_curve(self._curve_bytes(vtok=0))
        with self.assertRaises(ValueError):
            curve_price_sol(parse_bonding_curve(self._curve_bytes(complete=True)))
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        with self.assertRaises(ValueError):
            curve_price_sol(parse_bonding_curve(self._curve_bytes(quote=usdc)))

    def test_completed_curve_can_have_zero_virtual_reserves(self):
        from chain import curve_price_sol
        curve = parse_bonding_curve(
            self._curve_bytes(complete=True, vtok=0, vquote=0))

        self.assertTrue(curve["complete"])
        self.assertEqual(curve["virtual_token_reserves"], 0)
        self.assertEqual(curve["virtual_quote_reserves"], 0)
        with self.assertRaises(ValueError):
            curve_price_sol(curve)

    def _rpc_with(self, payload):
        session = MagicMock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"result": payload}
        return RpcClient("http://rpc.invalid", session=session)

    def test_transaction_sol_delta(self):
        tx = {"meta": {"err": None, "fee": 5000, "preBalances": [10_000, 1],
                       "postBalances": [4_000, 1]},
              "transaction": {"message": {"accountKeys": ["owner", "other"]}}}
        self.assertEqual(self._rpc_with(tx).transaction_sol_delta("sig", "owner"),
                         (-6_000, 5000))
        self.assertIsNone(self._rpc_with(None).transaction_sol_delta("sig", "owner"))

    def test_transaction_sol_delta_rejects_bad_metadata(self):
        base = {"meta": {"err": None, "fee": 5000, "preBalances": [10_000],
                         "postBalances": [4_000]},
                "transaction": {"message": {"accountKeys": ["owner"]}}}
        cases = []
        cases.append({**base, "meta": {**base["meta"], "err": {"x": 1}}})
        cases.append({**base, "meta": {**base["meta"], "postBalances": []}})
        cases.append({**base, "meta": {k: v for k, v in base["meta"].items() if k != "fee"}})
        cases.append(base)  # owner not in keys is tested below via a different owner
        for tx in cases[:3]:
            with self.assertRaises(RpcError):
                self._rpc_with(tx).transaction_sol_delta("sig", "owner")
        with self.assertRaises(RpcError):
            self._rpc_with(base).transaction_sol_delta("sig", "stranger")

    def test_account_data_decodes_and_handles_missing(self):
        raw = b"\x01\x02\x03"
        payload = {"value": {"data": [base64.b64encode(raw).decode(), "base64"]}}
        self.assertEqual(self._rpc_with(payload).account_data("a"), raw)
        self.assertIsNone(self._rpc_with({"value": None}).account_data("a"))
        with self.assertRaises(RpcError):
            self._rpc_with({"value": {"data": ["!!not-base64!!", "base64"]}}).account_data("a")

    def test_account_data_checks_expected_owner(self):
        raw = b"\x01\x02\x03"
        owner = "PumpFunProgram"
        value = {
            "owner": owner,
            "data": [base64.b64encode(raw).decode(), "base64"],
        }
        rpc = self._rpc_with({"value": value})
        self.assertEqual(rpc.account_data("a", expected_owner=owner), raw)
        with self.assertRaisesRegex(RpcError, "unexpected account owner"):
            rpc.account_data("a", expected_owner="OtherProgram")

    def test_build_transaction_sends_exact_decimal_amount(self):
        from decimal import Decimal
        session = MagicMock()
        session.post.return_value.status_code = 200
        session.post.return_value.content = b"tx"
        client = PumpPortalLocalClient(session=session)
        client.build_transaction("pk", "sell", "mint", Decimal("900.123456"), False, 15, 0.0001)
        self.assertEqual(session.post.call_args.kwargs["data"]["amount"], "900.123456")
        self.assertEqual(session.post.call_args.kwargs["data"]["denominatedInSol"], "false")

    def test_sol_balance_validates(self):
        self.assertEqual(self._rpc_with({"value": 42}).sol_balance("o"), 42)
        for bad in ({"value": -1}, {"value": True}, {"value": "1"}, None):
            with self.assertRaises(RpcError):
                self._rpc_with(bad).sol_balance("o")


if __name__ == "__main__":
    unittest.main()
