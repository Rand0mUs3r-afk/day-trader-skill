#!/usr/bin/env python3
"""Persistent simulated accounting only; no strategy, signals or broker calls.

Use the original scanner/news/entry/exit/Time Machine/budget/risk/approval
workflow unchanged. Its simulation executor supplies explicit fill events.
This component neither approves orders nor generates historical decisions.

CLI: ledger.py init ACCOUNT.json
     ledger.py fill ACCOUNT.json FILL.json
     ledger.py show ACCOUNT.json
Fill fields: id, at (ISO with UTC offset), symbol, side, qty, price, fee.
The fee must be explicit. Repeating an identical fill ID is a no-op.
"""
import argparse
from contextlib import contextmanager
import copy
import datetime as dt
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TICKET_BLOB = "721aa2bd73f7973b6ca19d21d047333077a217a7"


def _ticket():
    path = ROOT / "trader-broker/ticket.py"
    blob = path.read_bytes()
    digest = hashlib.sha1(b"blob " + str(len(blob)).encode() + b"\0" + blob).hexdigest()
    if digest != TICKET_BLOB:
        raise ValueError("original ticket.py changed; review integration before use")
    spec = importlib.util.spec_from_file_location("original_ticket_accounting", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _amount(value, positive=False):
    if isinstance(value, bool):
        raise ValueError("boolean is not a financial amount")
    number = Decimal(str(value))
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise ValueError("amount must be finite and nonnegative (positive for price/qty)")
    return number


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _save(path, data):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink()


def initialize(path):
    with _locked(path):
        if Path(path).exists():
            raise FileExistsError("refusing to reset an existing simulated account")
        data = {"schema": 1, "mode": "simulation_only", "initial_cash": 2000,
                "portfolio": {"cash": 2000.0, "positions": []}, "fills": []}
        _save(path, data)
    return data


def snapshot(path):
    data = _read(path)
    if data.get("schema") != 1 or data.get("mode") != "simulation_only" or data.get("initial_cash") != 2000:
        raise ValueError("not a supported USD 2,000 simulation ledger")
    return data


def record_fill(path, event):
    if set(event) != {"id", "at", "symbol", "side", "qty", "price", "fee"}:
        raise ValueError("fill requires exactly id, at, symbol, side, qty, price, fee")
    event = copy.deepcopy(event)
    if not isinstance(event["id"], str) or not event["id"].strip():
        raise ValueError("fill ID is required")
    now = dt.datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
    if now.tzinfo is None:
        raise ValueError("fill time requires an explicit UTC offset")
    now = now.astimezone(dt.timezone.utc)
    if not isinstance(event["symbol"], str) or not event["symbol"].strip():
        raise ValueError("symbol is required")
    event["symbol"] = event["symbol"].strip().upper()
    if event["side"] not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    qty = _amount(event["qty"], True)
    if qty != qty.to_integral_value():
        raise ValueError("this adapter records original v2 whole-share fills only")
    price, fee = _amount(event["price"], True), _amount(event["fee"])
    if fee != fee.quantize(Decimal("0.01")):
        raise ValueError("fee must be an explicit cent amount")
    event.update(at=now.isoformat(), qty=int(qty),
                 price=str(price.normalize()), fee=str(fee.normalize()))
    with _locked(path):
        data = snapshot(path)
        for old in data["fills"]:
            if old["event"]["id"] == event["id"]:
                if old["event"] != event:
                    raise ValueError("fill ID reused with different content")
                return data
        if data["fills"] and now < dt.datetime.fromisoformat(data["fills"][-1]["event"]["at"]):
            raise ValueError("fill events must be chronological")
        ticket = _ticket()
        date = ticket.to_eastern(now.replace(tzinfo=None)).date().isoformat()
        with tempfile.TemporaryDirectory() as folder:
            pf = Path(folder) / "portfolio.json"
            _save(pf, data["portfolio"])
            portfolio = ticket.apply_fill(str(pf), {"budget": 2000}, event["symbol"],
                event["side"], int(qty), float(price), date)
        cash = Decimal(str(portfolio["cash"])) - fee
        if cash < 0:
            raise ValueError("simulated fill/fee would require borrowed cash")
        portfolio["cash"] = float(cash)
        data["portfolio"] = portfolio
        data["fills"].append({"event": event, "cash_after": float(cash)})
        _save(path, data)
    return data


def sizing_context(path, marks):
    """Inputs for original size_trade; not a replacement sizing formula.

    The caller supplies only prices known at its simulation time. Risk checks,
    approvals and their original order history remain the orchestrator's job.
    """
    data = snapshot(path)
    portfolio = data["portfolio"]
    equity = Decimal(str(portfolio["cash"]))
    for position in portfolio["positions"]:
        symbol = position["ticker"]
        if symbol not in marks:
            raise ValueError("missing simulation-time price for " + symbol)
        equity += Decimal(str(position["shares"])) * _amount(marks[symbol], True)
    return {"budget": float(equity), "cash": portfolio["cash"],
            "portfolio": copy.deepcopy(portfolio)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "fill", "show"])
    parser.add_argument("account")
    parser.add_argument("event", nargs="?")
    args = parser.parse_args()
    if args.command == "init":
        result = initialize(args.account)
    elif args.command == "fill":
        if not args.event:
            parser.error("fill requires a fill-event JSON file")
        result = record_fill(args.account, _read(args.event))
    else:
        result = snapshot(args.account)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
