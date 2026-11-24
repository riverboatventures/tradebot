"""LUNA: sentiment analyst. Outputs crowd mood and hype risk."""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from ..data import SentimentFeed
from ..llm import LLM
from ..models import SentimentReport

log = logging.getLogger("LUNA")

_BULL = ("moon", "long", "calls", "bull", "breakout", "rip", "buy", "squeeze", "🚀")
_BEAR = ("short", "puts", "bear", "crash", "dump", "sell", "collapse", "rug")
_HYPE = ("🚀", "moon", "can't lose", "all in", "guaranteed", "100x", "yolo", "to the moon")


class _LlmSentiment(BaseModel):
    mood: Literal["fearful", "bearish", "neutral", "bullish", "euphoric"]
    mood_score: float
    hype_risk: Literal["low", "medium", "high"]
    summary: str


SYSTEM = """You are LUNA, the sentiment analyst on a futures trading desk.
Read the crowd chatter about the given contract and report the crowd's mood
(fearful/bearish/neutral/bullish/euphoric), a mood_score from -1 (extreme fear) to +1
(extreme greed), and hype_risk: how likely the crowd is overextended and due to reverse
(one-sided euphoria or panic, "can't lose" language, sudden chatter spikes = high).
Summarize in 1-2 sentences. Base everything only on the posts given."""


class Luna:
    def __init__(self, feed: SentimentFeed, llm: LLM):
        self.feed = feed
        self.llm = llm

    def analyze(self, symbol: str) -> SentimentReport:
        posts = self.feed.posts(symbol)
        if not posts:
            return SentimentReport(symbol=symbol, summary="No crowd chatter available.")

        listing = "\n".join(f"- ({p.get('source', '?')}, engagement {p.get('engagement', 0)}) {p['text']}"
                            for p in posts[:100])
        res = self.llm.structured(SYSTEM, f"Contract: {symbol}\nPosts:\n{listing}", _LlmSentiment)
        if res:
            r = SentimentReport(symbol=symbol, mood=res.mood, mood_score=max(-1.0, min(1.0, res.mood_score)),
                                hype_risk=res.hype_risk, summary=res.summary)
        else:
            total_w, score, hype = 0.0, 0.0, 0
            for p in posts:
                t = p["text"].lower()
                w = 1 + (p.get("engagement", 0) or 0) / 100
                score += w * (sum(k in t for k in _BULL) - sum(k in t for k in _BEAR))
                total_w += w
                hype += any(k in t for k in _HYPE)
            s = max(-1.0, min(1.0, score / max(total_w, 1)))
            mood = ("euphoric" if s > 0.6 else "bullish" if s > 0.2 else "fearful" if s < -0.6
                    else "bearish" if s < -0.2 else "neutral")
            hype_ratio = hype / len(posts)
            risk = "high" if hype_ratio > 0.3 or abs(s) > 0.8 else "medium" if hype_ratio > 0.1 or abs(s) > 0.5 else "low"
            r = SentimentReport(symbol=symbol, mood=mood, mood_score=round(s, 3), hype_risk=risk,
                                summary=f"Rules-only read of {len(posts)} posts.")
        log.info("%s: mood=%s (%.2f) hype=%s", symbol, r.mood, r.mood_score, r.hype_risk)
        return r
