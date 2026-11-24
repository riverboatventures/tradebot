"""ORION: news analyst. Outputs catalysts, source check, and a news blackout flag."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

from ..data import NewsFeed
from ..llm import LLM
from ..models import Catalyst, NewsReport

log = logging.getLogger("ORION")

# Sources ORION treats as verified. Anything else is flagged unverified.
TRUSTED_DOMAINS = {
    "reuters.com", "bloomberg.com", "wsj.com", "cnbc.com", "apnews.com", "ft.com",
    "marketwatch.com", "federalreserve.gov", "bls.gov", "eia.gov", "cmegroup.com",
    "treasury.gov", "bea.gov", "opec.org", "kitco.com",
}

BLACKOUT_BEFORE = timedelta(minutes=10)
BLACKOUT_AFTER = timedelta(minutes=15)

_BULL = ("beat", "surge", "rally", "cut rates", "rate cut", "stimulus", "record high", "cooler", "draw")
_BEAR = ("miss", "plunge", "selloff", "hike", "hotter", "recession", "tariff", "war", "build", "downgrade")


def source_verified(url_or_source: str) -> bool:
    host = urlparse(url_or_source).netloc or url_or_source
    host = host.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS)


class _LlmCatalyst(BaseModel):
    index: int
    impact: Literal["low", "medium", "high"]
    bias: Literal["bullish", "bearish", "neutral"]
    note: str


class _LlmNews(BaseModel):
    catalysts: list[_LlmCatalyst]
    net_bias: Literal["bullish", "bearish", "neutral"]
    summary: str


SYSTEM = """You are ORION, the news analyst on a futures trading desk.
For the given futures contract, judge each headline's likely market impact (low/medium/high)
and directional bias for THIS contract over the next few hours. Be skeptical: headlines from
unverified sources, rumors, and stale news get low impact. Never invent headlines.
Return the index of each headline you rate, an overall net bias, and a 1-2 sentence summary."""


class Orion:
    def __init__(self, feed: NewsFeed, llm: LLM):
        self.feed = feed
        self.llm = llm

    def check_blackout(self, now: datetime) -> tuple[bool, str]:
        for ev in self.feed.economic_events():
            if ev.get("impact") != "high":
                continue
            t = ev["time"]
            if t - BLACKOUT_BEFORE <= now <= t + BLACKOUT_AFTER:
                return True, f"High-impact event: {ev['name']} at {t:%H:%M}"
        return False, ""

    def analyze(self, symbol: str, now: datetime) -> NewsReport:
        raw = self.feed.headlines(symbol)
        catalysts = []
        for h in raw:
            src = h.get("url") or h.get("source", "")
            ok = source_verified(src)
            catalysts.append(Catalyst(
                headline=h["headline"], source=h.get("source", src), published_at=h.get("published_at"),
                source_verified=ok,
                verification_note="trusted outlet" if ok else "UNVERIFIED source - treat as rumor",
            ))
        blackout, why = self.check_blackout(now)
        report = NewsReport(symbol=symbol, catalysts=catalysts, blackout=blackout, blackout_reason=why)
        if not catalysts:
            report.summary = "No fresh headlines."
            return report

        listing = "\n".join(f"[{i}] ({'verified' if c.source_verified else 'UNVERIFIED'}: {c.source}) {c.headline}"
                            for i, c in enumerate(catalysts))
        res = self.llm.structured(SYSTEM, f"Contract: {symbol}\nHeadlines:\n{listing}", _LlmNews)
        if res:
            for rc in res.catalysts:
                if 0 <= rc.index < len(catalysts):
                    c = catalysts[rc.index]
                    # Unverified sources can never be rated high impact, whatever the AI says.
                    c.impact = rc.impact if c.source_verified or rc.impact != "high" else "medium"
                    c.bias = rc.bias
            report.net_bias, report.summary = res.net_bias, res.summary
        else:
            score = 0
            for c in catalysts:
                text = c.headline.lower()
                b = sum(k in text for k in _BULL) - sum(k in text for k in _BEAR)
                c.bias = "bullish" if b > 0 else "bearish" if b < 0 else "neutral"
                c.impact = "medium" if c.source_verified and b else "low"
                score += b * (2 if c.source_verified else 1)
            report.net_bias = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
            report.summary = f"Rules-only keyword read across {len(catalysts)} headlines."
        log.info("%s: bias=%s blackout=%s %s", symbol, report.net_bias, blackout, report.summary)
        return report
