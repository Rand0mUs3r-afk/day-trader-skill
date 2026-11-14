"""Offline integration tests against actual, hash-pinned upstream functions.

Run: python -m unittest discover -s evaluation -p 'test_account_bridge.py' -v
No sizing, risk, approval, or accounting function is mocked. These are
synthetic accounting fixtures, not historical performance evidence.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import account_bridge as bridge
import ledger


class OriginalWorkflowAccountIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.account = Path(folder.name) / "account.json"
        ledger.initialize(self.account)
        self.at = "2026-11-12T15:00:00Z"
        self.plan = {"symbol": "ABC", "date": "2026-11-12", "side": "buy",
                     "setup": "flag", "entry": 100, "stop": 95, "target": 105,
                     "entry_time": "10:00 AM", "flat_by": "15:55"}
        self.quotes = {"as_of": self.at, "prices": {"ABC": 100}}

    def proposal(self):
        result = bridge.prepare_plan(self.account, self.plan, self.quotes, self.at)
        self.assertTrue(result["ready"], result)
        return result

    def event(self, **changes):
        event = {"id": "entry", "at": self.at, "symbol": "ABC", "side": "buy",
                 "qty": 10, "price": 100, "fee": 0}
        event.update(changes)
        return event

    def assert_refused_without_write(self, proposal, reply="yes", event=None):
        before = self.account.read_bytes()
        with self.assertRaises(ValueError):
            bridge.approve_fill(self.account, proposal, reply, event or self.event())
        self.assertEqual(self.account.read_bytes(), before)
        self.assertFalse(self.account.with_name("account.json.lock").exists())

    def test_uses_actual_original_sizing_and_risk(self):
        proposal = self.proposal()
        size = bridge._module("oracle_size", "trader-budget/size.py")
        risk = bridge._module("oracle_risk", "trader-risk/check.py")
        config = json.loads(bridge._pinned("trader-budget/config.example.json"))
        config["budget"] = 2000
        expected = size.size_trade(self.plan, config, budget=2000, cash=2000,
                                   price=100, today="2026-11-12")
        order = size.bracket_order(expected)
        self.assertEqual(proposal["sizing"], expected)
        self.assertEqual(proposal["order"], order)
        verdict = risk.check([order], config, portfolio=ledger.snapshot(self.account)["portfolio"],
            prices={"ABC": 100}, state={"orders": []}, now=bridge._clock(self.at),
            today="2026-11-12", price_times={"ABC": bridge._clock(self.at)})
        verdict["date"] = "2026-11-12"
        self.assertEqual(proposal["risk"], verdict)
        self.assertEqual(proposal["sizing"]["budget"], 2000)
        self.assertEqual(proposal["order"]["plan"]["setup"], "flag")

    def test_preparing_never_changes_account(self):
        before = self.account.read_bytes()
        self.proposal()
        self.assertEqual(self.account.read_bytes(), before)

    def test_fill_matches_original_accounting_with_explicit_fee(self):
        proposal = self.proposal()
        ticket = ledger._ticket()
        oracle = self.account.with_name("oracle.json")
        expected = ticket.apply_fill(str(oracle), {"budget": 2000},
                                     "ABC", "buy", 10, 100, "2026-11-12")
        expected["cash"] -= 1
        result = bridge.approve_fill(self.account, proposal, "yes", self.event(fee=1))
        self.assertEqual(result["portfolio"], expected)
        self.assertEqual(result["portfolio"]["cash"], 999)
        self.assertEqual(len(result["fills"]), 1)
        self.assertEqual(len(result["workflow_audit"]), 1)
        self.assertEqual([r["status"] for r in result["broker_state"]["orders"]],
                         ["approved", "placed", "filled"])

    def test_original_risk_history_counts_entry_once(self):
        result = bridge.approve_fill(self.account, self.proposal(), "yes", self.event())
        risk = bridge._module("counting_risk", "trader-risk/check.py")
        placed = risk.placed_today(result["broker_state"], "2026-11-12")
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0]["kind"], "entry")
        self.assertEqual(placed[0]["max_loss"], 50)
        again = bridge.prepare_plan(self.account, self.plan, self.quotes, self.at)
        self.assertFalse(again["ready"])

    def test_exit_after_entry_window_uses_original_risk_exception(self):
        bridge.approve_fill(self.account, self.proposal(), "yes", self.event())
        at = "2026-11-12T21:05:00Z"
        quotes = {"as_of": at, "prices": {"ABC": 110}}
        order = {"symbol": "ABC", "side": "sell", "qty": "10", "type": "market",
                 "time_in_force": "day", "why": "original flatten ticket shape"}
        proposal = bridge.prepare_order(self.account, order, quotes, at)
        self.assertTrue(proposal["ready"], proposal)
        self.assertEqual(proposal["risk"]["results"][0]["kind"], "exit")
        result = bridge.approve_fill(self.account, proposal, "yes",
            self.event(id="exit", at=at, side="sell", price=110))
        self.assertEqual(result["portfolio"], {"cash": 2100.0, "positions": []})

    def test_account_equity_carries_forward_without_reset(self):
        bridge.approve_fill(self.account, self.proposal(), "yes", self.event())
        next_at = "2026-11-13T15:00:00Z"
        quotes = {"as_of": next_at, "prices": {"ABC": 110}}
        context = bridge._context(self.account, quotes, next_at)
        self.assertEqual(context[4]["budget"], 2100)
        self.assertEqual(context[4]["cash"], 1000)
        self.assertEqual(context[2]["budget"], 2100)
        self.assertEqual(len(context[5]["orders"]), 3)
        self.assertEqual(ledger.snapshot(self.account)["initial_cash"], 2000)

    def test_rejects_conditional_and_batch_approvals(self):
        proposal = self.proposal()
        for reply in ("ok", "yes to all", "yes if it drops", "yes?"):
            with self.subTest(reply=reply):
                self.assert_refused_without_write(proposal, reply)

    def test_rejects_changed_exact_ticket(self):
        proposal = self.proposal()
        proposal["order"]["qty"] = "9"
        self.assert_refused_without_write(proposal, event=self.event(qty=9))

    def test_rejects_account_changed_after_check(self):
        proposal = self.proposal()
        ledger.record_fill(self.account, self.event(id="other", symbol="XYZ", qty=1))
        self.assert_refused_without_write(proposal)

    def test_rejects_partial_fill_and_symbol_or_side_mismatch(self):
        proposal = self.proposal()
        for changes in ({"qty": 9}, {"symbol": "XYZ"}, {"side": "sell"}):
            with self.subTest(changes=changes):
                self.assert_refused_without_write(proposal, event=self.event(**changes))

    def test_rejects_fill_before_proposal(self):
        self.assert_refused_without_write(self.proposal(),
            event=self.event(at="2026-11-12T14:59:59Z"))

    def test_rejects_previous_day_risk_check(self):
        self.assert_refused_without_write(self.proposal(),
            event=self.event(at="2026-11-13T15:00:00Z"))

    def test_rejects_missing_explicit_fee(self):
        event = self.event()
        del event["fee"]
        self.assert_refused_without_write(self.proposal(), event=event)

    def test_failed_fee_validation_is_atomic(self):
        self.assert_refused_without_write(self.proposal(), event=self.event(fee=1001))

    def test_final_write_failure_preserves_account_and_history(self):
        proposal = self.proposal()
        before = self.account.read_bytes()
        save = ledger._save
        def fail_final(path, data):
            if Path(path) == self.account:
                raise OSError("simulated final write error")
            return save(path, data)
        with patch.object(ledger, "_save", side_effect=fail_final):
            with self.assertRaises(OSError):
                bridge.approve_fill(self.account, proposal, "yes", self.event())
        self.assertEqual(self.account.read_bytes(), before)
        result = bridge.approve_fill(self.account, proposal, "yes", self.event())
        self.assertEqual(len(result["fills"]), 1)
        self.assertEqual(len(result["broker_state"]["orders"]), 3)

    def test_future_quote_is_not_used(self):
        quotes = copy.deepcopy(self.quotes)
        quotes["as_of"] = "2026-11-12T15:00:01Z"
        before = self.account.read_bytes()
        with self.assertRaisesRegex(ValueError, "future-dated"):
            bridge.prepare_plan(self.account, self.plan, quotes, self.at)
        self.assertEqual(self.account.read_bytes(), before)

    def test_original_sizer_refusal_is_not_overridden(self):
        quotes = copy.deepcopy(self.quotes)
        quotes["prices"]["ABC"] = 101
        proposal = bridge.prepare_plan(self.account, self.plan, quotes, self.at)
        self.assertFalse(proposal["ready"])
        self.assertEqual(proposal["status"], "SIZE_BLOCK")
        self.assertIsNotNone(proposal["sizing"]["refusal"])

    def test_original_entry_window_is_not_overridden(self):
        at = "2026-11-12T14:00:00Z"
        proposal = bridge.prepare_plan(self.account, self.plan,
            {"as_of": at, "prices": {"ABC": 100}}, at)
        self.assertFalse(proposal["ready"])
        self.assertEqual(proposal["risk"]["block"], 1)

    def test_changed_original_source_is_refused_before_import(self):
        root = self.account.parent / "changed-root"
        file = root / "trader-budget/size.py"
        file.parent.mkdir(parents=True)
        file.write_text("raise RuntimeError('must not execute')", encoding="utf-8")
        with patch.object(bridge, "ROOT", root):
            with self.assertRaisesRegex(ValueError, "upstream source changed"):
                bridge.prepare_plan(self.account, self.plan, self.quotes, self.at)


if __name__ == "__main__":
    unittest.main()
