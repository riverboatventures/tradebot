from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tradebot.config import Settings
from tradebot.models import AccountState, TradeProposal

NY = ZoneInfo("America/New_York")


@pytest.fixture
def settings():
    s = Settings.model_validate({
        "universe": ["MES", "MNQ", "MCL", "MGC", "CL"],
        "llm": {"enabled": False},
        "risk": {"risk_per_trade_pct": 1.0, "daily_loss_limit_pct": 20.0,
                 "prop": {"enabled": True, "trailing_drawdown": 2500, "daily_loss_limit": 1000,
                          "max_contracts": 10, "drawdown_buffer": 250}},
    })
    return s


@pytest.fixture
def acct():
    return AccountState(equity=50000, start_of_day_equity=50000, peak_equity=50000)


@pytest.fixture
def midday():
    return datetime(2026, 11, 24, 11, 0, tzinfo=NY)


def proposal(**kw):
    base = dict(symbol="MES", direction="long", entry=6000.0, stop=5995.0, target=6010.0,
                quantity=0, confidence=0.8, thesis="test")
    base.update(kw)
    return TradeProposal(**base)
