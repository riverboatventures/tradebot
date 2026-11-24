"""Live order tracking: makes sure every bracket the bot sends actually filled and stays
protected by a working stop. Anything the bot can't confirm is cancelled or closed.

Checks, every cycle, for each tracked bracket:
  * entry rejected / cancelled        -> cancel the stop and target, mark failed
  * entry not filled after a timeout  -> cancel entry, stop and target, mark failed
  * position open, stop not working   -> close the position (never sit unprotected)
  * position bigger than ordered      -> close the position
  * position closed                   -> cancel any exit order still working, mark closed
And for positions the bot has no record of (e.g. after a crash): close them.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from .brokers.base import Broker, BrokerError
from .models import OrderResult, Position, TradeProposal

log = logging.getLogger("orders")

ENTRY_TIMEOUT = timedelta(seconds=60)    # market entries should fill in seconds
PROTECT_GRACE = timedelta(seconds=20)    # time allowed for the stop to show as working
UNTRACKED_GRACE = timedelta(seconds=30)  # a position the bot didn't open, seen this long -> close


class TrackedBracket(BaseModel):
    symbol: str
    direction: Literal["long", "short"]
    quantity: int
    entry_id: str
    stop_id: str | None = None
    target_id: str | None = None
    placed_at: datetime
    status: Literal["pending", "open", "closed", "failed"] = "pending"
    note: str = ""


class BracketStatus(BaseModel):
    entry: Literal["pending", "filled", "rejected", "cancelled", "unknown"]
    stop_working: bool
    target_working: bool


class OrderTracker:
    def __init__(self, broker: Broker, state_path: str | Path,
                 alert: Callable[[str, str], None] | None = None, flatten_untracked: bool = True):
        self.broker = broker
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.alert = alert or (lambda level, msg: log.warning(msg))
        self.flatten_untracked = flatten_untracked
        raw = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.brackets = [TrackedBracket.model_validate(b) for b in raw.get("brackets", [])]
        self.untracked_seen: dict[str, str] = raw.get("untracked_seen", {})

    def _save(self) -> None:
        # keep open brackets plus the last 50 finished ones for the record
        live = [b for b in self.brackets if b.status in ("pending", "open")]
        done = [b for b in self.brackets if b.status not in ("pending", "open")][-50:]
        self.brackets = done + live
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"brackets": [b.model_dump(mode="json") for b in self.brackets],
                                   "untracked_seen": self.untracked_seen}, indent=2))
        tmp.replace(self.path)

    @property
    def active(self) -> list[TrackedBracket]:
        return [b for b in self.brackets if b.status in ("pending", "open")]

    def track(self, plan: TradeProposal, result: OrderResult, now: datetime) -> TrackedBracket:
        b = TrackedBracket(symbol=plan.symbol, direction=plan.direction, quantity=plan.quantity,
                           entry_id=result.order_id or "", stop_id=result.stop_id,
                           target_id=result.target_id, placed_at=now)
        self.brackets.append(b)
        self._save()
        return b

    def _cancel(self, *ids: str | None) -> None:
        for oid in ids:
            if oid:
                try:
                    self.broker.cancel_order(oid)
                except BrokerError as e:
                    log.error("cancel %s failed: %s", oid, e)

    def _close(self, b: TrackedBracket | None, symbol: str, why: str) -> None:
        self.alert("critical", f"[{self.broker.name}] CLOSING {symbol}: {why}")
        try:
            self.broker.close_position(symbol)
        except BrokerError as e:
            self.alert("critical", f"[{self.broker.name}] close {symbol} FAILED ({e}). CHECK THE ACCOUNT MANUALLY.")
        if b:
            b.status, b.note = "closed", why

    def reconcile(self, positions: list[Position], now: datetime) -> None:
        pos_by_sym = {p.symbol: p.quantity for p in positions}
        for b in self.active:
            try:
                st = self.broker.bracket_status(b)
            except BrokerError as e:
                log.error("can't read order status for %s: %s", b.symbol, e)
                continue
            age = now - b.placed_at
            qty = pos_by_sym.get(b.symbol, 0)

            if st.entry in ("rejected", "cancelled"):
                self._cancel(b.stop_id, b.target_id)
                b.status, b.note = "failed", f"entry {st.entry}"
                self.alert("warning", f"[{self.broker.name}] {b.symbol} entry {st.entry}; exits cancelled")
                continue
            if st.entry in ("pending", "unknown") and qty == 0:
                if age > ENTRY_TIMEOUT:
                    self._cancel(b.entry_id, b.stop_id, b.target_id)
                    b.status, b.note = "failed", "entry never confirmed"
                    self.alert("warning", f"[{self.broker.name}] {b.symbol} entry not confirmed after "
                                          f"{ENTRY_TIMEOUT.seconds}s; cancelled")
                continue

            # Entry filled (or a position exists for it)
            if qty == 0:
                if b.status == "open" or st.entry == "filled":
                    if st.stop_working or st.target_working:
                        self._cancel(b.stop_id, b.target_id)  # leftover exit could open a new position
                    b.status, b.note = "closed", "position closed"
                continue
            b.status = "open"
            want_sign = 1 if b.direction == "long" else -1
            if qty * want_sign < 0:
                self._close(b, b.symbol, f"position is {qty}, opposite of the {b.direction} the bot placed")
            elif abs(qty) > b.quantity:
                self._close(b, b.symbol, f"position {qty} is larger than the {b.quantity} ordered")
            elif not st.stop_working and age > PROTECT_GRACE:
                self._close(b, b.symbol, "stop-loss is not working at the broker")

        # Positions the bot has no open record of
        tracked = {b.symbol for b in self.active}
        for sym, qty in pos_by_sym.items():
            if sym in tracked or qty == 0:
                self.untracked_seen.pop(sym, None)
                continue
            first = datetime.fromisoformat(self.untracked_seen.setdefault(sym, now.isoformat()))
            if self.flatten_untracked and now - first > UNTRACKED_GRACE:
                self._close(None, sym, "position not opened by this bot (or its record was lost)")
                self.untracked_seen.pop(sym, None)
        for sym in list(self.untracked_seen):
            if sym not in pos_by_sym:
                self.untracked_seen.pop(sym)
        self._save()

    def verify_new(self, b: TrackedBracket, timeout_s: float = 10.0, poll_s: float = 1.0) -> bool:
        """Right after placing: wait briefly for the fill and the stop. True if confirmed."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                st = self.broker.bracket_status(b)
                if st.entry in ("rejected", "cancelled"):
                    return False
                if st.entry == "filled" and st.stop_working:
                    b.status = "open"
                    self._save()
                    return True
            except BrokerError as e:
                log.error("status check failed: %s", e)
            if time.monotonic() >= deadline:
                self.alert("warning", f"[{self.broker.name}] {b.symbol} fill/stop not confirmed within "
                                      f"{timeout_s:.0f}s; will keep checking each cycle")
                return False
            time.sleep(poll_s)
