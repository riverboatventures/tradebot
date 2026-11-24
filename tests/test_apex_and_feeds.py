import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import proposal

from tradebot.account import AccountTracker
from tradebot.agents import Titan
from tradebot.data import DataError
from tradebot.data.ninjatrader import NinjaTraderMarketData
from tradebot.data.web import WebSearchNewsFeed, WebSearchSentimentFeed
from tradebot.models import AccountState, Position, RiskVerdict


@pytest.fixture
def apex(settings):
    p = settings.risk.prop
    p.starting_balance, p.trail_lock_offset, p.trailing_type = 50000, 100, "intraday"
    return settings


def test_apex_floor_trails_then_locks(apex):
    t = Titan(apex)
    st = lambda peak: AccountState(equity=peak, start_of_day_equity=peak, peak_equity=peak)
    assert t.drawdown_floor(st(51000)) == 48500          # still trailing
    assert t.drawdown_floor(st(52600)) == 50100          # reached lock level
    assert t.drawdown_floor(st(60000)) == 50100          # stays locked


def test_titan_uses_locked_floor(apex, midday):
    # Peak 60k, now 50,300: unlocked floor would be 57.5k (blown); locked floor 50.1k + 250 buffer
    acct = AccountState(equity=50300, start_of_day_equity=50300, peak_equity=60000)
    d = Titan(apex).review(proposal(), acct, None, None, midday)
    assert d.verdict == RiskVerdict.reject  # only $-50 room after buffer
    acct = AccountState(equity=50600, start_of_day_equity=50600, peak_equity=60000)
    d = Titan(apex).review(proposal(), acct, None, None, midday)
    assert d.verdict != RiskVerdict.reject and d.risk_dollars <= 250


def test_tracker_intraday_peak_includes_unrealized_high(tmp_path):
    tr = AccountTracker("x", tmp_path, "intraday", starting_balance=50000)
    s = tr.update(50200, [], date(2026, 11, 24), equity_high=50450)
    assert s.peak_equity == 50450
    s = tr.update(50100, [], date(2026, 11, 24))
    assert s.peak_equity == 50450  # never comes down
    # persisted across restarts
    assert AccountTracker("x", tmp_path, "intraday").update(50100, [], date(2026, 11, 24)).peak_equity == 50450


def test_tracker_eod_peak_only_moves_at_day_close(tmp_path):
    tr = AccountTracker("y", tmp_path, "eod", starting_balance=50000)
    assert tr.update(51000, [], date(2026, 11, 24)).peak_equity == 50000   # intraday gain ignored
    tr.update(50800, [], date(2026, 11, 24))                                # closes the day at 50.8k
    assert tr.update(50800, [], date(2026, 11, 25)).peak_equity == 50800


def _write_bars(folder, rows):
    out = folder / "outgoing"
    out.mkdir(exist_ok=True)
    f = out / "tradebot_bars_MES_12-26_5.csv"
    f.write_text("time_utc,open,high,low,close,volume\n" + "".join(
        f"{ts:%Y-%m-%dT%H:%M:%S},{o},{o+1},{o-1},{o+0.5},100\n" for ts, o in rows))
    return f


def test_nt_market_data_drops_forming_bar(tmp_path):
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    _write_bars(tmp_path, [(now - timedelta(minutes=10), 6000), (now - timedelta(minutes=5), 6001),
                           (now + timedelta(minutes=3), 6002)])
    md = NinjaTraderMarketData(str(tmp_path), {"MES": "12-26"}, 5)
    bars = md.bars("MES", 5, 10)
    assert [b.open for b in bars] == [6000, 6001]


def test_nt_market_data_stale_and_missing(tmp_path):
    md = NinjaTraderMarketData(str(tmp_path), {"MES": "12-26"}, 5)
    with pytest.raises(DataError):
        md.bars("MES", 5, 10)
    f = _write_bars(tmp_path, [(datetime(2026, 1, 1), 6000)])
    old = time.time() - 600
    os.utime(f, (old, old))
    with pytest.raises(DataError, match="not updated"):
        md.bars("MES", 5, 10)


def test_nt_subscribe_writes_symbols(tmp_path):
    md = NinjaTraderMarketData(str(tmp_path), {"MES": "12-26", "MCL": "01-27"}, 5)
    md.subscribe(["MES", "MCL"])
    assert (tmp_path / "tradebot" / "symbols.txt").read_text() == "MCL 01-27|5\nMES 12-26|5\n"


