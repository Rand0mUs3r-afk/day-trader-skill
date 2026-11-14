"""Offline tests using the actual original ticket.py accounting module.

Run from the repository root:
    python -m unittest discover -s evaluation -p 'test_ledger.py' -v
Missing or changed upstream code is an error, never a skipped integration test.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import ledger


class OriginalAccountingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ticket = ledger._ticket()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.account = Path(self.folder.name) / "account.json"
        ledger.initialize(self.account)

    def event(self, identifier="entry", side="buy", qty=3, price=100,
              fee=0, at="2026-11-12T15:00:00Z"):
        return dict(id=identifier, at=at, symbol="ABC", side=side,
                    qty=qty, price=price, fee=fee)

    def test_initial_balance(self):
        self.assertEqual(ledger.snapshot(self.account)["portfolio"],
                         {"cash": 2000.0, "positions": []})

    def test_matches_original_fill_accounting(self):
        oracle = Path(self.folder.name) / "original-portfolio.json"
        expected = self.ticket.apply_fill(str(oracle), {"budget": 2000},
                                         "ABC", "buy", 3, 100, "2026-11-12")
        actual = ledger.record_fill(self.account, self.event())
        self.assertEqual(actual["portfolio"], expected)

    def test_persists_across_days(self):
        ledger.record_fill(self.account, self.event())
        ledger.record_fill(self.account, self.event("exit", "sell", price=110,
                           at="2026-11-13T15:00:00Z"))
        reopened = ledger.snapshot(self.account)
        self.assertEqual(reopened["portfolio"], {"cash": 2030.0, "positions": []})
        self.assertEqual(len(reopened["fills"]), 2)

    def test_original_fifo_partial_sell(self):
        ledger.record_fill(self.account, self.event(qty=2))
        ledger.record_fill(self.account, self.event("second", qty=3, price=110,
                           at="2026-11-13T15:00:00Z"))
        result = ledger.record_fill(self.account, self.event("exit", "sell", qty=3,
                                   price=120, at="2026-11-13T15:05:00Z"))
        position = result["portfolio"]["positions"][0]
        self.assertEqual(result["portfolio"]["cash"], 1830.0)
        self.assertEqual(position["shares"], 2)
        self.assertEqual(len(position["lots"]), 1)
        self.assertEqual(position["lots"][0]["date"], "2026-11-13")
        self.assertEqual(position["lots"][0]["cost"], 220)

    def test_explicit_fees(self):
        ledger.record_fill(self.account, self.event(fee=1))
        result = ledger.record_fill(self.account, self.event("exit", "sell", price=110, fee=1))
        self.assertEqual(result["portfolio"]["cash"], 2028.0)

    def test_duplicate_fill_does_not_debit_twice(self):
        ledger.record_fill(self.account, self.event())
        result = ledger.record_fill(self.account, self.event(price="100.00", fee="0.00"))
        self.assertEqual(len(result["fills"]), 1)
        self.assertEqual(result["portfolio"]["cash"], 1700.0)

    def test_reused_id_with_different_content_is_rejected(self):
        ledger.record_fill(self.account, self.event())
        before = self.account.read_bytes()
        with self.assertRaises(ValueError):
            ledger.record_fill(self.account, self.event(price=101))
        self.assertEqual(before, self.account.read_bytes())

    def test_existing_account_cannot_be_reset(self):
        with self.assertRaises(FileExistsError):
            ledger.initialize(self.account)

    def test_no_borrowed_cash(self):
        before = self.account.read_bytes()
        with self.assertRaises(ValueError):
            ledger.record_fill(self.account, self.event(qty=21))
        self.assertEqual(before, self.account.read_bytes())

    def test_original_oversell_rejection(self):
        before = self.account.read_bytes()
        with self.assertRaisesRegex(Exception, "cannot record selling"):
            ledger.record_fill(self.account, self.event(side="sell"))
        self.assertEqual(before, self.account.read_bytes())

    def test_missing_position_mark_is_rejected(self):
        ledger.record_fill(self.account, self.event())
        with self.assertRaises(ValueError):
            ledger.sizing_context(self.account, {})

    def test_current_equity_and_cash_for_original_sizer(self):
        ledger.record_fill(self.account, self.event())
        context = ledger.sizing_context(self.account, {"ABC": 110.1234})
        self.assertEqual(context["cash"], 1700.0)
        self.assertAlmostEqual(context["budget"], 2030.3702)
        context["portfolio"]["cash"] = 0
        self.assertEqual(ledger.snapshot(self.account)["portfolio"]["cash"], 1700.0)

    def test_earlier_fill_cannot_be_appended(self):
        ledger.record_fill(self.account, self.event())
        with self.assertRaises(ValueError):
            ledger.record_fill(self.account, self.event("earlier", at="2026-11-11T15:00:00Z"))

    def test_explicit_timezone_and_fee_required(self):
        with self.assertRaises(ValueError):
            ledger.record_fill(self.account, self.event(at="2026-11-12T10:00:00"))
        event = self.event()
        del event["fee"]
        with self.assertRaises(ValueError):
            ledger.record_fill(self.account, event)

    def test_invalid_amounts_do_not_change_account(self):
        before = self.account.read_bytes()
        for value in ["NaN", "Infinity", -1, True, 0]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ledger.record_fill(self.account, self.event(price=value))
                self.assertEqual(before, self.account.read_bytes())

    def test_failed_final_write_preserves_previous_account(self):
        before = self.account.read_bytes()
        real_save = ledger._save
        def fail_final(path, data):
            if Path(path) == self.account:
                raise OSError("simulated disk error")
            return real_save(path, data)
        with patch.object(ledger, "_save", side_effect=fail_final):
            with self.assertRaises(OSError):
                ledger.record_fill(self.account, self.event())
        self.assertEqual(before, self.account.read_bytes())
        ledger.record_fill(self.account, self.event())

    def test_lock_prevents_concurrent_writer(self):
        lock = self.account.with_name(self.account.name + ".lock")
        lock.touch()
        with self.assertRaises(FileExistsError):
            ledger.record_fill(self.account, self.event())
        self.assertTrue(lock.exists())

    def test_changed_upstream_is_not_executed(self):
        root = Path(self.folder.name) / "changed-root"
        path = root / "trader-broker/ticket.py"
        path.parent.mkdir(parents=True)
        path.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
        with patch.object(ledger, "ROOT", root):
            with self.assertRaisesRegex(ValueError, "original ticket.py changed"):
                ledger._ticket()

    def test_cli_reopens_same_persisted_account(self):
        ledger.record_fill(self.account, self.event())
        result = subprocess.run([sys.executable, str(Path(ledger.__file__).resolve()),
                                 "show", str(self.account)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["portfolio"]["cash"], 1700.0)


if __name__ == "__main__":
    unittest.main()
