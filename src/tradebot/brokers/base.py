"""Common broker interface. Every connector implements the same four calls."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..models import OrderResult, Position, TradeProposal

log = logging.getLogger("broker")


class BrokerError(RuntimeError):
    pass


class LiveTradingNotArmed(BrokerError):
    pass


class Broker(ABC):
    name: str = "broker"

    def __init__(self, live: bool, live_armed: bool):
        self.live = live
        self.live_armed = live_armed

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def snapshot(self) -> tuple[float, list[Position]]:
        """Return (current equity incl. open P&L, open positions). Raise BrokerError if unknown:
        TITAN must never size a trade off a stale or guessed balance."""

    @abstractmethod
    def _place_bracket(self, plan: TradeProposal) -> OrderResult: ...

    @abstractmethod
    def flatten_all(self) -> None:
        """Cancel all working orders and close all positions."""

    def place_bracket(self, plan: TradeProposal) -> OrderResult:
        """Market entry with an attached stop-loss and profit target (OCO)."""
        if self.live and not self.live_armed:
            raise LiveTradingNotArmed(
                f"{self.name}: live order refused. Set TRADEBOT_LIVE_ARMED=yes-real-money to enable real-money trading.")
        if plan.quantity < 1:
            raise BrokerError("quantity must be >= 1")
        log.info("[%s%s] %s %d %s stop=%s target=%s", self.name, " LIVE" if self.live else "",
                 plan.direction, plan.quantity, plan.symbol, plan.stop, plan.target)
        return self._place_bracket(plan)
