"""News, sentiment and economic calendar via Claude web search.

Results are cached (news/sentiment for `refresh_minutes`, the calendar for the day)
to keep search and API costs down.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ..instruments import get as get_instrument
from ..llm import LLM
from . import NewsFeed, SentimentFeed

log = logging.getLogger("web")

# What actually moves each market, so the searches are targeted.
DRIVERS = {
    "equity_index": "US stock index futures (S&P 500, Nasdaq, Dow, Russell): Fed, inflation, jobs, "
                    "earnings of mega-cap stocks, Treasury yields, geopolitics",
    "energy": "crude oil futures (WTI): OPEC+, EIA/API inventories, Middle East and Russia supply, "
              "US dollar, demand outlook",
    "metals": "gold futures: Fed rate expectations, real yields, US dollar, central bank buying, "
              "safe-haven flows",
}


class _Event(BaseModel):
    name: str
    time_et: str  # "HH:MM" Eastern
    impact: Literal["low", "medium", "high"]


class _Calendar(BaseModel):
    events: list[_Event]


class _Post(BaseModel):
    text: str
    source: str
    engagement: int


class _Posts(BaseModel):
    posts: list[_Post]


class _Cache:
    def __init__(self, ttl_s: float):
        self.ttl = ttl_s
        self.store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        hit = self.store.get(key)
        return hit[1] if hit and time.time() - hit[0] < self.ttl else None

    def put(self, key: str, value) -> None:
        self.store[key] = (time.time(), value)


class WebSearchNewsFeed(NewsFeed):
    def __init__(self, llm: LLM, refresh_minutes: int = 15, max_searches: int = 3, tz: str = "America/New_York"):
        self.llm = llm
        self.max_searches = max_searches
        self.cache = _Cache(refresh_minutes * 60)
        self.tz = ZoneInfo(tz)
        self._calendar: tuple[date, list[dict]] | None = None

    def headlines(self, symbol: str) -> list[dict]:
        group = get_instrument(symbol).group
        if (hit := self.cache.get(group)) is not None:
            return hit  # MES/MNQ/MYM/M2K share one search
        res = self.llm.search(
            "You are a financial news researcher. Search for the most recent market-moving news. "
            "Prefer primary sources and major outlets. Only report news from the last 24 hours.",
            f"Find the latest market-moving news (last 24 hours) for {DRIVERS[group]}. "
            f"List each headline with its outlet.",
            max_searches=self.max_searches,
        )
        if res is None:
            return []
        _, results = res
        items = [{"headline": r["title"], "source": r["url"], "url": r["url"], "published_at": None}
                 for r in results[:15]]
        self.cache.put(group, items)
        log.info("%s: %d headlines from web search", group, len(items))
        return items

    def economic_events(self) -> list[dict]:
        today = datetime.now(self.tz).date()
        if self._calendar and self._calendar[0] == today:
            return self._calendar[1]
        res = self.llm.search(
            "You are an economic calendar researcher. Use official or well-known calendars "
            "(e.g. Federal Reserve, BLS, BEA, EIA, Forex Factory, Investing.com).",
            f"List every scheduled US economic release, Fed speech, FOMC event, and EIA/API oil "
            f"inventory report for {today:%A %B %d, %Y}, with the time in US Eastern and its "
            f"typical market impact (high = CPI, PPI, NFP, FOMC, GDP, retail sales, EIA crude "
            f"inventories for oil).",
            max_searches=self.max_searches,
        )
        events: list[dict] = []
        if res:
            text, _ = res
            cal = self.llm.structured("Extract the calendar events. Use 24h HH:MM Eastern time.",
                                      text, _Calendar)
            for e in cal.events if cal else []:
                try:
                    hh, mm = map(int, e.time_et.split(":"))
                except ValueError:
                    continue
                events.append({"name": e.name, "impact": e.impact,
                               "time": datetime(today.year, today.month, today.day, hh, mm, tzinfo=self.tz)})
        if not events:
            log.warning("Economic calendar unavailable; ORION can't enforce news blackouts today")
        else:
            log.info("Calendar: %s", ", ".join(f"{e['time']:%H:%M} {e['name']} ({e['impact']})" for e in events))
            self._calendar = (today, events)  # only cache successes, so failures retry next cycle
        return events


class WebSearchSentimentFeed(SentimentFeed):
    def __init__(self, llm: LLM, refresh_minutes: int = 15, max_searches: int = 3):
        self.llm = llm
        self.max_searches = max_searches
        self.cache = _Cache(refresh_minutes * 60)

    def posts(self, symbol: str) -> list[dict]:
        inst = get_instrument(symbol)
        if (hit := self.cache.get(inst.group)) is not None:
            return hit
        res = self.llm.search(
            "You research retail trader sentiment. Search social and community sources "
            "(Reddit r/FuturesTrading r/wallstreetbets r/Daytrading, StockTwits, X/Twitter, trading forums) "
            "and quote what traders are actually saying. Never invent quotes.",
            f"What is retail trader sentiment right now (last 24 hours) on {DRIVERS[inst.group]}? "
            f"Quote up to 15 short, representative posts or comments verbatim, each with its source "
            f"and a rough engagement estimate (likes/upvotes/replies).",
            max_searches=self.max_searches,
        )
        if res is None:
            return []
        text, _ = res
        parsed = self.llm.structured("Extract the quoted posts. engagement is an integer estimate (0 if unknown).",
                                     text, _Posts)
        posts = [p.model_dump() for p in parsed.posts] if parsed else []
        self.cache.put(inst.group, posts)
        return posts
