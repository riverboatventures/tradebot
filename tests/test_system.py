from datetime import datetime, timedelta

import pytest
from conftest import NY, proposal

from tradebot.agents.orion import Orion, source_verified
from tradebot.brokers import LiveTradingNotArmed, PaperBroker
from tradebot.brokers.ninjatrader import NinjaTraderBroker
from tradebot.data import StaticNewsFeed, StaticSentimentFeed, SyntheticMarketData
from tradebot.llm import LLM
from tradebot.models import Bar
from tradebot.pipeline import Pipeline


def test_source_check():
    assert source_verified("https://www.reuters.com/markets/x")
    assert source_verified("Bloomberg.com")
    assert not source_verified("https://totally-real-news.biz/fed")


def test_orion_blackout():
    t = datetime(2026, 11, 24, 10, 0, tzinfo=NY)
    feed = StaticNewsFeed(events=[{"name": "CPI", "time": t, "impact": "high"}])
    o = Orion(feed, LLM(enabled=False))
    assert o.check_blackout(t - timedelta(minutes=5))[0]
    assert not o.check_blackout(t + timedelta(minutes=30))[0]


def test_live_order_refused_without_arming(tmp_path):
    (tmp_path / "incoming").mkdir()
    b = NinjaTraderBroker(live=True, live_armed=False, account="LIVE1", nt_folder=str(tmp_path),
                          contracts={"MES": "12-26"})
    with pytest.raises(LiveTradingNotArmed):
        b.place_bracket(proposal(quantity=1))
    assert not list((tmp_path / "incoming").iterdir())


def test_ninjatrader_oif(tmp_path):
    (tmp_path / "incoming").mkdir()
    b = NinjaTraderBroker(live=False, live_armed=False, account="Sim101", nt_folder=str(tmp_path),
                          contracts={"MES": "12-26"})
    b.place_bracket(proposal(quantity=2))
    (f,) = list((tmp_path / "incoming").glob("oif*.txt"))
    lines = f.read_text().splitlines()
    assert lines[0].startswith("PLACE;Sim101;MES 12-26;BUY;2;MARKET")
    assert ";SELL;2;STOPMARKET;0;5995.0;" in lines[1]
    assert ";SELL;2;LIMIT;6010.0;0;" in lines[2]


def test_paper_stop_and_target():
    b = PaperBroker(10000, commission_per_side=0)
    b.place_bracket(proposal(quantity=1))
    b.on_bar("MES", Bar(ts=datetime.now(), open=6000, high=6011, low=5999, close=6010, volume=1))
    assert b.closed_trades[-1]["reason"] == "target"
    assert b.closed_trades[-1]["pnl"] == 50.0  # 10pt * $5/pt


def test_pipeline_end_to_end(settings, tmp_path, midday):
    broker = PaperBroker(50000)
    pipe = Pipeline(settings, SyntheticMarketData(), StaticNewsFeed(), StaticSentimentFeed(), [broker],
                    state_dir=str(tmp_path))
    pipe.run_once(midday)
    journal = list(tmp_path.glob("journal_*.jsonl"))
    assert journal and "ATLAS" in journal[0].read_text()
    for t in broker.brackets.values():
        assert abs(t["qty"]) <= settings.risk.max_contracts_per_trade
