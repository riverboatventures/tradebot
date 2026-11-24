"""Command line: tradebot run | once | kill | resume | check-broker."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .brokers import Broker, BrokerError, PaperBroker
from .config import Settings, load_dotenv, load_settings
from .llm import LLM
from .data import StaticNewsFeed, StaticSentimentFeed, SyntheticMarketData
from .pipeline import KILL_FILE, Pipeline

log = logging.getLogger("tradebot")


def build_brokers(s: Settings) -> list[Broker]:
    if s.mode == "paper":
        return [PaperBroker(s.paper.starting_equity)]
    live = s.mode == "live"
    if live and not s.live_armed:
        sys.exit("mode is 'live' but TRADEBOT_LIVE_ARMED is not set to 'yes-real-money'. Refusing to start.")
    brokers: list[Broker] = []
    if s.brokers.tradovate.enabled:
        from .brokers.tradovate import TradovateBroker
        brokers.append(TradovateBroker(live, s.live_armed, s.brokers.tradovate.account_spec))
    if s.brokers.ninjatrader.enabled:
        from .brokers.ninjatrader import NinjaTraderBroker
        nt = s.brokers.ninjatrader
        brokers.append(NinjaTraderBroker(live, s.live_armed, nt.account, nt.nt_folder, nt.contracts))
    if not brokers:
        sys.exit(f"mode is {s.mode!r} but no broker is enabled under brokers: in settings.yaml")
    return brokers


def build_pipeline(s: Settings) -> Pipeline:
    if s.data.source == "synthetic" and s.mode != "paper":
        sys.exit("data.source is 'synthetic' (fake prices). Set data.source: ninjatrader before demo/live.")
    brokers = build_brokers(s)
    for b in brokers:
        b.connect()
    llm = LLM(s.llm.model, s.llm.enabled)

    if s.data.source == "ninjatrader":
        from .data.ninjatrader import NinjaTraderMarketData
        nt = s.brokers.ninjatrader
        data = NinjaTraderMarketData(nt.nt_folder, nt.contracts, s.session.bar_minutes)
        data.subscribe([sym for sym in s.universe if sym in nt.contracts])
    else:
        data = SyntheticMarketData()

    if s.news.source == "web_search":
        if not llm.available:
            sys.exit("news.source is web_search but Claude isn't available (ANTHROPIC_API_KEY / pip install '.[ai]')")
        from .data.web import WebSearchNewsFeed, WebSearchSentimentFeed
        news = WebSearchNewsFeed(llm, s.news.refresh_minutes, s.news.max_searches, s.timezone)
        sentiment = WebSearchSentimentFeed(llm, s.news.refresh_minutes, s.news.max_searches)
    else:
        news, sentiment = StaticNewsFeed(), StaticSentimentFeed()
    return Pipeline(s, data, news, sentiment, brokers, llm=llm)


def warn_about_settings(s: Settings) -> None:
    if s.risk.daily_loss_limit_pct > 5:
        log.warning("daily_loss_limit_pct is %.0f%%. TITAN will use the stricter prop daily limit if set.",
                    s.risk.daily_loss_limit_pct)
    if s.mode == "live":
        log.warning("LIVE MODE: real-money orders will be placed automatically.")


def main(argv: list[str] | None = None) -> None:
    try:
        _main(argv)
    except BrokerError as e:
        sys.exit(f"Broker error: {e}")


def _main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tradebot")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the desk continuously")
    sub.add_parser("once", help="run one pipeline cycle and exit")
    sub.add_parser("kill", help="kill switch: flatten everything and block new trades")
    sub.add_parser("resume", help="remove the kill switch")
    cb = sub.add_parser("check-broker", help="connect and print account state")
    cb.add_argument("name", choices=["tradovate", "ninjatrader"])
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s")
    load_dotenv()
    s = load_settings(a.config)
    tz = ZoneInfo(s.timezone)

    if a.cmd == "kill":
        KILL_FILE.write_text(datetime.now(tz).isoformat())
        print("Kill switch ON. A running bot flattens all positions on its next cycle.")
        try:
            for b in build_brokers(s):
                b.connect()
                b.flatten_all()
                print(f"  {b.name}: flatten sent")
        except (BrokerError, SystemExit) as e:
            print(f"  direct flatten failed ({e}); the running bot will flatten on its next cycle")
        return
    if a.cmd == "resume":
        KILL_FILE.unlink(missing_ok=True)
        print("Kill switch OFF.")
        return
    if a.cmd == "check-broker":
        getattr(s.brokers, a.name).enabled = True
        if s.mode == "paper":
            s.mode = "demo"
        for b in build_brokers(s):
            if b.name == a.name:
                b.connect()
                eq, pos = b.snapshot()
                print(f"{b.name} ({'LIVE' if b.live else 'demo/sim'}): equity ${eq:,.2f}, positions {pos}")
        return

    warn_about_settings(s)
    pipe = build_pipeline(s)
    if a.cmd == "once":
        pipe.run_once(datetime.now(tz))
        return
    log.info("Desk running in %s mode. Ctrl+C to stop (positions stay open; use `tradebot kill` to flatten).",
             s.mode)
    while True:
        try:
            pipe.run_once(datetime.now(tz))
        except Exception:
            log.exception("cycle failed; continuing")
        time.sleep(s.session.loop_seconds)


if __name__ == "__main__":
    main()
