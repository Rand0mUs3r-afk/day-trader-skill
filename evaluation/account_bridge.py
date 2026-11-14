#!/usr/bin/env python3
"""Connect original plans/tickets to the persistent simulation ledger.

This is an accounting/execution adapter, not a strategy or full backtest.
The original skills produce the plan or exit order. This module never selects
symbols, generates signals, changes stops/targets, or invents Time Machine
results. Scanner/news/entry/exit/Time Machine orchestration remains separate.
No broker API or AI API is called. Fill prices/fees are explicit inputs, not
assumed executions. Approval is one exact ticket at a time, using upstream
approval parsing. Simulated fills and original risk history commit atomically.
"""
import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import ledger

ROOT = Path(__file__).resolve().parents[1]
PINS = {"trader-budget/size.py": "1c9ba4749ed6fc178ee836af4381472ac59eae77",
        "trader-risk/check.py": "2e51047392a491b30d3d49bcb504cd41bcee50fa",
        "trader-budget/config.example.json": "f02db426b5c96b2a6d43177e7f17fbcd3664226d"}


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _pinned(path):
    raw = (ROOT / path).read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if blob != PINS[path]:
        raise ValueError("upstream source changed: " + path)
    return raw


def _module(name, path):
    _pinned(path)
    spec = importlib.util.spec_from_file_location("simulation_original_" + name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clock(text):
    now = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if now.tzinfo is None:
        raise ValueError("simulation timestamp requires a UTC offset")
    return now.astimezone(dt.timezone.utc).replace(tzinfo=None)


def _context(account, quotes, at):
    now = _clock(at)
    size = _module("size", "trader-budget/size.py")
    risk = _module("risk", "trader-risk/check.py")
    config = json.loads(_pinned("trader-budget/config.example.json"))
    prices, times = {}, {}
    for symbol, value in quotes.get("prices", {}).items():
        symbol = str(symbol).upper()
        stamp = size.price_time(quotes, symbol)
        if stamp is None or stamp > now:
            raise ValueError("missing or future-dated quote time for " + symbol)
        prices[symbol] = float(ledger._amount(value, True))
        times[symbol] = stamp
    state = ledger.snapshot(account)
    context = ledger.sizing_context(account, prices)
    config["budget"] = context["budget"]
    history = copy.deepcopy(state.get("broker_state", {"orders": []}))
    today = size.to_eastern(now).date().isoformat()
    return size, risk, config, state, context, history, prices, times, now, today


def _proposal(account, order, quotes, at, context, sizing=None):
    size, risk, config, state, values, history, prices, times, now, today = context
    verdict = risk.check([order], config, portfolio=values["portfolio"],
                         prices=prices, state=history, now=now, today=today,
                         price_times=times)
    verdict["date"] = today
    ticket = ledger._ticket()
    ready, label, result = ticket.status_of(order, verdict, history, today)
    return {"mode": "simulation_only", "account": str(Path(account).resolve()),
            "account_sha256": hashlib.sha256(Path(account).read_bytes()).hexdigest(),
            "at": at, "order": copy.deepcopy(order), "quotes": copy.deepcopy(quotes),
            "risk": verdict, "sizing": sizing, "ready": ready, "status": label,
            "fingerprint": ticket.fingerprint(order)}


def prepare_plan(account, plan, quotes, at, limit=None):
    """Size the unchanged original exit plan using current ledger equity/cash."""
    with ledger._locked(account):
        context = _context(account, quotes, at)
        size, risk, config, state, values, history, prices, times, now, today = context
        price, stamp, fresh = size.fresh_price(quotes, plan["symbol"].upper(), now)
        sizing = size.size_trade(plan, config, budget=values["budget"],
            cash=values["cash"], price=price if fresh else None, limit=limit, today=today)
        order = size.bracket_order(sizing)
        if order is None:
            return {"mode": "simulation_only", "ready": False, "status": "SIZE_BLOCK",
                    "sizing": sizing, "at": at}
        return _proposal(account, order, quotes, at, context, sizing)


def prepare_order(account, order, quotes, at):
    """Risk-check an original ticket, including an original flatten/exit ticket."""
    with ledger._locked(account):
        return _proposal(account, order, quotes, at, _context(account, quotes, at))


def approve_fill(account, proposal, reply, event):
    """Record one explicitly approved simulated fill; never send an order.

    Changing account state requires a new proposal. Existing duplicate fill IDs
    are refused here rather than approving/resubmitting a ticket twice. The
    lower-level ledger remains idempotent for simple fill-record retries.
    """
    if proposal.get("mode") != "simulation_only" or not proposal.get("ready"):
        raise ValueError("no ready simulated proposal")
    if str(Path(account).resolve()) != proposal["account"]:
        raise ValueError("proposal belongs to another account")
    now = _clock(event["at"])
    if now < _clock(proposal["at"]):
        raise ValueError("fill predates the checked proposal")
    with ledger._locked(account):
        raw = Path(account).read_bytes()
        if hashlib.sha256(raw).hexdigest() != proposal["account_sha256"]:
            raise ValueError("account changed; run original sizing/risk again")
        state = ledger.snapshot(account)
        if any(row["event"]["id"] == event["id"] for row in state["fills"]):
            raise ValueError("fill ID already recorded; do not approve/resubmit twice")
        history = copy.deepcopy(state.get("broker_state", {"orders": []}))
        ticket = ledger._ticket()
        today = ticket.to_eastern(now).date().isoformat()
        order = proposal["order"]
        if order.get("qty") is None:
            raise ValueError("this ledger adapter requires whole-share qty, not notional orders")
        if ticket.fingerprint(order) != proposal["fingerprint"]:
            raise ValueError("exact ticket changed after risk check")
        ready, label, result = ticket.status_of(order, proposal["risk"], history, today)
        if not ready:
            raise ValueError(label)
        accepted, why = ticket.parse_yes(reply, order, [order.get("symbol", "")])
        if not accepted:
            raise ValueError("original approval refused: " + why)
        if (event["symbol"].strip().upper() != order["symbol"].upper()
                or event["side"] != order["side"]
                or ledger._amount(event["qty"], True) != ledger._amount(order["qty"], True)):
            raise ValueError("fill does not match this whole-share ticket; partial fills unsupported")
        record = {"at": ticket.ny_iso(now), "date": today,
                  "fingerprint": proposal["fingerprint"], "symbol": order["symbol"],
                  "side": order["side"], "qty": order["qty"],
                  "kind": ticket.kind_of(order, result), "max_loss": ticket.max_loss(order)[0],
                  "paper": True, "simulation_only": True, "simulation_event_id": event["id"]}
        history.setdefault("orders", []).extend([
            dict(record, status="approved", reply=reply),
            dict(record, status="placed"),
            dict(record, status="filled", price=float(ledger._amount(event["price"], True)))])
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "account.json"
            ledger._save(temporary, state)
            updated = ledger.record_fill(temporary, event)
        updated["broker_state"] = history
        updated.setdefault("workflow_audit", []).append({"proposal": copy.deepcopy(proposal),
                                                         "reply": reply, "fill": copy.deepcopy(event)})
        ledger._save(account, updated)
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, input_flag in (("plan", "--plan"), ("order", "--order")):
        command = commands.add_parser(name)
        command.add_argument("--account", required=True)
        command.add_argument(input_flag, required=True)
        command.add_argument("--quotes", required=True)
        command.add_argument("--at", required=True)
        command.add_argument("--out", required=True)
        if name == "plan":
            command.add_argument("--limit", type=float)
    fill = commands.add_parser("fill")
    fill.add_argument("--account", required=True)
    fill.add_argument("--proposal", required=True)
    fill.add_argument("--reply", required=True)
    fill.add_argument("--event", required=True)
    args = parser.parse_args()
    if args.command == "fill":
        result = approve_fill(args.account, _load(args.proposal), args.reply, _load(args.event))
    else:
        if args.command == "plan":
            result = prepare_plan(args.account, _load(args.plan), _load(args.quotes), args.at, args.limit)
        else:
            result = prepare_order(args.account, _load(args.order), _load(args.quotes), args.at)
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
