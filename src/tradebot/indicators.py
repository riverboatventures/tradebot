"""Plain-Python indicators (no numpy needed). Deterministic, used by ATLAS and VEGA."""
from __future__ import annotations

from collections.abc import Sequence

from .models import Bar


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            trs.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return trs


def atr(bars: Sequence[Bar], period: int = 14) -> float:
    trs = true_ranges(bars)
    if not trs:
        return 0.0
    window = trs[-period:]
    return sum(window) / len(window)


def vwap(bars: Sequence[Bar]) -> float:
    vol = sum(b.volume for b in bars)
    if vol == 0:
        return bars[-1].close if bars else 0.0
    return sum(((b.high + b.low + b.close) / 3) * b.volume for b in bars) / vol


def swing_points(bars: Sequence[Bar], left: int = 3, right: int = 3) -> tuple[list[float], list[float]]:
    """Return (swing_highs, swing_lows) as price levels, oldest first."""
    highs, lows = [], []
    for i in range(left, len(bars) - right):
        h = bars[i].high
        l = bars[i].low
        if all(h >= bars[j].high for j in range(i - left, i + right + 1)):
            highs.append(h)
        if all(l <= bars[j].low for j in range(i - left, i + right + 1)):
            lows.append(l)
    return highs, lows


def resample(bars: Sequence[Bar], factor: int) -> list[Bar]:
    """Combine every `factor` bars into one (e.g. 5m -> 15m with factor 3)."""
    out = []
    for i in range(0, len(bars) - factor + 1, factor):
        chunk = bars[i : i + factor]
        out.append(
            Bar(
                ts=chunk[0].ts,
                open=chunk[0].open,
                high=max(b.high for b in chunk),
                low=min(b.low for b in chunk),
                close=chunk[-1].close,
                volume=sum(b.volume for b in chunk),
            )
        )
    return out


def trend_strength(closes: Sequence[float], fast: int = 9, slow: int = 21) -> float:
    """Signed, normalized gap between fast and slow EMA. >0 uptrend, <0 downtrend."""
    if len(closes) < slow:
        return 0.0
    f = ema(closes, fast)[-1]
    s = ema(closes, slow)[-1]
    return (f - s) / closes[-1] * 100
