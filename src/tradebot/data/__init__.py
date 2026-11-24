"""Data providers: market bars, news, and sentiment.

Each is an interface so real feeds can be plugged in without touching the agents.
"""
from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from ..instruments import get as get_instrument
from ..models import Bar


class MarketData(ABC):
    @abstractmethod
    def bars(self, symbol: str, minutes: int, count: int) -> list[Bar]:
        """Most recent `count` bars of `minutes` size, oldest first."""


class NewsFeed(ABC):
    @abstractmethod
    def headlines(self, symbol: str) -> list[dict]:
        """Recent headlines: dicts with headline, source, url, published_at."""

    def economic_events(self) -> list[dict]:
        """Scheduled events today: dicts with name, time (datetime), impact (low/medium/high)."""
        return []


class SentimentFeed(ABC):
    @abstractmethod
    def posts(self, symbol: str) -> list[dict]:
        """Recent crowd chatter: dicts with text, source, engagement."""


# ---- Offline/simulated providers (for paper runs and tests) ----------------

_BASE_PRICES = {"MES": 6000.0, "MNQ": 21000.0, "MYM": 44000.0, "M2K": 2300.0,
                "CL": 70.0, "MCL": 70.0, "GC": 2700.0, "MGC": 2700.0}


class SyntheticMarketData(MarketData):
    """Random-walk bars with a drift, reproducible per symbol. For dry runs only."""

    def __init__(self, seed: int = 7):
        self.seed = seed

    def bars(self, symbol: str, minutes: int, count: int) -> list[Bar]:
        inst = get_instrument(symbol)
        rng = random.Random(f"{self.seed}-{symbol}-{minutes}")
        price = _BASE_PRICES.get(symbol, 100.0)
        drift = rng.uniform(-0.0004, 0.0004)
        vol = price * 0.0015
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        out = []
        for i in range(count):
            o = price
            c = o * (1 + drift) + rng.gauss(0, vol)
            h = max(o, c) + abs(rng.gauss(0, vol / 2))
            l = min(o, c) - abs(rng.gauss(0, vol / 2))
            v = max(1.0, 1000 * (1 + 0.5 * math.sin(i / 10)) * rng.uniform(0.5, 1.5))
            out.append(Bar(ts=now - timedelta(minutes=minutes * (count - i)),
                           open=inst.round_price(o), high=inst.round_price(h),
                           low=inst.round_price(l), close=inst.round_price(c), volume=round(v)))
            price = c
        return out


class StaticNewsFeed(NewsFeed):
    def __init__(self, items: dict[str, list[dict]] | None = None, events: list[dict] | None = None):
        self.items = items or {}
        self.events = events or []

    def headlines(self, symbol: str) -> list[dict]:
        return self.items.get(symbol, [])

    def economic_events(self) -> list[dict]:
        return self.events


class StaticSentimentFeed(SentimentFeed):
    def __init__(self, items: dict[str, list[dict]] | None = None):
        self.items = items or {}

    def posts(self, symbol: str) -> list[dict]:
        return self.items.get(symbol, [])
