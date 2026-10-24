import base64
import struct
import unittest
from unittest.mock import patch
from solders.pubkey import Pubkey

from curve_verifier import (
    CURVE,
    DISC,
    MINT,
    PUMP,
    TOKEN_PROGRAMS,
    decode_accounts,
    decode_token_account,
    derive_curve_address,
    derive_curve_token_account,
    read_multiple_accounts,
    verify,
)


class TestCurveVerifier(unittest.TestCase):
    def setUp(self):
        self.mint_owner = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
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
        self.assertEqual(result["real_token_reserves_raw"], 793_100_000_000_000)

    def test_quote_reserve_names_match_current_idl(self):
        result = self.decode()
        self.assertIn("virtual_quote_reserves", result)
        self.assertIn("real_quote_reserves", result)
        self.assertNotIn("virtual_sol_reserves", result)
        self.assertNotIn("real_sol_reserves", result)

    def test_liquidity_is_not_inferred(self):
        self.assertIsNone(self.decode()["liquidity_usd"])

    def test_real_reserves_and_total_supply_are_separate_fields(self):
        result = self.decode()
        self.assertEqual(result["real_token_reserves_raw"], 793_100_000_000_000)
        self.assertEqual(result["curve_supply_raw"], 1_000_000_000_000_000)
        self.assertNotEqual(result["real_token_reserves_raw"], result["curve_supply_raw"])

    def test_derived_curve_matches_configured_curve(self):
        self.assertEqual(derive_curve_address(MINT), CURVE)

    def test_different_mint_derives_different_curve(self):
        other_mint = "So11111111111111111111111111111111111111112"
        self.assertNotEqual(derive_curve_address(other_mint), CURVE)

    def test_derive_curve_token_account(self):
        ata = derive_curve_token_account(CURVE, MINT, self.mint_owner)
        self.assertEqual(ata, "A26uPpMb9JaWdhrbVmW5NnEjUXniwRPXE7WHvRRBQNkH")

    def _token_account_data(self, mint=MINT, authority=CURVE, amount=10):
        data = bytearray(72)
        data[:32] = bytes(Pubkey.from_string(mint))
        data[32:64] = bytes(Pubkey.from_string(authority))
        struct.pack_into("<Q", data, 64, amount)
        return bytes(data)

    def test_token_account_decodes_balance_and_validates_associations(self):
        account = {"owner": self.mint_owner}
        decoded = decode_token_account(account, self._token_account_data(amount=123), MINT, CURVE)
        self.assertEqual(decoded["amount_raw"], 123)

    def test_token_account_owner_must_match_mint_program(self):
        account = {"owner": next(iter(TOKEN_PROGRAMS))}
        wrong_program = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        if account["owner"] == wrong_program:
            wrong_program = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
        with self.assertRaisesRegex(RuntimeError, "does not match mint token program"):
            decode_token_account(
                account, self._token_account_data(), MINT, CURVE,
                expected_token_program=wrong_program,
            )

    def test_wrong_token_account_mint_rejected(self):
        account = {"owner": self.mint_owner}
        wrong_mint = "So11111111111111111111111111111111111111112"
        with self.assertRaisesRegex(RuntimeError, "mint mismatch"):
            decode_token_account(account, self._token_account_data(mint=wrong_mint), MINT, CURVE)

    def test_wrong_token_account_authority_rejected(self):
        account = {"owner": self.mint_owner}
        wrong_authority = "11111111111111111111111111111111"
        with self.assertRaisesRegex(RuntimeError, "authority mismatch"):
            decode_token_account(account, self._token_account_data(authority=wrong_authority), MINT, CURVE)

    def test_short_token_account_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "too short"):
            decode_token_account({"owner": self.mint_owner}, b"x" * 71, MINT, CURVE)

    def test_unrecognized_token_account_owner_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "recognized token program"):
            decode_token_account({"owner": "wrong"}, b"x" * 72, MINT, CURVE)

    @patch("curve_verifier.requests.post")
    def test_get_multiple_accounts_returns_same_snapshot_slot(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "result": {
                "context": {"slot": 12345},
                "value": [
                    {"owner": self.mint_owner, "data": [base64.b64encode(b"mint").decode(), "base64"]},
                    {"owner": PUMP, "data": [base64.b64encode(b"curve").decode(), "base64"]},
                ],
            }
        }
        slot, accounts = read_multiple_accounts(["mint-address", "curve-address"])
        self.assertEqual(slot, 12345)
        self.assertEqual([raw for _account, raw in accounts], [b"mint", b"curve"])

    @patch("curve_verifier.requests.post")
    def test_get_multiple_accounts_missing_account_fails_closed(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "result": {"context": {"slot": 12345}, "value": [None]}
        }
        with self.assertRaisesRegex(RuntimeError, "Account not found"):
            read_multiple_accounts(["missing"])


    @patch("curve_verifier.requests.post")
    def test_mint_owner_change_between_rpc_snapshots_fails_closed(self, mock_post):
        wrong_owner = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

        first_response = unittest.mock.Mock()
        first_response.raise_for_status.return_value = None
        first_response.json.return_value = {
            "result": {"value": {"owner": self.mint_owner}}
        }

        snapshot_response = unittest.mock.Mock()
        snapshot_response.raise_for_status.return_value = None
        snapshot_response.json.return_value = {
            "result": {
                "context": {"slot": 12346},
                "value": [
                    {
                        "owner": wrong_owner,
                        "data": [base64.b64encode(bytes(self.mint_data)).decode(), "base64"],
                    },
                    {
                        "owner": PUMP,
                        "data": [base64.b64encode(bytes(self.curve_data)).decode(), "base64"],
                    },
                    {
                        "owner": self.mint_owner,
                        "data": [
                            base64.b64encode(self._token_account_data()).decode(),
                            "base64",
                        ],
                    },
                ],
            }
        }

        mock_post.side_effect = [first_response, snapshot_response]

        with self.assertRaisesRegex(
            RuntimeError, "Mint token program changed between RPC snapshots"
        ):
            verify()

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
