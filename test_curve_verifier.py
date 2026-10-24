import struct
import unittest

from curve_verifier import (
    CURVE,
    DISC,
    MINT,
    PUMP,
    TOKEN_PROGRAMS,
    decode_accounts,
    derive_curve_address,
)


class TestCurveVerifier(unittest.TestCase):
    def setUp(self):
        self.mint_owner = next(iter(TOKEN_PROGRAMS))
        self.mint_data = bytearray(82)
        struct.pack_into("<Q", self.mint_data, 36, 1_000_000_000_000_000)
        self.mint_data[44] = 6

        self.curve_data = bytearray(166)
        self.curve_data[:8] = DISC

        values = [
            1_073_000_000_000_000,
            30_000_000_022,
            793_100_000_000_000,
            22,
            1_000_000_000_000_000,
        ]
        for i, value in enumerate(values):
            struct.pack_into("<Q", self.curve_data, 8 + 8 * i, value)

        self.curve_data[48] = 0
        self.mint_account = {"owner": self.mint_owner}
        self.curve_account = {"owner": PUMP}

    def decode(self):
        return decode_accounts(
            self.mint_account,
            bytes(self.mint_data),
            self.curve_account,
            bytes(self.curve_data),
        )

    def test_valid_accounts_decode(self):
        result = self.decode()
        self.assertEqual(result["decimals"], 6)
        self.assertEqual(result["mint_supply"], 1_000_000_000)
        self.assertEqual(result["complete"], 0)

    def test_quote_reserve_names_match_current_idl(self):
        result = self.decode()
        self.assertIn("virtual_quote_reserves", result)
        self.assertIn("real_quote_reserves", result)
        self.assertNotIn("virtual_sol_reserves", result)
        self.assertNotIn("real_sol_reserves", result)

    def test_liquidity_is_not_inferred(self):
        self.assertIsNone(self.decode()["liquidity_usd"])

    def test_derived_curve_matches_configured_curve(self):
        self.assertEqual(derive_curve_address(MINT), CURVE)

    def test_different_mint_derives_different_curve(self):
        other_mint = "So11111111111111111111111111111111111111112"
        self.assertNotEqual(derive_curve_address(other_mint), CURVE)

    def test_wrong_curve_owner_rejected(self):
        self.curve_account["owner"] = "wrong-owner"
        with self.assertRaisesRegex(RuntimeError, "owner mismatch"):
            self.decode()

    def test_wrong_discriminator_rejected(self):
        self.curve_data[0] ^= 0xFF
        with self.assertRaisesRegex(RuntimeError, "layout validation"):
            self.decode()

    def test_truncated_curve_rejected(self):
        self.curve_data = self.curve_data[:48]
        with self.assertRaisesRegex(RuntimeError, "layout validation"):
            self.decode()

    def test_invalid_complete_flag_rejected(self):
        self.curve_data[48] = 2
        with self.assertRaisesRegex(RuntimeError, "complete flag"):
            self.decode()

    def test_unrecognized_mint_owner_rejected(self):
        self.mint_account["owner"] = "wrong-token-program"
        with self.assertRaisesRegex(RuntimeError, "token program"):
            self.decode()

    def test_short_mint_rejected(self):
        self.mint_data = self.mint_data[:44]
        with self.assertRaisesRegex(RuntimeError, "Mint data too short"):
            self.decode()


if __name__ == "__main__":
    unittest.main()
