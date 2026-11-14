#!/usr/bin/env python3
"""Manual, credential-free, long-only upstream ORB account evaluation."""
import argparse
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE = "3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd"
PINS = {
    "trader-budget/size.py": "1c9ba4749ed6fc178ee836af4381472ac59eae77",
    "trader-risk/check.py": "2e51047392a491b30d3d49bcb504cd41bcee50fa",
    "trader-timemachine/timemachine.py": "026cc782034f4489af65b4f2d80d5edbb0c53ebc",
    "trader-budget/config.example.json": "f02db426b5c96b2a6d43177e7f17fbcd3664226d",
}


def number(value, positive=False):
    if isinstance(value, bool):
        raise ValueError("boolean is not a price or amount")
    n = Decimal(str(value))
    if not n.is_finite() or (n <= 0 if positive else n < 0):
        raise ValueError("amount must be finite and %s" % ("positive" if positive else "nonnegative"))
    return n


def money(value):
    return number(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


class Upstream:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        for name, expected in PINS.items():
            blob = (self.root / name).read_bytes()
            actual = hashlib.sha1(b"blob " + str(len(blob)).encode() + b"\0" + blob).hexdigest()
            if actual != expected:
                raise ValueError("upstream fingerprint mismatch: " + name)
        self.size = self.load("budget", "trader-budget/size.py")
        self.risk = self.load("risk", "trader-risk/check.py")
        self.tm = self.load("tm", "trader-timemachine/timemachine.py")
        self.config = json.loads((self.root / "trader-budget/config.example.json").read_text())
        self.config["budget"] = 2000

    def load(self, name, path):
        spec = importlib.util.spec_from_file_location("evaluation_upstream_" + name, self.root / path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


class Account:
    def __init__(self, upstream, fee="0"):
        self.u = upstream
        self.cfg = dict(upstream.config)
        self.cash = Decimal("2000.00")
        self.fee = money(fee)
        self.positions = {}
        self.state = {"orders": []}
        self.fills, self.decisions, self.equity, self.closed = [], [], [], []
        self.clock = None

    def tick(self, now):
        if now.tzinfo is not None or (self.clock is not None and now < self.clock):
            raise ValueError("historical clock must be monotonic naive UTC")
        self.clock = now

    def value(self, marks):
        missing = set(self.positions) - set(marks)
        if missing:
            raise ValueError("missing contemporaneous marks: " + str(sorted(missing)))
        return self.cash + sum((number(marks[s], True) * p["qty"] for s, p in self.positions.items()), Decimal(0))

    def observe(self, now, marks):
        self.tick(now)
        self.equity.append({"at": now.isoformat() + "Z", "cash": float(self.cash),
                            "equity": float(self.value(marks).quantize(Decimal("0.01"))),
                            "marks": {s: float(number(marks[s], True)) for s in self.positions}})

    def portfolio(self):
        return {"cash": float(self.cash), "positions": [
            {"ticker": s, "shares": p["qty"]} for s, p in self.positions.items()]}

    def check(self, order, now, marks):
        return self.u.risk.check([order], self.cfg, portfolio=self.portfolio(),
            prices={s: float(number(v, True)) for s, v in marks.items()}, state=self.state,
            now=now, today=self.u.size.to_eastern(now).date().isoformat(),
            price_times={s: now for s in marks})

    def record(self, symbol, side, qty, price, now, max_loss=0):
        at = now.isoformat() + "Z"
        self.state["orders"].append({"symbol": symbol, "side": side, "qty": str(qty),
            "date": self.u.size.to_eastern(now).date().isoformat(), "at": at,
            "status": "placed", "kind": "entry" if side == "buy" else "exit", "max_loss": max_loss})
        self.fills.append({"at": at, "symbol": symbol, "side": side, "qty": qty,
                           "price": float(price), "fee": float(self.fee), "cash_after": float(self.cash)})

    def enter(self, plan, now, marks):
        self.tick(now)
        symbol = plan["symbol"]
        equity = self.value(marks)
        decision = {"at": now.isoformat() + "Z", "plan": plan}
        self.decisions.append(decision)
        sized = self.u.size.size_trade(plan, self.cfg, budget=float(equity),
            cash=float(self.cash), price=plan["entry"], today=self.u.size.to_eastern(now).date().isoformat())
        decision["sizing"] = sized
        if sized["refusal"] or sized["qty"] <= 0:
            decision["status"] = "SIZE_BLOCK"
            return False
        order = self.u.size.bracket_order(sized)
        quotes = dict(marks, **{symbol: sized["entry"]})
        verdict = self.check(order, now, quotes)
        decision["risk"] = verdict
        if verdict["pass"] != 1 or verdict["block"]:
            decision["status"] = "RISK_BLOCK"
            return False
        if symbol in self.positions:
            raise ValueError("unsupported addition to an existing position")
        price, qty = money(sized["entry"]), int(sized["qty"])
        remaining = self.cash - qty * price - self.fee
        buffer = equity * number(self.cfg["cash_buffer_pct"]) / 100
        if remaining < 0 or (self.fee > 0 and remaining < buffer):
            decision["status"] = "COST_BUFFER_BLOCK"
            return False
        self.cash = remaining
        self.positions[symbol] = {"qty": qty, "entry": price, "entry_fee": self.fee}
        self.record(symbol, "buy", qty, price, now, sized["max_loss"])
        decision["status"] = "SIMULATED_FILL"
        return True

    def exit(self, symbol, price, now, marks, reason):
        self.tick(now)
        if symbol not in self.positions:
            return
        price = money(price)
        p = self.positions[symbol]
        order = {"symbol": symbol, "side": "sell", "qty": str(p["qty"]),
                 "type": "market", "time_in_force": "day"}
        verdict = self.check(order, now, dict(marks, **{symbol: float(price)}))
        if verdict["pass"] != 1 or verdict["block"]:
            raise ValueError("upstream blocked simulated exit: " + json.dumps(verdict))
        if self.cash + price * p["qty"] < self.fee:
            raise ValueError("exit fee would require margin")
        self.cash += price * p["qty"] - self.fee
        pnl = (price - p["entry"]) * p["qty"] - p["entry_fee"] - self.fee
        self.closed.append({"symbol": symbol, "at": now.isoformat() + "Z", "reason": reason,
                            "qty": p["qty"], "net_pnl": float(pnl)})
        del self.positions[symbol]
        self.record(symbol, "sell", p["qty"], price, now)


def events(upstream, date, bars, symbol, rule, last):
    replay = upstream.tm.history_replay([(date, bars)], rule, last, side="buy")
    result = []
    for trade in replay["trades"]:
        k = (upstream.tm.parse_clock(trade["time"], "entry") - upstream.tm.OPEN_MIN) // 5 - 1
        exit_slot, outcome, price = last, "close", bars[last]["c"]
        for i in range(k + 1, last + 1):
            if bars[i]["l"] <= trade["stop"]:
                exit_slot, outcome, price = i, "stop", trade["stop"]
                break
            if bars[i]["h"] >= trade["target"]:
                exit_slot, outcome, price = i, "target", trade["target"]
                break
        r = round((price - trade["entry"]) / (trade["entry"] - trade["stop"]), 2)
        if outcome != trade["outcome"] or r != trade["R"]:
            raise ValueError("upstream replay/event reconstruction disagreement")
        plan = {"symbol": symbol, "side": "buy", "date": date, "setup": "orb",
                "entry_time": trade["time"], "entry": trade["entry"],
                "stop": trade["stop"], "target": trade["target"]}
        result.append({"entry_slot": k, "exit_slot": exit_slot, "exit_price": price,
                       "outcome": outcome, "plan": plan, "upstream": trade})
    return result


def simulate(upstream, sessions, symbols, rule, last, fee, account=None, candidates=None):
    account = Account(upstream, fee) if account is None else account
    candidates = [] if candidates is None else candidates
    for date in sorted({d for days in sessions.values() for d in days}):
        daily = {s: sessions[s][date] for s in symbols if date in sessions[s]}
        schedule = []
        for rank, s in enumerate(symbols):
            if s not in daily:
                continue
            for event in events(upstream, date, daily[s], s, rule, last):
                candidates.append(dict(event, symbol=s, date=date))
                schedule.append((rank, s, event))
        start = upstream.tm.eastern_to_utc(dt.datetime.fromisoformat(date + "T09:30:00"))
        account.observe(start, {})
        for slot in range(last + 1):
            clock = upstream.tm.clock24(upstream.tm.OPEN_MIN + 5 * (slot + 1))
            now = upstream.tm.eastern_to_utc(dt.datetime.fromisoformat(date + "T" + clock))
            marks = {s: rows[slot]["c"] for s, rows in daily.items()}
            for _, s, e in schedule:
                if e["exit_slot"] == slot:
                    account.exit(s, e["exit_price"], now, marks, e["outcome"])
            for _, s, e in schedule:
                if e["entry_slot"] == slot:
                    accepted = account.enter(e["plan"], now, marks)
                    if accepted and e["exit_slot"] == slot:
                        account.exit(s, e["exit_price"], now, marks, e["outcome"])
            account.observe(now, marks)
        if account.positions:
            raise ValueError("positions remained open after cutoff")
    return account, candidates


def metrics(account):
    pnl = [t["net_pnl"] for t in account.closed]
    wins, losses = [p for p in pnl if p > 0], [p for p in pnl if p < 0]
    peak, drawdown = 2000.0, 0.0
    for point in account.equity:
        v = point["equity"]
        peak = max(peak, v)
        drawdown = max(drawdown, (peak - v) / peak)
    return {"starting_cash": 2000, "ending_cash": float(account.cash),
            "return_pct": (float(account.cash) / 2000 - 1) * 100,
            "max_observed_drawdown_pct": drawdown * 100,
            "drawdown_frequency": "five-minute bar closes after fills, not intrabar/tick drawdown",
            "trades": len(pnl), "win_rate": len(wins) / len(pnl) if pnl else None,
            "average_win": sum(wins) / len(wins) if wins else None,
            "average_loss": sum(losses) / len(losses) if losses else None,
            "profit_factor": sum(wins) / -sum(losses) if losses else None,
            "profit_factor_note": "undefined without losing trades" if not losses else None,
            "net_pnl": sum(pnl), "total_fees": sum(f["fee"] for f in account.fills)}


def execute(args, upstream, out):
    if not args.accept_limitations:
        raise ValueError("explicit --accept-limitations required; read evaluation/README.md")
    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    if not symbols or len(symbols) > 20 or len(set(symbols)) != len(symbols):
        raise ValueError("provide 1..20 unique comma-separated tickers in tie-break order")
    if any(not upstream.risk.TICKER.fullmatch(s) for s in symbols):
        raise ValueError("only upstream-supported US stock/ETF ticker syntax is allowed")
    if args.or_minutes < 5 or args.or_minutes >= 390 or args.or_minutes % 5:
        raise ValueError("opening range must be a multiple of five in [5,385]")
    target_r = float(number(args.target_r, True))
    if not math.isfinite(target_r):
        raise ValueError("target R is not a finite float")
    fee = money(args.fee_per_fill)
    rule = {"setup": "orb", "or_minutes": args.or_minutes,
            "stop_rule": args.stop_rule, "target_r": target_r}
    cutoff = upstream.tm.parse_clock(upstream.config["flat_by"], "flat_by")
    last = (cutoff - upstream.tm.OPEN_MIN) // 5 - 1
    if (cutoff - upstream.tm.OPEN_MIN) % 5 or not 0 <= last < 78:
        raise ValueError("unsupported upstream session cutoff")
    now = upstream.tm.parse_now(args.as_of)
    collected, full, coverage = {}, {}, {}
    rawdir = out / "raw"
    rawdir.mkdir()
    for s in symbols:
        text = upstream.tm.read_offline(args.offline, s) if args.offline else upstream.tm.fetch_chart(s)
        (rawdir / (s + "-60d5m.json")).write_text(text, encoding="utf-8")
        result = upstream.tm.chart_result(text, s)
        if str((result.get("meta") or {}).get("symbol", "")).upper().replace(".", "-") != s.replace(".", "-"):
            raise ValueError("chart symbol missing or does not match " + s)
        if (result.get("meta") or {}).get("dataGranularity") != "5m":
            raise ValueError("chart must identify five-minute bars: " + s)
        stamps = result.get("timestamp") or []
        if len(stamps) != len(set(stamps)):
            raise ValueError("duplicate timestamps: " + s)
        _, bars, dropped = upstream.tm.parse_bars(result, now)
        for b in bars:
            for key in ("o", "h", "l", "c"):
                number(b[key], True)
            if b["l"] > min(b["o"], b["c"]) or b["h"] < max(b["o"], b["c"]) or b["l"] > b["h"]:
                raise ValueError("invalid OHLC: " + s)
        days = upstream.tm.split_sessions(bars)
        completed, short = upstream.tm.full_sessions(days, "9999-12-31")
        if not completed:
            raise ValueError("no complete sessions for " + s)
        full[s] = dict(completed)
        collected[s] = hashlib.sha256(text.encode()).hexdigest()
        coverage[s] = {"raw_sha256": collected[s], "parsed_bars": len(bars),
                       "dropped_bars": dropped, "incomplete_sessions": short,
                       "source_first_full": completed[0][0], "source_last_full": completed[-1][0],
                       "meta": result.get("meta", {})}
        write_json(out / "coverage.json", coverage)
        if not args.offline:
            upstream.tm._sleep(upstream.tm.PAUSE)
    end = max(max(days) for days in full.values())
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=59)).isoformat()
    dates = [dt.date.fromisoformat(start) + dt.timedelta(days=i) for i in range(60)]
    if any(d.year not in upstream.risk.CALENDAR_YEARS for d in dates):
        raise ValueError("window outside upstream supported holiday-calendar years")
    expected = [d.isoformat() for d in dates if upstream.risk.trading_day(d)[0]]
    missing = {}
    for s in symbols:
        full[s] = {d: rows for d, rows in full[s].items() if d in expected}
        missing[s] = sorted(set(expected) - set(full[s]))
        coverage[s].update({"used_dates": sorted(full[s]), "used_sessions": len(full[s]),
                            "missing_expected_sessions": missing[s]})
    manifest = {"upstream_commit": BASE, "upstream_git_blob_pins": PINS,
        "evaluated_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream.root, text=True).strip(),
        "command_argv": sys.argv, "python_version": sys.version,
        "as_of_utc": now.isoformat() + "Z",
        "data_fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "offline" if args.offline else "Yahoo range=60d interval=5m",
        "requested_calendar_days": 60, "start_inclusive": start, "end_inclusive": end,
        "window_anchor": "latest complete session available among input symbols",
        "symbols_in_cash_tie_break_order": symbols, "rule": rule, "last_slot": last,
        "account_config": upstream.config, "fee_per_fill": float(fee),
        "allow_partial": args.allow_partial, "data_sha256": collected,
        "scope": "upstream historical long ORB + account sizing/risk; NOT historical scanner/Claude replay",
        "execution": "idealized trigger-close fills; gap stop at stop; target at target; no slippage/spread; reusable cash; exits before entries at bar-end; input ticker order breaks entry ties; rounded-cent fills",
        "corporate_actions": "Yahoo chart OHLC unmodified; no independent split/dividend adjustment or delisted-universe reconstruction",
        "monte_carlo": "NOT RUN and not used as an entry filter; no pre-window training pool available",
        "biases": "user-selected current tickers; survivorship/selection bias; ex-post complete-session filter; idealized fills"}
    write_json(out / "manifest.json", manifest)
    write_json(out / "coverage.json", coverage)
    if any(missing.values()) and not args.allow_partial:
        raise ValueError("missing expected sessions; inspect coverage.json; --allow-partial explicitly accepts exclusions")
    if any(not full[s] for s in symbols):
        raise ValueError("a symbol has no sessions in the requested window")
    account, candidates = Account(upstream, str(fee)), []
    try:
        simulate(upstream, full, symbols, rule, last, str(fee), account, candidates)
    finally:
        write_json(out / "candidates.json", candidates)
        write_json(out / "decisions.json", account.decisions)
        write_json(out / "fills.json", account.fills)
        write_json(out / "closed_trades.json", account.closed)
        write_json(out / "equity.json", account.equity)
    result = metrics(account)
    result.update({"status": "SUCCESS_PARTIAL_COVERAGE" if any(missing.values()) else "SUCCESS",
                   "limitations": manifest["scope"], "monte_carlo": "not evaluated"})
    write_json(out / "summary.json", result)
    (out / "REPORT.md").write_text("# Simulated historical ORB account evaluation\n\n"
        + "Not a replay of Claude decisions or the historical scanner. Not proof of profitability.\n\n"
        + "```json\n" + json.dumps(result, indent=2, allow_nan=False) + "\n```\n\n"
        + "Read manifest.json and coverage.json for assumptions, dates, exclusions and reproduction.\n", encoding="utf-8")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", required=True)
    p.add_argument("--or-minutes", type=int, required=True)
    p.add_argument("--stop-rule", choices=["or_mid", "or_low"], required=True)
    p.add_argument("--target-r", required=True)
    p.add_argument("--fee-per-fill", default="0")
    p.add_argument("--accept-limitations", action="store_true")
    p.add_argument("--allow-partial", action="store_true")
    p.add_argument("--offline", help="directory with SYMBOL-60d5m.json(.gz) snapshots")
    p.add_argument("--as-of", help="original manifest as_of_utc for offline reproduction")
    p.add_argument("--out", default="evaluation-results")
    args = p.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "summary.json", {"status": "RUNNING"})
    try:
        execute(args, Upstream(), out)
    except Exception as exc:
        write_json(out / "summary.json", {"status": "FAILED", "error": str(exc), "exception": type(exc).__name__})
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
