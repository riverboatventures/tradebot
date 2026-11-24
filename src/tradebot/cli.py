"""Command line: tradebot run | once | kill | resume | check-broker."""
from __future__ import annotations

import argparse
import os
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


def build_brokers(s: Settings, require_armed: bool = True) -> list[Broker]:
    if s.mode == "paper":
        return [PaperBroker(s.paper.starting_equity)]
    live = s.mode == "live"
    if live and require_armed and not s.live_armed:
        sys.exit("mode is 'live' but TRADEBOT_LIVE_ARMED is not set to 'yes-real-money'. Refusing to start.")
    brokers: list[Broker] = []
    if s.brokers.tradovate.enabled:
        from .brokers.tradovate import TradovateBroker
        brokers.append(TradovateBroker(live, s.live_armed, s.brokers.tradovate.account_spec))
    if s.brokers.ninjatrader.enabled:
        from .brokers.ninjatrader import NinjaTraderBroker
        nt = s.brokers.ninjatrader
        for acct in nt.accounts:
            if acct.live and live and acct.name.startswith("YOUR-"):
                sys.exit("Set brokers.ninjatrader.accounts[].name to your real live account name first.")
            if acct.live and not live:
                log.info("Skipping live account %s (mode is %s)", acct.name, s.mode)
                continue
            brokers.append(NinjaTraderBroker(acct.live, s.live_armed, acct.name, nt.nt_folder, nt.contracts,
                                             bot_only=acct.bot_only))
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
        if s.risk.prop.enabled and s.risk.prop.daily_loss_limit:
            log.warning("daily_loss_limit_pct is %.0f%%; the stricter prop daily limit ($%.0f) applies.",
                        s.risk.daily_loss_limit_pct, s.risk.prop.daily_loss_limit)
        else:
            log.warning("daily_loss_limit_pct is %.0f%% and no prop limit is set: the bot may lose up to "
                        "%.0f%% of each account in one day before stopping.",
                        s.risk.daily_loss_limit_pct, s.risk.daily_loss_limit_pct)
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
    bt = sub.add_parser("backtest", help="replay historical bars through the desk")
    bt.add_argument("--csv", action="append", default=[], metavar="SYMBOL=PATH",
                    help="historical 5m bars for a symbol, e.g. --csv MES=data/MES.csv (repeatable)")
    bt.add_argument("--synthetic-days", type=int, default=0,
                    help="use N days of random synthetic bars instead (for trying it out)")
    bt.add_argument("--trades", action="store_true", help="also print every trade")
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
    if a.cmd == "backtest":
        from .backtest import load_csv, run_backtest, synthetic_history
        if a.synthetic_days:
            bars = synthetic_history(s.universe, a.synthetic_days, s.session.bar_minutes)
            print("NOTE: synthetic random-walk data. Results say nothing about real markets.\n")
        elif a.csv:
            bars = {}
            for spec in a.csv:
                sym, _, path = spec.partition("=")
                bars[sym.upper()] = load_csv(path)
        else:
            sys.exit("give --csv SYMBOL=PATH (repeatable) or --synthetic-days N")
        res = run_backtest(s, bars)
        print(res.report())
        if a.trades:
            for t in res.trades:
                print(f"  {t['symbol']:4} {t['qty']:+d} @ {t['entry']} -> {t['exit']} ({t['reason']}) ${t['pnl']:,.2f}")
        return
    if a.cmd == "check-broker":
        # Read-only: builds every account (live ones too) but never places orders.
        getattr(s.brokers, a.name).enabled = True
        s.mode = "live"
        os.environ.pop("TRADEBOT_LIVE_ARMED", None)  # guarantees no live order can go out from here
        for b in build_brokers(s, require_armed=False):
            if b.name.startswith(a.name):
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
