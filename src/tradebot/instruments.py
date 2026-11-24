"""Contract specifications for the futures tradebot trades."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    tick_size: float
    tick_value: float  # $ per tick per contract
    micro: bool
    group: str  # used for correlated-exposure checks

    def ticks(self, price_distance: float) -> float:
        return abs(price_distance) / self.tick_size

    def round_price(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 6)

    def dollars(self, price_distance: float, qty: int = 1) -> float:
        return self.ticks(price_distance) * self.tick_value * qty


INSTRUMENTS: dict[str, Instrument] = {
    i.symbol: i
    for i in [
        Instrument("MES", "Micro E-mini S&P 500", 0.25, 1.25, True, "equity_index"),
        Instrument("MNQ", "Micro E-mini Nasdaq-100", 0.25, 0.50, True, "equity_index"),
        Instrument("MYM", "Micro E-mini Dow", 1.0, 0.50, True, "equity_index"),
        Instrument("M2K", "Micro E-mini Russell 2000", 0.10, 0.50, True, "equity_index"),
        Instrument("CL", "Crude Oil", 0.01, 10.0, False, "energy"),
        Instrument("MCL", "Micro Crude Oil", 0.01, 1.0, True, "energy"),
        Instrument("GC", "Gold", 0.10, 10.0, False, "metals"),
        Instrument("MGC", "Micro Gold", 0.10, 1.0, True, "metals"),
    ]
}


def get(symbol: str) -> Instrument:
    try:
        return INSTRUMENTS[symbol]
    except KeyError as e:
        raise KeyError(f"Unknown instrument {symbol!r}; add it to tradebot/instruments.py") from e
