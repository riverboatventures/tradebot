from datetime import datetime

from conftest import NY, proposal

from tradebot.agents import Titan
from tradebot.models import AccountState, NewsReport, Position, RiskVerdict, SentimentReport


def test_sizes_to_one_percent(settings, acct, midday):
    # MES 5pt stop = 20 ticks + 1 slippage = 21 * $1.25 = $26.25/contract; 1% of 50k = $500 -> 19, capped at 5
    d = Titan(settings).review(proposal(), acct, None, None, midday)
    assert d.verdict == RiskVerdict.revise or d.verdict == RiskVerdict.approve
    assert d.proposal.quantity == 5


def test_size_cut_is_revise(settings, acct, midday):
    d = Titan(settings).review(proposal(quantity=50), acct, None, None, midday)
    assert d.verdict == RiskVerdict.revise
    assert d.proposal.quantity == 5


def test_prop_daily_limit_is_stricter_than_20_percent(settings, midday):
    t = Titan(settings)
    acct = AccountState(equity=49000, start_of_day_equity=50000, peak_equity=50000)
    assert t.daily_loss_limit(acct) == 1000  # not $10,000
    d = t.review(proposal(), acct, None, None, midday)
    assert d.verdict == RiskVerdict.reject
    assert "Daily loss limit" in d.reasons[0]


def test_trailing_drawdown_buffer(settings, midday):
    # peak 52k, trailing 2.5k -> fail at 49.5k; buffer 250 -> TITAN stops at 49.75k
    acct = AccountState(equity=49700, start_of_day_equity=49700, peak_equity=52000)
    d = Titan(settings).review(proposal(), acct, None, None, midday)
    assert d.verdict == RiskVerdict.reject


def test_drawdown_room_limits_size(settings, midday):
    # room = 50000 - (52000-2500) - 250 = 250 -> 250 // 26.25 = 9 -> capped 5; use bigger stop
    acct = AccountState(equity=50000, start_of_day_equity=50000, peak_equity=52000)
    d = Titan(settings).review(proposal(stop=5980.0, target=6040.0), acct, None, None, midday)
    # 20pt stop = 80+1 ticks * 1.25 = 101.25/contract -> 250 // 101.25 = 2
    assert d.proposal.quantity == 2


def test_rejects_bad_geometry(settings, acct, midday):
    t = Titan(settings)
    assert t.review(proposal(stop=6005.0), acct, None, None, midday).verdict == RiskVerdict.reject
    assert t.review(proposal(target=6004.0), acct, None, None, midday).verdict == RiskVerdict.reject  # R:R 0.8
    assert t.review(proposal(stop=5999.5), acct, None, None, midday).verdict == RiskVerdict.reject  # 2 ticks


def test_rejects_outside_hours(settings, acct):
    t = Titan(settings)
    for hh, mm in [(9, 0), (15, 45), (20, 0)]:
        d = t.review(proposal(), acct, None, None, datetime(2026, 11, 24, hh, mm, tzinfo=NY))
        assert d.verdict == RiskVerdict.reject


def test_rejects_full_size_when_disallowed(settings, acct, midday):
    d = Titan(settings).review(proposal(symbol="CL", entry=70.0, stop=69.8, target=70.5), acct, None, None, midday)
    assert d.verdict == RiskVerdict.reject


def test_news_blackout(settings, acct, midday):
    news = NewsReport(symbol="MES", blackout=True, blackout_reason="CPI")
    assert Titan(settings).review(proposal(), acct, news, None, midday).verdict == RiskVerdict.reject


def test_hype_risk_halves_size(settings, acct, midday):
    mood = SentimentReport(symbol="MES", hype_risk="high")
    d = Titan(settings).review(proposal(), acct, None, mood, midday)
    assert d.proposal.quantity == 2


def test_correlated_exposure(settings, midday):
    acct = AccountState(equity=50000, start_of_day_equity=50000, peak_equity=50000,
                        positions=[Position(symbol="MNQ", quantity=2, avg_price=21000)])
    d = Titan(settings).review(proposal(), acct, None, None, midday)
    assert d.verdict == RiskVerdict.reject and "correlated" in d.reasons[0]
    # opposite direction in the same group is allowed (hedge)
    d = Titan(settings).review(proposal(direction="short", stop=6005.0, target=5990.0), acct, None, None, midday)
    assert d.verdict != RiskVerdict.reject


def test_max_trades_per_day(settings, midday):
    acct = AccountState(equity=50000, start_of_day_equity=50000, peak_equity=50000, trades_today=6)
    assert Titan(settings).review(proposal(), acct, None, None, midday).verdict == RiskVerdict.reject


def test_open_risk_reduces_daily_room(settings, midday):
    # $1,000 daily stop, already down $700 with $250 riding on an open MNQ trade -> $50 left
    acct = AccountState(equity=49300, start_of_day_equity=50000, peak_equity=50000,
                        positions=[Position(symbol="MCL", quantity=1, avg_price=70)], open_risk=250)
    d = Titan(settings).review(proposal(), acct, None, None, midday)  # 1 MES = $26.25
    assert d.verdict != RiskVerdict.reject and d.risk_dollars <= 50
    acct.open_risk = 290
    assert Titan(settings).review(proposal(), acct, None, None, midday).verdict == RiskVerdict.reject
