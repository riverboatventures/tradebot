"""Backtester: replays historical bars through the real desk (ATLAS, VEGA, NOVA, TITAN)
with the paper broker, one bar at a time, with no look-ahead.

News and sentiment aren't replayed (there's no historical web search), so ORION and LUNA
are neutral and NOVA runs rules-only. That means this measures the chart strategy and
TITAN's risk rules, which is exactly what we want to validate first.

Bar data: CSV with header time_utc,open,high,low,close,volume (bar CLOSE time, UTC), or
NinjaTrader's export format (Tools > Historical Data > Export, "yyyyMMdd HHmmss;o;h;l;c;v").
"""
from __future__ import annotations

import bisect
import csv
import logging
import math
import random
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .agents import Titan
from .brokers import PaperBroker
from .config import Settings
from .data import DataError, MarketData, StaticNewsFeed, StaticSentimentFeed
from .instruments import get as get_instrument
from .journal import Journal
from .llm import LLM
from .models import AccountState, Bar
from .pipeline import Pipeline

log = logging.getLogger("backtest")


class HistoricalMarketData(MarketData):
    """Serves only bars that had closed by the current replay time."""

    def __init__(self, bars: dict[str, list[Bar]], bar_minutes: int):
        self.series = {s: sorted(b, key=lambda x: x.ts) for s, b in bars.items()}
        self.times = {s: [b.ts for b in bs] for s, bs in self.series.items()}
        self.bar_minutes = bar_minutes
        self.now = datetime.min.replace(tzinfo=timezone.utc)

    def bars(self, symbol: str, minutes: int, count: int) -> list[Bar]:
        if symbol not in self.series:
            raise DataError(f"no history for {symbol}")
        if minutes != self.bar_minutes:
            raise DataError(f"history is {self.bar_minutes}m bars")
        end = bisect.bisect_right(self.times[symbol], self.now)
        if end == 0:
            raise DataError(f"no {symbol} bars before {self.now}")
        if self.now - self.times[symbol][end - 1] > timedelta(minutes=3 * minutes):
            raise DataError(f"{symbol} has no recent bar (market closed)")
        return self.series[symbol][max(0, end - count):end]


def load_csv(path: str | Path) -> list[Bar]:
    text = Path(path).read_text().strip().splitlines()
    out = []
    if text and text[0].startswith("time_utc"):
        for row in csv.DictReader(text):
            out.append(Bar(ts=datetime.fromisoformat(row["time_utc"]).replace(tzinfo=timezone.utc),
                           open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                           close=float(row["close"]), volume=float(row["volume"])))
    else:  # NinjaTrader export: yyyyMMdd HHmmss;open;high;low;close;volume (exported in UTC)
        for line in text:
            p = line.split(";")
            if len(p) < 6:
                continue
            out.append(Bar(ts=datetime.strptime(p[0], "%Y%m%d %H%M%S").replace(tzinfo=timezone.utc),
                           open=float(p[1]), high=float(p[2]), low=float(p[3]), close=float(p[4]),
                           volume=float(p[5])))
    return out


