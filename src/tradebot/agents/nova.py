"""NOVA: head trader. Drafts the trade from the team's reports, then makes the
final call (trade or reject) on the plan TITAN signed off on.

NOVA can only say yes or no to TITAN's plan. It cannot change size, stop, or target.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from ..config import Settings
from ..llm import LLM
from ..models import (ChartAnalysis, NewsReport, RiskDecision, RiskVerdict,
                      SentimentReport, TradeDecision, TradeProposal, WatchlistItem)

log = logging.getLogger("NOVA")


class _LlmCall(BaseModel):
    action: Literal["trade", "reject"]
    confidence: float
    reasoning: str


SYSTEM = """You are NOVA, head trader of an intraday futures desk. Your analysts have
reported and the risk manager (TITAN) has already approved a specific plan; you cannot
change its size, stop, or target. Decide only: take this trade, or reject it.
Reject when the analysts conflict in a way that undermines the thesis (e.g. strong news
against the direction, euphoric crowd at resistance) or the setup is weak. A missed trade
costs nothing; a bad trade costs money. Give a confidence 0-1 and 2-3 sentences of reasoning."""


def _blend_confidence(chart: ChartAnalysis, news: NewsReport, mood: SentimentReport) -> tuple[float, list[str]]:
    conf = chart.confidence
    notes = []
    want = "bullish" if chart.setup == "long" else "bearish"
    against = "bearish" if chart.setup == "long" else "bullish"
    if news.net_bias == want:
        conf += 0.05
        notes.append("news agrees")
    elif news.net_bias == against:
        conf -= 0.15
        notes.append("news disagrees")
    # Crowd extremes in OUR direction are a warning (late to the party); against us is fine.
    if (chart.setup == "long" and mood.mood == "euphoric") or (chart.setup == "short" and mood.mood == "fearful"):
        conf -= 0.1
        notes.append(f"crowd {mood.mood} - late entry risk")
    return round(max(0.0, min(1.0, conf)), 3), notes


class Nova:
    def __init__(self, settings: Settings, llm: LLM):
        self.s = settings
        self.llm = llm

    def draft(self, item: WatchlistItem, chart: ChartAnalysis, news: NewsReport,
              mood: SentimentReport) -> TradeProposal | None:
        if not chart.setup:
            return None
        conf, notes = _blend_confidence(chart, news, mood)
        if conf < self.s.nova.min_confidence:
            log.info("%s: pass, blended confidence %.2f < %.2f (%s)", chart.symbol, conf,
                     self.s.nova.min_confidence, ", ".join(notes) or "weak setup")
            return None
        thesis = f"{chart.reasoning} News: {news.net_bias}. Crowd: {mood.mood}/{mood.hype_risk} hype."
        if notes:
            thesis += f" ({', '.join(notes)})"
        return TradeProposal(symbol=chart.symbol, direction=chart.setup, entry=chart.entry, stop=chart.stop,
                             target=chart.target, quantity=0,  # 0 = let TITAN size it
                             confidence=conf, thesis=thesis)

    def decide(self, risk: RiskDecision, chart: ChartAnalysis, news: NewsReport,
               mood: SentimentReport) -> TradeDecision:
        if risk.verdict == RiskVerdict.reject or risk.proposal is None:
            return TradeDecision(action="reject", reasoning="TITAN rejected: " + "; ".join(risk.reasons))
        plan = risk.proposal
        prompt = (
            f"PLAN (approved by TITAN, verdict={risk.verdict.value}): {plan.direction} {plan.quantity} {plan.symbol} "
            f"entry {plan.entry} stop {plan.stop} target {plan.target}, risk ${risk.risk_dollars}, R:R {risk.reward_risk}\n"
            f"TITAN notes: {'; '.join(risk.reasons)}\n\n"
            f"VEGA (chart): {chart.model_dump_json()}\n\n"
            f"ORION (news): {news.model_dump_json()}\n\n"
            f"LUNA (sentiment): {mood.model_dump_json()}"
        )
        call = self.llm.structured(SYSTEM, prompt, _LlmCall)
        if call is None:
            # Rules-only: the draft already passed the confidence bar, so take it.
            d = TradeDecision(action="trade", plan=plan, reasoning=f"Rules-only. {plan.thesis}")
        elif call.action == "trade" and call.confidence >= self.s.nova.min_confidence:
            d = TradeDecision(action="trade", plan=plan, reasoning=call.reasoning)
        else:
            d = TradeDecision(action="reject", reasoning=f"(conf {call.confidence:.2f}) {call.reasoning}")
        log.info("%s %s: %s", d.action.upper(), plan.symbol, d.reasoning)
        return d
