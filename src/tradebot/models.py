"""The data each agent produces. These are the contracts between agents."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Bar(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


Direction = Literal["long", "short"]


# ---- ATLAS ---------------------------------------------------------------
class WatchlistItem(BaseModel):
    symbol: str
    rank: int
    score: float
    atr_pct: float
    rel_volume: float
    trend_strength: float
    notes: str = ""


class Watchlist(BaseModel):
    generated_at: datetime
    items: list[WatchlistItem]


# ---- VEGA ----------------------------------------------------------------
class Trend(str, Enum):
    up = "up"
    down = "down"
    range = "range"


class ChartAnalysis(BaseModel):
    symbol: str
    last_price: float
    trend: Trend
    higher_tf_trend: Trend
    atr: float
    vwap: float
    support: list[float]
    resistance: list[float]
    setup: Direction | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    confidence: float = 0.0
    reasoning: str = ""


# ---- ORION ---------------------------------------------------------------
class Catalyst(BaseModel):
    headline: str
    source: str
    published_at: datetime | None = None
    impact: Literal["low", "medium", "high"] = "low"
    bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    source_verified: bool = False
    verification_note: str = ""


class NewsReport(BaseModel):
    symbol: str
    catalysts: list[Catalyst] = Field(default_factory=list)
    net_bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    blackout: bool = False  # high-impact scheduled event imminent -> no new trades
    blackout_reason: str = ""
    summary: str = ""


# ---- LUNA ----------------------------------------------------------------
class SentimentReport(BaseModel):
    symbol: str
    mood: Literal["fearful", "bearish", "neutral", "bullish", "euphoric"] = "neutral"
    mood_score: float = 0.0  # -1 (extreme fear) .. +1 (extreme greed)
    hype_risk: Literal["low", "medium", "high"] = "low"
    summary: str = ""


# ---- NOVA (draft) -> TITAN -> NOVA (final) --------------------------------
class TradeProposal(BaseModel):
    symbol: str
    direction: Direction
    entry: float
    stop: float
    target: float
    quantity: int
    confidence: float
    thesis: str


class RiskVerdict(str, Enum):
    approve = "approve"
    revise = "revise"
    reject = "reject"


class RiskDecision(BaseModel):
    verdict: RiskVerdict
    proposal: TradeProposal | None  # the (possibly revised) plan TITAN signs off on
    reasons: list[str] = Field(default_factory=list)
    risk_dollars: float = 0.0
    reward_risk: float = 0.0


class TradeDecision(BaseModel):
    action: Literal["trade", "reject"]
    plan: TradeProposal | None = None
    reasoning: str = ""


# ---- Broker --------------------------------------------------------------
class Position(BaseModel):
    symbol: str
    quantity: int  # +long / -short
    avg_price: float


class AccountState(BaseModel):
    equity: float
    start_of_day_equity: float
    peak_equity: float
    realized_pnl_today: float = 0.0
    open_pnl: float = 0.0
    trades_today: int = 0
    positions: list[Position] = Field(default_factory=list)

    @property
    def day_pnl(self) -> float:
        return self.equity - self.start_of_day_equity


class OrderResult(BaseModel):
    ok: bool
    broker: str
    order_id: str | None = None
    message: str = ""
