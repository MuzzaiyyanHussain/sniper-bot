"""Regression tests for the Pump.fun scanner and dry-run safety helpers."""
import json
import math
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from bot import CompoundingBot, Config, Journal, PriceOracle, StrategyCalc, parse_bool_env
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
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.liquidity_usd, 0.0)

        ok, reason = listener._passes_filters(candidate)
        self.assertFalse(ok)
        self.assertIn("low liquidity", reason)

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


if __name__ == "__main__":
    unittest.main()