class FakeLLM:
    available = True

    def __init__(self):
        self.searches = 0

    def search(self, system, prompt, max_searches=3):
        self.searches += 1
        return "CPI at 08:30 ET, high impact", [
            {"title": "Fed holds rates", "url": "https://www.reuters.com/a", "page_age": None},
            {"title": "Crash coming!!", "url": "https://random-blog.xyz/b", "page_age": None}]

    def structured(self, system, prompt, schema):
        if schema.__name__ == "_Calendar":
            return schema(events=[{"name": "CPI", "time_et": "08:30", "impact": "high"}])
        return schema(posts=[{"text": "longing MES 🚀", "source": "reddit", "engagement": 40}])


def test_web_news_feed_uses_real_urls_and_caches():
    llm = FakeLLM()
    feed = WebSearchNewsFeed(llm)
    h = feed.headlines("MES")
    assert [x["url"] for x in h] == ["https://www.reuters.com/a", "https://random-blog.xyz/b"]
    feed.headlines("MNQ")  # same market group -> cached
    assert llm.searches == 1
    ev = feed.economic_events()
    assert ev[0]["name"] == "CPI" and ev[0]["time"].hour == 8 and ev[0]["impact"] == "high"


def test_web_sentiment_feed():
    posts = WebSearchSentimentFeed(FakeLLM()).posts("MCL")
    assert posts[0]["source"] == "reddit"


def test_backtest_has_no_lookahead_and_runs(settings):
    from tradebot.backtest import HistoricalMarketData, run_backtest, synthetic_history
    bars = synthetic_history(["MES", "MCL"], 3)
    md = HistoricalMarketData(bars, 5)
    md.now = bars["MES"][200].ts
    assert all(b.ts <= md.now for b in md.bars("MES", 5, 500))
    res = run_backtest(settings, bars)
    assert res.stats["trades"] == len(res.trades)
    for t in res.trades:
        assert abs(t["qty"]) <= settings.risk.max_contracts_per_trade


def test_load_ninjatrader_export(tmp_path):
    from tradebot.backtest import load_csv
    f = tmp_path / "MES.txt"
    f.write_text("20261124 143000;6000.25;6001;5999.5;6000.75;1234\n")
    (b,) = load_csv(f)
    assert b.ts.hour == 14 and b.close == 6000.75 and b.volume == 1234


@pytest.fixture
def apex50(settings):
    p = settings.risk.prop
    p.starting_balance, p.trailing_drawdown, p.trail_lock_offset = 50000, 2000, 100
    p.max_contracts, p.micro_weight, p.profit_target = 6, 0.1, 3000
    settings.risk.max_contracts_per_trade = 100
    settings.risk.risk_per_trade_pct = 100  # isolate the contract cap
    return settings


def test_apex_micro_cap_is_60(apex50, midday):
    acct = AccountState(equity=50000, start_of_day_equity=50000, peak_equity=50000)
    apex50.risk.prop.daily_loss_limit = None
    apex50.risk.prop.trailing_drawdown = 10**9
    d = Titan(apex50).review(proposal(quantity=500), acct, None, None, midday)
    assert d.proposal.quantity == 60
    # 25 MNQ open = 2.5 minis used -> 35 micros left
    acct.positions = [Position(symbol="MNQ", quantity=-25, avg_price=21000)]
    d = Titan(apex50).review(proposal(quantity=500), acct, None, None, midday)
    assert d.proposal.quantity == 35


def test_apex_profit_target_stops_trading(apex50, midday):
    acct = AccountState(equity=53000, start_of_day_equity=52500, peak_equity=53000)
    assert "Profit target" in Titan(apex50).trading_halted(acct)
    acct.equity = 52900
    assert Titan(apex50).trading_halted(acct) is None


def test_apex_2000_drawdown_floor(apex50):
    t = Titan(apex50)
    assert t.drawdown_floor(AccountState(equity=50000, start_of_day_equity=50000, peak_equity=50000)) == 48000
    assert t.drawdown_floor(AccountState(equity=52500, start_of_day_equity=52500, peak_equity=52500)) == 50100


def test_tradeify_static_drawdown(settings, midday):
    p = settings.risk.prop
    p.trailing_type, p.starting_balance, p.trailing_drawdown, p.drawdown_buffer = "static", 10000, 500, 100
    p.daily_loss_limit, p.max_contracts, p.micro_weight = 250, 10, 1.0
    t = Titan(settings)
    # floor never moves, no matter how high equity went
    assert t.drawdown_floor(AccountState(equity=10800, start_of_day_equity=10800, peak_equity=10900)) == 9500
    # $9,650 equity -> $50 of room left before TITAN's $9,600 stop
    acct = AccountState(equity=9650, start_of_day_equity=9650, peak_equity=10000)
    d = t.review(proposal(), acct, None, None, midday)  # 1 MES = $26.25
    assert d.proposal.quantity == 1
    acct.equity = 9600
    assert "drawdown" in t.trading_halted(acct)
