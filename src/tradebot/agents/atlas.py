"""ATLAS: market scout. Scans the universe and outputs a ranked watchlist."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .. import indicators as ind
from ..config import Settings
from ..data import MarketData
from ..instruments import get as get_instrument
from ..models import Watchlist, WatchlistItem

log = logging.getLogger("ATLAS")


class Atlas:
    """Ranks symbols by tradeability: volatility (room to move), relative volume
    (participation) and trend strength (directional clarity)."""

    def __init__(self, settings: Settings, data: MarketData):
        self.s = settings
        self.data = data

    def scan(self) -> Watchlist:
        items = []
        for sym in self.s.universe:
            inst = get_instrument(sym)
            if not inst.micro and not self.s.risk.allow_full_size:
                continue  # TITAN would reject it anyway; don't waste analysis on it
            bars = self.data.bars(sym, self.s.session.bar_minutes, 60)
            if len(bars) < 30:
                log.warning("%s: not enough bars (%d)", sym, len(bars))
                continue
            closes = [b.close for b in bars]
            atr_pct = ind.atr(bars) / closes[-1] * 100
            avg_vol = sum(b.volume for b in bars) / len(bars)
            recent_vol = sum(b.volume for b in bars[-6:]) / 6
            rel_vol = recent_vol / avg_vol if avg_vol else 0.0
            trend = ind.trend_strength(closes)
            score = atr_pct * rel_vol * (1 + abs(trend) * 10)
            items.append(WatchlistItem(symbol=sym, rank=0, score=round(score, 4),
                                       atr_pct=round(atr_pct, 4), rel_volume=round(rel_vol, 2),
                                       trend_strength=round(trend, 4)))
        items.sort(key=lambda i: i.score, reverse=True)
        items = items[: self.s.atlas.watchlist_size]
        for n, it in enumerate(items, 1):
            it.rank = n
        log.info("Watchlist: %s", ", ".join(f"{i.rank}.{i.symbol}({i.score})" for i in items))
        return Watchlist(generated_at=datetime.now(timezone.utc), items=items)
