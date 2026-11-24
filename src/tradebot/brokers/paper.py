"""In-process paper broker: simulated fills, no external account needed."""
from __future__ import annotations

import itertools

from ..instruments import get as get_instrument
from ..models import Bar, OrderResult, Position, TradeProposal
from .base import Broker


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, starting_equity: float = 50000.0, commission_per_side: float = 0.62):
        super().__init__(live=False, live_armed=False)
        self.cash = starting_equity
        self.commission = commission_per_side
        self.brackets: dict[str, dict] = {}  # symbol -> {qty, entry, stop, target}
        self.marks: dict[str, float] = {}
        self.closed_trades: list[dict] = []
        self._ids = itertools.count(1)

    def connect(self) -> None:
        pass

    def snapshot(self) -> tuple[float, list[Position]]:
        open_pnl = 0.0
        positions = []
        for sym, b in self.brackets.items():
            inst = get_instrument(sym)
            mark = self.marks.get(sym, b["entry"])
            open_pnl += (mark - b["entry"]) / inst.tick_size * inst.tick_value * b["qty"]
            positions.append(Position(symbol=sym, quantity=b["qty"], avg_price=b["entry"]))
        return self.cash + open_pnl, positions

    def _place_bracket(self, plan: TradeProposal) -> OrderResult:
        qty = plan.quantity if plan.direction == "long" else -plan.quantity
        self.brackets[plan.symbol] = {"qty": qty, "entry": plan.entry, "stop": plan.stop, "target": plan.target}
        self.marks[plan.symbol] = plan.entry
        self.cash -= self.commission * plan.quantity
        n = next(self._ids)
        return OrderResult(ok=True, broker=self.name, order_id=f"P{n}E", stop_id=f"P{n}S", target_id=f"P{n}T")

    def _close(self, sym: str, price: float, reason: str) -> None:
        b = self.brackets.pop(sym)
        inst = get_instrument(sym)
        pnl = (price - b["entry"]) / inst.tick_size * inst.tick_value * b["qty"]
        self.cash += pnl - self.commission * abs(b["qty"])
        self.marks.pop(sym, None)
        self.closed_trades.append({"symbol": sym, "qty": b["qty"], "entry": b["entry"], "exit": price,
                                   "pnl": round(pnl, 2), "reason": reason})

    def on_bar(self, sym: str, bar: Bar) -> None:
        """Advance the simulation: check stop first (pessimistic), then target."""
        b = self.brackets.get(sym)
        self.marks[sym] = bar.close
        if not b:
            return
        long = b["qty"] > 0
        if (long and bar.low <= b["stop"]) or (not long and bar.high >= b["stop"]):
            self._close(sym, b["stop"], "stop")
        elif (long and bar.high >= b["target"]) or (not long and bar.low <= b["target"]):
            self._close(sym, b["target"], "target")

    def bracket_status(self, bracket):
        from ..orders import BracketStatus
        live = bracket.symbol in self.brackets
        return BracketStatus(entry="filled", stop_working=live, target_working=live)

    def cancel_order(self, order_id: str) -> None:
        pass  # paper exits disappear with the position

    def close_position(self, symbol: str) -> None:
        if symbol in self.brackets:
            self._close(symbol, self.marks.get(symbol, self.brackets[symbol]["entry"]), "close")

    def flatten_all(self) -> None:
        for sym in list(self.brackets):
            self._close(sym, self.marks.get(sym, self.brackets[sym]["entry"]), "flatten")
