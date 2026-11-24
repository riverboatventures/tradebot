"""The desk: ATLAS -> VEGA -> ORION + LUNA -> NOVA (draft) -> TITAN -> NOVA (final) -> broker."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .account import AccountTracker
from .agents import Atlas, Luna, Nova, Orion, Titan, Vega
from .brokers import Broker, BrokerError, PaperBroker
from .config import Settings
from .instruments import get as get_instrument
from .data import DataError, MarketData, NewsFeed, SentimentFeed
from .journal import Journal
from .llm import LLM
from .models import AccountState, Position
from .orders import OrderTracker

log = logging.getLogger("desk")

KILL_FILE = Path("KILL")


@dataclass
class Desk:
    broker: Broker
    tracker: AccountTracker
    orders: OrderTracker
    halted: str | None = None
    last_state: AccountState | None = field(default=None)


class Pipeline:
    def __init__(self, settings: Settings, data: MarketData, news: NewsFeed, sentiment: SentimentFeed,
                 brokers: list[Broker], journal: Journal | None = None, state_dir: str = "logs",
                 llm: LLM | None = None):
        self.s = settings
        self.data = data
        llm = llm or LLM(settings.llm.model, settings.llm.enabled)
        self.atlas = Atlas(settings, data)
        self.vega = Vega(settings, data)
        self.orion = Orion(news, llm)
        self.luna = Luna(sentiment, llm)
        self.titan = Titan(settings)
        self.nova = Nova(settings, llm)
        self.journal = journal or Journal(state_dir)
        prop = settings.risk.prop
        self.desks = []
        for b in brokers:
            key = f"{b.name.replace(':', '_')}_{'live' if b.live else 'sim'}"
            acct = AccountTracker(key, state_dir, trailing_type=prop.trailing_type,
                                  starting_balance=prop.starting_balance if prop.enabled else None)
            orders = OrderTracker(b, Path(state_dir) / f"orders_{key}.json", alert=self._alert,
                                  flatten_untracked=getattr(b, "bot_only", True))
            self.desks.append(Desk(b, acct, orders))

    def _alert(self, level: str, msg: str) -> None:
        (log.critical if level == "critical" else log.warning)(msg)
        self.journal.write("orders", level, {"message": msg})

    def _flatten(self, desk: Desk, why: str) -> None:
        log.warning("[%s] FLATTEN: %s", desk.broker.name, why)
        self.journal.write("TITAN", "flatten", {"broker": desk.broker.name, "reason": why})
        desk.broker.flatten_all()

    def _equity_high(self, equity: float, positions) -> float:
        """Add back the best unrealized gain seen in the latest bar for each open position, so the
        intraday trailing peak isn't missed between polls (conservative: raises the peak)."""
        extra = 0.0
        for pos in positions:
            try:
                bar = self.data.bars(pos.symbol, self.s.session.bar_minutes, 1)[-1]
            except (DataError, IndexError):
                continue
            inst = get_instrument(pos.symbol)
            move = (bar.high - bar.close) if pos.quantity > 0 else (bar.close - bar.low)
            extra += max(0.0, move) / inst.tick_size * inst.tick_value * abs(pos.quantity)
        return equity + extra

    def run_once(self, now: datetime) -> None:
        # Advance paper fills with the latest bar
        for d in self.desks:
            if isinstance(d.broker, PaperBroker):
                for sym in list(d.broker.brackets):
                    try:
                        d.broker.on_bar(sym, self.data.bars(sym, self.s.session.bar_minutes, 1)[-1])
                    except (DataError, IndexError) as e:
                        log.warning("paper mark for %s unavailable: %s", sym, e)

        # Read every account and reconcile orders first, so a missing stop or a stray order is
        # handled even when the bot is otherwise halted or outside trading hours.
        snaps: dict[int, tuple[float, list[Position]]] = {}
        for d in self.desks:
            try:
                eq, pos = d.broker.snapshot()
                d.orders.reconcile(pos, now)
                snaps[id(d)] = (eq, pos)
            except BrokerError as e:
                log.error("[%s] can't read account, skipping this cycle: %s", d.broker.name, e)

        if KILL_FILE.exists():
            for d in self.desks:
                if d.halted != "kill switch":  # flatten once per kill
                    self._flatten(d, "kill switch file present")
                    d.halted = "kill switch"
            return

        # Account gates
        active: list[tuple[Desk, AccountState]] = []
        for d in self.desks:
            if id(d) not in snaps:
                continue
            eq, pos = snaps[id(d)]
            acct = d.tracker.update(eq, pos, now.date(), self._equity_high(eq, pos))
            d.last_state = acct
            if now.time() >= self.s.session.flatten_at:
                if pos:
                    self._flatten(d, f"end of day ({self.s.session.flatten_at:%H:%M})")
                continue
            if halt := self.titan.trading_halted(acct):
                if d.halted != halt:
                    d.halted = halt
                    self._flatten(d, halt)
                continue
            d.halted = None  # limits cleared (e.g. new day): the next halt must flatten again
            if now.time() < self.s.session.first_entry or now.time() > self.s.session.last_entry:
                continue
            active.append((d, acct))
        if not active:
            return

        # Analysis (shared across accounts)
        watchlist = self.atlas.scan()
        self.journal.write("ATLAS", "watchlist", watchlist)
        for item in watchlist.items:
            try:
                chart = self.vega.analyze(item.symbol)
            except DataError as e:
                log.warning("VEGA skipped %s: %s", item.symbol, e)
                continue
            self.journal.write("VEGA", "analysis", chart)
            if not chart.setup:
                continue
            news = self.orion.analyze(item.symbol, now)
            self.journal.write("ORION", "news", news)
            mood = self.luna.analyze(item.symbol)
            self.journal.write("LUNA", "sentiment", mood)
            draft = self.nova.draft(item, chart, news, mood)
            self.journal.write("NOVA", "draft", draft)
            if not draft:
                continue

            for d, acct in active:
                if any(b.symbol == draft.symbol for b in d.orders.active):
                    continue  # an order for this market is still being confirmed
                risk = self.titan.review(draft, acct, news, mood, now)
                self.journal.write("TITAN", "review", risk, broker=d.broker.name)
                decision = self.nova.decide(risk, chart, news, mood)
                self.journal.write("NOVA", "decision", decision, broker=d.broker.name)
                if decision.action != "trade" or not decision.plan:
                    continue
                try:
                    result = d.broker.place_bracket(decision.plan)
                except BrokerError as e:
                    log.error("[%s] order failed: %s", d.broker.name, e)
                    self.journal.write("broker", "error", {"error": str(e)}, broker=d.broker.name)
                    continue
                self.journal.write("broker", "order", result, broker=d.broker.name)
                if result.ok:
                    bracket = d.orders.track(decision.plan, result, now)
                    d.orders.verify_new(bracket, timeout_s=0 if isinstance(d.broker, PaperBroker) else 10)
                    # refresh so the next symbol is sized against current positions and open risk
                    eq, pos = d.broker.snapshot()
                    if not any(p.symbol == decision.plan.symbol for p in pos):
                        # fill not visible yet (e.g. NinjaTrader files lag): count it anyway
                        from .models import Position
                        q = decision.plan.quantity * (1 if decision.plan.direction == "long" else -1)
                        pos = pos + [Position(symbol=decision.plan.symbol, quantity=q, avg_price=decision.plan.entry)]
                    d.tracker.record_trade(decision.plan.symbol, risk.risk_dollars)
                    acct = d.tracker.update(eq, pos, now.date())
                    active = [(dd, acct if dd is d else aa) for dd, aa in active]