def synthetic_history(symbols: list[str], days: int, minutes: int = 5, seed: int = 1) -> dict[str, list[Bar]]:
    """Random-walk weekday history, for trying the backtester out. Not real market behavior."""
    base = {"MES": 6000.0, "MNQ": 21000.0, "MYM": 44000.0, "M2K": 2300.0,
            "CL": 70.0, "MCL": 70.0, "GC": 2700.0, "MGC": 2700.0}
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    out = {}
    for sym in symbols:
        inst, rng = get_instrument(sym), random.Random(f"{seed}-{sym}")
        price, bars, t = base.get(sym, 100.0), [], end - timedelta(days=days * 7 // 5 + 2)
        drift = 0.0
        while t < end:
            t += timedelta(minutes=minutes)
            if t.weekday() >= 5:
                continue
            if rng.random() < 0.02:
                drift = rng.gauss(0, 0.00015)  # regime change: mild trend or chop
            o = price
            c = o * (1 + drift) + rng.gauss(0, o * 0.0012)
            h, l = max(o, c) + abs(rng.gauss(0, o * 0.0005)), min(o, c) - abs(rng.gauss(0, o * 0.0005))
            bars.append(Bar(ts=t, open=inst.round_price(o), high=inst.round_price(h), low=inst.round_price(l),
                            close=inst.round_price(c), volume=round(rng.uniform(500, 1500))))
            price = c
        out[sym] = bars
    return out


@dataclass
class BacktestResult:
    trades: list[dict]
    equity_curve: list[tuple[datetime, float]]
    starting_equity: float
    prop_breached_at: datetime | None = None
    stats: dict = field(default_factory=dict)

    def report(self) -> str:
        s = self.stats
        lines = [
            f"Period            {s['start']:%Y-%m-%d} -> {s['end']:%Y-%m-%d} ({s['days']} trading days)",
            f"Trades            {s['trades']}  (wins {s['wins']}, losses {s['losses']}, win rate {s['win_rate']:.0%})",
            f"Net P&L           ${s['net_pnl']:,.2f}  ({s['return_pct']:+.2f}%)",
            f"Profit factor     {s['profit_factor']:.2f}",
            f"Avg win / loss    ${s['avg_win']:,.2f} / ${s['avg_loss']:,.2f}",
            f"Max drawdown      ${s['max_drawdown']:,.2f}",
            f"Worst day         ${s['worst_day']:,.2f}",
            f"Exits             {s['exits']}",
        ]
        if s.get("prop"):
            lines.append(f"Prop account      {'BREACHED on ' + f'{self.prop_breached_at:%Y-%m-%d %H:%M}' if self.prop_breached_at else 'survived (never touched the drawdown line)'}")
        return "\n".join(lines)


def run_backtest(settings: Settings, bars: dict[str, list[Bar]]) -> BacktestResult:
    s = settings.model_copy(deep=True)
    s.universe = [u for u in s.universe if u in bars]
    s.llm.enabled = False
    tz = ZoneInfo(s.timezone)
    data = HistoricalMarketData(bars, s.session.bar_minutes)
    start_eq = s.risk.prop.starting_balance if s.risk.prop.enabled and s.risk.prop.starting_balance \
        else s.paper.starting_equity
    broker = PaperBroker(start_eq)
    titan = Titan(s)
    timeline = sorted({b.ts for bs in bars.values() for b in bs})

    curve: list[tuple[datetime, float]] = []
    breached_at = None
    peak = start_eq
    prop = s.risk.prop
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.setLevel(logging.WARNING)  # agents are chatty; keep the report readable
    try:
        with tempfile.TemporaryDirectory() as tmp:
            pipe = Pipeline(s, data, StaticNewsFeed(), StaticSentimentFeed(), [broker],
                            journal=Journal(tmp), state_dir=tmp, llm=LLM(enabled=False))
            for ts in timeline:
                data.now = ts
                pipe.run_once(ts.astimezone(tz))
                eq, _ = broker.snapshot()
                curve.append((ts, eq))
                peak = max(peak, eq)
                if prop.enabled and prop.trailing_drawdown and breached_at is None:
                    floor = titan.drawdown_floor(AccountState(equity=eq, start_of_day_equity=eq, peak_equity=peak))
                    if eq <= floor:
                        breached_at = ts
    finally:
        root_logger.setLevel(prev_level)

    res = BacktestResult(trades=broker.closed_trades, equity_curve=curve, starting_equity=start_eq,
                         prop_breached_at=breached_at)
    res.stats = _stats(res, tz, prop.enabled)
    return res


def _stats(r: BacktestResult, tz: ZoneInfo, prop: bool) -> dict:
    pnls = [t["pnl"] for t in r.trades]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p <= 0]
    peak, max_dd = r.starting_equity, 0.0
    daily: dict = {}
    for ts, eq in r.equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        daily.setdefault(ts.astimezone(tz).date(), [eq, eq])[1] = eq
    day_pnls = [end - start for start, end in daily.values()]
    exits: dict[str, int] = {}
    for t in r.trades:
        exits[t["reason"]] = exits.get(t["reason"], 0) + 1
    final = r.equity_curve[-1][1] if r.equity_curve else r.starting_equity
    return {
        "start": r.equity_curve[0][0] if r.equity_curve else None,
        "end": r.equity_curve[-1][0] if r.equity_curve else None,
        "days": len(daily),
        "trades": len(pnls), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "net_pnl": final - r.starting_equity,
        "return_pct": (final - r.starting_equity) / r.starting_equity * 100,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else (math.inf if wins else 0.0),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "max_drawdown": max_dd,
        "worst_day": min(day_pnls) if day_pnls else 0.0,
        "exits": exits,
        "prop": prop,
    }
