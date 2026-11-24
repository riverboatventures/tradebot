"""Settings loading. All risk numbers come from config/settings.yaml."""
from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

LIVE_ARM_VALUE = "yes-real-money"


class SessionCfg(BaseModel):
    first_entry: time = time(9, 35)
    last_entry: time = time(15, 30)
    flatten_at: time = time(15, 50)
    bar_minutes: int = 5
    higher_tf_minutes: int = 15
    loop_seconds: int = 60

    @field_validator("first_entry", "last_entry", "flatten_at", mode="before")
    @classmethod
    def _parse_time(cls, v):
        if isinstance(v, str):
            h, m = v.split(":")
            return time(int(h), int(m))
        return v


class AtlasCfg(BaseModel):
    watchlist_size: int = 3


class PropCfg(BaseModel):
    enabled: bool = False
    firm: str = ""
    starting_balance: float | None = None   # the account's original balance
    trailing_drawdown: float | None = None
    # intraday: line trails the highest equity incl. unrealized P&L (Apex-style)
    # eod:      line trails the highest end-of-day balance
    trailing_type: Literal["intraday", "eod"] = "intraday"
    # Line stops trailing once it reaches starting_balance + this (Apex: +$100). null = never locks.
    trail_lock_offset: float | None = None
    daily_loss_limit: float | None = None
    # Contract cap in MINI-equivalents. Apex 50K: 6 minis = 60 micros, so a micro weighs 0.1.
    max_contracts: float | None = None
    micro_weight: float = 0.1
    # Evaluation profit target ($ above starting_balance). With stop_at_profit_target, TITAN
    # stops opening trades once it's reached: the eval is passed, no reason to risk giving it back.
    profit_target: float | None = None
    stop_at_profit_target: bool = True
    drawdown_buffer: float = 0.0


class RiskCfg(BaseModel):
    risk_per_trade_pct: float = 1.0
    daily_loss_limit_pct: float = 2.0
    max_trades_per_day: int = 6
    max_open_positions: int = 2
    max_contracts_per_trade: int = 5
    min_reward_risk: float = 1.5
    min_stop_ticks: int = 4
    allow_full_size: bool = False
    hype_risk_size_cut: float = 0.5
    prop: PropCfg = Field(default_factory=PropCfg)


class NovaCfg(BaseModel):
    min_confidence: float = 0.6


class LlmCfg(BaseModel):
    model: str = "claude-opus-5-5"
    enabled: bool = True


class TradovateCfg(BaseModel):
    enabled: bool = False
    account_spec: str = ""


class NinjaTraderCfg(BaseModel):
    enabled: bool = False
    account: str = "Sim101"
    nt_folder: str = ""
    starting_equity: float = 50000.0
    contracts: dict[str, str] = Field(default_factory=dict)


class BrokersCfg(BaseModel):
    tradovate: TradovateCfg = Field(default_factory=TradovateCfg)
    ninjatrader: NinjaTraderCfg = Field(default_factory=NinjaTraderCfg)


class DataCfg(BaseModel):
    source: Literal["synthetic", "ninjatrader"] = "synthetic"


class NewsCfg(BaseModel):
    source: Literal["none", "web_search"] = "none"
    refresh_minutes: int = 15
    max_searches: int = 3


class PaperCfg(BaseModel):
    starting_equity: float = 50000.0


class Settings(BaseModel):
    mode: Literal["paper", "demo", "live"] = "paper"
    timezone: str = "America/New_York"
    universe: list[str] = Field(default_factory=lambda: ["MES", "MNQ"])
    session: SessionCfg = Field(default_factory=SessionCfg)
    atlas: AtlasCfg = Field(default_factory=AtlasCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    nova: NovaCfg = Field(default_factory=NovaCfg)
    llm: LlmCfg = Field(default_factory=LlmCfg)
    brokers: BrokersCfg = Field(default_factory=BrokersCfg)
    paper: PaperCfg = Field(default_factory=PaperCfg)
    data: DataCfg = Field(default_factory=DataCfg)
    news: NewsCfg = Field(default_factory=NewsCfg)

    @property
    def live_armed(self) -> bool:
        return self.mode == "live" and os.environ.get("TRADEBOT_LIVE_ARMED") == LIVE_ARM_VALUE


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Settings.model_validate(data)
