"""Ledger tests plus real, pinned-upstream integration tests (offline)."""
import datetime as dt
from decimal import Decimal
import json
import math
from pathlib import Path
import tempfile
import types
import unittest
from runner import Account, Upstream, events, metrics, number, simulate, ROOT, PINS


class Stub:
    """Accounting-only fixture. It does NOT establish upstream rule equivalence."""
    config = {"cash_buffer_pct": 10}

    def __init__(self):
        self.size = types.SimpleNamespace(to_eastern=lambda t: t,
            size_trade=self.size_trade, bracket_order=lambda x: dict(x))
        self.risk = types.SimpleNamespace(check=lambda *a, **k: {"pass": 1, "block": 0})

    def size_trade(self, p, c, budget, cash, price, today):
        q = max(0, min(math.floor(50 / (p["entry"] - p["stop"])),
                       math.floor(budget * .75 / price), math.floor((cash - budget * .1) / price)))
        return dict(p, qty=q, refusal=None if q else "no cash", max_loss=q * (p["entry"] - p["stop"]))


def plan(s="AAA", date="2026-11-12"):
    return {"symbol": s, "entry": 100, "stop": 98, "target": 104,
            "side": "buy", "setup": "orb", "date": date, "entry_time": "10:00 AM"}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.a = Account(Stub())
        self.t = dt.datetime(2026, 11, 12, 15)

    def test_continuous_account(self):
        self.assertTrue(self.a.enter(plan(), self.t, {"AAA": 100}))
        self.a.exit("AAA", 104, self.t, {"AAA": 104}, "target")
        self.assertEqual(self.a.cash, Decimal("2060.00"))
        self.a.enter(plan("BBB"), self.t, {"BBB": 100})
        self.a.exit("BBB", 98, self.t, {"BBB": 98}, "stop")
        self.assertEqual(self.a.cash, Decimal("2030.00"))
        self.assertEqual(sum(x["net_pnl"] for x in self.a.closed), 30)

    def test_overlapping_cash_and_zero_shares(self):
        self.a.enter(plan(), self.t, {"AAA": 100})
        self.a.enter(plan("BBB"), self.t, {"AAA": 100, "BBB": 100})
        self.assertEqual(self.a.cash, Decimal("200.00"))
        self.assertFalse(self.a.enter(plan("CCC"), self.t, {"AAA": 100, "BBB": 100, "CCC": 100}))
        self.assertEqual(self.a.value({"AAA": 100, "BBB": 100}), Decimal("2000.00"))

    def test_costs(self):
        a = Account(Stub(), "2")
        a.enter(plan(), self.t, {"AAA": 100})
        a.exit("AAA", 104, self.t, {"AAA": 104}, "target")
        self.assertEqual(a.cash, Decimal("2056.00"))
        self.assertEqual(metrics(a)["total_fees"], 4)

    def test_clock(self):
        self.a.observe(self.t, {})
        with self.assertRaises(ValueError):
            self.a.observe(self.t - dt.timedelta(seconds=1), {})

    def test_missing_marks(self):
        self.a.enter(plan(), self.t, {"AAA": 100})
        with self.assertRaises(ValueError):
            self.a.value({})

    def test_nonfinite_or_negative(self):
        for v in ["NaN", "Infinity", "-1", True]:
            with self.assertRaises(ValueError):
                number(v)

    def test_empty_metrics(self):
        m = metrics(self.a)
        self.assertIsNone(m["win_rate"])
        self.assertIsNone(m["profit_factor"])

    def test_delegated_risk_block(self):
        self.a.u.risk.check = lambda *a, **k: {"pass": 0, "block": 1}
        self.assertFalse(self.a.enter(plan(), self.t, {"AAA": 100}))
        self.assertEqual(self.a.cash, 2000)
        self.assertEqual(self.a.decisions[-1]["status"], "RISK_BLOCK")


class UpstreamIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / "trader-budget/size.py").exists():
            raise unittest.SkipTest("upstream checkout absent; integration must run in GitHub checkout")
        cls.u = Upstream()

    def test_original_sizer_and_risk(self):
        a = Account(self.u)
        now = self.u.size.parse_now("2026-11-12 10:00")
        self.assertTrue(a.enter(plan(), now, {"AAA": 100}))
        self.assertEqual(a.positions["AAA"]["qty"], 15)
        a.exit("AAA", 104, now, {"AAA": 104}, "target")
        self.assertEqual(a.cash, 2060)
        self.assertFalse(a.enter(plan(), now, {"AAA": 100}))
        self.assertEqual(a.decisions[-1]["status"], "RISK_BLOCK")

    def test_original_entry_window(self):
        a = Account(self.u)
        now = self.u.size.parse_now("2026-11-12 09:40")
        self.assertFalse(a.enter(plan(), now, {"AAA": 100}))

    def test_source_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            for path in PINS:
                dst = Path(d) / path
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes((ROOT / path).read_bytes())
            (Path(d) / "trader-budget/size.py").write_text("# changed\n")
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                Upstream(Path(d))

    def test_stop_first_replay_equivalence(self):
        bars = [{"o": 100, "h": 101, "l": 99, "c": 100} for _ in range(78)]
        bars[3] = {"o": 100, "h": 103, "l": 100, "c": 102}
        bars[4] = {"o": 102, "h": 105, "l": 99, "c": 103}
        rule = {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1}
        e = events(self.u, "2026-11-12", bars, "AAA", rule, 76)[0]
        self.assertEqual((e["outcome"], e["exit_slot"], e["exit_price"]), ("stop", 4, 100))
        account, candidates = simulate(self.u, {"AAA": {"2026-11-12": bars}}, ["AAA"], rule, 76, "0")
        self.assertEqual(account.cash, 1972)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(account.positions)

    def test_dst(self):
        before = self.u.size.to_eastern(dt.datetime(2026, 3, 6, 15))
        after = self.u.size.to_eastern(dt.datetime(2026, 3, 9, 15))
        self.assertEqual((before.hour, after.hour), (10, 11))


if __name__ == "__main__":
    unittest.main()
