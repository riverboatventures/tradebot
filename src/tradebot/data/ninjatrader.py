"""Market data from NinjaTrader via the TradebotBridge AddOn.

tradebot writes the list of contracts it wants to <NT folder>/tradebot/symbols.txt.
The AddOn subscribes to each one and keeps outgoing/tradebot_bars_<instrument>_<minutes>.csv
updated (UTC close time, open, high, low, close, volume).
"""
from __future__ import annotations

import csv
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ..models import Bar
from . import DataError, MarketData

log = logging.getLogger("nt-data")

STALE_AFTER_S = 120


class NinjaTraderMarketData(MarketData):
    def __init__(self, nt_folder: str, contracts: dict[str, str], bar_minutes: int):
        self.root = Path(nt_folder)
        self.contracts = contracts
        self.bar_minutes = bar_minutes

    def instrument(self, symbol: str) -> str:
        if symbol not in self.contracts:
            raise DataError(f"No NinjaTrader contract month configured for {symbol}")
        return f"{symbol} {self.contracts[symbol]}"

    def subscribe(self, symbols: list[str]) -> None:
        cfg = self.root / "tradebot" / "symbols.txt"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        lines = sorted(f"{self.instrument(s)}|{self.bar_minutes}" for s in symbols)
        if not cfg.exists() or cfg.read_text().splitlines() != lines:
            cfg.write_text("\n".join(lines) + "\n")
            log.info("Requested NinjaTrader bars for: %s", ", ".join(lines))

    def bars(self, symbol: str, minutes: int, count: int) -> list[Bar]:
        if minutes != self.bar_minutes:
            raise DataError(f"NinjaTrader feed is {self.bar_minutes}m; resample instead of requesting {minutes}m")
        name = self.instrument(symbol).replace(" ", "_")
        f = self.root / "outgoing" / f"tradebot_bars_{name}_{minutes}.csv"
        if not f.exists():
            raise DataError(f"{f.name} missing. Is the TradebotBridge AddOn running?")
        if time.time() - f.stat().st_mtime > STALE_AFTER_S:
            raise DataError(f"{f.name} not updated for >{STALE_AFTER_S}s (market closed or NinjaTrader disconnected)")
        now = datetime.now(timezone.utc)
        out = []
        with open(f, newline="") as fh:
            for row in csv.DictReader(fh):
                ts = datetime.fromisoformat(row["time_utc"]).replace(tzinfo=timezone.utc)
                if ts > now:
                    continue  # bar still forming; NinjaTrader stamps bars with their close time
                out.append(Bar(ts=ts, open=float(row["open"]), high=float(row["high"]),
                               low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"])))
        return out[-count:]
