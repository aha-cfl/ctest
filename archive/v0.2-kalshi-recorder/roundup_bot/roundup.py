"""Spare-change round-ups: compute, persist in a ledger, sweep in batches.

Money movement is behind the FundingSink interface. Real ACH bank->exchange
transfers are NOT something Plaid transactions access can do; you need a
money-movement product (e.g. Plaid Transfer, Dwolla) or your broker's own
recurring-deposit feature. ManualSink just tells you what to move.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Iterable, Protocol

from .config import RoundupConfig

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Transaction:
    txn_id: str
    amount: Decimal  # positive = money leaving the account (purchase)
    description: str = ""


def roundup_amount(amount: Decimal, cfg: RoundupConfig) -> Decimal:
    if amount <= 0:  # refunds, deposits
        return Decimal("0")
    spare = amount.to_integral_value(rounding=ROUND_CEILING) - amount
    if spare == 0 and cfg.whole_dollar_adds_one:
        spare = Decimal("1")
    return (spare * Decimal(str(cfg.multiplier))).quantize(CENT)


class Ledger:
    """Idempotent: each transaction id is counted once, so re-syncs are safe."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.seen: set[str] = set()
        self.pending = Decimal("0")
        self.swept_total = Decimal("0")
        self.cursor: str | None = None
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.seen = set(raw["seen"])
            self.pending = Decimal(raw["pending"])
            self.swept_total = Decimal(raw["swept_total"])
            self.cursor = raw.get("cursor")

    def add(self, txns: Iterable[Transaction], cfg: RoundupConfig) -> Decimal:
        added = Decimal("0")
        for t in txns:
            if t.txn_id in self.seen:
                continue
            self.seen.add(t.txn_id)
            added += roundup_amount(t.amount, cfg)
        self.pending += added
        return added

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "seen": sorted(self.seen),
            "pending": str(self.pending),
            "swept_total": str(self.swept_total),
            "cursor": self.cursor,
        }, indent=2))
        os.replace(tmp, self.path)


class FundingSink(Protocol):
    def deposit(self, amount_usd: Decimal) -> None: ...


class ManualSink:
    def deposit(self, amount_usd: Decimal) -> None:
        print(f"[roundup] ACTION REQUIRED: move ${amount_usd} from bank to trading account")


def sweep(ledger: Ledger, sink: FundingSink, cfg: RoundupConfig) -> Decimal:
    if ledger.pending < Decimal(str(cfg.min_sweep_usd)):
        return Decimal("0")
    amount = ledger.pending
    sink.deposit(amount)
    ledger.pending = Decimal("0")
    ledger.swept_total += amount
    return amount


def load_csv(path: str | Path) -> list[Transaction]:
    """CSV with columns: id,amount,description (amount positive for purchases)."""
    with open(path, newline="") as fh:
        return [Transaction(r["id"], Decimal(r["amount"]), r.get("description", ""))
                for r in csv.DictReader(fh)]


def fetch_plaid(ledger: Ledger) -> list[Transaction]:
    """Incremental pull via Plaid /transactions/sync.

    Env: PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ACCESS_TOKEN, PLAID_ENV (sandbox|production).
    Plaid sign convention: positive amount = money out, which matches Transaction.
    """
    import requests

    host = {"sandbox": "https://sandbox.plaid.com",
            "production": "https://production.plaid.com"}[os.environ.get("PLAID_ENV", "sandbox")]
    out: list[Transaction] = []
    cursor = ledger.cursor
    while True:
        body = {"client_id": os.environ["PLAID_CLIENT_ID"], "secret": os.environ["PLAID_SECRET"],
                "access_token": os.environ["PLAID_ACCESS_TOKEN"]}
        if cursor:
            body["cursor"] = cursor
        resp = requests.post(f"{host}/transactions/sync", json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        out += [Transaction(t["transaction_id"], Decimal(str(t["amount"])), t.get("name", ""))
                for t in data["added"] if not t.get("pending")]
        cursor = data["next_cursor"]
        if not data["has_more"]:
            break
    ledger.cursor = cursor
    return out
